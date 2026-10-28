"""Read-only local S11 watchdog for recent UTC four-hour anchors."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import run_s11_research as runner


def expected_anchors(now: datetime, count: int) -> list[datetime]:
    if now.tzinfo is None or now.utcoffset() != timedelta(0) or count < 1 or count > 336:
        raise ValueError("UTC time and 1..336 anchors required")
    current = now.replace(hour=now.hour - now.hour % 4, minute=0, second=0, microsecond=0)
    return [current - runner.STEP * offset for offset in range(count - 1, -1, -1)]


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def audit_anchor(
    anchor: datetime,
    now: datetime,
    attempts: list[tuple[str, str, str | None, str | None]],
    market: list[tuple[str, str, str, int]],
    rules: list[str],
) -> dict:
    deadline = anchor + runner.WINDOW
    claim = "missing"
    result = "missing"
    result_at = None
    if attempts:
        _, attempted_at, status, first_seen_at = attempts[0]
        claim = "ok" if _time(attempted_at) <= deadline else "late"
        if status is None:
            result = "unknown"
        elif first_seen_at is not None and _time(first_seen_at) > deadline:
            result = "late"
        else:
            result = status if status in {"ok", "error"} else "error"
            result_at = _time(first_seen_at) if first_seen_at else None

    market_status = []
    decision_at = max(anchor, result_at) if result_at else anchor
    for start in runner.market_windows(anchor):
        observations = [
            _time(observed)
            for window_start, observed, source, version in market
            if _time(window_start) == start
            and source == "rest"
            and version > 0
            and _time(observed) >= start + runner.STEP
        ]
        timely = [observed for observed in observations if observed <= deadline]
        if timely:
            market_status.append("ok")
            decision_at = max(decision_at, min(timely))
        else:
            market_status.append("late" if observations else "missing")

    observed_rules = [_time(value) for value in rules]
    timely_rules = [value for value in observed_rules if value <= decision_at]
    if timely_rules:
        rules_status = "ok" if decision_at - max(timely_rules) <= timedelta(hours=1) else "stale"
    else:
        rules_status = "late" if observed_rules else "missing"
    complete = claim == result == rules_status == "ok" and market_status == ["ok", "ok"]
    pending = (
        now <= deadline and not complete and claim != "late" and result not in {"late", "error"}
    )
    if pending:
        claim = "pending" if claim == "missing" else claim
        result = "pending" if result == "missing" else result
        market_status = ["pending" if value == "missing" else value for value in market_status]
        rules_status = "pending" if rules_status == "missing" else rules_status
    return {
        "anchor": anchor.isoformat(),
        "claim": claim,
        "result": result,
        "m1": market_status,
        "rules": rules_status,
        "status": "pending" if pending else ("ok" if complete else "attention"),
    }


def read_audit(anchors: list[datetime], now: datetime) -> list[dict]:
    path = runner.REAL_JOURNAL_PATH
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve() != runner.ROOT / "data/research/s11-gkg-sparse.sqlite"
    ):
        raise PermissionError("Fixed S11 journal missing or redirected")
    uri = f"file:{path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("S11 journal integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not runner.REQUIRED_TABLES <= tables:
            raise RuntimeError("S11 journal schema mismatch")
        output = []
        for anchor in anchors:
            key = runner.sqlite_datetime(anchor)
            starts = runner.market_windows(anchor)
            attempts = db.execute(
                "SELECT a.attempt_id, a.attempted_at, r.status, r.first_seen_at "
                "FROM sparse_gkg_attempt a LEFT JOIN sparse_gkg_result r "
                "ON r.attempt_id=a.attempt_id "
                "WHERE a.anchor_at=?",
                (key,),
            ).fetchall()
            market = db.execute(
                "SELECT window_start, observed_at, source, version FROM market_candle_observation "
                "WHERE symbol='BTC_USDT' AND timeframe='4h' AND window_start IN (?,?)",
                tuple(runner.sqlite_datetime(value) for value in starts),
            ).fetchall()
            rules = [
                row[0]
                for row in db.execute(
                    "SELECT observed_at FROM instrument_rules_observation WHERE symbol='BTC_USDT' "
                    "AND observed_at <= ? ORDER BY observed_at DESC",
                    (runner.sqlite_datetime(anchor + runner.WINDOW),),
                )
            ]
            output.append(audit_anchor(anchor, now, attempts, market, rules))
        return output


def write_audit_log(report: dict) -> None:
    runner.append_local_jsonl("s11-watchdog.jsonl", report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors", type=int, default=6, help="Recent UTC anchors, 1..336")
    args = parser.parse_args()
    try:
        now = datetime.now(UTC)
        anchors = expected_anchors(now, args.anchors)
        rows = read_audit(anchors, now)
        status = (
            "attention"
            if any(row["status"] == "attention" for row in rows)
            else ("pending" if any(row["status"] == "pending" for row in rows) else "ok")
        )
        report = {
            "at": now.isoformat(),
            "status": status,
            "anchors": rows,
        }
        write_audit_log(report)
        print(json.dumps(report, separators=(",", ":")))
        raise SystemExit(2 if report["status"] == "attention" else 0)
    except SystemExit:
        raise
    except Exception:
        print("S11 watchdog failed; inspect fixed journal and local log", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
