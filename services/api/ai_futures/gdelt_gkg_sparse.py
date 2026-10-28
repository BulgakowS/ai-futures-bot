"""Default-off, one-shot sparse GKG v3 research collector on disposable SQLite."""

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_forward import MAX_ARCHIVE, _Failure, parse_archive
from ai_futures.gdelt_gkg_sparse_models import SparseGkgAttempt, SparseGkgBudget, SparseGkgResult
from ai_futures.schemas import require_utc, utc_now

SOURCE_VERSION = "gdelt-gkg-sparse-v3"
BUDGET_VERSION = "gkg-sparse-budget-v2"
TOTAL_CAP_BYTES = 5_000_000_000
# 22,384,251 known old GKG ZIP bytes; the remainder conservatively covers
# prior manifests, DOC/HEAD probes, the codebook and unmeasured overhead.
PRIOR_NEWS_BYTES = 128 * 1024 * 1024
HEADER_ALLOWANCE = 64 * 1024
READ_CHUNK = 16 * 1024
RESERVE_BYTES = MAX_ARCHIVE + HEADER_ALLOWANCE + READ_CHUNK
ANCHOR_LAG = timedelta(minutes=15)
BASE_URL = "https://data.gdeltproject.org/gdeltv2/"
REAL_JOURNAL_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "research" / "s11-gkg-sparse.sqlite"
)


@dataclass(frozen=True)
class SparseResult:
    status: str
    anchor_at: datetime
    target_batch_at: datetime
    attempt_id: str | None = None
    error_code: str | None = None
    response_bytes: int = 0


def validate_anchor(anchor_at: datetime, now: datetime) -> tuple[datetime, str]:
    anchor = require_utc(anchor_at)
    now = require_utc(now)
    if anchor.minute or anchor.second or anchor.microsecond or anchor.hour % 4:
        raise ValueError("anchor must be exactly on a UTC four-hour boundary")
    if not anchor <= now <= anchor + ANCHOR_LAG:
        raise ValueError("anchor must be current and no more than 15 minutes old")
    target = anchor - timedelta(minutes=15)
    return target, target.strftime("%Y%m%d%H%M%S")


def _content_length(response: httpx.Response) -> int:
    value = response.headers.get("content-length")
    if value is None or not value.isascii() or not value.isdecimal():
        raise _Failure("content_length")
    size = int(value)
    if not 0 < size <= MAX_ARCHIVE:
        raise _Failure("archive_limit")
    return size


def _header_size(response: httpx.Response) -> int:
    # Conservative in-process approximation; reservation also allows 64 KiB overhead.
    size = 32 + sum(len(k.encode()) + len(v.encode()) + 4 for k, v in response.headers.items())
    if size > HEADER_ALLOWANCE:
        raise _Failure("header_limit")
    return size


class SparseGkgCollector:
    def __init__(
        self, database: Database, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.database = database
        self.settings = settings
        self.client = client
        if (
            settings.app_env != "test"
            or not settings.gkg_sparse_enabled
            or settings.gkg_forward_enabled
            or settings.gdelt_forward_enabled
        ):
            raise PermissionError("Sparse GKG collection requires test mode and explicit opt-in")
        url = make_url(settings.database_url.get_secret_value())
        if url.drivername != "sqlite+aiosqlite" or not url.database:
            raise PermissionError("Sparse GKG collection requires a SQLite file")
        path = Path(url.database).resolve()
        if not path.is_file():
            raise PermissionError("Sparse GKG collection requires an existing SQLite file")
        if client is None:
            if path != REAL_JOURNAL_PATH:
                raise PermissionError("Real sparse GKG HTTP requires the fixed research journal")
        elif not path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise PermissionError("Injected sparse GKG HTTP requires a temporary test journal")

    async def collect_once(
        self, anchor_at: datetime, *, observed_at: datetime | None = None
    ) -> SparseResult:
        if observed_at is not None and self.settings.app_env != "test":
            raise PermissionError("Observation clock override is test-only")
        now = require_utc(observed_at or utc_now())
        target, batch_id = validate_anchor(anchor_at, now)
        attempt_id = uuid4()
        # BEGIN IMMEDIATE serializes cap and unique-anchor checks across SQLite processes.
        # Commit before HEAD/GET so a crash burns the entire reservation permanently.
        async with self.database.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            budget = (
                (await connection.execute(select(SparseGkgBudget.__table__)))
                .mappings()
                .one_or_none()
            )
            if budget is None:
                await connection.execute(
                    SparseGkgBudget.__table__.insert().values(
                        id=1,
                        cap_bytes=TOTAL_CAP_BYTES,
                        prior_bytes=PRIOR_NEWS_BYTES,
                        budget_version=BUDGET_VERSION,
                        created_at=now,
                    )
                )
            elif (
                budget["cap_bytes"] != TOTAL_CAP_BYTES
                or budget["prior_bytes"] != PRIOR_NEWS_BYTES
                or budget["budget_version"] != BUDGET_VERSION
            ):
                await connection.rollback()
                raise RuntimeError("Sparse GKG budget version mismatch")
            existing = (
                await connection.execute(
                    select(SparseGkgAttempt.attempt_id).where(
                        SparseGkgAttempt.anchor_at == anchor_at
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await connection.rollback()
                return SparseResult("already_attempted", anchor_at, target, str(existing))
            reserved = (
                await connection.execute(
                    select(func.coalesce(func.sum(SparseGkgAttempt.reserved_bytes), 0))
                )
            ).scalar_one()
            if PRIOR_NEWS_BYTES + reserved + RESERVE_BYTES > TOTAL_CAP_BYTES:
                await connection.rollback()
                return SparseResult("budget_exhausted", anchor_at, target)
            await connection.execute(
                SparseGkgAttempt.__table__.insert().values(
                    attempt_id=attempt_id,
                    anchor_at=anchor_at,
                    target_batch_at=target,
                    attempted_at=now,
                    reserved_bytes=RESERVE_BYTES,
                    source_version=SOURCE_VERSION,
                    config_version=self.settings.config_version,
                )
            )
            await connection.commit()

        response_bytes = 0
        archive = bytearray()
        error_code = None
        aggregate = None
        digest = None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        url = f"{BASE_URL}{batch_id}.gkg.csv.zip"
        try:
            async with asyncio.timeout(15):
                head = await client.head(url, follow_redirects=False, timeout=15)
                response_bytes += _header_size(head)
                if head.status_code != 200:
                    raise _Failure(f"head_http_{head.status_code}")
                if head.headers.get("content-encoding", "identity").lower() != "identity":
                    raise _Failure("content_encoding")
                expected = _content_length(head)
                async with client.stream(
                    "GET",
                    url,
                    headers={"Accept-Encoding": "identity"},
                    follow_redirects=False,
                    timeout=15,
                ) as response:
                    response_bytes += _header_size(response)
                    if response_bytes > HEADER_ALLOWANCE:
                        raise _Failure("header_limit")
                    if response.status_code != 200:
                        raise _Failure(f"get_http_{response.status_code}")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _Failure("content_encoding")
                    if _content_length(response) != expected:
                        raise _Failure("content_length_mismatch")
                    async for chunk in response.aiter_raw(chunk_size=READ_CHUNK):
                        response_bytes += len(chunk)
                        if len(archive) + len(chunk) > expected or response_bytes > RESERVE_BYTES:
                            raise _Failure("archive_limit")
                        archive.extend(chunk)
                if len(archive) != expected:
                    raise _Failure("archive_size")
                digest = hashlib.sha256(archive).hexdigest()
                aggregate = parse_archive(bytes(archive), batch_id)
        except _Failure as exc:
            error_code = exc.code
        except (httpx.HTTPError, TimeoutError):
            error_code = "transport"
        finally:
            if self.client is None:
                await client.aclose()
        seen_at = now if observed_at is not None else utc_now()
        status = "error" if error_code else "ok"
        async with self.database.sessions() as session, session.begin():
            session.add(
                SparseGkgResult(
                    result_id=uuid4(),
                    attempt_id=attempt_id,
                    first_seen_at=seen_at,
                    status=status,
                    error_code=error_code,
                    response_bytes=response_bytes,
                    archive_sha256=digest if aggregate is not None else None,
                    total_rows=aggregate.total_rows if aggregate else None,
                    btc_rows=aggregate.btc_rows if aggregate else None,
                    mean_btc_document_tone=aggregate.mean_tone if aggregate else None,
                    source_version=SOURCE_VERSION,
                    config_version=self.settings.config_version,
                )
            )
        return SparseResult(status, anchor_at, target, str(attempt_id), error_code, response_bytes)
