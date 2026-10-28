import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.mexc_usdt_research import (
    TickerEvidence,
    UsdtCatalogError,
    UsdtResearchPolicy,
    parse_public_usdt_catalog,
    select_usdt_research_shortlist,
)

NOW = datetime(2026, 10, 21, 7, tzinfo=UTC)


def row(symbol="BTC_USDT", **changes):
    return {
        "symbol": symbol,
        "baseCoin": symbol.removesuffix("_USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "state": 0,
        "futureType": 1,
        "apiAllowed": True,
        "makerFeeRate": "0.0002",
        "takerFeeRate": "0.0005",
        "isZeroFeeRate": False,
        "isZeroFeeSymbol": False,
    } | changes


def catalog(rows=None, observed_at=NOW):
    return parse_public_usdt_catalog(
        json.dumps({"success": True, "code": 0, "data": rows or [row()]}).encode(),
        observed_at,
    )


def ticker(symbol="BTC_USDT", *, bid="99", ask="101", amount="1000"):
    return TickerEvidence(symbol, Decimal(bid), Decimal(ask), Decimal(amount), NOW, NOW)


def policy(**changes):
    return UsdtResearchPolicy(
        **{
            "max_spread_bps": Decimal("250"),
            "max_ticker_age_seconds": Decimal("5"),
            "max_catalog_age_seconds": Decimal("300"),
            "max_pairs": 3,
        }
        | changes
    )


def select(rows=None, tickers=None, *, observed_at=NOW, now=NOW, config=None):
    return select_usdt_research_shortlist(
        catalog=catalog(rows, observed_at),
        tickers=tuple(tickers if tickers is not None else [ticker()]),
        policy=config or policy(),
        now=now,
    )


def test_nonzero_fee_btc_and_stock_are_research_candidates():
    result = select(
        [row(), row("AAPL_USDT", makerFeeRate="0.001", takerFeeRate="0.002")],
        [ticker(), ticker("AAPL_USDT", amount="2000")],
    )
    assert [candidate.symbol for candidate in result.candidates] == ["AAPL_USDT", "BTC_USDT"]
    assert result.candidates[0].spread_bps == Decimal("200")
    assert result.trade_ready is False
    assert result.needs_account_fee_validation is True
    assert result.needs_cost_aware_risk_gate is True
    assert result.needs_depth_validation is True


def test_zero_fee_flags_and_rates_do_not_change_universe():
    all_flags = row(isZeroFeeRate=True, isZeroFeeSymbol=True, makerFeeRate="0", takerFeeRate="0")
    no_flags = row(isZeroFeeRate=False, isZeroFeeSymbol=False)
    missing_flags = row()
    for field in ("isZeroFeeRate", "isZeroFeeSymbol", "makerFeeRate", "takerFeeRate"):
        missing_flags.pop(field)
    assert catalog([all_flags]).symbols == catalog([no_flags]).symbols
    assert catalog([missing_flags]).symbols == ("BTC_USDT",)


def test_research_selection_caps_at_three():
    symbols = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "AAPL_USDT")
    result = select(
        [row(symbol) for symbol in symbols],
        [ticker(symbol, amount=str(index)) for index, symbol in enumerate(symbols, start=1)],
    )
    assert [candidate.symbol for candidate in result.candidates] == [
        "AAPL_USDT",
        "SOL_USDT",
        "ETH_USDT",
    ]


def test_non_usdt_inactive_delivery_and_api_denied_rejected():
    rows = [
        row(),
        row("ETH_USD", quoteCoin="USD", settleCoin="USD"),
        row("SOL_USDT", state=4),
        row("XRP_USDT", futureType=2),
        row("AAPL_USDT", apiAllowed=False),
    ]
    result = catalog(rows)
    assert result.symbols == ("BTC_USDT",)
    assert result.excluded_rows == 4
    assert select(rows, [ticker("AAPL_USDT")]).reason == "TICKER_SYMBOL_MISMATCH"


def test_missing_unknown_and_stale_catalog_fail_closed():
    assert select([row(), row("AAPL_USDT")]).rejected_counts == (("missing_ticker", 1),)
    assert select([row(baseCoin=None)]).reason == "CATALOG_STALE_OR_INVALID"
    assert select([row(), row()], [ticker()]).reason == "CATALOG_STALE_OR_INVALID"
    assert select(observed_at=NOW - timedelta(seconds=301)).reason == "CATALOG_STALE_OR_INVALID"
    assert select(observed_at=NOW + timedelta(seconds=1)).reason == "CATALOG_STALE_OR_INVALID"
    assert select(config=policy(max_pairs=4)).reason == "INVALID_POLICY"


@pytest.mark.parametrize(
    "change",
    [
        {"bid": Decimal("0")},
        {"ask": Decimal("99")},
        {"bid": Decimal("NaN")},
        {"source_time": NOW - timedelta(seconds=6)},
        {"received_at": NOW + timedelta(seconds=1)},
    ],
)
def test_crossed_invalid_and_stale_ticker_fail_closed(change):
    result = select(tickers=[replace(ticker(), **change)])
    assert result.candidates == ()
    assert result.rejected_counts == (("stale_or_invalid_ticker", 1),)


def test_duplicate_or_foreign_ticker_denies_batch():
    assert select(tickers=[ticker(), ticker()]).reason == "TICKER_SYMBOL_MISMATCH"
    assert select(tickers=[ticker("DOGE_USDT")]).reason == "TICKER_SYMBOL_MISMATCH"


@pytest.mark.parametrize("body", [b"bad", b'{"success":true,"code":0,"data":{}}'])
def test_bad_catalog_envelope_fails(body):
    with pytest.raises(UsdtCatalogError):
        parse_public_usdt_catalog(body, NOW)
