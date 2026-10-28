from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from ai_futures.mexc import (
    MarketDataError,
    MexcPublic,
    decimal,
    epoch,
    normalize_candles,
    normalize_depth,
    normalize_funding_page,
    normalize_rules,
    normalize_ticker,
    parse_json,
)


def test_exact_json_and_contract_units():
    assert parse_json('{"price":81946.1,"rate":-1e-06}')["price"] == Decimal("81946.1")
    assert decimal(Decimal("0.0001")) == "0.0001"
    with pytest.raises(MarketDataError):
        decimal(0.1)
    with pytest.raises(MarketDataError):
        decimal("NaN")
    assert epoch(1791991035731).microsecond == 731000
    with pytest.raises(MarketDataError):
        epoch(1791991035)


def test_rules_from_historical_public_snapshots():
    data = {
        "symbol": "BTC_USDT",
        "futureType": 1,
        "settleCoin": "USDT",
        "state": 0,
        "contractSize": "0.0001",
        "priceUnit": "0.1",
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 400000,
        "minLeverage": 1,
        "maxLeverage": 500,
        "makerFeeRate": 0,
        "takerFeeRate": "0.0002",
        "feeRateMode": "NORMAL",
        "feeRateType": "BASE",
        "tieredFeeRates": [],
        "leverageFeeRates": [],
        "riskLimitMode": "CUSTOM",
        "riskLimitType": "BY_VOLUME",
        "riskLimitCustom": [
            {"level": 1, "maxVol": 120000, "mmr": "0.001", "imr": "0.002", "maxLeverage": "500"}
        ],
        "maintenanceMarginRate": "0.001",
        "initialMarginRate": "0.002",
        "liquidationFeeRate": "0.0004",
    }
    rules = normalize_rules([data], "BTC_USDT")
    assert rules["quantity_unit"] == "contracts" and rules["contractSize"] == "0.0001"
    assert rules["priceUnit"] == "0.1" and rules["volUnit"] == "1"
    assert rules["makerFeeRate"] == "0" and rules["takerFeeRate"] == "0.0002"
    new_public = data | {
        "makerFeeRate": "0.0001",
        "takerFeeRate": "0.0004",
        "riskLimitCustom": [
            {"level": 1, "maxVol": 310000, "mmr": "0.001", "imr": "0.002", "maxLeverage": "500"}
        ],
    }
    updated = normalize_rules(new_public, "BTC_USDT")
    assert updated["version"] != rules["version"]
    assert updated["makerFeeRate"] == "0.0001"
    from ai_futures.mexc import public_fee_terms_supported

    assert public_fee_terms_supported(rules)
    assert public_fee_terms_supported(updated)
    for change in (
        {"feeRateMode": "TIERED"},
        {"feeRateType": "OTHER"},
        {"tieredFeeRates": [{"level": 1}]},
        {"state": 1},
    ):
        changed_terms = normalize_rules(new_public | change, "BTC_USDT")
        assert changed_terms["version"] != updated["version"]
        if change.get("state") != 1:
            assert not public_fee_terms_supported(changed_terms)
    changed = normalize_rules(data | {"volUnit": 2}, "BTC_USDT")
    assert changed["version"] != rules["version"]
    with pytest.raises(MarketDataError):
        normalize_rules([data, data], "BTC_USDT")


def test_candles_arrays_and_finality():
    t = 1791991020
    data = {
        "time": [t, t + 60],
        "open": ["10", "11"],
        "close": ["11", "11"],
        "high": ["12", "12"],
        "low": ["9", "9"],
        "vol": [100, 0],
        "amount": [10, 0],
    }
    rows = normalize_candles(data, "1m", epoch(t + 61, False))
    assert rows[0][2] is True and rows[1][2] is False
    assert rows[0][1]["vol"] == "100"
    with pytest.raises(MarketDataError):
        normalize_candles(data | {"vol": [100]}, "1m", epoch(t + 61, False))
    with pytest.raises(MarketDataError):
        normalize_candles(data | {"low": ["15", "9"]}, "1m", epoch(t + 61, False))

    hour4 = int(datetime(2026, 10, 18, tzinfo=UTC).timestamp())
    four_hour = data | {"time": [hour4, hour4 + 14400]}
    rows = normalize_candles(four_hour, "4h", epoch(hour4 + 14401, False))
    assert [row[2] for row in rows] == [True, False]


def test_depth_contracts_and_malformed_arrays():
    book = {
        "asks": [["11", 100, 1]],
        "bids": [["10", 200, 2]],
        "version": 100,
        "timestamp": 1791991035731,
    }
    parsed, timestamp = normalize_depth(book)
    assert parsed["asks"] == [["11", "100"]]
    assert parsed["fill_model"] == "estimated_rest_snapshot"
    assert timestamp == datetime(2026, 10, 14, 15, 17, 15, 731000, UTC)
    with pytest.raises(MarketDataError):
        normalize_depth(book | {"asks": [["11"]]})
    with pytest.raises(MarketDataError):
        normalize_depth(book | {"bids": [["12", 200]]})


async def test_public_http_application_errors_retry_and_no_credentials():
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text='{"success":true,"code":0,"data":1791991035731}')

    adapter = MexcPublic(
        httpx.AsyncClient(base_url="https://api.mexc.com", transport=httpx.MockTransport(respond))
    )
    assert (await adapter.server_time()).microsecond == 731000
    assert len(calls) == 2
    assert all(
        "authorization" not in call.headers and "apikey" not in call.headers for call in calls
    )
    await adapter.close()
    adapter = MexcPublic(
        httpx.AsyncClient(
            base_url="https://api.mexc.com",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"success": False})),
        )
    )
    with pytest.raises(MarketDataError):
        await adapter.server_time()
    await adapter.close()


def test_ticker_rejects_symbol_and_crossed_spread():
    ticker = {
        "symbol": "BTC_USDT",
        "lastPrice": "10.1",
        "bid1": "10",
        "ask1": "11",
        "fairPrice": "10",
        "indexPrice": "10",
        "fundingRate": "-0.000001",
        "holdVol": 10,
        "timestamp": 1791991035731,
    }
    assert normalize_ticker(ticker, "BTC_USDT")[0]["fundingRate"] == "-0.000001"
    with pytest.raises(MarketDataError):
        normalize_ticker(ticker, "ETH_USDT")
    with pytest.raises(MarketDataError):
        normalize_ticker(ticker | {"bid1": 12}, "BTC_USDT")


def test_funding_page_metadata_is_checked():
    row = {"fundingRate": "-0.00001", "settleTime": 1791993600000, "collectCycle": 8}
    page = {
        "resultList": [row],
        "currentPage": 1,
        "pageSize": 2,
        "totalCount": 3,
        "totalPage": 2,
    }
    assert normalize_funding_page(page, 1, 2)["rows"][0]["fundingRate"] == "-0.00001"
    for invalid in (
        page | {"currentPage": 2},
        page | {"totalPage": 1},
        page | {"pageSize": 3},
        page | {"resultList": [row | {"collectCycle": 0}]},
    ):
        with pytest.raises(MarketDataError):
            normalize_funding_page(invalid, 1, 2)
