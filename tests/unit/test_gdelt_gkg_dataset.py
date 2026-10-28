"""Synthetic GKG v2 as-of contract tests; no network or trading."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from ai_futures.gdelt_dataset import MarketCandle
from ai_futures.gdelt_gkg_dataset import (
    Config,
    Release,
    Scan,
    Slot,
    build_dataset,
    build_versioned_market_dataset,
    chronological_split,
    verify_dataset,
)
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION

START = datetime(2026, 10, 18, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
SLOT = timedelta(minutes=15)


def at(hours):
    return START + hours * HOUR


def fixture(*, btc=1):
    market = tuple(
        MarketCandle(
            f"m{i}",
            at(4 * i),
            at(4 * i + 4),
            at(4 * i + 4),
            str(100 + i),
            f"{i + 1:064x}",
            "fixture",
            "test-v1",
        )
        for i in range(4)
    )
    releases, slots, scans = [], [], []
    for i in range(16):
        batch = at(4) + i * SLOT
        seen = batch + SLOT
        scan_id = f"scan{i}"
        scans.append(Scan(scan_id, seen, batch, "ok", SOURCE_VERSION, "test-v1"))
        slots.append(Slot(f"obs{i}", scan_id, batch, seen, "ok", SOURCE_VERSION, "test-v1"))
        releases.append(
            Release(
                f"rev{i}",
                scan_id,
                batch,
                seen,
                seen,
                1,
                100,
                btc,
                "-0.5" if btc else None,
                f"{i + 1:064x}",
                "a" * 32,
                1000,
                SOURCE_VERSION,
                "test-v1",
            )
        )
    return market, tuple(releases), tuple(slots), tuple(scans)


def build(market=None, releases=None, slots=None, scans=None, *, config=None, cutoff=None):
    base = fixture()
    return build_dataset(
        market if market is not None else base[0],
        releases if releases is not None else base[1],
        slots if slots is not None else base[2],
        scans if scans is not None else base[3],
        cutoff=cutoff or at(12),
        config=config or Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )


def test_complete_numeric_features_checksum_and_zero():
    data = build()
    assert len(data.samples) == 1 and data.waits == ()
    row = data.samples[0]
    assert row.coverage_slots == 16 and row.btc_theme_count == 16
    assert row.denominator_rows == 1600 and row.btc_share == "0.01"
    assert row.mean_btc_document_tone == "-0.5" and not row.tone_missing
    assert row.decision_at == at(8)
    assert row.target_close_at == at(12)
    assert row.time_to_target_seconds == "14400"
    assert data == build()
    assert verify_dataset(data)
    assert not verify_dataset(replace(data, samples=(replace(row, btc_theme_count=99),)))
    market, releases, slots, scans = fixture(btc=0)
    zero = build(market, releases, slots, scans).samples[0]
    assert zero.btc_theme_count == 0 and zero.mean_btc_document_tone is None
    assert zero.tone_missing and zero.btc_share == "0"
    missing = build(market, releases[:-1], slots, scans)
    assert missing.waits[0].reason == "release_slot_missing"


def test_late_final_candle_and_gkg_release_set_bounded_decision_time():
    market, releases, slots, scans = fixture()
    market = (
        market[:1]
        + (
            replace(
                market[1],
                source_time=at(8) + timedelta(minutes=1),
                received_at=at(8) + timedelta(minutes=2),
            ),
        )
        + market[2:]
    )
    seen = at(8) + timedelta(minutes=3)
    releases = releases[:-1] + (replace(releases[-1], first_seen_at=seen, observed_at=seen),)
    slots = slots[:-1] + (replace(slots[-1], observed_at=seen),)
    scans = scans[:-1] + (replace(scans[-1], attempted_at=seen),)
    first = build(market, releases, slots, scans)
    assert first.waits == ()
    row = first.samples[0]
    assert row.decision_at == seen
    assert row.target_close_at == at(12)
    assert row.time_to_target_seconds == "14220"
    assert verify_dataset(first)
    assert not verify_dataset(
        replace(first, samples=(replace(row, target_close_at=at(12) + SLOT),))
    )
    assert build(market, releases, slots, scans) == first
    microsecond_market = (
        market[:1]
        + (replace(market[1], received_at=at(8) + timedelta(minutes=2, microseconds=123456)),)
        + market[2:]
    )
    precise = build(microsecond_market, *fixture()[1:]).samples[0]
    assert precise.time_to_target_seconds == "14279.876544"

    correction = replace(
        releases[0],
        revision_id="late-revision",
        revision=2,
        observed_at=at(8) + timedelta(minutes=10),
        btc_rows=9,
        scan_id="late-scan",
    )
    later_scan = Scan(
        "late-scan",
        correction.observed_at,
        releases[0].batch_at,
        "ok",
        SOURCE_VERSION,
        "test-v1",
    )
    after = build(market, releases + (correction,), slots, scans + (later_scan,))
    assert after.samples[0].decision_at == row.decision_at
    assert after.samples[0].feature_hash == row.feature_hash
    assert after.samples[0].release_revision_ids == row.release_revision_ids


def test_versioned_market_rejects_predecision_correction_and_ignores_later_one():
    market, releases, slots, scans = fixture()
    market = tuple(replace(row, source="rest") for row in market)
    seen = at(8) + timedelta(minutes=3)
    releases = releases[:-1] + (replace(releases[-1], first_seen_at=seen, observed_at=seen),)
    slots = slots[:-1] + (replace(slots[-1], observed_at=seen),)
    scans = scans[:-1] + (replace(scans[-1], attempted_at=seen),)
    before = replace(
        market[1],
        event_id="before",
        close="102",
        payload_hash="b" * 64,
        source_time=at(8) + timedelta(minutes=1),
        received_at=at(8) + timedelta(minutes=1),
    )
    after = replace(
        market[1],
        event_id="after",
        close="105",
        payload_hash="c" * 64,
        source_time=at(8) + timedelta(minutes=5),
        received_at=at(8) + timedelta(minutes=5),
    )
    base = build_versioned_market_dataset(
        market,
        releases,
        slots,
        scans,
        cutoff=at(12),
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )
    replay = build_versioned_market_dataset(
        market + (after,),
        releases,
        slots,
        scans,
        cutoff=at(12),
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )
    assert base == replay
    assert replay.samples[0].market_event_ids[1] == market[1].event_id
    assert replay.samples[0].decision_at == seen
    assert verify_dataset(replay)
    conflict = build_versioned_market_dataset(
        market + (before,),
        releases,
        slots,
        scans,
        cutoff=at(12),
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )
    assert conflict.samples == ()
    assert conflict.waits[0].reason == "market_revision_conflict"
    assert verify_dataset(conflict)
    future_correction = replace(
        market[2],
        event_id="future-correction",
        close="120",
        payload_hash="d" * 64,
        source_time=at(12) + timedelta(minutes=1),
        received_at=at(12) + timedelta(minutes=1),
    )
    label_cutoff = at(12) + timedelta(minutes=5)
    label_base = build_versioned_market_dataset(
        market,
        releases,
        slots,
        scans,
        cutoff=label_cutoff,
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )
    label_changed = build_versioned_market_dataset(
        market + (future_correction,),
        releases,
        slots,
        scans,
        cutoff=label_cutoff,
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
    )
    assert label_changed.samples[0].feature_hash == label_base.samples[0].feature_hash
    assert label_changed.samples[0].label_payload_hash != label_base.samples[0].label_payload_hash
    assert verify_dataset(label_changed)
    metadata = {row.event_id: ("hour4-rest-v1", "rules-v1", "test-v1") for row in market}
    sealed = build_versioned_market_dataset(
        market,
        releases,
        slots,
        scans,
        cutoff=at(12),
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
        market_metadata=metadata,
    )
    changed_rules = build_versioned_market_dataset(
        market,
        releases,
        slots,
        scans,
        cutoff=at(12),
        config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
        market_metadata=metadata | {market[1].event_id: ("hour4-rest-v1", "rules-v2", "test-v1")},
    )
    assert sealed.samples[0].feature_hash == changed_rules.samples[0].feature_hash
    assert sealed.checksum != changed_rules.checksum
    assert verify_dataset(sealed) and verify_dataset(changed_rules)
    with pytest.raises(ValueError, match="final REST"):
        build_versioned_market_dataset(
            market + (replace(market[1], source="ws"),),
            releases,
            slots,
            scans,
            cutoff=at(12),
            config=Config(at(8), at(8), Decimal("10"), "cost-v1"),
        )


def test_decision_delay_cutoff_and_incomplete_coverage_wait():
    market, releases, slots, scans = fixture()
    on_boundary = market[:1] + (replace(market[1], received_at=at(8) + SLOT),) + market[2:]
    assert build(on_boundary, releases, slots, scans).samples[0].decision_at == at(8) + SLOT
    too_late = (
        market[:1] + (replace(market[1], received_at=at(8) + timedelta(minutes=16)),) + market[2:]
    )
    assert build(too_late, releases, slots, scans).waits[0].reason == "market_late_or_corrected"
    seen = at(8) + timedelta(minutes=16)
    late_release = releases[:-1] + (replace(releases[-1], first_seen_at=seen, observed_at=seen),)
    late_slot = slots[:-1] + (replace(slots[-1], observed_at=seen),)
    late_scan = scans[:-1] + (replace(scans[-1], attempted_at=seen),)
    assert (
        build(market, late_release, late_slot, late_scan).waits[0].reason == "release_slot_missing"
    )
    assert (
        build(market, releases[:-1], slots[:-1], scans[:-1]).waits[0].reason
        == "release_slot_missing"
    )
    assert (
        build(
            market[:1]
            + (replace(market[1], received_at=at(8) + timedelta(minutes=1)),)
            + market[2:],
            releases,
            slots,
            scans,
            cutoff=at(8),
        )
        .waits[0]
        .reason
        == "decision_unavailable"
    )
    assert (
        build(market, releases, slots, scans, cutoff=at(12) - timedelta(seconds=1)).waits[0].reason
        == "label_missing"
    )


def test_future_batch_unavailable_before_anchor_and_late_release():
    market, releases, slots, scans = fixture()
    late = replace(
        releases[-1],
        first_seen_at=at(8) + timedelta(minutes=16),
        observed_at=at(8) + timedelta(minutes=16),
    )
    late_scan = replace(scans[-1], attempted_at=late.observed_at)
    late_slot = replace(slots[-1], observed_at=late.observed_at)
    data = build(
        market, releases[:-1] + (late,), slots[:-1] + (late_slot,), scans[:-1] + (late_scan,)
    )
    assert data.waits[0].reason == "release_slot_missing"
    over_lag = replace(releases[0], first_seen_at=at(4) + HOUR, observed_at=at(4) + HOUR)
    lag_scan = replace(scans[0], attempted_at=over_lag.observed_at)
    lag_slot = replace(slots[0], observed_at=over_lag.observed_at)
    assert (
        build(market, (over_lag,) + releases[1:], (lag_slot,) + slots[1:], (lag_scan,) + scans[1:])
        .waits[0]
        .reason
        == "release_slot_late"
    )


def test_revision_after_anchor_does_not_rewrite_and_asof_correction_fails_closed():
    market, releases, slots, scans = fixture()
    old = build(market, releases, slots, scans)
    correction = replace(
        releases[0],
        revision_id="correction",
        revision=2,
        observed_at=at(9),
        btc_rows=2,
        mean_btc_document_tone="1",
    )
    later_scan = Scan("later", at(9), releases[0].batch_at, "ok", SOURCE_VERSION, "test-v1")
    correction = replace(correction, scan_id="later")
    after = build(market, releases + (correction,), slots, scans + (later_scan,))
    assert after.samples[0].feature_hash == old.samples[0].feature_hash
    assert after.samples[0].btc_theme_count == old.samples[0].btc_theme_count
    before = replace(correction, observed_at=at(7), scan_id="earlier")
    earlier_scan = replace(later_scan, scan_id="earlier", attempted_at=at(7))
    assert (
        build(market, releases + (before,), slots, scans + (earlier_scan,)).waits[0].reason
        == "release_revision_conflict"
    )


def test_gap_error_and_market_faults_wait():
    market, releases, slots, scans = fixture()
    unknown = replace(slots[3], status="unknown")
    assert (
        build(market, releases, slots[:3] + (unknown,) + slots[4:], scans).waits[0].reason
        == "release_slot_unknown"
    )
    error_scan = Scan(
        "error-scan", at(7) + SLOT, releases[3].batch_at, "error", SOURCE_VERSION, "test-v1"
    )
    error_slot = Slot(
        "error-obs",
        "error-scan",
        releases[3].batch_at,
        at(7) + SLOT,
        "error",
        SOURCE_VERSION,
        "test-v1",
    )
    assert (
        build(market, releases, slots + (error_slot,), scans + (error_scan,)).waits[0].reason
        == "release_slot_unknown"
    )
    error_scan = replace(scans[3], status="error")
    with pytest.raises(ValueError):
        build(market, releases, slots, scans[:3] + (error_scan,) + scans[4:])
    assert (
        build(
            tuple(
                replace(market[1], received_at=at(9)) if i == 1 else m for i, m in enumerate(market)
            ),
            releases,
            slots,
            scans,
        )
        .waits[0]
        .reason
        == "market_late_or_corrected"
    )
    assert (
        build(
            tuple(replace(market[1], final=False) if i == 1 else m for i, m in enumerate(market)),
            releases,
            slots,
            scans,
        )
        .waits[0]
        .reason
        == "market_nonfinal"
    )


def test_future_label_mutation_does_not_change_features_and_embargo():
    market, releases, slots, scans = fixture()
    old = build(market, releases, slots, scans)
    changed = market[:2] + (replace(market[2], close="50", payload_hash="f" * 64),) + market[3:]
    new = build(changed, releases, slots, scans)
    assert old.samples[0].feature_hash == new.samples[0].feature_hash
    assert old.samples[0].label != new.samples[0].label
    with pytest.raises(ValueError):
        chronological_split(
            old, train_end=at(8), validation_end=at(12), test_end=at(16), embargo=HOUR
        )


def test_bad_utc_decimal_and_zero_tone_rejected():
    market, releases, slots, scans = fixture()
    with pytest.raises(ValueError):
        build(
            market,
            releases,
            slots,
            scans,
            config=Config(at(8).replace(tzinfo=None), at(8), Decimal(10), "v"),
        )
    with pytest.raises(ValueError):
        build(
            market,
            releases,
            slots,
            scans,
            config=Config(at(8).astimezone(timezone(timedelta(hours=2))), at(8), Decimal(10), "v"),
        )
    with pytest.raises(ValueError):
        build(market, releases, slots, scans, config=Config(at(8), at(8), Decimal("NaN"), "v"))
    with pytest.raises(ValueError):
        build(
            market,
            (replace(releases[0], mean_btc_document_tone="NaN"),) + releases[1:],
            slots,
            scans,
        )
    with pytest.raises(ValueError):
        build(market, (replace(releases[0], btc_rows=0),) + releases[1:], slots, scans)
