"""Deterministic final-candle replay. No paper broker, network or wall clock calls."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from ai_futures.paper import RiskProfile, cash
from ai_futures.schemas import require_utc
from ai_futures.strategies import STEP, StrategyConfig, evaluate

D = Decimal
ZERO = D(0)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def event_id(key):
    return str(uuid5(NAMESPACE_URL, key))


def timestamp(value):
    return require_utc(datetime.fromisoformat(value)).isoformat()


def rounded(value, step, up=False):
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def _decimal(value, positive=False):
    if isinstance(value, float | bool):
        raise ValueError("Binary float is forbidden")
    result = D(value)
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("Invalid decimal")
    return result


def make_dataset(candles, rules, *, symbol, timeframe, rules_version, funding=()):
    """Freeze source rows; repairing any input produces a different checksum."""
    if timeframe not in STEP or symbol != "BTC_USDT" or len(candles) < 2:
        raise ValueError("Unsupported or short dataset")
    needed = (
        "contractSize",
        "priceUnit",
        "volUnit",
        "minVol",
        "maxVol",
        "maxLeverage",
        "takerFeeRate",
    )
    frozen_rules = {name: str(_decimal(rules[name], name != "takerFeeRate")) for name in needed}
    if _decimal(frozen_rules["takerFeeRate"]) < 0 or _decimal(frozen_rules["takerFeeRate"]) >= D(
        "0.1"
    ):
        raise ValueError("Invalid fee")
    rows = []
    previous = None
    gaps = []
    for candle in candles:
        if (
            candle.symbol != symbol
            or candle.timeframe != timeframe
            or not candle.final
            or candle.source != "rest"
        ):
            raise ValueError("Only matching REST-final candles are accepted")
        start = timestamp(candle.window_start.isoformat())
        when = datetime.fromisoformat(start)
        if previous is not None:
            if when <= previous:
                raise ValueError("Duplicate or out-of-order candle")
            if when - previous != STEP[timeframe]:
                gaps.append({"after": previous.isoformat(), "before": start})
        previous = when
        payload = {
            name: str(_decimal(candle.payload[name], True))
            for name in ("open", "high", "low", "close")
        }
        payload["vol"] = str(_decimal(candle.payload["vol"]))
        if _decimal(payload["vol"]) < 0 or not (
            D(payload["low"])
            <= min(D(payload["open"]), D(payload["close"]))
            <= max(D(payload["open"]), D(payload["close"]))
            <= D(payload["high"])
        ):
            raise ValueError("Invalid OHLCV")
        rows.append(
            {
                "window_start": start,
                "source_time": timestamp(candle.source_time.isoformat()),
                "received_at": timestamp(candle.received_at.isoformat()),
                "source_event_id": str(candle.event_id),
                "payload_hash": candle.payload_hash,
                "source": "rest",
                "final": True,
                "payload": payload,
            }
        )
    settlements = []
    previous_funding = None
    event_times = {datetime.fromisoformat(row["window_start"]) + STEP[timeframe] for row in rows}
    for entry in funding:
        when = datetime.fromisoformat(timestamp(entry["settleTime"]))
        if previous_funding is not None and when <= previous_funding:
            raise ValueError("Duplicate or unordered funding")
        if when not in event_times:
            raise ValueError("Funding needs a matching final candle close")
        previous_funding = when
        settlements.append(
            {"settleTime": when.isoformat(), "fundingRate": str(_decimal(entry["fundingRate"]))}
        )
    content = {
        "schema_version": 1,
        "source": "MEXC public REST-final cache",
        "symbol": symbol,
        "timeframe": timeframe,
        "start_utc": rows[0]["window_start"],
        "end_utc": (previous + STEP[timeframe]).isoformat(),
        "price_type": "MEXC contract OHLCV trade price",
        "normalization_version": "market-v1/backtest-v1",
        "rules_version": rules_version,
        "rules": frozen_rules,
        "funding_coverage": "known_settlements_only" if settlements else "unavailable",
        "funding": settlements,
        "gaps": gaps,
        "candles": rows,
    }
    return {**content, "checksum": digest(content)}


@dataclass
class Position:
    direction: str
    quantity: Decimal
    entry: Decimal
    stop: Decimal
    opened_at: str
    entry_fee: Decimal
    funding: Decimal = ZERO


def risk_gate(
    action,
    direction,
    qty,
    stop,
    price,
    position,
    wallet,
    equity,
    deposit,
    peak,
    day_start,
    leverage,
    profile,
    rules,
):
    """Historical snapshot gate using the paper RiskProfile limits and instrument precision."""
    step, tick = D(rules["volUnit"]), D(rules["priceUnit"])
    if (
        qty <= 0
        or qty < D(rules["minVol"])
        or qty > D(rules["maxVol"])
        or qty != rounded(qty, step)
    ):
        return "CONTRACT_SIZE"
    if action == "CLOSE":
        return (
            None
            if position and qty <= position.quantity and direction == position.direction
            else "REDUCE_ONLY_VIOLATION"
        )
    if position:
        return "POSITION_CONFLICT"
    if equity <= 0:
        return "INSOLVENT"
    if equity <= day_start * (1 - profile.max_daily_loss):
        return "DAILY_LOSS_LIMIT"
    if equity <= peak * (1 - profile.max_drawdown):
        return "DRAWDOWN_LIMIT"
    if stop is None or stop != rounded(stop, tick):
        return "PRICE_PRECISION"
    if (direction == "LONG" and stop >= price) or (direction == "SHORT" and stop <= price):
        return "STOP_LOSS_INVALID"
    multiplier, fee = D(rules["contractSize"]), D(rules["takerFeeRate"])
    notional = qty * multiplier * price
    if leverage < 1 or leverage > min(profile.max_leverage, D(rules["maxLeverage"])):
        return "LEVERAGE_LIMIT"
    if notional / leverage + cash(notional * fee) > wallet:
        return "INSUFFICIENT_MARGIN"
    if notional > equity * profile.max_notional_equity:
        return "EXPOSURE_LIMIT"
    risk = qty * multiplier * (abs(price - stop) + (price + stop) * fee)
    if risk > equity * min(profile.risk_per_trade, profile.max_total_risk):
        return "TRADE_RISK_LIMIT"
    if deposit <= 0:
        return "LEVERAGE_LIMIT"
    return None


def replay(
    dataset,
    config: StrategyConfig,
    profile: RiskProfile,
    deposit: Decimal,
    *,
    seed=0,
    config_version="backtest-v1",
):
    """All event times are candle close; signal fills at the next REST-final close."""
    content = {key: value for key, value in dataset.items() if key != "checksum"}
    if digest(content) != dataset.get("checksum"):
        raise ValueError("Dataset checksum mismatch")
    if config.timeframe != dataset["timeframe"] or dataset["symbol"] != "BTC_USDT":
        raise ValueError("Strategy and dataset mismatch")
    deposit = _decimal(deposit, True)
    rules = dataset["rules"]
    _decimal(profile.slippage_bps)
    input_key = digest(
        {
            "dataset": dataset["checksum"],
            "config": config.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "deposit": str(deposit),
            "seed": seed,
            "config_version": config_version,
        }
    )
    wallet, peak, day_start = cash(deposit), cash(deposit), cash(deposit)
    day = None
    position = None
    pending = None
    decisions, intents, verdicts, orders, fills, ledger, curve, trades = ([] for _ in range(8))
    fees = funding_total = slippage_total = gross_realized = ZERO
    funding_by_time = {x["settleTime"]: D(x["fundingRate"]) for x in dataset["funding"]}
    candle_history = []
    previous = None
    multiplier, fee_rate, tick = (
        D(rules[k]) for k in ("contractSize", "takerFeeRate", "priceUnit")
    )

    def record_ledger(reason, amount, when, cause):
        ledger.append(
            {
                "id": event_id(f"{input_key}:ledger:{len(ledger)}"),
                "correlation_id": cause,
                "occurred_at": when,
                "config_version": config_version,
                "reason": reason,
                "wallet_delta": str(amount),
                "offset_delta": str(-amount),
            }
        )

    for row in dataset["candles"]:
        start = datetime.fromisoformat(row["window_start"])
        when = (start + STEP[config.timeframe]).isoformat()
        contiguous = previous is None or start - previous == STEP[config.timeframe]
        previous = start
        if day != start.date():
            day, day_start = start.date(), wallet
        close = D(row["payload"]["close"])
        candle_history.append(
            SimpleNamespace(window_start=start, final=True, source="rest", payload=row["payload"])
        )
        if not contiguous:
            pending = None
        # Settlement belongs to the position held before this event's possible fill.
        if position and when in funding_by_time:
            rate = funding_by_time[when]
            signed = -1 if position.direction == "LONG" else 1
            amount = cash(position.quantity * multiplier * close * rate * signed)
            funding_total += amount
            position.funding += amount
            wallet = cash(wallet + amount)
            record_ledger("FUNDING", amount, when, event_id(f"{input_key}:funding:{when}"))
        if pending is not None:
            action, direction, qty, stop, cause = pending
            pending = None
            # The price is known only when this final event arrives.
            base = close
            buy = (action == "OPEN" and direction == "LONG") or (
                action == "CLOSE" and direction == "SHORT"
            )
            raw = base * (1 + (D(1) if buy else D(-1)) * profile.slippage_bps / 10000)
            price = rounded(raw, tick, buy)
            unrealized = (
                ZERO
                if position is None
                else (close - position.entry)
                * position.quantity
                * multiplier
                * (1 if position.direction == "LONG" else -1)
            )
            equity = cash(wallet + unrealized)
            intent_id = event_id(f"{input_key}:intent:{when}:{action}")
            intent = {
                "id": intent_id,
                "decision_id": cause,
                "action": action,
                "direction": direction,
                "quantity": str(qty),
                "occurred_at": when,
                "config_version": config_version,
            }
            intents.append(intent)
            reason = risk_gate(
                action,
                direction,
                qty,
                stop,
                price,
                position,
                wallet,
                equity,
                deposit,
                peak,
                day_start,
                config.leverage,
                profile,
                rules,
            )
            verdict_id = event_id(f"{intent_id}:risk")
            verdicts.append(
                {
                    "id": verdict_id,
                    "intent_id": intent_id,
                    "outcome": "REJECT" if reason else "ALLOW",
                    "reason": reason or "RISK_OK",
                    "occurred_at": when,
                    "config_version": config_version,
                    "rules_version": dataset["rules_version"],
                }
            )
            if reason is None:
                order_id = event_id(f"{intent_id}:order")
                fee = cash(qty * multiplier * price * fee_rate)
                slip = cash(qty * multiplier * abs(price - base))
                fees, slippage_total = fees + fee, slippage_total + slip
                orders.append(
                    {
                        "id": order_id,
                        "intent_id": intent_id,
                        "verdict_id": verdict_id,
                        "action": action,
                        "status": "FILLED",
                        "quantity": str(qty),
                        "occurred_at": when,
                        "config_version": config_version,
                    }
                )
                fill_id = event_id(f"{order_id}:fill")
                realized = ZERO
                if action == "OPEN":
                    position = Position(direction, qty, price, stop, when, fee)
                else:
                    realized = cash(
                        (price - position.entry)
                        * qty
                        * multiplier
                        * (1 if direction == "LONG" else -1)
                    )
                    gross_realized += realized
                    trades.append(
                        {
                            "entry_at": position.opened_at,
                            "exit_at": when,
                            "gross_pnl": str(realized),
                            "net_pnl": str(realized - position.entry_fee - fee + position.funding),
                        }
                    )
                    position = None
                    wallet = cash(wallet + realized)
                    record_ledger("REALIZED_PNL", realized, when, fill_id)
                wallet = cash(wallet - fee)
                record_ledger("FEE", -fee, when, fill_id)
                fills.append(
                    {
                        "id": fill_id,
                        "order_id": order_id,
                        "quantity": str(qty),
                        "price": str(price),
                        "fee": str(fee),
                        "realized_pnl": str(realized),
                        "slippage_cost": str(slip),
                        "occurred_at": when,
                        "config_version": config_version,
                    }
                )
        signal = evaluate(config, candle_history, position)
        reason = signal.reason
        if not contiguous:
            reason = "CANDLE_GAP"
            signal = SimpleNamespace(action="WAIT", direction=None)
        elif position and (
            (position.direction == "LONG" and close <= position.stop)
            or (position.direction == "SHORT" and close >= position.stop)
        ):
            reason = "STOP_BREACHED_CLOSE"
            signal = SimpleNamespace(action="CLOSE", direction=position.direction)
        decision_id = event_id(f"{input_key}:decision:{when}")
        decisions.append(
            {
                "id": decision_id,
                "candle_event_id": row["source_event_id"],
                "action": signal.action,
                "reason": reason,
                "direction": signal.direction,
                "occurred_at": when,
                "config_version": config_version,
            }
        )
        if signal.action in {"OPEN", "CLOSE"}:
            qty = config.quantity if signal.action == "OPEN" else position.quantity
            stop = None
            if signal.action == "OPEN":
                factor = (
                    (1 - config.stop_distance)
                    if signal.direction == "LONG"
                    else (1 + config.stop_distance)
                )
                stop = rounded(close * factor, tick, signal.direction == "SHORT")
            pending = (signal.action, signal.direction, qty, stop, decision_id)
        unrealized = (
            ZERO
            if position is None
            else cash(
                (close - position.entry)
                * position.quantity
                * multiplier
                * (1 if position.direction == "LONG" else -1)
            )
        )
        equity = cash(wallet + unrealized)
        peak = max(peak, equity)
        curve.append(
            {
                "occurred_at": when,
                "equity": str(equity),
                "wallet": str(wallet),
                "unrealized_pnl": str(unrealized),
                "exposure": str(position.quantity * multiplier * close if position else ZERO),
            }
        )
    ending_equity = D(curve[-1]["equity"])
    running_peak, max_drawdown = deposit, ZERO
    for point in curve:
        equity = D(point["equity"])
        running_peak = max(running_peak, equity)
        max_drawdown = max(max_drawdown, D(1) - equity / running_peak)
    wins = [D(t["net_pnl"]) for t in trades if D(t["net_pnl"]) > 0]
    losses = [D(t["net_pnl"]) for t in trades if D(t["net_pnl"]) < 0]
    report = {
        "mode": "backtest",
        "dataset_checksum": dataset["checksum"],
        "input_key": input_key,
        "interval": {"start_utc": dataset["start_utc"], "end_utc": dataset["end_utc"]},
        "config_version": config_version,
        "seed": seed,
        "strategy": config.model_dump(mode="json"),
        "risk_profile": profile.model_dump(mode="json"),
        "assumptions": {
            "fill": "next REST-final candle close; full quantity; no book/partial fills",
            "stop": "breach on final close queues CLOSE for next final event",
            "slippage_bps": str(profile.slippage_bps),
            "fee_rate": str(fee_rate),
            "funding_coverage": dataset["funding_coverage"],
                "margin": "isolated configured leverage; maintenance/liquidation not modeled",
            "rules_version": dataset["rules_version"],
            "gaps": dataset["gaps"],
        },
        "account": {
            "mode": "backtest",
            "initial_balance": str(deposit),
            "wallet_balance": str(wallet),
            "ending_equity": str(ending_equity),
        },
        "positions": (
            [
                {
                    "direction": position.direction,
                    "quantity": str(position.quantity),
                    "entry": str(position.entry),
                    "stop": str(position.stop),
                    "opened_at": position.opened_at,
                }
            ]
            if position
            else []
        ),
        "decisions": decisions,
        "intents": intents,
        "verdicts": verdicts,
        "orders": orders,
        "fills": fills,
        "ledger": ledger,
        "trades": trades,
        "equity_curve": curve,
        "metrics": {
            "gross_pnl": str(gross_realized + D(curve[-1]["unrealized_pnl"])),
            "gross_realized_pnl": str(gross_realized),
            "unrealized_pnl": curve[-1]["unrealized_pnl"],
            "net_pnl": str(ending_equity - deposit),
            "fees": str(fees),
            "funding": str(funding_total),
            "slippage_cost": str(slippage_total),
            "max_drawdown": str(max_drawdown),
            "win_rate": str(D(len(wins)) / len(trades) if trades else ZERO),
            "profit_factor": str(sum(wins) / -sum(losses)) if losses else None,
            "closed_trades": len(trades),
            "exposure_event_fraction": str(
                D(sum(D(x["exposure"]) > 0 for x in curve)) / len(curve)
            ),
        },
    }
    return report


def compare(reports):
    if len(reports) < 2:
        raise ValueError("At least two runs required")
    first = reports[0]
    for report in reports[1:]:
        for key in ("dataset_checksum", "interval", "risk_profile", "seed", "config_version"):
            if report[key] != first[key]:
                raise ValueError(f"Comparison mismatch: {key}")
        for key in ("initial_balance",):
            if report["account"][key] != first["account"][key]:
                raise ValueError(f"Comparison mismatch: {key}")
        for key in (
            "fill", "stop", "slippage_bps", "fee_rate", "funding_coverage",
            "rules_version", "margin",
        ):
            if report["assumptions"][key] != first["assumptions"][key]:
                raise ValueError(f"Comparison mismatch: {key}")
    return {
        "dataset_checksum": first["dataset_checksum"],
        "interval": first["interval"],
        "runs": [
            {
                "input_key": r["input_key"],
                "strategy": r["strategy"]["strategy"],
                "metrics": r["metrics"],
            }
            for r in reports
        ],
    }
