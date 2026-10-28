"""N2b synthetic persisted coverage history and dataset export."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from ai_futures.db import Database
from ai_futures.models import Candle
from ai_futures.news import AnnouncementJournal
from ai_futures.news_dataset import DatasetConfig
from ai_futures.news_export import export_persisted_dataset
from ai_futures.news_models import AnnouncementCoverageObservation
from ai_futures.paper_models import PaperOrder
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_news import announcement, news_db  # noqa: F401

START = datetime(2026, 10, 17, tzinfo=UTC)
HOUR = timedelta(hours=1)
CONFIG = DatasetConfig(Decimal("10"), "synthetic-cost-v1")


def at(hour: int) -> datetime:
    return START + hour * HOUR


async def seed_candles(database):
    async with database.sessions.begin() as session:
        for index in range(6):
            start = at(index * 4)
            session.add(
                Candle(
                    symbol="BTC_USDT",
                    timeframe="4h",
                    window_start=start,
                    event_id=uuid4(),
                    source_time=start + 4 * HOUR,
                    received_at=start + 4 * HOUR,
                    payload={"close": str(100 + index)},
                    payload_hash=f"{index + 1:064x}",
                    final=True,
                    source="synthetic",
                    config_version="fixture-v1",
                )
            )


async def test_asof_coverage_recovery_restart_and_immutable_history(news_db):  # noqa: F811
    database, settings = news_db
    await seed_candles(database)
    journal = AnnouncementJournal(database, settings)
    await journal.ingest_page([], page=1, total_pages=1, scan_id="complete", fetched_at=at(7))
    await journal.record_failure(scan_id="failure", status_code=503, observed_at=at(11))
    await journal.ingest_page([], page=1, total_pages=1, scan_id="recovery", fetched_at=at(15))
    before = await export_persisted_dataset(database, settings, cutoff=at(20), config=CONFIG)
    assert [row.reason for row in before.dataset.waits if row.anchor == at(12)] == [
        "news_coverage_gap"
    ]
    assert any(row.anchor == at(8) for row in before.dataset.samples)
    assert any(row.anchor == at(16) for row in before.dataset.samples)
    async with database.sessions() as session:
        observations = (
            await session.scalars(
                select(AnnouncementCoverageObservation).order_by(
                    AnnouncementCoverageObservation.observed_at
                )
            )
        ).all()
        assert [row.status for row in observations] == ["ok", "error", "ok"]
        assert [row.scan_id for row in observations] == ["complete", "failure", "recovery"]
        assert observations[1].last_success_at == at(7)
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    with pytest.raises(IntegrityError):
        async with database.sessions.begin() as session:
            await session.execute(text("UPDATE announcement_coverage_observation SET status='ok'"))
    with pytest.raises(IntegrityError):
        async with database.sessions.begin() as session:
            await session.execute(text("DELETE FROM announcement_coverage_observation"))

    restarted = Database(settings)
    try:
        replay = await export_persisted_dataset(restarted, settings, cutoff=at(20), config=CONFIG)
        assert replay == before
        late_cutoff = await export_persisted_dataset(
            restarted, settings, cutoff=at(13), config=CONFIG
        )
        assert len(late_cutoff.coverage_observation_ids) == 2
        assert all(row.anchor < at(12) for row in late_cutoff.dataset.samples)
    finally:
        await restarted.close()


async def test_late_edit_and_current_candle_correction_fail_closed(news_db):  # noqa: F811
    database, settings = news_db
    await seed_candles(database)
    journal = AnnouncementJournal(database, settings)
    first = announcement("btc-edit", "BTC first", at(7))
    await journal.ingest_page([first], page=1, total_pages=1, scan_id="first", fetched_at=at(7))
    baseline = await export_persisted_dataset(database, settings, cutoff=at(12), config=CONFIG)
    early = next(row for row in baseline.dataset.samples if row.anchor == at(8))
    edit = announcement("btc-edit", "BTC edited", at(15))
    await journal.ingest_page([edit], page=1, total_pages=1, scan_id="edit", fetched_at=at(15))
    after = await export_persisted_dataset(database, settings, cutoff=at(20), config=CONFIG)
    assert next(row for row in after.dataset.samples if row.anchor == at(8)).feature_hash == (
        early.feature_hash
    )
    assert [row.reason for row in after.dataset.waits if row.anchor == at(16)] == ["news_edit"]
    assert baseline.checksum != after.checksum

    async with database.sessions.begin() as session:
        future = await session.get(Candle, ("BTC_USDT", "4h", at(8)))
        future.payload = {"close": "88"}
        future.payload_hash = "e" * 64
    future_changed = await export_persisted_dataset(
        database, settings, cutoff=at(20), config=CONFIG
    )
    changed_early = next(row for row in future_changed.dataset.samples if row.anchor == at(8))
    assert changed_early.feature_hash == early.feature_hash
    assert changed_early.label == "DOWN" and future_changed.checksum != after.checksum

    async with database.sessions.begin() as session:
        candle = await session.get(Candle, ("BTC_USDT", "4h", at(4)))
        candle.received_at = at(10)  # The currently stored correction is later than anchor 08:00.
        candle.payload = {"close": "111"}
        candle.payload_hash = "f" * 64
    corrected = await export_persisted_dataset(database, settings, cutoff=at(20), config=CONFIG)
    assert [row.reason for row in corrected.dataset.waits if row.anchor == at(8)] == ["market_late"]
    assert all(row.anchor != at(8) for row in corrected.dataset.samples)
    assert corrected.checksum != after.checksum
    assert any(row.anchor == at(8) for row in baseline.dataset.samples)


async def test_mixed_mode_and_disabled_export_are_rejected(news_db):  # noqa: F811
    database, settings = news_db
    await seed_candles(database)
    disabled = settings.model_copy(update={"news_fixture_ingest_enabled": False})
    with pytest.raises(PermissionError):
        await export_persisted_dataset(database, disabled, cutoff=at(20), config=CONFIG)
    async with database.sessions.begin() as session:
        candle = await session.get(Candle, ("BTC_USDT", "4h", at(0)))
        candle.source = "mexc_rest"
    with pytest.raises(ValueError, match="Mixed or non-synthetic"):
        await export_persisted_dataset(database, settings, cutoff=at(20), config=CONFIG)
