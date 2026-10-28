import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from ai_futures.mexc_private_readonly import (
    FEE_URL,
    MexcPrivateFeeReadOnly,
    PrivateFeeError,
    parse_account_fee,
    signed_fee_headers,
)

STAMP = datetime(2026, 10, 20, tzinfo=UTC)


def fee_body(**changes):
    data = {
        "symbol": "BTC_USDT",
        "feeRateMode": "NORMAL",
        "realMakerFee": "0.0001",
        "realTakerFee": "0.0004",
    } | changes
    return json.dumps({"success": True, "code": 0, "data": data}).encode()


def test_official_futures_get_signature_vector():
    headers = signed_fee_headers("access", "secret", "BTC_USDT", 1792480000000)
    assert headers["Request-Time"] == "1792480000000"
    assert headers["Signature"] == (
        "1edd9e3626c567661ff98abfa9b6ffd13a7e10a8584216d73869e49543c733c5"
    )
    assert signed_fee_headers("access", "secret", "BTC_USDT", 1792480000000) == headers
    for symbol in ("BTC/USDT", "btc_usdt", "BTC_USDT&x=1", "", "A_USDT"):
        with pytest.raises(PrivateFeeError):
            signed_fee_headers("access", "secret", symbol, 1792480000000)


@pytest.mark.asyncio
async def test_default_off_and_single_signed_get():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=fee_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = MexcPrivateFeeReadOnly(
            client, "access", "secret", timestamp_ms=lambda: 1792480000000
        )
        with pytest.raises(PrivateFeeError, match="disabled"):
            await reader.account_fee("BTC_USDT")
        reader._enabled = "yes"
        with pytest.raises(PrivateFeeError, match="disabled"):
            await reader.account_fee("BTC_USDT")
        assert not requests
        reader = MexcPrivateFeeReadOnly(
            client, "access", "secret", enabled=True, timestamp_ms=lambda: 1792480000000
        )
        fee = await reader.account_fee("BTC_USDT")

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET" and str(request.url) == f"{FEE_URL}?symbol=BTC_USDT"
    assert request.headers["ApiKey"] == "access"
    assert (
        request.headers["Signature"]
        == signed_fee_headers("access", "secret", "BTC_USDT", 1792480000000)["Signature"]
    )
    assert fee.symbol == "BTC_USDT"
    assert fee.maker_rate == Decimal("0.0001")
    assert fee.taker_rate == Decimal("0.0004")
    assert fee.observed_at.tzinfo == UTC
    assert fee.source == "private_account_fee" and fee.promotion_eligible is False


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETH_USDT"},
        {"feeRateMode": "TIERED"},
        {"feeRateMode": "LEVERAGE"},
        {"feeRateMode": "UNKNOWN"},
        {"realMakerFee": "NaN"},
        {"realMakerFee": "-0.1"},
        {"realTakerFee": 1},
        {"realTakerFee": True},
        {"tieredFeeRates": [{"realTakerFee": 0}]},
        {"leverageFeeRates": [{"realTakerFee": 0}]},
    ],
)
def test_malformed_or_unsupported_fee_data_fails_closed(changes):
    with pytest.raises(PrivateFeeError):
        parse_account_fee(fee_body(**changes), "BTC_USDT", STAMP)


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b'{"success":true,"code":0,"data":{}}',
        b'{"success":false,"code":0,"data":{}}',
        b'{"success":true,"code":true,"data":{}}',
        b'{"success":true,"code":0,"data":{"symbol":"BTC_USDT","symbol":"BTC_USDT"}}',
        b'{"success":true,"code":0,"data":[]}',
    ],
)
def test_response_envelope_rejected(content):
    with pytest.raises(PrivateFeeError):
        parse_account_fee(content, "BTC_USDT", STAMP)


def test_public_zero_flag_has_no_authority_over_account_fee():
    public_rules = {
        "symbol": "BTC_USDT",
        "isZeroFeeSymbol": True,
        "isZeroFeeRate": True,
        "makerFeeRate": 0,
        "takerFeeRate": 0,
    }
    fee = parse_account_fee(fee_body(), public_rules["symbol"], STAMP)
    assert fee.taker_rate == Decimal("0.0004")
    assert fee.promotion_eligible is False


@pytest.mark.asyncio
async def test_error_is_sanitized_and_not_retried():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, text="remote-secret-like-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = MexcPrivateFeeReadOnly(client, "access", "very-private-secret", enabled=True)
        with pytest.raises(PrivateFeeError) as caught:
            await reader.account_fee("BTC_USDT")
    assert len(requests) == 1
    assert "very-private-secret" not in str(caught.value)
    assert "remote-secret-like-body" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_sanitized_and_not_retried():
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-transport-detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        reader = MexcPrivateFeeReadOnly(client, "access", "secret", enabled=True)
        with pytest.raises(PrivateFeeError) as caught:
            await reader.account_fee("BTC_USDT")
    assert len(requests) == 1
    assert "private-transport-detail" not in str(caught.value)
