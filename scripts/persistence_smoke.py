"""Read-only state checks plus restart of this project's containers."""

import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

COMPOSE = ["docker", "compose", "-p", "ai-futures"]


def read(path):
    with urlopen(f"http://127.0.0.1:8000/api/v1/{path}", timeout=8) as response:
        return json.load(response)


def ready():
    for attempt in range(30):
        try:
            return read("health/ready")
        except (HTTPError, URLError, ConnectionError):
            if attempt == 29:
                raise
            time.sleep(1)


def counts():
    return subprocess.check_output(
        COMPOSE
        + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "ai_futures",
            "-d",
            "ai_futures",
            "-Atc",
            "SELECT (SELECT count(*) FROM app_state), (SELECT count(*) FROM audit_event), "
            "(SELECT count(*) FROM outbox_event);",
        ],
        text=True,
    ).strip()


before_health = ready()
before = read("state")
before_counts = counts()
subprocess.run(COMPOSE + ["restart"], check=True)
after_health = ready()
after = read("state")
assert before == after, "AppState changed after restart"
assert before_counts == counts() == "1|1|1", "Bootstrap duplicated after restart"
evidence = {
    "before": before,
    "after": after,
    "app_state_audit_outbox_counts": before_counts,
    "health_before": before_health,
    "health_after": after_health,
    "result": "passed",
}
Path("docs/evidence/foundation-2026-10-14.json").write_text(json.dumps(evidence, indent=2) + "\n")
print("PASS: all containers restarted; AppState unchanged; AppState/audit/outbox counts 1/1/1")
