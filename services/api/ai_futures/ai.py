"""Optional OpenAI analysis. Only PaperService may turn a proposal into an order."""

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select

from ai_futures.ai_models import AiAnalysis, AiUsage
from ai_futures.market import market_quality
from ai_futures.models import Candle, MarketSnapshot
from ai_futures.paper import CloseRequest, OrderRequest, PaperError, PaperService
from ai_futures.paper_models import PaperPortfolio, PaperPosition
from ai_futures.schemas import ExactDecimal, Schema, UTCDateTime, utc_now

D = Decimal
TIMEFRAMES = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}


def digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["LONG", "SHORT", "WAIT", "CLOSE"]},
        "confidence": {"type": "string"},
        "horizon": {"type": "string", "enum": ["1m", "5m", "15m"]},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "invalidation": {"type": "string"},
        "entry": {"type": ["string", "null"]},
        "stop_loss": {"type": ["string", "null"]},
        "take_profit": {"type": ["string", "null"]},
        "context_id": {"type": "string"},
    },
    "required": [
        "action", "confidence", "horizon", "reason_codes", "explanation",
        "invalidation", "entry", "stop_loss", "take_profit", "context_id",
    ],
}


class AiPolicy(Schema):
    model: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    prompt_version: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    system_prompt: str = Field(min_length=1, max_length=1000)
    timeframe: Literal["1m", "5m", "15m"] = "1m"
    frequency_seconds: int = Field(60, ge=60, le=86400)
    quantity: ExactDecimal = Field(gt=0, le=D("1000000000"))
    leverage: ExactDecimal = Field(D(1), ge=1, le=3)
    daily_requests: int = Field(10, ge=1, le=1000)
    monthly_requests: int = Field(100, ge=1, le=10000)
    daily_usd: ExactDecimal = Field(gt=0, le=D("10000"))
    monthly_usd: ExactDecimal = Field(gt=0, le=D("100000"))
    input_usd_per_million: ExactDecimal = Field(gt=0, le=D("1000"))
    output_usd_per_million: ExactDecimal = Field(gt=0, le=D("1000"))
    max_output_tokens: int = Field(512, ge=64, le=1024)
    submit_to_paper: bool = False

    @model_validator(mode="after")
    def budget_order(self):
        if self.daily_usd > self.monthly_usd or self.daily_requests > self.monthly_requests:
            raise ValueError("Daily cap must not exceed monthly cap")
        return self

    def reservation(self):
        # Conservative local reservation; provider billing remains authoritative.
        return (D(24000) * self.input_usd_per_million +
                D(self.max_output_tokens) * self.output_usd_per_million) / D(1000000)


class AiRequest(Schema):
    analysis_id: UUID
    portfolio_id: UUID
    policy: AiPolicy


class ContextCandle(Schema):
    event_id: UUID
    source_time: UTCDateTime
    quality: Literal["final_rest"] = "final_rest"
    open: ExactDecimal = Field(gt=0)
    high: ExactDecimal = Field(gt=0)
    low: ExactDecimal = Field(gt=0)
    close: ExactDecimal = Field(gt=0)
    volume: ExactDecimal = Field(ge=0)

    @model_validator(mode="after")
    def ohlc(self):
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid OHLC")
        if self.low > self.high:
            raise ValueError("Invalid OHLC range")
        return self


class ContextPosition(Schema):
    id: UUID
    direction: Literal["LONG", "SHORT"]
    quantity: ExactDecimal = Field(gt=0)
    opened_at: UTCDateTime
    quality: Literal["paper"] = "paper"


class MarketContext(Schema):
    version: Literal["market-context-v1"] = "market-context-v1"
    context_id: str = Field(min_length=64, max_length=64)
    symbol: Literal["BTC_USDT"] = "BTC_USDT"
    timeframe: Literal["1m", "5m", "15m"]
    ticker_event_id: UUID
    ticker_source_time: UTCDateTime
    ticker_quality: Literal["ok"] = "ok"
    bid: ExactDecimal = Field(gt=0)
    ask: ExactDecimal = Field(gt=0)
    candles: list[ContextCandle] = Field(min_length=1, max_length=20)
    position: ContextPosition | None
    risk_budget: ExactDecimal = Field(gt=0)
    config_version: str = Field(min_length=1, max_length=128)


class AiProposal(Schema):
    action: Literal["LONG", "SHORT", "WAIT", "CLOSE"]
    confidence: ExactDecimal = Field(ge=0, le=1)
    horizon: Literal["1m", "5m", "15m"]
    reason_codes: list[str] = Field(min_length=1, max_length=5)
    explanation: str = Field(min_length=1, max_length=300)
    invalidation: str = Field(min_length=1, max_length=200)
    entry: ExactDecimal | None = Field(None, gt=0)
    stop_loss: ExactDecimal | None = Field(None, gt=0)
    take_profit: ExactDecimal | None = Field(None, gt=0)
    context_id: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def prices_and_codes(self):
        if self.action in {"LONG", "SHORT"} and self.stop_loss is None:
            raise ValueError("Open requires stop_loss")
        if any(not 1 <= len(code) <= 32 or not code.replace("_", "").isalnum()
               for code in self.reason_codes):
            raise ValueError("Invalid reason code")
        if self.entry is not None and self.stop_loss is not None:
            if self.action == "LONG" and self.stop_loss >= self.entry:
                raise ValueError("Long stop must be below entry")
            if self.action == "SHORT" and self.stop_loss <= self.entry:
                raise ValueError("Short stop must be above entry")
        if self.entry is not None and self.take_profit is not None:
            if self.action == "LONG" and self.take_profit <= self.entry:
                raise ValueError("Long take profit must be above entry")
            if self.action == "SHORT" and self.take_profit >= self.entry:
                raise ValueError("Short take profit must be below entry")
        return self


class OpenAIAdapter:
    """Responses API boundary; no exchange credentials or broker reference."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def propose(self, context: MarketContext, policy: AiPolicy):
        payload = {
            "model": policy.model,
            "store": False,
            "max_output_tokens": policy.max_output_tokens,
            "input": [
                {"role": "system", "content": policy.system_prompt},
                {"role": "user", "content": context.model_dump_json()},
            ],
            "text": {"format": {"type": "json_schema", "name": "paper_proposal",
                                "strict": True, "schema": PROPOSAL_SCHEMA}},
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        if response.status_code == 429:
            raise AiFailure("RATE_LIMIT")
        if response.status_code >= 400:
            raise AiFailure("PROVIDER_ERROR")
        body = response.json()
        if body.get("status") != "completed":
            raise AiFailure("INCOMPLETE")
        texts = [part.get("text") for item in body.get("output", [])
                 if item.get("type") == "message" for part in item.get("content", [])
                 if part.get("type") == "output_text"]
        if len(texts) != 1 or not isinstance(texts[0], str):
            raise AiFailure("SCHEMA_ERROR")
        return {"proposal": json.loads(texts[0]), "usage": body.get("usage")}


class AiFailure(Exception):
    def __init__(self, code):
        self.code = code


class AiService:
    def __init__(self, database, settings, adapter=None):
        self.database = database
        self.settings = settings
        self.adapter = adapter
        self.paper = PaperService(database, settings.config_version)

    async def _context(self, session, portfolio, policy):
        now = utc_now()
        quality = await market_quality(session, "BTC_USDT", now)
        if not quality["trade_eligible"]:
            raise AiFailure("STALE_CONTEXT")
        ticker = await session.get(MarketSnapshot, ("BTC_USDT", "ticker"))
        candles = (await session.scalars(select(Candle).where(
            Candle.symbol == "BTC_USDT", Candle.timeframe == policy.timeframe,
            Candle.final.is_(True), Candle.source == "rest",
        ).order_by(Candle.window_start.desc()).limit(20))).all()
        if not candles or now - (candles[0].window_start + TIMEFRAMES[policy.timeframe]) > (
            2 * TIMEFRAMES[policy.timeframe]
        ):
            raise AiFailure("STALE_CONTEXT")
        if any(
            newer.window_start - older.window_start != TIMEFRAMES[policy.timeframe]
            for newer, older in zip(candles, candles[1:], strict=False)
        ):
            raise AiFailure("CANDLE_GAP")
        positions = (await session.scalars(select(PaperPosition).where(
            PaperPosition.portfolio_id == portfolio.id,
            PaperPosition.epoch == portfolio.epoch,
            PaperPosition.closed_at.is_(None),
        ))).all()
        if len(positions) > 1:
            raise AiFailure("MULTIPLE_POSITIONS")
        position = positions[0] if positions else None
        try:
            bid, ask = D(ticker.payload["bid1"]), D(ticker.payload["ask1"])
            if not (D(0) < bid <= ask):
                raise ValueError("Invalid spread")
            rows = [ContextCandle(
                event_id=row.event_id, source_time=row.source_time,
                open=row.payload["open"], high=row.payload["high"],
                low=row.payload["low"], close=row.payload["close"],
                volume=row.payload["vol"],
            ) for row in reversed(candles)]
            data = {
                "timeframe": policy.timeframe, "ticker_event_id": ticker.event_id,
                "ticker_source_time": ticker.source_time, "bid": bid, "ask": ask,
                "candles": rows,
                "position": (ContextPosition(
                    id=position.id, direction=position.direction,
                    quantity=position.quantity, opened_at=position.opened_at,
                ) if position else None),
                "risk_budget": portfolio.risk_profile["risk_per_trade"],
                "config_version": self.settings.config_version,
            }
            canonical = MarketContext(context_id="0" * 64, **data).model_dump(mode="json")
            data["context_id"] = digest(canonical)
            context = MarketContext(**data)
            if len(context.model_dump_json().encode()) > 12000:
                raise AiFailure("CONTEXT_TOO_LARGE")
            return context
        except AiFailure:
            raise
        except (KeyError, ValueError, ValidationError, TypeError) as exc:
            raise AiFailure("INVALID_CONTEXT") from exc

    async def _budget_reason(self, session, portfolio_id, policy, now):
        start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_month = start_day.replace(day=1)
        rows = (await session.scalars(select(AiUsage).where(
            AiUsage.portfolio_id == portfolio_id,
            AiUsage.attempted.is_(True), AiUsage.occurred_at >= start_month,
        ))).all()
        day = [row for row in rows if row.occurred_at >= start_day]
        reserve = policy.reservation()
        if (len(day) >= policy.daily_requests or
            sum((row.reserved_usd for row in day), D(0)) + reserve > policy.daily_usd or
            len(rows) >= policy.monthly_requests or
            sum((row.reserved_usd for row in rows), D(0)) + reserve > policy.monthly_usd):
            return "BUDGET_EXHAUSTED"
        return None

    async def analyze(self, request: AiRequest):
        policy = request.policy
        request_hash = digest(request.model_dump(mode="json"))
        context = None
        async with self.database.sessions.begin() as session:
            portfolio = await session.scalar(select(PaperPortfolio).where(
                PaperPortfolio.id == request.portfolio_id).with_for_update())
            if portfolio is None:
                raise AiFailure("PORTFOLIO_NOT_FOUND")
            existing = await session.get(AiAnalysis, request.analysis_id)
            if existing:
                if existing.request_hash != request_hash:
                    raise AiFailure("ANALYSIS_ID_CONFLICT")
                if existing.status == "PENDING_PAPER":
                    pending = True
                else:
                    if (existing.status == "REQUESTED" and
                        utc_now() - existing.occurred_at > timedelta(seconds=30)):
                        existing.status = "DONE"
                        existing.reason = "REQUEST_UNCERTAIN"
                        existing.completed_at = utc_now()
                    return self.describe(existing)
            else:
                pending = False
                reason = None
                if not self.settings.ai_enabled or not self.settings.openai_api_key:
                    reason = "AI_DISABLED"
                else:
                    try:
                        context = await self._context(session, portfolio, policy)
                    except AiFailure as exc:
                        reason = exc.code
                    except (KeyError, ValueError, TypeError, ValidationError):
                        reason = "INVALID_CONTEXT"
                now = utc_now()
                if reason is None:
                    last = await session.scalar(select(AiUsage).where(
                        AiUsage.portfolio_id == portfolio.id, AiUsage.attempted.is_(True),
                    ).order_by(AiUsage.occurred_at.desc()).limit(1))
                    interval = timedelta(seconds=policy.frequency_seconds)
                    if last and now - last.occurred_at < interval:
                        reason = "FREQUENCY_LIMIT"
                    else:
                        reason = await self._budget_reason(session, portfolio.id, policy, now)
                attempted = reason is None
                row = AiAnalysis(
                    id=request.analysis_id, portfolio_id=portfolio.id, request_hash=request_hash,
                    status="REQUESTED" if attempted else "DONE", action="WAIT",
                    reason="IN_PROGRESS" if attempted else reason,
                    model=policy.model, prompt_version=policy.prompt_version,
                    policy={
                        **policy.model_dump(mode="json", exclude={"system_prompt"}),
                        "system_prompt_sha256": digest(policy.system_prompt),
                    },
                    context_id=context.context_id if context else None,
                    context=context.model_dump(mode="json") if context else None,
                    occurred_at=now,
                    completed_at=None if attempted else now,
                    config_version=self.settings.config_version,
                )
                session.add(row)
                session.add(AiUsage(
                    analysis_id=row.id, portfolio_id=portfolio.id, attempted=attempted,
                    reserved_usd=policy.reservation() if attempted else D(0),
                    occurred_at=now, config_version=self.settings.config_version,
                ))
                if not attempted:
                    return self.describe(row)
        if pending:
            return await self._deliver(request.analysis_id)
        adapter = self.adapter or OpenAIAdapter(
            self.settings.openai_api_key.get_secret_value())
        proposal, usage, cost = None, None, None
        try:
            raw = await asyncio.wait_for(adapter.propose(context, policy), timeout=15)
            if not isinstance(raw, dict):
                raise AiFailure("SCHEMA_ERROR")
            usage = raw.get("usage")
            if not isinstance(usage, dict):
                raise AiFailure("USAGE_MISSING")
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            if (type(input_tokens) is not int or type(output_tokens) is not int or
                input_tokens < 0 or output_tokens < 0):
                raise AiFailure("USAGE_MISSING")
            cost = (D(input_tokens) * policy.input_usd_per_million +
                    D(output_tokens) * policy.output_usd_per_million) / D(1000000)
            proposal = AiProposal.model_validate(raw["proposal"])
            if proposal.context_id != context.context_id or proposal.horizon != policy.timeframe:
                raise AiFailure("SCHEMA_ERROR")
            reason = proposal.reason_codes[0]
        except (TimeoutError, httpx.TimeoutException):
            proposal, reason = None, "TIMEOUT"
        except (ValidationError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            proposal, reason = None, "SCHEMA_ERROR"
        except AiFailure as exc:
            proposal, reason = None, exc.code
        except httpx.HTTPError:
            proposal, reason = None, "PROVIDER_ERROR"
        except Exception:
            proposal, reason = None, "PROVIDER_ERROR"
        async with self.database.sessions.begin() as session:
            row = await session.get(AiAnalysis, request.analysis_id)
            if row.status != "REQUESTED":
                return self.describe(row)
            usage_row = await session.get(AiUsage, request.analysis_id)
            if usage and cost is not None:
                usage_row.input_tokens = usage["input_tokens"]
                usage_row.output_tokens = usage["output_tokens"]
                usage_row.estimated_usd = cost
                usage_row.reserved_usd = max(usage_row.reserved_usd, cost)
            if proposal:
                row.proposal = proposal.model_dump(mode="json")
                row.action = proposal.action
                if proposal.action in {"LONG", "SHORT", "CLOSE"} and policy.submit_to_paper:
                    if proposal.action == "CLOSE":
                        position_id = context.position.id if context.position else None
                        if position_id is None:
                            row.action, reason = "WAIT", "POSITION_NOT_FOUND"
                        else:
                            row.position_id = position_id
                            row.paper_request = CloseRequest(
                                idempotency_key=f"ai:{row.id}",
                            ).model_dump(mode="json")
                    else:
                        row.paper_request = OrderRequest(
                            idempotency_key=f"ai:{row.id}", direction=proposal.action,
                            quantity=policy.quantity, leverage=policy.leverage,
                            stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
                            source=f"ai:{row.id}",
                        ).model_dump(mode="json")
                    if row.paper_request:
                        row.status = "PENDING_PAPER"
                elif proposal.action != "WAIT":
                    reason = "ANALYSIS_ONLY"
            row.reason = reason
            if row.status != "PENDING_PAPER":
                row.status, row.completed_at = "DONE", utc_now()
            if row.status == "DONE":
                return self.describe(row)
        return await self._deliver(request.analysis_id)

    async def _deliver(self, analysis_id):
        async with self.database.sessions() as session:
            row = await session.get(AiAnalysis, analysis_id)
            if row.status != "PENDING_PAPER":
                return self.describe(row)
            portfolio_id, position_id = row.portfolio_id, row.position_id
            paper_request = row.paper_request
        try:
            request = (CloseRequest if position_id else OrderRequest).model_validate(paper_request)
            result = await self.paper.submit(portfolio_id, request, position_id)
        except PaperError as exc:
            result = None
            error = exc.code
        async with self.database.sessions.begin() as session:
            row = await session.get(AiAnalysis, analysis_id)
            if row.status == "PENDING_PAPER":
                if result:
                    row.intent_id = UUID(result["intent"]["id"])
                    row.risk_id = UUID(result["risk"]["id"])
                    row.order_id = UUID(result["order"]["id"]) if result["order"] else None
                    row.risk_reason = result["risk"]["reason"]
                else:
                    row.risk_reason = error
                    row.action = "WAIT"
                row.status, row.completed_at = "DONE", utc_now()
            return self.describe(row)

    @staticmethod
    def describe(row):
        return {
            "analysis_id": str(row.id), "portfolio_id": str(row.portfolio_id),
            "status": row.status, "action": row.action, "reason": row.reason,
            "model": row.model, "prompt_version": row.prompt_version,
            "context_id": row.context_id, "proposal": row.proposal,
            "intent_id": str(row.intent_id) if row.intent_id else None,
            "risk_id": str(row.risk_id) if row.risk_id else None,
            "order_id": str(row.order_id) if row.order_id else None,
            "risk_reason": row.risk_reason, "occurred_at": row.occurred_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "config_version": row.config_version,
        }


def register_ai_routes(app):
    @app.exception_handler(AiFailure)
    async def ai_error(request: Request, exc: AiFailure):
        return JSONResponse(status_code=409, content={
            "code": exc.code, "message": exc.code, "details": {},
            "request_id": str(request.state.request_id), "retryable": False,
        })

    @app.post("/api/v1/ai/analyses")
    async def analyze(body: AiRequest):
        return await AiService(app.state.database, app.state.settings).analyze(body)

    @app.get("/api/v1/portfolios/{portfolio_id}/ai/analyses")
    async def history(portfolio_id: UUID):
        async with app.state.database.sessions() as session:
            rows = (await session.scalars(select(AiAnalysis).where(
                AiAnalysis.portfolio_id == portfolio_id,
            ).order_by(AiAnalysis.occurred_at.desc()).limit(100))).all()
            return [AiService.describe(row) for row in rows]
