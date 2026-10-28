"""Measure a bounded local paper observation without placing any orders."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

BASE = "http://127.0.0.1:8000/api/v1"
COMPOSE = ["docker", "compose", "-p", "ai-futures"]
SQL = """SELECT json_build_object(
 'final_candles', (SELECT count(*) FROM candle WHERE final),
 'paper_orders', (SELECT count(*) FROM paper_order),
 'paper_fills', (SELECT count(*) FROM paper_fill),
 'paper_ledger', (SELECT count(*) FROM paper_ledger),
 'unknown_orders', (SELECT count(*) FROM paper_order WHERE status='UNKNOWN'),
 'queued_alerts', (SELECT count(*) FROM outbox_event
   WHERE topic='alert.telegram' AND published_at IS NULL)
)::text;"""


def utc():
    return datetime.now(UTC).isoformat()


def get(path):
    try:
        with urlopen(f"{BASE}/{path}", timeout=4) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)
    except (URLError, TimeoutError, ConnectionError):
        return 0, {}


def db_counts():
    result = subprocess.run(
        COMPOSE + ["exec", "-T", "postgres", "sh", "-c",
                   'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'],
        input=SQL.encode(), capture_output=True, check=True,
    )
    return json.loads(result.stdout.decode().strip())


def resources():
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
        capture_output=True, text=True, check=True,
    )
    rows = {}
    for line in result.stdout.splitlines():
        name, cpu, memory = line.split("|", 2)
        if name.startswith("ai-futures-"):
            rows[name] = {
                "cpu_percent": float(cpu.rstrip("%")),
                "memory": memory.split("/")[0].strip(),
            }
    return rows


def main(seconds, interval):
    if not 10 <= seconds <= 300 or not 1 <= interval <= 30:
        raise ValueError("Observation must be 10-300 seconds with 1-30 second interval")
    status, state = get("state")
    if status != 200 or state.get("kill_switch") is not True:
        raise RuntimeError("Paper kill switch must be on for short observation")
    start = utc()
    before = db_counts()
    deadline = time.monotonic() + seconds
    samples = []
    while True:
        ready_code, ready = get("health/ready")
        market_code, market = get("markets/BTC_USDT/quality")
        samples.append({
            "at_utc": utc(), "ready_status": ready_code,
            "dependencies": ready.get("dependencies", {}),
            "market_status": market.get("status") if market_code == 200 else "unavailable",
            "market_reasons": market.get("reasons", []) if market_code == 200 else [],
            "resources": resources(),
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    after = db_counts()
    return {
        "start_utc": start, "end_utc": utc(), "duration_seconds": seconds,
        "interval_seconds": interval, "config_version": state["config_version"],
        "kill_switch": True, "samples": samples,
        "readiness_ok_samples": sum(s["ready_status"] == 200 for s in samples),
        "readiness_error_samples": sum(s["ready_status"] != 200 for s in samples),
        "market_ok_samples": sum(s["market_status"] == "ok" for s in samples),
        "counts_before": before, "counts_after": after,
        "final_candle_delta": after["final_candles"] - before["final_candles"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    try:
        print(json.dumps(main(args.seconds, args.interval), sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"short soak failed: {type(exc).__name__}") from None
