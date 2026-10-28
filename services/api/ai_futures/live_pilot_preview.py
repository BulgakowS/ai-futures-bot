"""Pure, fail-closed exposure preview for an inert future Live pilot draft."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, StrictBool, ValidationError

from ai_futures.live_pilot_settings import LivePilotDraft
from ai_futures.schemas import ExactDecimal, Schema, UTCDateTime


class Evidence(Schema):
    observed_at: UTCDateTime
    source_id: str = Field(min_length=1)


class ObservedDecimal(Evidence):
    value: ExactDecimal


class InstrumentRule(Evidence):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    contract_size: ExactDecimal = Field(gt=0)
    reference_price: ExactDecimal = Field(gt=0)
    max_leverage: ExactDecimal = Field(ge=1)
    validated: StrictBool


class Exposure(Schema):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    remaining_contracts: ExactDecimal = Field(gt=0)
    recorded_price: ExactDecimal = Field(gt=0)
    leverage: ExactDecimal = Field(ge=1)


class ExposureBook(Evidence):
    entries: list[Exposure]
    complete: StrictBool
    states_known: StrictBool


class PilotSnapshot(Schema):
    snapshot_id: str = Field(min_length=1)
    equity: ObservedDecimal
    daily_loss: ObservedDecimal
    positions: ExposureBook
    pending_entries: ExposureBook
    rules: list[InstrumentRule]


class Candidate(Evidence):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    side: Literal["LONG", "SHORT"]
    contracts: ExactDecimal = Field(gt=0)
    entry_price: ExactDecimal = Field(gt=0)
    leverage: ExactDecimal = Field(ge=1)
    stop_price: ExactDecimal = Field(gt=0)
    take_price: ExactDecimal = Field(gt=0)


@dataclass(frozen=True)
class PilotPreview:
    preview_passed: bool
    live_allowed: bool
    reasons: tuple[str, ...]
    snapshot_id: str | None
    config_version: str | None
    aggregate_notional_usdt: Decimal | None = None
    pair_notional_usdt: Decimal | None = None
    distinct_pairs: int | None = None


def _fresh(evidence: Evidence, now: datetime, max_age: timedelta) -> bool:
    age = now - evidence.observed_at
    return timedelta(0) <= age <= max_age and bool(evidence.source_id.strip())


def _cap(limit, equity: Decimal) -> Decimal:
    if limit.unit == "USDT":
        return limit.value
    return limit.value * equity / Decimal(100)


def evaluate_live_pilot_preview(
    draft: LivePilotDraft | dict | None,
    *,
    config_version: str | None,
    snapshot: PilotSnapshot | dict | None,
    candidate: Candidate | dict | None,
    now: datetime,
    max_age: timedelta,
) -> PilotPreview:
    """Evaluate one new entry; never authorize an order or mutate any state.

    The caller must supply reconciled, complete account/order state and validated
    current contract rules/prices. Pending entries contain only unfilled remaining
    contracts; filled contracts belong in positions. Unknown remainder/state denies.
    """
    reasons: list[str] = []
    try:
        if draft is None:
            return PilotPreview(False, False, ("PILOT_CONFIG_MISSING",), None, config_version)
        config = LivePilotDraft.model_validate(
            draft.model_dump() if isinstance(draft, LivePilotDraft) else draft
        )
        if not config_version or not config_version.strip():
            reasons.append("CONFIG_VERSION_MISSING")
    except (ValidationError, TypeError, ValueError):
        return PilotPreview(False, False, ("PILOT_CONFIG_INVALID",), None, config_version)

    try:
        state = PilotSnapshot.model_validate(
            snapshot.model_dump() if isinstance(snapshot, PilotSnapshot) else snapshot
        )
        intent = Candidate.model_validate(
            candidate.model_dump() if isinstance(candidate, Candidate) else candidate
        )
    except (ValidationError, TypeError, ValueError):
        return PilotPreview(False, False, ("SNAPSHOT_OR_INTENT_INVALID",), None, config_version)

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        reasons.append("CLOCK_INVALID")
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        reasons.append("MAX_AGE_INVALID")
    if reasons:
        return PilotPreview(False, False, tuple(reasons), state.snapshot_id, config_version)

    sources = (state.equity, state.daily_loss, state.positions, state.pending_entries, intent)
    if any(not _fresh(item, now, max_age) for item in sources):
        reasons.append("SOURCE_STALE_OR_UNKNOWN")
    if not state.positions.complete or not state.pending_entries.complete:
        reasons.append("EXPOSURE_INCOMPLETE")
    if not state.positions.states_known or not state.pending_entries.states_known:
        reasons.append("ORDER_STATE_UNKNOWN")
    if state.equity.value <= 0 or state.daily_loss.value < 0:
        reasons.append("ACCOUNT_VALUE_INVALID")
    if not state.snapshot_id.strip():
        reasons.append("SNAPSHOT_ID_MISSING")

    symbols = {intent.symbol}
    symbols.update(item.symbol for item in state.positions.entries)
    symbols.update(item.symbol for item in state.pending_entries.entries)
    rules: dict[str, InstrumentRule] = {}
    for rule in state.rules:
        if rule.symbol in rules:
            reasons.append("RULE_DUPLICATE")
        rules[rule.symbol] = rule
    if any(symbol not in rules for symbol in symbols):
        reasons.append("RULE_MISSING")
    if any(not rule.validated or not _fresh(rule, now, max_age) for rule in rules.values()):
        reasons.append("RULE_STALE_OR_UNVALIDATED")
    if reasons:
        return PilotPreview(
            False, False, tuple(dict.fromkeys(reasons)), state.snapshot_id, config_version
        )

    # Decimal context is sized from every operand, so sums and products cannot
    # silently round a limit crossing at the default 28-digit precision.
    numbers = [
        state.equity.value,
        state.daily_loss.value,
        intent.contracts,
        intent.entry_price,
        intent.leverage,
        intent.stop_price,
        intent.take_price,
        config.per_pair_notional.value,
        config.aggregate_notional.value,
        config.max_daily_loss.value,
        config.max_leverage,
        config.stop_loss_pct,
        config.take_profit_pct,
    ]
    for rule in rules.values():
        numbers.extend((rule.contract_size, rule.reference_price, rule.max_leverage))
    for item in (*state.positions.entries, *state.pending_entries.entries):
        numbers.extend((item.remaining_contracts, item.recorded_price, item.leverage))
    with localcontext() as context:
        context.prec = max(80, sum(len(number.as_tuple().digits) for number in numbers) + 20)
        pair_totals: dict[str, Decimal] = {}
        for item in (*state.positions.entries, *state.pending_entries.entries):
            rule = rules[item.symbol]
            notional = (
                item.remaining_contracts
                * rule.contract_size
                * max(rule.reference_price, item.recorded_price)
            )
            pair_totals[item.symbol] = pair_totals.get(item.symbol, Decimal(0)) + notional
        candidate_rule = rules[intent.symbol]
        candidate_notional = (
            intent.contracts
            * candidate_rule.contract_size
            * max(candidate_rule.reference_price, intent.entry_price)
        )
        pair_totals[intent.symbol] = pair_totals.get(intent.symbol, Decimal(0)) + candidate_notional
        aggregate = sum(pair_totals.values(), Decimal(0))
        pair_notional = pair_totals[intent.symbol]

        if len(pair_totals) > config.max_pairs:
            reasons.append("MAX_PAIRS_BREACH")
        if any(
            value > _cap(config.per_pair_notional, state.equity.value)
            for value in pair_totals.values()
        ):
            reasons.append("PER_PAIR_NOTIONAL_BREACH")
        if aggregate > _cap(config.aggregate_notional, state.equity.value):
            reasons.append("AGGREGATE_NOTIONAL_BREACH")
        if intent.leverage > config.max_leverage or intent.leverage > candidate_rule.max_leverage:
            reasons.append("LEVERAGE_BREACH")
        if any(
            item.leverage > config.max_leverage or item.leverage > rules[item.symbol].max_leverage
            for item in (*state.positions.entries, *state.pending_entries.entries)
        ):
            reasons.append("EXISTING_LEVERAGE_BREACH")
        if state.daily_loss.value >= _cap(config.max_daily_loss, state.equity.value):
            reasons.append("DAILY_LOSS_BREACH")

        if intent.side == "LONG":
            stop_distance = intent.entry_price - intent.stop_price
            take_distance = intent.take_price - intent.entry_price
        else:
            stop_distance = intent.stop_price - intent.entry_price
            take_distance = intent.entry_price - intent.take_price
        if stop_distance <= 0 or take_distance <= 0:
            reasons.append("PROTECTION_DIRECTION_INVALID")
        elif stop_distance * 100 > intent.entry_price * config.stop_loss_pct:
            reasons.append("STOP_LOSS_BREACH")
        elif take_distance * 100 < intent.entry_price * config.take_profit_pct:
            reasons.append("TAKE_PROFIT_BREACH")

    return PilotPreview(
        not reasons,
        False,
        tuple(reasons) if reasons else ("PREVIEW_PASS_LIVE_DISABLED",),
        state.snapshot_id,
        config_version,
        aggregate,
        pair_notional,
        len(pair_totals),
    )
