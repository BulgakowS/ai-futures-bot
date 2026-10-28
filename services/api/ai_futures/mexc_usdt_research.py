"""Pure research selection across public active API-allowed USDT-M contracts.

No network, account access, broker, or Live activation is connected to this module.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

CATALOG_SOURCE = "https://api.mexc.com/api/v1/contract/detail/country"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
SYMBOL = re.compile(r"[A-Z0-9]{1,30}_USDT\Z")


class UsdtCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class PublicUsdtCatalog:
    observed_at: datetime
    total_rows: int
    symbols: tuple[str, ...]
    excluded_rows: int
    unknown_rows: int


@dataclass(frozen=True)
class TickerEvidence:
    symbol: str
    bid: Decimal
    ask: Decimal
    amount24_raw: Decimal
    source_time: datetime
    received_at: datetime


@dataclass(frozen=True)
class UsdtResearchPolicy:
    max_spread_bps: Decimal
    max_ticker_age_seconds: Decimal
    max_catalog_age_seconds: Decimal
    max_pairs: int


@dataclass(frozen=True)
class UsdtResearchCandidate:
    symbol: str
    spread_bps: Decimal
    amount24_raw: Decimal


@dataclass(frozen=True)
class UsdtResearchShortlist:
    candidates: tuple[UsdtResearchCandidate, ...]
    reason: str
    rejected_counts: tuple[tuple[str, int], ...] = ()
    trade_ready: bool = False
    turnover_unit: str = "unverified_currency"
    needs_depth_validation: bool = True
    needs_account_fee_validation: bool = True
    needs_cost_aware_risk_gate: bool = True


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _seconds(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    return (
        Decimal(delta.days) * 86400
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / 1_000_000
    )


def _positive(value: object) -> bool:
    return type(value) is Decimal and value.is_finite() and value > 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsdtCatalogError("Invalid contract catalog JSON")
        result[key] = value
    return result


def _reject_constant(_value):
    raise UsdtCatalogError("Invalid contract catalog JSON")


def parse_public_usdt_catalog(content: bytes, observed_at: datetime) -> PublicUsdtCatalog:
    """Parse a caller-supplied Contract Info response; fee fields are ignored."""
    when = _utc(observed_at)
    if when is None or not isinstance(content, bytes) or len(content) > MAX_CATALOG_BYTES:
        raise UsdtCatalogError("Invalid contract catalog context")
    try:
        body = json.loads(
            content,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise UsdtCatalogError("Invalid contract catalog JSON") from None
    if (
        not isinstance(body, dict)
        or body.get("success") is not True
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(body.get("data"), list)
        or not body["data"]
    ):
        raise UsdtCatalogError("Invalid contract catalog envelope")
    symbols = []
    excluded = unknown = 0
    seen = set()
    for row in body["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            unknown += 1
            continue
        symbol = row["symbol"]
        if not symbol.endswith("_USDT"):
            excluded += 1
            continue
        if SYMBOL.fullmatch(symbol) is None or symbol in seen:
            unknown += 1
            continue
        seen.add(symbol)
        base = symbol.removesuffix("_USDT")
        if (
            row.get("baseCoin") != base
            or row.get("quoteCoin") != "USDT"
            or row.get("settleCoin") != "USDT"
            or type(row.get("state")) is not int
            or type(row.get("futureType")) is not int
            or type(row.get("apiAllowed")) is not bool
        ):
            unknown += 1
            continue
        if row["state"] != 0 or row["futureType"] != 1 or row["apiAllowed"] is not True:
            excluded += 1
            continue
        symbols.append(symbol)
    return PublicUsdtCatalog(when, len(body["data"]), tuple(sorted(symbols)), excluded, unknown)


def select_usdt_research_shortlist(
    *,
    catalog: PublicUsdtCatalog | None,
    tickers: tuple[TickerEvidence, ...],
    policy: UsdtResearchPolicy | None,
    now: datetime,
) -> UsdtResearchShortlist:
    """Rank current public contracts by raw turnover/spread, for research only."""
    current = _utc(now)
    if current is None or not isinstance(policy, UsdtResearchPolicy):
        return UsdtResearchShortlist((), "INVALID_CONTEXT")
    if (
        not _positive(policy.max_spread_bps)
        or not _positive(policy.max_ticker_age_seconds)
        or not _positive(policy.max_catalog_age_seconds)
        or type(policy.max_pairs) is not int
        or not 1 <= policy.max_pairs <= 3
    ):
        return UsdtResearchShortlist((), "INVALID_POLICY")
    if not isinstance(catalog, PublicUsdtCatalog):
        return UsdtResearchShortlist((), "CATALOG_MISSING")
    catalog_at = _utc(catalog.observed_at)
    if (
        catalog_at is None
        or not 0 <= _seconds(current, catalog_at) <= policy.max_catalog_age_seconds
        or type(catalog.total_rows) is not int
        or catalog.total_rows < 1
        or type(catalog.excluded_rows) is not int
        or catalog.excluded_rows < 0
        or type(catalog.unknown_rows) is not int
        or catalog.unknown_rows != 0
        or not isinstance(catalog.symbols, tuple)
        or not catalog.symbols
        or catalog.total_rows != len(catalog.symbols) + catalog.excluded_rows
        or len(set(catalog.symbols)) != len(catalog.symbols)
        or any(not isinstance(s, str) or SYMBOL.fullmatch(s) is None for s in catalog.symbols)
    ):
        return UsdtResearchShortlist((), "CATALOG_STALE_OR_INVALID")
    if not isinstance(tickers, tuple) or any(not isinstance(t, TickerEvidence) for t in tickers):
        return UsdtResearchShortlist((), "TICKER_SET_INVALID")
    ticker_symbols = [t.symbol for t in tickers]
    if (
        any(not isinstance(s, str) for s in ticker_symbols)
        or len(ticker_symbols) != len(set(ticker_symbols))
        or not set(ticker_symbols) <= set(catalog.symbols)
    ):
        return UsdtResearchShortlist((), "TICKER_SYMBOL_MISMATCH")
    rejections = Counter()
    rejections["missing_ticker"] = len(set(catalog.symbols) - set(ticker_symbols))
    ranked = []
    for ticker in tickers:
        source_at = _utc(ticker.source_time)
        received_at = _utc(ticker.received_at)
        if (
            source_at is None
            or received_at is None
            or not source_at <= received_at <= current
            or _seconds(current, source_at) > policy.max_ticker_age_seconds
            or _seconds(current, received_at) > policy.max_ticker_age_seconds
            or not _positive(ticker.bid)
            or not _positive(ticker.ask)
            or not _positive(ticker.amount24_raw)
            or ticker.bid >= ticker.ask
        ):
            rejections["stale_or_invalid_ticker"] += 1
            continue
        midpoint = (ticker.ask + ticker.bid) / 2
        spread_bps = (ticker.ask - ticker.bid) / midpoint * 10_000
        if spread_bps > policy.max_spread_bps:
            rejections["spread_limit_exceeded"] += 1
            continue
        ranked.append(UsdtResearchCandidate(ticker.symbol, spread_bps, ticker.amount24_raw))
    counts = tuple(sorted((key, count) for key, count in rejections.items() if count))
    if not ranked:
        return UsdtResearchShortlist((), "NO_VALID_CANDIDATES", counts)
    ranked.sort(key=lambda item: (-item.amount24_raw, item.spread_bps, item.symbol))
    return UsdtResearchShortlist(tuple(ranked[: policy.max_pairs]), "RESEARCH_ONLY", counts)
