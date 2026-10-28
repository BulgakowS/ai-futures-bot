"""One explicit public MEXC Hour4/rules read into the isolated S11 research SQLite."""

import argparse
import asyncio
import json
import os
import secrets

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_sparse import REAL_JOURNAL_PATH
from ai_futures.sparse_market_research import collect_market_once

from scripts.poll_gkg_sparse_once import utc_anchor


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", required=True, type=utc_anchor)
    args = parser.parse_args()
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("SPARSE_MARKET_RESEARCH_ENABLED") != "true"
    ):
        raise PermissionError("APP_ENV=test and SPARSE_MARKET_RESEARCH_ENABLED=true required")
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_mode="paper",
        live_trading_enabled=False,
        ai_enabled=False,
        market_candle_journal_enabled=True,
        instrument_rules_journal_enabled=True,
        database_url=f"sqlite+aiosqlite:///{REAL_JOURNAL_PATH}",
        backend_auth_token=secrets.token_urlsafe(48),
    )
    database = Database(settings)
    try:
        result = await collect_market_once(database, settings, args.anchor)
        print(
            json.dumps(
                {
                    "anchor_at": result.anchor_at.isoformat(),
                    "rules_version": result.rules_version,
                    "final_windows": [value.isoformat() for value in result.final_windows],
                    "observed_at": result.observed_at.isoformat(),
                },
                sort_keys=True,
            )
        )
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
