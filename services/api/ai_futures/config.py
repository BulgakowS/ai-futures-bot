from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_env: Literal["local", "test"] = "local"
    app_mode: Literal["paper"] = "paper"
    live_trading_enabled: bool = False
    config_version: str = "foundation-v1"
    backend_auth_token: SecretStr
    credential_master_key: SecretStr | None = None
    database_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    market_symbol: Literal["BTC_USDT"] = "BTC_USDT"
    worker_heartbeat_seconds: int = 5
    worker_stale_seconds: int = 20
    ai_enabled: bool = False
    news_fixture_ingest_enabled: bool = False
    gdelt_forward_enabled: bool = False
    gkg_forward_enabled: bool = False
    gkg_sparse_enabled: bool = False
    market_candle_journal_enabled: bool = False
    instrument_rules_journal_enabled: bool = False
    forecast_fixture_enabled: bool = False
    model_paper_fixture_enabled: bool = False
    model_bot_fixture_enabled: bool = False
    openai_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_delivery_enabled: bool = False
    telegram_delivery_cutoff_utc: datetime | None = None

    @field_validator("telegram_delivery_cutoff_utc", mode="before")
    @classmethod
    def parse_telegram_cutoff(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(
                    "Telegram delivery cutoff must be an ISO 8601 UTC timestamp"
                ) from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Telegram delivery cutoff must be an ISO 8601 UTC timestamp")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def safe_runtime(self):
        if self.live_trading_enabled:
            raise ValueError("Live trading is unavailable")
        if self.news_fixture_ingest_enabled and self.app_env != "test":
            raise ValueError("Announcement fixture ingest is test-only")
        if self.forecast_fixture_enabled and self.app_env != "test":
            raise ValueError("Forecast fixture journal is test-only")
        if self.model_paper_fixture_enabled and self.app_env != "test":
            raise ValueError("Model Paper fixture execution is test-only")
        if self.model_bot_fixture_enabled and self.app_env != "test":
            raise ValueError("Model bot fixture execution is test-only")
        token = self.backend_auth_token.get_secret_value()
        if len(token) < 32 or token == "generate-with-make-setup":
            raise ValueError("Generate a backend token of at least 32 characters")
        url = self.database_url.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            if not (self.app_env == "test" and url.startswith("sqlite+aiosqlite://")):
                raise ValueError("Runtime requires PostgreSQL; SQLite is only allowed in tests")
        if not 1 <= self.worker_heartbeat_seconds < self.worker_stale_seconds <= 300:
            raise ValueError("Heartbeat interval must be positive and below stale threshold")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ValueError("Telegram token and chat ID must be configured together")
        if self.telegram_delivery_cutoff_utc and self.telegram_delivery_cutoff_utc > datetime.now(
            UTC
        ):
            raise ValueError("Telegram delivery cutoff cannot be in the future")
        if self.telegram_delivery_enabled and (
            not self.telegram_bot_token
            or not self.telegram_chat_id
            or self.telegram_delivery_cutoff_utc is None
        ):
            raise ValueError("Telegram delivery requires token, chat ID and UTC cutoff")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
