"""Paper-only persisted domain; decimal strings retain precision on all dialects."""

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import utc_now


class Money(TypeDecorator):
    impl = String(96)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(value)
        if not value.is_finite():
            raise ValueError("Non-finite decimal")
        return str(value)

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolio"
    __table_args__ = (CheckConstraint("mode = 'paper'", name="paper_only"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
    risk_profile: Mapped[dict] = mapped_column(JSON)


class PaperEpoch(Base):
    __tablename__ = "paper_epoch"
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    deposit: Mapped[Decimal] = mapped_column(Money)
    peak_equity: Mapped[Decimal] = mapped_column(Money)
    day_start_equity: Mapped[Decimal] = mapped_column(Money)
    risk_day: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperIntent(Base):
    __tablename__ = "paper_intent"
    __table_args__ = (UniqueConstraint("portfolio_id", "idempotency_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperCommandResult(Base):
    __tablename__ = "paper_command_result"
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("paper_intent.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class PaperVerdict(Base):
    __tablename__ = "paper_verdict"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("paper_intent.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperOrder(Base):
    __tablename__ = "paper_order"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("paper_intent.id"), unique=True)
    verdict_id: Mapped[UUID] = mapped_column(ForeignKey("paper_verdict.id"))
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(Money)
    filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    average_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    reserve: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    payload: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    updated_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperPosition(Base):
    __tablename__ = "paper_position"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Money)
    average_entry: Mapped[Decimal] = mapped_column(Money)
    margin: Mapped[Decimal] = mapped_column(Money)
    leverage: Mapped[Decimal] = mapped_column(Money)
    multiplier: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal] = mapped_column(Money)
    take_profit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    rules_version: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    closed_at: Mapped[object | None] = mapped_column(UTCDateTimeType, nullable=True)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperFill(Base):
    __tablename__ = "paper_fill"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("paper_order.id"), index=True)
    position_id: Mapped[UUID] = mapped_column(ForeignKey("paper_position.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money)
    exposure_delta: Mapped[Decimal] = mapped_column(Money)
    provenance: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class PaperLedger(Base):
    """One journal transaction = wallet posting and equal opposite counteraccount posting."""

    __tablename__ = "paper_ledger"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "event_key"),
        CheckConstraint(
            "CAST(wallet_delta AS NUMERIC) + CAST(offset_delta AS NUMERIC) = 0", name="balanced"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("paper_portfolio.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    event_key: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(32))
    counteraccount: Mapped[str] = mapped_column(String(32))
    wallet_delta: Mapped[Decimal] = mapped_column(Money)
    offset_delta: Mapped[Decimal] = mapped_column(Money)
    provenance: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[object] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))
