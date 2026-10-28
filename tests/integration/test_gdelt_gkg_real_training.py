"""Real-shaped numeric rows in disposable SQLite; fixtures are not real evidence."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_dataset import Config
from ai_futures.gdelt_gkg_export import export_persisted_gkg_dataset
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.gdelt_gkg_real_training import evaluate_persisted_real_gkg
from ai_futures.gdelt_gkg_training import TrainingConfig
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION
from ai_futures.models import MarketCandleObservation
from sqlalchemy import select

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
SLOT = timedelta(minutes=15)


async def fixture_database(tmp_path, *, count=50, late=False, corrected=False, synthetic=False):
    settings = Settings(
        _env_file=None,
        app_env="test",
        market_candle_journal_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'real-shaped-fixture.db'}",
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
    first = START + 8 * HOUR
    last = first + (count - 1) * 4 * HOUR
    data_config = Config(first, last, Decimal("10"), "unverified-fixture-cost-v1")
    version = "synthetic-origin" if synthetic else "fixture-gkg-numeric-v1"
    async with database.sessions() as session, session.begin():
        for index in range(count + 2):
            window = START + index * 4 * HOUR
            seen = window + 4 * HOUR + timedelta(minutes=2)
            observation = MarketCandleObservation(
                observation_id=uuid4(),
                causal_id=uuid4(),
                symbol="BTC_USDT",
                timeframe="4h",
                window_start=window,
                version=1,
                first_seen_at=seen,
                source_time=window + 4 * HOUR,
                observed_at=seen,
                close=str(100 + index % 7),
                payload_hash=f"{index + 1:064x}",
                source="rest",
                source_version=MARKET_CANDLE_SOURCE_VERSION,
                config_version="fixture-market-v1",
                rules_version="fixture-rules-version-only",
            )
            session.add(observation)
            if corrected and index == 1:
                session.add(
                    MarketCandleObservation(
                        observation_id=uuid4(),
                        causal_id=observation.causal_id,
                        symbol="BTC_USDT",
                        timeframe="4h",
                        window_start=window,
                        version=2,
                        first_seen_at=seen,
                        source_time=first + timedelta(minutes=3),
                        observed_at=first + timedelta(minutes=3),
                        close="999",
                        payload_hash="e" * 64,
                        source="rest",
                        source_version=MARKET_CANDLE_SOURCE_VERSION,
                        config_version="fixture-market-v1",
                        rules_version="fixture-rules-version-only",
                    )
                )
        for anchor_index in range(count):
            anchor = first + anchor_index * 4 * HOUR
            for slot_index in range(16):
                batch = anchor - 4 * HOUR + slot_index * SLOT
                seen = batch + (
                    timedelta(minutes=35)
                    if late and anchor_index == 0 and slot_index == 0
                    else SLOT
                )
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
                        config_version=version,
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
                        config_version=version,
                    )
                )
                btc = slot_index % 3
                session.add(
                    GkgReleaseRevision(
                        revision_id=uuid4(),
                        scan_id=scan_id,
                        batch_at=batch,
                        first_seen_at=seen,
                        observed_at=seen,
                        revision=1,
                        total_rows=100,
                        btc_rows=btc,
                        mean_btc_document_tone=None if btc == 0 else str(slot_index % 5 - 2),
                        archive_sha256=f"{anchor_index * 16 + slot_index + 1:064x}",
                        manifest_md5="a" * 32,
                        manifest_size=1000,
                        source_version=SOURCE_VERSION,
                        config_version=version,
                    )
                )
    return database, settings, data_config


async def config_for(database, settings, data_config):
    exported = await export_persisted_gkg_dataset(
        database,
        settings,
        cutoff=data_config.last_anchor + 4 * HOUR + SLOT,
        config=data_config,
        label_observation_delay=SLOT,
    )
    first = data_config.first_anchor
    return exported, TrainingConfig(
        7,
        first + 26 * 4 * HOUR,
        first + 37 * 4 * HOUR,
        data_config.last_anchor,
        exported.dataset.source_checksum,
    )


@pytest.mark.asyncio
async def test_persisted_numeric_cohort_waits_for_immutable_cost_rules(tmp_path):
    database, settings, data_config = await fixture_database(tmp_path)
    try:
        exported, training_config = await config_for(database, settings, data_config)
        assert len(exported.dataset.samples) == 50
        assert exported.market_corrections == exported.gkg_corrections == 0
        result = await evaluate_persisted_real_gkg(
            database, settings, dataset_config=data_config, training_config=training_config
        )
        assert result.reason == "missing_cost_provenance"
        assert result.status == "collect-only" and not result.artifacts
        assert result.report["split"] == {"train": 26, "validation": 10, "test": 12}
        assert len(result.report["snapshot_revision_ids"]) == 800
        assert len(result.report["market_observation_ids"]) == 52
        assert result.report["label_cutoff_rule"] == "target_close_at+15m"
        mismatch = await evaluate_persisted_real_gkg(
            database,
            settings,
            dataset_config=data_config,
            training_config=replace(training_config, expected_source_checksum="0" * 64),
        )
        assert mismatch.reason == "source_checksum_mismatch" and not mismatch.artifacts
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_late_label_correction_cannot_rewrite_as_of_label(tmp_path):
    database, settings, data_config = await fixture_database(tmp_path, count=2)
    try:
        before = await export_persisted_gkg_dataset(
            database,
            settings,
            cutoff=data_config.last_anchor + 4 * HOUR + SLOT,
            config=data_config,
            label_observation_delay=SLOT,
        )
        first_label = before.dataset.samples[0].label_event_id
        async with database.sessions.begin() as session:
            original = await session.scalar(
                select(MarketCandleObservation).where(
                    MarketCandleObservation.window_start == data_config.first_anchor
                )
            )
            session.add(
                MarketCandleObservation(
                    observation_id=uuid4(),
                    causal_id=original.causal_id,
                    symbol=original.symbol,
                    timeframe=original.timeframe,
                    window_start=original.window_start,
                    version=2,
                    first_seen_at=original.first_seen_at,
                    source_time=data_config.first_anchor + 4 * HOUR + timedelta(minutes=20),
                    observed_at=data_config.first_anchor + 4 * HOUR + timedelta(minutes=20),
                    close="999",
                    payload_hash="f" * 64,
                    source=original.source,
                    source_version=original.source_version,
                    config_version=original.config_version,
                    rules_version=original.rules_version,
                )
            )
        after = await export_persisted_gkg_dataset(
            database,
            settings,
            cutoff=data_config.last_anchor + 4 * HOUR + SLOT,
            config=data_config,
            label_observation_delay=SLOT,
        )
        assert after.dataset.samples[0].label_event_id == first_label
        assert after.dataset.samples[0].label == before.dataset.samples[0].label
        assert after.checksum != before.checksum
        assert after.market_corrections == 1
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"count": 8}, "too_few_samples"),
        ({"count": 50, "late": True}, "source_quality_wait"),
        ({"count": 50, "corrected": True}, "corrected_source_wait"),
        ({"count": 50, "synthetic": True}, "invalid_real_source_provenance"),
    ],
)
async def test_real_training_rejects_invalid_fixture_sources(tmp_path, options, expected):
    database, settings, data_config = await fixture_database(tmp_path, **options)
    try:
        exported, config = await config_for(database, settings, data_config)
        if options.get("count") == 8:
            config = replace(
                config,
                train_end=data_config.first_anchor + 3 * 4 * HOUR,
                validation_end=data_config.first_anchor + 5 * 4 * HOUR,
                test_end=data_config.last_anchor,
            )
        result = await evaluate_persisted_real_gkg(
            database, settings, dataset_config=data_config, training_config=config
        )
        assert result.reason == expected
        assert result.status == "collect-only" and not result.artifacts
        assert result.report["persisted_export_checksum"] == exported.checksum
    finally:
        await database.close()
