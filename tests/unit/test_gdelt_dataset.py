"""Synthetic G1-compatible DTO replay; no live source, DB, training, or trading."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from ai_futures.gdelt_dataset import (
    DatasetConfig,
    MarketCandle,
    VolumeRevision,
    VolumeScan,
    build_dataset,
    chronological_split,
    verify_dataset,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
BUCKET = timedelta(minutes=15)
SOURCE = "gdelt-doc-timelinevol-bitcoin-v1"


def at(hours):
    return START + hours * HOUR


def market(count=8):
    return tuple(
        MarketCandle(
            event_id=f"market-{i}",
            window_start=at(4 * i),
            source_time=at(4 * i + 4),
            received_at=at(4 * i + 4),
            close=str(100 + i),
            payload_hash=f"{i + 1:064x}",
            source="synthetic-market",
            config_version="fixture-v1",
        )
        for i in range(count)
    )


def volume(last_hour=24):
    revisions, scans = [], []
    for i in range(-64, int(last_hour * 4)):
        point = START + i * BUCKET
        observed = point + BUCKET
        scan_id = f"scan-{i}"
        scans.append(
            VolumeScan(
                scan_id,
                observed,
                "ok" if i >= 31 else "gap",
                96 if i >= 31 else i + 65,
                0 if i >= 31 else 96 - (i + 65),
                SOURCE,
                "fixture-v1",
            )
        )
        revisions.append(
            VolumeRevision(
                f"revision-{i}",
                scan_id,
                point,
                observed,
                observed,
                1,
                "0" if i % 5 == 0 else "0.125",
                SOURCE,
                "fixture-v1",
            )
        )
    return tuple(revisions), tuple(scans)


def config(last=12):
    return DatasetConfig(at(8), at(last), Decimal("10"), "synthetic-cost-v1")


def build(*, candles=None, revisions=None, scans=None, cutoff=None, cfg=None):
    base_revisions, base_scans = volume()
    return build_dataset(
        market() if candles is None else candles,
        base_revisions if revisions is None else revisions,
        base_scans if scans is None else scans,
        cutoff=at(28) if cutoff is None else cutoff,
        config=config() if cfg is None else cfg,
    )


def test_repeatable_market_only_and_numeric_features_with_label_provenance():
    revisions, scans = volume()
    first = build(revisions=revisions, scans=scans)
    reordered = build(
        candles=tuple(reversed(market())),
        revisions=tuple(reversed(revisions)),
        scans=tuple(reversed(scans)),
    )
    assert first == reordered
    assert verify_dataset(first)
    assert [row.anchor for row in first.samples] == [at(8), at(12)]
    row = first.samples[0]
    assert row.coverage_slots == 16 and len(row.volume_revision_ids) == 16
    assert row.volume_nonzero_count == 13
    assert row.volume_sum == "1.625" and row.volume_mean == "0.1015625"
    assert row.market_event_ids == ("market-0", "market-1")
    assert row.label_event_id == "market-2" and row.label == "UP"
    assert len({row.market_feature_hash, row.aggregate_feature_hash, row.feature_hash}) == 3
    tampered = replace(first, samples=(replace(row, label="DOWN"),) + first.samples[1:])
    assert not verify_dataset(tampered)


def test_future_market_and_revision_do_not_change_prior_features():
    baseline = build()
    changed_market = list(market())
    changed_market[2] = replace(changed_market[2], close="80", payload_hash="f" * 64)
    later_label = build(candles=tuple(changed_market))
    assert later_label.samples[0].feature_hash == baseline.samples[0].feature_hash
    assert later_label.samples[0].label == "DOWN"
    revisions, scans = volume()
    prior = next(row for row in revisions if row.point_at == at(7))
    correction = replace(
        prior,
        revision_id="late-correction",
        scan_id="late-scan",
        revision=2,
        observed_at=at(8) + BUCKET,
        value="0.5",
    )
    late_scan = VolumeScan("late-scan", at(8) + BUCKET, "ok", 96, 0, SOURCE, "fixture-v1")
    revised = build(revisions=revisions + (correction,), scans=scans + (late_scan,))
    assert revised.samples[0].feature_hash == baseline.samples[0].feature_hash
    assert revised.checksum != baseline.checksum


def test_current_correction_and_missing_or_late_slot_fail_closed():
    candles = list(market())
    candles[1] = replace(candles[1], received_at=at(8) + BUCKET, payload_hash="f" * 64)
    assert build(candles=tuple(candles)).waits[0].reason == "market_late_or_corrected"
    revisions, scans = volume()
    prior = next(row for row in revisions if row.point_at == at(7))
    correction = replace(
        prior,
        revision_id="current-correction",
        scan_id="correction-scan",
        revision=2,
        observed_at=at(7.5),
        value="0.5",
    )
    correction_scan = VolumeScan("correction-scan", at(7.5), "ok", 96, 0, SOURCE, "fixture-v1")
    corrected = build(revisions=revisions + (correction,), scans=scans + (correction_scan,))
    assert corrected.waits[0].reason == "volume_revision_conflict"
    without_slot = tuple(row for row in revisions if row.point_at != at(7))
    assert build(revisions=without_slot).waits[0].reason == "volume_slot_missing"
    late = replace(prior, scan_id="late-slot-scan", first_seen_at=at(7.75), observed_at=at(7.75))
    late_scan = VolumeScan("late-slot-scan", at(7.75), "ok", 96, 0, SOURCE, "fixture-v1")
    replaced = tuple(row for row in revisions if row != prior) + (late,)
    late_dataset = build(revisions=replaced, scans=scans + (late_scan,))
    assert late_dataset.waits[0].reason == "volume_slot_late"


def test_coverage_sparse_stale_gap_and_nonfinal_label_wait():
    revisions, scans = volume()
    assert build(revisions=(), scans=()).waits[0].reason == "coverage_missing"
    earlier_scans = tuple(row for row in scans if row.attempted_at <= at(7))
    assert build(revisions=(), scans=earlier_scans).waits[0].reason == "coverage_stale"
    gap_scans = tuple(
        replace(row, status="gap", missing_bucket_count=1) if row.attempted_at == at(8) else row
        for row in scans
    )
    assert build(revisions=revisions, scans=gap_scans).waits[0].reason == "coverage_gap"
    candles = list(market())
    candles[2] = replace(candles[2], final=False)
    assert build(candles=tuple(candles)).waits[0].reason == "label_nonfinal"
    assert build(cutoff=at(8), cfg=config(8)).waits[0].reason == "label_missing"


def test_split_embargo_and_invalid_utc_decimal_or_revision_sequence():
    dataset = build(cfg=config(24))
    groups = chronological_split(
        dataset,
        train_end=at(12),
        validation_end=at(20),
        test_end=at(24),
    )
    assert [[row.anchor for row in group] for group in groups] == [
        [at(8)],
        [at(16)],
        [at(24)],
    ]
    with pytest.raises(ValueError):
        chronological_split(
            dataset,
            train_end=at(12),
            validation_end=at(20),
            test_end=at(24),
            embargo=timedelta(hours=3),
        )
    with pytest.raises(ValueError):
        build(cfg=replace(config(), first_anchor=at(8).replace(tzinfo=None)))
    with pytest.raises(ValueError):
        build(
            candles=(
                replace(market()[0], received_at=at(4).astimezone(timezone(timedelta(hours=2)))),
            )
            + market()[1:]
        )
    with pytest.raises(ValueError):
        build(candles=(replace(market()[0], close="NaN"),) + market()[1:])
    revisions, scans = volume()
    with pytest.raises(ValueError):
        build(revisions=(replace(revisions[0], value="Infinity"),) + revisions[1:])
    with pytest.raises(ValueError):
        build(revisions=(replace(revisions[0], value="0.1234567"),) + revisions[1:])
    with pytest.raises(ValueError):
        build(revisions=(replace(revisions[0], revision=2),) + revisions[1:])
