"""Explicit 4h15m disposable-SQLite GKG research run; never used by runtime workers."""

import asyncio
import os
import secrets
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_forward import GkgForwardPoller
from ai_futures.gdelt_gkg_pilot import RealClock, json_line, run_pilot

ROOT = Path(__file__).resolve().parents[1]


def enabled() -> bool:
    return (
        os.environ.get("APP_ENV") == "test"
        and os.environ.get("GKG_PILOT_ENABLED") == "true"
        and os.environ.get("GKG_FORWARD_ENABLED") == "true"
    )


def create_disposable_database() -> tuple[Path, Settings]:
    if not enabled():
        raise PermissionError("Pilot requires APP_ENV=test and both explicit GKG opt-ins")
    handle, name = tempfile.mkstemp(prefix="ai-futures-gkg-pilot-", suffix=".sqlite")
    os.close(handle)
    path = Path(name)
    json_line({"status": "preparing", "journal_sqlite": str(path)})
    token = secrets.token_urlsafe(48)
    url = f"sqlite+aiosqlite:///{path}"
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_mode="paper",
        live_trading_enabled=False,
        ai_enabled=False,
        telegram_delivery_enabled=False,
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_delivery_cutoff_utc=None,
        openai_api_key=None,
        news_fixture_ingest_enabled=False,
        gdelt_forward_enabled=False,
        gkg_forward_enabled=True,
        forecast_fixture_enabled=False,
        model_paper_fixture_enabled=False,
        model_bot_fixture_enabled=False,
        database_url=url,
        backend_auth_token=token,
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "APP_ENV": "test",
        "DATABASE_URL": url,
        "BACKEND_AUTH_TOKEN": token,
        "PYTHONPATH": f"{ROOT}:{ROOT / 'services/api'}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("Disposable SQLite migration failed")
    return path, settings


async def run(database: Database, settings: Settings):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    try:
        return await run_pilot(
            GkgForwardPoller(database, settings),
            database,
            clock=RealClock(),
            stop=stop,
            emit=json_line,
        )
    finally:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)


def main() -> int:
    if not enabled():
        json_line(
            {
                "status": "disabled",
                "required": "APP_ENV=test; GKG_PILOT_ENABLED=true; GKG_FORWARD_ENABLED=true",
            }
        )
        return 2
    try:
        path, settings = create_disposable_database()
        json_line({"status": "started", "journal_sqlite": str(path)})
        summary = asyncio.run(_run_and_close(settings))
        return 0 if summary["reason"] in {"deadline", "attempt_cap", "interrupted"} else 1
    except KeyboardInterrupt:
        json_line({"status": "interrupted"})
        return 130
    except Exception:
        json_line({"status": "failed", "error_code": "pilot_setup_or_runtime"})
        return 1


async def _run_and_close(settings: Settings):
    database = Database(settings)
    try:
        return await run(database, settings)
    finally:
        await database.close()


if __name__ == "__main__":
    raise SystemExit(main())
