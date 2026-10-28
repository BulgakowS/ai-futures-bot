"""Read-only localhost cache sampling; never queries MEXC or mutates Paper state."""

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

BASE = "http://127.0.0.1:8000/api/v1/markets/BTC_USDT"


def fetch(path):
    with urlopen(f"{BASE}/{path}", timeout=3) as response:
        return json.load(response)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)], 3)


def main(seconds, interval, output):
    started = time.monotonic()
    rows = []
    with Path(output).open("w", encoding="utf-8") as stream:
        while True:
            now = datetime.now(UTC)
            try:
                quality, ticker = fetch("quality"), fetch("ticker")
                source_time = ticker.get("source_time")
                age = (
                    (now - datetime.fromisoformat(source_time)).total_seconds()
                    if source_time else None
                )
                row = {
                    "at_utc": now.isoformat(), "quality": quality.get("status"),
                    "reasons": quality.get("reasons", []), "ticker_source": ticker.get("source"),
                    "source_time": source_time, "ticker_age_seconds": round(age, 3)
                    if age is not None else None,
                }
            except (OSError, ValueError, TimeoutError) as exc:
                row = {"at_utc": now.isoformat(), "error": type(exc).__name__}
            rows.append(row)
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    ages = [row["ticker_age_seconds"] for row in rows if row.get("ticker_age_seconds") is not None]
    source_times = sorted({datetime.fromisoformat(row["source_time"]) for row in rows
                           if row.get("source_time")})
    steps = [(new - old).total_seconds() for old, new in zip(
        source_times, source_times[1:], strict=False
    )]
    return {
        "start_utc": rows[0]["at_utc"], "end_utc": rows[-1]["at_utc"],
        "duration_seconds": round(time.monotonic() - started, 3), "interval_seconds": interval,
        "sample_count": len(rows), "error_count": sum("error" in row for row in rows),
        "ticker_stale_samples": sum("ticker_stale" in row.get("reasons", []) for row in rows),
        "market_non_ok_samples": sum(row.get("quality") != "ok" for row in rows),
        "age_seconds": {"p50": percentile(ages, .5), "p95": percentile(ages, .95),
                        "p99": percentile(ages, .99), "max": max(ages, default=None)},
        "source_step_seconds": {"p50": percentile(steps, .5), "p95": percentile(steps, .95),
                                "p99": percentile(steps, .99), "max": max(steps, default=None)},
        "jsonl": output,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(main(args.seconds, args.interval, args.output), sort_keys=True))
