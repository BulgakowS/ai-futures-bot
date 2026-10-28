"""One-shot S11 research runner. Print a launchd plist with --print-plist.

The plist wakes once a minute; this process only acts during a current UTC
four-hour anchor's first 15 minutes. The GKG claim is the durable run marker.
"""

import argparse
import asyncio
import fcntl
import json
import os
import plistlib
import secrets
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from ai_futures.config import Settings  # noqa: E402
from ai_futures.db import Database  # noqa: E402
from ai_futures.gdelt_gkg_sparse import (  # noqa: E402
    BUDGET_VERSION,
    PRIOR_NEWS_BYTES,
    REAL_JOURNAL_PATH,
    TOTAL_CAP_BYTES,
    SparseGkgCollector,
    validate_anchor,
)
from ai_futures.mexc import MexcPublic  # noqa: E402
from ai_futures.sparse_market_research import collect_market_once  # noqa: E402

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

STEP = timedelta(hours=4)
WINDOW = timedelta(minutes=15)
REQUIRED_TABLES = {
    "alembic_version",
    "sparse_gkg_attempt",
    "sparse_gkg_result",
    "sparse_gkg_budget",
    "market_candle_observation",
    "instrument_rules_observation",
}
MIN_RESEARCH_REVISION = "71a5d0e9c324"


def current_anchor(now: datetime) -> datetime | None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("UTC clock required")
    now = now.astimezone(UTC)
    anchor = now.replace(hour=now.hour - now.hour % 4, minute=0, second=0, microsecond=0)
    return anchor if now <= anchor + WINDOW else None


def market_windows(anchor: datetime) -> tuple[datetime, datetime]:
    return anchor - 2 * STEP, anchor - STEP


def utc_now() -> datetime:
    return datetime.now(UTC)


def write_state_event(anchor: datetime, run_id: str, phase: str, status: str) -> None:
    """Append only bounded, predefined metadata; never serialize responses/errors."""
    if phase not in {"preflight", "clock", "gkg", "market", "complete"} or status not in {
        "started",
        "already_attempted",
        "ok",
        "error",
        "budget_exhausted",
    }:
        raise ValueError("Unexpected S11 state event")
    record = {
        "at": utc_now().isoformat(),
        "anchor": anchor.isoformat(),
        "run_id": run_id,
        "phase": phase,
        "status": status,
        "reason": phase if status == "error" else None,
    }
    append_local_jsonl("s11-runner-state.jsonl", record)


def append_local_jsonl(name: str, record: dict) -> None:
    if name not in {"s11-runner-state.jsonl", "s11-watchdog.jsonl"}:
        raise ValueError("Unexpected S11 log")
    directory = ROOT / "data/research"
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or directory.resolve() != ROOT / "data/research"
    ):
        raise PermissionError("Fixed S11 research directory missing or redirected")
    path = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    lock_fd = os.open(
        directory / f"{name}.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise PermissionError("S11 log lock is not a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PermissionError("S11 log is not a regular file")
            if os.fstat(fd).st_size + len(payload) > 1_048_576:
                os.close(fd)
                fd = -1
                os.replace(path, directory / f"{name}.1")
                fd = os.open(path, flags, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(lock_fd)


def sqlite_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def market_complete(anchor: datetime, rows: list[tuple]) -> bool:
    """Both windows must have timely immutable REST observations."""
    expected = set(market_windows(anchor))
    seen = set()
    for start, observed, source, version in rows:
        if start not in expected or source != "rest" or version < 1:
            raise ValueError("M1 journal mismatch")
        if not anchor <= observed <= anchor + WINDOW:
            # A prior anchor may have already observed the overlapping candle.
            if not start + STEP <= observed <= anchor + WINDOW:
                raise ValueError("M1 observation outside as-of window")
        seen.add(start)
    return seen == expected


def compatible_revision(version: str) -> bool:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    try:
        current = scripts.get_revision(version)
    except Exception:
        return False
    while current is not None:
        if current.revision == MIN_RESEARCH_REVISION:
            return True
        parent = current.down_revision
        if not isinstance(parent, str):
            return False
        current = scripts.get_revision(parent)
    return False


def inspect_journal(anchor: datetime) -> tuple[bool, bool]:
    """Read the only permitted journal without creating or migrating it."""
    path = REAL_JOURNAL_PATH
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve() != ROOT / "data/research/s11-gkg-sparse.sqlite"
    ):
        raise PermissionError("Fixed S11 journal missing or redirected")
    uri = f"file:{path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("S11 journal integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES <= tables:
            raise RuntimeError("S11 journal schema mismatch")
        versions = db.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(versions) != 1 or not compatible_revision(versions[0][0]):
            raise RuntimeError("S11 journal migration mismatch")
        budgets = db.execute(
            "SELECT cap_bytes, prior_bytes, budget_version FROM sparse_gkg_budget"
        ).fetchall()
        if budgets and budgets != [(TOTAL_CAP_BYTES, PRIOR_NEWS_BYTES, BUDGET_VERSION)]:
            raise RuntimeError("S11 budget mismatch")
        attempted = (
            db.execute(
                "SELECT 1 FROM sparse_gkg_attempt WHERE anchor_at = ?",
                (sqlite_datetime(anchor),),
            ).fetchone()
            is not None
        )
        starts = market_windows(anchor)
        rows = db.execute(
            "SELECT window_start, observed_at, source, version "
            "FROM market_candle_observation "
            "WHERE symbol = 'BTC_USDT' AND timeframe = '4h' "
            "AND window_start IN (?, ?)",
            tuple(sqlite_datetime(value) for value in starts),
        ).fetchall()
    parsed = [
        (
            datetime.fromisoformat(start).replace(tzinfo=UTC),
            datetime.fromisoformat(observed).replace(tzinfo=UTC),
            source,
            version,
        )
        for start, observed, source, version in rows
    ]
    return attempted, market_complete(anchor, parsed)


def settings_for_journal() -> Settings:
    # model_validate avoids inheriting unrelated app or secret settings.
    return Settings.model_validate(
        {
            "app_env": "test",
            "app_mode": "paper",
            "live_trading_enabled": False,
            "ai_enabled": False,
            "gdelt_forward_enabled": False,
            "gkg_forward_enabled": False,
            "gkg_sparse_enabled": True,
            "market_candle_journal_enabled": True,
            "instrument_rules_journal_enabled": True,
            "telegram_delivery_enabled": False,
            "backend_auth_token": secrets.token_urlsafe(48),
            "database_url": f"sqlite+aiosqlite:///{REAL_JOURNAL_PATH}",
        }
    )


async def run_once() -> str:
    if os.environ.get("APP_ENV") != "test":
        raise PermissionError("APP_ENV=test required")
    now = utc_now()
    anchor = current_anchor(now)
    if anchor is None:
        return "outside_window"
    run_id = secrets.token_hex(8)
    phase = "preflight"
    write_state_event(anchor, run_id, phase, "started")
    try:
        validate_anchor(anchor, now)
        attempted, complete = inspect_journal(anchor)
        if attempted:
            write_state_event(anchor, run_id, phase, "already_attempted")
            return "already_attempted"

        # Verify the OS clock against a public exchange clock before any GKG claim/GET.
        phase = "clock"
        public = MexcPublic()
        try:
            before = utc_now()
            remote = await public.server_time()
            after = utc_now()
            if abs((remote - (before + (after - before) / 2)).total_seconds()) > 2:
                raise ValueError("MEXC server clock skew")
            validate_anchor(anchor, after)
        finally:
            await public.close()

        settings = settings_for_journal()
        database = Database(settings)
        try:
            phase = "gkg"
            result = await SparseGkgCollector(database, settings).collect_once(anchor)
            if result.status in {"ok", "error"} and not complete:
                phase = "market"
                validate_anchor(anchor, utc_now())
                await collect_market_once(database, settings, anchor)
            write_state_event(anchor, run_id, "complete", result.status)
            return result.status
        finally:
            await database.close()
    except Exception:
        write_state_event(anchor, run_id, phase, "error")
        raise


def launchd_plist() -> bytes:
    return plistlib.dumps(
        {
            "Label": "local.ai-futures.s11-research",
            "ProgramArguments": [
                str(Path(sys.executable).absolute()),
                str(Path(__file__).resolve()),
            ],
            "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": {"APP_ENV": "test"},
            "RunAtLoad": True,
            "StartInterval": 60,
            "StandardOutPath": str(ROOT / "data/research/s11-runner.stdout.log"),
            "StandardErrorPath": str(ROOT / "data/research/s11-runner.stderr.log"),
        }
    )


def awake_launchd_plist() -> bytes:
    return plistlib.dumps(
        {
            "Label": "local.ai-futures.s11-awake",
            "ProgramArguments": ["/usr/bin/caffeinate", "-is"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(ROOT / "data/research/s11-awake.stdout.log"),
            "StandardErrorPath": str(ROOT / "data/research/s11-awake.stderr.log"),
        }
    )


def audit_launchd_plist() -> bytes:
    return plistlib.dumps(
        {
            "Label": "local.ai-futures.s11-audit",
            "ProgramArguments": [
                str(Path(sys.executable).absolute()),
                str(ROOT / "scripts/audit_s11_research.py"),
                "--anchors",
                "2",
            ],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "StartInterval": 300,
            "StandardOutPath": str(ROOT / "data/research/s11-audit.stdout.log"),
            "StandardErrorPath": str(ROOT / "data/research/s11-audit.stderr.log"),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    plist = parser.add_mutually_exclusive_group()
    plist.add_argument("--print-plist", action="store_true")
    plist.add_argument("--print-awake-plist", action="store_true")
    plist.add_argument("--print-audit-plist", action="store_true")
    args = parser.parse_args()
    if args.print_plist:
        sys.stdout.buffer.write(launchd_plist())
    elif args.print_awake_plist:
        sys.stdout.buffer.write(awake_launchd_plist())
    elif args.print_audit_plist:
        sys.stdout.buffer.write(audit_launchd_plist())
    else:
        try:
            status = asyncio.run(run_once())
        except Exception:
            print("S11 runner failed; inspect local state log", file=sys.stderr)
            raise SystemExit(1) from None
        if status not in {"outside_window", "already_attempted"}:
            print(status)


if __name__ == "__main__":
    main()
