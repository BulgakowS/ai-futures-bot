"""Bounded public MEXC WS loss/recovery proof in a disposable SQLite datastore."""

import asyncio
import json
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from ai_futures.config import Settings
from ai_futures.db import Base, Database
from ai_futures.market import market_quality
from ai_futures.market_worker import MarketIngestion
from ai_futures.mexc import WS_URL, MexcPublic, epoch, parse_json
from ai_futures.models import Candle, MarketSnapshot
from ai_futures.schemas import utc_now
from sqlalchemy import func, select
from websockets.asyncio.client import connect


class CountedPublic(MexcPublic):
    def __init__(self):
        super().__init__()
        self.requests = Counter()

    async def get(self, path, params=None):
        if sum(self.requests.values()) >= 60:
            raise RuntimeError("public REST request bound exceeded")
        self.requests[path] += 1
        return await super().get(path, params)


class ObservedSocket:
    def __init__(self, socket, generation, allow_second_ticker, reconciliation_barrier):
        self.socket = socket
        self.generation = generation
        self.allow_second_ticker = allow_second_ticker
        self.reconciliation_barrier = reconciliation_barrier
        self.tickers = 0
        self.skipped_stale_tickers = 0

    async def send(self, raw):
        await self.socket.send(raw)

    async def close(self):
        await self.socket.close()

    async def messages(self):
        async for raw in self.socket:
            message = parse_json(raw) if isinstance(raw, str) else None
            if isinstance(message, dict) and message.get("channel") == "push.ticker":
                if self.generation == 2:
                    await self.allow_second_ticker.wait()
                    source_time = epoch(message["data"]["timestamp"])
                    if (
                        source_time <= self.reconciliation_barrier["at"]
                        or (utc_now() - source_time).total_seconds() > 4
                    ):
                        self.skipped_stale_tickers += 1
                        continue
                self.tickers += 1
            yield raw

    def __aiter__(self):
        return self.messages()


async def observe(database):
    async with database.sessions() as session:
        quality = await market_quality(session, "BTC_USDT")
        persisted = await session.get(MarketSnapshot, ("BTC_USDT", "quality"))
        ticker = await session.get(MarketSnapshot, ("BTC_USDT", "ticker"))
        candles = (await session.scalars(select(Candle).where(Candle.final))).all()
        return {
            "at": utc_now().isoformat(),
            "quality_status": persisted.status if persisted else None,
            "quality_reason": persisted.payload.get("reason") if persisted else None,
            "trade_eligible": quality["trade_eligible"],
            "reasons": quality["reasons"],
            "transport": quality["transport"],
            "ticker_source": ticker.source if ticker else None,
            "ticker_source_time": ticker.source_time.isoformat() if ticker else None,
            "final_candles": len(candles),
            "unique_final_keys": len({(c.symbol, c.timeframe, c.window_start) for c in candles}),
        }


async def until(database, predicate, seconds=35):
    async with asyncio.timeout(seconds):
        while True:
            state = await observe(database)
            if predicate(state):
                return state
            await asyncio.sleep(0.1)


async def replay_final_candle(worker):
    database = worker.store.database
    async with database.sessions() as session:
        row = await session.scalar(
            select(Candle).where(Candle.final).order_by(Candle.window_start.desc()).limit(1)
        )
        before_count = await session.scalar(
            select(func.count()).select_from(Candle).where(Candle.final)
        )
        key = (row.symbol, row.timeframe, row.window_start)
        original_id = row.event_id
        timeframe, start, payload, source_time = (
            row.timeframe,
            row.window_start,
            row.payload,
            row.source_time,
        )
    for _ in range(2):
        await worker.store.candles(timeframe, [(start, payload, True)], source_time, "rest")
    async with database.sessions() as session:
        after = await session.get(Candle, key)
        after_count = await session.scalar(
            select(func.count()).select_from(Candle).where(Candle.final)
        )
    assert after.final and after.event_id == original_id and after_count == before_count
    return {
        "source": "persisted_real_mexc_final_rest_candle",
        "replays": 2,
        "count_delta": 0,
        "event_id_unchanged": True,
    }


async def run():
    evidence = {
        "started_at": utc_now().isoformat(),
        "scope": "disposable_sqlite_public_rest_ws_only",
        "symbol": "BTC_USDT",
        "private_requests": 0,
        "paper_orders": 0,
        "external_messages": 0,
        "max_seconds": 170,
        "max_public_rest_operations": 60,
        "public_rest_rate_policy": "shared_0.55_second_minimum_spacing_with_retry_backoff",
    }
    async with asyncio.timeout(170):
        with tempfile.TemporaryDirectory(prefix="public-ws-fault-") as directory:
            settings = Settings(
                _env_file=None,
                app_env="test",
                app_mode="paper",
                live_trading_enabled=False,
                backend_auth_token="x" * 48,
                database_url=f"sqlite+aiosqlite:///{directory}/market.db",
                ai_enabled=False,
                openai_api_key=None,
                telegram_bot_token=None,
                telegram_chat_id=None,
                telegram_delivery_enabled=False,
            )
            database = Database(settings)
            adapter = CountedPublic()
            stop = asyncio.Event()
            allow_reconnect = asyncio.Event()
            allow_second_ticker = asyncio.Event()
            second_reconciled = asyncio.Event()
            reconciliation_barrier = {"at": None}
            sockets = []

            @asynccontextmanager
            async def connector(url, **kwargs):
                assert url == WS_URL
                generation = len(sockets) + 1
                if generation == 2:
                    await allow_reconnect.wait()
                if generation > 2:
                    raise RuntimeError("WS connection bound exceeded")
                async with connect(url, **kwargs) as raw:
                    socket = ObservedSocket(
                        raw, generation, allow_second_ticker, reconciliation_barrier
                    )
                    sockets.append(socket)
                    yield socket

            worker = MarketIngestion(database, settings, adapter, connector)
            original_reconcile = worker.reconcile
            reconciliation_count = 0

            async def tracked_reconcile():
                nonlocal reconciliation_count
                await original_reconcile()
                reconciliation_count += 1
                if reconciliation_count == 2:
                    reconciliation_barrier["at"] = utc_now()
                    second_reconciled.set()

            worker.reconcile = tracked_reconcile
            task = None
            try:
                async with database.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                task = asyncio.create_task(worker.run(stop))
                evidence["before"] = await until(
                    database, lambda s: s["trade_eligible"] and s["ticker_source"] == "ws", 70
                )
                assert sockets[0].tickers > 0
                evidence["injected_close_at"] = utc_now().isoformat()
                await sockets[0].close()
                evidence["disconnected"] = await until(
                    database,
                    lambda s: s["quality_status"] == "stale" and not s["trade_eligible"],
                    15,
                )
                assert evidence["disconnected"]["transport"] == "rest_fallback"
                evidence["rest_fallback"] = await until(
                    database,
                    lambda s: s["ticker_source"] == "rest_fallback"
                    and s["quality_status"] == "stale"
                    and not s["trade_eligible"],
                    20,
                )
                allow_reconnect.set()
                await asyncio.wait_for(second_reconciled.wait(), 65)
                evidence["second_reconciled_at"] = reconciliation_barrier["at"].isoformat()
                evidence["reconciled_before_ws_ticker"] = await observe(database)
                assert sockets[1].tickers == 0
                assert not evidence["reconciled_before_ws_ticker"]["trade_eligible"]
                allow_second_ticker.set()
                evidence["recovered"] = await until(
                    database, lambda s: s["trade_eligible"] and s["ticker_source"] == "ws", 25
                )
                assert sockets[1].tickers > 0
                assert (
                    evidence["recovered"]["ticker_source_time"] > evidence["second_reconciled_at"]
                )
                assert all(
                    s["final_candles"] == s["unique_final_keys"]
                    for s in (evidence["before"], evidence["recovered"])
                )
                evidence["final_candle_replay"] = await replay_final_candle(worker)
                evidence["result"] = "pass"
            except Exception as exc:
                evidence["result"] = "blocked_or_failed"
                evidence["error_type"] = type(exc).__name__
                evidence["error_message"] = str(exc)[:160]
            finally:
                stop.set()
                allow_reconnect.set()
                allow_second_ticker.set()
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                evidence["finished_at"] = utc_now().isoformat()
                evidence["ws_connections"] = len(sockets)
                evidence["ws_ticker_frames"] = [socket.tickers for socket in sockets]
                evidence["ws_tickers_skipped_after_pause"] = [
                    socket.skipped_stale_tickers for socket in sockets
                ]
                evidence["reconciliations"] = reconciliation_count
                evidence["public_rest_operations"] = sum(adapter.requests.values())
                evidence["public_rest_paths"] = dict(sorted(adapter.requests.items()))
                await adapter.close()
                await database.close()
    path = Path("docs/evidence/public-ws-fault-2026-10-17.json")
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"{evidence['result'].upper()} evidence={path}")
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
