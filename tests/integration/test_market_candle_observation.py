"""Disposable journal invariants; never touches the active market database."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.market import MarketStore
from ai_futures.models import Candle, MarketCandleObservation
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError


def fixture_time():
    now = datetime.now(UTC)
    anchor = now.replace(hour=(now.hour // 4) * 4, minute=0, second=0, microsecond=0)
    return anchor - timedelta(hours=4), now


def payload(close="101"):
    return {
        "open": "100",
        "close": close,
        "high": "102" if close != "103" else "104",
        "low": "99",
        "vol": "1",
        "amount": "101",
    }


async def migrated(tmp_path, name="market-journal.db", enabled=True):
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        market_candle_journal_enabled=enabled,
        database_url=f"sqlite+aiosqlite:///{tmp_path / name}",
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
    return Database(settings), settings


@pytest.mark.asyncio
async def test_final_rest_replay_correction_and_mixed_source(tmp_path):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, journal_enabled=True)
    start, now = fixture_time()
    await store.rules({"version": "rules-v1", "state": 0})
    await store.candles("4h", [(start, payload(), False)], now, "ws")
    await store.candles("4h", [(start, payload(), True)], now, "rest")
    await MarketStore(database, settings.config_version, journal_enabled=True).candles(
        "4h", [(start, payload(), True)], now, "rest"
    )
    await store.candles("4h", [(start, payload("103"), False)], now, "ws")
    await store.candles("4h", [(start, payload("103"), True)], now + timedelta(seconds=1))
    await store.candles("4h", [(start, payload(), True)], now + timedelta(seconds=2))
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(MarketCandleObservation).order_by(MarketCandleObservation.version)
            )
        ).all()
        candle = await session.get(Candle, ("BTC_USDT", "4h", start))
        assert [row.version for row in rows] == [1, 2, 3]
        assert [row.close for row in rows] == ["101", "103", "101"]
        assert len({row.causal_id for row in rows}) == 1
        assert all(row.first_seen_at == rows[0].first_seen_at for row in rows)
        assert rows[0].observed_at <= rows[1].observed_at <= rows[2].observed_at
        assert all(row.rules_version == "rules-v1" for row in rows)
        assert candle.payload["close"] == "101"
        assert candle.final
    await database.close()


@pytest.mark.asyncio
async def test_immutable_triggers_and_atomic_missing_table_default_off(tmp_path):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, journal_enabled=True)
    await store.rules({"version": "rules-v1", "state": 0})
    start, now = fixture_time()
    await store.candles("4h", [(start, payload(), True)], now)
    async with database.sessions() as session:
        observation = await session.scalar(select(MarketCandleObservation))
        oid = observation.observation_id.hex
    for statement in (
        f"UPDATE market_candle_observation SET close='1' WHERE observation_id='{oid}'",
        f"DELETE FROM market_candle_observation WHERE observation_id='{oid}'",
    ):
        with pytest.raises(DBAPIError):
            async with database.sessions.begin() as session:
                await session.execute(text(statement))
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP TABLE market_candle_observation"))
    next_start = start - timedelta(hours=4)
    with pytest.raises(DBAPIError):
        await store.candles("4h", [(next_start, payload(), True)], now)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Candle)) == 1
    await MarketStore(database, settings.config_version).candles(
        "4h", [(next_start, payload(), True)], now
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Candle)) == 2
    await database.close()
