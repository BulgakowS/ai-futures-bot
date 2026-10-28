"""Versioned, non-execution GKG scenario cost with as-of public rules proof."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ai_futures.liquidation import risk_tiers
from ai_futures.market import INSTRUMENT_RULES_SOURCE_VERSION
from ai_futures.mexc import normalize_rules, payload_hash, public_fee_terms_supported
from ai_futures.models import InstrumentRulesObservation
from ai_futures.schemas import require_utc

SCENARIO_COST_VERSION = "gkg-scenario-cost-v1"
ASSUMPTION_SOURCE = "predeclared_conservative_assumption"
RULES_MAX_AGE = timedelta(hours=1)


@dataclass(frozen=True)
class ScenarioCost:
    """Bps per fixed notional; funding and slippage are declared bounds, not fills."""

    declared_at: datetime
    funding_bps: str
    slippage_bps: str
    funding_source: str = ASSUMPTION_SOURCE
    slippage_source: str = ASSUMPTION_SOURCE
    version: str = SCENARIO_COST_VERSION

    def __post_init__(self):
        require_utc(self.declared_at)
        if self.version != SCENARIO_COST_VERSION:
            raise ValueError("Unsupported scenario cost version")
        if self.funding_source != ASSUMPTION_SOURCE or self.slippage_source != ASSUMPTION_SOURCE:
            raise ValueError("Only explicit conservative assumptions are supported")
        for value in (self.funding_bps, self.slippage_bps):
            if not isinstance(value, str):
                raise ValueError("Scenario amounts must be Decimal strings")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("Invalid scenario amount") from exc
            if not number.is_finite() or not 0 <= number <= 10000:
                raise ValueError("Invalid scenario amount")

    @property
    def checksum(self) -> str:
        return payload_hash(
            {
                "version": self.version,
                "declared_at": self.declared_at.isoformat(),
                "funding_bps": self.funding_bps,
                "slippage_bps": self.slippage_bps,
                "funding_source": self.funding_source,
                "slippage_source": self.slippage_source,
            }
        )


@dataclass(frozen=True)
class ScenarioCostQuote:
    total_bps: Decimal
    round_trip_public_taker_fee_bps: Decimal
    funding_bps: Decimal
    slippage_bps: Decimal
    rules_observation_id: str
    rules_payload_hash: str
    rules_version: str
    scenario_checksum: str
    source: str = "public_schedule_plus_assumptions_not_actual_fills"


async def scenario_cost_asof(session, *, at: datetime, rules_version: str, scenario: ScenarioCost):
    """Fail closed if a public fee/rules observation was unavailable at decision time."""
    require_utc(at)
    if scenario.declared_at > at:
        raise ValueError("Scenario was not predeclared at decision time")
    row = await session.scalar(
        select(InstrumentRulesObservation)
        .where(
            InstrumentRulesObservation.symbol == "BTC_USDT",
            InstrumentRulesObservation.observed_at <= at,
            InstrumentRulesObservation.effective_at <= at,
        )
        .order_by(
            InstrumentRulesObservation.observed_at.desc(), InstrumentRulesObservation.version.desc()
        )
        .limit(1)
    )
    if row is None:
        raise ValueError("No as-of public rules observation")
    if at - row.observed_at > RULES_MAX_AGE:
        raise ValueError("Stale as-of public rules observation")
    payload = row.payload
    if (
        row.effective_at != row.observed_at
        or row.source_version != INSTRUMENT_RULES_SOURCE_VERSION
        or row.payload_hash != payload_hash(payload)
        or row.rules_version != rules_version
        or payload.get("version") != rules_version
        or normalize_rules(payload.get("raw"), "BTC_USDT") != payload
        or payload.get("state") != 0
        or risk_tiers(payload) is None
        or not public_fee_terms_supported(payload)
    ):
        raise ValueError("Unsupported or unverified public rules/fee provenance")
    fee = Decimal(payload["takerFeeRate"])
    if not fee.is_finite() or fee < 0:
        raise ValueError("Invalid public taker fee")
    round_trip_fee = 2 * fee * Decimal(10000)
    funding = Decimal(scenario.funding_bps)
    slippage = Decimal(scenario.slippage_bps)
    return ScenarioCostQuote(
        total_bps=round_trip_fee + funding + slippage,
        round_trip_public_taker_fee_bps=round_trip_fee,
        funding_bps=funding,
        slippage_bps=slippage,
        rules_observation_id=str(row.observation_id),
        rules_payload_hash=row.payload_hash,
        rules_version=row.rules_version,
        scenario_checksum=scenario.checksum,
    )
