"""Pure public MEXC zone research shortlist; never authorizes an order."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ai_futures.mexc_zero_fee_catalog import (
    KNOWN_FEE_RATE_TYPES,
    SYMBOL_PATTERN,
    PublicZeroFeeCandidate,
    PublicZeroFeeCatalog,
)


@dataclass(frozen=True)
class TickerEvidence:
    symbol: str
    bid: Decimal
    ask: Decimal
    amount24_raw: Decimal
    source_time: datetime
    received_at: datetime


@dataclass(frozen=True)
class ResearchPolicy:
    max_spread_bps: Decimal
    max_ticker_age_seconds: Decimal
    max_catalog_age_seconds: Decimal
    max_pairs: int
    scope_symbols: frozenset[str]


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    spread_bps: Decimal
    amount24_raw: Decimal


@dataclass(frozen=True)
class ResearchShortlist:
    candidates: tuple[RankedCandidate, ...]
    reason: str
    rejected_counts: tuple[tuple[str, int], ...] = ()
    trade_ready: bool = False
    turnover_unit: str = "unverified_currency"
    needs_depth_validation: bool = True
    needs_account_api_maker_gate: bool = True
    needs_shared_risk_gate: bool = True


def _deny(reason: str) -> ResearchShortlist:
    return ResearchShortlist((), reason)


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _positive_decimal(value: object) -> bool:
    return type(value) is Decimal and value.is_finite() and value > 0


def _seconds_between(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    return (
        Decimal(delta.days) * 86400
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / 1_000_000
    )


def select_research_shortlist(
    *,
    catalog: PublicZeroFeeCatalog | None,
    tickers: tuple[TickerEvidence, ...],
    policy: ResearchPolicy | None,
    now: datetime,
) -> ResearchShortlist:
    """Rank by raw relative turnover, then spread, then symbol; deny incomplete input."""
    current = _utc(now)
    if current is None or not isinstance(policy, ResearchPolicy):
        return _deny("INVALID_CONTEXT")
    if (
        not _positive_decimal(policy.max_spread_bps)
        or not _positive_decimal(policy.max_ticker_age_seconds)
        or not _positive_decimal(policy.max_catalog_age_seconds)
        or type(policy.max_pairs) is not int
        or not 1 <= policy.max_pairs <= 3
        or not isinstance(policy.scope_symbols, frozenset)
        or not policy.scope_symbols
        or any(
            not isinstance(symbol, str) or SYMBOL_PATTERN.fullmatch(symbol) is None
            for symbol in policy.scope_symbols
        )
    ):
        return _deny("INVALID_POLICY")
    if not isinstance(catalog, PublicZeroFeeCatalog):
        return _deny("CATALOG_MISSING")
    catalog_at = _utc(catalog.observed_at)
    if (
        catalog_at is None
        or _seconds_between(current, catalog_at) < 0
        or _seconds_between(current, catalog_at) > policy.max_catalog_age_seconds
        or type(catalog.total_rows) is not int
        or catalog.total_rows < 1
        or not isinstance(catalog.candidates, tuple)
        or any(not isinstance(item, PublicZeroFeeCandidate) for item in catalog.candidates)
        or any(
            not isinstance(item.symbol, str) or SYMBOL_PATTERN.fullmatch(item.symbol) is None
            for item in catalog.candidates
        )
        or any(
            type(count) is not int or count < 0
            for count in (
                catalog.non_candidate_rows,
                catalog.unknown_rows,
                catalog.invalid_rows,
            )
        )
        or catalog.total_rows
        != len(catalog.candidates)
        + catalog.non_candidate_rows
        + catalog.unknown_rows
        + catalog.invalid_rows
    ):
        return _deny("CATALOG_STALE_OR_INVALID")
    candidate_symbols = [item.symbol for item in catalog.candidates]
    if len(candidate_symbols) != len(set(candidate_symbols)):
        return _deny("CATALOG_STALE_OR_INVALID")
    candidates = {
        item.symbol
        for item in catalog.candidates
        if isinstance(item.fee_rate_type, str)
        and item.fee_rate_type in KNOWN_FEE_RATE_TYPES
        and item.promo_excludes_api_users is True
        and item.api_account_fee == "unknown"
    }
    if not policy.scope_symbols <= candidates:
        return _deny("SCOPE_NOT_CURRENT_ZONE_CANDIDATE")
    if not isinstance(tickers, tuple):
        return _deny("TICKER_SET_INVALID")
    if any(not isinstance(ticker, TickerEvidence) for ticker in tickers):
        return _deny("TICKER_INVALID")
    ticker_symbols = [ticker.symbol for ticker in tickers]
    if any(not isinstance(symbol, str) for symbol in ticker_symbols):
        return _deny("TICKER_SYMBOL_MISMATCH")
    if (
        len(ticker_symbols) != len(set(ticker_symbols))
        or not set(ticker_symbols) <= policy.scope_symbols
    ):
        return _deny("TICKER_SYMBOL_MISMATCH")
    rejections = Counter()
    rejections["missing_ticker"] = len(policy.scope_symbols - set(ticker_symbols))
    ranked = []
    for ticker in tickers:
        source_at = _utc(ticker.source_time)
        received_at = _utc(ticker.received_at)
        if (
            source_at is None
            or received_at is None
            or not source_at <= received_at <= current
            or _seconds_between(current, source_at) > policy.max_ticker_age_seconds
            or _seconds_between(current, received_at) > policy.max_ticker_age_seconds
            or not _positive_decimal(ticker.bid)
            or not _positive_decimal(ticker.ask)
            or not _positive_decimal(ticker.amount24_raw)
            or ticker.bid >= ticker.ask
        ):
            rejections["stale_or_invalid_ticker"] += 1
            continue
        midpoint = (ticker.ask + ticker.bid) / 2
        spread_bps = (ticker.ask - ticker.bid) / midpoint * 10_000
        if spread_bps > policy.max_spread_bps:
            rejections["spread_limit_exceeded"] += 1
            continue
        ranked.append(RankedCandidate(ticker.symbol, spread_bps, ticker.amount24_raw))
    rejected_counts = tuple(sorted((key, count) for key, count in rejections.items() if count))
    if not ranked:
        return ResearchShortlist((), "NO_VALID_CANDIDATES", rejected_counts)
    ranked.sort(key=lambda item: (-item.amount24_raw, item.spread_bps, item.symbol))
    return ResearchShortlist(
        tuple(ranked[: policy.max_pairs]), "RESEARCH_SHORTLIST_ONLY", rejected_counts
    )
