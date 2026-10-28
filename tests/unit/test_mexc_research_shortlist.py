import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.mexc_research_shortlist import (
    ResearchPolicy,
    TickerEvidence,
    select_research_shortlist,
)
from ai_futures.mexc_zero_fee_catalog import parse_public_zero_fee_catalog

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
SYMBOLS = ("ARB_USDT", "LINK_USDT", "TAO_USDT", "ZEC_USDT")


def catalog_row(symbol, **changes):
    return {
        "symbol": symbol,
        "baseCoin": symbol.removesuffix("_USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "state": 0,
        "futureType": 1,
        "apiAllowed": True,
        "isZeroFeeRate": True,
        "isZeroFeeSymbol": True,
        "makerFeeRate": "0",
        "takerFeeRate": "0",
        "feeRateType": "TEMP",
    } | changes


def catalog(rows=None, observed_at=NOW):
    rows = rows if rows is not None else [catalog_row(symbol) for symbol in SYMBOLS]
    return parse_public_zero_fee_catalog(
        json.dumps({"success": True, "code": 0, "data": rows}).encode(), observed_at
    )


def ticker(symbol, *, bid="99", ask="101", amount="1000", source_at=NOW):
    return TickerEvidence(
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        amount24_raw=Decimal(amount),
        source_time=source_at,
        received_at=NOW,
    )


def policy(scope=SYMBOLS, **changes):
    values = {
        "max_spread_bps": Decimal("250"),
        "max_ticker_age_seconds": Decimal("5"),
        "max_catalog_age_seconds": Decimal("300"),
        "max_pairs": 3,
        "scope_symbols": frozenset(scope),
    } | changes
    return ResearchPolicy(**values)


def select(*, public_catalog=None, observations=None, selector_policy=None, now=NOW):
    return select_research_shortlist(
        catalog=catalog() if public_catalog is None else public_catalog,
        tickers=tuple(ticker(symbol) for symbol in SYMBOLS)
        if observations is None
        else observations,
        policy=policy() if selector_policy is None else selector_policy,
        now=now,
    )


def test_relative_turnover_then_spread_then_symbol_and_three_cap():
    observations = (
        ticker("ARB_USDT", amount="3000", bid="99", ask="101"),
        ticker("LINK_USDT", amount="3000", bid="99.5", ask="100.5"),
        ticker("TAO_USDT", amount="2000", bid="99", ask="101"),
        ticker("ZEC_USDT", amount="2000", bid="99", ask="101"),
    )
    result = select(observations=observations)
    assert [item.symbol for item in result.candidates] == [
        "LINK_USDT",
        "ARB_USDT",
        "TAO_USDT",
    ]
    assert result.candidates[0].spread_bps == Decimal("100")
    assert result.reason == "RESEARCH_SHORTLIST_ONLY"
    assert result.trade_ready is False
    assert result.turnover_unit == "unverified_currency"
    assert result.needs_depth_validation is True
    assert result.needs_account_api_maker_gate is True
    assert result.needs_shared_risk_gate is True


def test_no_default_thresholds_and_no_fourth_pilot_pair():
    with pytest.raises(TypeError):
        ResearchPolicy(max_pairs=3, scope_symbols=frozenset(SYMBOLS))
    assert select(selector_policy=policy(max_pairs=4)).reason == "INVALID_POLICY"
    assert select(selector_policy=policy(max_spread_bps=Decimal("NaN"))).reason == (
        "INVALID_POLICY"
    )
    assert select(selector_policy=policy(scope=())).reason == "INVALID_POLICY"


def test_missing_stale_future_and_non_candidate_catalog_denied():
    assert (
        select_research_shortlist(
            catalog=None,
            tickers=tuple(ticker(symbol) for symbol in SYMBOLS),
            policy=policy(),
            now=NOW,
        ).reason
        == "CATALOG_MISSING"
    )
    for stamp in (NOW - timedelta(seconds=301), NOW + timedelta(seconds=1)):
        assert select(public_catalog=catalog(observed_at=stamp)).reason == (
            "CATALOG_STALE_OR_INVALID"
        )
    rows = [catalog_row(symbol) for symbol in SYMBOLS]
    rows[0]["isZeroFeeSymbol"] = None
    assert select(public_catalog=catalog(rows)).reason == "SCOPE_NOT_CURRENT_ZONE_CANDIDATE"


def test_scope_is_explicit_and_asset_class_is_not_inferred():
    restricted = policy(scope=("ARB_USDT", "LINK_USDT"), max_pairs=2)
    observations = (ticker("ARB_USDT"), ticker("LINK_USDT"))
    result = select(selector_policy=restricted, observations=observations)
    assert len(result.candidates) == 2
    partial = select(selector_policy=restricted, observations=observations[:1])
    assert [item.symbol for item in partial.candidates] == ["ARB_USDT"]
    assert partial.rejected_counts == (("missing_ticker", 1),)
    unknown_scope = policy(scope=("STOCK_USDT",))
    assert select(selector_policy=unknown_scope, observations=(ticker("STOCK_USDT"),)).reason == (
        "SCOPE_NOT_CURRENT_ZONE_CANDIDATE"
    )
    stock_catalog = catalog(rows=[catalog_row("STOCK_USDT")])
    stock = select(
        public_catalog=stock_catalog,
        selector_policy=unknown_scope,
        observations=(ticker("STOCK_USDT"),),
    )
    assert [item.symbol for item in stock.candidates] == ["STOCK_USDT"]
    assert stock.trade_ready is False


def test_ticker_missing_skipped_but_duplicate_and_extra_symbol_deny_batch():
    observations = tuple(ticker(symbol) for symbol in SYMBOLS)
    partial = select(observations=observations[:-1])
    assert len(partial.candidates) == 3
    assert partial.rejected_counts == (("missing_ticker", 1),)
    duplicate = observations[:-1] + (observations[0],)
    assert select(observations=duplicate).reason == "TICKER_SYMBOL_MISMATCH"
    wrong = observations[:-1] + (ticker("DOGE_USDT"),)
    assert select(observations=wrong).reason == "TICKER_SYMBOL_MISMATCH"


@pytest.mark.parametrize(
    "changes",
    [
        {"bid": Decimal("0")},
        {"ask": Decimal("99")},
        {"amount24_raw": Decimal("0")},
        {"bid": Decimal("NaN")},
        {"source_time": NOW - timedelta(seconds=6)},
        {"source_time": NOW + timedelta(seconds=1)},
        {"received_at": NOW + timedelta(seconds=1)},
    ],
)
def test_invalid_crossed_or_stale_ticker_is_skipped(changes):
    observations = tuple(ticker(symbol) for symbol in SYMBOLS)
    bad = replace(observations[0], **changes)
    result = select(observations=(bad,) + observations[1:])
    assert len(result.candidates) == 3
    assert SYMBOLS[0] not in {item.symbol for item in result.candidates}
    assert result.rejected_counts == (("stale_or_invalid_ticker", 1),)


def test_wide_spread_filtered_without_fake_liquidity_fallback():
    observations = tuple(ticker(symbol, bid="1", ask="2") for symbol in SYMBOLS)
    all_wide = select(observations=observations)
    assert all_wide.reason == "NO_VALID_CANDIDATES"
    assert all_wide.rejected_counts == (("spread_limit_exceeded", 4),)
    mixed = (ticker(SYMBOLS[0], bid="99", ask="101"),) + observations[1:]
    mixed_result = select(observations=mixed)
    assert [item.symbol for item in mixed_result.candidates] == [SYMBOLS[0]]
    assert mixed_result.rejected_counts == (("spread_limit_exceeded", 3),)


def test_one_stale_one_valid_and_all_invalid():
    scoped = policy(scope=("ARB_USDT", "LINK_USDT"), max_pairs=2)
    stale = ticker("ARB_USDT", source_at=NOW - timedelta(seconds=6))
    valid = ticker("LINK_USDT")
    partial = select(selector_policy=scoped, observations=(stale, valid))
    assert [item.symbol for item in partial.candidates] == ["LINK_USDT"]
    assert partial.rejected_counts == (("stale_or_invalid_ticker", 1),)
    none = select(selector_policy=scoped, observations=(stale,))
    assert none.candidates == () and none.reason == "NO_VALID_CANDIDATES"
    assert none.rejected_counts == (("missing_ticker", 1), ("stale_or_invalid_ticker", 1))


def test_naive_clock_and_float_policy_fail_closed():
    assert select(now=datetime(2026, 10, 20, 14, 0)).reason == "INVALID_CONTEXT"
    assert select(selector_policy=policy(max_spread_bps=250.0)).reason == "INVALID_POLICY"
