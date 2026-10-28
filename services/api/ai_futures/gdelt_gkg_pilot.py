"""One bounded, test-only GKG research pilot; no application scheduler or trading path."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select

from ai_futures.db import Database
from ai_futures.gdelt_gkg_forward import MAX_ARCHIVE, MAX_MANIFEST, POLL_INTERVAL, PollResult
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.schemas import require_utc

MAX_ATTEMPTS = 18
MAX_TOTAL_ARCHIVE = 128 * 1024 * 1024
DURATION = timedelta(hours=4, minutes=15)
MIN_START_REMAINING_SECONDS = 60


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def wait(self, seconds: float, stop: asyncio.Event) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def wait(self, seconds: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def journal_counts(database: Database) -> dict[str, int | float | None]:
    """Return only bounded numeric coverage and traffic from the disposable journal."""
    async with database.sessions() as session:
        scans = (await session.scalars(select(GkgScan))).all()
        slots = (await session.scalars(select(GkgSlotObservation))).all()
        releases = (await session.scalars(select(GkgReleaseRevision))).all()
    if len(scans) > MAX_ATTEMPTS or len(slots) > MAX_ATTEMPTS * 97:
        raise ValueError("Pilot journal row cap exceeded")
    lags = [(r.first_seen_at - r.batch_at).total_seconds() for r in releases]
    return {
        "scans": len(scans),
        "releases": len(releases),
        "slots_ok": sum(s.status == "ok" for s in slots),
        "slots_unknown": sum(s.status == "unknown" for s in slots),
        "slots_error": sum(s.status == "error" for s in slots),
        "archive_bytes": sum(s.archive_bytes for s in scans),
        "manifest_bytes": sum(s.manifest_bytes for s in scans),
        "max_first_seen_lag_seconds": max(lags) if lags else None,
    }


async def latest_scan_at(database: Database) -> datetime | None:
    async with database.sessions() as session:
        attempted_at = await session.scalar(
            select(GkgScan.attempted_at).order_by(GkgScan.attempted_at.desc()).limit(1)
        )
    return require_utc(attempted_at) if attempted_at is not None else None


async def run_pilot(poller, database: Database, *, clock: Clock, stop: asyncio.Event, emit):
    """Poll at most once per 15m within a 4h15m window, stopping on any error."""
    deadline = clock.monotonic() + DURATION.total_seconds()
    attempts = 0
    last_attempt = None
    download_bytes = 0
    reason = "deadline"
    while attempts < MAX_ATTEMPTS:
        if stop.is_set():
            reason = "interrupted"
            break
        elapsed_now = clock.monotonic()
        if elapsed_now >= deadline:
            break
        db_attempted_at = await latest_scan_at(database)
        elapsed_now = clock.monotonic()
        if elapsed_now >= deadline:
            break
        now = clock.now()
        wait_seconds = max(
            0.0,
            last_attempt + POLL_INTERVAL.total_seconds() - elapsed_now
            if last_attempt is not None
            else 0.0,
            (db_attempted_at + POLL_INTERVAL - now).total_seconds()
            if db_attempted_at is not None
            else 0.0,
        )
        if wait_seconds > 0:
            seconds = min(wait_seconds, deadline - elapsed_now)
            await clock.wait(seconds, stop)
            continue
        # Both bounded HTTP requests finish within 30s; leave room to commit the
        # append-only scan before the hard window closes.
        if deadline - elapsed_now < MIN_START_REMAINING_SECONDS:
            reason = "deadline"
            break
        remaining = MAX_TOTAL_ARCHIVE - download_bytes
        if remaining < MAX_MANIFEST:
            reason = "byte_cap"
            break
        last_attempt = elapsed_now
        attempts += 1
        result: PollResult = await poller.poll_once(
            download_budget_bytes=min(remaining, MAX_ARCHIVE + MAX_MANIFEST)
        )
        download_bytes += result.archive_bytes + result.manifest_bytes
        if download_bytes > MAX_TOTAL_ARCHIVE:
            raise ValueError("Pilot download byte cap exceeded")
        counts = await journal_counts(database)
        emit(
            {
                "attempt": attempts,
                "at_utc": now.isoformat(),
                "status": result.status,
                "error_code": result.error_code,
                "batch_id": result.batch_id,
                "revision": result.revision,
                "archive_bytes_this_attempt": result.archive_bytes,
                "manifest_bytes_this_attempt": result.manifest_bytes,
                "counts": counts,
            }
        )
        if result.status == "error":
            reason = "poll_error"
            break
        if result.status == "throttled":
            reason = "unexpected_throttle"
            break
    else:
        reason = "attempt_cap"
    if stop.is_set() and reason not in {"poll_error", "unexpected_throttle"}:
        reason = "interrupted"
    summary = {
        "ended_at_utc": clock.now().isoformat(),
        "reason": reason,
        "attempts": attempts,
        "counts": await journal_counts(database),
    }
    emit({"summary": summary})
    return summary


def json_line(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)
