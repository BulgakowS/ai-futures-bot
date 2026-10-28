"""N1b mock-only transport and bounded polling failure contracts."""

import httpx
import pytest
from ai_futures.news import SOURCE, AnnouncementJournal
from ai_futures.news_models import (
    AnnouncementCoverage,
    AnnouncementCoverageObservation,
    AnnouncementVersion,
)
from ai_futures.news_transport import (
    MAX_ATTEMPTS,
    MAX_PAGES,
    AnnouncementPoller,
    MockAnnouncementTransport,
)
from ai_futures.paper_models import PaperOrder
from ai_futures.schemas import utc_now
from sqlalchemy import func, select

from tests.integration.test_news import PUBLISHED, announcement, news_db  # noqa: F401


def reply(rows, pages=3):
    return httpx.Response(200, json={"code": 0, "data": {"totalPage": pages, "rows": rows}})


class Clock:
    def __init__(self):
        self.value = 100.0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


async def test_default_off_makes_no_http_or_journal_write(news_db):  # noqa: F811
    database, settings = news_db
    disabled = settings.model_copy(update={"news_fixture_ingest_enabled": False})
    calls = []

    def respond(request):
        calls.append(request)
        return reply([announcement()])

    transport = MockAnnouncementTransport(disabled, httpx.MockTransport(respond))
    try:
        with pytest.raises(PermissionError, match="hard-disabled"):
            await AnnouncementPoller(AnnouncementJournal(database, disabled), transport).poll_once()
        assert calls == [] and transport.attempts == 0
        with pytest.raises(TypeError, match="MockTransport"):
            MockAnnouncementTransport(settings, httpx.AsyncHTTPTransport())
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 0
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await transport.close()


async def test_bounded_pages_shift_edit_restart_and_first_seen(news_db):  # noqa: F811
    database, settings = news_db
    clock = Clock()
    calls = []
    first = announcement("first", "BTC first version")
    edited = announcement("first", "BTC edited version")
    second = announcement("second", "Second title")
    third = announcement("third", "Third title")
    page_one_reads = 0

    def respond(request):
        nonlocal page_one_reads
        assert request.method == "GET" and request.url.host == "api.mexc.com"
        assert request.url.path == "/api/v3/announcements"
        assert request.url.params["language"] == "en-US"
        assert request.url.params["limit"] == "100"
        page = int(request.url.params["page"])
        calls.append((page, clock.now()))
        if page == 1:
            page_one_reads += 1
            return reply([first if page_one_reads == 1 else edited], pages=20)
        return reply([second if page == 2 else third], pages=20)

    transport = MockAnnouncementTransport(
        settings, httpx.MockTransport(respond), monotonic=clock.now, sleep=clock.sleep
    )
    try:
        before = utc_now()
        result = await AnnouncementPoller(
            AnnouncementJournal(database, settings), transport
        ).poll_once(scan_id="scan-shift")
        assert result.status == "shifted"
        assert result.pages_seen == (1, 2, 3) and result.inserted == 4
        assert result.http_attempts == 4 and [page for page, _ in calls] == [1, 2, 3, 1]
        assert all(
            later - earlier >= 0.5
            for (_, earlier), (_, later) in zip(calls, calls[1:], strict=False)
        )
        async with database.sessions() as session:
            versions = (await session.scalars(select(AnnouncementVersion))).all()
            coverage = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
            assert len(versions) == 4
            assert sorted(
                v.version_number for v in versions if v.canonical_url.endswith("/first")
            ) == [1, 2]
            assert all(
                v.first_seen_at >= before and v.available_at == v.first_seen_at for v in versions
            )
            assert all(v.post_time == PUBLISHED for v in versions)
            assert coverage.page_shift_detected and coverage.status == "shifted"
            assert coverage.seen_pages == [1, 2, 3] and coverage.unseen_page_count == 17
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await transport.close()

    restart = MockAnnouncementTransport(
        settings,
        httpx.MockTransport(
            lambda request: reply(
                [edited if request.url.params["page"] == "1" else second], pages=2
            )
        ),
        monotonic=clock.now,
        sleep=clock.sleep,
    )
    try:
        replay = await AnnouncementPoller(
            AnnouncementJournal(database, settings), restart
        ).poll_once(scan_id="scan-restart")
        assert replay.inserted == 0 and replay.pages_seen == (1, 2)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 4
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await restart.close()


@pytest.mark.parametrize("status", [429, 503])
async def test_transient_http_failure_retries_once(news_db, status):  # noqa: F811
    database, settings = news_db
    clock = Clock()
    calls = []

    def respond(request):
        calls.append(clock.now())
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": "1"})
        return reply([announcement()], pages=1)

    transport = MockAnnouncementTransport(
        settings, httpx.MockTransport(respond), monotonic=clock.now, sleep=clock.sleep
    )
    try:
        result = await AnnouncementPoller(
            AnnouncementJournal(database, settings), transport
        ).poll_once(scan_id="transient-retry")
        assert result.status == "ok" and result.inserted == 1
        assert result.http_attempts == 3 and len(calls) == 3
        assert calls[1] - calls[0] >= 1
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 1
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_failure_preserves_last_success(news_db, status):  # noqa: F811
    database, settings = news_db
    journal = AnnouncementJournal(database, settings)
    clock = Clock()
    first = MockAnnouncementTransport(
        settings,
        httpx.MockTransport(lambda _: reply([announcement()])),
        monotonic=clock.now,
        sleep=clock.sleep,
    )
    try:
        success = await AnnouncementPoller(journal, first).poll_once(scan_id="prior-success")
        assert success.inserted == 1
    finally:
        await first.close()
    async with database.sessions() as session:
        prior = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
        last_success = prior.last_success_at

    calls = []

    def unavailable(request):
        calls.append(clock.now())
        return httpx.Response(status, headers={"Retry-After": "1"})

    failing = MockAnnouncementTransport(
        settings, httpx.MockTransport(unavailable), monotonic=clock.now, sleep=clock.sleep
    )
    try:
        result = await AnnouncementPoller(journal, failing).poll_once(scan_id="failed-scan")
        assert result.status == ("rate_limited" if status == 429 else "error")
        assert result.error_code == status and result.http_attempts == 2
        assert len(calls) == 2 and calls[1] - calls[0] >= 1
        async with database.sessions() as session:
            coverage = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
            assert coverage.last_success_at == last_success and coverage.last_error_code == status
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 1
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await failing.close()


async def test_attempt_and_page_bounds(news_db):  # noqa: F811
    database, settings = news_db
    calls = []
    clock = Clock()

    def respond(request):
        calls.append(int(request.url.params["page"]))
        return reply([announcement()], pages=999)

    transport = MockAnnouncementTransport(
        settings, httpx.MockTransport(respond), monotonic=clock.now, sleep=clock.sleep
    )
    poller = AnnouncementPoller(AnnouncementJournal(database, settings), transport)
    try:
        first = await poller.poll_once(scan_id="bound-1")
        second = await poller.poll_once(scan_id="bound-2")
        assert first.pages_seen == second.pages_seen == tuple(range(1, MAX_PAGES + 1))
        assert transport.attempts == MAX_ATTEMPTS == len(calls) == 8
        with pytest.raises(RuntimeError, match="attempt bound"):
            await poller.poll_once(scan_id="bound-3")
        assert len(calls) == 8
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 1
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await transport.close()


@pytest.mark.parametrize("failure", ["http_400", "invalid_schema", "connect_error"])
async def test_invalid_transport_response_records_failure_without_news(news_db, failure):  # noqa: F811
    database, settings = news_db

    def respond(_request):
        if failure == "http_400":
            return httpx.Response(400)
        if failure == "connect_error":
            raise httpx.ConnectError("synthetic connection failure")
        return httpx.Response(200, json={"code": 0, "data": {"rows": []}})

    transport = MockAnnouncementTransport(settings, httpx.MockTransport(respond))
    try:
        result = await AnnouncementPoller(
            AnnouncementJournal(database, settings), transport
        ).poll_once(scan_id="invalid-response")
        assert result.status == "error"
        assert result.error_code == (400 if failure == "http_400" else 0)
        async with database.sessions() as session:
            coverage = await session.scalar(select(AnnouncementCoverageObservation))
            assert coverage.status == "error" and coverage.error_code == result.error_code
            assert await session.scalar(select(func.count()).select_from(AnnouncementVersion)) == 0
            assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        await transport.close()
