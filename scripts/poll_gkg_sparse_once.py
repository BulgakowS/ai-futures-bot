"""One explicit sparse GKG v3 research attempt; no scheduler or active database."""

import argparse
import asyncio
import json
import os
import secrets
from datetime import UTC, datetime

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_sparse import REAL_JOURNAL_PATH, SparseGkgCollector


def utc_anchor(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("anchor must be an ISO 8601 UTC timestamp") from exc


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", required=True, type=utc_anchor)
    args = parser.parse_args()
    if os.environ.get("APP_ENV") != "test" or os.environ.get("GKG_SPARSE_ENABLED") != "true":
        raise PermissionError("APP_ENV=test and GKG_SPARSE_ENABLED=true are required")
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_mode="paper",
        live_trading_enabled=False,
        ai_enabled=False,
        gkg_forward_enabled=False,
        gdelt_forward_enabled=False,
        gkg_sparse_enabled=True,
        database_url=f"sqlite+aiosqlite:///{REAL_JOURNAL_PATH}",
        backend_auth_token=secrets.token_urlsafe(48),
    )
    database = Database(settings)
    try:
        result = await SparseGkgCollector(database, settings).collect_once(args.anchor)
        print(
            json.dumps(
                {
                    "status": result.status,
                    "anchor_at": result.anchor_at.isoformat(),
                    "target_batch_at": result.target_batch_at.isoformat(),
                    "attempt_id": result.attempt_id,
                    "error_code": result.error_code,
                    "response_bytes": result.response_bytes,
                },
                sort_keys=True,
            )
        )
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
