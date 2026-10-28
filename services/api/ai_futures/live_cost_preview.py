"""Inert, conservative round-trip cost preview for one future Post Only entry."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from pydantic import Field, StrictBool, ValidationError

from ai_futures.live_pilot_settings import LivePilotDraft
from ai_futures.schemas import ExactDecimal, Schema, UTCDateTime


class CostCandidate(Schema):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    notional_usdt: ExactDecimal = Field(gt=0)
    order_type: str = Field(min_length=1)


class AccountFeeEvidence(Schema):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    observed_at: UTCDateTime
    source_id: str = Field(min_length=1)
    authenticated: StrictBool
    maker_rate: ExactDecimal
    taker_rate: ExactDecimal


class MarketCostEvidence(Schema):
    symbol: str = Field(pattern=r"^[A-Z0-9]+_USDT$")
    observed_at: UTCDateTime
    source_id: str = Field(min_length=1)
    validated: StrictBool
    bid: ExactDecimal = Field(gt=0)
    ask: ExactDecimal = Field(gt=0)
    executable_notional_usdt: ExactDecimal = Field(gt=0)


class CostReserves(Schema):
    exit_slippage_pct: ExactDecimal = Field(ge=0)
    funding_reserve_pct: ExactDecimal = Field(ge=0)


@dataclass(frozen=True)
class CostPreview:
    preview_passed: bool
    live_allowed: bool
    reasons: tuple[str, ...]
    config_version: str | None
    estimated_round_trip_cost_pct: Decimal | None = None
    max_round_trip_cost_pct: Decimal | None = None


def _fresh(observed_at: datetime, source_id: str, now: datetime, max_age: timedelta) -> bool:
    age = now - observed_at
    return bool(source_id.strip()) and timedelta(0) <= age <= max_age


def evaluate_live_cost_preview(
    draft: LivePilotDraft | dict | None,
    *,
    config_version: str | None,
    candidate: CostCandidate | dict | None,
    account_fee: AccountFeeEvidence | dict | None,
    market: MarketCostEvidence | dict | None,
    reserves: CostReserves | dict | None,
    now: datetime,
    max_age: timedelta,
) -> CostPreview:
    """Estimate percent of full entry notional, with no execution authority.

    Fee rates are fractions (0.0001 = 0.01%). All budget, spread and reserve
    values are percent units (0.5 = 0.5%). Full bid-ask spread is charged once;
    maker entry and possible taker protective exit fees are clamped at zero so
    rebates never subsidize the risk budget. Caller validates fee provenance,
    depth units/coverage and reserve policy; this module cannot attest to them.
    """
    if draft is None:
        return CostPreview(False, False, ("COST_CONFIG_MISSING",), config_version)
    try:
        config = LivePilotDraft.model_validate(draft)
    except (ValidationError, TypeError, ValueError):
        return CostPreview(False, False, ("COST_CONFIG_INVALID",), config_version)
    if not config_version or not config_version.strip():
        return CostPreview(False, False, ("CONFIG_VERSION_MISSING",), config_version)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        return CostPreview(False, False, ("CLOCK_INVALID",), config_version)
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        return CostPreview(False, False, ("MAX_AGE_INVALID",), config_version)

    reasons: list[str] = []
    parsed = []
    for value, model, missing, unknown in (
        (candidate, CostCandidate, "CANDIDATE_MISSING", "CANDIDATE_UNKNOWN"),
        (account_fee, AccountFeeEvidence, "ACCOUNT_FEE_MISSING", "ACCOUNT_FEE_UNKNOWN"),
        (market, MarketCostEvidence, "MARKET_COST_MISSING", "MARKET_COST_UNKNOWN"),
        (reserves, CostReserves, "COST_RESERVES_MISSING", "COST_RESERVES_UNKNOWN"),
    ):
        if value is None:
            reasons.append(missing)
            parsed.append(None)
            continue
        try:
            parsed.append(model.model_validate(value))
        except (ValidationError, TypeError, ValueError):
            reasons.append(unknown)
            parsed.append(None)
    if reasons:
        return CostPreview(False, False, tuple(reasons), config_version)
    intent, fee, quote, reserve = parsed
    if intent.order_type != "POST_ONLY":
        reasons.append("POST_ONLY_REQUIRED")
    if fee.symbol != intent.symbol or quote.symbol != intent.symbol:
        reasons.append("EVIDENCE_SYMBOL_MISMATCH")
    if not fee.authenticated:
        reasons.append("ACCOUNT_FEE_UNKNOWN")
    if not quote.validated or quote.bid >= quote.ask:
        reasons.append("MARKET_COST_UNKNOWN")
    if not _fresh(fee.observed_at, fee.source_id, now, max_age):
        reasons.append("ACCOUNT_FEE_STALE")
    if not _fresh(quote.observed_at, quote.source_id, now, max_age):
        reasons.append("MARKET_COST_STALE")
    if quote.executable_notional_usdt < intent.notional_usdt:
        reasons.append("LIQUIDITY_INSUFFICIENT")
    if reasons:
        return CostPreview(False, False, tuple(dict.fromkeys(reasons)), config_version)

    values = (
        fee.maker_rate,
        fee.taker_rate,
        quote.bid,
        quote.ask,
        reserve.exit_slippage_pct,
        reserve.funding_reserve_pct,
        config.max_round_trip_cost_pct,
    )
    with localcontext() as context:
        context.prec = max(80, sum(len(value.as_tuple().digits) for value in values) + 20)
        spread_pct = (quote.ask - quote.bid) * Decimal(200) / (quote.ask + quote.bid)
        estimate = (
            (max(fee.maker_rate, Decimal(0)) + max(fee.taker_rate, Decimal(0))) * Decimal(100)
            + spread_pct
            + reserve.exit_slippage_pct
            + reserve.funding_reserve_pct
        )
        budget = config.max_round_trip_cost_pct
        if estimate > budget:
            reasons.append("ROUND_TRIP_COST_OVER_BUDGET")
    return CostPreview(
        not reasons,
        False,
        tuple(reasons) if reasons else ("COST_PREVIEW_PASS_LIVE_DISABLED",),
        config_version,
        estimate,
        budget,
    )
