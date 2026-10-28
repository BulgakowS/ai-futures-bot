from datetime import timedelta

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.health import dependencies, expected_revisions
from ai_futures.main import create_app
from ai_futures.models import AppState, AuditEvent, OutboxEvent, WorkerHeartbeat
from ai_futures.schemas import utc_now
from ai_futures.state import seed_state
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


class RedisStub:
    def __init__(self, fail=False):
        self.fail = fail

    async def ping(self):
        if self.fail:
            raise ConnectionError("private connection detail")
        return True

    async def aclose(self):
        pass


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )


async def migrate(config):
    import asyncio
    import os

    env = os.environ | {
        "APP_ENV": "test",
        "BACKEND_AUTH_TOKEN": "x" * 48,
        "DATABASE_URL": config.database_url.get_secret_value(),
    }
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/alembic",
        "upgrade",
        "head",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = await process.communicate()
    assert process.returncode == 0, output


async def test_migration_bootstrap_restart_and_constraints(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "v1")
    async with database.sessions() as session:
        first = await session.get(AppState, 1)
        instance_id = first.instance_id
        assert first.kill_switch and first.mode == "paper"
        assert first.created_at.tzinfo is not None
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    await database.close()
    restarted = Database(config)
    await seed_state(restarted, "v2")
    async with restarted.sessions() as session:
        state = await session.get(AppState, 1)
        assert state.instance_id == instance_id and state.config_version == "v1"
        state.mode = "live"
        with pytest.raises(IntegrityError):
            await session.commit()
    await restarted.close()


async def test_readiness_detects_stale_worker_redis_and_migration(config):
    await migrate(config)
    database = Database(config)
    await seed_state(database, "v1")
    checks = await dependencies(database, RedisStub(), config)
    assert checks["database"] == checks["migrations"] == checks["state"] == "ok"
    assert checks["trading"] == "unavailable"
    async with database.sessions.begin() as session:
        state = await session.get(AppState, 1)
        session.add(
            WorkerHeartbeat(
                name="trading", instance_id=state.instance_id, status="idle", occurred_at=utc_now()
            )
        )
    assert all(
        value == "ok" for value in (await dependencies(database, RedisStub(), config)).values()
    )
    async with database.sessions.begin() as session:
        heartbeat = await session.get(WorkerHeartbeat, "trading")
        heartbeat.occurred_at = utc_now() - timedelta(seconds=30)
        await session.execute(text("UPDATE alembic_version SET version_num='old'"))
    checks = await dependencies(database, RedisStub(fail=True), config)
    assert checks["trading"] == "stale" and checks["redis"] == "unavailable"
    assert checks["migrations"] == "pending"
    assert "private" not in str(checks)
    assert expected_revisions()
    await database.close()


async def test_mutations_are_automatically_authenticated(config):
    await migrate(config)
    database = Database(config)
    app = create_app(config, database=database, redis=RedisStub())

    @app.post("/test-command")
    async def command():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 503
        rejected = client.post("/test-command")
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "AUTH_REQUIRED"
        assert rejected.json()["request_id"] == rejected.headers["x-request-id"]
        assert (
            client.post("/test-command", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.post(
                "/test-command", headers={"Authorization": "Bearer " + "x" * 48}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/state").json()["kill_switch"] is True
