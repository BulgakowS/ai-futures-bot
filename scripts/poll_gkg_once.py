"""Manually run one explicitly enabled GKG candidate scan."""

import asyncio
from dataclasses import asdict

from ai_futures.config import get_settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_forward import GkgForwardPoller


async def main():
    settings = get_settings()
    database = Database(settings)
    try:
        print(asdict(await GkgForwardPoller(database, settings).poll_once()))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
