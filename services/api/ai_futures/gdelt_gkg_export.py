"""Bounded, test-only persisted GKG/immutable Hour4 snapshot adapter."""

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

from sqlalchemy import select, text

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_dataset import HEX64, HORIZON, MarketCandle, _decimal, _hash, _utc
from ai_futures.gdelt_gkg_dataset import (
    Config,
    Dataset,
    Release,
    Scan,
    Slot,
    build_versioned_market_dataset,
)
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION
from ai_futures.models import MarketCandleObservation

MAX_ROWS = 8192


@dataclass(frozen=True)
class PersistedGkgExport:
    dataset: Dataset
    checksum: str
    snapshot_revision_ids: tuple[str, ...]
    snapshot_observation_ids: tuple[str, ...]
    market_observation_ids: tuple[str, ...]
    market_rules_versions: tuple[str, ...]
    market_corrections: int
    gkg_corrections: int
    late_releases: int


def _disposable_sqlite(settings: Settings) -> None:
    if settings.app_env != "test":
        raise PermissionError("GKG export requires test environment")
    url = urlparse(settings.database_url.get_secret_value())
    if url.scheme != "sqlite+aiosqlite" or not url.path or url.path in {"/:memory:", "/"}:
        raise PermissionError("GKG export requires disposable SQLite file")
    path = os.path.realpath(unquote(url.path))
    temporary = os.path.realpath(tempfile.gettempdir())
    if os.path.commonpath((path, temporary)) != temporary:
        raise PermissionError("GKG export requires SQLite file in temporary directory")


async def export_persisted_gkg_dataset(
    database: Database,
    settings: Settings,
    *,
    cutoff: datetime,
    config: Config,
    label_observation_delay: timedelta | None = None,
) -> PersistedGkgExport:
    """Read bounded G3 and immutable market versions in one SQLite snapshot."""
    _disposable_sqlite(settings)
    if (
        database.settings.database_url.get_secret_value()
        != settings.database_url.get_secret_value()
    ):
        raise PermissionError("Export settings/database mismatch")
    cutoff = _utc(cutoff)
    first, last = _utc(config.first_anchor), _utc(config.last_anchor)
    if first > last or last > cutoff or (last - first) / HORIZON > 255:
        raise ValueError("Invalid export range")
    lower = first - HORIZON
    market_lower = first - 2 * HORIZON
    async with database.sessions() as session:
        # SQLite's default deferred transaction may begin at first SELECT. Explicit
        # BEGIN pins one read snapshot for all four tables and concurrent writers.
        await session.execute(text("BEGIN"))

        async def rows(statement):
            result = (await session.scalars(statement.limit(MAX_ROWS + 1))).all()
            if len(result) > MAX_ROWS:
                raise ValueError("GKG snapshot row cap exceeded")
            return result

        candles = (
            await rows(
                select(MarketCandleObservation)
                .where(
                    MarketCandleObservation.symbol == "BTC_USDT",
                    MarketCandleObservation.timeframe == "4h",
                    MarketCandleObservation.window_start >= market_lower,
                    MarketCandleObservation.window_start <= last,
                    MarketCandleObservation.observed_at <= cutoff,
                )
                .order_by(MarketCandleObservation.window_start, MarketCandleObservation.version)
            )
            if settings.market_candle_journal_enabled
            else []
        )
        previous = {}
        for row in candles:
            prior = previous.get(row.window_start)
            if (
                row.source != "rest"
                or row.source_version != MARKET_CANDLE_SOURCE_VERSION
                or not row.config_version
                or not row.rules_version
                or not HEX64.fullmatch(row.payload_hash)
                or row.source_time < row.window_start + HORIZON
                or row.observed_at < row.window_start + HORIZON
                or row.first_seen_at > row.observed_at
                or row.version != (prior.version + 1 if prior else 1)
                or (
                    prior
                    and (
                        row.first_seen_at != prior.first_seen_at
                        or row.observed_at <= prior.observed_at
                        or row.causal_id != prior.causal_id
                    )
                )
            ):
                raise ValueError("Invalid immutable Hour4 journal sequence")
            _decimal(row.close, positive=True)
            previous[row.window_start] = row
        revisions = await rows(
            select(GkgReleaseRevision)
            .where(
                GkgReleaseRevision.batch_at >= lower,
                GkgReleaseRevision.batch_at < last,
                GkgReleaseRevision.observed_at <= cutoff,
            )
            .order_by(GkgReleaseRevision.batch_at, GkgReleaseRevision.revision)
        )
        observations = await rows(
            select(GkgSlotObservation)
            .where(
                GkgSlotObservation.slot_at >= lower,
                GkgSlotObservation.slot_at < last,
                GkgSlotObservation.observed_at <= cutoff,
            )
            .order_by(
                GkgSlotObservation.slot_at,
                GkgSlotObservation.observed_at,
                GkgSlotObservation.observation_id,
            )
        )
        scan_ids = {row.scan_id for row in revisions} | {row.scan_id for row in observations}
        scans = (
            await rows(
                select(GkgScan)
                .where(GkgScan.scan_id.in_(scan_ids), GkgScan.attempted_at <= cutoff)
                .order_by(GkgScan.attempted_at, GkgScan.scan_id)
            )
            if scan_ids
            else []
        )
        market = tuple(
            MarketCandle(
                str(r.observation_id),
                r.window_start,
                r.source_time,
                r.observed_at,
                r.close,
                r.payload_hash,
                r.source,
                r.config_version,
                True,
                r.symbol,
                r.timeframe,
            )
            for r in candles
        )
        market_provenance = tuple(
            (
                str(r.observation_id),
                r.version,
                r.source_version,
                r.rules_version,
                r.config_version,
                r.payload_hash,
                r.close,
                r.source_time,
                r.observed_at,
            )
            for r in candles
        )
        market_metadata = {
            str(r.observation_id): (r.source_version, r.rules_version, r.config_version)
            for r in candles
        }
        releases = tuple(
            Release(
                str(r.revision_id),
                str(r.scan_id),
                r.batch_at,
                r.first_seen_at,
                r.observed_at,
                r.revision,
                r.total_rows,
                r.btc_rows,
                r.mean_btc_document_tone,
                r.archive_sha256,
                r.manifest_md5,
                r.manifest_size,
                r.source_version,
                r.config_version,
            )
            for r in revisions
        )
        slots = tuple(
            Slot(
                str(r.observation_id),
                str(r.scan_id),
                r.slot_at,
                r.observed_at,
                r.status,
                r.source_version,
                r.config_version,
            )
            for r in observations
        )
        scan_rows = tuple(
            Scan(
                str(r.scan_id),
                r.attempted_at,
                r.batch_at,
                r.status,
                r.source_version,
                r.config_version,
            )
            for r in scans
        )
        market_observation_ids = tuple(str(r.observation_id) for r in candles)
        market_rules_versions = tuple(sorted({r.rules_version for r in candles}))
        market_corrections = sum(r.version > 1 for r in candles)
        gkg_corrections = sum(r.revision > 1 for r in revisions)
        late_releases = sum(
            r.first_seen_at - r.batch_at > config.max_release_lag for r in revisions
        )
        await session.rollback()
    dataset = build_versioned_market_dataset(
        market,
        releases,
        slots,
        scan_rows,
        cutoff=cutoff,
        config=config,
        market_metadata=market_metadata,
        label_observation_delay=label_observation_delay,
    )
    revision_ids = tuple(r.revision_id for r in releases)
    observation_ids = tuple(r.observation_id for r in slots)
    checksum = _hash(
        {
            "schema": "persisted-gkg-market-export-v3",
            "cutoff": cutoff,
            "dataset": dataset.checksum,
            "revisions": revision_ids,
            "observations": observation_ids,
            "market_observations": market_provenance,
            "label_observation_delay_seconds": (
                None if label_observation_delay is None else label_observation_delay.total_seconds()
            ),
        }
    )
    return PersistedGkgExport(
        dataset,
        checksum,
        revision_ids,
        observation_ids,
        market_observation_ids,
        market_rules_versions,
        market_corrections,
        gkg_corrections,
        late_releases,
    )
