import asyncio

from ai_futures import exchange_credentials, models  # noqa: F401
from ai_futures.config import get_settings
from ai_futures.db import Base, Database

from alembic import context


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    database = Database(get_settings())
    async with database.engine.connect() as connection:
        await connection.run_sync(migrate)
    await database.close()


if context.is_offline_mode():
    raise RuntimeError("Use online migrations against the configured database")
asyncio.run(online())
