"""Sparse GKG v3 budget and one-attempt behavior with mock HTTP/disposable SQLite."""

import asyncio
import hashlib
import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_sparse import (
    BUDGET_VERSION,
    PRIOR_NEWS_BYTES,
    REAL_JOURNAL_PATH,
    RESERVE_BYTES,
    SparseGkgCollector,
    validate_anchor,
)
from ai_futures.gdelt_gkg_sparse_models import SparseGkgAttempt, SparseGkgBudget, SparseGkgResult
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

ANCHOR = datetime(2026, 10, 19, 8, tzinfo=UTC)
BATCH = "20261019074500"


def test_real_journal_is_fixed_under_ignored_repo_data():
    repo = Path(__file__).resolve().parents[2]
    assert REAL_JOURNAL_PATH == repo / "data" / "research" / "s11-gkg-sparse.sqlite"


def archive(rows=None):
    columns = ["x"] * 27
    columns[1] = BATCH
    columns[7] = "ECON_BITCOIN"
    columns[15] = "-0.5,1,2,3,4,5,6"
    columns[4] = "https://private.example/article"
    payload = "\t".join(columns) + "\n"
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as result:
        result.writestr(f"{BATCH}.gkg.csv", rows or payload)
    return target.getvalue()


@pytest.fixture
async def sparse_db(tmp_path):
    path = tmp_path / "sparse.sqlite"
    # Collector requires a disposable path inside the OS temp directory.
    settings = Settings(
        _env_file=None,
        app_env="test",
        gkg_sparse_enabled=True,
        gkg_forward_enabled=False,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{path}",
    )
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/alembic",
        "upgrade",
        "head",
        env=os.environ
        | {
            "APP_ENV": "test",
            "BACKEND_AUTH_TOKEN": "x" * 48,
            "DATABASE_URL": settings.database_url.get_secret_value(),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    database = Database(settings)
    try:
        yield database, settings
    finally:
        await database.close()


def transport(body, *, head_status=200, get_status=200, head_length=None, get_length=None):
    calls = []

    def handle(request):
        calls.append((request.method, str(request.url)))
        assert str(request.url) == f"https://data.gdeltproject.org/gdeltv2/{BATCH}.gkg.csv.zip"
        size = len(body)
        if request.method == "HEAD":
            return httpx.Response(
                head_status,
                headers={"Content-Length": str(head_length if head_length is not None else size)},
            )
        return httpx.Response(
            get_status,
            stream=httpx.ByteStream(body),
            headers={"Content-Length": str(get_length if get_length is not None else size)},
        )

    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize(
    "candidate",
    [ANCHOR - timedelta(minutes=1), ANCHOR + timedelta(hours=1), ANCHOR + timedelta(days=1)],
)
def test_invalid_or_future_anchor(candidate):
    with pytest.raises(ValueError):
        validate_anchor(candidate, ANCHOR)


async def test_success_is_numeric_and_restart_blocks_second_get(sparse_db):
    database, settings = sparse_db
    body = archive()
    mock, calls = transport(body)
    async with httpx.AsyncClient(transport=mock) as client:
        collector = SparseGkgCollector(database, settings, client=client)
        first = await collector.collect_once(ANCHOR, observed_at=ANCHOR + timedelta(minutes=2))
        second = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR + timedelta(minutes=3)
        )
    assert (first.status, second.status) == ("ok", "already_attempted")
    assert [method for method, _ in calls] == ["HEAD", "GET"]
    async with database.sessions() as session:
        budget = await session.get(SparseGkgBudget, 1)
        attempt = (await session.scalars(select(SparseGkgAttempt))).one()
        result = (await session.scalars(select(SparseGkgResult))).one()
    assert budget.prior_bytes == PRIOR_NEWS_BYTES
    assert attempt.reserved_bytes == RESERVE_BYTES
    assert attempt.target_batch_at == ANCHOR - timedelta(minutes=15)
    assert result.total_rows == result.btc_rows == 1
    assert result.mean_btc_document_tone == "-0.5"
    assert result.archive_sha256 == hashlib.sha256(body).hexdigest()
    assert result.first_seen_at == ANCHOR + timedelta(minutes=2)
    assert result.response_bytes >= len(body)
    assert not hasattr(result, "url") and not hasattr(result, "raw_body")
    async with database.sessions() as session:
        with pytest.raises(DatabaseError):
            await session.execute(text("UPDATE sparse_gkg_attempt SET reserved_bytes=0"))
            await session.commit()
        await session.rollback()


@pytest.mark.parametrize(
    ("head_status", "head_length", "expected"),
    [(302, None, "head_http_302"), (200, 8 * 1024 * 1024 + 1, "archive_limit")],
)
async def test_head_rejection_burns_anchor(sparse_db, head_status, head_length, expected):
    database, settings = sparse_db
    mock, calls = transport(archive(), head_status=head_status, head_length=head_length)
    async with httpx.AsyncClient(transport=mock) as client:
        collector = SparseGkgCollector(database, settings, client=client)
        first = await collector.collect_once(ANCHOR, observed_at=ANCHOR)
        second = await collector.collect_once(ANCHOR, observed_at=ANCHOR)
    assert first.status == "error" and first.error_code == expected
    assert second.status == "already_attempted"
    assert [method for method, _ in calls] == ["HEAD"]


async def test_get_length_mismatch_and_bad_zip_are_unknown(sparse_db):
    database, settings = sparse_db
    mock, calls = transport(archive(), get_length=len(archive()) + 1)
    async with httpx.AsyncClient(transport=mock) as client:
        first = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR
        )
    assert first.error_code == "content_length_mismatch"
    assert len(calls) == 2
    async with database.sessions() as session:
        result = (await session.scalars(select(SparseGkgResult))).one()
    assert result.total_rows is None and result.archive_sha256 is None


async def test_get_redirect_does_not_follow_or_retry(sparse_db):
    database, settings = sparse_db
    mock, calls = transport(archive(), get_status=302)
    async with httpx.AsyncClient(transport=mock, follow_redirects=True) as client:
        result = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR
        )
    assert result.status == "error" and result.error_code == "get_http_302"
    assert [method for method, _ in calls] == ["HEAD", "GET"]


async def test_corrupt_zip_consumes_attempt_without_numeric_result(sparse_db):
    database, settings = sparse_db
    mock, calls = transport(b"not a zip")
    async with httpx.AsyncClient(transport=mock) as client:
        result = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR
        )
    assert result.status == "error" and result.error_code == "archive_schema"
    assert [method for method, _ in calls] == ["HEAD", "GET"]
    async with database.sessions() as session:
        stored = (await session.scalars(select(SparseGkgResult))).one()
    assert stored.total_rows is None and stored.archive_sha256 is None


async def test_partial_oversized_body_is_bounded_and_not_retried(sparse_db):
    database, settings = sparse_db
    body = b"x" * 33_000
    mock, calls = transport(body, head_length=100, get_length=100)
    async with httpx.AsyncClient(transport=mock) as client:
        collector = SparseGkgCollector(database, settings, client=client)
        result = await collector.collect_once(ANCHOR, observed_at=ANCHOR)
        repeat = await collector.collect_once(ANCHOR, observed_at=ANCHOR)
    assert result.status == "error" and result.error_code == "archive_limit"
    assert result.response_bytes <= RESERVE_BYTES
    assert repeat.status == "already_attempted"
    assert [method for method, _ in calls] == ["HEAD", "GET"]


async def test_missing_outcome_after_crash_remains_unknown(sparse_db):
    database, settings = sparse_db
    # Model a process killed immediately after committed reservation.
    async with database.sessions() as session, session.begin():
        session.add(
            SparseGkgBudget(
                id=1,
                cap_bytes=5_000_000_000,
                prior_bytes=PRIOR_NEWS_BYTES,
                budget_version=BUDGET_VERSION,
                created_at=ANCHOR,
            )
        )
        from uuid import uuid4

        session.add(
            SparseGkgAttempt(
                attempt_id=uuid4(),
                anchor_at=ANCHOR,
                target_batch_at=ANCHOR - timedelta(minutes=15),
                attempted_at=ANCHOR,
                reserved_bytes=RESERVE_BYTES,
                source_version="gdelt-gkg-sparse-v3",
                config_version=settings.config_version,
            )
        )
    mock, calls = transport(archive())
    async with httpx.AsyncClient(transport=mock) as client:
        result = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR + timedelta(minutes=1)
        )
    assert result.status == "already_attempted" and calls == []
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(SparseGkgResult))
    assert count == 0


async def test_cap_is_checked_before_any_request(sparse_db, monkeypatch):
    database, settings = sparse_db
    import ai_futures.gdelt_gkg_sparse as sparse

    monkeypatch.setattr(sparse, "TOTAL_CAP_BYTES", PRIOR_NEWS_BYTES + RESERVE_BYTES - 1)
    mock, calls = transport(archive())
    async with httpx.AsyncClient(transport=mock) as client:
        result = await SparseGkgCollector(database, settings, client=client).collect_once(
            ANCHOR, observed_at=ANCHOR
        )
    assert result.status == "budget_exhausted" and calls == []
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(SparseGkgAttempt))
    assert count == 0


async def test_default_off_and_non_temp_database_rejected(sparse_db, tmp_path):
    database, settings = sparse_db
    off = settings.model_copy(update={"gkg_sparse_enabled": False})
    with pytest.raises(PermissionError):
        SparseGkgCollector(database, off)
    legacy_on = settings.model_copy(update={"gkg_forward_enabled": True})
    with pytest.raises(PermissionError):
        SparseGkgCollector(database, legacy_on)
    outside = tmp_path / "../outside.sqlite"
    # The true temp-path restriction is enforced in the collector; a missing file also fails.
    invalid = settings.model_copy(
        update={"database_url": SecretStr(f"sqlite+aiosqlite:///{outside}")}
    )
    with pytest.raises(PermissionError):
        SparseGkgCollector(database, invalid)
    with pytest.raises(PermissionError, match="fixed research journal"):
        SparseGkgCollector(database, settings)
