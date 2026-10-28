"""Offline announcement journal contract. No network collector is wired into runtime."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from sqlalchemy import select

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.news_models import (
    AnnouncementCoverage,
    AnnouncementCoverageObservation,
    AnnouncementVersion,
)
from ai_futures.schemas import require_utc

SOURCE = "mexc_public_announcements"
SCHEMA_VERSION = "mexc-announcement-v1"
COVERAGE_VERSION = "mexc-announcement-coverage-v1"
SCAN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True)
class ParsedAnnouncement:
    canonical_id: str
    canonical_url: str
    title: str
    language: str
    post_time: datetime
    payload_hash: str


@dataclass(frozen=True)
class IngestResult:
    inserted: int
    deduped: int
    version_ids: tuple[UUID, ...]
    coverage_status: str


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_announcement(raw: object, fetched_at: datetime) -> ParsedAnnouncement:
    fetched_at = require_utc(fetched_at)
    if not isinstance(raw, dict):
        raise ValueError("Announcement must be an object")
    title = raw.get("title")
    if not isinstance(title, str):
        raise ValueError("Announcement title is required")
    title = unicodedata.normalize("NFC", " ".join(title.split()))
    if not 1 <= len(title) <= 512:
        raise ValueError("Announcement title length is invalid")
    language = raw.get("language")
    if language != "en-US":
        raise ValueError("Only en-US announcement fixtures are supported")
    timestamp_ms = raw.get("postTime")
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise ValueError("postTime must be Unix milliseconds")
    try:
        post_time = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("postTime is outside timestamp range") from exc
    if post_time.year < 2018 or post_time > fetched_at + timedelta(minutes=5):
        raise ValueError("postTime is outside supported range")
    url = raw.get("url")
    if not isinstance(url, str):
        raise ValueError("Announcement URL is required")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "www.mexc.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or not parts.path.startswith("/help/")
        or ".." in parts.path.split("/")
    ):
        raise ValueError("Announcement URL must be a MEXC help URL")
    canonical_url = urlunsplit(("https", "www.mexc.com", parts.path.rstrip("/"), "", ""))
    if len(canonical_url) > 1024:
        raise ValueError("Announcement URL is too long")
    fields = {
        "language": language,
        "postTime": timestamp_ms,
        "title": title,
        "url": canonical_url,
    }
    return ParsedAnnouncement(
        canonical_id=hashlib.sha256(canonical_url.encode()).hexdigest(),
        canonical_url=canonical_url,
        title=title,
        language=language,
        post_time=post_time,
        payload_hash=_digest(fields),
    )


class AnnouncementJournal:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def _require_offline_fixture(self) -> None:
        if self.settings.app_env != "test" or not self.settings.news_fixture_ingest_enabled:
            raise PermissionError(
                "Announcement ingestion is disabled outside explicit test fixtures"
            )

    @staticmethod
    def _observe(coverage: AnnouncementCoverage) -> AnnouncementCoverageObservation:
        return AnnouncementCoverageObservation(
            source=coverage.source,
            language=coverage.language,
            source_version=COVERAGE_VERSION,
            config_version=coverage.config_version,
            scan_id=coverage.scan_id,
            observed_at=coverage.last_attempt_at,
            status=coverage.status,
            reported_total_pages=coverage.reported_total_pages,
            seen_pages=list(coverage.seen_pages),
            page_hashes=dict(coverage.page_hashes),
            internal_gap_pages=list(coverage.internal_gap_pages),
            unseen_page_count=coverage.unseen_page_count,
            page_shift_detected=coverage.page_shift_detected,
            last_success_at=coverage.last_success_at,
            error_code=coverage.last_error_code,
        )

    async def ingest_page(
        self,
        details: list[object],
        *,
        page: int,
        total_pages: int,
        scan_id: str,
        fetched_at: datetime,
        observation_kind: str = "forward",
    ) -> IngestResult:
        self._require_offline_fixture()
        fetched_at = require_utc(fetched_at)
        if (
            isinstance(page, bool)
            or isinstance(total_pages, bool)
            or not isinstance(page, int)
            or not isinstance(total_pages, int)
            or not 1 <= page <= total_pages
            or not isinstance(details, list)
            or len(details) > 100
            or not isinstance(scan_id, str)
            or not SCAN_ID.fullmatch(scan_id)
            or observation_kind not in {"forward", "backfill"}
        ):
            raise ValueError("Invalid announcement page metadata")
        parsed = [parse_announcement(raw, fetched_at) for raw in details]
        unique: dict[str, ParsedAnnouncement] = {}
        for item in parsed:
            previous = unique.get(item.canonical_id)
            if previous and previous.payload_hash != item.payload_hash:
                raise ValueError("Conflicting announcement versions within one page")
            unique[item.canonical_id] = item
        page_hash = _digest([(item.canonical_id, item.payload_hash) for item in parsed])
        inserted_ids: list[UUID] = []
        deduped = len(parsed) - len(unique)
        async with self.database.sessions.begin() as session:
            coverage = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
            if coverage and fetched_at <= coverage.last_attempt_at:
                raise ValueError("Announcement observation time cannot move backwards")
            for item in unique.values():
                latest = await session.scalar(
                    select(AnnouncementVersion)
                    .where(AnnouncementVersion.canonical_id == item.canonical_id)
                    .order_by(AnnouncementVersion.version_number.desc())
                    .limit(1)
                )
                if latest and latest.payload_hash == item.payload_hash:
                    deduped += 1
                    continue
                version_id = uuid4()
                session.add(
                    AnnouncementVersion(
                        version_id=version_id,
                        canonical_id=item.canonical_id,
                        canonical_url=item.canonical_url,
                        version_number=latest.version_number + 1 if latest else 1,
                        title=item.title,
                        language=item.language,
                        post_time=item.post_time,
                        first_seen_at=fetched_at,
                        fetched_at=fetched_at,
                        available_at=max(item.post_time, fetched_at),
                        payload_hash=item.payload_hash,
                        observation_kind=observation_kind,
                        source=SOURCE,
                        schema_version=SCHEMA_VERSION,
                        config_version=self.settings.config_version,
                    )
                )
                inserted_ids.append(version_id)
            if coverage is None:
                coverage = AnnouncementCoverage(source=SOURCE, language="en-US")
                session.add(coverage)
            new_scan = coverage.scan_id != scan_id
            pages = [] if new_scan else list(coverage.seen_pages)
            hashes = {} if new_scan else dict(coverage.page_hashes)
            shifted = False if new_scan else coverage.page_shift_detected
            if not new_scan and coverage.reported_total_pages != total_pages:
                shifted = True
            if str(page) in hashes and hashes[str(page)] != page_hash:
                shifted = True
            hashes[str(page)] = page_hash
            pages = sorted(set(pages) | {page})
            coverage.scan_id = scan_id
            coverage.reported_total_pages = total_pages
            coverage.seen_pages = pages
            coverage.page_hashes = hashes
            coverage.internal_gap_pages = [
                p for p in range(min(pages), max(pages) + 1) if p not in pages
            ]
            coverage.unseen_page_count = max(0, total_pages - len(pages))
            coverage.page_shift_detected = shifted
            coverage.status = (
                "shifted" if shifted else "ok" if len(pages) == total_pages else "partial"
            )
            coverage.last_success_at = fetched_at
            coverage.last_attempt_at = fetched_at
            coverage.last_error_code = None
            coverage.config_version = self.settings.config_version
            session.add(self._observe(coverage))
        return IngestResult(len(inserted_ids), deduped, tuple(inserted_ids), coverage.status)

    async def record_failure(
        self, *, scan_id: str, status_code: int, observed_at: datetime
    ) -> None:
        self._require_offline_fixture()
        observed_at = require_utc(observed_at)
        if not isinstance(scan_id, str) or not SCAN_ID.fullmatch(scan_id):
            raise ValueError("Invalid announcement scan ID")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not (status_code == 0 or 400 <= status_code <= 599)
        ):
            raise ValueError("Invalid announcement fixture failure code")
        async with self.database.sessions.begin() as session:
            coverage = await session.get(AnnouncementCoverage, (SOURCE, "en-US"))
            if coverage is None:
                coverage = AnnouncementCoverage(
                    source=SOURCE,
                    language="en-US",
                    scan_id=scan_id,
                    reported_total_pages=1,
                    seen_pages=[],
                    page_hashes={},
                    internal_gap_pages=[],
                    unseen_page_count=1,
                    page_shift_detected=False,
                    last_success_at=None,
                    config_version=self.settings.config_version,
                )
                session.add(coverage)
            elif observed_at <= coverage.last_attempt_at:
                raise ValueError("Announcement observation time cannot move backwards")
            elif coverage.scan_id != scan_id:
                coverage.scan_id = scan_id
                coverage.seen_pages = []
                coverage.page_hashes = {}
                coverage.internal_gap_pages = []
                coverage.unseen_page_count = coverage.reported_total_pages
                coverage.page_shift_detected = False
            coverage.last_attempt_at = observed_at
            coverage.last_error_code = status_code
            coverage.status = "rate_limited" if status_code == 429 else "error"
            coverage.config_version = self.settings.config_version
            session.add(self._observe(coverage))
