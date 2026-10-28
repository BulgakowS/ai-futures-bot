"""Bounded disposable SQLite sparse export with immutable Hour4 joins."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_sparse import RESERVE_BYTES, SOURCE_VERSION
from ai_futures.gdelt_gkg_sparse_dataset import Config
from ai_futures.gdelt_gkg_sparse_export import _disposable_sqlite, export_sparse_dataset
from ai_futures.gdelt_gkg_sparse_models import SparseGkgAttempt, SparseGkgResult
from ai_futures.gdelt_gkg_sparse_real_training import evaluate_persisted_sparse
from ai_futures.gdelt_gkg_sparse_training import TrainingConfig
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION
from ai_futures.models import MarketCandleObservation

ANCHOR = datetime(2026, 10, 19, 8, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def test_export_rejects_persistent_or_non_test_database():
    persistent = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url="sqlite+aiosqlite:////Users/example/data/research/s11-gkg-sparse.sqlite",
    )
    with pytest.raises(PermissionError):
        _disposable_sqlite(persistent)
    production = Settings(
        _env_file=None,
        app_env="local",
        backend_auth_token="x" * 48,
        database_url="postgresql+asyncpg://tester:tester@localhost/tester",
    )
    with pytest.raises(PermissionError):
        _disposable_sqlite(production)


@pytest.mark.asyncio
async def test_sparse_export_restart_zero_crash_and_market_correction(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        market_candle_journal_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sparse-export.sqlite'}",
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
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    database = Database(settings)
    config = Config(ANCHOR, ANCHOR + 4 * HOUR, Decimal("10"), "scenario-v1")
    attempt_id = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            SparseGkgAttempt(
                attempt_id=attempt_id,
                anchor_at=ANCHOR,
                target_batch_at=ANCHOR - 15 * MINUTE,
                attempted_at=ANCHOR + MINUTE,
                reserved_bytes=RESERVE_BYTES,
                source_version=SOURCE_VERSION,
                config_version="cfg",
            )
        )
        session.add(
            SparseGkgResult(
                result_id=uuid4(),
                attempt_id=attempt_id,
                first_seen_at=ANCHOR + 2 * MINUTE,
                status="ok",
                error_code=None,
                response_bytes=100,
                archive_sha256="a" * 64,
                total_rows=10,
                btc_rows=0,
                mean_btc_document_tone=None,
                source_version=SOURCE_VERSION,
                config_version="cfg",
            )
        )
        # The next anchor is deliberately a committed claim without a result.
        session.add(
            SparseGkgAttempt(
                attempt_id=uuid4(),
                anchor_at=ANCHOR + 4 * HOUR,
                target_batch_at=ANCHOR + 4 * HOUR - 15 * MINUTE,
                attempted_at=ANCHOR + 4 * HOUR + MINUTE,
                reserved_bytes=RESERVE_BYTES,
                source_version=SOURCE_VERSION,
                config_version="cfg",
            )
        )
        for i in range(4):
            start = ANCHOR + (i - 2) * 4 * HOUR
            session.add(
                MarketCandleObservation(
                    observation_id=uuid4(),
                    causal_id=uuid4(),
                    symbol="BTC_USDT",
                    timeframe="4h",
                    window_start=start,
                    version=1,
                    first_seen_at=start + 4 * HOUR + (MINUTE if i == 1 else timedelta()),
                    observed_at=start + 4 * HOUR + (MINUTE if i == 1 else timedelta()),
                    source_time=start + 4 * HOUR,
                    close=str(100 + i),
                    payload_hash=f"{i + 1:064x}",
                    source="rest",
                    source_version=MARKET_CANDLE_SOURCE_VERSION,
                    config_version="cfg",
                    rules_version="rules",
                )
            )
    try:
        cutoff = ANCHOR + 8 * HOUR + 15 * MINUTE
        first = await export_sparse_dataset(database, settings, cutoff=cutoff, config=config)
        second = await export_sparse_dataset(database, settings, cutoff=cutoff, config=config)
        assert first.checksum == second.checksum
        assert first.dataset.samples[0].tone_missing
        assert first.dataset.waits[0].reason == "sparse_claim_unknown"
        assert first.eligibility == "collect-only"
        readiness = await evaluate_persisted_sparse(
            database,
            settings,
            dataset_config=config,
            training_config=TrainingConfig(
                ANCHOR - 8 * HOUR,
                ANCHOR - 4 * HOUR,
                ANCHOR,
                first.dataset.source_checksum,
            ),
        )
        assert (readiness.status, readiness.reason, readiness.artifacts) == (
            "collect-only",
            "incomplete_source_cohort",
            (),
        )
        async with database.sessions() as session, session.begin():
            original = await session.get(
                MarketCandleObservation, UUID(first.dataset.samples[0].label_observation_id)
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
                    observed_at=ANCHOR + 4 * HOUR + 16 * MINUTE,
                    source_time=original.source_time,
                    close="80",
                    payload_hash="e" * 64,
                    source="rest",
                    source_version=MARKET_CANDLE_SOURCE_VERSION,
                    config_version="cfg",
                    rules_version="rules",
                )
            )
        changed = await export_sparse_dataset(database, settings, cutoff=cutoff, config=config)
        assert changed.dataset.samples[0].label == first.dataset.samples[0].label
        assert changed.dataset.samples[0].feature_hash == first.dataset.samples[0].feature_hash
        assert changed.checksum != first.checksum
    finally:
        await database.close()
