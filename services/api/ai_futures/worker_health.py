import asyncio

from ai_futures.config import get_settings
from ai_futures.db import Database
from ai_futures.models import WorkerHeartbeat
from ai_futures.schemas import utc_now


def components_healthy(tasks, last_success, now, stale_seconds):
    return all(not task.done() for task in tasks) and all(
        completed is not None and 0 <= now - completed < stale_seconds
        for completed in last_success.values()
    )


def ingestion_healthy(last_ticker, now, stale_seconds):
    if last_ticker is None:
        return False
    age = (now - last_ticker).total_seconds()
    return 0 <= age < stale_seconds


async def main() -> None:
    settings = get_settings()
    database = Database(settings)
    try:
        async with asyncio.timeout(3), database.sessions() as session:
            heartbeat = await session.get(WorkerHeartbeat, "trading")
            if heartbeat is None or heartbeat.status != "idle":
                raise SystemExit(1)
            age = (utc_now() - heartbeat.occurred_at).total_seconds()
            if not 0 <= age < settings.worker_stale_seconds:
                raise SystemExit(1)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
