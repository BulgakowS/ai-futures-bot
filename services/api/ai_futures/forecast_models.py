"""Append-only offline forecast and candidate proposal journals."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType


class ForecastDecision(Base):
    __tablename__ = "news_forecast"
    __table_args__ = (UniqueConstraint("symbol", "anchor", name="uq_news_forecast_window"),)

    forecast_id: Mapped[UUID] = mapped_column(primary_key=True)
    correlation_id: Mapped[UUID] = mapped_column(unique=True)
    symbol: Mapped[str] = mapped_column(String(64))
    anchor: Mapped[datetime] = mapped_column(UTCDateTimeType)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    quality: Mapped[str] = mapped_column(String(16))
    probabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_checksum: Mapped[str] = mapped_column(String(64))
    source_versions: Mapped[list] = mapped_column(JSON)
    market_event_ids: Mapped[list] = mapped_column(JSON)
    news_version_ids: Mapped[list] = mapped_column(JSON)
    feature_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    decision_hash: Mapped[str] = mapped_column(String(64))
    approval_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType)


class ForecastProposal(Base):
    __tablename__ = "news_forecast_proposal"

    proposal_id: Mapped[UUID] = mapped_column(primary_key=True)
    forecast_id: Mapped[UUID] = mapped_column(
        ForeignKey("news_forecast.forecast_id", ondelete="RESTRICT"), unique=True
    )
    direction: Mapped[str] = mapped_column(String(8))
    entry: Mapped[str] = mapped_column(String(96))
    stop_loss: Mapped[str] = mapped_column(String(96))
    take_profit: Mapped[str] = mapped_column(String(96))
    rules_version: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str] = mapped_column(String(128))
    proposal_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
