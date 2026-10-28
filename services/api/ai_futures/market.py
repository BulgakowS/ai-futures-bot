"""Persistent public cache and conservative quality gate shared by API and paper core."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from ai_futures.mexc import REST_INTERVALS, MarketDataError, normalize_rules, payload_hash
from ai_futures.models import (
    Candle,
    InstrumentRules,
    InstrumentRulesObservation,
    MarketCandleObservation,
    MarketSnapshot,
)
from ai_futures.schemas import utc_now

TTLS = {"ticker": 5, "depth": 8, "funding": 90, "funding_history": 180, "quality": 35}
MARKET_CANDLE_SOURCE_VERSION = "mexc-futures-hour4-rest-v1"
INSTRUMENT_RULES_SOURCE_VERSION = "mexc-futures-contract-info-observed-v1"
RULES_REOBSERVATION_INTERVAL = timedelta(minutes=30)


class MarketStore:
    def __init__(
        self,
        database,
        config_version,
        symbol="BTC_USDT",
        journal_enabled=False,
        rules_journal_enabled=False,
    ):
        self.database = database
        self.config_version = config_version
        self.symbol = symbol
        self.journal_enabled = journal_enabled
        self.rules_journal_enabled = rules_journal_enabled

    async def snapshot(self, kind, payload, source_time, source="rest", status="ok"):
        now = utc_now()
        async with self.database.sessions.begin() as session:
            old = await session.get(MarketSnapshot, (self.symbol, kind))
            if old and old.source_time > source_time:
                return False
            if old and kind == "depth":
                previous = old.payload.get("version")
                current = payload.get("version")
                if isinstance(previous, int) and isinstance(current, int):
                    if current < previous:
                        old.status = "stale"  # A newer observation regressed the book version.
                        return False
                    if current == previous:
                        if old.status == "ok" and (
                            old.payload.get("asks"),
                            old.payload.get("bids"),
                        ) == (payload.get("asks"), payload.get("bids")):
                            return False  # A repeated public book is not new fill liquidity.
                        old.status = "stale"  # Same version with conflicting levels is a gap.
                        return False
            await session.merge(
                MarketSnapshot(
                    symbol=self.symbol,
                    kind=kind,
                    event_id=uuid4(),
                    source_time=source_time,
                    received_at=now,
                    source=source,
                    status=status,
                    payload=payload,
                    config_version=self.config_version,
                )
            )
        return True

    async def quality(self, status, reason, transport="ws", clock_skew_ms=0):
        await self.snapshot(
            "quality",
            {"reason": reason, "transport": transport, "clock_skew_ms": clock_skew_ms},
            utc_now(),
            source="worker",
            status=status,
        )

    async def rules(self, payload):
        async with self.database.sessions.begin() as session:
            observed_at = utc_now()
            if self.rules_journal_enabled:
                if (
                    self.symbol != "BTC_USDT"
                    or normalize_rules(payload["raw"], self.symbol) != payload
                ):
                    raise MarketDataError("Unverified public contract rules")
                latest = await session.scalar(
                    select(InstrumentRulesObservation)
                    .where(InstrumentRulesObservation.symbol == self.symbol)
                    .order_by(InstrumentRulesObservation.version.desc())
                    .limit(1)
                    .with_for_update()
                )
                digest = payload_hash(payload)
                if latest is not None and observed_at < latest.observed_at:
                    raise MarketDataError("Rules observation time regressed")
                if (
                    latest is None
                    or latest.payload_hash != digest
                    or observed_at - latest.observed_at >= RULES_REOBSERVATION_INTERVAL
                ):
                    session.add(
                        InstrumentRulesObservation(
                            observation_id=uuid4(),
                            causal_id=latest.causal_id if latest else uuid4(),
                            symbol=self.symbol,
                            version=latest.version + 1 if latest else 1,
                            first_seen_at=latest.first_seen_at if latest else observed_at,
                            observed_at=observed_at,
                            effective_at=observed_at,
                            payload_hash=digest,
                            rules_version=payload["version"],
                            source_version=INSTRUMENT_RULES_SOURCE_VERSION,
                            config_version=self.config_version,
                            payload=payload,
                        )
                    )
            old = await session.get(InstrumentRules, self.symbol)
            changed = old is not None and (old.version != payload["version"] or not old.validated)
            await session.merge(
                InstrumentRules(
                    symbol=self.symbol,
                    exchange="mexc",
                    validated=not changed,
                    version=payload["version"],
                    payload=payload,
                    received_at=observed_at,
                    config_version=self.config_version,
                )
            )
        return changed

    async def candles(self, timeframe, rows, source_time, source="rest"):
        async with self.database.sessions.begin() as session:
            for start, payload, final in rows:
                key = (self.symbol, timeframe, start)
                old = await session.get(Candle, key)
                digest = payload_hash(payload)
                if old and (old.final and source != "rest" or old.source_time > source_time):
                    continue
                if (
                    self.journal_enabled
                    and self.symbol == "BTC_USDT"
                    and timeframe == "4h"
                    and final
                    and source == "rest"
                ):
                    if source_time < start + timedelta(hours=4):
                        raise ValueError("Final Hour4 source time precedes candle close")
                    latest = await session.scalar(
                        select(MarketCandleObservation)
                        .where(
                            MarketCandleObservation.symbol == self.symbol,
                            MarketCandleObservation.timeframe == timeframe,
                            MarketCandleObservation.window_start == start,
                        )
                        .order_by(MarketCandleObservation.version.desc())
                        .limit(1)
                        .with_for_update()
                    )
                    if latest is None or latest.payload_hash != digest:
                        rules = await session.get(InstrumentRules, self.symbol)
                        if rules is None or not rules.validated or not rules.version:
                            raise ValueError("Validated rules required for market candle journal")
                        close = Decimal(payload["close"])
                        if not close.is_finite() or close <= 0:
                            raise ValueError("Invalid Hour4 close")
                        observed_at = utc_now()
                        session.add(
                            MarketCandleObservation(
                                observation_id=uuid4(),
                                causal_id=latest.causal_id if latest else uuid4(),
                                symbol=self.symbol,
                                timeframe=timeframe,
                                window_start=start,
                                version=latest.version + 1 if latest else 1,
                                first_seen_at=latest.first_seen_at if latest else observed_at,
                                observed_at=observed_at,
                                source_time=source_time,
                                payload_hash=digest,
                                close=format(close, "f"),
                                source=source,
                                source_version=MARKET_CANDLE_SOURCE_VERSION,
                                config_version=self.config_version,
                                rules_version=rules.version,
                            )
                        )
                if old:
                    if old.payload_hash == digest and old.final == final:
                        continue
                await session.merge(
                    Candle(
                        symbol=self.symbol,
                        timeframe=timeframe,
                        window_start=start,
                        event_id=old.event_id if old else uuid4(),
                        source_time=source_time,
                        received_at=utc_now(),
                        payload=payload,
                        payload_hash=digest,
                        final=final,
                        source=source,
                        config_version=self.config_version,
                    )
                )


async def market_quality(session, symbol, now=None):
    now = now or utc_now()
    quality = await session.get(MarketSnapshot, (symbol, "quality"))
    ticker = await session.get(MarketSnapshot, (symbol, "ticker"))
    rules = await session.get(InstrumentRules, symbol)
    reasons = []
    if quality is None or quality.status != "ok":
        reasons.append(quality.payload["reason"] if quality else "not_initialized")
    elif now - quality.received_at > timedelta(seconds=TTLS["quality"]):
        reasons.append("worker_stale")
    if ticker is None or not snapshot_fresh(ticker, now):
        reasons.append("ticker_stale")
    if rules is None or now - rules.received_at > timedelta(hours=1):
        reasons.append("rules_stale")
    elif not rules.validated:
        reasons.append("rules_changed_requires_validation")
    elif rules.payload["state"] != 0:
        reasons.append("instrument_inactive")
    return {
        "status": "stale" if reasons else "ok",
        "trade_eligible": not reasons,
        "reasons": reasons,
        "transport": quality.payload.get("transport") if quality else "unavailable",
    }


def snapshot_fresh(snapshot, now=None):
    now = now or utc_now()
    age = (now - snapshot.source_time).total_seconds()
    return snapshot.status == "ok" and -2 <= age <= TTLS.get(snapshot.kind, 30)


def snapshot_json(snapshot):
    return {
        "symbol": snapshot.symbol,
        "kind": snapshot.kind,
        "event_id": str(snapshot.event_id),
        "source_time": snapshot.source_time.isoformat(),
        "received_at": snapshot.received_at.isoformat(),
        "source": snapshot.source,
        "quality_status": "ok" if snapshot_fresh(snapshot) else "stale",
        "config_version": snapshot.config_version,
        "data": snapshot.payload,
    }


def register_market_routes(app):
    from fastapi import HTTPException, Query

    @app.get("/api/v1/markets")
    async def markets():
        async with app.state.database.sessions() as session:
            rows = (await session.scalars(select(InstrumentRules))).all()
            return [
                {
                    "symbol": row.symbol,
                    "exchange": row.exchange,
                    "rules": row.payload,
                    "received_at": row.received_at,
                    "rules_version": row.version,
                    "quality": await market_quality(session, row.symbol),
                }
                for row in rows
            ]

    @app.get("/api/v1/markets/{symbol}/ticker")
    @app.get("/api/v1/markets/{symbol}/funding")
    @app.get("/api/v1/markets/{symbol}/depth")
    async def snapshot_route(symbol: str, request: Request):
        kind = request.url.path.rsplit("/", 1)[-1]
        async with app.state.database.sessions() as session:
            row = await session.get(MarketSnapshot, (symbol, kind))
            if row is None:
                raise HTTPException(503, "Market snapshot unavailable")
            result = snapshot_json(row)
            result["quality"] = await market_quality(session, symbol)
            if kind == "funding":
                history = await session.get(MarketSnapshot, (symbol, "funding_history"))
                result["history"] = snapshot_json(history) if history else None
            return result

    @app.get("/api/v1/markets/{symbol}/quality")
    async def quality_route(symbol: str):
        async with app.state.database.sessions() as session:
            return await market_quality(session, symbol)

    @app.get("/api/v1/markets/{symbol}/candles")
    async def candles_route(
        symbol: str, timeframe: str = "1m", limit: int = Query(200, ge=1, le=2000)
    ):
        if timeframe not in REST_INTERVALS:
            raise HTTPException(422, "Unsupported timeframe")
        async with app.state.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(Candle)
                    .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                    .order_by(Candle.window_start.desc())
                    .limit(limit)
                )
            ).all()
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "quality": await market_quality(session, symbol),
                "candles": [
                    {
                        "window_start": row.window_start,
                        "source_time": row.source_time,
                        "received_at": row.received_at,
                        "final": row.final,
                        "source": row.source,
                        "event_id": str(row.event_id),
                        **row.payload,
                    }
                    for row in reversed(rows)
                ],
            }


# Module global required for FastAPI's resolution of postponed/local endpoint annotations.
from fastapi import Request  # noqa: E402
