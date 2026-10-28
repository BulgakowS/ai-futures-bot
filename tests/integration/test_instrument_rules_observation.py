"""Default-off public rules/cost provenance against disposable SQLite only."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.market import MarketStore
from ai_futures.mexc import normalize_rules
from ai_futures.models import InstrumentRules, InstrumentRulesObservation
from ai_futures.scenario_cost import ScenarioCost, scenario_cost_asof
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError


def public_rules(*, fee="0.0004", fee_mode="NORMAL", tiers=None):
    raw = {
        "symbol": "BTC_USDT",
        "futureType": 1,
        "settleCoin": "USDT",
        "state": 0,
        "contractSize": "0.0001",
        "priceUnit": "0.1",
        "volUnit": "1",
        "minVol": "1",
        "maxVol": "120000",
        "minLeverage": "1",
        "maxLeverage": "100",
        "makerFeeRate": "0.0001",
        "takerFeeRate": fee,
        "maintenanceMarginRate": "0.001",
        "initialMarginRate": "0.01",
        "liquidationFeeRate": "0.0004",
        "feeRateMode": fee_mode,
        "feeRateType": "BASE",
        "tieredFeeRates": tiers if tiers is not None else [],
        "leverageFeeRates": [],
        "riskLimitMode": "CUSTOM",
        "riskLimitType": "BY_VOLUME",
        "riskLimitCustom": [
            {"level": 1, "maxVol": "120000", "mmr": "0.001", "imr": "0.01", "maxLeverage": "100"}
        ],
    }
    return normalize_rules(raw, "BTC_USDT")


async def migrated(tmp_path, *, enabled=True):
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        instrument_rules_journal_enabled=enabled,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rules.db'}",
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
async def test_rules_dedupe_revisions_asof_and_scenario_provenance(tmp_path, monkeypatch):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, rules_journal_enabled=True)
    t1 = datetime(2026, 10, 19, 8, tzinfo=UTC)
    scenario = ScenarioCost(t1 - timedelta(days=1), "3", "2")
    first = public_rules()
    second = public_rules(fee="0.0005")
    times = iter((t1, t1 + timedelta(minutes=1), t1 + timedelta(hours=1), t1 + timedelta(hours=2)))
    monkeypatch.setattr("ai_futures.market.utc_now", lambda: next(times))
    await store.rules(first)
    await store.rules(first)
    await store.rules(second)
    await store.rules(first)
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(InstrumentRulesObservation).order_by(InstrumentRulesObservation.version)
            )
        ).all()
        cache = await session.get(InstrumentRules, "BTC_USDT")
        assert [row.version for row in rows] == [1, 2, 3]
        assert len({row.causal_id for row in rows}) == 1
        assert all(row.first_seen_at == t1 for row in rows)
        assert [row.effective_at for row in rows] == [row.observed_at for row in rows]
        assert [row.rules_version for row in rows] == [
            first["version"],
            second["version"],
            first["version"],
        ]
        assert cache.version == first["version"] and not cache.validated
        with pytest.raises(ValueError, match="No as-of"):
            await scenario_cost_asof(
                session,
                at=t1 - timedelta(seconds=1),
                rules_version=first["version"],
                scenario=scenario,
            )
        quote = await scenario_cost_asof(
            session,
            at=t1 + timedelta(minutes=30),
            rules_version=first["version"],
            scenario=scenario,
        )
        assert quote.total_bps == Decimal("13")
        assert quote.round_trip_public_taker_fee_bps == Decimal("8")
        assert quote.rules_observation_id == str(rows[0].observation_id)
        assert quote.source == "public_schedule_plus_assumptions_not_actual_fills"
        with pytest.raises(ValueError, match="provenance"):
            await scenario_cost_asof(
                session,
                at=t1 + timedelta(hours=1, minutes=30),
                rules_version=first["version"],
                scenario=scenario,
            )
        revised = await scenario_cost_asof(
            session,
            at=t1 + timedelta(hours=1, minutes=30),
            rules_version=second["version"],
            scenario=scenario,
        )
        assert revised.total_bps == Decimal("15")
        assert revised.rules_observation_id == str(rows[1].observation_id)
        with pytest.raises(ValueError, match="Stale"):
            await scenario_cost_asof(
                session,
                at=t1 + timedelta(hours=4),
                rules_version=first["version"],
                scenario=scenario,
            )
    await database.close()


@pytest.mark.asyncio
async def test_unchanged_rules_refresh_renews_asof_proof(tmp_path, monkeypatch):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, rules_journal_enabled=True)
    t0 = datetime(2026, 10, 19, 8, tzinfo=UTC)
    rules = public_rules()
    scenario = ScenarioCost(t0 - timedelta(days=1), "3", "2")
    times = iter((t0, t0 + timedelta(minutes=1), t0 + timedelta(seconds=3500)))
    monkeypatch.setattr("ai_futures.market.utc_now", lambda: next(times))
    await store.rules(rules)
    await store.rules(rules)
    async with database.sessions() as session:
        rows = (await session.scalars(select(InstrumentRulesObservation))).all()
        assert len(rows) == 1
        with pytest.raises(ValueError, match="Stale"):
            await scenario_cost_asof(
                session,
                at=t0 + timedelta(hours=1, minutes=1),
                rules_version=rules["version"],
                scenario=scenario,
            )
    await store.rules(rules)
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(InstrumentRulesObservation).order_by(InstrumentRulesObservation.version)
            )
        ).all()
        cache = await session.get(InstrumentRules, "BTC_USDT")
        assert [row.version for row in rows] == [1, 2]
        assert rows[0].observation_id != rows[1].observation_id
        assert rows[0].payload_hash == rows[1].payload_hash
        assert rows[0].causal_id == rows[1].causal_id
        assert rows[0].first_seen_at == rows[1].first_seen_at == t0
        assert rows[1].observed_at == rows[1].effective_at == t0 + timedelta(seconds=3500)
        assert rows[1].config_version == settings.config_version
        assert cache.version == rules["version"] and cache.validated
        quote = await scenario_cost_asof(
            session,
            at=t0 + timedelta(hours=1, minutes=1),
            rules_version=rules["version"],
            scenario=scenario,
        )
        assert quote.rules_observation_id == str(rows[1].observation_id)
        assert quote.total_bps == Decimal("13")
        with pytest.raises(ValueError, match="Stale"):
            await scenario_cost_asof(
                session,
                at=t0 + timedelta(hours=2),
                rules_version=rules["version"],
                scenario=scenario,
            )
    await database.close()


@pytest.mark.asyncio
async def test_unknown_fee_modes_tiers_and_future_assumption_fail_closed(tmp_path, monkeypatch):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, rules_journal_enabled=True)
    now = datetime(2026, 10, 19, 8, tzinfo=UTC)
    monkeypatch.setattr("ai_futures.market.utc_now", lambda: now)
    for payload in (
        public_rules(fee_mode="UNKNOWN"),
        public_rules(tiers=[{"level": 1, "takerFeeRate": "0.0001"}]),
    ):
        await store.rules(payload)
        async with database.sessions() as session:
            with pytest.raises(ValueError, match="provenance"):
                await scenario_cost_asof(
                    session,
                    at=now,
                    rules_version=payload["version"],
                    scenario=ScenarioCost(now - timedelta(days=1), "3", "2"),
                )
    with pytest.raises(ValueError, match="predeclared"):
        async with database.sessions() as session:
            await scenario_cost_asof(
                session,
                at=now,
                rules_version=public_rules()["version"],
                scenario=ScenarioCost(now + timedelta(seconds=1), "3", "2"),
            )
    with pytest.raises(ValueError, match="Decimal strings"):
        ScenarioCost(now, 3.0, "2")
    await database.close()


@pytest.mark.asyncio
async def test_immutable_atomic_and_default_off_without_journal_table(tmp_path):
    database, settings = await migrated(tmp_path)
    store = MarketStore(database, settings.config_version, rules_journal_enabled=True)
    first = public_rules()
    await store.rules(first)
    async with database.sessions() as session:
        row = await session.scalar(select(InstrumentRulesObservation))
        oid = row.observation_id.hex
    for statement in (
        f"UPDATE instrument_rules_observation SET rules_version='x' WHERE observation_id='{oid}'",
        f"DELETE FROM instrument_rules_observation WHERE observation_id='{oid}'",
    ):
        with pytest.raises(DBAPIError):
            async with database.sessions.begin() as session:
                await session.execute(text(statement))
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP TABLE instrument_rules_observation"))
    with pytest.raises(DBAPIError):
        await store.rules(public_rules(fee="0.0005"))
    async with database.sessions() as session:
        assert (await session.get(InstrumentRules, "BTC_USDT")).version == first["version"]
    await MarketStore(database, settings.config_version).rules(public_rules(fee="0.0005"))
    async with database.sessions() as session:
        assert (await session.get(InstrumentRules, "BTC_USDT")).version != first["version"]
    await database.close()
