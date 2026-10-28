"""Pure, default-deny assessment of a proposed MEXC API maker fee exception.

No network, credential, broker, or Live activation path lives here. A caller must
independently authenticate both the account binding and MEXC-issued evidence.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_futures.mexc_private_readonly import AccountFeeBaseline
from ai_futures.mexc_zero_fee_catalog import (
    KNOWN_FEE_RATE_TYPES,
    PublicZeroFeeCandidate,
    PublicZeroFeeCatalog,
)
from ai_futures.schemas import require_decimal

MAX_SNAPSHOT_AGE = timedelta(minutes=5)
SYMBOL = re.compile(r"[A-Z0-9]{2,20}_USDT\Z")
ACCOUNT_REF = re.compile(r"[A-Za-z0-9_-]{4,80}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
API_MAKER_SCOPE = "account_specific_futures_api_maker_zero_percent"
CONFIRMATION_CHANNELS = frozenset({"mexc_support_written", "mexc_account_notice"})


class MakerGateError(ValueError):
    """Malformed public instrument data; do not include response content."""


@dataclass(frozen=True)
class PublicApiContract:
    symbol: str
    observed_at: datetime
    rules_sha256: str
    api_allowed: bool
    active: bool


@dataclass(frozen=True)
class AccountFeeEvidence:
    account_ref: str
    fee: AccountFeeBaseline
    account_binding_sha256: str


@dataclass(frozen=True)
class MexcApiMakerConfirmation:
    issuer: str
    channel: str
    reference: str
    artifact_sha256: str
    account_ref: str
    symbol: str
    scope: str
    maker_rate: Decimal
    issued_at: datetime
    expires_at: datetime
    reviewed_at: datetime
    reviewer_ref: str


@dataclass(frozen=True)
class MakerFeeAssessment:
    conditions_met: bool
    reason: str
    taker_rate: Decimal | None
    live_ready: bool = False


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _positive_decimal(value: object) -> Decimal:
    try:
        result = require_decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        raise MakerGateError("Invalid public contract") from None
    if result <= 0:
        raise MakerGateError("Invalid public contract")
    return result


def parse_public_api_contract(row: object, symbol: str, observed_at: datetime) -> PublicApiContract:
    """Parse one exact public contract row; promotional fee fields are irrelevant."""
    when = _utc(observed_at)
    if (
        not isinstance(row, dict)
        or not isinstance(symbol, str)
        or SYMBOL.fullmatch(symbol) is None
        or row.get("symbol") != symbol
        or when is None
    ):
        raise MakerGateError("Invalid public contract")
    base = symbol.removesuffix("_USDT")
    if (
        row.get("baseCoin") != base
        or row.get("quoteCoin") != "USDT"
        or row.get("settleCoin") != "USDT"
        or type(row.get("state")) is not int
        or type(row.get("futureType")) is not int
        or type(row.get("apiAllowed")) is not bool
    ):
        raise MakerGateError("Invalid public contract")
    rules = {
        field: str(_positive_decimal(row.get(field)))
        for field in ("contractSize", "priceUnit", "volUnit", "minVol", "maxVol")
    }
    if Decimal(rules["minVol"]) > Decimal(rules["maxVol"]):
        raise MakerGateError("Invalid public contract")
    rules.update(
        symbol=symbol,
        state=row["state"],
        futureType=row["futureType"],
        apiAllowed=row["apiAllowed"],
    )
    digest = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()
    return PublicApiContract(
        symbol=symbol,
        observed_at=when,
        rules_sha256=digest,
        api_allowed=row["apiAllowed"],
        active=row["state"] == 0 and row["futureType"] == 1,
    )


def assess_api_maker_zero(
    *,
    symbol: str,
    account_ref: str,
    zone_catalog: PublicZeroFeeCatalog | None,
    public: PublicApiContract | None,
    account_fee: AccountFeeEvidence | None,
    confirmation: MexcApiMakerConfirmation | None,
    now: datetime,
) -> MakerFeeAssessment:
    """Evaluate metadata only. True never authorizes Live or verifies issuer authenticity."""
    current = _utc(now)
    if current is None or not isinstance(symbol, str) or SYMBOL.fullmatch(symbol) is None:
        return MakerFeeAssessment(False, "INVALID_CONTEXT", None)
    if not isinstance(account_ref, str) or ACCOUNT_REF.fullmatch(account_ref) is None:
        return MakerFeeAssessment(False, "INVALID_CONTEXT", None)
    if not isinstance(zone_catalog, PublicZeroFeeCatalog):
        return MakerFeeAssessment(False, "PUBLIC_ZONE_MISSING", None)
    zone_at = _utc(zone_catalog.observed_at)
    if (
        zone_at is None
        or not timedelta(0) <= current - zone_at <= MAX_SNAPSHOT_AGE
        or type(zone_catalog.total_rows) is not int
        or zone_catalog.total_rows < 1
    ):
        return MakerFeeAssessment(False, "PUBLIC_ZONE_STALE_OR_INVALID", None)
    if not isinstance(zone_catalog.candidates, tuple) or any(
        not isinstance(candidate, PublicZeroFeeCandidate) for candidate in zone_catalog.candidates
    ):
        return MakerFeeAssessment(False, "PUBLIC_ZONE_SYMBOL_NOT_CANDIDATE", None)
    matches = [candidate for candidate in zone_catalog.candidates if candidate.symbol == symbol]
    if (
        len(matches) != 1
        or matches[0].fee_rate_type not in KNOWN_FEE_RATE_TYPES
        or matches[0].promo_excludes_api_users is not True
        or matches[0].api_account_fee != "unknown"
    ):
        return MakerFeeAssessment(False, "PUBLIC_ZONE_SYMBOL_NOT_CANDIDATE", None)
    if not isinstance(public, PublicApiContract) or public.symbol != symbol:
        return MakerFeeAssessment(False, "PUBLIC_CONTRACT_MISSING", None)
    public_at = _utc(public.observed_at)
    if (
        public.active is not True
        or public.api_allowed is not True
        or not isinstance(public.rules_sha256, str)
        or SHA256.fullmatch(public.rules_sha256) is None
        or public_at is None
        or public_at != zone_at
        or not timedelta(0) <= current - public_at <= MAX_SNAPSHOT_AGE
    ):
        return MakerFeeAssessment(False, "PUBLIC_CONTRACT_UNSUITABLE", None)
    if not isinstance(account_fee, AccountFeeEvidence) or account_fee.account_ref != account_ref:
        return MakerFeeAssessment(False, "ACCOUNT_FEE_MISSING", None)
    fee = account_fee.fee
    if not isinstance(fee, AccountFeeBaseline):
        return MakerFeeAssessment(False, "ACCOUNT_FEE_INVALID", None)
    fee_at = _utc(fee.observed_at)
    if (
        fee.symbol != symbol
        or fee.source != "private_account_fee"
        or fee.fee_rate_mode != "NORMAL"
        or fee.promotion_eligible is not False
        or not isinstance(account_fee.account_binding_sha256, str)
        or SHA256.fullmatch(account_fee.account_binding_sha256) is None
        or fee_at is None
        or not timedelta(0) <= current - fee_at <= MAX_SNAPSHOT_AGE
        or type(fee.maker_rate) is not Decimal
        or type(fee.taker_rate) is not Decimal
        or not fee.maker_rate.is_finite()
        or not fee.taker_rate.is_finite()
        or not 0 <= fee.maker_rate < 1
        or not 0 <= fee.taker_rate < 1
    ):
        return MakerFeeAssessment(False, "ACCOUNT_FEE_INVALID", None)
    if fee.maker_rate != 0:
        return MakerFeeAssessment(False, "MAKER_RATE_NOT_ZERO", fee.taker_rate)
    if not isinstance(confirmation, MexcApiMakerConfirmation):
        return MakerFeeAssessment(False, "MEXC_API_CONFIRMATION_MISSING", fee.taker_rate)
    issued = _utc(confirmation.issued_at)
    expires = _utc(confirmation.expires_at)
    reviewed = _utc(confirmation.reviewed_at)
    if (
        confirmation.issuer != "MEXC"
        or not isinstance(confirmation.channel, str)
        or confirmation.channel not in CONFIRMATION_CHANNELS
        or not isinstance(confirmation.reference, str)
        or not 4 <= len(confirmation.reference.strip()) <= 120
        or not isinstance(confirmation.artifact_sha256, str)
        or SHA256.fullmatch(confirmation.artifact_sha256) is None
        or confirmation.account_ref != account_ref
        or confirmation.symbol != symbol
        or confirmation.scope != API_MAKER_SCOPE
        or type(confirmation.maker_rate) is not Decimal
        or not confirmation.maker_rate.is_finite()
        or confirmation.maker_rate != 0
        or issued is None
        or expires is None
        or reviewed is None
        or not issued <= reviewed <= current < expires
        or not issued <= fee_at
        or not isinstance(confirmation.reviewer_ref, str)
        or not 4 <= len(confirmation.reviewer_ref.strip()) <= 80
    ):
        return MakerFeeAssessment(False, "MEXC_API_CONFIRMATION_INVALID", fee.taker_rate)
    return MakerFeeAssessment(True, "CONDITIONS_MET_REVIEW_ONLY", fee.taker_rate)
