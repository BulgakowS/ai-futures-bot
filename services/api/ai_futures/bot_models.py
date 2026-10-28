"""Persisted paper bot configuration and decision journal."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import utc_now


class Bot(Base):
    __tablename__ = "bot"
    __table_args__ = (
        CheckConstraint("mode = 'paper'", name="paper_only"),
        UniqueConstraint("portfolio_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"))
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    active_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
    last_window: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class BotConfig(Base):
    __tablename__ = "bot_config"
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bot.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class BotDecision(Base):
    __tablename__ = "bot_decision"
    __table_args__ = (UniqueConstraint("bot_id", "timeframe", "window_start"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bot.id"), index=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"))
    strategy_version: Mapped[int] = mapped_column(Integer)
    timeframe: Mapped[str] = mapped_column(String(8))
    window_start: Mapped[datetime] = mapped_column(UTCDateTimeType)
    candle_event_id: Mapped[UUID] = mapped_column(Uuid)
    candle_hash: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(64))
    risk_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    intent_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    risk_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    config_version: Mapped[str] = mapped_column(String(128))
