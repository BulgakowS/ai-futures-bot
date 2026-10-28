from collections.abc import AsyncIterator
from datetime import UTC

from sqlalchemy import DateTime, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from ai_futures.config import Settings
from ai_futures.schemas import require_utc


class UTCDateTimeType(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return require_utc(value) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)  # SQLite test driver drops timezone metadata.
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
