"""Test-only persisted news/market snapshot adapter; no runtime collector wiring."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.models import Candle
from ai_futures.news import SOURCE
from ai_futures.news_dataset import (
    CandleRecord,
    CoverageRecord,
    Dataset,
    DatasetConfig,
    NewsRecord,
    build_dataset,
)
from ai_futures.news_models import AnnouncementCoverageObservation, AnnouncementVersion
from ai_futures.schemas import require_utc


@dataclass(frozen=True)
class PersistedDatasetExport:
    dataset: Dataset
    coverage_observation_ids: tuple[str, ...]
    checksum: str


async def export_persisted_dataset(
    database: Database,
    settings: Settings,
    *,
    cutoff: datetime,
    config: DatasetConfig,
) -> PersistedDatasetExport:
    """Copy synthetic rows from one DB transaction into a sealed N2a dataset."""
    if settings.app_env != "test" or not settings.news_fixture_ingest_enabled:
        raise PermissionError("Persisted news export is limited to explicit test fixtures")
    cutoff = require_utc(cutoff)
    if config.source_version != "synthetic-fixture-v1":
        raise ValueError("Persisted news export requires synthetic source config")
    async with database.sessions.begin() as session:
        candles = (
            await session.scalars(
                select(Candle)
                .where(Candle.symbol == "BTC_USDT", Candle.timeframe == "4h")
                .order_by(Candle.window_start)
            )
        ).all()
        versions = (
            await session.scalars(
                select(AnnouncementVersion).order_by(
                    AnnouncementVersion.canonical_id, AnnouncementVersion.version_number
                )
            )
        ).all()
        observations = (
            await session.scalars(
                select(AnnouncementCoverageObservation)
                .where(
                    AnnouncementCoverageObservation.source == SOURCE,
                    AnnouncementCoverageObservation.language == "en-US",
                )
                .order_by(AnnouncementCoverageObservation.observed_at)
            )
        ).all()
        if any(row.source != "synthetic" for row in candles):
            raise ValueError("Mixed or non-synthetic market source in dataset export")
        if any(row.source != SOURCE or row.language != "en-US" for row in versions):
            raise ValueError("Mixed or non-synthetic announcement source in dataset export")
        market = tuple(
            CandleRecord(
                event_id=str(row.event_id),
                window_start=row.window_start,
                source_time=row.source_time,
                received_at=row.received_at,
                close=row.payload["close"],
                payload_hash=row.payload_hash,
                source=row.source,
                config_version=row.config_version,
                final=row.final,
                symbol=row.symbol,
                timeframe=row.timeframe,
            )
            for row in candles
        )
        news = tuple(
            NewsRecord(
                version_id=str(row.version_id),
                canonical_id=row.canonical_id,
                version_number=row.version_number,
                title=row.title,
                post_time=row.post_time,
                first_seen_at=row.first_seen_at,
                available_at=row.available_at,
                payload_hash=row.payload_hash,
                source=row.source,
                schema_version=row.schema_version,
                config_version=row.config_version,
                observation_kind=row.observation_kind,
            )
            for row in versions
        )
        coverage = tuple(
            CoverageRecord(
                observed_at=row.observed_at,
                last_success_at=row.last_success_at,
                status=row.status,
                gap_count=len(row.internal_gap_pages) + row.unseen_page_count,
                page_shift_detected=row.page_shift_detected,
                source_version=f"{row.source_version}:{row.config_version}",
            )
            for row in observations
        )
        visible_ids = tuple(
            str(row.observation_id) for row in observations if row.observed_at <= cutoff
        )
    dataset = build_dataset(market, news, coverage, cutoff=cutoff, config=config)
    manifest = {
        "schema": "persisted-news-market-export-v1",
        "cutoff": cutoff.isoformat(),
        "dataset_checksum": dataset.checksum,
        "coverage_observation_ids": visible_ids,
    }
    checksum = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PersistedDatasetExport(dataset, visible_ids, checksum)
