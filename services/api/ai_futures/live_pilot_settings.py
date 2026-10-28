"""Versioned, inert per-bot settings for a possible future Live pilot."""

from datetime import datetime
from decimal import InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy import JSON, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.bot_models import Bot
from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import ExactDecimal, Schema, require_decimal, utc_now


def valid_pilot_decimal(value):
    try:
        return require_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("A finite decimal string is required") from exc


class AmountLimit(Schema):
    unit: Literal["USDT", "PCT_EQUITY"]
    value: ExactDecimal = Field(gt=0)

    _valid_value = field_validator("value", mode="before")(valid_pilot_decimal)


class LivePilotDraft(Schema):
    max_pairs: int = Field(strict=True, ge=1, le=3)
    per_pair_notional: AmountLimit
    aggregate_notional: AmountLimit
    max_leverage: ExactDecimal = Field(ge=1)
    take_profit_pct: ExactDecimal = Field(gt=0, lt=100)
    stop_loss_pct: ExactDecimal = Field(gt=0, lt=100)
    max_daily_loss: AmountLimit
    max_round_trip_cost_pct: ExactDecimal = Field(gt=0, lt=100)

    _valid_decimals = field_validator(
        "max_leverage", "take_profit_pct", "stop_loss_pct", "max_round_trip_cost_pct", mode="before"
    )(valid_pilot_decimal)

    @model_validator(mode="after")
    def comparable_notional_caps(self):
        if self.per_pair_notional.unit != self.aggregate_notional.unit:
            raise ValueError("Notional cap units must match")
        if self.aggregate_notional.value < self.per_pair_notional.value:
            raise ValueError("Aggregate notional cap must be at least the per-pair cap")
        if self.max_daily_loss.unit == "PCT_EQUITY" and self.max_daily_loss.value >= 100:
            raise ValueError("Daily loss must be below 100% of equity")
        return self


class LivePilotBotConfig(Base):
    __tablename__ = "live_pilot_bot_config"

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bot.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


class LivePilotSettingsService:
    def __init__(self, database, config_version: str):
        self.database = database
        self.config_version = config_version

    @staticmethod
    def describe(bot_id: UUID, row: LivePilotBotConfig | None):
        return {
            "bot_id": str(bot_id),
            "draft": row.payload if row else None,
            "version": row.version if row else None,
            "created_at": row.created_at if row else None,
            "config_version": row.config_version if row else None,
            "live_allowed": False,
            "reason": "LIVE_DISABLED" if row else "PILOT_CONFIG_MISSING",
        }

    async def get(self, bot_id: UUID):
        from ai_futures.bots import BotError

        async with self.database.sessions() as session:
            if await session.get(Bot, bot_id) is None:
                raise BotError("BOT_NOT_FOUND", 404)
            row = await session.scalar(
                select(LivePilotBotConfig)
                .where(LivePilotBotConfig.bot_id == bot_id)
                .order_by(LivePilotBotConfig.version.desc())
                .limit(1)
            )
            return self.describe(bot_id, row)

    async def put(self, bot_id: UUID, draft: LivePilotDraft):
        from ai_futures.bots import BotError

        async with self.database.sessions.begin() as session:
            bot = await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
            if bot is None:
                raise BotError("BOT_NOT_FOUND", 404)
            previous = await session.scalar(
                select(LivePilotBotConfig)
                .where(LivePilotBotConfig.bot_id == bot_id)
                .order_by(LivePilotBotConfig.version.desc())
                .limit(1)
            )
            row = LivePilotBotConfig(
                bot_id=bot.id,
                version=previous.version + 1 if previous else 1,
                payload=draft.model_dump(mode="json"),
                created_at=utc_now(),
                config_version=self.config_version,
            )
            session.add(row)
            await session.flush()
            return self.describe(bot_id, row)
