from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, PlainSerializer


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone is required")
    return value.astimezone(UTC)


def require_decimal(value):
    if isinstance(value, float | bool):
        raise ValueError("Use a decimal string, integer or Decimal, never binary float")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError("Decimal must be finite")
    return parsed


UTCDateTime = Annotated[datetime, AfterValidator(require_utc)]
ExactDecimal = Annotated[
    Decimal,
    BeforeValidator(require_decimal),
    PlainSerializer(str, return_type=str, when_used="json"),
]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AppStateResponse(Schema):
    instance_id: UUID
    mode: str
    kill_switch: bool
    config_version: str
    created_at: UTCDateTime
