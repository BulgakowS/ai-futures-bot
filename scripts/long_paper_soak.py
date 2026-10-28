"""Bounded, read-only observation of the local public-market Paper stack."""

import argparse
import json
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

BASE = "http://127.0.0.1:8000/api/v1"
COMPOSE = ["docker", "compose", "-p", "ai-futures"]
SQL = """SELECT json_build_object(
 'final_candles', (SELECT count(*) FROM candle WHERE final),
 'latest_final_1m', (SELECT max(window_start) FROM candle WHERE final AND timeframe='1m'),
 'funding_settlements', (SELECT count(*) FROM funding_settlement),
 'funding_coverage', (SELECT row_to_json(f) FROM
   (SELECT status, covered_from, covered_until, gap_time, source, observed_at
    FROM funding_coverage WHERE symbol='BTC_USDT') f),
 'paper_intents', (SELECT count(*) FROM paper_intent),
 'paper_verdicts', (SELECT count(*) FROM paper_verdict),
 'paper_orders', (SELECT count(*) FROM paper_order),
 'paper_fills', (SELECT count(*) FROM paper_fill),
 'paper_ledger', (SELECT count(*) FROM paper_ledger),
 'unknown_orders', (SELECT count(*) FROM paper_order WHERE status='UNKNOWN'),
 'bot_decisions', (SELECT count(*) FROM bot_decision),
 'queued_alerts', (SELECT count(*) FROM outbox_event
   WHERE topic='alert.telegram' AND published_at IS NULL)
)::text;"""


def utc():
    return datetime.now(UTC).isoformat()


def get(path):
    try:
        with urlopen(f"{BASE}/{path}", timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, {}
    except (URLError, TimeoutError, ConnectionError):
        return 0, {}


def db_state():
    result = subprocess.run(
        COMPOSE
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1',
        ],
        input=SQL.encode(),
        capture_output=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout.decode().strip())


def resources():
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
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


def containers():
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=ai-futures",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = {}
    for name in result.stdout.splitlines():
        item = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        state = json.loads(item.stdout)
        rows[name] = {
            "status": state.get("Status"),
            "started_at": state.get("StartedAt"),
            "restart_count": state.get("RestartCount", 0),
            "health": (state.get("Health") or {}).get("Status"),
        }
    return rows


def sample():
    codes = {}
    data = {}
    for name, path in {
        "ready": "health/ready",
        "quality": "markets/BTC_USDT/quality",
        "ticker": "markets/BTC_USDT/ticker",
        "depth": "markets/BTC_USDT/depth",
        "funding": "markets/BTC_USDT/funding",
        "rules": "markets",
    }.items():
        codes[name], data[name] = get(path)
    ticker = data["ticker"]
    rules = (
        next((r for r in data["rules"] if r.get("symbol") == "BTC_USDT"), {})
        if isinstance(data["rules"], list)
        else {}
    )
    now = datetime.now(UTC)

    def age(snapshot):
        try:
            return round((now - datetime.fromisoformat(snapshot["source_time"])).total_seconds(), 3)
        except (KeyError, ValueError, TypeError):
            return None

    result = {
        "at_utc": now.isoformat(),
        "http_status": codes,
        "dependencies": data["ready"].get("dependencies", {}),
        "quality": data["quality"],
        "ticker": {
            "source": ticker.get("source"),
            "source_time": ticker.get("source_time"),
            "age_seconds": age(ticker),
            "status": ticker.get("quality_status"),
        },
        "depth": {
            "source": data["depth"].get("source"),
            "source_time": data["depth"].get("source_time"),
            "age_seconds": age(data["depth"]),
            "status": data["depth"].get("quality_status"),
            "version": data["depth"].get("data", {}).get("version"),
        },
        "funding": {
            "source": data["funding"].get("source"),
            "source_time": data["funding"].get("source_time"),
            "age_seconds": age(data["funding"]),
            "history_source_time": (data["funding"].get("history") or {}).get("source_time"),
        },
        "rules": {
            "version": rules.get("rules_version"),
            "received_at": rules.get("received_at"),
            "state": rules.get("rules", {}).get("state"),
        },
    }
    for key, fn in (("db", db_state), ("resources", resources), ("containers", containers)):
        try:
            result[key] = fn()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
        ) as exc:
            result[key] = {"error": type(exc).__name__}
    return result


def main(seconds, interval, output):
    if seconds != 1800 or interval != 60:
        raise ValueError("This evidence run requires exactly 1800 seconds and 60-second sampling")
    state_code, state = get("state")
    if state_code != 200 or state.get("mode") != "paper":
        raise RuntimeError("Local API must be available in Paper mode")
    started = time.monotonic()
    start_utc = utc()
    samples = []
    deadline = started + seconds
    with open(output, "w", encoding="utf-8") as stream:
        while True:
            row = sample()
            samples.append(row)
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            next_at = started + min(len(samples) * interval, seconds)
            time.sleep(max(0, next_at - time.monotonic()))
    first, last = samples[0], samples[-1]
    counters = Counter()
    for row in samples:
        if row["http_status"].get("ready") != 200:
            counters["readiness_failure_samples"] += 1
        if row["quality"].get("status") != "ok":
            counters["market_non_ok_samples"] += 1
        for reason in row["quality"].get("reasons", []):
            counters[f"quality_reason:{reason}"] += 1
        for code in row["http_status"].values():
            if code != 200:
                counters[f"http_status:{code}"] += 1
        for key in ("db", "resources", "containers"):
            if "error" in row[key]:
                counters[f"{key}_error_samples"] += 1
    count_keys = (
        "final_candles",
        "funding_settlements",
        "paper_intents",
        "paper_verdicts",
        "paper_orders",
        "paper_fills",
        "paper_ledger",
        "unknown_orders",
        "bot_decisions",
        "queued_alerts",
    )
    delta = (
        {key: last["db"].get(key, 0) - first["db"].get(key, 0) for key in count_keys}
        if "error" not in first["db"] and "error" not in last["db"]
        else {}
    )
    return {
        "start_utc": start_utc,
        "end_utc": utc(),
        "actual_duration_seconds": round(time.monotonic() - started, 3),
        "target_seconds": seconds,
        "interval_seconds": interval,
        "config_version": state.get("config_version"),
        "initial_kill_switch": state.get("kill_switch"),
        "sample_count": len(samples),
        "event_deltas": delta,
        "error_counts": dict(counters),
        "sample_jsonl": output,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(main(args.seconds, args.interval, args.output), sort_keys=True))
