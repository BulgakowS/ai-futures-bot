"""Bounded read-only MEXC public probe; sanitized evidence, no private keys."""

import asyncio
import json
from pathlib import Path

from ai_futures.mexc import WS_URL, MexcPublic, normalize_ticker, parse_json, strings
from ai_futures.schemas import utc_now
from websockets.asyncio.client import connect


async def main():
    adapter = MexcPublic()
    evidence = {"checked_at": utc_now().isoformat(), "private_keys": False}
    try:
        server = await adapter.server_time()
        rules = await adapter.contract_rules("BTC_USDT")
        ticker, timestamp = await adapter.ticker("BTC_USDT")
        evidence["rules"] = {
            key: rules[key]
            for key in ("version", "contractSize", "priceUnit", "volUnit", "takerFeeRate")
        }
        evidence["ticker"] = {"source_time": timestamp.isoformat(), **ticker}
        evidence["candles"] = {}
        for timeframe in ("1m", "5m", "15m"):
            rows = await adapter.candles(
                "BTC_USDT", timeframe, server, int(server.timestamp()) - 3600
            )
            evidence["candles"][timeframe] = {
                "count": len(rows),
                "final_count": sum(row[2] for row in rows),
            }
        funding, timestamp = await adapter.funding_current("BTC_USDT")
        evidence["funding"] = {"source_time": timestamp.isoformat(), **funding}
        evidence["history_count"] = len(await adapter.funding_history("BTC_USDT"))
        depth, timestamp = await adapter.depth("BTC_USDT")
        evidence["depth"] = {
            "source_time": timestamp.isoformat(),
            "version": depth["version"],
            "asks": depth["asks"][:1],
            "bids": depth["bids"][:1],
        }
        channels = set()
        async with connect(WS_URL, ping_interval=None, open_timeout=10) as ws:
            for payload in (
                {"method": "ping"},
                {"method": "sub.ticker", "param": {"symbol": "BTC_USDT"}},
                {
                    "method": "sub.kline",
                    "param": {"symbol": "BTC_USDT", "interval": "Min1"},
                    "gzip": False,
                },
            ):
                await ws.send(json.dumps(payload))
            async with asyncio.timeout(15):
                while not {"pong", "push.ticker", "push.kline"}.issubset(channels):
                    message = parse_json(await ws.recv())
                    channels.add(message["channel"])
                    if message["channel"] == "push.ticker":
                        _, timestamp = normalize_ticker(message["data"], "BTC_USDT")
                        evidence["ws_source_time"] = timestamp.isoformat()
        evidence["ws_channels"] = sorted(channels)
        path = Path("docs/evidence/market-live-2026-10-14.json")
        path.write_text(json.dumps(strings(evidence), indent=2) + "\n")
        print(f"PASS public REST/WS; evidence={path}")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
