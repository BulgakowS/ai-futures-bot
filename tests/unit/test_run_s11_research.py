"""Offline scheduling and journal safety checks for the local S11 runner."""

import plistlib
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audit_s11_research as audit
from scripts import run_s11_research as runner

ANCHOR = datetime(2026, 10, 21, 8, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_real_state_log(monkeypatch):
    monkeypatch.setattr(runner, "write_state_event", lambda *_: None)


def test_exact_utc_anchor_window():
    assert runner.current_anchor(ANCHOR) == ANCHOR
    assert runner.current_anchor(ANCHOR + timedelta(minutes=15)) == ANCHOR
    assert runner.current_anchor(ANCHOR + timedelta(minutes=15, microseconds=1)) is None
    assert runner.current_anchor(ANCHOR - timedelta(seconds=1)) is None
    assert runner.current_anchor(ANCHOR + timedelta(hours=4)) == ANCHOR + timedelta(hours=4)
    with pytest.raises(ValueError, match="UTC"):
        runner.current_anchor(ANCHOR.replace(tzinfo=None))


def test_market_complete_requires_two_timely_rest_windows():
    old, recent = runner.market_windows(ANCHOR)
    rows = [(old, ANCHOR - timedelta(hours=4), "rest", 1), (recent, ANCHOR, "rest", 1)]
    assert runner.market_complete(ANCHOR, rows)
    assert not runner.market_complete(ANCHOR, rows[:1])
    with pytest.raises(ValueError, match="as-of"):
        runner.market_complete(ANCHOR, [(old, ANCHOR + timedelta(minutes=16), "rest", 1)])
    with pytest.raises(ValueError, match="mismatch"):
        runner.market_complete(ANCHOR, [(old, ANCHOR, "ws", 1)])


def journal(tmp_path, monkeypatch):
    root = tmp_path
    path = root / "data/research/s11-gkg-sparse.sqlite"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "REAL_JOURNAL_PATH", path)
    monkeypatch.setattr(runner, "compatible_revision", lambda value: value == "test-head")
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT);
            INSERT INTO alembic_version VALUES ('test-head');
            CREATE TABLE sparse_gkg_attempt (attempt_id TEXT, anchor_at TEXT, attempted_at TEXT);
            CREATE TABLE sparse_gkg_result (attempt_id TEXT, status TEXT, first_seen_at TEXT);
            CREATE TABLE sparse_gkg_budget (
                cap_bytes INTEGER, prior_bytes INTEGER, budget_version TEXT
            );
            CREATE TABLE market_candle_observation (
                symbol TEXT, timeframe TEXT, window_start TEXT,
                observed_at TEXT, source TEXT, version INTEGER
            );
            CREATE TABLE instrument_rules_observation (
                symbol TEXT, observed_at TEXT, version INTEGER
            );
            """
        )
    return path


def test_fixed_journal_claim_and_m3_skip(tmp_path, monkeypatch):
    path = journal(tmp_path, monkeypatch)
    assert runner.inspect_journal(ANCHOR) == (False, False)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sparse_gkg_attempt VALUES (?,?,?)",
            ("id-1", runner.sqlite_datetime(ANCHOR), runner.sqlite_datetime(ANCHOR)),
        )
        for start in runner.market_windows(ANCHOR):
            db.execute(
                "INSERT INTO market_candle_observation VALUES (?,?,?,?,?,?)",
                (
                    "BTC_USDT",
                    "4h",
                    runner.sqlite_datetime(start),
                    runner.sqlite_datetime(ANCHOR),
                    "rest",
                    1,
                ),
            )
    assert runner.inspect_journal(ANCHOR) == (True, True)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE alembic_version SET version_num='wrong'")
    with pytest.raises(RuntimeError, match="migration mismatch"):
        runner.inspect_journal(ANCHOR)


def test_budget_and_redirect_mismatch_fail_closed(tmp_path, monkeypatch):
    path = journal(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO sparse_gkg_budget VALUES (1, 2, 'wrong')")
    with pytest.raises(RuntimeError, match="budget mismatch"):
        runner.inspect_journal(ANCHOR)
    path.unlink()
    path.symlink_to(tmp_path / "other.sqlite")
    with pytest.raises(PermissionError, match="redirected"):
        runner.inspect_journal(ANCHOR)


@pytest.mark.asyncio
async def test_outside_window_and_existing_claim_never_open_network(monkeypatch):
    def forbidden():
        raise AssertionError("network or database opened")

    monkeypatch.setattr(runner, "MexcPublic", forbidden)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(runner, "utc_now", lambda: ANCHOR + timedelta(minutes=16))
    assert await runner.run_once() == "outside_window"
    monkeypatch.setattr(runner, "utc_now", lambda: ANCHOR + timedelta(minutes=1))
    monkeypatch.setattr(runner, "inspect_journal", lambda _: (True, False))
    assert await runner.run_once() == "already_attempted"


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_one_claim_and_conditional_market_poll(monkeypatch, complete):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(runner, "utc_now", lambda: ANCHOR + timedelta(minutes=1))
    monkeypatch.setattr(runner, "inspect_journal", lambda _: (False, complete))
    calls = []

    class Public:
        async def server_time(self):
            calls.append("clock")
            return ANCHOR + timedelta(minutes=1)

        async def close(self):
            calls.append("close")

    class Collector:
        def __init__(self, *_):
            pass

        async def collect_once(self, anchor):
            assert anchor == ANCHOR
            calls.append("claim")
            return SimpleNamespace(status="ok")

    class Database:
        def __init__(self, _):
            calls.append("db")

        async def close(self):
            calls.append("db_close")

    async def market(*_):
        calls.append("market")

    monkeypatch.setattr(runner, "MexcPublic", Public)
    monkeypatch.setattr(runner, "settings_for_journal", lambda: object())
    monkeypatch.setattr(runner, "Database", Database)
    monkeypatch.setattr(runner, "SparseGkgCollector", Collector)
    monkeypatch.setattr(runner, "collect_market_once", market)
    assert await runner.run_once() == "ok"
    assert calls == ["clock", "close", "db", "claim", *([] if complete else ["market"]), "db_close"]


@pytest.mark.asyncio
async def test_clock_skew_blocks_claim(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(runner, "utc_now", lambda: ANCHOR + timedelta(minutes=1))
    monkeypatch.setattr(runner, "inspect_journal", lambda _: (False, False))
    events = []
    monkeypatch.setattr(runner, "write_state_event", lambda *event: events.append(event))

    class SkewedPublic:
        async def server_time(self):
            return ANCHOR + timedelta(minutes=2)

        async def close(self):
            pass

    monkeypatch.setattr(runner, "MexcPublic", SkewedPublic)
    monkeypatch.setattr(
        runner, "SparseGkgCollector", lambda *_: pytest.fail("GKG claim after clock skew")
    )
    with pytest.raises(ValueError, match="clock skew"):
        await runner.run_once()
    assert [(event[2], event[3]) for event in events] == [
        ("preflight", "started"),
        ("clock", "error"),
    ]


def test_launchd_plist_is_local_and_minimal():
    data = plistlib.loads(runner.launchd_plist())
    assert data["StartInterval"] == 60
    assert data["EnvironmentVariables"] == {"APP_ENV": "test"}
    assert data["ProgramArguments"][0] == str(Path(sys.executable).absolute())
    assert data["ProgramArguments"][1] == str(Path(runner.__file__).resolve())
    assert data["StandardErrorPath"] == str(runner.ROOT / "data/research/s11-runner.stderr.log")
    assert data["StandardOutPath"] == str(runner.ROOT / "data/research/s11-runner.stdout.log")
    awake = plistlib.loads(runner.awake_launchd_plist())
    assert awake["ProgramArguments"] == ["/usr/bin/caffeinate", "-is"]
    assert awake["KeepAlive"] is True
    assert "StartInterval" not in awake
    audit_plist = plistlib.loads(runner.audit_launchd_plist())
    assert audit_plist["ProgramArguments"] == [
        str(Path(sys.executable).absolute()),
        str(runner.ROOT / "scripts/audit_s11_research.py"),
        "--anchors",
        "2",
    ]
    assert audit_plist["StartInterval"] == 300
    assert audit_plist["RunAtLoad"] is True
    assert audit_plist["StandardOutPath"] == str(runner.ROOT / "data/research/s11-audit.stdout.log")
    assert audit_plist["StandardErrorPath"] == str(
        runner.ROOT / "data/research/s11-audit.stderr.log"
    )


def test_state_log_contains_only_allowlisted_metadata_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    directory = tmp_path / "data/research"
    directory.mkdir(parents=True)
    runner.append_local_jsonl("s11-runner-state.jsonl", {"status": "preflight"})
    path = directory / "s11-runner-state.jsonl"
    path.write_bytes(b"x" * 1_048_576)
    runner.append_local_jsonl("s11-runner-state.jsonl", {"status": "error"})
    assert path.read_text() == '{"status":"error"}\n'
    assert (directory / "s11-runner-state.jsonl.1").stat().st_size == 1_048_576
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        runner.append_local_jsonl("s11-runner-state.jsonl", {"status": "error"})


def test_watchdog_pending_missing_late_and_no_future_label():
    assert audit.expected_anchors(ANCHOR + timedelta(minutes=1), 2) == [
        ANCHOR - runner.STEP,
        ANCHOR,
    ]
    pending = audit.audit_anchor(ANCHOR, ANCHOR + timedelta(minutes=1), [], [], [])
    assert pending["status"] == pending["claim"] == pending["result"] == "pending"
    assert (
        audit.audit_anchor(ANCHOR, ANCHOR + timedelta(minutes=15), [], [], [])["status"]
        == "pending"
    )
    missing = audit.audit_anchor(ANCHOR, ANCHOR + timedelta(minutes=16), [], [], [])
    assert missing["status"] == "attention" and missing["claim"] == "missing"
    late = audit.audit_anchor(
        ANCHOR,
        ANCHOR + timedelta(minutes=16),
        [
            (
                "id",
                runner.sqlite_datetime(ANCHOR),
                "ok",
                runner.sqlite_datetime(ANCHOR + timedelta(minutes=16)),
            )
        ],
        [],
        [],
    )
    assert late["result"] == "late"
    assert "label" not in late
    failed = audit.audit_anchor(
        ANCHOR,
        ANCHOR + timedelta(minutes=1),
        [("id", runner.sqlite_datetime(ANCHOR), "error", runner.sqlite_datetime(ANCHOR))],
        [],
        [],
    )
    assert failed["status"] == "attention"


def test_watchdog_reads_fixed_sqlite_without_mutation(tmp_path, monkeypatch):
    path = journal(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sparse_gkg_attempt VALUES (?,?,?)",
            ("id", runner.sqlite_datetime(ANCHOR), runner.sqlite_datetime(ANCHOR)),
        )
        db.execute(
            "INSERT INTO sparse_gkg_result VALUES (?,?,?)",
            ("id", "ok", runner.sqlite_datetime(ANCHOR + timedelta(minutes=1))),
        )
        for start in runner.market_windows(ANCHOR):
            db.execute(
                "INSERT INTO market_candle_observation VALUES (?,?,?,?,?,?)",
                (
                    "BTC_USDT",
                    "4h",
                    runner.sqlite_datetime(start),
                    runner.sqlite_datetime(ANCHOR + timedelta(minutes=2)),
                    "rest",
                    1,
                ),
            )
        db.execute(
            "INSERT INTO instrument_rules_observation VALUES (?,?,?)",
            ("BTC_USDT", runner.sqlite_datetime(ANCHOR + timedelta(minutes=2)), 1),
        )
    before = path.read_bytes()
    rows = audit.read_audit([ANCHOR], ANCHOR + timedelta(minutes=16))
    assert rows[0]["status"] == "ok"
    assert path.read_bytes() == before
