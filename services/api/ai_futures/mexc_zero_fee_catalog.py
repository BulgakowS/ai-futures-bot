"""Public MEXC zero-fee candidates for research, never API fee eligibility."""

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ai_futures.schemas import require_decimal

CATALOG_URL = "https://api.mexc.com/api/v1/contract/detail/country"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{1,30}_USDT\Z")
KNOWN_FEE_RATE_TYPES = {"BASE", "TEMP"}


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class PublicZeroFeeCandidate:
    symbol: str
    fee_rate_type: str
    promo_excludes_api_users: bool = True
    api_account_fee: str = "unknown"


@dataclass(frozen=True)
class PublicZeroFeeCatalog:
    observed_at: datetime
    total_rows: int
    candidates: tuple[PublicZeroFeeCandidate, ...]
    non_candidate_rows: int
    unknown_rows: int
    invalid_rows: int


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Invalid catalog JSON")
        result[key] = value
    return result


def _reject_constant(_value):
    raise CatalogError("Invalid catalog JSON")


def _field(row, name, expected_type):
    if name not in row or row[name] is None:
        return None, "unknown"
    value = row[name]
    if expected_type is int:
        valid = type(value) is int
    elif expected_type is bool:
        valid = type(value) is bool
    else:
        valid = isinstance(value, expected_type)
    return (value, None) if valid else (None, "invalid")


def _fee(row, name):
    if name not in row or row[name] is None:
        return None, "unknown"
    try:
        value = require_decimal(row[name])
    except (TypeError, ValueError, ArithmeticError):
        return None, "invalid"
    return (value, None) if value >= 0 else (None, "invalid")


def _classify(row, duplicate_symbols):
    if not isinstance(row, dict):
        return "invalid", None
    symbol, error = _field(row, "symbol", str)
    if error or not SYMBOL_PATTERN.fullmatch(symbol):
        return "invalid", None
    if symbol in duplicate_symbols:
        return "invalid", None
    values = {}
    unknown = False
    for field, expected_type in (
        ("state", int),
        ("futureType", int),
        ("baseCoin", str),
        ("quoteCoin", str),
        ("settleCoin", str),
        ("apiAllowed", bool),
        ("isZeroFeeRate", bool),
        ("isZeroFeeSymbol", bool),
        ("feeRateType", str),
    ):
        value, error = _field(row, field, expected_type)
        if error == "invalid":
            return "invalid", None
        if error == "unknown":
            unknown = True
        values[field] = value
    for field in ("makerFeeRate", "takerFeeRate"):
        value, error = _fee(row, field)
        if error == "invalid":
            return "invalid", None
        if error == "unknown":
            unknown = True
        values[field] = value
    if unknown:
        return "unknown", None
    if (
        not values["baseCoin"]
        or symbol != f"{values['baseCoin']}_USDT"
        or values["feeRateType"] not in KNOWN_FEE_RATE_TYPES
    ):
        return "unknown", None
    if (
        values["state"] != 0
        or values["futureType"] != 1
        or values["quoteCoin"] != "USDT"
        or values["settleCoin"] != "USDT"
        or values["apiAllowed"] is not True
        or values["isZeroFeeRate"] is not True
        or values["isZeroFeeSymbol"] is not True
        or values["makerFeeRate"] != Decimal(0)
        or values["takerFeeRate"] != Decimal(0)
    ):
        return "non_candidate", None
    return "candidate", PublicZeroFeeCandidate(symbol, values["feeRateType"])


def parse_public_zero_fee_catalog(content: bytes, observed_at: datetime) -> PublicZeroFeeCatalog:
    if len(content) > MAX_CATALOG_BYTES:
        raise CatalogError("Catalog response exceeds cap")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CatalogError("Observation time must be UTC aware")
    try:
        body = json.loads(
            content,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise CatalogError("Invalid catalog JSON") from None
    if (
        not isinstance(body, dict)
        or body.get("success") is not True
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(body.get("data"), list)
    ):
        raise CatalogError("Invalid catalog envelope")
    rows = body["data"]
    symbols = Counter(
        row["symbol"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("symbol"), str)
    )
    duplicate_symbols = {symbol for symbol, count in symbols.items() if count > 1}
    counts = Counter()
    candidates = []
    for row in rows:
        status, candidate = _classify(row, duplicate_symbols)
        counts[status] += 1
        if candidate is not None:
            candidates.append(candidate)
    return PublicZeroFeeCatalog(
        observed_at=observed_at.astimezone(UTC),
        total_rows=len(rows),
        candidates=tuple(sorted(candidates, key=lambda item: item.symbol)),
        non_candidate_rows=counts["non_candidate"],
        unknown_rows=counts["unknown"],
        invalid_rows=counts["invalid"],
    )


async def fetch_public_zero_fee_catalog(client: httpx.AsyncClient) -> PublicZeroFeeCatalog:
    """Exactly one public GET, with a fixed body cap and no retries or redirects."""
    try:
        async with client.stream(
            "GET", CATALOG_URL, timeout=10.0, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise CatalogError("Catalog read failed")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_CATALOG_BYTES:
                    raise CatalogError("Catalog response exceeds cap")
    except (httpx.HTTPError, OSError):
        raise CatalogError("Catalog read failed") from None
    return parse_public_zero_fee_catalog(bytes(content), datetime.now(UTC))


def sanitized_catalog(catalog: PublicZeroFeeCatalog) -> dict:
    return {
        "observed_at": catalog.observed_at.isoformat(),
        "source": CATALOG_URL,
        "scope": "public research candidates only",
        "candidate_fields_checked": ["baseCoin", "quoteCoin", "settleCoin"],
        "total_rows": catalog.total_rows,
        "candidate_count": len(catalog.candidates),
        "non_candidate_rows": catalog.non_candidate_rows,
        "unknown_rows": catalog.unknown_rows,
        "invalid_rows": catalog.invalid_rows,
        "classification_note": (
            "invalid means outside the local S14 USDT symbol/field schema, "
            "not necessarily invalid at MEXC"
        ),
        "candidates": [
            {
                "symbol": item.symbol,
                "fee_rate_type": item.fee_rate_type,
                "promo_excludes_api_users": item.promo_excludes_api_users,
                "api_account_fee": item.api_account_fee,
            }
            for item in catalog.candidates
        ],
        "private_requests": 0,
        "orders": 0,
    }
