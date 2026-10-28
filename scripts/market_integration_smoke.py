"""Run inside API container against persisted worker market state."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5) as client:
        async with asyncio.timeout(60):
            while True:
                ticker = (await client.get("/api/v1/markets/BTC_USDT/ticker")).json()
                if ticker.get("quality", {}).get("trade_eligible"):
                    break
                await asyncio.sleep(1)
        markets = (await client.get("/api/v1/markets")).json()
        assert markets[0]["rules"]["contractSize"] == "0.0001"
        assert ticker["source"] == "ws"
        assert datetime.fromisoformat(ticker["source_time"]).tzinfo is not None
        evidence = {
            "checked_at": datetime.now(UTC).isoformat(),
            "ticker": ticker,
            "rules_version": markets[0]["rules_version"],
            "private_keys": False,
        }
        for kind in ("depth", "funding"):
            response = await client.get(f"/api/v1/markets/BTC_USDT/{kind}")
            assert response.status_code == 200
            value = response.json()
            assert value["source"] == "rest"
            assert value["source_time"] != value["received_at"]
            evidence[kind] = {
                key: value[key] for key in ("source_time", "source", "quality_status")
            }
        evidence["candles"] = {}
        for timeframe in ("1m", "5m", "15m"):
            value = (
                await client.get(
                    "/api/v1/markets/BTC_USDT/candles",
                    params={"timeframe": timeframe, "limit": 200},
                )
            ).json()
            assert len(value["candles"]) >= 100
            assert any(row["final"] for row in value["candles"])
            evidence["candles"][timeframe] = {
                "count": len(value["candles"]),
                "final_count": sum(row["final"] for row in value["candles"]),
                "last_source_time": value["candles"][-1]["source_time"],
            }
        health = await client.get("/api/v1/health/ready")
        assert health.status_code == 200
        evidence["health"] = health.json()
        path = Path("/tmp/market-integration.json")
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
