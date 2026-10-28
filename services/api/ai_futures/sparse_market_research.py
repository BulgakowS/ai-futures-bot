"""One-shot public MEXC Hour4 observation into the isolated S11 research journal."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_sparse import REAL_JOURNAL_PATH, validate_anchor
from ai_futures.market import MarketStore
from ai_futures.mexc import MexcPublic
from ai_futures.schemas import require_utc, utc_now


@dataclass(frozen=True)
class MarketResearchResult:
    anchor_at: datetime
    rules_version: str
    final_windows: tuple[datetime, datetime]
    observed_at: datetime


async def collect_market_once(
    database: Database,
    settings: Settings,
    anchor_at: datetime,
    *,
    adapter: MexcPublic | None = None,
    observed_at: datetime | None = None,
) -> MarketResearchResult:
    """Use existing public adapter; reject stale, incomplete or nonfinal pairs."""
    if (
        settings.app_env != "test"
        or settings.app_mode != "paper"
        or settings.live_trading_enabled
        or not settings.market_candle_journal_enabled
        or not settings.instrument_rules_journal_enabled
        or settings.market_symbol != "BTC_USDT"
        or database.settings.database_url.get_secret_value()
        != settings.database_url.get_secret_value()
    ):
        raise PermissionError("Isolated test research market journals are required")
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername != "sqlite+aiosqlite" or not url.database:
        raise PermissionError("Research market collection requires SQLite")
    path = Path(url.database).resolve()
    if not path.is_file() or (adapter is None and path != REAL_JOURNAL_PATH):
        raise PermissionError("Real public HTTP requires the fixed S11 research journal")
    anchor = require_utc(anchor_at)
    validate_anchor(anchor, observed_at or utc_now())
    public = adapter or MexcPublic()
    try:
        before = utc_now()
        server_at = await public.server_time()
        after = utc_now()
        midpoint = before + (after - before) / 2
        if abs((server_at - midpoint).total_seconds()) > 2:
            raise ValueError("MEXC server clock skew")
        rules = await public.contract_rules("BTC_USDT")
        rows = await public.candles(
            "BTC_USDT",
            "4h",
            server_at,
            int((anchor - timedelta(hours=8)).timestamp()),
            int(anchor.timestamp()),
        )
        expected = (anchor - timedelta(hours=8), anchor - timedelta(hours=4))
        selected = tuple(row for row in rows if row[0] in expected)
        if (
            len(selected) != 2
            or tuple(row[0] for row in selected) != expected
            or not all(row[2] for row in selected)
            or any(row[0] not in (*expected, anchor) for row in rows)
        ):
            raise ValueError("Missing, duplicate or nonfinal Hour4 market windows")
        if utc_now() > anchor + timedelta(minutes=15):
            raise ValueError("Market observation arrived after decision window")
        store = MarketStore(
            database,
            settings.config_version,
            "BTC_USDT",
            journal_enabled=True,
            rules_journal_enabled=True,
        )
        await store.rules(rules)
        await store.candles("4h", list(selected), server_at, "rest")
        return MarketResearchResult(anchor, rules["version"], expected, utc_now())
    finally:
        if adapter is None:
            await public.close()
