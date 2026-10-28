import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from ai_futures.mexc_zero_fee_catalog import (
    CATALOG_URL,
    MAX_CATALOG_BYTES,
    CatalogError,
    fetch_public_zero_fee_catalog,
    parse_public_zero_fee_catalog,
    sanitized_catalog,
)

STAMP = datetime(2026, 10, 20, 12, 0, tzinfo=timezone(timedelta(hours=3)))


def row(symbol="ARB_USDT", **changes):
    base = symbol.removesuffix("_USDT")
    return {
        "symbol": symbol,
        "baseCoin": base,
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "futureType": 1,
        "state": 0,
        "apiAllowed": True,
        "isZeroFeeRate": True,
        "isZeroFeeSymbol": True,
        "makerFeeRate": "0",
        "takerFeeRate": "0.0000",
        "feeRateType": "TEMP",
        "price": "1000000",
    } | changes


def payload(rows):
    return json.dumps({"success": True, "code": 0, "data": rows}).encode()


def test_candidates_are_public_research_only_and_raw_is_removed():
    rows = [row(), row("AVAX_USDT", feeRateType="BASE")]
    result = parse_public_zero_fee_catalog(payload(rows), STAMP)
    output = sanitized_catalog(result)
    assert result.observed_at == datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    assert output["candidate_count"] == 2
    assert [item["symbol"] for item in output["candidates"]] == ["ARB_USDT", "AVAX_USDT"]
    assert output["candidates"][0]["fee_rate_type"] == "TEMP"
    assert all(item["promo_excludes_api_users"] is True for item in output["candidates"])
    assert all(item["api_account_fee"] == "unknown" for item in output["candidates"])
    assert "price" not in json.dumps(output) and "raw" not in json.dumps(output)


def test_known_non_candidates_unknown_invalid_and_duplicates_are_separate():
    rows = [
        row(),
        row("BTC_USDT", makerFeeRate="0.0001"),
        row("ETH_USDT", isZeroFeeSymbol=False),
        row("LINK_USDT", apiAllowed=False),
        row("DOGE_USDT", state=1),
        row("XRP_USDT", futureType=2),
        row("SOL_USDT", quoteCoin="USD"),
        row("LTC_USDT", settleCoin="USD"),
        row("ADA_USDT", isZeroFeeRate=None),
        row("DOT_USDT", feeRateType="OTHER"),
        row("ATOM_USDT", makerFeeRate="NaN"),
        row("NEAR_USDT", apiAllowed="true"),
        row("BAD/USDT"),
        row("AAVE_USDT"),
        row("AAVE_USDT"),
    ]
    result = parse_public_zero_fee_catalog(payload(rows), STAMP)
    assert result.total_rows == 15
    assert [item.symbol for item in result.candidates] == ["ARB_USDT"]
    assert result.non_candidate_rows == 7
    assert result.unknown_rows == 2
    assert result.invalid_rows == 5


@pytest.mark.parametrize(
    "content",
    [
        b"bad-json",
        b'{"success":false,"code":0,"data":[]}',
        b'{"success":true,"code":true,"data":[]}',
        b'{"success":true,"code":0,"data":{}}',
        b'{"success":true,"success":true,"code":0,"data":[]}',
    ],
)
def test_bad_catalog_envelope_fails_closed(content):
    with pytest.raises(CatalogError):
        parse_public_zero_fee_catalog(content, STAMP)


@pytest.mark.asyncio
async def test_one_public_get_without_query_auth_or_retry():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=payload([row()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch_public_zero_fee_catalog(client)
    assert len(requests) == 1
    assert requests[0].method == "GET" and str(requests[0].url) == CATALOG_URL
    assert "ApiKey" not in requests[0].headers
    assert len(result.candidates) == 1


@pytest.mark.asyncio
async def test_response_cap_and_http_error_fail_without_retry():
    for status, content in ((200, b"x" * (MAX_CATALOG_BYTES + 1)), (503, b"unavailable")):
        calls = []

        def respond(request, *, _calls=calls, _status=status, _content=content):
            _calls.append(request)
            return httpx.Response(_status, content=_content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(CatalogError):
                await fetch_public_zero_fee_catalog(client)
        assert len(calls) == 1


def test_parser_cap_and_timezone_guard():
    with pytest.raises(CatalogError):
        parse_public_zero_fee_catalog(b"x" * (MAX_CATALOG_BYTES + 1), STAMP)
    with pytest.raises(CatalogError):
        parse_public_zero_fee_catalog(payload([]), datetime(2026, 10, 20))
