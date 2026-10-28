"""Bounded, manual, forward-only GDELT GKG candidate collection."""

import asyncio
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from uuid import uuid4

import httpx
from sqlalchemy import select, text

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.schemas import require_utc, utc_now

SOURCE_VERSION = "gdelt-gkg-econ-bitcoin-candidate-v1"
MANIFEST_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
HOST = "data.gdeltproject.org"
MAX_MANIFEST = 4096
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_UNCOMPRESSED = 30 * 1024 * 1024
MAX_GAP_SLOTS = 96
POLL_INTERVAL = timedelta(minutes=15)
LINE = re.compile(
    r"(?P<size>[1-9][0-9]*) (?P<md5>[0-9a-f]{32}) "
    r"https?://data\.gdeltproject\.org/gdeltv2/(?P<id>[0-9]{14})\.gkg\.csv\.zip"
)


@dataclass(frozen=True)
class Manifest:
    batch_at: datetime
    batch_id: str
    size: int
    md5: str
    url: str


@dataclass(frozen=True)
class Aggregate:
    total_rows: int
    btc_rows: int
    mean_tone: str | None


@dataclass(frozen=True)
class PollResult:
    status: str
    scan_id: str | None
    batch_id: str | None = None
    revision: int = 0
    error_code: str | None = None
    archive_bytes: int = 0
    manifest_bytes: int = 0


class _Failure(Exception):
    def __init__(self, code: str):
        self.code = code


def parse_manifest(body: bytes, observed_at: datetime) -> Manifest:
    """Select exactly one fixed-host latest GKG entry; reject ambiguous manifests."""
    if len(body) > MAX_MANIFEST:
        raise _Failure("manifest_limit")
    try:
        lines = body.decode("ascii").splitlines()
        if not 1 <= len(lines) <= 8 or any(not line for line in lines):
            raise ValueError
        candidates = [line for line in lines if ".gkg.csv.zip" in line]
        if len(candidates) != 1:
            raise ValueError
        match = LINE.fullmatch(candidates[0])
        if match is None:
            raise ValueError
        batch_id = match["id"]
        batch_at = datetime.strptime(batch_id, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        now = require_utc(observed_at)
        if (
            batch_at.minute % 15
            or batch_at.second
            or batch_at > now + timedelta(minutes=15)
            or batch_at < now - timedelta(hours=2)
        ):
            raise ValueError
        size = int(match["size"])
        if size > MAX_ARCHIVE:
            raise _Failure("archive_limit")
        return Manifest(
            batch_at, batch_id, size, match["md5"], f"https://{HOST}/gdeltv2/{batch_id}.gkg.csv.zip"
        )
    except (UnicodeError, ValueError, OverflowError) as exc:
        raise _Failure("manifest_schema") from exc


def parse_archive(body: bytes, batch_id: str) -> Aggregate:
    """Read one bounded TSV in memory and retain only numeric aggregates."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != f"{batch_id}.gkg.csv":
                raise ValueError
            member = members[0]
            if member.is_dir() or member.flag_bits & 1 or member.file_size > MAX_UNCOMPRESSED:
                raise ValueError
            with archive.open(member) as stream:
                payload = stream.read(MAX_UNCOMPRESSED + 1)
            if len(payload) > MAX_UNCOMPRESSED or len(payload) != member.file_size:
                raise ValueError
        text_payload = payload.decode("utf-8")
        total = btc = 0
        tone_sum = Decimal(0)
        for line in text_payload.splitlines():
            fields = line.split("\t")
            if len(fields) != 27 or fields[1] != batch_id:
                raise ValueError
            tones = fields[15].split(",")
            if len(tones) != 7:
                raise ValueError
            values = [Decimal(value) for value in tones]
            if any(
                not value.is_finite()
                or len(value.as_tuple().digits) > 64
                or abs(value.adjusted()) > 30
                for value in values
            ):
                raise ValueError
            total += 1
            if "ECON_BITCOIN" in fields[7].split(";"):
                btc += 1
                tone_sum += values[0]
        if total == 0:
            raise ValueError
        mean = format((tone_sum / btc).normalize(), "f") if btc else None
        return Aggregate(total, btc, mean)
    except (zipfile.BadZipFile, UnicodeError, ValueError, DecimalException, OverflowError) as exc:
        raise _Failure("archive_schema") from exc


class GkgForwardPoller:
    def __init__(
        self, database: Database, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.database = database
        self.settings = settings
        self.client = client

    async def poll_once(
        self, *, observed_at: datetime | None = None, download_budget_bytes: int | None = None
    ) -> PollResult:
        if not self.settings.gkg_forward_enabled:
            raise PermissionError("GKG candidate collection is disabled")
        if download_budget_bytes is not None and (
            self.settings.app_env != "test"
            or not MAX_MANIFEST <= download_budget_bytes <= MAX_ARCHIVE * 18
        ):
            raise PermissionError("Download budget override is test-only and bounded")
        if observed_at is not None and self.settings.app_env != "test":
            raise PermissionError("Production observation clock cannot be overridden")
        now = require_utc(observed_at or utc_now())
        async with self.database.sessions() as session:
            last_scan = await session.scalar(
                select(GkgScan).order_by(GkgScan.attempted_at.desc()).limit(1)
            )
            latest_batch = await session.scalar(
                select(GkgScan.batch_at)
                .where(GkgScan.batch_at.is_not(None))
                .order_by(GkgScan.batch_at.desc())
                .limit(1)
            )
        if last_scan and now - last_scan.attempted_at < POLL_INTERVAL:
            return PollResult("throttled", None)
        scan_id = uuid4()
        manifest = None
        archive_bytes = manifest_bytes = 0
        aggregate = None
        archive_sha256 = None
        error_code = None
        previous = None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            manifest_body = await self._get(client, MANIFEST_URL, MAX_MANIFEST, "manifest")
            manifest_bytes = len(manifest_body)
            manifest = parse_manifest(manifest_body, now)
            if (
                download_budget_bytes is not None
                and manifest.size + manifest_bytes > download_budget_bytes
            ):
                raise _Failure("download_budget")
            if latest_batch and manifest.batch_at < latest_batch:
                raise _Failure("batch_regression")
            if (
                latest_batch
                and (manifest.batch_at - latest_batch) / POLL_INTERVAL > MAX_GAP_SLOTS + 1
            ):
                raise _Failure("gap_limit")
            async with self.database.sessions() as session:
                previous = await session.scalar(
                    select(GkgReleaseRevision)
                    .where(GkgReleaseRevision.batch_at == manifest.batch_at)
                    .order_by(GkgReleaseRevision.revision.desc())
                    .limit(1)
                )
            if previous is None or (previous.manifest_md5, previous.manifest_size) != (
                manifest.md5,
                manifest.size,
            ):
                archive = await self._get(client, manifest.url, MAX_ARCHIVE, "archive")
                archive_bytes = len(archive)
                if archive_bytes != manifest.size:
                    raise _Failure("archive_size")
                if hashlib.md5(archive, usedforsecurity=False).hexdigest() != manifest.md5:
                    raise _Failure("archive_md5")
                archive_sha256 = hashlib.sha256(archive).hexdigest()
                aggregate = parse_archive(archive, manifest.batch_id)
        except _Failure as exc:
            error_code = exc.code
        except (httpx.HTTPError, TimeoutError):
            error_code = "transport"
        finally:
            if self.client is None:
                await client.aclose()

        seen_at = now if observed_at is not None else utc_now()
        batch_at = manifest.batch_at if manifest else None
        now_slot = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        slot_at = batch_at or now_slot
        baseline = latest_batch or batch_at
        expected_end = max(batch_at or now_slot, now_slot - POLL_INTERVAL)
        gap_slots = int((expected_end - baseline) / POLL_INTERVAL) if baseline else 0
        if gap_slots > MAX_GAP_SLOTS:
            error_code = "gap_limit"
        status = "error" if error_code else "ok" if aggregate else "repeat"
        revision = 0
        async with self.database.sessions() as session, session.begin():
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(1128713295)"))
            last = await session.scalar(
                select(GkgScan.attempted_at).order_by(GkgScan.attempted_at.desc()).limit(1)
            )
            if last and now - last < POLL_INTERVAL:
                return PollResult("throttled", None)
            # A concurrent writer may have committed the same manifest during HTTP.
            current = None
            if batch_at:
                current = await session.scalar(
                    select(GkgReleaseRevision)
                    .where(GkgReleaseRevision.batch_at == batch_at)
                    .order_by(GkgReleaseRevision.revision.desc())
                    .limit(1)
                )
            if (
                not error_code
                and current
                and (current.manifest_md5, current.manifest_size) == (manifest.md5, manifest.size)
            ):
                status = "repeat"
                aggregate = None
            session.add(
                GkgScan(
                    scan_id=scan_id,
                    attempted_at=now,
                    batch_at=batch_at,
                    status=status,
                    error_code=error_code,
                    manifest_bytes=manifest_bytes,
                    archive_bytes=archive_bytes,
                    source_version=SOURCE_VERSION,
                    config_version=self.settings.config_version,
                )
            )
            # Record every expected past slot since the first observed batch. A late
            # release may later add an ok observation, without rewriting this scan.
            if baseline and error_code != "gap_limit":
                for offset in range(1, gap_slots + 1):
                    expected_slot = baseline + offset * POLL_INTERVAL
                    if expected_slot != slot_at:
                        session.add(self._slot(scan_id, expected_slot, seen_at, "unknown"))
            session.add(self._slot(scan_id, slot_at, seen_at, status))
            if aggregate is not None and not error_code:
                revision = current.revision + 1 if current else 1
                session.add(
                    GkgReleaseRevision(
                        revision_id=uuid4(),
                        scan_id=scan_id,
                        batch_at=batch_at,
                        first_seen_at=current.first_seen_at if current else seen_at,
                        observed_at=seen_at,
                        revision=revision,
                        total_rows=aggregate.total_rows,
                        btc_rows=aggregate.btc_rows,
                        mean_btc_document_tone=aggregate.mean_tone,
                        archive_sha256=archive_sha256,
                        manifest_md5=manifest.md5,
                        manifest_size=manifest.size,
                        source_version=SOURCE_VERSION,
                        config_version=self.settings.config_version,
                    )
                )
        return PollResult(
            status,
            str(scan_id),
            manifest.batch_id if manifest else None,
            revision,
            error_code,
            archive_bytes,
            manifest_bytes,
        )

    def _slot(self, scan_id, slot_at, observed_at, status):
        return GkgSlotObservation(
            observation_id=uuid4(),
            scan_id=scan_id,
            slot_at=slot_at,
            observed_at=observed_at,
            status=status,
            source_version=SOURCE_VERSION,
            config_version=self.settings.config_version,
        )

    async def _get(self, client, url, limit, kind):
        body = bytearray()
        async with asyncio.timeout(15):
            async with client.stream("GET", url, timeout=15, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise _Failure(f"{kind}_http_{response.status_code}")
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise _Failure(f"{kind}_limit")
        return bytes(body)
