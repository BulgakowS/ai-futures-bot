"""Controlled local Compose dependency outages; always restart stopped services."""

import json
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

COMPOSE = ["docker", "compose", "-p", "ai-futures"]
READY = "http://127.0.0.1:8000/api/v1/health/ready"
STATE = "http://127.0.0.1:8000/api/v1/state"


def compose(*args):
    result = subprocess.run(COMPOSE + list(args), capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Compose {args[0]} failed")
    return result.stdout.decode().strip()


def request(url):
    try:
        with urlopen(url, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)
    except (URLError, TimeoutError, ConnectionError):
        return 0, {}


def until(predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = request(READY)
        if predicate(*state):
            return state
        time.sleep(1)
    raise RuntimeError("Expected health transition did not occur")


def counts():
    value = compose(
        "exec", "-T", "postgres", "sh", "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
        '"SELECT (SELECT count(*) FROM paper_order), '
        '(SELECT count(*) FROM paper_fill), (SELECT count(*) FROM paper_ledger), '
        '(SELECT count(*) FROM backtest_run);"',
    )
    return [int(part) for part in value.split("|")]


def main():
    status, initial = request(READY)
    if status != 200 or initial.get("mode") != "paper":
        raise RuntimeError("Paper readiness must be healthy before fault injection")
    _, state = request(STATE)
    if state.get("kill_switch") is not True:
        raise RuntimeError("Paper kill switch must be on before fault injection")
    before = counts()
    results = {}
    for service, dependency, delay in (
        ("redis", "redis", 45),
        ("postgres", "database", 45),
        ("api", None, 15),
        ("trading", "trading", 45),
    ):
        compose("stop", service)
        try:
            if dependency:
                observed_status, degraded = until(
                    lambda code, body, key=dependency: code == 503
                    and body.get("dependencies", {}).get(key) != "ok",
                    delay,
                )
                results[service] = {
                    "http_status": observed_status,
                    "dependency": degraded["dependencies"][dependency],
                }
            else:
                until(lambda code, _: code == 0, delay)
                results[service] = {"http_status": 0}
        finally:
            compose("start", service)
            until(lambda code, body: code == 200 and body.get("status") == "ok", 90)
    after = counts()
    return {"faults": results, "counts_before": before, "counts_after": after,
            "recovered": True, "kill_switch": state["kill_switch"]}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
