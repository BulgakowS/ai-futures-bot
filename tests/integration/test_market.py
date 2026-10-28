from datetime import timedelta

import pytest
from ai_futures.config import Settings
from ai_futures.db import Base, Database
from ai_futures.market import MarketStore, market_quality, snapshot_fresh
from ai_futures.market_worker import MarketIngestion
from ai_futures.models import Candle, MarketSnapshot
from ai_futures.schemas import utc_now
from sqlalchemy import func, select


@pytest.fixture
async def market_db(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'markets.db'}",
    )
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database, settings
    await database.close()


def ticker_message(timestamp=None):
    return {
        "channel": "push.ticker",
        "data": {
            "symbol": "BTC_USDT",
            "lastPrice": "10",
            "bid1": "9",
            "ask1": "11",
            "fairPrice": "10",
            "indexPrice": "10",
            "fundingRate": "0.000001",
            "holdVol": 10,
            "timestamp": timestamp or int(utc_now().timestamp() * 1000),
        },
    }


async def test_dedupe_final_candle_no_ws_revision(market_db):
    database, settings = market_db
    store = MarketStore(database, settings.config_version)
    now = utc_now()
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    payload = {"open": "10", "close": "10", "high": "11", "low": "9", "vol": "1", "amount": "10"}
    await store.candles("1m", [(start, payload, True)], now)
    await store.candles("1m", [(start, payload, True)], now)
    await store.candles("1m", [(start, payload | {"close": "11"}, False)], now, "ws")
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Candle)) == 1
        row = await session.get(Candle, ("BTC_USDT", "1m", start))
        assert row.final and row.payload["close"] == "10"
        assert row.source_time == now and row.received_at >= now


async def test_disconnect_and_reconnect_gate(market_db):
    database, settings = market_db
    worker = MarketIngestion(database, settings, None)
    await worker.store.rules({"version": "v1", "state": 0})
    worker.connected = worker.reconciled = True
    await worker.handle(ticker_message())
    async with database.sessions() as session:
        assert (await market_quality(session, "BTC_USDT"))["trade_eligible"]
    await worker.disconnect("injected_disconnect")
    await worker.handle(ticker_message())
    async with database.sessions() as session:
        assert not (await market_quality(session, "BTC_USDT"))["trade_eligible"]
    worker.connected = True
    worker.reconciled = False
    await worker.handle(ticker_message())
    async with database.sessions() as session:
        assert not (await market_quality(session, "BTC_USDT"))["trade_eligible"]
    worker.reconciled = True
    await worker.handle(ticker_message())
    async with database.sessions() as session:
        assert (await market_quality(session, "BTC_USDT"))["trade_eligible"]
        ticker = await session.get(MarketSnapshot, ("BTC_USDT", "ticker"))
        assert not snapshot_fresh(ticker, utc_now() + timedelta(seconds=6))
        assert not (await market_quality(session, "BTC_USDT", utc_now() + timedelta(seconds=6)))[
            "trade_eligible"
        ]


async def test_schema_stale_rules_and_out_of_order_snapshot(market_db):
    database, settings = market_db
    worker = MarketIngestion(database, settings, None)
    worker.connected = worker.reconciled = True
    await worker.store.rules({"version": "v1", "state": 0})
    assert await worker.store.rules({"version": "v2", "state": 0})
    worker.rules_changed = True
    await worker.handle(ticker_message())
    async with database.sessions() as session:
        assert not (await market_quality(session, "BTC_USDT"))["trade_eligible"]
    from ai_futures.mexc import MarketDataError

    with pytest.raises(MarketDataError):
        await worker.handle(
            ticker_message(int((utc_now() - timedelta(seconds=10)).timestamp() * 1000))
        )
    now = utc_now()
    assert await worker.store.snapshot("depth", {"version": 2}, now)
    assert not await worker.store.snapshot("depth", {"version": 1}, now - timedelta(seconds=1))
    assert not await worker.store.snapshot("depth", {"version": 1}, now + timedelta(seconds=1))
    async with database.sessions() as session:
        depth = await session.get(MarketSnapshot, ("BTC_USDT", "depth"))
        assert not snapshot_fresh(depth)


async def test_market_endpoints_read_cache_without_adapter(market_db):
    from ai_futures.main import create_app
    from fastapi.testclient import TestClient

    from tests.integration.test_foundation import RedisStub

    database, settings = market_db
    store = MarketStore(database, settings.config_version)
    await store.rules({"version": "v1", "state": 0})
    await store.snapshot("ticker", {"lastPrice": "10.1"}, utc_now(), "ws")
    await store.snapshot("funding", {"fundingRate": "0.000001"}, utc_now())
    with TestClient(create_app(settings, database, RedisStub())) as client:
        assert client.get("/api/v1/markets").json()[0]["symbol"] == "BTC_USDT"
        response = client.get("/api/v1/markets/BTC_USDT/ticker").json()
        assert response["data"]["lastPrice"] == "10.1" and response["source"] == "ws"
        assert response["quality"]["trade_eligible"] is False
        assert client.get("/api/v1/markets/BTC_USDT/funding").status_code == 200
        assert client.get("/api/v1/markets/BTC_USDT/candles?timeframe=5m").status_code == 200
        assert client.get("/api/v1/markets/BTC_USDT/candles?timeframe=1h").status_code == 422
        assert client.get("/api/v1/markets/UNKNOWN/ticker").status_code == 503


async def test_actual_reconnect_after_malformed_ws(market_db):
    import asyncio
    from contextlib import asynccontextmanager

    database, settings = market_db
    stop = asyncio.Event()
    connections = []

    class Socket:
        def __init__(self, index):
            self.index = index

        async def send(self, raw):
            pass

        def __aiter__(self):
            return self.messages()

        async def messages(self):
            if self.index == 1:
                yield "[]"  # Schema failure must disconnect; no eligible market event.
            else:
                import json

                while not stop.is_set():
                    yield json.dumps(ticker_message())
                    await asyncio.sleep(0.02)

    @asynccontextmanager
    async def connector(*args, **kwargs):
        connections.append(len(connections) + 1)
        yield Socket(connections[-1])

    worker = MarketIngestion(database, settings, None, connector)
    reconciliations = []

    async def reconcile():
        reconciliations.append(len(reconciliations) + 1)
        worker.reconciled = False
        await worker.store.rules({"version": "v1", "state": 0})
        await worker.store.quality("stale", "reconciling")
        worker.reconciled = True

    worker.reconcile = reconcile
    task = asyncio.create_task(worker.websocket(stop))
    try:
        async with asyncio.timeout(5):
            while True:
                async with database.sessions() as session:
                    quality = await market_quality(session, "BTC_USDT")
                if quality["trade_eligible"]:
                    break
                await asyncio.sleep(0.02)
        assert connections == [1, 2] and reconciliations == [1, 2]
    finally:
        stop.set()
        await task


async def test_market_task_failure_marks_worker_degraded(market_db, monkeypatch):
    import asyncio

    from services.trading import worker as module
    from tests.integration.test_foundation import RedisStub

    database, settings = market_db
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "Database", lambda _: database)
    monkeypatch.setattr(module.Redis, "from_url", lambda *args, **kwargs: RedisStub())

    class FailingIngestion:
        def __init__(self, *args):
            pass

        async def run(self, stop):
            raise ConnectionError("injected market network failure")

    monkeypatch.setattr(module, "MarketIngestion", FailingIngestion)
    stop = asyncio.Event()
    task = asyncio.create_task(module.run_worker(stop))
    try:
        from ai_futures.models import WorkerHeartbeat

        async with asyncio.timeout(3):
            while True:
                async with database.sessions() as session:
                    heartbeat = await session.get(WorkerHeartbeat, "trading")
                if heartbeat:
                    break
                await asyncio.sleep(0.02)
        assert heartbeat.status == "degraded"
    finally:
        stop.set()
        await task
