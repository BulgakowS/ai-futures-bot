from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import utc_now


class AppState(Base):
    __tablename__ = "app_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("mode = 'paper'", name="paper_only"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    instance_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4, unique=True)
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=True)
    config_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
    topic: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)


class AlertState(Base):
    __tablename__ = "alert_state"
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    pending_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)


class InstrumentRules(Base):
    __tablename__ = "instrument_rules"
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), default="mexc")
    validated: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    config_version: Mapped[str] = mapped_column(String(128))


class InstrumentRulesObservation(Base):
    """Append-only public rules observed from the local collection time onward."""

    __tablename__ = "instrument_rules_observation"
    __table_args__ = (
        UniqueConstraint("symbol", "version", name="uq_instrument_rules_observation_version"),
        CheckConstraint("symbol = 'BTC_USDT'", name="instrument_rules_observation_scope"),
        CheckConstraint("version > 0", name="instrument_rules_observation_positive_version"),
        CheckConstraint(
            "effective_at = observed_at", name="instrument_rules_observation_local_effective"
        ),
        Index("ix_instrument_rules_observation_asof", "symbol", "observed_at"),
    )

    observation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    causal_id: Mapped[UUID] = mapped_column(Uuid)
    symbol: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    payload_hash: Mapped[str] = mapped_column(String(64))
    rules_version: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshot"
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    source_time: Mapped[datetime] = mapped_column(UTCDateTimeType)
    received_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    config_version: Mapped[str] = mapped_column(String(128))


class Candle(Base):
    __tablename__ = "candle"
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTimeType, primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    source_time: Mapped[datetime] = mapped_column(UTCDateTimeType)
    received_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32))
    config_version: Mapped[str] = mapped_column(String(128))


class MarketCandleObservation(Base):
    """Immutable REST-reconciled final Hour4 observation for as-of research."""

    __tablename__ = "market_candle_observation"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "window_start", "version", name="uq_market_candle_version"
        ),
        CheckConstraint("symbol = 'BTC_USDT' AND timeframe = '4h'", name="market_candle_scope"),
        CheckConstraint("version > 0", name="market_candle_version"),
        CheckConstraint("source = 'rest'", name="market_candle_rest"),
        Index("ix_market_candle_asof", "symbol", "timeframe", "window_start", "observed_at"),
    )

    observation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    causal_id: Mapped[UUID] = mapped_column(Uuid)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    window_start: Mapped[datetime] = mapped_column(UTCDateTimeType)
    version: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    source_time: Mapped[datetime] = mapped_column(UTCDateTimeType)
    payload_hash: Mapped[str] = mapped_column(String(64))
    close: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(16))
    source_version: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(128))
    rules_version: Mapped[str] = mapped_column(String(64))


class FundingSettlement(Base):
    """Immutable observed public settlement rate, independent of paper portfolios."""

    __tablename__ = "funding_settlement"
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    settle_time: Mapped[datetime] = mapped_column(UTCDateTimeType, primary_key=True)
    rate: Mapped[str] = mapped_column(String(96))
    collect_cycle: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSON)
    raw_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    config_version: Mapped[str] = mapped_column(String(128))


class FundingCoverage(Base):
    """Last fully checked interval: [covered_from, covered_until)."""

    __tablename__ = "funding_coverage"
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    covered_from: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    covered_until: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    gap_time: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    config_version: Mapped[str] = mapped_column(String(128))


from ai_futures import (  # noqa: E402,F401
    ai_models,
    backtest_models,
    bot_models,
    forecast_models,
    gdelt_gkg_models,
    gdelt_gkg_sparse_models,
    gdelt_models,
    news_models,
    paper_models,
)
