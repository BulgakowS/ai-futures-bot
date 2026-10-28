"""One-shot isolated public market research contract with no real HTTP."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.models import InstrumentRulesObservation, MarketCandleObservation
from ai_futures.sparse_market_research import collect_market_once
from sqlalchemy import func, select

from tests.integration.test_instrument_rules_observation import public_rules

ANCHOR = datetime(2026, 10, 20, 4, tzinfo=UTC)
NOW = ANCHOR + timedelta(minutes=2)
PAYLOAD = {
    "open": "100",
    "close": "101",
    "high": "102",
    "low": "99",
    "vol": "1",
    "amount": "101",
}


class FakePublic:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def server_time(self):
        self.calls.append("server_time")
        return NOW

    async def contract_rules(self, symbol):
        self.calls.append(("rules", symbol))
        return public_rules()

    async def candles(self, symbol, timeframe, server_time, start, end):
        self.calls.append(("candles", symbol, timeframe, server_time, start, end))
        return self.rows


@pytest.fixture
async def research_db(tmp_path):
    path = tmp_path / "research.sqlite"
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{path}",
        market_candle_journal_enabled=True,
        instrument_rules_journal_enabled=True,
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
    _, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    database = Database(settings)
    try:
        yield database, settings
    finally:
        await database.close()


async def test_two_final_hour4_windows_and_rules_are_journaled(research_db, monkeypatch):
    database, settings = research_db
    monkeypatch.setattr("ai_futures.sparse_market_research.utc_now", lambda: NOW)
    monkeypatch.setattr("ai_futures.market.utc_now", lambda: NOW)
    expected = (ANCHOR - timedelta(hours=8), ANCHOR - timedelta(hours=4))
    fake = FakePublic([(start, PAYLOAD, True) for start in expected])
    result = await collect_market_once(database, settings, ANCHOR, adapter=fake)
    assert result.final_windows == expected
    assert [call[0] if isinstance(call, tuple) else call for call in fake.calls] == [
        "server_time",
        "rules",
        "candles",
    ]
    async with database.sessions() as session:
        market = (await session.scalars(select(MarketCandleObservation))).all()
        rules = (await session.scalars(select(InstrumentRulesObservation))).all()
    assert [row.window_start for row in market] == list(expected)
    assert len(rules) == 1
    assert {row.rules_version for row in market} == {rules[0].rules_version}


async def test_gap_or_late_window_causes_no_market_write(research_db, monkeypatch):
    database, settings = research_db
    monkeypatch.setattr("ai_futures.sparse_market_research.utc_now", lambda: NOW)
    fake = FakePublic([(ANCHOR - timedelta(hours=4), PAYLOAD, True)])
    with pytest.raises(ValueError, match="Missing, duplicate or nonfinal"):
        await collect_market_once(database, settings, ANCHOR, adapter=fake)
    with pytest.raises(ValueError, match="anchor must be current"):
        await collect_market_once(
            database,
            settings,
            ANCHOR,
            adapter=fake,
            observed_at=ANCHOR + timedelta(minutes=16),
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MarketCandleObservation)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(InstrumentRulesObservation)) == 0
        )


async def test_flag_and_fixed_real_journal_gate(research_db):
    database, settings = research_db
    fake = FakePublic([])
    off = settings.model_copy(update={"market_candle_journal_enabled": False})
    with pytest.raises(PermissionError, match="Isolated"):
        await collect_market_once(database, off, ANCHOR, adapter=fake)
    with pytest.raises(PermissionError, match="fixed S11"):
        await collect_market_once(database, settings, ANCHOR)
    assert fake.calls == []
