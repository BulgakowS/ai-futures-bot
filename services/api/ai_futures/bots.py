"""Paper bot lifecycle and one-decision-per-final-candle scheduler."""

import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from ai_futures.bot_models import Bot, BotConfig, BotDecision
from ai_futures.live_pilot_settings import LivePilotDraft, LivePilotSettingsService
from ai_futures.market import market_quality
from ai_futures.models import AppState, Candle, InstrumentRules
from ai_futures.paper import (
    CloseRequest,
    OrderRequest,
    PaperError,
    PaperService,
    RiskProfile,
    json_row,
)
from ai_futures.paper_models import PaperOrder, PaperPortfolio, PaperPosition
from ai_futures.schemas import ExactDecimal, Schema, utc_now
from ai_futures.strategies import STEP, StrategyConfig, evaluate

D = Decimal
logger = logging.getLogger("ai_futures.bots")


class LearnedPaperConfig(Schema):
    kind: Literal["learned_paper"]
    max_position_notional_usdt: ExactDecimal = Field(gt=0, le=D("1000000000"))
    leverage: ExactDecimal = Field(ge=1, le=3)
    stop_loss_pct: ExactDecimal = Field(gt=0, lt=100)
    take_profit_pct: ExactDecimal = Field(gt=0, lt=100)


class BotCreate(Schema):
    name: str = Field(min_length=1, max_length=100)
    deposit: ExactDecimal = Field(gt=0, le=D("1000000000"))
    risk_profile: RiskProfile = Field(default_factory=RiskProfile)
    config: StrategyConfig | LearnedPaperConfig


def bot_kind(payload: dict) -> str:
    return "learned_paper" if payload.get("kind") == "learned_paper" else "rule_based"


class BotError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class BotService:
    def __init__(self, database, config_version):
        self.database = database
        self.config_version = config_version
        self.paper = PaperService(database, config_version)

    async def create(self, request: BotCreate):
        async with self.database.sessions.begin() as session:
            portfolio = PaperPortfolio(
                name=request.name,
                config_version=self.config_version,
                risk_profile=request.risk_profile.model_dump(mode="json"),
            )
            session.add(portfolio)
            await session.flush()
            await self.paper.new_epoch(session, portfolio, request.deposit)
            bot = Bot(portfolio_id=portfolio.id, config_version=self.config_version)
            session.add(bot)
            await session.flush()
            session.add(
                BotConfig(
                    bot_id=bot.id,
                    version=1,
                    payload=request.config.model_dump(mode="json"),
                    config_version=self.config_version,
                )
            )
            return self.describe(bot, request.config.model_dump(mode="json"))

    def describe(self, bot, config=None):
        return {
            **json_row(bot),
            "config": config,
            "bot_kind": bot_kind(config) if config is not None else "rule_based",
            "readiness_code": (
                "MODEL_NOT_APPROVED" if config and bot_kind(config) == "learned_paper" else None
            ),
        }

    async def get(self, bot_id):
        async with self.database.sessions() as session:
            bot = await session.get(Bot, bot_id)
            if not bot:
                raise BotError("BOT_NOT_FOUND", 404)
            version = await session.get(BotConfig, (bot.id, bot.latest_version))
            return self.describe(bot, version.payload)

    async def list(self):
        async with self.database.sessions() as session:
            bots = (await session.scalars(select(Bot).order_by(Bot.created_at))).all()
            configs = {
                (row.bot_id, row.version): row.payload
                for row in (
                    await session.scalars(
                        select(BotConfig).join(
                            Bot,
                            (BotConfig.bot_id == Bot.id)
                            & (BotConfig.version == Bot.latest_version),
                        )
                    )
                ).all()
            }
            return [self.describe(b, configs.get((b.id, b.latest_version))) for b in bots]

    async def configure(self, bot_id, config: StrategyConfig):
        async with self.database.sessions.begin() as session:
            bot = await self._lock(session, bot_id)
            current = await session.get(BotConfig, (bot.id, bot.latest_version))
            if bot_kind(current.payload) == "learned_paper":
                raise BotError("MODEL_NOT_APPROVED")
            if bot.status not in {"DRAFT", "READY", "PAUSED", "STOPPED", "ERROR"}:
                raise BotError("BOT_CONFIG_LOCKED")
            bot.latest_version += 1
            bot.updated_at = utc_now()
            session.add(
                BotConfig(
                    bot_id=bot.id,
                    version=bot.latest_version,
                    payload=config.model_dump(mode="json"),
                    config_version=self.config_version,
                )
            )
            return self.describe(bot, config.model_dump(mode="json"))

    async def _lock(self, session, bot_id):
        bot = await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
        if not bot:
            raise BotError("BOT_NOT_FOUND", 404)
        return bot

    async def transition(self, bot_id, action):
        async with self.database.sessions.begin() as session:
            bot = await self._lock(session, bot_id)
            config_row = await session.get(BotConfig, (bot.id, bot.latest_version))
            if action in {"ready", "start"} and bot_kind(config_row.payload) == "learned_paper":
                raise BotError("MODEL_NOT_APPROVED")
            if action == "ready" and bot.status == "DRAFT":
                bot.status = "READY"
            elif action == "pause" and bot.status == "RUNNING":
                bot.status = "PAUSED"
            elif action == "stop" and bot.status in {"RUNNING", "PAUSED", "RISK_HALTED", "ERROR"}:
                bot.status = "STOPPED"
            elif action == "start" and bot.status in {"READY", "PAUSED", "STOPPED", "RISK_HALTED"}:
                config = config_row
                state = await session.get(AppState, 1)
                quality = await market_quality(session, "BTC_USDT")
                if state is None or state.kill_switch:
                    raise BotError("KILL_SWITCH")
                if not quality["trade_eligible"]:
                    raise BotError("DATA_STALE")
                if bot.status in {"READY", "PAUSED", "STOPPED"}:
                    latest = await session.scalar(
                        select(Candle)
                        .where(
                            Candle.symbol == "BTC_USDT",
                            Candle.timeframe == config.payload["timeframe"],
                            Candle.final.is_(True),
                            Candle.source == "rest",
                        )
                        .order_by(Candle.window_start.desc())
                        .limit(1)
                    )
                    bot.last_window = latest.window_start if latest else None
                bot.active_version = bot.latest_version
                bot.status = "RUNNING"
            else:
                raise BotError("INVALID_BOT_TRANSITION")
            bot.updated_at = utc_now()
            return self.describe(bot, config_row.payload)

    async def decisions(self, bot_id):
        async with self.database.sessions() as session:
            if not await session.get(Bot, bot_id):
                raise BotError("BOT_NOT_FOUND", 404)
            rows = (
                await session.scalars(
                    select(BotDecision)
                    .where(BotDecision.bot_id == bot_id)
                    .order_by(BotDecision.window_start.desc())
                    .limit(200)
                )
            ).all()
            return [json_row(row) for row in rows]

    async def tick(self):
        async with self.database.sessions() as session:
            ids = (
                await session.scalars(
                    select(Bot.id).where(Bot.status.in_(["RUNNING", "RISK_HALTED"]))
                )
            ).all()
        for bot_id in ids:
            try:
                decision_id = await self._claim(bot_id)
                if decision_id:
                    await self._deliver(bot_id, decision_id)
            except Exception as exc:
                logger.error("bot_tick_failed bot_id=%s error=%s", bot_id, type(exc).__name__)

    async def _claim(self, bot_id):
        async with self.database.sessions.begin() as session:
            bot = await self._lock(session, bot_id)
            latest_config = await session.get(BotConfig, (bot.id, bot.latest_version))
            if bot_kind(latest_config.payload) == "learned_paper":
                bot.status = "DRAFT"
                bot.active_version = None
                bot.updated_at = utc_now()
                logger.warning("bot_execution_blocked bot_id=%s code=MODEL_NOT_APPROVED", bot_id)
                return None
            pending = await session.scalar(
                select(BotDecision)
                .where(BotDecision.bot_id == bot.id, BotDecision.status == "PROCESSING")
                .order_by(BotDecision.window_start)
                .limit(1)
            )
            if pending:
                return pending.id
            if bot.status not in {"RUNNING", "RISK_HALTED"}:
                return None
            config_row = await session.get(BotConfig, (bot.id, bot.active_version))
            config = StrategyConfig.model_validate(config_row.payload)
            state = await session.get(AppState, 1)
            if state is None or state.kill_switch:
                bot.status = "RISK_HALTED"
                bot.updated_at = utc_now()
            candle = await session.scalar(
                select(Candle)
                .where(
                    Candle.symbol == "BTC_USDT",
                    Candle.timeframe == config.timeframe,
                    Candle.final.is_(True),
                    Candle.source == "rest",
                )
                .order_by(Candle.window_start.desc())
                .limit(1)
            )
            if not candle or (bot.last_window and candle.window_start <= bot.last_window):
                return None
            quality = await market_quality(session, "BTC_USDT")
            reason = None
            if state is None or state.kill_switch:
                reason = "KILL_SWITCH"
                bot.status = "RISK_HALTED"
            elif bot.status == "RISK_HALTED":
                reason = "RISK_HALTED"
            elif not quality["trade_eligible"]:
                reason = "DATA_STALE"
            elif candle.window_start + STEP[config.timeframe] > utc_now():
                reason = "CANDLE_FUTURE"
            elif (
                utc_now() - (candle.window_start + STEP[config.timeframe])
                > STEP[config.timeframe] * 2
            ):
                reason = "CANDLE_STALE"
            elif bot.last_window and candle.window_start - bot.last_window > STEP[config.timeframe]:
                reason = "EVENT_GAP"
            positions = (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.portfolio_id == bot.portfolio_id,
                        PaperPosition.closed_at.is_(None),
                    )
                )
            ).all()
            orders = (
                await session.scalars(
                    select(PaperOrder).where(PaperOrder.portfolio_id == bot.portfolio_id)
                )
            ).all()
            terminal = {
                "FILLED",
                "CANCELED",
                "REJECTED",
                "CLOSE_CANCELED",
                "RESET_CANCELED",
                "KILL_CANCELED",
                "RISK_CANCELED",
                "PARTIALLY_FILLED_CANCELED",
                "NO_LIQUIDITY",
            }
            if any(o.status not in terminal for o in orders):
                reason = reason or "ORDER_UNRESOLVED"
            history = (
                await session.scalars(
                    select(Candle)
                    .where(
                        Candle.symbol == "BTC_USDT",
                        Candle.timeframe == config.timeframe,
                        Candle.window_start <= candle.window_start,
                    )
                    .order_by(Candle.window_start.desc())
                    .limit(config.history)
                )
            ).all()
            signal = evaluate(config, list(reversed(history)), positions[0] if positions else None)
            action, signal_reason = signal.action, signal.reason
            proposal = None
            if reason and not (action == "CLOSE" and reason in {"KILL_SWITCH", "RISK_HALTED"}):
                action, signal_reason = "WAIT", reason
            elif action == "OPEN":
                rules = await session.get(InstrumentRules, "BTC_USDT")
                if rules is None or not rules.validated:
                    action, signal_reason = "WAIT", "RULES_UNKNOWN"
                else:
                    price = D(candle.payload["close"])
                    step = D(rules.payload["priceUnit"])
                    stop_factor = (
                        1 - config.stop_distance
                        if signal.direction == "LONG"
                        else 1 + config.stop_distance
                    )
                    stop = price * stop_factor
                    rounding = ROUND_FLOOR if signal.direction == "LONG" else ROUND_CEILING
                    stop = (stop / step).to_integral_value(rounding=rounding) * step
                    key = f"bot:{bot.id}:{config.timeframe}:{candle.window_start.isoformat()}"
                    request = OrderRequest(
                        idempotency_key=key,
                        direction=signal.direction,
                        quantity=config.quantity,
                        leverage=config.leverage,
                        stop_loss=stop,
                        source=f"bot:{bot.id}:v{bot.active_version}",
                    )
                    proposal = {"request": request.model_dump(mode="json"), "position_id": None}
            elif action == "CLOSE":
                key = f"bot:{bot.id}:{config.timeframe}:{candle.window_start.isoformat()}"
                request = CloseRequest(idempotency_key=key)
                proposal = {
                    "request": request.model_dump(mode="json"),
                    "position_id": str(positions[0].id),
                }
            decision = BotDecision(
                bot_id=bot.id,
                portfolio_id=bot.portfolio_id,
                strategy_version=bot.active_version,
                timeframe=config.timeframe,
                window_start=candle.window_start,
                candle_event_id=candle.event_id,
                candle_hash=candle.payload_hash,
                action=action,
                reason=signal_reason,
                status="PROCESSING" if proposal else "DONE",
                proposal=proposal,
                completed_at=None if proposal else utc_now(),
                config_version=self.config_version,
            )
            session.add(decision)
            bot.last_window = candle.window_start
            bot.updated_at = utc_now()
            await session.flush()
            return decision.id if proposal else None

    async def _deliver(self, bot_id, decision_id):
        # Serialize lifecycle commands against submission. The claim is already durable.
        async with self.database.sessions.begin() as session:
            bot = await self._lock(session, bot_id)
            decision = await session.get(BotDecision, decision_id)
            latest_config = await session.get(BotConfig, (bot.id, bot.latest_version))
            if bot_kind(latest_config.payload) == "learned_paper":
                bot.status = "DRAFT"
                bot.active_version = None
                bot.updated_at = utc_now()
                if decision.status == "PROCESSING":
                    decision.action, decision.reason = "WAIT", "MODEL_NOT_APPROVED"
                    decision.proposal = None
                    decision.status, decision.completed_at = "DONE", utc_now()
                return
            if decision.status != "PROCESSING":
                return
            if bot.status != "RUNNING" and decision.action == "OPEN":
                decision.action, decision.reason = "WAIT", bot.status
                decision.proposal = None
                decision.status, decision.completed_at = "DONE", utc_now()
                return
            state = await session.get(AppState, 1)
            if decision.action == "OPEN" and (state is None or state.kill_switch):
                bot.status = "RISK_HALTED"
                decision.action, decision.reason = "WAIT", "KILL_SWITCH"
                decision.proposal = None
                decision.status, decision.completed_at = "DONE", utc_now()
                return
            proposal = decision.proposal
            try:
                if decision.action == "OPEN":
                    request = OrderRequest.model_validate(proposal["request"])
                    result = await self.paper.submit(bot.portfolio_id, request)
                else:
                    request = CloseRequest.model_validate(proposal["request"])
                    result = await self.paper.submit(
                        bot.portfolio_id, request, UUID(proposal["position_id"])
                    )
            except PaperError as exc:
                bot.status = "ERROR"
                decision.risk_reason = exc.code
                decision.status, decision.completed_at = "ERROR", utc_now()
                return
            decision.intent_id = UUID(result["intent"]["id"])
            decision.risk_id = UUID(result["risk"]["id"])
            decision.order_id = UUID(result["order"]["id"]) if result["order"] else None
            decision.risk_reason = result["risk"]["reason"]
            decision.status, decision.completed_at = "DONE", utc_now()


def register_bot_routes(app):
    def service():
        return BotService(app.state.database, app.state.settings.config_version)

    def pilot_settings():
        return LivePilotSettingsService(app.state.database, app.state.settings.config_version)

    @app.exception_handler(BotError)
    async def bot_error(request: Request, exc: BotError):
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": exc.code,
                "message": exc.code,
                "details": {},
                "request_id": str(request.state.request_id),
                "retryable": False,
            },
        )

    @app.get("/api/v1/bots")
    async def list_bots():
        return await service().list()

    @app.post("/api/v1/bots")
    async def create_bot(body: BotCreate):
        return await service().create(body)

    @app.get("/api/v1/bots/{bot_id}")
    async def get_bot(bot_id: UUID):
        return await service().get(bot_id)

    @app.post("/api/v1/bots/{bot_id}/config")
    async def configure_bot(bot_id: UUID, body: StrategyConfig):
        return await service().configure(bot_id, body)

    @app.get("/api/v1/bots/{bot_id}/live-pilot-settings")
    async def get_live_pilot_settings(bot_id: UUID):
        return await pilot_settings().get(bot_id)

    @app.put("/api/v1/bots/{bot_id}/live-pilot-settings")
    async def put_live_pilot_settings(bot_id: UUID, body: LivePilotDraft):
        return await pilot_settings().put(bot_id, body)

    for action in ("ready", "start", "pause", "stop"):

        def endpoint(action=action):
            async def command(bot_id: UUID):
                return await service().transition(bot_id, action)

            return command

        app.add_api_route(f"/api/v1/bots/{{bot_id}}/{action}", endpoint(), methods=["POST"])

    @app.get("/api/v1/bots/{bot_id}/decisions")
    async def bot_decisions(bot_id: UUID):
        return await service().decisions(bot_id)
