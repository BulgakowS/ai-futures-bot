"""Append-only budget claims and numeric results for sparse GKG v3."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType


class SparseGkgBudget(Base):
    __tablename__ = "sparse_gkg_budget"
    __table_args__ = (CheckConstraint("id = 1", name="sparse_gkg_budget_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cap_bytes: Mapped[int] = mapped_column(BigInteger)
    prior_bytes: Mapped[int] = mapped_column(Integer)
    budget_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType)


class SparseGkgAttempt(Base):
    __tablename__ = "sparse_gkg_attempt"
    __table_args__ = (
        UniqueConstraint("anchor_at", name="uq_sparse_gkg_anchor"),
        CheckConstraint("reserved_bytes > 0", name="sparse_gkg_positive_reservation"),
    )

    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    anchor_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    target_batch_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    reserved_bytes: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))


class SparseGkgResult(Base):
    __tablename__ = "sparse_gkg_result"
    __table_args__ = (
        CheckConstraint("status IN ('ok','error')", name="sparse_gkg_status"),
        CheckConstraint("total_rows IS NULL OR total_rows > 0", name="sparse_gkg_rows"),
    )

    result_id: Mapped[UUID] = mapped_column(primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_bytes: Mapped[int] = mapped_column(Integer)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    btc_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mean_btc_document_tone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
