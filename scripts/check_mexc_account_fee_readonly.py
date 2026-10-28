"""Manual, default-off read-only MEXC account fee check; never places orders."""

import argparse
import asyncio
import json
import os

import httpx
from ai_futures.mexc_private_readonly import MexcPrivateFeeReadOnly, PrivateFeeError


async def check(symbol: str) -> None:
    if os.getenv("MEXC_FEE_READ_ENABLED") != "1":
        raise PrivateFeeError("Private fee read disabled")
    api_key = os.getenv("MEXC_FUTURES_API_KEY")
    secret = os.getenv("MEXC_FUTURES_API_SECRET")
    if not api_key or not secret:
        raise PrivateFeeError("Missing local credentials")
    async with httpx.AsyncClient(trust_env=False) as client:
        fee = await MexcPrivateFeeReadOnly(client, api_key, secret, enabled=True).account_fee(
            symbol
        )
    print(
        json.dumps(
            {
                "symbol": fee.symbol,
                "maker_rate": str(fee.maker_rate),
                "taker_rate": str(fee.taker_rate),
                "fee_rate_mode": fee.fee_rate_mode,
                "source": fee.source,
                "observed_at": fee.observed_at.isoformat(),
                "promotion_eligible": fee.promotion_eligible,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help="Exact MEXC contract symbol, e.g. BTC_USDT")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.symbol))
    except PrivateFeeError:
        parser.exit(1, "Read-only fee check failed or is disabled.\n")
