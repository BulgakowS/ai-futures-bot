"""Independent, append-only GDELT-owned aggregate observations."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType


class GdeltVolumeRevision(Base):
    __tablename__ = "gdelt_volume_revision"
    __table_args__ = (
        UniqueConstraint("point_at", "revision", name="uq_gdelt_point_revision"),
        Index("ix_gdelt_point_asof", "point_at", "observed_at"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    scan_id: Mapped[UUID] = mapped_column(index=True)
    point_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    revision: Mapped[int] = mapped_column(Integer)
    value: Mapped[str] = mapped_column(String(32))
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))


class GdeltVolumeScan(Base):
    __tablename__ = "gdelt_volume_scan"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'gap', 'error')", name="gdelt_scan_status"),
    )

    scan_id: Mapped[UUID] = mapped_column(primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    status: Mapped[str] = mapped_column(String(16))
    point_count: Mapped[int] = mapped_column(Integer)
    nonzero_count: Mapped[int] = mapped_column(Integer)
    missing_bucket_count: Mapped[int] = mapped_column(Integer)
    revised_count: Mapped[int] = mapped_column(Integer)
    inserted_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
