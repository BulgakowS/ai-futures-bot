"""Explicit one-shot forward collection of GDELT-owned BTC aggregate volume only."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import httpx
from sqlalchemy import select, text

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_models import GdeltVolumeRevision, GdeltVolumeScan
from ai_futures.schemas import require_utc, utc_now

SOURCE_VERSION = "gdelt-doc-timelinevol-bitcoin-v1"
URL = "https://api.gdeltproject.org/api/v2/doc/doc"
PARAMS = {"query": "bitcoin", "mode": "timelinevol", "format": "json", "timespan": "1d"}
MAX_BODY_BYTES = 256_000
MAX_POINTS = 96
POLL_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True)
class VolumePoint:
    point_at: datetime
    value: str


@dataclass(frozen=True)
class PollResult:
    status: str
    scan_id: str | None
    points: int = 0
    inserted: int = 0
    revised: int = 0
    missing_buckets: int = 0
    error_code: str | None = None


def parse_volume(body: bytes, observed_at: datetime) -> tuple[VolumePoint, ...]:
    """Reject all schemas except the fixed aggregate series; never persist response text."""
    now = require_utc(observed_at)
    try:
        data = json.loads(body, parse_float=Decimal)
        if not isinstance(data, dict) or set(data) != {"query_details", "timeline"}:
            raise ValueError
        details = data["query_details"]
        if (
            not isinstance(details, dict)
            or set(details) != {"title", "date_resolution"}
            or details.get("title") != "bitcoin"
            or details.get("date_resolution") != "15m"
        ):
            raise ValueError
        timeline = data["timeline"]
        if not isinstance(timeline, list) or len(timeline) != 1:
            raise ValueError
        series = timeline[0]
        if (
            not isinstance(series, dict)
            or set(series) != {"series", "data"}
            or series["series"] != "Volume Intensity"
        ):
            raise ValueError
        rows = series["data"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_POINTS:
            raise ValueError
        parsed = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"date", "value"}:
                raise ValueError
            stamp = datetime.strptime(row["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if (
                stamp in seen
                or stamp.minute % 15
                or stamp.second
                or stamp < now - timedelta(hours=25)
                or stamp > now + timedelta(minutes=15)
            ):
                raise ValueError
            seen.add(stamp)
            raw = row["value"]
            if isinstance(raw, bool) or not isinstance(raw, int | Decimal):
                raise ValueError
            value = Decimal(raw)
            if not value.is_finite() or value < 0 or value > 100 or value.as_tuple().exponent < -6:
                raise ValueError
            parsed.append(VolumePoint(stamp, format(value.normalize(), "f")))
        if [p.point_at for p in parsed] != sorted(p.point_at for p in parsed):
            raise ValueError
        return tuple(parsed)
    except (TypeError, KeyError, ValueError, InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid GDELT aggregate response") from exc


class GdeltForwardPoller:
    def __init__(
        self, database: Database, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.database = database
        self.settings = settings
        self.client = client

    async def poll_once(self, *, observed_at: datetime | None = None) -> PollResult:
        if not self.settings.gdelt_forward_enabled:
            raise PermissionError("GDELT forward collection is disabled")
        now = require_utc(observed_at or utc_now())
        if self.settings.app_env != "test" and observed_at is not None:
            raise PermissionError("Production observation clock cannot be overridden")
        async with self.database.sessions() as session:
            last = await session.scalar(
                select(GdeltVolumeScan.attempted_at)
                .order_by(GdeltVolumeScan.attempted_at.desc())
                .limit(1)
            )
        if last is not None and now - require_utc(last) < POLL_INTERVAL:
            return PollResult("throttled", None)

        scan_id = uuid4()
        client = self.client or httpx.AsyncClient(timeout=8, follow_redirects=False)
        try:
            async with client.stream("GET", URL, params=PARAMS) as response:
                if response.status_code != 200:
                    raise _PollFailure(f"http_{response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise _PollFailure("body_limit")
            points = parse_volume(bytes(body), now)
        except _PollFailure as exc:
            return await self._failed(scan_id, now, exc.code)
        except (httpx.HTTPError, ValueError):
            return await self._failed(scan_id, now, "transport_or_schema")
        finally:
            if self.client is None:
                await client.aclose()

        # Count the fixed trailing 24h grid, not just returned rows; absent buckets stay unknown.
        newest_bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        expected = {newest_bucket - timedelta(minutes=15 * i) for i in range(96)}
        missing = len(expected - {point.point_at for point in points})
        nonzero = sum(Decimal(point.value) > 0 for point in points)
        inserted = revised = 0
        async with self.database.sessions() as session, session.begin():
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(1128713294)"))
            last = await session.scalar(
                select(GdeltVolumeScan.attempted_at)
                .order_by(GdeltVolumeScan.attempted_at.desc())
                .limit(1)
            )
            if last is not None and now - require_utc(last) < POLL_INTERVAL:
                return PollResult("throttled", None)
            for point in points:
                previous = await session.scalar(
                    select(GdeltVolumeRevision)
                    .where(GdeltVolumeRevision.point_at == point.point_at)
                    .order_by(GdeltVolumeRevision.revision.desc())
                    .limit(1)
                )
                if previous is not None and previous.value == point.value:
                    continue
                session.add(
                    GdeltVolumeRevision(
                        revision_id=uuid4(),
                        scan_id=scan_id,
                        point_at=point.point_at,
                        first_seen_at=previous.first_seen_at if previous else now,
                        observed_at=now,
                        revision=(previous.revision + 1) if previous else 1,
                        value=point.value,
                        source_version=SOURCE_VERSION,
                        config_version=self.settings.config_version,
                    )
                )
                inserted += 1
                revised += previous is not None
            session.add(
                GdeltVolumeScan(
                    scan_id=scan_id,
                    attempted_at=now,
                    status="gap" if missing else "ok",
                    point_count=len(points),
                    nonzero_count=nonzero,
                    missing_bucket_count=missing,
                    revised_count=revised,
                    inserted_count=inserted,
                    error_code=None,
                    source_version=SOURCE_VERSION,
                    config_version=self.settings.config_version,
                )
            )
        return PollResult(
            "gap" if missing else "ok", str(scan_id), len(points), inserted, revised, missing
        )

    async def _failed(self, scan_id, now, code):
        async with self.database.sessions() as session, session.begin():
            session.add(
                GdeltVolumeScan(
                    scan_id=scan_id,
                    attempted_at=now,
                    status="error",
                    point_count=0,
                    nonzero_count=0,
                    missing_bucket_count=96,
                    revised_count=0,
                    inserted_count=0,
                    error_code=code,
                    source_version=SOURCE_VERSION,
                    config_version=self.settings.config_version,
                )
            )
        return PollResult("error", str(scan_id), missing_buckets=96, error_code=code)


class _PollFailure(Exception):
    def __init__(self, code: str):
        self.code = code
