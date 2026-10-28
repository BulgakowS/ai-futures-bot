from uuid import uuid4

from ai_futures.db import Database
from ai_futures.models import AppState, AuditEvent, OutboxEvent


async def seed_state(database: Database, config_version: str) -> None:
    async with database.sessions.begin() as session:
        if await session.get(AppState, 1) is not None:
            return
        state = AppState(config_version=config_version)
        session.add(state)
        await session.flush()
        correlation_id = uuid4()
        payload = {"instance_id": str(state.instance_id), "mode": state.mode}
        session.add(
            AuditEvent(
                correlation_id=correlation_id,
                config_version=config_version,
                actor="bootstrap",
                action="app.initialized",
                payload=payload,
            )
        )
        session.add(
            OutboxEvent(
                correlation_id=correlation_id,
                config_version=config_version,
                topic="app.initialized",
                payload=payload,
            )
        )
