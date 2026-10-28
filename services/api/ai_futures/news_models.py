from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType


class AnnouncementVersion(Base):
    __tablename__ = "announcement_version"
    __table_args__ = (
        UniqueConstraint("canonical_id", "version_number", name="uq_announcement_version_sequence"),
    )

    version_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    canonical_id: Mapped[str] = mapped_column(String(64), index=True)
    canonical_url: Mapped[str] = mapped_column(String(1024))
    version_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(16))
    post_time: Mapped[datetime] = mapped_column(UTCDateTimeType)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    available_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    payload_hash: Mapped[str] = mapped_column(String(64))
    observation_kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))


class AnnouncementCoverage(Base):
    __tablename__ = "announcement_coverage"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(64))
    reported_total_pages: Mapped[int] = mapped_column(Integer)
    seen_pages: Mapped[list] = mapped_column(JSON)
    page_hashes: Mapped[dict] = mapped_column(JSON)
    internal_gap_pages: Mapped[list] = mapped_column(JSON)
    unseen_page_count: Mapped[int] = mapped_column(Integer)
    page_shift_detected: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    last_attempt_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    last_error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_version: Mapped[str] = mapped_column(String(128))


class AnnouncementCoverageObservation(Base):
    __tablename__ = "announcement_coverage_observation"
    __table_args__ = (
        UniqueConstraint(
            "source", "language", "observed_at", name="uq_announcement_coverage_observed_at"
        ),
        Index("ix_coverage_observation_asof", "source", "language", "observed_at"),
    )

    observation_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(16))
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
    scan_id: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    status: Mapped[str] = mapped_column(String(32))
    reported_total_pages: Mapped[int] = mapped_column(Integer)
    seen_pages: Mapped[list] = mapped_column(JSON)
    page_hashes: Mapped[dict] = mapped_column(JSON)
    internal_gap_pages: Mapped[list] = mapped_column(JSON)
    unseen_page_count: Mapped[int] = mapped_column(Integer)
    page_shift_detected: Mapped[bool] = mapped_column(Boolean)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
