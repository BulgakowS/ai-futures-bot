"""Manually run one explicitly enabled, bounded GDELT aggregate poll."""

import asyncio
from dataclasses import asdict

from ai_futures.config import get_settings
from ai_futures.db import Database
from ai_futures.gdelt_forward import GdeltForwardPoller


async def main():
    settings = get_settings()
    database = Database(settings)
    try:
        result = await GdeltForwardPoller(database, settings).poll_once()
        print(asdict(result))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
