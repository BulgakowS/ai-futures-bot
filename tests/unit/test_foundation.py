from datetime import UTC, datetime
from decimal import Decimal

import pytest
from ai_futures.config import Settings
from ai_futures.schemas import ExactDecimal, UTCDateTime
from pydantic import TypeAdapter, ValidationError


def settings(**overrides):
    values = {
        "_env_file": None,
        "backend_auth_token": "x" * 48,
        "database_url": "postgresql+asyncpg://user:password@localhost/test",
    }
    return Settings(**(values | overrides))


@pytest.mark.parametrize(
    "values",
    [
        {"app_mode": "live"},
        {"live_trading_enabled": True},
        {"backend_auth_token": "weak"},
        {"database_url": "sqlite+aiosqlite:///:memory:"},
        {"worker_heartbeat_seconds": 0},
        {"worker_stale_seconds": 5},
    ],
)
def test_safe_config_rejects_unsafe_runtime(values):
    with pytest.raises(ValidationError):
        settings(**values)


def test_sqlite_only_for_tests_and_secrets_hidden():
    config = settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:")
    assert "password" not in repr(config)
    assert "x" * 48 not in repr(config)


def test_telegram_credentials_alone_leave_delivery_disabled():
    config = settings(telegram_bot_token="local-test-token", telegram_chat_id="test-chat")
    assert config.telegram_delivery_enabled is False
    assert config.telegram_delivery_cutoff_utc is None


@pytest.mark.parametrize("overrides", [
    {"telegram_delivery_enabled": True},
    {"telegram_delivery_enabled": True, "telegram_bot_token": "token", "telegram_chat_id": "chat"},
    {"telegram_delivery_cutoff_utc": "2026-01-01T00:00:00"},
    {"telegram_delivery_cutoff_utc": "2026-01-01T03:00:00+03:00"},
    {"telegram_delivery_cutoff_utc": "invalid"},
    {"telegram_delivery_cutoff_utc": "2999-01-01T00:00:00Z"},
    {"telegram_delivery_cutoff_utc": 0},
])
def test_telegram_delivery_rejects_missing_or_invalid_config(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_telegram_delivery_accepts_explicit_past_utc_cutoff():
    config = settings(
        telegram_bot_token="token", telegram_chat_id="chat",
        telegram_delivery_enabled=True,
        telegram_delivery_cutoff_utc="2026-01-01T00:00:00Z",
    )
    assert config.telegram_delivery_cutoff_utc == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", [1.1, True, "NaN", "Infinity"])
def test_decimal_rejects_float_nonfinite(value):
    with pytest.raises(ValidationError):
        TypeAdapter(ExactDecimal).validate_python(value)


def test_decimal_remains_exact_and_json_string():
    adapter = TypeAdapter(ExactDecimal)
    value = adapter.validate_python("0.1000000000000000001")
    assert value == Decimal("0.1000000000000000001")
    assert adapter.dump_json(value) == b'"0.1000000000000000001"'


def test_datetime_requires_timezone_and_normalizes_utc():
    adapter = TypeAdapter(UTCDateTime)
    with pytest.raises(ValidationError):
        adapter.validate_python(datetime(2026, 1, 1))
    parsed = adapter.validate_python("2026-01-01T03:00:00+03:00")
    assert parsed == datetime(2026, 1, 1, tzinfo=UTC)
