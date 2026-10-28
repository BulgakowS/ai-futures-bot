"""N1a synthetic-only announcement journal and coverage contracts."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.news import SOURCE, AnnouncementJournal, parse_announcement
from ai_futures.news_models import (
    AnnouncementCoverage,
    AnnouncementCoverageObservation,
    AnnouncementVersion,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

OBSERVED = datetime(2026, 10, 17, 12, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 9, 9, tzinfo=UTC)


def announcement(slug="btc-market-update", title="BTC futures update", post_time=PUBLISHED):
    return {
        "title": title,
        "url": f"https://www.mexc.com/help/{slug}",
        "language": "en-US",
        "postTime": int(post_time.timestamp() * 1000),
    }


@pytest.fixture
async def news_db(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        news_fixture_ingest_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'news.db'}",
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
    output = await process.communicate()
    assert process.returncode == 0, output
    database = Database(settings)
    yield database, settings
    await database.close()


async def test_versions_are_append_only_late_and_restart_safe(news_db):
    database, settings = news_db
    journal = AnnouncementJournal(database, settings)
    first = announcement()
    result = await journal.ingest_page(
        [first],
        page=1,
        total_pages=3,
        scan_id="scan-1",
        fetched_at=OBSERVED,
        observation_kind="backfill",
    )
    assert result.inserted == 1 and result.deduped == 0
    assert (
        await journal.ingest_page(
            [first],
            page=1,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=1),
        )
    ).inserted == 0
    edit = announcement(title="BTC futures rules update")
    assert (
        await journal.ingest_page(
            [edit],
            page=1,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=2),
        )
    ).inserted == 1
    assert (
        await journal.ingest_page(
            [first],
            page=1,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=3),
        )
    ).inserted == 1  # Reverting content is another observed version.
    late = announcement("old-btc-notice", "Late BTC notice", PUBLISHED - timedelta(days=300))
    assert (
        await journal.ingest_page(
            [late],
            page=2,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=4),
            observation_kind="backfill",
        )
    ).inserted == 1
    async with database.sessions() as session:
        versions = (
            await session.scalars(
                select(AnnouncementVersion).order_by(
                    AnnouncementVersion.canonical_url, AnnouncementVersion.version_number
                )
            )
        ).all()
        assert len(versions) == 4
        btc = [row for row in versions if row.canonical_url.endswith("btc-market-update")]
        assert [row.version_number for row in btc] == [1, 2, 3]
        assert [row.first_seen_at for row in btc] == [
            OBSERVED,
            OBSERVED + timedelta(seconds=2),
            OBSERVED + timedelta(seconds=3),
        ]
        assert all(row.available_at == row.first_seen_at for row in versions)
        assert all(row.post_time < row.available_at for row in versions)
        assert versions[0].observation_kind == "backfill"
        assert len({row.version_id for row in versions}) == 4

    restarted = Database(settings)
    try:
        replay = await AnnouncementJournal(restarted, settings).ingest_page(
            [first],
            page=1,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=5),
        )
        assert replay.inserted == 0 and replay.deduped == 1
        async with restarted.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 4
        with pytest.raises(IntegrityError):
            async with restarted.sessions.begin() as session:
                await session.execute(text("UPDATE announcement_version SET title='changed'"))
        with pytest.raises(IntegrityError):
            async with restarted.sessions.begin() as session:
                await session.execute(text("DELETE FROM announcement_version"))
        future = OBSERVED + timedelta(minutes=2)
        await AnnouncementJournal(restarted, settings).ingest_page(
            [announcement("scheduled-notice", "Scheduled notice", future)],
            page=3,
            total_pages=3,
            scan_id="scan-1",
            fetched_at=OBSERVED + timedelta(seconds=6),
        )
        async with restarted.sessions() as session:
            row = await session.scalar(
                select(AnnouncementVersion).where(
                    AnnouncementVersion.canonical_url.endswith("scheduled-notice")
                )
            )
            assert row.first_seen_at == OBSERVED + timedelta(seconds=6)
            assert row.available_at == row.post_time == future
    finally:
        await restarted.close()


async def test_coverage_gap_shift_rate_limit_and_atomic_invalid_page(news_db):
    database, settings = news_db
    journal = AnnouncementJournal(database, settings)
    first = announcement()
    other = announcement("futures-system", "Futures system maintenance")
    await journal.ingest_page([first], page=1, total_pages=3, scan_id="scan-A", fetched_at=OBSERVED)
    await journal.ingest_page(
        [other],
        page=3,
        total_pages=3,
        scan_id="scan-A",
        fetched_at=OBSERVED + timedelta(seconds=1),
    )
    async with database.sessions() as session:
        row = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        assert row.status == "partial" and row.seen_pages == [1, 3]
        assert row.internal_gap_pages == [2] and row.unseen_page_count == 1
    changed = announcement(title="BTC changed headline")
    result = await journal.ingest_page(
        [changed],
        page=1,
        total_pages=3,
        scan_id="scan-A",
        fetched_at=OBSERVED + timedelta(seconds=2),
    )
    assert result.inserted == 1 and result.coverage_status == "shifted"
    await journal.record_failure(
        scan_id="scan-A", status_code=429, observed_at=OBSERVED + timedelta(seconds=3)
    )
    async with database.sessions() as session:
        row = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        assert row.status == "rate_limited" and row.last_error_code == 429
        assert row.last_success_at == OBSERVED + timedelta(seconds=2)
        assert row.page_shift_detected and row.internal_gap_pages == [2]
    await journal.record_failure(
        scan_id="scan-A", status_code=503, observed_at=OBSERVED + timedelta(seconds=4)
    )
    await journal.ingest_page(
        [other],
        page=2,
        total_pages=4,
        scan_id="scan-A",
        fetched_at=OBSERVED + timedelta(seconds=5),
    )
    async with database.sessions() as session:
        row = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        assert row.status == "shifted" and row.internal_gap_pages == []
        assert row.unseen_page_count == 1 and row.last_error_code is None
        before = await session.scalar(select(func.count()).select_from(AnnouncementVersion))
        observations_before = await session.scalar(
            select(func.count()).select_from(AnnouncementCoverageObservation)
        )
    with pytest.raises(ValueError):
        await journal.ingest_page(
            [announcement("valid-new", "Valid new"), {**first, "url": "https://evil.test/help/x"}],
            page=1,
            total_pages=4,
            scan_id="scan-B",
            fetched_at=OBSERVED + timedelta(seconds=6),
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == before
        assert (
            await session.scalar(select(func.count()).select_from(AnnouncementCoverageObservation))
            == observations_before
        )
        row = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        assert row.scan_id == "scan-A" and row.last_attempt_at == OBSERVED + timedelta(seconds=5)
    await journal.ingest_page(
        [first],
        page=1,
        total_pages=4,
        scan_id="scan-B",
        fetched_at=OBSERVED + timedelta(seconds=7),
    )
    async with database.sessions() as session:
        row = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        assert row.scan_id == "scan-B" and row.status == "partial"
        assert row.seen_pages == [1] and row.unseen_page_count == 3
        assert row.page_shift_detected is False


async def test_default_off_rejects_fixture_writes(news_db):
    database, settings = news_db
    default_settings = Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=settings.database_url.get_secret_value(),
    )
    assert default_settings.news_fixture_ingest_enabled is False
    disabled = settings.model_copy(update={"news_fixture_ingest_enabled": False})
    with pytest.raises(PermissionError):
        await AnnouncementJournal(database, disabled).ingest_page(
            [announcement()], page=1, total_pages=1, scan_id="disabled", fetched_at=OBSERVED
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 0
    with pytest.raises(ValueError, match="test-only"):
        Settings(
            _env_file=None,
            app_env="local",
            news_fixture_ingest_enabled=True,
            backend_auth_token="x" * 48,
            database_url="postgresql+asyncpg://localhost/test",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"title": "  "},
        {"language": "zh-CN"},
        {"postTime": 1.5},
        {"postTime": int((OBSERVED + timedelta(days=1)).timestamp() * 1000)},
        {"url": "http://www.mexc.com/help/x"},
        {"url": "https://www.mexc.com.evil.test/help/x"},
    ],
)
def test_parser_rejects_invalid_fixture(change):
    with pytest.raises(ValueError):
        parse_announcement(announcement() | change, OBSERVED)


def test_parser_canonical_url_and_future_availability():
    future = OBSERVED + timedelta(minutes=2)
    parsed = parse_announcement(
        announcement(post_time=future)
        | {
            "url": "https://www.mexc.com/help/btc-market-update/?tracking=1#section",
            "title": " BTC   futures  update ",
        },
        OBSERVED,
    )
    assert parsed.canonical_url == "https://www.mexc.com/help/btc-market-update"
    assert parsed.title == "BTC futures update" and parsed.post_time == future
