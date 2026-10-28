"""Synthetic-only N2a event-time and chronology checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.news_dataset import (
    CandleRecord,
    CoverageRecord,
    DatasetConfig,
    NewsRecord,
    build_dataset,
    chronological_split,
    verify_dataset,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
CONFIG = DatasetConfig(Decimal("10"), "synthetic-cost-v1")


def at(hours: int) -> datetime:
    return START + hours * HOUR


def candles(count: int = 10) -> tuple[CandleRecord, ...]:
    return tuple(
        CandleRecord(
            event_id=f"market-{index}",
            window_start=at(4 * index),
            source_time=at(4 * index + 4),
            received_at=at(4 * index + 4),
            close=str(100 + index),
            payload_hash=f"{index + 1:064x}",
            source="synthetic",
            config_version="fixture-v1",
        )
        for index in range(count)
    )


def coverage(count: int = 10) -> tuple[CoverageRecord, ...]:
    return tuple(CoverageRecord(at(4 * index), at(4 * index), "ok") for index in range(count + 1))


def news(
    *,
    version: int = 1,
    post_hour: int = 6,
    seen_hour: int = 7,
    canonical: str = "announcement-one",
    observation_kind: str = "forward",
) -> NewsRecord:
    return NewsRecord(
        version_id=f"{canonical}-v{version}",
        canonical_id=canonical,
        version_number=version,
        title="Synthetic BTC futures update",
        post_time=at(post_hour),
        first_seen_at=at(seen_hour),
        available_at=at(max(post_hour, seen_hour)),
        payload_hash=f"{version + 100:064x}",
        source="synthetic",
        schema_version="fixture-v1",
        config_version="fixture-v1",
        observation_kind=observation_kind,
    )


def build(
    market: tuple[CandleRecord, ...] | None = None,
    announcements: tuple[NewsRecord, ...] = (),
    states: tuple[CoverageRecord, ...] | None = None,
    *,
    cutoff: datetime | None = None,
):
    return build_dataset(
        candles() if market is None else market,
        announcements,
        coverage() if states is None else states,
        cutoff=at(40) if cutoff is None else cutoff,
        config=CONFIG,
    )


def test_replay_checksum_order_duplicate_and_future_mutation():
    event = news()
    original = build(announcements=(event, event))
    assert verify_dataset(original)
    assert original == build(announcements=(event,))
    assert original == build(tuple(reversed(candles())), (event,), tuple(reversed(coverage())))
    assert original.samples[0].anchor == at(8)
    assert original.samples[0].news_version_ids == (event.version_id,)
    assert original.samples[0].btc_title_count == 1
    assert original.samples[0].futures_title_count == 1
    assert original.samples[0].label == "UP"
    later_market = list(candles())
    later_market[2] = replace(later_market[2], close="90", payload_hash="f" * 64)
    changed = build(tuple(later_market), (event,))
    assert changed.checksum != original.checksum
    assert changed.source_checksum != original.source_checksum
    assert changed.samples[0].feature_hash == original.samples[0].feature_hash
    assert changed.samples[0].label == "DOWN"
    assert not verify_dataset(
        replace(
            original, samples=(replace(original.samples[0], label="DOWN"),) + original.samples[1:]
        )
    )


def test_future_edit_and_snapshot_cutoff_do_not_change_prior_feature():
    first = news()
    edit = replace(
        news(version=2, post_hour=14, seen_hour=15), title="Edited synthetic BTC headline"
    )
    before = build(announcements=(first,), cutoff=at(12))
    after = build(announcements=(edit, first), cutoff=at(40))
    assert before.samples[0].feature_hash == after.samples[0].feature_hash
    assert before.samples[0].news_version_ids == (first.version_id,)
    assert next(row for row in after.waits if row.anchor == at(16)).reason == "news_edit"
    assert build(announcements=(first, edit), cutoff=at(12)) == before


def test_backfill_and_late_news_wait_without_retroactive_feature():
    old = news(post_hour=1, seen_hour=7, observation_kind="backfill")
    result = build(announcements=(old,))
    assert next(row for row in result.waits if row.anchor == at(8)).reason == (
        "news_late_or_backfill"
    )
    assert all(old.version_id not in row.news_version_ids for row in result.samples)
    late = replace(old, observation_kind="forward")
    assert next(
        row for row in build(announcements=(late,)).waits if row.anchor == at(8)
    ).reason == ("news_late_or_backfill")


@pytest.mark.parametrize(
    ("states", "reason"),
    [
        ((), "news_coverage_missing"),
        ((CoverageRecord(at(8), at(8), "gap", gap_count=1),), "news_coverage_gap"),
        ((CoverageRecord(at(3), at(3), "ok"),), "news_coverage_stale"),
    ],
)
def test_missing_gap_and_stale_coverage_wait(states, reason):
    result = build(states=states)
    assert next(row for row in result.waits if row.anchor == at(8)).reason == reason


def test_gap_late_nonfinal_and_conflicting_market():
    market = candles()
    assert build(tuple(item for item in market if item.window_start != at(12))).waits[1].reason == (
        "market_gap_or_unavailable_label"
    )
    delayed = list(market)
    delayed[1] = replace(delayed[1], received_at=at(12))
    assert next(row for row in build(tuple(delayed)).waits if row.anchor == at(8)).reason == (
        "market_late"
    )
    late_source = list(market)
    late_source[1] = replace(late_source[1], source_time=at(12))
    assert next(row for row in build(tuple(late_source)).waits if row.anchor == at(8)).reason == (
        "market_late"
    )
    nonfinal = list(market)
    nonfinal[2] = replace(nonfinal[2], final=False)
    assert next(row for row in build(tuple(nonfinal)).waits if row.anchor == at(8)).reason == (
        "market_gap_or_unavailable_label"
    )
    with pytest.raises(ValueError, match="Conflicting candle"):
        build(market + (replace(market[0], payload_hash="f" * 64),))


def test_invalid_news_provenance_and_utc_rejected():
    with pytest.raises(ValueError, match="provenance"):
        build(announcements=(replace(news(), available_at=at(6)),))
    with pytest.raises(ValueError, match="UTC"):
        build(cutoff=datetime(2026, 1, 2))
    with pytest.raises(ValueError, match="Conflicting announcement"):
        build(announcements=(news(), replace(news(), title="Changed")))
    with pytest.raises(ValueError, match="sequence has a gap"):
        build(announcements=(news(version=2),))
    with pytest.raises(ValueError, match="Reused announcement version ID"):
        build(
            announcements=(news(), replace(news(canonical="other"), version_id=news().version_id))
        )


def test_decimal_neutral_threshold_and_source_version_checksum():
    market = list(candles())
    market[2] = replace(market[2], close="101.05")
    result = build(tuple(market))
    assert result.samples[0].label == "NEUTRAL"
    different = build_dataset(
        tuple(market),
        (),
        coverage(),
        cutoff=at(40),
        config=replace(CONFIG, cost_version="synthetic-cost-v2"),
    )
    assert result.config_checksum != different.config_checksum
    assert result.checksum != different.checksum
    revised_coverage = build(
        tuple(market),
        states=tuple(replace(row, source_version="synthetic-coverage-v2") for row in coverage()),
    )
    assert result.source_checksum != revised_coverage.source_checksum


def test_chronological_embargo_and_related_news_group():
    dataset = build()
    train, validation, test = chronological_split(
        dataset, train_end=at(16), validation_end=at(28), test_end=at(40)
    )
    assert max(row.anchor for row in train) <= at(12)
    assert min(row.anchor for row in validation) >= at(20)
    assert min(row.anchor for row in test) >= at(32)
    with pytest.raises(ValueError, match="embargo"):
        chronological_split(
            dataset,
            train_end=at(16),
            validation_end=at(28),
            test_end=at(40),
            embargo=HOUR,
        )
    with_news = build(announcements=(news(),))
    grouped = replace(
        with_news,
        samples=tuple(
            replace(row, news_canonical_ids=(news().canonical_id,)) if row.anchor == at(32) else row
            for row in with_news.samples
        ),
    )
    with pytest.raises(ValueError, match="Related announcements"):
        chronological_split(grouped, train_end=at(16), validation_end=at(28), test_end=at(40))
