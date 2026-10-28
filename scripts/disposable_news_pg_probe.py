import asyncio
import getpass
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.news import AnnouncementJournal
from ai_futures.news_models import AnnouncementCoverageObservation, AnnouncementVersion
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError


async def verify(url):
    settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=url,
        news_fixture_ingest_enabled=True,
    )
    database = Database(settings)
    try:
        now = datetime.now(UTC).replace(microsecond=0)
        raw = {
            "title": "Synthetic BTC futures update",
            "url": "https://www.mexc.com/help/n2b-pg-fixture",
            "language": "en-US",
            "postTime": int((now - timedelta(minutes=1)).timestamp() * 1000),
        }
        journal = AnnouncementJournal(database, settings)
        await journal.ingest_page([raw], page=1, total_pages=1, scan_id="pg-base", fetched_at=now)
        for statement in (
            "UPDATE announcement_coverage_observation SET status='error'",
            "DELETE FROM announcement_coverage_observation",
        ):
            try:
                async with database.sessions.begin() as session:
                    await session.execute(text(statement))
            except DBAPIError:
                print("pg_trigger_rejected", statement.split()[0])
            else:
                raise AssertionError("PostgreSQL immutable trigger did not reject mutation")
        edit = raw | {"title": "Synthetic BTC edited update"}
        outcomes = await asyncio.gather(
            journal.ingest_page(
                [edit],
                page=1,
                total_pages=1,
                scan_id="pg-concurrent",
                fetched_at=now + timedelta(seconds=1),
            ),
            journal.ingest_page(
                [edit],
                page=1,
                total_pages=1,
                scan_id="pg-concurrent",
                fetched_at=now + timedelta(seconds=2),
            ),
            return_exceptions=True,
        )
        print("pg_concurrent_outcomes", [type(item).__name__ for item in outcomes])
        async with database.sessions() as session:
            versions = await session.scalar(select(func.count()).select_from(AnnouncementVersion))
            observations = await session.scalar(
                select(func.count()).select_from(AnnouncementCoverageObservation)
            )
            print("pg_counts", versions, observations)
            assert versions == 2 and observations >= 2
    finally:
        await database.close()


with tempfile.TemporaryDirectory(prefix="n2b-disposable-pg-") as folder:
    root = Path(folder)
    data = root / "data"
    sockets = root / "sockets"
    sockets.mkdir()
    log = root / "postgres.log"
    init = subprocess.run(
        ["initdb", "-D", str(data), "-A", "trust", "--no-instructions"],
        capture_output=True,
        text=True,
    )
    print("pg_initdb", init.returncode)
    if init.returncode:
        print((init.stdout + init.stderr)[-1000:])
        raise SystemExit(init.returncode)
    started = False
    try:
        start = subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(data),
                "-l",
                str(log),
                "-o",
                f"-F -c listen_addresses='' -k {sockets} -p 5432",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
        )
        print("pg_start", start.returncode)
        if start.returncode:
            print((start.stdout + start.stderr + log.read_text())[-1500:])
            raise SystemExit(start.returncode)
        started = True
        url = f"postgresql+asyncpg://{getpass.getuser()}@/postgres?host={sockets}"
        env = os.environ | {
            "APP_ENV": "test",
            "BACKEND_AUTH_TOKEN": "x" * 48,
            "DATABASE_URL": url,
            "NEWS_FIXTURE_INGEST_ENABLED": "true",
        }
        migration = subprocess.run(
            [".venv/bin/alembic", "upgrade", "head"],
            env=env,
            capture_output=True,
            text=True,
        )
        print("pg_migration", migration.returncode)
        if migration.returncode:
            print((migration.stdout + migration.stderr)[-2000:])
            raise SystemExit(migration.returncode)
        asyncio.run(verify(url))
    finally:
        if started:
            stop = subprocess.run(
                ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                text=True,
            )
            print("pg_stop", stop.returncode)
