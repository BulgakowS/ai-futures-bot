"""Versioned historical input and isolated, immutable backtest reports."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import utc_now


class BacktestDataset(Base):
    __tablename__ = "backtest_dataset"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class BacktestRun(Base):
    __tablename__ = "backtest_run"
    __table_args__ = (CheckConstraint("mode = 'backtest'", name="backtest_only"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("backtest_dataset.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="backtest")
    report: Mapped[dict] = mapped_column(JSON)
    report_checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
