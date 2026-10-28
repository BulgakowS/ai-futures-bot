"""Disposable SQLite snapshot and restart for GKG numeric exporter."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_dataset import Config
from ai_futures.gdelt_gkg_export import export_persisted_gkg_dataset
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.models import Candle, MarketCandleObservation
from sqlalchemy import select

START = datetime(2026, 10, 18, tzinfo=UTC)
HOUR = timedelta(hours=1)
SLOT = timedelta(minutes=15)


@pytest.mark.asyncio
async def test_snapshot_restart_and_future_batch_not_early(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        market_candle_journal_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gkg-export.db'}",
    )
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/alembic",
        "upgrade",
        "head",
        env=os.environ
        | {
            "APP_ENV": "test",
            "BACKEND_AUTH_TOKEN": "x" * 48,
            "DATABASE_URL": settings.database_url.get_secret_value(),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = await process.communicate()
    assert process.returncode == 0, output
    database = Database(settings)
    config = Config(START + 8 * HOUR, START + 8 * HOUR, Decimal("10"), "cost-v1")
    cutoff = START + 12 * HOUR
    async with database.sessions() as session, session.begin():
        for i in range(3):
            window = START + i * 4 * HOUR
            session.add(
                MarketCandleObservation(
                    observation_id=uuid4(),
                    causal_id=uuid4(),
                    symbol="BTC_USDT",
                    timeframe="4h",
                    window_start=window,
                    version=1,
                    first_seen_at=window
                    + 4 * HOUR
                    + (timedelta(minutes=2) if i == 1 else timedelta()),
                    source_time=window + 4 * HOUR,
                    observed_at=window
                    + 4 * HOUR
                    + (timedelta(minutes=2) if i == 1 else timedelta()),
                    close=str(100 + i),
                    payload_hash=f"{i + 1:064x}",
                    source="rest",
                    source_version="mexc-futures-hour4-rest-v1",
                    config_version="test-v1",
                    rules_version="test-rules-v1",
                )
            )
        for i in range(16):
            batch = START + 4 * HOUR + i * SLOT
            seen = batch + SLOT
            scan_id = uuid4()
            session.add(
                GkgScan(
                    scan_id=scan_id,
                    attempted_at=seen,
                    batch_at=batch,
                    status="ok",
                    error_code=None,
                    manifest_bytes=100,
                    archive_bytes=1000,
                    source_version=SOURCE_VERSION,
                    config_version="test-v1",
                )
            )
            session.add(
                GkgSlotObservation(
                    observation_id=uuid4(),
                    scan_id=scan_id,
                    slot_at=batch,
                    observed_at=seen,
                    status="ok",
                    source_version=SOURCE_VERSION,
                    config_version="test-v1",
                )
            )
            session.add(
                GkgReleaseRevision(
                    revision_id=uuid4(),
                    scan_id=scan_id,
                    batch_at=batch,
                    first_seen_at=seen,
                    observed_at=seen,
                    revision=1,
                    total_rows=100,
                    btc_rows=0,
                    mean_btc_document_tone=None,
                    archive_sha256=f"{i + 1:064x}",
                    manifest_md5="a" * 32,
                    manifest_size=1000,
                    source_version=SOURCE_VERSION,
                    config_version="test-v1",
                )
            )
        # This release belongs to the next 4h interval. Its first sighting
        # precedes its batch clock, so availability must still be 08:00 UTC.
        batch = START + 8 * HOUR
        scan_id = uuid4()
        early = batch - timedelta(minutes=3)
        session.add(
            GkgScan(
                scan_id=scan_id,
                attempted_at=early,
                batch_at=batch,
                status="ok",
                error_code=None,
                manifest_bytes=100,
                archive_bytes=1000,
                source_version=SOURCE_VERSION,
                config_version="test-v1",
            )
        )
        session.add(
            GkgSlotObservation(
                observation_id=uuid4(),
                scan_id=scan_id,
                slot_at=batch,
                observed_at=early,
                status="ok",
                source_version=SOURCE_VERSION,
                config_version="test-v1",
            )
        )
        session.add(
            GkgReleaseRevision(
                revision_id=uuid4(),
                scan_id=scan_id,
                batch_at=batch,
                first_seen_at=early,
                observed_at=early,
                revision=1,
                total_rows=688,
                btc_rows=2,
                mean_btc_document_tone="-0.35",
                archive_sha256="f" * 64,
                manifest_md5="b" * 32,
                manifest_size=3042368,
                source_version=SOURCE_VERSION,
                config_version="test-v1",
            )
        )
    first = await export_persisted_gkg_dataset(database, settings, cutoff=cutoff, config=config)
    assert len(first.dataset.samples) == 1
    assert first.dataset.samples[0].decision_at == START + 8 * HOUR + timedelta(minutes=2)
    assert first.dataset.samples[0].time_to_target_seconds == "14280"
    assert first.dataset.samples[0].btc_theme_count == 0
    assert first.dataset.samples[0].tone_missing
    assert len(first.snapshot_revision_ids) == 16
    await database.close()
    restarted = Database(settings)
    second = await export_persisted_gkg_dataset(restarted, settings, cutoff=cutoff, config=config)
    assert first == second
    async with restarted.sessions.begin() as session:
        original = await session.scalar(
            select(MarketCandleObservation).where(
                MarketCandleObservation.window_start == START + 4 * HOUR
            )
        )
        session.add(
            MarketCandleObservation(
                observation_id=uuid4(),
                causal_id=original.causal_id,
                symbol="BTC_USDT",
                timeframe="4h",
                window_start=original.window_start,
                version=2,
                first_seen_at=original.first_seen_at,
                observed_at=START + 8 * HOUR + timedelta(minutes=3),
                source_time=START + 8 * HOUR + timedelta(minutes=3),
                payload_hash="e" * 64,
                close="999",
                source="rest",
                source_version=original.source_version,
                config_version=original.config_version,
                rules_version=original.rules_version,
            )
        )
        session.add(
            Candle(
                symbol="BTC_USDT",
                timeframe="4h",
                window_start=original.window_start,
                event_id=uuid4(),
                source_time=START + 8 * HOUR + timedelta(minutes=3),
                received_at=START + 8 * HOUR + timedelta(minutes=3),
                payload={"close": "999"},
                payload_hash="e" * 64,
                final=True,
                source="rest",
                config_version="test-v1",
            )
        )
    corrected = await export_persisted_gkg_dataset(
        restarted, settings, cutoff=cutoff, config=config
    )
    assert corrected.dataset.samples == first.dataset.samples
    assert corrected.dataset.checksum == first.dataset.checksum
    assert corrected.checksum != first.checksum  # New journal row is included in export provenance.
    await restarted.close()
    disabled = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=settings.database_url.get_secret_value(),
        market_candle_journal_enabled=False,
    )
    disabled_database = Database(disabled)
    waited = await export_persisted_gkg_dataset(
        disabled_database, disabled, cutoff=cutoff, config=config
    )
    assert waited.dataset.samples == ()
    assert waited.dataset.waits[0].reason == "market_gap"
    await disabled_database.close()


@pytest.mark.asyncio
async def test_export_rejects_non_disposable_database(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fixture.db'}",
    )
    database = Database(settings)
    local = SimpleNamespace(app_env="local", database_url=settings.database_url)
    with pytest.raises(PermissionError):
        await export_persisted_gkg_dataset(
            database,
            local,
            cutoff=START + 12 * HOUR,
            config=Config(START + 8 * HOUR, START + 8 * HOUR, Decimal(10), "v"),
        )
    await database.close()
    outside = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{Path.cwd() / 'unused-gkg.db'}",
    )
    outside_database = Database(outside)
    with pytest.raises(PermissionError):
        await export_persisted_gkg_dataset(
            outside_database,
            outside,
            cutoff=START + 12 * HOUR,
            config=Config(START + 8 * HOUR, START + 8 * HOUR, Decimal(10), "v"),
        )
    await outside_database.close()
