"""Public ingestion runs independently of worker health heartbeats."""

import asyncio
import logging
import random
from contextlib import suppress
from datetime import timedelta

from websockets.asyncio.client import connect

from ai_futures.funding import reconcile_funding
from ai_futures.market import MarketStore
from ai_futures.mexc import (
    INTERVALS,
    REST_INTERVALS,
    WS_URL,
    MarketDataError,
    decimal,
    epoch,
    normalize_ticker,
    parse_json,
    validate_ohlcv,
)
from ai_futures.schemas import utc_now

logger = logging.getLogger("ai_futures.market")


class MarketIngestion:
    def __init__(self, database, settings, adapter, connector=connect):
        self.store = MarketStore(
            database,
            settings.config_version,
            settings.market_symbol,
            journal_enabled=settings.market_candle_journal_enabled,
            rules_journal_enabled=settings.instrument_rules_journal_enabled,
        )
        self.adapter = adapter
        self.connector = connector
        self.symbol = settings.market_symbol
        self.connected = False
        self.reconciled = False
        self.rules_changed = False
        self.clock_skew_ms = 0
        self.server_at = None
        self.local_at = None
        self.last_pong = None
        self.last_ticker = None
        self.last_rules = None

    def server_now(self):
        if self.server_at is None:
            raise MarketDataError("Missing server clock")
        return self.server_at + (utc_now() - self.local_at)

    async def clock(self):
        before = utc_now()
        server = await self.adapter.server_time()
        after = utc_now()
        midpoint = before + (after - before) / 2
        self.clock_skew_ms = int((server - midpoint).total_seconds() * 1000)
        self.server_at, self.local_at = server, midpoint
        if abs(self.clock_skew_ms) > 2000:
            raise MarketDataError("Server clock skew")

    async def reconcile(self):
        self.reconciled = False
        await self.store.quality("stale", "reconciling")
        await self.clock()
        rules = await self.adapter.contract_rules(self.symbol)
        self.rules_changed = await self.store.rules(rules) or self.rules_changed
        self.last_rules = utc_now()
        ticker, timestamp = await self.adapter.ticker(self.symbol)
        await self.store.snapshot("ticker", ticker, timestamp, "rest")
        await self.refresh_context()
        self.reconciled = True

    async def refresh_context(self):
        server = self.server_now()
        for timeframe in REST_INTERVALS if self.store.journal_enabled else INTERVALS:
            # On restart/reconnect backfill from last stored final window. At most 2000 per call.
            from sqlalchemy import func, select

            from ai_futures.models import Candle

            async with self.store.database.sessions() as session:
                last = await session.scalar(
                    select(func.max(Candle.window_start)).where(
                        Candle.symbol == self.symbol, Candle.timeframe == timeframe, Candle.final
                    )
                )
            start = int(
                (
                    last - timedelta(seconds=2 * REST_INTERVALS[timeframe][1])
                    if last and timeframe == "4h"
                    else last or server - timedelta(seconds=200 * REST_INTERVALS[timeframe][1])
                ).timestamp()
            )
            end = int(server.timestamp())
            span = REST_INTERVALS[timeframe][1] * 1999
            while start <= end:
                segment_end = min(end, start + span)
                rows = await self.adapter.candles(
                    self.symbol, timeframe, self.server_now(), start, segment_end
                )
                await self.store.candles(timeframe, rows, self.server_now())
                start = segment_end + REST_INTERVALS[timeframe][1]
        funding, timestamp = await self.adapter.funding_current(self.symbol)
        await self.store.snapshot("funding", funding, timestamp)
        await reconcile_funding(
            self.store.database,
            self.adapter,
            self.symbol,
            self.store.config_version,
            funding,
        )
        history = await self.adapter.funding_history(self.symbol)
        await self.store.snapshot(
            "funding_history",
            {"settlements": history, "coverage": "latest_20_only"},
            self.server_now(),
        )
        depth, timestamp = await self.adapter.depth(self.symbol)
        await self.store.snapshot("depth", depth, timestamp)

    async def disconnect(self, reason):
        self.connected = self.reconciled = False
        await self.store.quality("stale", reason, "rest_fallback")

    async def handle(self, message):
        if not isinstance(message, dict):
            raise MarketDataError("Invalid WS frame")
        channel = message.get("channel", "")
        if channel == "pong":
            epoch(message["data"])
            self.last_pong = utc_now()
        elif channel.startswith("rs.sub."):
            if message.get("data") != "success":
                raise MarketDataError("Subscription rejected")
        elif channel == "push.ticker":
            ticker, timestamp = normalize_ticker(message["data"], self.symbol)
            if (utc_now() - timestamp).total_seconds() > 5 or timestamp > utc_now() + timedelta(
                seconds=2
            ):
                raise MarketDataError("Stale WS ticker")
            await self.store.snapshot("ticker", ticker, timestamp, "ws")
            self.last_ticker = utc_now()
            if self.connected and self.reconciled:
                if self.rules_changed:
                    from ai_futures.models import InstrumentRules

                    async with self.store.database.sessions() as session:
                        acknowledged = await session.get(InstrumentRules, self.symbol)
                        self.rules_changed = acknowledged is None or not acknowledged.validated
                status = "stale" if self.rules_changed else "ok"
                reason = (
                    "rules_changed_requires_validation" if self.rules_changed else "stream_active"
                )
                await self.store.quality(status, reason, clock_skew_ms=self.clock_skew_ms)
        elif channel == "push.kline":
            data = message["data"]
            if data.get("symbol") != self.symbol:
                raise MarketDataError("Unexpected candle symbol")
            timeframe = next(
                (key for key, value in INTERVALS.items() if value[0] == data.get("interval")), None
            )
            if timeframe is None:
                raise MarketDataError("Unexpected candle interval")
            start = epoch(data["t"], False)
            if int(start.timestamp()) % INTERVALS[timeframe][1]:
                raise MarketDataError("Unaligned WS candle")
            payload = {
                field: decimal(data[key], field in {"open", "close", "high", "low"})
                for field, key in {
                    "open": "o",
                    "close": "c",
                    "high": "h",
                    "low": "l",
                    "vol": "q",
                    "amount": "a",
                }.items()
            }
            validate_ohlcv(payload)
            await self.store.candles(
                timeframe, [(start, payload, False)], epoch(message["ts"]), "ws"
            )

    async def read_stream(self, ws):
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    raise MarketDataError("Unexpected compressed WS frame")
                await self.handle(parse_json(raw))
            raise MarketDataError("WS closed")
        finally:
            with suppress(Exception):
                await self.disconnect("disconnect_or_schema_error")

    async def websocket(self, stop):
        attempt = 0
        while not stop.is_set():
            reader = None
            try:
                async with self.connector(
                    WS_URL, ping_interval=None, open_timeout=10, close_timeout=3, max_size=2**20
                ) as ws:
                    self.connected = True
                    self.last_pong = utc_now()
                    self.last_ticker = None
                    await ws.send('{"method":"ping"}')
                    import json

                    for method, param in [
                        ("sub.ticker", {"symbol": self.symbol}),
                        *[
                            ("sub.kline", {"symbol": self.symbol, "interval": value[0]})
                            for value in INTERVALS.values()
                        ],
                    ]:
                        await ws.send(json.dumps({"method": method, "param": param, "gzip": False}))
                    reader = asyncio.create_task(self.read_stream(ws))
                    await self.reconcile()
                    attempt = 0
                    while not stop.is_set():
                        if not self.reconciled:
                            raise MarketDataError("Reconciliation required")
                        if reader.done():
                            await reader
                        if (utc_now() - self.last_pong).total_seconds() > 35:
                            raise MarketDataError("WS heartbeat timeout")
                        if (
                            self.last_ticker is None
                            or (utc_now() - self.last_ticker).total_seconds() > 5
                        ):
                            await self.store.quality("stale", "ticker_timeout")
                        await ws.send('{"method":"ping"}')
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=15)
                        except TimeoutError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("market_ws_unavailable type=%s", type(exc).__name__)
                with suppress(Exception):
                    await self.disconnect("disconnect_or_schema_error")
                delay = min(30, 2 ** min(attempt, 5)) + random.uniform(0, 0.5)
                attempt += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
            finally:
                self.connected = self.reconciled = False
                if reader is not None:
                    reader.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await reader

    async def polling(self, stop):
        refreshed = None
        while not stop.is_set():
            try:
                if self.connected and self.reconciled:
                    depth, timestamp = await self.adapter.depth(self.symbol)
                    await self.store.snapshot("depth", depth, timestamp)
                    if refreshed is None or (utc_now() - refreshed).total_seconds() >= 30:
                        await self.clock()
                        await self.refresh_context()
                        refreshed = utc_now()
                    if self.last_rules and (utc_now() - self.last_rules).total_seconds() > 3500:
                        self.rules_changed = (
                            await self.store.rules(await self.adapter.contract_rules(self.symbol))
                            or self.rules_changed
                        )
                        self.last_rules = utc_now()
                elif not self.connected:
                    # Readable fallback remains ineligible while WebSocket is disconnected.
                    ticker, timestamp = await self.adapter.ticker(self.symbol)
                    await self.store.snapshot("ticker", ticker, timestamp, "rest_fallback")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconciled = False
                logger.warning("market_rest_unavailable type=%s", type(exc).__name__)
                with suppress(Exception):
                    await self.store.quality("stale", "rest_reconciliation_failed")
                # Force reconnect and complete reconciliation instead of silently recovering.
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass

    async def run(self, stop):
        with suppress(Exception):
            await self.store.quality("stale", "starting")
        tasks = [asyncio.create_task(self.websocket(stop)), asyncio.create_task(self.polling(stop))]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                await self.disconnect("worker_stopped")
