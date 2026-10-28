"""Consistent PostgreSQL dump and invariant check in a disposable database.

The active database is read by pg_dump only. Restore always targets a newly
generated database and is dropped in finally, even when verification fails.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

COMPOSE = ["docker", "compose", "-p", "ai-futures", "exec", "-T", "postgres"]
RESTORE_PREFIX = "s09_restore_"
VERIFY_SQL = """
SELECT json_build_object(
  'revision', (SELECT version_num FROM alembic_version),
  'paper_portfolios', (SELECT count(*) FROM paper_portfolio),
  'paper_orders', (SELECT count(*) FROM paper_order),
  'paper_fills', (SELECT count(*) FROM paper_fill),
  'paper_ledger', (SELECT count(*) FROM paper_ledger),
  'backtest_datasets', (SELECT count(*) FROM backtest_dataset),
  'backtest_runs', (SELECT count(*) FROM backtest_run),
  'unbalanced_ledger', (SELECT count(*) FROM paper_ledger
    WHERE wallet_delta::numeric + offset_delta::numeric <> 0),
  'orphan_paper_orders', (SELECT count(*) FROM paper_order o
    LEFT JOIN paper_verdict v ON v.id = o.verdict_id WHERE v.id IS NULL),
  'orphan_paper_fills', (SELECT count(*) FROM paper_fill f
    LEFT JOIN paper_order o ON o.id = f.order_id WHERE o.id IS NULL),
  'orphan_backtest_runs', (SELECT count(*) FROM backtest_run r
    LEFT JOIN backtest_dataset d ON d.id = r.dataset_id WHERE d.id IS NULL),
  'invalid_backtest_modes', (SELECT count(*) FROM backtest_run WHERE mode <> 'backtest'),
  'unknown_paper_orders', (SELECT count(*) FROM paper_order WHERE status = 'UNKNOWN')
)::text;
"""


def run(stage, *args, stdin=None, stdout=None):
    result = subprocess.run(
        COMPOSE + list(args), stdin=stdin, stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{stage} failed (exit {result.returncode}); inspect container logs")
    return result.stdout


def backup_and_verify(destination: Path):
    destination = destination.resolve()
    repository = Path(__file__).resolve().parents[1]
    if destination.is_relative_to(repository):
        raise ValueError("Backup destination must be outside the repository")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            run(
                "pg_dump", "sh", "-c",
                'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl',
                stdout=stream,
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    restore_name = RESTORE_PREFIX + uuid4().hex[:16]
    created = False
    try:
        run("createdb", "sh", "-c", f'createdb -U "$POSTGRES_USER" {restore_name}')
        created = True
        with destination.open("rb") as stream:
            run(
                "pg_restore", "sh", "-c",
                f'pg_restore -U "$POSTGRES_USER" -d {restore_name} '
                '--exit-on-error --no-owner --no-acl',
                stdin=stream,
            )
        # Run SQL through stdin, preserving the fixed query.
        result = subprocess.run(
            COMPOSE + ["sh", "-c", f'psql -U "$POSTGRES_USER" -d {restore_name} '
                       '-At -v ON_ERROR_STOP=1'],
            input=VERIFY_SQL.encode(), capture_output=True, check=False,
        )
        if result.returncode:
            raise RuntimeError("verification query failed; inspect container logs")
        report = json.loads(result.stdout.decode().strip())
        if report["revision"] != "b7638c3920e4":
            raise RuntimeError("restored backup is not at the S09 schema revision")
        checks = (
            "unbalanced_ledger", "orphan_paper_orders", "orphan_paper_fills",
            "orphan_backtest_runs", "invalid_backtest_modes",
        )
        if any(report[key] != 0 for key in checks):
            raise RuntimeError("restored ledger or references failed invariant checks")
        return {"backup_sha256": digest, "restore": "isolated_pass", "tables": report}
    finally:
        if created:
            run("dropdb", "sh", "-c", f'dropdb -U "$POSTGRES_USER" --force {restore_name}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(backup_and_verify(args.output), sort_keys=True))
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
