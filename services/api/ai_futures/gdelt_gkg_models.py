"""Append-only observations for the independent GKG candidate source."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType


class GkgScan(Base):
    __tablename__ = "gkg_scan"
    __table_args__ = (CheckConstraint("status IN ('ok','repeat','error')", name="gkg_scan_status"),)

    scan_id: Mapped[UUID] = mapped_column(primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    batch_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    manifest_bytes: Mapped[int] = mapped_column(Integer)
    archive_bytes: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))


class GkgSlotObservation(Base):
    __tablename__ = "gkg_slot_observation"
    __table_args__ = (
        UniqueConstraint("scan_id", "slot_at", name="uq_gkg_scan_slot"),
        CheckConstraint("status IN ('ok','unknown','error','repeat')", name="gkg_slot_status"),
        Index("ix_gkg_slot_asof", "slot_at", "observed_at"),
    )

    observation_id: Mapped[UUID] = mapped_column(primary_key=True)
    scan_id: Mapped[UUID] = mapped_column(index=True)
    slot_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    status: Mapped[str] = mapped_column(String(16))
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))


class GkgReleaseRevision(Base):
    __tablename__ = "gkg_release_revision"
    __table_args__ = (
        UniqueConstraint("batch_at", "revision", name="uq_gkg_batch_revision"),
        CheckConstraint(
            "revision > 0 AND total_rows > 0 AND btc_rows >= 0 AND btc_rows <= total_rows",
            name="gkg_counts",
        ),
        Index("ix_gkg_batch_asof", "batch_at", "observed_at"),
    )

    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    scan_id: Mapped[UUID] = mapped_column(index=True)
    batch_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    revision: Mapped[int] = mapped_column(Integer)
    total_rows: Mapped[int] = mapped_column(Integer)
    btc_rows: Mapped[int] = mapped_column(Integer)
    mean_btc_document_tone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    archive_sha256: Mapped[str] = mapped_column(String(64))
    manifest_md5: Mapped[str] = mapped_column(String(32))
    manifest_size: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
