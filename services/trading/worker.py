import asyncio
import logging
import signal
import time
from uuid import uuid4

from ai_futures.bots import BotService
from ai_futures.config import get_settings
from ai_futures.db import Database
from ai_futures.market_worker import MarketIngestion
from ai_futures.mexc import MexcPublic
from ai_futures.models import WorkerHeartbeat
from ai_futures.paper import PaperService
from ai_futures.schemas import utc_now
from ai_futures.worker_health import components_healthy, ingestion_healthy
from redis.asyncio import Redis

logger = logging.getLogger("ai_futures.trading")


async def run_worker(stop: asyncio.Event) -> None:
    settings = get_settings()
    database = Database(settings)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=3, socket_timeout=3
    )
    instance_id = uuid4()
    adapter = MexcPublic()
    market = MarketIngestion(database, settings, adapter)
    ingestion = asyncio.create_task(market.run(stop))
    last_success = {"paper": None, "bot": None}

    async def paper_loop():
        manager = PaperService(database, settings.config_version)
        while not stop.is_set():
            try:
                await manager.tick()
                last_success["paper"] = time.monotonic()
            except Exception:
                logger.exception("paper_manager_tick_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass

    paper_task = asyncio.create_task(paper_loop())

    async def bot_loop():
        manager = BotService(database, settings.config_version)
        while not stop.is_set():
            try:
                await manager.tick()
                last_success["bot"] = time.monotonic()
            except Exception:
                logger.exception("bot_manager_tick_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass

    bot_task = asyncio.create_task(bot_loop())
    try:
        while not stop.is_set():
            try:
                async with asyncio.timeout(5):
                    await redis.ping()
                    status = (
                        "idle"
                        if components_healthy(
                            (ingestion, paper_task, bot_task),
                            last_success,
                            time.monotonic(),
                            settings.worker_stale_seconds,
                        )
                        and ingestion_healthy(
                            market.last_ticker, utc_now(), settings.worker_stale_seconds
                        )
                        else "degraded"
                    )
                    async with database.sessions.begin() as session:
                        await session.merge(
                            WorkerHeartbeat(
                                name="trading",
                                instance_id=instance_id,
                                status=status,
                                occurred_at=utc_now(),
                            )
                        )
            except Exception:
                logger.warning("worker_dependencies_unavailable")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_heartbeat_seconds)
            except TimeoutError:
                pass
    finally:
        ingestion.cancel()
        paper_task.cancel()
        bot_task.cancel()
        await asyncio.gather(ingestion, paper_task, bot_task, return_exceptions=True)
        await adapter.close()
        await redis.aclose()
        await database.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await run_worker(stop)


if __name__ == "__main__":
    asyncio.run(main())
