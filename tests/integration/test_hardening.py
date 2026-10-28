"""S09 transition, outbox, restart and dependency failure checks."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import ai_futures.alerts as alerts_module
import httpx
import pytest
from ai_futures.alerts import TelegramSender, deliver_once, monitor_once, observe, run_alerts
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.health import dependencies
from ai_futures.market import MarketStore
from ai_futures.models import AlertState, OutboxEvent, WorkerHeartbeat
from ai_futures.paper import PaperService
from ai_futures.schemas import utc_now
from ai_futures.state import seed_state
from sqlalchemy import func, select


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'hardening.db'}",
        worker_stale_seconds=20,
    )


async def migrate(config):
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/alembic", "upgrade", "head",
        env=os.environ | {
            "APP_ENV": "test", "BACKEND_AUTH_TOKEN": "x" * 48,
            "DATABASE_URL": config.database_url.get_secret_value(),
        },
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = await process.communicate()
    assert process.returncode == 0, output


async def test_alert_transitions_survive_restart_and_retry_without_token(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "s09")
    async with database.sessions.begin() as session:
        session.add(WorkerHeartbeat(
            name="trading", instance_id=uuid4(), status="idle",
            occurred_at=utc_now() - timedelta(seconds=30),
        ))
    await monitor_once(database, config)
    async with database.sessions.begin() as session:
        row = await session.get(AlertState, ("worker_down", "trading"))
        row.pending_at = utc_now() - timedelta(seconds=11)
    await monitor_once(database, config)
    async with database.sessions() as session:
        alerts = (await session.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "alert.telegram")
        )).all()
        assert len(alerts) == 1 and alerts[0].payload["kind"] == "worker_down"
    await database.close()

    restarted = Database(config)
    await monitor_once(restarted, config)
    async with restarted.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AlertState)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.topic == "alert.telegram"
        )) == 1

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200, json={
            "ok": len(requests) > 1, "result": {"message_id": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = TelegramSender("local-test-token", "test-chat", client)
        cutoff = utc_now() - timedelta(minutes=1)
        assert await deliver_once(restarted, sender, cutoff) == 0
        assert await deliver_once(restarted, sender, cutoff) == 1
        assert await deliver_once(restarted, sender, cutoff) == 0
    assert len(requests) == 2
    assert requests[0].url.path.endswith("/sendMessage")
    assert b"local-test-token" not in requests[0].content
    assert b"worker unavailable or degraded" in requests[0].content
    async with restarted.sessions() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == "alert.telegram")
        )
        assert event.published_at is not None
    await restarted.close()


async def test_delivery_cutoff_preserves_older_queued_rows_across_restart(config):
    await migrate(config)
    database = Database(config)
    cutoff = utc_now() - timedelta(minutes=1)
    old_id, edge_id, new_id = uuid4(), uuid4(), uuid4()
    async with database.sessions.begin() as session:
        for event_id, occurred_at in (
            (old_id, cutoff - timedelta(seconds=1)),
            (edge_id, cutoff),
            (new_id, cutoff + timedelta(seconds=1)),
        ):
            session.add(OutboxEvent(
                event_id=event_id, correlation_id=uuid4(),
                occurred_at=occurred_at, config_version="test", topic="alert.telegram",
                payload={"kind": "worker_down", "subject": "trading"},
            ))
    await database.close()

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    restarted = Database(config)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = TelegramSender("local-test-token", "test-chat", client)
        assert await deliver_once(restarted, sender, cutoff) == 2
        assert await deliver_once(restarted, sender, cutoff) == 0
    assert len(requests) == 2
    async with restarted.sessions() as session:
        rows = {row.event_id: row for row in (await session.scalars(select(OutboxEvent))).all()}
        assert rows[old_id].published_at is None
        assert rows[edge_id].published_at is not None
        assert rows[new_id].published_at is not None
    await restarted.close()


async def test_alert_monitor_runs_with_credentials_and_delivery_default_off(config, monkeypatch):
    configured = Settings(
        _env_file=None, app_env="test", backend_auth_token="x" * 48,
        database_url=config.database_url.get_secret_value(),
        telegram_bot_token="local-test-token", telegram_chat_id="test-chat",
    )
    await migrate(configured)
    database = Database(configured)
    async with database.sessions.begin() as session:
        assert await observe(session, "kill_switch", "paper", True, "test")

    def unexpected_sender(*args, **kwargs):
        raise AssertionError("default-off alert loop constructed a Telegram sender")

    monkeypatch.setattr(alerts_module, "TelegramSender", unexpected_sender)
    original_monitor = monitor_once
    stop = asyncio.Event()
    monitored = []

    async def one_monitor_cycle(db, settings):
        await original_monitor(db, settings)
        monitored.append(True)
        stop.set()

    monkeypatch.setattr(alerts_module, "monitor_once", one_monitor_cycle)
    await run_alerts(stop, database, configured)
    assert monitored == [True]
    async with database.sessions() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == "alert.telegram")
        )
        assert event is not None and event.published_at is None
    await database.close()


async def test_delivery_rejects_invalid_cutoff_before_transport():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = TelegramSender("local-test-token", "test-chat", client)
        for cutoff in (None, utc_now().replace(tzinfo=None), utc_now() + timedelta(days=1)):
            with pytest.raises(ValueError):
                await deliver_once(None, sender, cutoff)
    assert requests == []


async def test_stale_market_and_kill_alerts_are_transition_only(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "s09")
    await MarketStore(database, "s09").quality("stale", "injected_disconnect")
    await monitor_once(database, config)
    async with database.sessions.begin() as session:
        row = await session.get(AlertState, ("market_stale", "BTC_USDT"))
        row.pending_at = utc_now() - timedelta(seconds=16)
    await monitor_once(database, config)
    async with database.sessions.begin() as session:
        assert not await observe(session, "market_stale", "BTC_USDT", False, "s09")
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s09")
        assert not await observe(session, "market_stale", "BTC_USDT", False, "s09")
        row = await session.get(AlertState, ("market_stale", "BTC_USDT"))
        row.recovered_at = utc_now() - timedelta(seconds=31)
        assert await observe(session, "market_stale", "BTC_USDT", False, "s09")
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s09")
        row.pending_at = utc_now() - timedelta(seconds=16)
        assert await observe(session, "market_stale", "BTC_USDT", True, "s09")
    paper = PaperService(database, "s09")
    await paper.kill(False)
    await paper.kill(True)
    await paper.kill(True)
    async with database.sessions() as session:
        alerts = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.topic == "alert.telegram"
        ))).all()
        assert [event.payload["kind"] for event in alerts].count("market_stale") == 2
        assert [event.payload["kind"] for event in alerts].count("kill_switch") == 1
    await database.close()


async def test_short_stale_is_not_alerted_but_sustained_repeat_is(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "s10g")
    async with database.sessions.begin() as session:
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s10g")
        row = await session.get(AlertState, ("market_stale", "BTC_USDT"))
        row.pending_at = utc_now() - timedelta(seconds=4)
        assert not await observe(session, "market_stale", "BTC_USDT", False, "s10g")
        assert row.pending_at is None and not row.active
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s10g")
        row.pending_at = utc_now() - timedelta(seconds=16)
        assert await observe(session, "market_stale", "BTC_USDT", True, "s10g")
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s10g")
        assert not await observe(session, "market_stale", "BTC_USDT", False, "s10g")
        row.recovered_at = utc_now() - timedelta(seconds=31)
        assert await observe(session, "market_stale", "BTC_USDT", False, "s10g")
    async with database.sessions.begin() as session:
        assert not await observe(session, "market_stale", "BTC_USDT", True, "s10g")
        row = await session.get(AlertState, ("market_stale", "BTC_USDT"))
        row.pending_at = utc_now() - timedelta(seconds=16)
        assert await observe(session, "market_stale", "BTC_USDT", True, "s10g")
    async with database.sessions() as session:
        events = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.topic == "alert.telegram"
        ))).all()
        assert [event.payload["kind"] for event in events] == ["market_stale"] * 2
    await database.close()


async def test_missing_worker_heartbeat_alerts_after_confirmation(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "s10g")
    await monitor_once(database, config)
    async with database.sessions.begin() as session:
        row = await session.get(AlertState, ("worker_down", "trading"))
        assert row and not row.active and row.pending_at is not None
        row.pending_at = utc_now() - timedelta(seconds=11)
    await monitor_once(database, config)
    async with database.sessions() as session:
        row = await session.get(AlertState, ("worker_down", "trading"))
        events = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.topic == "alert.telegram"
        ))).all()
        assert row.active and len(events) == 1
        assert events[0].payload == {"kind": "worker_down", "subject": "trading"}
    await database.close()


async def test_readiness_fails_closed_during_db_and_redis_outage(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "s09")

    class FailedRedis:
        async def ping(self):
            raise ConnectionError("private redis URL")

    checks = await dependencies(database, FailedRedis(), config)
    assert checks["redis"] == "unavailable"
    assert "private" not in str(checks)
    await database.close()

    class FailedEngine:
        def connect(self):
            raise ConnectionError("private database URL")

    class FailedDatabase:
        engine = FailedEngine()

    checks = await dependencies(FailedDatabase(), FailedRedis(), config)
    assert checks["database"] == checks["trading"] == checks["state"] == "unavailable"
    assert "private" not in str(checks)
