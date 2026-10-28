"""MEXC public market contracts. All quantities are contracts, never base units."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ai_futures.schemas import require_decimal

INTERVALS = {"1m": ("Min1", 60), "5m": ("Min5", 300), "15m": ("Min15", 900)}
REST_INTERVALS = INTERVALS | {"4h": ("Hour4", 14400)}
REST_BASE = "https://api.mexc.com"
WS_URL = "wss://contract.mexc.com/edge"


class MarketDataError(ValueError):
    pass


def parse_json(text):
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=lambda _: None)
    except (ValueError, TypeError) as exc:
        raise MarketDataError("Invalid JSON") from exc


def decimal(value, positive=False):
    try:
        result = require_decimal(value)
        if positive and result <= 0:
            raise ValueError("Positive value required")
        return str(result)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise MarketDataError("Invalid decimal") from exc


def epoch(value, milliseconds=True):
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataError("Timestamp must be an integer")
    try:
        # Integer arithmetic avoids rounding a millisecond boundary via binary float.
        seconds, millis = divmod(value, 1000) if milliseconds else (value, 0)
        result = datetime.fromtimestamp(seconds, UTC).replace(microsecond=millis * 1000)
        if result.year < 2020:
            raise ValueError("Invalid timestamp unit")
        return result
    except (ValueError, OverflowError, OSError) as exc:
        raise MarketDataError("Invalid timestamp") from exc


def strings(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [strings(item) for item in value]
    if isinstance(value, dict):
        return {key: strings(item) for key, item in value.items()}
    return value


def payload_hash(payload):
    return hashlib.sha256(json.dumps(strings(payload), sort_keys=True).encode()).hexdigest()


def symbol_object(data, symbol):
    if isinstance(data, list):
        matches = [item for item in data if isinstance(item, dict) and item.get("symbol") == symbol]
        if len(matches) != 1:
            raise MarketDataError("Missing or ambiguous instrument")
        data = matches[0]
    if not isinstance(data, dict) or data.get("symbol") != symbol:
        raise MarketDataError("Unexpected instrument")
    return data


def normalize_rules(data, symbol):
    data = symbol_object(data, symbol)
    result = {"symbol": symbol, "quantity_unit": "contracts", "raw": strings(data)}
    for field in (
        "contractSize",
        "priceUnit",
        "volUnit",
        "minVol",
        "maxVol",
        "minLeverage",
        "maxLeverage",
        "makerFeeRate",
        "takerFeeRate",
        "maintenanceMarginRate",
        "initialMarginRate",
        "liquidationFeeRate",
    ):
        result[field] = decimal(
            data[field],
            positive=field
            in {"contractSize", "priceUnit", "volUnit", "minVol", "maxVol", "maxLeverage"},
        )
    if data.get("futureType") != 1 or data.get("settleCoin") != "USDT":
        raise MarketDataError("Unsupported contract type")
    result["state"] = data["state"]
    result["feeRateMode"] = data.get("feeRateMode")
    result["feeRateType"] = data.get("feeRateType")
    result["tieredFeeRates"] = strings(data.get("tieredFeeRates"))
    result["leverageFeeRates"] = strings(data.get("leverageFeeRates"))
    result["riskLimitMode"] = data.get("riskLimitMode")
    result["riskLimitType"] = data.get("riskLimitType")
    result["riskLimitCustom"] = strings(data.get("riskLimitCustom"))
    result["version"] = payload_hash(data)
    return result


def public_fee_terms_supported(rules):
    """Only the observed public base tariff is usable without private account data."""
    try:
        return (
            rules["feeRateMode"] == "NORMAL"
            and rules["feeRateType"] == "BASE"
            and rules["tieredFeeRates"] == []
            and rules["leverageFeeRates"] == []
            and Decimal(rules["makerFeeRate"]) >= 0
            and Decimal(rules["takerFeeRate"]) >= 0
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def normalize_ticker(data, symbol):
    data = symbol_object(data, symbol)
    result = {
        field: decimal(data[field], positive=field != "fundingRate")
        for field in ("lastPrice", "bid1", "ask1", "fairPrice", "indexPrice", "fundingRate")
    }
    result["holdVol"] = decimal(data["holdVol"])
    if Decimal(result["holdVol"]) < 0 or Decimal(result["bid1"]) > Decimal(result["ask1"]):
        raise MarketDataError("Invalid ticker quantities or spread")
    return result, epoch(data["timestamp"])


def normalize_candles(data, timeframe, server_time):
    fields = ("time", "open", "close", "high", "low", "vol", "amount")
    if not isinstance(data, dict) or any(not isinstance(data.get(f), list) for f in fields):
        raise MarketDataError("Expected candle arrays")
    if len({len(data[f]) for f in fields}) != 1:
        raise MarketDataError("Mismatched candle arrays")
    result = []
    for values in zip(*(data[f] for f in fields), strict=True):
        row = dict(zip(fields, values, strict=True))
        start = epoch(row.pop("time"), False)
        if int(start.timestamp()) % REST_INTERVALS[timeframe][1]:
            raise MarketDataError("Unaligned candle")
        row = {
            key: decimal(value, positive=key in {"open", "close", "high", "low"})
            for key, value in row.items()
        }
        validate_ohlcv(row)
        final = int(start.timestamp()) + REST_INTERVALS[timeframe][1] <= int(
            server_time.timestamp()
        )
        result.append((start, row, final))
    return result


def validate_ohlcv(row):
    o, c, h, low, vol, amount = (
        Decimal(row[f]) for f in ("open", "close", "high", "low", "vol", "amount")
    )
    if low > min(o, c) or h < max(o, c) or h < low or vol < 0 or amount < 0:
        raise MarketDataError("Invalid OHLCV")


def normalize_depth(data):
    if not isinstance(data, dict):
        raise MarketDataError("Expected depth object")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise MarketDataError("Invalid depth version")
    result = {"version": version, "fill_model": "estimated_rest_snapshot"}
    for side in ("asks", "bids"):
        rows = data[side]
        if not isinstance(rows, list) or not rows:
            raise MarketDataError("Empty book")
        normalized = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                raise MarketDataError("Malformed depth tuple")
            normalized.append([decimal(row[0], True), decimal(row[1], True)])
        prices = [Decimal(row[0]) for row in normalized]
        if prices != sorted(prices, reverse=side == "bids"):
            raise MarketDataError("Unsorted book")
        result[side] = normalized
    if Decimal(result["bids"][0][0]) >= Decimal(result["asks"][0][0]):
        raise MarketDataError("Crossed book")
    result["raw"] = strings(data)
    return result, epoch(data["timestamp"])


def normalize_funding(data, symbol):
    data = symbol_object(data, symbol)
    cycle = data["collectCycle"]
    if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle <= 0:
        raise MarketDataError("Invalid funding cycle")
    return {
        "fundingRate": decimal(data["fundingRate"]),
        "collectCycle": cycle,
        "nextSettleTime": epoch(data["nextSettleTime"]).isoformat(),
        "settlement_confirmed": False,
    }, epoch(data["timestamp"])


def normalize_funding_history(data):
    if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
        raise MarketDataError("Invalid funding history")
    return [
        {
            "fundingRate": decimal(row["fundingRate"]),
            "settleTime": epoch(row["settleTime"]).isoformat(),
            "collectCycle": valid_cycle(row["collectCycle"]),
        }
        for row in data["resultList"]
    ]


def valid_cycle(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MarketDataError("Invalid funding cycle")
    return value


def normalize_funding_page(data, page_num, page_size):
    rows = normalize_funding_history(data)
    for key in ("currentPage", "pageSize", "totalCount", "totalPage"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MarketDataError("Invalid funding page metadata")
    if (
        data["currentPage"] != page_num
        or data["pageSize"] != page_size
        or data["totalPage"] != (data["totalCount"] + page_size - 1) // page_size
        or len(rows) > page_size
    ):
        raise MarketDataError("Inconsistent funding page metadata")
    return {"rows": rows, "total_count": data["totalCount"], "page": page_num}


class MexcPublic:
    """One limiter shared by all public calls; no credentials or private methods."""

    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(base_url=REST_BASE, timeout=10)
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def close(self):
        await self.client.aclose()

    async def get(self, path, params=None):
        async with self.lock:
            for attempt in range(3):
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0, self.next_request - loop.time()))
                self.next_request = loop.time() + 0.55
                response = await self.client.get(path, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    retry = response.headers.get("Retry-After", "")
                    delay = min(30, float(retry)) if retry.isdigit() else 2**attempt
                    self.next_request = loop.time() + delay
                    continue
                response.raise_for_status()
                body = parse_json(response.text)
                if (
                    not isinstance(body, dict)
                    or body.get("success") is not True
                    or body.get("code") != 0
                    or "data" not in body
                ):
                    raise MarketDataError("MEXC application error")
                return body["data"]
        raise MarketDataError("MEXC retry limit")

    async def server_time(self):
        return epoch(await self.get("/api/v1/contract/ping"))

    async def contract_rules(self, symbol):
        return normalize_rules(
            await self.get("/api/v1/contract/detail/country", {"symbol": symbol}), symbol
        )

    async def ticker(self, symbol):
        return normalize_ticker(
            await self.get("/api/v1/contract/ticker", {"symbol": symbol}), symbol
        )

    async def candles(self, symbol, timeframe, server_time, start=None, end=None):
        interval = REST_INTERVALS[timeframe][0]
        params = {"interval": interval}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return normalize_candles(
            await self.get(f"/api/v1/contract/kline/{symbol}", params), timeframe, server_time
        )

    async def funding_current(self, symbol):
        return normalize_funding(await self.get(f"/api/v1/contract/funding_rate/{symbol}"), symbol)

    async def funding_history(self, symbol):
        return (await self.funding_history_page(symbol, 1, 20))["rows"]

    async def funding_history_page(self, symbol, page_num, page_size):
        if page_num < 1 or not 1 <= page_size <= 1000:
            raise MarketDataError("Invalid funding page request")
        data = await self.get(
            "/api/v1/contract/funding_rate/history",
            {"symbol": symbol, "page_num": page_num, "page_size": page_size},
        )
        return normalize_funding_page(data, page_num, page_size)

    async def depth(self, symbol):
        return normalize_depth(await self.get(f"/api/v1/contract/depth/{symbol}", {"limit": 5}))
