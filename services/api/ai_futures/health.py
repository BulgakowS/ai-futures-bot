import asyncio
from pathlib import Path

from sqlalchemy import text

from ai_futures.models import AppState, WorkerHeartbeat
from ai_futures.schemas import utc_now
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory


def expected_revisions() -> set[str]:
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    return set(ScriptDirectory.from_config(config).get_heads())


def current_revisions(connection) -> set[str]:
    return set(MigrationContext.configure(connection).get_current_heads())


async def dependencies(database, redis, settings) -> dict[str, str]:
    checks = {
        "database": "unavailable",
        "migrations": "unavailable",
        "redis": "unavailable",
        "trading": "unavailable",
        "state": "unavailable",
    }
    try:
        async with asyncio.timeout(3):
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                checks["database"] = "ok"
                revisions = await connection.run_sync(current_revisions)
                checks["migrations"] = "ok" if revisions == expected_revisions() else "pending"
            async with database.sessions() as session:
                state = await session.get(AppState, 1)
                checks["state"] = "ok" if state is not None else "uninitialized"
                heartbeat = await session.get(WorkerHeartbeat, "trading")
                if heartbeat:
                    age = (utc_now() - heartbeat.occurred_at).total_seconds()
                    checks["trading"] = (
                        "ok"
                        if heartbeat.status == "idle" and 0 <= age < settings.worker_stale_seconds
                        else "stale"
                    )
    except Exception:
        pass  # Public health contains dependency codes, never exception text or connection secrets.
    try:
        async with asyncio.timeout(3):
            checks["redis"] = "ok" if await redis.ping() else "unavailable"
    except Exception:
        pass
    return checks
