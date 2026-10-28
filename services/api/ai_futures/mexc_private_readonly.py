"""Default-off, single-endpoint MEXC Futures account fee reader."""

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ai_futures.schemas import require_decimal

FEE_URL = "https://api.mexc.com/api/v1/private/account/tiered_fee_rate/v2"
MAX_RESPONSE_BYTES = 65_536
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,20}_[A-Z0-9]{2,20}\Z")


class PrivateFeeError(ValueError):
    """A sanitized failure; remote response bodies and credentials are never attached."""


def _valid_symbol(symbol: object) -> bool:
    return isinstance(symbol, str) and SYMBOL_PATTERN.fullmatch(symbol) is not None


@dataclass(frozen=True)
class AccountFeeBaseline:
    symbol: str
    maker_rate: Decimal
    taker_rate: Decimal
    observed_at: datetime
    source: str = "private_account_fee"
    fee_rate_mode: str = "NORMAL"
    promotion_eligible: bool = False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrivateFeeError("Invalid account fee response")
        result[key] = value
    return result


def _reject_constant(_value):
    raise PrivateFeeError("Invalid account fee response")


def _rate(value):
    try:
        result = require_decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        raise PrivateFeeError("Invalid account fee response") from None
    if not 0 <= result < 1:
        raise PrivateFeeError("Invalid account fee response")
    return result


def parse_account_fee(content: bytes, symbol: str, observed_at: datetime) -> AccountFeeBaseline:
    """Accept only an exact-symbol NORMAL account rate, never a promotion claim."""
    if not _valid_symbol(symbol) or len(content) > MAX_RESPONSE_BYTES:
        raise PrivateFeeError("Invalid account fee response")
    try:
        body = json.loads(
            content,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PrivateFeeError("Invalid account fee response") from None
    if (
        not isinstance(body, dict)
        or body.get("success") is not True
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(body.get("data"), dict)
    ):
        raise PrivateFeeError("Invalid account fee response")
    data = body["data"]
    if data.get("symbol") != symbol:
        raise PrivateFeeError("Invalid account fee response")
    if data.get("feeRateMode") != "NORMAL":
        raise PrivateFeeError("Unsupported account fee mode")
    if data.get("tieredFeeRates", []) or data.get("leverageFeeRates", []):
        raise PrivateFeeError("Unsupported account fee mode")
    try:
        maker = _rate(data["realMakerFee"])
        taker = _rate(data["realTakerFee"])
    except KeyError:
        raise PrivateFeeError("Invalid account fee response") from None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise PrivateFeeError("Invalid observation time")
    return AccountFeeBaseline(symbol, maker, taker, observed_at.astimezone(UTC))


def signed_fee_headers(api_key: str, secret: str, symbol: str, timestamp_ms: int) -> dict[str, str]:
    """Sign only the ASCII symbol query defined by the official Futures demo."""
    if (
        not _valid_symbol(symbol)
        or not api_key
        or api_key != api_key.strip()
        or not secret
        or type(timestamp_ms) is not int
        or timestamp_ms <= 0
    ):
        raise PrivateFeeError("Invalid fee request")
    if any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise PrivateFeeError("Invalid fee request")
    target = f"{api_key}{timestamp_ms}symbol={symbol}"
    signature = hmac.new(secret.encode("utf-8"), target.encode("utf-8"), hashlib.sha256)
    return {
        "ApiKey": api_key,
        "Request-Time": str(timestamp_ms),
        "Signature": signature.hexdigest(),
        "Accept": "application/json",
    }


class MexcPrivateFeeReadOnly:
    """One signed GET; callers must opt in and supply an httpx client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        secret: str,
        *,
        enabled: bool = False,
        timestamp_ms: Callable[[], int] | None = None,
    ):
        self._client = client
        self._api_key = api_key
        self._secret = secret
        self._enabled = enabled
        self._timestamp_ms = timestamp_ms or (lambda: time.time_ns() // 1_000_000)

    async def account_fee(self, symbol: str) -> AccountFeeBaseline:
        if self._enabled is not True:
            raise PrivateFeeError("Private fee read disabled")
        headers = signed_fee_headers(self._api_key, self._secret, symbol, self._timestamp_ms())
        try:
            async with self._client.stream(
                "GET",
                FEE_URL,
                params={"symbol": symbol},
                headers=headers,
                timeout=5.0,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise PrivateFeeError("MEXC fee read failed")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise PrivateFeeError("MEXC fee response too large")
        except (httpx.HTTPError, OSError):
            raise PrivateFeeError("MEXC fee read failed") from None
        return parse_account_fee(bytes(content), symbol, datetime.now(UTC))
