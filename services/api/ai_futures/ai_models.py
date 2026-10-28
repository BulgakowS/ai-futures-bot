"""Durable, paper-scoped AI analysis and usage records."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType
from ai_futures.paper_models import Money
from ai_futures.schemas import utc_now


class AiAnalysis(Base):
    __tablename__ = "ai_analysis"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(8), default="WAIT")
    reason: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON)
    context_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    paper_request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    position_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    intent_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    risk_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    config_version: Mapped[str] = mapped_column(String(128))


class AiUsage(Base):
    __tablename__ = "ai_usage"

    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("ai_analysis.id"), primary_key=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    estimated_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
