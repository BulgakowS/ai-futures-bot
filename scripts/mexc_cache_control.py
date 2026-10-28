"""One bounded read-only MEXC rules/candle control against the active API cache."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from ai_futures.mexc import REST_BASE, MexcPublic, payload_hash
from ai_futures.schemas import utc_now

SYMBOL = "BTC_USDT"
PUBLIC_GET_LIMIT = 5
RULE_FIELDS = (
    "contractSize",
    "priceUnit",
    "volUnit",
    "minVol",
    "state",
    "makerFeeRate",
    "takerFeeRate",
    "feeRateMode",
    "feeRateType",
    "riskLimitMode",
    "riskLimitType",
)
CANDLE_FIELDS = ("open", "close", "high", "low", "vol", "amount")


async def local_json(client, path):
    response = await client.get(f"/api/backend/{path}")
    response.raise_for_status()
    return response.json()


async def run():
    checked_at = utc_now()
    evidence = {
        "checked_at": checked_at.isoformat(),
        "scope": "read_only_public_mexc_vs_active_api_cache",
        "official_docs_checked_at": "2026-10-17T07:57:11Z",
        "official_docs": [
            "https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info",
            "https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data",
        ],
        "public_get_limit": PUBLIC_GET_LIMIT,
        "public_get_attempts": 0,
        "local_api_gets": 0,
        "private_api_requests": 0,
        "post_requests": 0,
        "paper_mutations": 0,
    }
    public_client = None

    async def count_public(request):
        assert request.method == "GET" and request.url.host == "api.mexc.com"
        if evidence["public_get_attempts"] >= PUBLIC_GET_LIMIT:
            raise RuntimeError("Public GET bound exceeded")
        evidence["public_get_attempts"] += 1

    try:
        async with asyncio.timeout(45):
            public_client = httpx.AsyncClient(
                base_url=REST_BASE, timeout=10, event_hooks={"request": [count_public]}
            )
            adapter = MexcPublic(public_client)
            server_time = await adapter.server_time()
            evidence["mexc_server_time"] = server_time.isoformat()
            async with httpx.AsyncClient(base_url="http://127.0.0.1:3030", timeout=8) as local:
                markets = await local_json(local, "markets")
                evidence["local_api_gets"] += 1
                ticker = await local_json(local, f"markets/{SYMBOL}/ticker")
                evidence["local_api_gets"] += 1
                candles = await local_json(local, f"markets/{SYMBOL}/candles?timeframe=1m&limit=20")
                evidence["local_api_gets"] += 1

            matches = [row for row in markets if row.get("symbol") == SYMBOL]
            if len(matches) != 1:
                raise ValueError("Active cache has missing or ambiguous BTC_USDT rules")
            cached = matches[0]
            direct_rules = await adapter.contract_rules(SYMBOL)
            cached_rules = cached["rules"]
            field_matches = {
                key: cached_rules.get(key) == direct_rules.get(key) for key in RULE_FIELDS
            }
            evidence["rules"] = {
                "cached_version": cached["rules_version"],
                "direct_version": direct_rules["version"],
                "cached_received_at": cached["received_at"],
                "cached_quality": cached["quality"],
                "field_matches": field_matches,
                "cached_public_fields": {key: cached_rules.get(key) for key in RULE_FIELDS},
                "direct_public_fields": {key: direct_rules.get(key) for key in RULE_FIELDS},
                "cached_risk_tiers_hash": payload_hash(cached_rules.get("riskLimitCustom")),
                "direct_risk_tiers_hash": payload_hash(direct_rules.get("riskLimitCustom")),
            }

            final_candidates = [
                row
                for row in candles.get("candles", [])
                if row.get("final") is True
                and datetime.fromisoformat(row["window_start"]) + timedelta(minutes=2)
                <= server_time
            ]
            if not final_candidates:
                raise ValueError("No sufficiently old final 1m candle in active cache")
            cached_candle = final_candidates[-1]
            start = datetime.fromisoformat(cached_candle["window_start"])
            direct_rows = await adapter.candles(
                SYMBOL, "1m", server_time, int(start.timestamp()), int(start.timestamp()) + 60
            )
            exact = [row for row in direct_rows if row[0] == start and row[2]]
            if len(exact) != 1:
                raise ValueError("Direct MEXC final 1m candle missing for cached UTC window")
            direct_candle = exact[0][1]
            candle_matches = {
                key: Decimal(str(cached_candle[key])) == Decimal(direct_candle[key])
                for key in CANDLE_FIELDS
            }
            evidence["candle"] = {
                "window_start": start.isoformat(),
                "cached_final": cached_candle["final"],
                "direct_final_by_server_time": exact[0][2],
                "cached_source": cached_candle["source"],
                "cached_source_time": cached_candle["source_time"],
                "cached_payload": {key: cached_candle[key] for key in CANDLE_FIELDS},
                "direct_payload": {key: direct_candle[key] for key in CANDLE_FIELDS},
                "field_matches": candle_matches,
                "cached_payload_hash": payload_hash(
                    {key: cached_candle[key] for key in CANDLE_FIELDS}
                ),
                "direct_payload_hash": payload_hash(direct_candle),
            }

            direct_ticker, direct_time = await adapter.ticker(SYMBOL)
            del direct_ticker  # A moving market price is not compared across request times.
            now = utc_now()
            cache_time = datetime.fromisoformat(ticker["source_time"])
            evidence["ticker"] = {
                "cached_source": ticker["source"],
                "cached_source_time": cache_time.isoformat(),
                "cached_source_age_seconds_at_check": round((now - cache_time).total_seconds(), 3),
                "cached_quality_status": ticker["quality_status"],
                "direct_source_time": direct_time.isoformat(),
                "direct_source_age_seconds_at_check": round((now - direct_time).total_seconds(), 3),
                "price_compared": False,
            }
            rules_equal = (
                cached["rules_version"] == direct_rules["version"]
                and all(field_matches.values())
                and evidence["rules"]["cached_risk_tiers_hash"]
                == evidence["rules"]["direct_risk_tiers_hash"]
            )
            candle_equal = cached_candle["source"] == "rest" and all(candle_matches.values())
            evidence["result"] = (
                "pass_point_in_time" if rules_equal and candle_equal else "blocked_cache_mismatch"
            )
    except Exception as exc:
        evidence["result"] = "blocked_probe_error"
        evidence["error_type"] = type(exc).__name__
        evidence["error_message"] = str(exc)[:160]
    finally:
        if public_client is not None:
            await public_client.aclose()
        evidence["finished_at"] = utc_now().isoformat()

    path = Path("docs/evidence/mexc-cache-control-2026-10-17.json")
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"{evidence['result'].upper()} evidence={path}")
    return 0 if evidence["result"] == "pass_point_in_time" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
