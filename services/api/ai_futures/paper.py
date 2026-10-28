"""Paper broker/risk service. No exchange adapter or network dependency exists here."""

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import select

from ai_futures.liquidation import estimate, risk_tiers, tier_for_quantity
from ai_futures.market import market_quality, snapshot_fresh
from ai_futures.mexc import normalize_rules, payload_hash, public_fee_terms_supported
from ai_futures.models import (
    AppState,
    AuditEvent,
    Candle,
    FundingCoverage,
    FundingSettlement,
    InstrumentRules,
    MarketSnapshot,
    OutboxEvent,
    WorkerHeartbeat,
)
from ai_futures.paper_models import (
    PaperCommandResult,
    PaperEpoch,
    PaperFill,
    PaperIntent,
    PaperLedger,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperVerdict,
)
from ai_futures.schemas import ExactDecimal, Schema, utc_now

D = Decimal
ZERO = D(0)
PENDING = ("PENDING", "PARTIALLY_FILLED")
DEFAULT_PROFILE = {
    "max_leverage": "3",
    "risk_per_trade": "0.005",
    "max_total_risk": "0.015",
    "max_daily_loss": "0.03",
    "max_drawdown": "0.10",
    "max_positions": 3,
    "max_notional_equity": "3",
    "slippage_bps": "2",
    "depth_participation": "0.10",
}


class PaperError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class RiskProfile(Schema):
    max_leverage: ExactDecimal = Field(D("3"), ge=1, le=3)
    risk_per_trade: ExactDecimal = Field(D("0.005"), gt=0, le=D("0.005"))
    max_total_risk: ExactDecimal = Field(D("0.015"), gt=0, le=D("0.015"))
    max_daily_loss: ExactDecimal = Field(D("0.03"), gt=0, le=D("0.03"))
    max_drawdown: ExactDecimal = Field(D("0.10"), gt=0, le=D("0.10"))
    max_positions: int = Field(3, ge=1, le=3)
    max_notional_equity: ExactDecimal = Field(D("3"), gt=0, le=3)
    slippage_bps: ExactDecimal = Field(D("2"), ge=2, le=100)
    depth_participation: ExactDecimal = Field(D("0.10"), gt=0, le=D("0.10"))


class PortfolioCreate(Schema):
    name: str = Field("Paper", min_length=1, max_length=100)
    deposit: ExactDecimal = Field(D("1000"), gt=0, le=D("1000000000"))
    risk_profile: RiskProfile = Field(default_factory=RiskProfile)


class OrderRequest(Schema):
    idempotency_key: str = Field(min_length=1, max_length=128)
    symbol: Literal["BTC_USDT"] = "BTC_USDT"
    direction: Literal["LONG", "SHORT"]
    kind: Literal["market", "limit"] = "market"
    quantity: ExactDecimal = Field(gt=0)
    leverage: ExactDecimal = Field(D(1), ge=1)
    limit_price: ExactDecimal | None = Field(None, gt=0)
    stop_loss: ExactDecimal | None = Field(None, gt=0)
    take_profit: ExactDecimal | None = Field(None, gt=0)
    source: str = Field("manual", min_length=1, max_length=64)


class CloseRequest(Schema):
    idempotency_key: str = Field(min_length=1, max_length=128)
    quantity: ExactDecimal | None = Field(None, gt=0)
    reason: Literal["manual", "stop_loss", "take_profit", "liquidation_estimate"] = "manual"


class ResetRequest(Schema):
    deposit: ExactDecimal = Field(D("1000"), gt=0, le=D("1000000000"))


class KillRequest(Schema):
    kill_switch: bool


class RulesAck(Schema):
    expected_version: str = Field(min_length=64, max_length=64)


def rounded(value, step, up=False):
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def cash(value):
    return value.quantize(D("0.000000000001"))


def valid_book(depth, price_step, volume_step):
    """A REST book is usable only as one complete, ordered, tick-aligned snapshot."""
    try:
        book = depth.payload
        version = book["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            return False
        for side in ("asks", "bids"):
            rows = book[side]
            if not isinstance(rows, list) or not rows:
                return False
            prices = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    return False
                price, quantity = D(row[0]), D(row[1])
                if (
                    not price.is_finite() or not quantity.is_finite()
                    or price <= 0 or quantity <= 0
                    or rounded(price, price_step) != price
                    or rounded(quantity, volume_step) != quantity
                ):
                    return False
                prices.append(price)
            if prices != sorted(prices, reverse=side == "bids") or len(prices) != len(set(prices)):
                return False
        return D(book["bids"][0][0]) <= D(book["asks"][0][0])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def json_row(row):
    result = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, Decimal | UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        result[col.name] = value
    return result


class PaperService:
    def __init__(self, database, config_version):
        self.database, self.config_version = database, config_version

    async def lock(self, session, portfolio_id):
        row = await session.scalar(
            select(PaperPortfolio).where(PaperPortfolio.id == portfolio_id).with_for_update()
        )
        if row is None:
            raise PaperError("PORTFOLIO_NOT_FOUND", 404)
        return row

    async def state(self, session):
        # Shared lock permits concurrent portfolios, kill mutation takes the exclusive lock.
        return await session.scalar(
            select(AppState).where(AppState.id == 1).with_for_update(read=True)
        )

    def post(self, session, portfolio, correlation, key, reason, amount, provenance=None):
        amount = cash(amount)
        session.add(
            PaperLedger(
                portfolio_id=portfolio.id,
                epoch=portfolio.epoch,
                correlation_id=correlation,
                event_key=key,
                reason=reason,
                counteraccount=reason.lower(),
                wallet_delta=amount,
                offset_delta=-amount,
                provenance=provenance or {},
                config_version=self.config_version,
            )
        )

    async def create(self, request):
        async with self.database.sessions.begin() as session:
            row = PaperPortfolio(
                name=request.name,
                config_version=self.config_version,
                risk_profile=request.risk_profile.model_dump(mode="json"),
            )
            session.add(row)
            await session.flush()
            await self.new_epoch(session, row, request.deposit)
            await session.flush()
            return await self.summary(session, row)

    async def new_epoch(self, session, row, deposit):
        now = utc_now()
        session.add(
            PaperEpoch(
                portfolio_id=row.id,
                number=row.epoch,
                deposit=deposit,
                peak_equity=deposit,
                day_start_equity=deposit,
                risk_day=now.date().isoformat(),
                config_version=self.config_version,
            )
        )
        self.post(session, row, uuid4(), f"deposit:{row.epoch}", "DEPOSIT", deposit)
        session.add(
            AuditEvent(
                correlation_id=row.id,
                config_version=self.config_version,
                actor="paper",
                action="paper.epoch_created",
                payload={"epoch": row.epoch, "deposit": str(deposit)},
            )
        )

    async def summary(self, session, portfolio):
        positions = (
            await session.scalars(
                select(PaperPosition).where(
                    PaperPosition.portfolio_id == portfolio.id,
                    PaperPosition.epoch == portfolio.epoch,
                    PaperPosition.closed_at.is_(None),
                )
            )
        ).all()
        orders = (
            await session.scalars(
                select(PaperOrder).where(
                    PaperOrder.portfolio_id == portfolio.id,
                    PaperOrder.epoch == portfolio.epoch,
                    PaperOrder.status.in_(PENDING),
                )
            )
        ).all()
        ledger = (
            await session.scalars(
                select(PaperLedger).where(
                    PaperLedger.portfolio_id == portfolio.id, PaperLedger.epoch == portfolio.epoch
                )
            )
        ).all()
        wallet = sum((row.wallet_delta for row in ledger), ZERO)
        margin = sum((row.margin for row in positions), ZERO)
        reserve = sum((row.reserve for row in orders), ZERO)
        pnl, known = ZERO, True
        for pos in positions:
            ticker = await session.get(MarketSnapshot, (pos.symbol, "ticker"))
            quality = await market_quality(session, pos.symbol)
            if ticker is None or not snapshot_fresh(ticker) or not quality["trade_eligible"]:
                known = False
                continue
            sign = D(1) if pos.direction == "LONG" else D(-1)
            pnl += (
                (D(ticker.payload["fairPrice"]) - pos.average_entry)
                * pos.quantity
                * pos.multiplier
                * sign
            )
        epoch = await session.get(PaperEpoch, (portfolio.id, portfolio.epoch))
        funding_unresolved, funding_gap, funding_reason = await self.funding_status(
            session, portfolio
        )
        known = known and not funding_unresolved
        return {
            **json_row(portfolio),
            "wallet_balance": str(wallet),
            "initial_deposit": format(cash(epoch.deposit), "f"),
            "realized_pnl_net": format(cash(wallet - epoch.deposit), "f"),
            "total_pnl_net": format(cash(wallet + pnl - epoch.deposit), "f") if known else None,
            "reserved_margin": str(margin),
            "pending_order_reserve": str(reserve),
            "available_balance": str(wallet - margin - reserve),
            "unrealized_pnl": str(cash(pnl)) if known else None,
            "equity": str(cash(wallet + pnl)) if known else None,
            "valuation_status": "confirmed_snapshot"
            if known
            else ("FUNDING_UNRESOLVED" if funding_unresolved else "DATA_STALE"),
            "pending_funding_status": "unresolved" if funding_unresolved else "reconciled",
            "funding_gap_time": funding_gap.isoformat() if funding_gap else None,
            "funding_unresolved_reason": funding_reason,
            "peak_equity": str(epoch.peak_equity),
            "day_start_equity": str(epoch.day_start_equity),
            "margin_mode": "isolated",
            "position_mode": "one_way",
            "mode": "paper",
            "positions": [
                await self.position_json(session, p, not funding_unresolved)
                for p in positions
            ],
            "pending_orders": [json_row(o) for o in orders],
        }

    async def position_json(self, session, pos, funding_resolved):
        value = json_row(pos)
        rules, ticker, _ = await self.context(session, pos.symbol)
        quality = await market_quality(session, pos.symbol)
        supported = (
            rules is not None and rules.validated
            and utc_now() - rules.received_at <= timedelta(hours=1)
            and public_fee_terms_supported(rules.payload)
            and rules.payload.get("state") == 0
        )
        valid_mark = ticker is not None and snapshot_fresh(ticker) and quality["trade_eligible"]
        result = None
        if (
            pos.closed_at is None
            and pos.quantity > 0
            and supported
            and valid_mark
            and funding_resolved
        ):
            try:
                result = estimate(pos, rules.payload, D(ticker.payload["fairPrice"]))
            except (KeyError, ValueError, ArithmeticError):
                pass
        value["liquidation_estimate"] = str(result["price"]) if result else None
        value["liquidation_provenance"] = {
            "model": "model_estimate", "status": "estimated" if result else "unknown",
            "reason": None if result else (
                "POSITION_CLOSED" if pos.closed_at is not None else
                "FUNDING_UNRESOLVED" if not funding_resolved else
                "DATA_STALE" if not valid_mark else "RULES_UNKNOWN"
            ),
            "rules_version": rules.version if rules else None,
            "rules_config_version": rules.config_version if rules else None,
            "config_version": self.config_version,
            "mark_event_id": str(ticker.event_id) if valid_mark else None,
            "mark_source": "public_fair_price" if valid_mark else None,
            "observed_at_utc": utc_now().isoformat(),
            "tier_level": result["tier"]["level"] if result else None,
            "tier_max_vol_contracts": str(result["tier"]["max_vol"]) if result else None,
            "mmr": str(result["tier"]["mmr"]) if result else None,
            "imr": str(result["tier"]["imr"]) if result else None,
            "max_leverage": str(result["tier"]["max_leverage"]) if result else None,
            "assumptions": (
                "Public CUSTOM tier by cumulative position contracts; isolated posted margin "
                "plus settled funding once; entry fees already posted to wallet; projected "
                "taker close and liquidation fees; private risk level, ADL and MEXC "
                "liquidation formula unverified"
            ),
        }
        return value

    async def record_verdict(self, session, intent, reason, snapshot):
        row = PaperVerdict(
            intent_id=intent.id,
            outcome="REJECT" if reason else "ALLOW",
            reason=reason or "RISK_OK",
            snapshot=snapshot,
            config_version=self.config_version,
        )
        session.add(row)
        await session.flush()  # Persist gate before any order/fill.
        return row

    async def context(self, session, symbol):
        rules = await session.scalar(
            select(InstrumentRules)
            .where(InstrumentRules.symbol == symbol)
            .with_for_update(read=True)
        )
        ticker = await session.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.symbol == symbol, MarketSnapshot.kind == "ticker")
            .with_for_update(read=True)
        )
        depth = await session.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.symbol == symbol, MarketSnapshot.kind == "depth")
            .with_for_update(read=True)
        )
        return rules, ticker, depth

    async def check(self, session, portfolio, payload, reduce_position=None, pending=None):
        state = await self.state(session)
        rules, ticker, depth = await self.context(session, payload["symbol"])
        quality = await market_quality(session, payload["symbol"])
        snapshot = {
            "quality": quality,
            "ticker_id": str(ticker.event_id) if ticker else None,
            "depth_id": str(depth.event_id) if depth else None,
            "rules_version": rules.version if rules else None,
            "reduce_only": bool(reduce_position),
            "profile": portfolio.risk_profile,
        }
        if not reduce_position and (state is None or state.kill_switch):
            return "KILL_SWITCH", snapshot
        if not reduce_position:
            heartbeat = await session.get(WorkerHeartbeat, "trading")
            age = (utc_now() - heartbeat.occurred_at).total_seconds() if heartbeat else None
            snapshot["worker_heartbeat_age_seconds"] = age
            if (
                heartbeat is None
                or heartbeat.status != "idle"
                or not 0 <= age < self.database.settings.worker_stale_seconds
            ):
                return "WORKER_STALE", snapshot
        if not quality["trade_eligible"]:
            return "DATA_STALE", snapshot
        if ticker is None or not snapshot_fresh(ticker):
            return "DATA_STALE", snapshot
        if depth is None or not snapshot_fresh(depth):
            return "DEPTH_STALE", snapshot
        if (
            rules is None
            or not rules.validated
            or utc_now() - rules.received_at > timedelta(hours=1)
        ):
            return "RULES_UNKNOWN", snapshot
        if not public_fee_terms_supported(rules.payload) or rules.payload.get("state") != 0:
            return "RULES_UNKNOWN", snapshot
        r, p = rules.payload, portfolio.risk_profile
        try:
            for field in (
                "contractSize",
                "priceUnit",
                "volUnit",
                "minVol",
                "maxVol",
                "maxLeverage",
            ):
                if D(r[field]) <= 0:
                    return "RULES_UNKNOWN", snapshot
            if not valid_book(depth, D(r["priceUnit"]), D(r["volUnit"])):
                return "DEPTH_INVALID", snapshot
            fee = D(r["takerFeeRate"])
            if not ZERO <= fee < D("0.1"):
                return "RULES_UNKNOWN", snapshot
            snapshot["fee_source"] = "public_contract_rules_taker"
            snapshot["fee_rate"] = str(fee)
            snapshot["fee_rate_mode"] = r["feeRateMode"]
            qty = D(payload["quantity"])
            if qty < D(r["minVol"]) or qty > D(r["maxVol"]):
                return "CONTRACT_SIZE", snapshot
            if qty != rounded(qty, D(r["volUnit"])):
                return "QUANTITY_PRECISION", snapshot
            for field in ("limit_price", "stop_loss", "take_profit"):
                if payload.get(field) and D(payload[field]) != rounded(
                    D(payload[field]), D(r["priceUnit"])
                ):
                    return "PRICE_PRECISION", snapshot
            if reduce_position:
                if qty > reduce_position.quantity or reduce_position.closed_at:
                    return "REDUCE_ONLY_VIOLATION", snapshot
                if reduce_position.multiplier != D(r["contractSize"]):
                    return "RULES_CHANGED", snapshot
                return None, snapshot
            own_quantity = ZERO
            if pending and pending.payload.get("position_id"):
                own_position = await session.get(
                    PaperPosition, UUID(pending.payload["position_id"])
                )
                if own_position and own_position.closed_at is None:
                    own_quantity = own_position.quantity
            tier = tier_for_quantity(r, qty + own_quantity)
            if tier is None:
                return "RISK_TIER_UNKNOWN", snapshot
            snapshot["risk_tier"] = {
                "level": tier["level"], "max_vol_contracts": str(tier["max_vol"]),
                "mmr": str(tier["mmr"]), "imr": str(tier["imr"]),
                "max_leverage": str(tier["max_leverage"]),
                "rules_version": rules.version,
            }
            leverage = D(payload["leverage"])
            if leverage > min(D(p["max_leverage"]), D(r["maxLeverage"]),
                              tier["max_leverage"]) or leverage < D(
                r.get("minLeverage", 1)
            ):
                return "LEVERAGE_LIMIT", snapshot
            if 1 / leverage < tier["imr"]:
                return "LEVERAGE_LIMIT", snapshot
            direction = payload["direction"]
            ask, bid = D(ticker.payload["ask1"]), D(ticker.payload["bid1"])
            price = (
                D(payload["limit_price"])
                if payload.get("limit_price")
                else (ask if direction == "LONG" else bid)
            )
            worst = (
                price if payload.get("limit_price") else rounded(
                    price * (1 + 2 * D(p["slippage_bps"]) / 10000
                             * (1 if direction == "LONG" else -1)),
                    D(r["priceUnit"]), direction == "LONG",
                )
            )
            sl = D(payload["stop_loss"]) if payload.get("stop_loss") else None
            if (
                sl is None
                or (direction == "LONG" and sl >= min(price, bid))
                or (direction == "SHORT" and sl <= max(price, ask))
            ):
                return "STOP_LOSS_INVALID", snapshot
            tp = D(payload["take_profit"]) if payload.get("take_profit") else None
            if tp and (
                (direction == "LONG" and tp <= price) or (direction == "SHORT" and tp >= price)
            ):
                return "TAKE_PROFIT_INVALID", snapshot
            summary = await self.summary(session, portfolio)
            snapshot["account"] = {
                k: summary[k] for k in ("equity", "available_balance", "wallet_balance")
            }
            if summary["pending_funding_status"] == "unresolved":
                return "FUNDING_UNRESOLVED", snapshot
            if summary["equity"] is None:
                return "DATA_STALE", snapshot
            equity = D(summary["equity"])
            epoch = await session.get(PaperEpoch, (portfolio.id, portfolio.epoch))
            if equity <= 0:
                return "INSOLVENT", snapshot
            if equity <= epoch.day_start_equity * (1 - D(p["max_daily_loss"])):
                return "DAILY_LOSS_LIMIT", snapshot
            if equity <= epoch.peak_equity * (1 - D(p["max_drawdown"])):
                return "DRAWDOWN_LIMIT", snapshot
            positions, orders = summary["positions"], summary["pending_orders"]
            orders = [o for o in orders if pending is None or o["id"] != str(pending.id)]
            if any(x["symbol"] == payload["symbol"] for x in positions + orders):
                # A pending order can add fills to its own position, never a separate intent.
                own = pending.payload.get("position_id") if pending else None
                if not own or any(
                    x["symbol"] == payload["symbol"] and x["id"] != own for x in positions + orders
                ):
                    return "POSITION_CONFLICT", snapshot
            if not pending and len(positions) + len(orders) >= p["max_positions"]:
                return "MAX_POSITIONS", snapshot
            multiplier = D(r["contractSize"])
            notional = qty * multiplier * max(price, worst)
            reserve = notional / leverage + notional * fee
            available = D(summary["available_balance"]) + (pending.reserve if pending else ZERO)
            if reserve > available:
                return "INSUFFICIENT_MARGIN", snapshot
            risk = (
                qty
                * multiplier
                * (abs(worst - sl) + (worst + sl) * fee + worst * D(p["slippage_bps"]) / 10000)
            )
            snapshot["trade_risk"] = str(risk)
            snapshot["margin_and_fee_reserve"] = str(reserve)
            if risk > equity * D(p["risk_per_trade"]):
                return "TRADE_RISK_LIMIT", snapshot
            total_risk, exposure = risk, notional
            for pos in positions:
                total_risk += (
                    D(pos["quantity"])
                    * D(pos["multiplier"])
                    * (
                        abs(D(pos["average_entry"]) - D(pos["stop_loss"]))
                        + (D(pos["average_entry"]) + D(pos["stop_loss"])) * fee
                    )
                )
                exposure += D(pos["quantity"]) * D(pos["multiplier"]) * D(pos["average_entry"])
            for order in orders:
                total_risk += D(order["payload"].get("reserved_risk", "0"))
                exposure += D(order["payload"].get("reserved_notional", "0"))
            if total_risk > equity * D(p["max_total_risk"]):
                return "TOTAL_RISK_LIMIT", snapshot
            if exposure > equity * D(p["max_notional_equity"]):
                return "EXPOSURE_LIMIT", snapshot
            return None, snapshot
        except (KeyError, ValueError, ArithmeticError):
            return "RULES_UNKNOWN", snapshot

    async def submit(self, portfolio_id, request, position_id=None):
        payload = request.model_dump(mode="json")
        payload["action"] = "close" if position_id else "open"
        if position_id:
            payload["position_id"] = str(position_id)
        async with self.database.sessions.begin() as session:
            portfolio = await self.lock(session, portfolio_id)
            existing = await session.scalar(
                select(PaperIntent).where(
                    PaperIntent.portfolio_id == portfolio_id,
                    PaperIntent.idempotency_key == request.idempotency_key,
                )
            )
            digest = payload_hash(payload)
            if existing:
                if existing.payload_hash != digest:
                    raise PaperError("IDEMPOTENCY_CONFLICT")
                return (await session.get(PaperCommandResult, existing.id)).payload
            intent = PaperIntent(
                portfolio_id=portfolio.id,
                epoch=portfolio.epoch,
                idempotency_key=request.idempotency_key,
                payload_hash=digest,
                payload=payload,
                config_version=self.config_version,
            )
            session.add(intent)
            await session.flush()
            position = None
            if position_id:
                position = await session.get(PaperPosition, position_id)
                if (
                    position is None
                    or position.portfolio_id != portfolio.id
                    or position.epoch != portfolio.epoch
                    or position.closed_at
                ):
                    verdict = await self.record_verdict(session, intent, "POSITION_NOT_FOUND", {})
                    result = {"intent": json_row(intent), "risk": json_row(verdict), "order": None}
                    session.add(PaperCommandResult(intent_id=intent.id, payload=result))
                    return result
                payload = {
                    **payload,
                    "symbol": position.symbol,
                    "direction": position.direction,
                    "quantity": payload.get("quantity") or str(position.quantity),
                    "kind": "market",
                    "leverage": str(position.leverage),
                }
            elif payload["kind"] == "limit" and not payload.get("limit_price"):
                verdict = await self.record_verdict(session, intent, "LIMIT_PRICE_REQUIRED", {})
                result = {"intent": json_row(intent), "risk": json_row(verdict), "order": None}
                session.add(PaperCommandResult(intent_id=intent.id, payload=result))
                return result
            if position is None:
                await self.funding(session, portfolio)
                await session.flush()
            reason, snapshot = await self.check(session, portfolio, payload, position)
            verdict = await self.record_verdict(session, intent, reason, snapshot)
            order = None
            if not reason:
                if position is not None:
                    pending_orders = (
                        await session.scalars(
                            select(PaperOrder).where(
                                PaperOrder.portfolio_id == portfolio.id,
                                PaperOrder.epoch == portfolio.epoch,
                                PaperOrder.symbol == position.symbol,
                                PaperOrder.status.in_(PENDING),
                            )
                        )
                    ).all()
                    for pending_order in pending_orders:
                        pending_order.status, pending_order.reserve = "CLOSE_CANCELED", ZERO
                order = PaperOrder(
                    intent_id=intent.id,
                    verdict_id=verdict.id,
                    portfolio_id=portfolio.id,
                    epoch=portfolio.epoch,
                    symbol=payload["symbol"],
                    direction=payload["direction"],
                    kind=payload["kind"],
                    status="PENDING",
                    quantity=D(payload["quantity"]),
                    filled_quantity=ZERO,
                    reserve=ZERO,
                    payload={
                        **payload,
                        "rules_version": snapshot["rules_version"],
                        "fill_model": "estimated_rest_snapshot",
                        "limit_model": (
                            "crossing_rest_book_no_queue" if payload["kind"] == "limit" else None
                        ),
                    },
                    config_version=self.config_version,
                )
                session.add(order)
                await session.flush()
                await self.execute(session, portfolio, order, position)
            result = {
                "intent": json_row(intent),
                "risk": json_row(verdict),
                "order": json_row(order) if order else None,
            }
            session.add(PaperCommandResult(intent_id=intent.id, payload=result))
            return result

    async def execute(self, session, portfolio, order, position=None):
        # Broker itself enforces provenance; callers cannot bypass the gate.
        await self.lock(session, portfolio.id)
        verdict = await session.get(PaperVerdict, order.verdict_id)
        intent = await session.get(PaperIntent, order.intent_id)
        if (
            not verdict
            or verdict.outcome != "ALLOW"
            or not intent
            or verdict.intent_id != intent.id
            or intent.portfolio_id != portfolio.id
            or intent.epoch != portfolio.epoch
            or order.portfolio_id != portfolio.id
            or order.epoch != portfolio.epoch
            or order.status not in PENDING
            or verdict.snapshot.get("reduce_only") != (order.payload.get("action") == "close")
            or intent.payload.get("action") != order.payload.get("action")
        ):
            raise PaperError("BROKER_RISK_VERDICT_REQUIRED")
        rules, ticker, depth = await self.context(session, order.symbol)
        if (
            not rules
            or not ticker
            or not depth
            or not snapshot_fresh(ticker)
            or not snapshot_fresh(depth)
            or not rules.validated
            or not public_fee_terms_supported(rules.payload)
            or not valid_book(depth, D(rules.payload["priceUnit"]), D(rules.payload["volUnit"]))
            or verdict.snapshot.get("ticker_id") != str(ticker.event_id)
            or verdict.snapshot.get("depth_id") != str(depth.event_id)
            or verdict.snapshot.get("rules_version") != rules.version
            or not (await market_quality(session, order.symbol))["trade_eligible"]
        ):
            raise PaperError("BROKER_CONTEXT_CHANGED")
        state = await self.state(session)
        if order.payload["action"] != "close" and (not state or state.kill_switch):
            raise PaperError("KILL_SWITCH")
        r, p, payload = rules.payload, portfolio.risk_profile, dict(order.payload)
        remaining = order.quantity - order.filled_quantity
        reducing = payload["action"] == "close"
        if reducing and (
            position is None
            or position.id != UUID(payload["position_id"])
            or position.portfolio_id != portfolio.id
            or position.epoch != portfolio.epoch
            or position.closed_at is not None
            or position.symbol != order.symbol
            or position.direction != order.direction
            or remaining > position.quantity
        ):
            raise PaperError("BROKER_REDUCE_ONLY_VIOLATION")
        buying = (order.direction == "LONG") != reducing
        top = D(ticker.payload["ask1" if buying else "bid1"])
        slip = D(p["slippage_bps"]) / 10000
        limit = D(payload["limit_price"]) if payload.get("limit_price") else None
        fills, left = [], remaining
        if payload.get("last_depth_id") != str(depth.event_id):
            for raw_price, raw_qty in depth.payload["asks" if buying else "bids"]:
                book_price = D(raw_price)
                price = max(book_price, top) if buying else min(book_price, top)
                price = rounded(
                    price * (1 + slip if buying else 1 - slip), D(r["priceUnit"]), buying
                )
                if limit is not None and (price > limit if buying else price < limit):
                    break
                # One scenario slippage allowance plus one allowance for visible book movement.
                bound = rounded(
                    top * (1 + 2 * slip if buying else 1 - 2 * slip),
                    D(r["priceUnit"]), buying,
                )
                if not limit and (price > bound if buying else price < bound):
                    break
                qty = min(left, rounded(D(raw_qty) * D(p["depth_participation"]), D(r["volUnit"])))
                if qty <= 0 or (not reducing and qty < D(r["minVol"])):
                    continue
                fills.append((qty, price))
                left -= qty
                if left == 0:
                    break
        payload["last_depth_id"] = str(depth.event_id)
        multiplier, fee_rate = D(r["contractSize"]), D(r["takerFeeRate"])
        if not reducing and position is None and fills:
            own = payload.get("position_id")
            position = await session.get(PaperPosition, UUID(own)) if own else None
            if position is None:
                position = PaperPosition(
                    portfolio_id=portfolio.id,
                    epoch=portfolio.epoch,
                    symbol=order.symbol,
                    direction=order.direction,
                    quantity=ZERO,
                    average_entry=ZERO,
                    margin=ZERO,
                    leverage=D(payload["leverage"]),
                    multiplier=multiplier,
                    stop_loss=D(payload["stop_loss"]),
                    take_profit=D(payload["take_profit"]) if payload.get("take_profit") else None,
                    realized_pnl=ZERO,
                    fees=ZERO,
                    funding=ZERO,
                    rules_version=rules.version,
                    config_version=self.config_version,
                )
                session.add(position)
                await session.flush()
            payload["position_id"] = str(position.id)
        for qty, price in fills:
            fee = cash(qty * multiplier * price * fee_rate)
            liquidation_fee = (
                cash(qty * multiplier * price * D(r["liquidationFeeRate"]))
                if reducing and payload.get("reason") == "liquidation_estimate"
                else ZERO
            )
            pnl = ZERO
            if reducing:
                sign = D(1) if position.direction == "LONG" else D(-1)
                pnl = cash((price - position.average_entry) * qty * multiplier * sign)
                remaining_fraction = 1 - qty / position.quantity
                position.margin = cash(position.margin * remaining_fraction)
                position.funding = cash(position.funding * remaining_fraction)
                position.quantity -= qty
                position.realized_pnl += pnl
                if position.quantity == 0:
                    position.closed_at = utc_now()
            else:
                total = position.quantity + qty
                position.average_entry = (
                    position.average_entry * position.quantity + price * qty
                ) / total
                position.quantity = total
                position.margin += cash(qty * multiplier * price / position.leverage)
            position.fees += fee + liquidation_fee
            fill = PaperFill(
                order_id=order.id,
                position_id=position.id,
                quantity=qty,
                price=price,
                fee=fee + liquidation_fee,
                realized_pnl=pnl,
                exposure_delta=-qty if reducing else qty,
                provenance={
                    "model": "estimated_fill",
                    "depth_id": str(depth.event_id),
                    "depth_version": depth.payload["version"],
                    "ticker_id": str(ticker.event_id),
                    "rules_version": rules.version,
                    "rules_config_version": rules.config_version,
                    "fee_source": "public_contract_rules_taker",
                    "fee_rate": str(fee_rate),
                    "fee_rate_mode": r["feeRateMode"],
                    "fee_rate_type": r["feeRateType"],
                    "public_maker_rate": r["makerFeeRate"],
                    "public_taker_rate": r["takerFeeRate"],
                    "trading_fee": str(fee),
                    "liquidation_fee": str(liquidation_fee),
                    "liquidation_fee_source": "public_contract_rules_liquidation_rate",
                    "liquidation_model": (
                        {
                            "model": "model_estimate",
                            "tier_level": tier_for_quantity(r, position.quantity + qty)["level"],
                            "rules_version": rules.version,
                            "rules_config_version": rules.config_version,
                            "mark_event_id": str(ticker.event_id),
                            "mark_source": "public_fair_price",
                            "assumptions": "isolated public tier; private formula/ADL unverified",
                        }
                        if reducing and payload.get("reason") == "liquidation_estimate"
                        and tier_for_quantity(r, position.quantity + qty)
                        else None
                    ),
                    "close_reason": payload.get("reason"),
                    "slippage_bps": p["slippage_bps"],
                    "depth_participation": p["depth_participation"],
                    "limit_model": "crossing_rest_book_no_queue" if order.kind == "limit" else None,
                },
                config_version=self.config_version,
            )
            session.add(fill)
            await session.flush()
            self.post(
                session, portfolio, order.intent_id, f"fee:{fill.id}", "FEE", -fee, fill.provenance
            )
            if liquidation_fee:
                self.post(
                    session,
                    portfolio,
                    order.intent_id,
                    f"liquidation-fee:{fill.id}",
                    "LIQUIDATION_FEE",
                    -liquidation_fee,
                    fill.provenance,
                )
            if reducing:
                self.post(
                    session, portfolio, order.intent_id, f"pnl:{fill.id}", "REALIZED_PNL", pnl
                )
            old = order.filled_quantity
            order.filled_quantity += qty
            order.average_price = (
                (order.average_price or ZERO) * old + price * qty
            ) / order.filled_quantity
        order.updated_at = utc_now()
        order.status = (
            "FILLED" if left == 0 else ("PARTIALLY_FILLED" if order.filled_quantity else "PENDING")
        )
        if order.kind == "market" and left:
            order.status = "PARTIALLY_FILLED_CANCELED" if order.filled_quantity else "NO_LIQUIDITY"
        if order.kind == "limit" and left:
            notional = left * multiplier * limit
            order.reserve = cash(notional / D(payload["leverage"]) + notional * fee_rate)
            payload["reserved_notional"] = str(notional)
            payload["reserved_risk"] = str(
                left
                * multiplier
                * (
                    abs(limit - D(payload["stop_loss"]))
                    + (limit + D(payload["stop_loss"])) * fee_rate
                )
            )
        else:
            order.reserve = ZERO
        order.payload = payload
        await session.flush()

    async def cancel(self, portfolio_id, order_id):
        async with self.database.sessions.begin() as session:
            await self.lock(session, portfolio_id)
            order = await session.get(PaperOrder, order_id)
            if order is None or order.portfolio_id != portfolio_id:
                raise PaperError("ORDER_NOT_FOUND", 404)
            if order.status in PENDING:
                order.status, order.reserve, order.updated_at = "CANCELED", ZERO, utc_now()
                session.add(
                    AuditEvent(
                        correlation_id=order.intent_id,
                        actor="paper",
                        action="paper.order_canceled",
                        config_version=self.config_version,
                        payload={"order_id": str(order.id)},
                    )
                )
            return json_row(order)

    async def reset(self, portfolio_id, request):
        async with self.database.sessions.begin() as session:
            row = await self.lock(session, portfolio_id)
            positions = (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.portfolio_id == row.id,
                        PaperPosition.epoch == row.epoch,
                        PaperPosition.closed_at.is_(None),
                    )
                )
            ).all()
            if positions:
                raise PaperError("RESET_OPEN_POSITIONS")
            orders = (
                await session.scalars(
                    select(PaperOrder).where(
                        PaperOrder.portfolio_id == row.id,
                        PaperOrder.epoch == row.epoch,
                        PaperOrder.status.in_(PENDING),
                    )
                )
            ).all()
            for order in orders:
                order.status, order.reserve = "RESET_CANCELED", ZERO
            await self.funding(session, row)
            await session.flush()
            if await self.unresolved_funding(session, row):
                raise PaperError("FUNDING_UNRESOLVED")
            row.epoch += 1
            await self.new_epoch(session, row, request.deposit)
            await session.flush()
            return await self.summary(session, row)

    async def kill(self, value):
        async with self.database.sessions.begin() as session:
            state = await session.scalar(select(AppState).where(AppState.id == 1).with_for_update())
            activated = value and not state.kill_switch
            state.kill_switch = value
            if activated:
                session.add(
                    OutboxEvent(
                        correlation_id=uuid4(),
                        config_version=self.config_version,
                        topic="alert.telegram",
                        payload={"kind": "kill_switch", "subject": "paper"},
                    )
                )
            session.add(
                AuditEvent(
                    correlation_id=uuid4(),
                    actor="authenticated_api",
                    action="paper.kill_switch",
                    payload={"kill_switch": value},
                    config_version=self.config_version,
                )
            )
        if value:
            # Release state lock first: all broker paths lock portfolio then shared state.
            await self.cancel_halted()
        return {"kill_switch": value, "mode": "paper"}

    async def cancel_halted(self):
        async with self.database.sessions() as session:
            ids = (await session.scalars(select(PaperPortfolio.id))).all()
        for pid in ids:
            async with self.database.sessions.begin() as session:
                await self.lock(session, pid)
                state = await self.state(session)
                if not state or not state.kill_switch:
                    continue
                orders = (
                    await session.scalars(
                        select(PaperOrder).where(
                            PaperOrder.portfolio_id == pid, PaperOrder.status.in_(PENDING)
                        )
                    )
                ).all()
                for order in orders:
                    order.status, order.reserve, order.updated_at = "KILL_CANCELED", ZERO, utc_now()

    async def acknowledge(self, symbol, expected_version):
        async with self.database.sessions.begin() as session:
            row = await session.scalar(
                select(InstrumentRules).where(InstrumentRules.symbol == symbol).with_for_update()
            )
            if row is None or row.version != expected_version:
                raise PaperError("RULES_VERSION_CONFLICT")
            normalized = normalize_rules(row.payload["raw"], symbol)
            if (
                normalized["version"] != expected_version
                or normalized["state"] != 0
                or not public_fee_terms_supported(normalized)
                or risk_tiers(normalized) is None
            ):
                raise PaperError("RULES_INVALID")
            row.validated = True
            session.add(
                AuditEvent(
                    correlation_id=uuid4(),
                    actor="authenticated_api",
                    action="paper.rules_acknowledged",
                    payload={"symbol": symbol, "version": expected_version},
                    config_version=self.config_version,
                )
            )
            return {"symbol": symbol, "version": expected_version, "validated": True}

    async def unresolved_funding(self, session, portfolio):
        unresolved, _, _ = await self.funding_status(session, portfolio)
        return unresolved

    async def funding_status(self, session, portfolio):
        positions = (
            await session.scalars(
                select(PaperPosition).where(
                    PaperPosition.portfolio_id == portfolio.id,
                    PaperPosition.epoch == portfolio.epoch,
                )
            )
        ).all()
        for pos in positions:
            fills = (
                await session.scalars(select(PaperFill).where(PaperFill.position_id == pos.id))
            ).all()
            if not fills:
                continue
            earliest = min(fill.occurred_at for fill in fills)
            coverage = await session.get(FundingCoverage, pos.symbol)
            if (
                coverage is None
                or coverage.status != "verified"
                or coverage.covered_from is None
                or coverage.covered_from > earliest
                or coverage.covered_until is None
                or coverage.covered_until <= utc_now()
            ):
                gap = coverage.gap_time if coverage and coverage.gap_time else earliest
                return True, gap, "COVERAGE_UNPROVEN"
            settlements = (
                await session.scalars(
                    select(FundingSettlement).where(
                        FundingSettlement.symbol == pos.symbol,
                        FundingSettlement.settle_time >= coverage.covered_from,
                        FundingSettlement.settle_time < coverage.covered_until,
                    )
                )
            ).all()
            digest = payload_hash(
                {row.settle_time.isoformat(): row.raw for row in sorted(
                    settlements, key=lambda item: item.settle_time
                )}
            )
            if digest != coverage.snapshot_hash:
                return True, coverage.covered_from, "JOURNAL_MISMATCH"
            for settlement in settlements:
                when = settlement.settle_time
                if when <= earliest:
                    continue
                held = sum((f.exposure_delta for f in fills if f.occurred_at < when), ZERO)
                key = f"funding:{portfolio.epoch}:{pos.id}:{when.isoformat()}"
                if held > 0 and not await session.scalar(
                    select(PaperLedger.id).where(
                        PaperLedger.portfolio_id == portfolio.id, PaperLedger.event_key == key
                    )
                ):
                    return True, when, "SETTLEMENT_UNPOSTED"
        return False, None, None

    async def funding(self, session, portfolio):
        """Confirmed settlement rate + held-at-time quantity; historical close is an estimate."""
        positions = (
            await session.scalars(
                select(PaperPosition).where(
                    PaperPosition.portfolio_id == portfolio.id,
                    PaperPosition.epoch == portfolio.epoch,
                )
            )
        ).all()
        for pos in positions:
            fills = (
                await session.scalars(
                    select(PaperFill)
                    .where(PaperFill.position_id == pos.id)
                    .order_by(PaperFill.occurred_at)
                )
            ).all()
            if not fills:
                continue
            settlements = (
                await session.scalars(
                    select(FundingSettlement)
                    .where(FundingSettlement.symbol == pos.symbol)
                    .order_by(FundingSettlement.settle_time)
                )
            ).all()
            for settlement in settlements:
                when = settlement.settle_time
                if when > utc_now() or when < pos.opened_at:
                    continue
                key = f"funding:{portfolio.epoch}:{pos.id}:{when.isoformat()}"
                existing = await session.scalar(
                    select(PaperLedger.id).where(
                        PaperLedger.portfolio_id == portfolio.id, PaperLedger.event_key == key
                    )
                )
                if existing:
                    continue
                held = sum((f.exposure_delta for f in fills if f.occurred_at < when), ZERO)
                if held <= 0:
                    continue
                # Funding rate history has no settlement mark. Use only completed prior minute.
                candle = await session.get(Candle, (pos.symbol, "1m", when - timedelta(minutes=1)))
                if candle is None or not candle.final:
                    continue  # Missing historical mark leaves funding unresolved.
                price = D(candle.payload["close"])
                rate = D(settlement.rate)
                sign = D(-1) if pos.direction == "LONG" else D(1)
                amount = cash(held * pos.multiplier * price * rate * sign)
                provenance = {
                    "rate_source": "confirmed_public_funding_history",
                    "rate_raw_hash": settlement.raw_hash,
                    "rate_config_version": settlement.config_version,
                    "settlement_time": when.isoformat(),
                    "rate": str(rate),
                    "held_contracts": str(held),
                    "price": str(price),
                    "price_source": "estimated_prior_final_1m_close",
                    "candle_id": str(candle.event_id),
                }
                self.post(session, portfolio, pos.id, key, "FUNDING", amount, provenance)
                pos.funding += amount

    async def tick(self):
        """Recover projections from DB each tick; no in-memory order/position authority."""
        async with self.database.sessions() as session:
            ids = (await session.scalars(select(PaperPortfolio.id))).all()
        for pid in ids:
            protective = []
            async with self.database.sessions.begin() as session:
                portfolio = await self.lock(session, pid)
                state = await self.state(session)
                positions = (
                    await session.scalars(
                        select(PaperPosition).where(
                            PaperPosition.portfolio_id == pid,
                            PaperPosition.epoch == portfolio.epoch,
                            PaperPosition.closed_at.is_(None),
                        )
                    )
                ).all()
                for pos in positions:
                    rules, ticker, depth = await self.context(session, pos.symbol)
                    quality = await market_quality(session, pos.symbol)
                    if (
                        not ticker
                        or not snapshot_fresh(ticker)
                        or not quality["trade_eligible"]
                        or not rules
                        or not rules.validated
                        or utc_now() - rules.received_at > timedelta(hours=1)
                        or not public_fee_terms_supported(rules.payload)
                        or rules.payload.get("state") != 0
                    ):
                        continue
                    mark = D(ticker.payload["fairPrice"])
                    unresolved, _, _ = await self.funding_status(session, portfolio)
                    model = None
                    if not unresolved:
                        try:
                            model = estimate(pos, rules.payload, mark)
                        except (KeyError, ValueError, ArithmeticError):
                            pass
                    reason = None
                    if model and model["triggered"]:
                        reason = "liquidation_estimate"
                    elif (
                        mark <= pos.stop_loss if pos.direction == "LONG" else mark >= pos.stop_loss
                    ):
                        reason = "stop_loss"
                    elif pos.take_profit and (
                        mark >= pos.take_profit
                        if pos.direction == "LONG"
                        else mark <= pos.take_profit
                    ):
                        reason = "take_profit"
                    if reason:
                        protective.append(
                            (pos.id, f"protect:{reason}:{pos.id}:{ticker.event_id}", reason)
                        )
                # Financial obligations precede new pending entries; protective closes run below.
                if not protective:
                    await self.funding(session, portfolio)
                orders = (
                    await session.scalars(
                        select(PaperOrder).where(
                            PaperOrder.portfolio_id == pid,
                            PaperOrder.epoch == portfolio.epoch,
                            PaperOrder.status.in_(PENDING),
                        )
                    )
                ).all()
                for order in orders:
                    if protective or not state or state.kill_switch:
                        order.status, order.reserve = "RISK_CANCELED", ZERO
                        continue
                    payload = {
                        **order.payload,
                        "quantity": str(order.quantity - order.filled_quantity),
                    }
                    reason, snapshot = await self.check(session, portfolio, payload, pending=order)
                    intent = await session.get(PaperIntent, order.intent_id)
                    if snapshot.get("rules_version") != order.payload["rules_version"]:
                        reason = "RULES_CHANGED"
                    if not reason and order.payload.get("last_depth_id") == snapshot.get(
                        "depth_id"
                    ):
                        continue
                    verdict = await self.record_verdict(session, intent, reason, snapshot)
                    order.verdict_id = verdict.id
                    if reason:
                        order.status, order.reserve, order.updated_at = (
                            "RISK_CANCELED",
                            ZERO,
                            utc_now(),
                        )
                    else:
                        await self.execute(session, portfolio, order)
                summary = await self.summary(session, portfolio)
                if summary["equity"] is not None:
                    epoch = await session.get(PaperEpoch, (pid, portfolio.epoch))
                    equity = D(summary["equity"])
                    epoch.peak_equity = max(epoch.peak_equity, equity)
                    today = utc_now().date().isoformat()
                    if epoch.risk_day != today:
                        epoch.risk_day, epoch.day_start_equity = today, equity
            for position_id, key, reason in protective:
                await self.submit(
                    pid, CloseRequest(idempotency_key=key, reason=reason), position_id
                )
            if protective:
                async with self.database.sessions.begin() as session:
                    portfolio = await self.lock(session, pid)
                    await self.funding(session, portfolio)


def register_paper_routes(app):
    from fastapi import Request
    from fastapi.responses import JSONResponse

    def service():
        return PaperService(app.state.database, app.state.settings.config_version)

    @app.exception_handler(PaperError)
    async def paper_error(request: Request, exc):
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

    @app.get("/api/v1/portfolios")
    async def portfolios():
        async with app.state.database.sessions() as session:
            rows = (
                await session.scalars(select(PaperPortfolio).order_by(PaperPortfolio.created_at))
            ).all()
            return [await service().summary(session, row) for row in rows]

    @app.post("/api/v1/portfolios", status_code=201)
    async def create_portfolio(body: PortfolioCreate):
        return await service().create(body)

    @app.get("/api/v1/portfolios/{portfolio_id}")
    async def get_portfolio(portfolio_id: UUID):
        async with app.state.database.sessions() as session:
            row = await session.get(PaperPortfolio, portfolio_id)
            if row is None:
                raise PaperError("PORTFOLIO_NOT_FOUND", 404)
            return await service().summary(session, row)

    @app.post("/api/v1/portfolios/{portfolio_id}/reset")
    async def reset_portfolio(portfolio_id: UUID, body: ResetRequest):
        return await service().reset(portfolio_id, body)

    @app.post("/api/v1/portfolios/{portfolio_id}/orders")
    async def submit_order(portfolio_id: UUID, body: OrderRequest):
        return await service().submit(portfolio_id, body)

    @app.post("/api/v1/portfolios/{portfolio_id}/positions/{position_id}/close")
    async def close_position(portfolio_id: UUID, position_id: UUID, body: CloseRequest):
        return await service().submit(portfolio_id, body, position_id)

    @app.post("/api/v1/portfolios/{portfolio_id}/orders/{order_id}/cancel")
    async def cancel_order(portfolio_id: UUID, order_id: UUID):
        return await service().cancel(portfolio_id, order_id)

    @app.get("/api/v1/portfolios/{portfolio_id}/orders")
    @app.get("/api/v1/portfolios/{portfolio_id}/positions")
    @app.get("/api/v1/portfolios/{portfolio_id}/decisions")
    @app.get("/api/v1/portfolios/{portfolio_id}/ledger")
    async def history(portfolio_id: UUID, request: Request, epoch: int | None = None):
        kind = request.url.path.rsplit("/", 1)[-1]
        model = {
            "orders": PaperOrder,
            "positions": PaperPosition,
            "decisions": PaperIntent,
            "ledger": PaperLedger,
        }[kind]
        async with app.state.database.sessions() as session:
            portfolio = await session.get(PaperPortfolio, portfolio_id)
            if portfolio is None:
                raise PaperError("PORTFOLIO_NOT_FOUND", 404)
            query = select(model).where(model.portfolio_id == portfolio_id)
            if epoch is not None:
                query = query.where(model.epoch == epoch)
            rows = (await session.scalars(query)).all()
            if kind == "positions":
                funding_resolved = not await service().unresolved_funding(session, portfolio)
                return [
                    await service().position_json(session, row, funding_resolved)
                    for row in rows
                ]
            return [json_row(row) for row in rows]

    @app.get("/api/v1/portfolios/{portfolio_id}/risk")
    async def risk_history(portfolio_id: UUID):
        async with app.state.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PaperVerdict)
                    .join(PaperIntent)
                    .where(PaperIntent.portfolio_id == portfolio_id)
                    .order_by(PaperVerdict.occurred_at)
                )
            ).all()
            return [json_row(r) for r in rows]

    @app.get("/api/v1/portfolios/{portfolio_id}/fills")
    async def fill_history(portfolio_id: UUID):
        async with app.state.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PaperFill)
                    .join(PaperOrder)
                    .where(PaperOrder.portfolio_id == portfolio_id)
                    .order_by(PaperFill.occurred_at)
                )
            ).all()
            return [json_row(r) for r in rows]

    @app.get("/api/v1/risk")
    async def risk_state():
        async with app.state.database.sessions() as session:
            state = await session.get(AppState, 1)
            return {"kill_switch": state.kill_switch, "mode": "paper", "defaults": DEFAULT_PROFILE}

    @app.post("/api/v1/risk/kill-switch")
    async def kill_switch(body: KillRequest):
        return await service().kill(body.kill_switch)

    @app.post("/api/v1/markets/{symbol}/rules/acknowledge")
    async def acknowledge_rules(symbol: str, body: RulesAck):
        return await service().acknowledge(symbol, body.expected_version)
