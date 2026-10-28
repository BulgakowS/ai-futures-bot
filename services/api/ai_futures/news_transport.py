"""Mock-only bounded announcement poller; no production news HTTP path exists."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import httpx

from ai_futures.config import Settings
from ai_futures.news import AnnouncementJournal
from ai_futures.schemas import utc_now

PATH = "/api/v3/announcements"
LANGUAGE = "en-US"
PAGE_LIMIT = 100
MAX_PAGES = 3
MAX_ATTEMPTS = 8
MIN_INTERVAL_SECONDS = 0.5
MAX_BODY_BYTES = 4_000_000


class AnnouncementHTTPError(ValueError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Announcement fixture HTTP {status_code}")


@dataclass(frozen=True)
class AnnouncementPage:
    rows: list[object]
    total_pages: int


@dataclass(frozen=True)
class PollResult:
    status: str
    scan_id: str
    pages_seen: tuple[int, ...]
    inserted: int
    deduped: int
    http_attempts: int
    error_code: int | None = None


class MockAnnouncementTransport:
    """An injected MockTransport is the only possible HTTP transport."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.MockTransport,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep=asyncio.sleep,
    ):
        if not isinstance(transport, httpx.MockTransport):
            raise TypeError("Only httpx.MockTransport is accepted")
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url="https://api.mexc.com", transport=transport, timeout=8
        )
        self.monotonic = monotonic
        self.sleep = sleep
        self.lock = asyncio.Lock()
        self.next_at = 0.0
        self.attempts = 0

    def require_test_gate(self):
        if self.settings.app_env != "test" or not self.settings.news_fixture_ingest_enabled:
            raise PermissionError("Announcement polling is hard-disabled outside test fixtures")

    async def close(self):
        await self.client.aclose()

    async def _request(self, page: int) -> httpx.Response:
        self.require_test_gate()
        async with self.lock:
            if self.attempts >= MAX_ATTEMPTS:
                raise RuntimeError("Announcement fixture HTTP attempt bound exceeded")
            await self.sleep(max(0.0, self.next_at - self.monotonic()))
            self.next_at = self.monotonic() + MIN_INTERVAL_SECONDS
            self.attempts += 1
            return await self.client.get(
                PATH, params={"language": LANGUAGE, "page": page, "limit": PAGE_LIMIT}
            )

    async def page(self, page: int) -> AnnouncementPage:
        self.require_test_gate()
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGES:
            raise ValueError("Announcement fixture page is outside bound")
        for attempt in range(2):
            response = await self._request(page)
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt == 0:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = min(2, int(retry_after)) if retry_after.isdigit() else 0.5
                    await self.sleep(max(0.5, delay))
                    continue
                raise AnnouncementHTTPError(response.status_code)
            response.raise_for_status()
            if len(response.content) > MAX_BODY_BYTES:
                raise ValueError("Announcement fixture response exceeds body bound")
            body = response.json()
            if not isinstance(body, dict) or body.get("code") != 0:
                raise ValueError("Announcement fixture application error")
            data = body.get("data")
            if not isinstance(data, dict):
                raise ValueError("Announcement fixture data must be an object")
            total = data.get("totalPage")
            rows = data.get("rows")
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < 1
                or not isinstance(rows, list)
                or len(rows) > PAGE_LIMIT
            ):
                raise ValueError("Announcement fixture page metadata is invalid")
            return AnnouncementPage(rows, total)
        raise AssertionError("Unreachable announcement retry state")


class AnnouncementPoller:
    def __init__(self, journal: AnnouncementJournal, transport: MockAnnouncementTransport):
        self.journal = journal
        self.transport = transport

    async def poll_once(self, *, scan_id: str | None = None) -> PollResult:
        self.transport.require_test_gate()  # No HTTP or journal write before this check.
        scan_id = scan_id or uuid4().hex
        pages_seen = []
        inserted = deduped = 0
        total_pages = 1
        try:
            for page in range(1, MAX_PAGES + 1):
                if page > total_pages:
                    break
                result = await self._ingest(page, scan_id)
                pages_seen.append(page)
                inserted += result.inserted
                deduped += result.deduped
                total_pages = result.total_pages
            # Re-read page 1: page numbers are not durable cursors under a moving feed.
            result = await self._ingest(1, scan_id)
            inserted += result.inserted
            deduped += result.deduped
            status = result.status
            return PollResult(
                status, scan_id, tuple(pages_seen), inserted, deduped, self.transport.attempts
            )
        except AnnouncementHTTPError as exc:
            await self.journal.record_failure(
                scan_id=scan_id, status_code=exc.status_code, observed_at=utc_now()
            )
            return PollResult(
                "rate_limited" if exc.status_code == 429 else "error",
                scan_id,
                tuple(pages_seen),
                inserted,
                deduped,
                self.transport.attempts,
                exc.status_code,
            )
        except (httpx.HTTPError, ValueError) as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
            await self.journal.record_failure(
                scan_id=scan_id, status_code=code, observed_at=utc_now()
            )
            return PollResult(
                "error",
                scan_id,
                tuple(pages_seen),
                inserted,
                deduped,
                self.transport.attempts,
                code,
            )

    async def _ingest(self, page: int, scan_id: str):
        fetched = await self.transport.page(page)
        journal_result = await self.journal.ingest_page(
            fetched.rows,
            page=page,
            total_pages=fetched.total_pages,
            scan_id=scan_id,
            fetched_at=utc_now(),
            observation_kind="forward",
        )
        return _IngestedPage(
            fetched.total_pages,
            journal_result.inserted,
            journal_result.deduped,
            journal_result.coverage_status,
        )


@dataclass(frozen=True)
class _IngestedPage:
    total_pages: int
    inserted: int
    deduped: int
    status: str
