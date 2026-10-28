"""Disposable, test-only 4h event tick from persisted inputs through Paper risk."""

import asyncio
import fcntl
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import make_url

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.forecast_journal import ForecastJournal
from ai_futures.model_paper import ModelPaperPolicy, ModelPaperResult, ModelPaperService
from ai_futures.models import Candle
from ai_futures.news_dataset import CandleRecord, CoverageRecord, DatasetConfig, NewsRecord, _utc
from ai_futures.news_forecast import (
    ArtifactApproval,
    CandidateRules,
    ProposalPolicy,
    build_forecast_context,
    evaluate_forecast,
)
from ai_futures.news_models import AnnouncementCoverageObservation, AnnouncementVersion
from ai_futures.news_training import ModelArtifact

HORIZON = timedelta(hours=4)


@dataclass(frozen=True)
class ModelBotResult:
    status: str
    reason: str
    forecast_id: UUID | None = None
    paper: ModelPaperResult | None = None


class ModelBotService:
    """Explicit tick only; no scheduler, network client, exchange key, or broker handle."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.journal = ForecastJournal(database, settings)
        self.execution = ModelPaperService(database, settings)

    def _lock_path(self) -> Path:
        url = self.settings.database_url.get_secret_value()
        if (
            self.settings.app_env != "test"
            or self.database.settings.app_env != "test"
            or not self.settings.model_bot_fixture_enabled
            or not self.settings.model_paper_fixture_enabled
            or not self.settings.forecast_fixture_enabled
            or self.database.settings.database_url.get_secret_value() != url
            or not url.startswith("sqlite+aiosqlite:///")
        ):
            raise PermissionError(
                "Model bot tick is hard-disabled outside disposable test fixtures"
            )
        path = Path(make_url(url).database)
        if not path.is_absolute() or not path.is_file():
            raise PermissionError("Model bot requires an existing disposable SQLite file")
        return path.with_name(path.name + ".model-bot.lock")

    async def tick(
        self,
        event_id: UUID,
        portfolio_id: UUID,
        artifact: ModelArtifact | None,
        approval: ArtifactApproval | None,
        rules: CandidateRules | None,
        proposal_policy: ProposalPolicy,
        execution_policy: ModelPaperPolicy,
        *,
        config: DatasetConfig,
        now=None,
    ) -> ModelBotResult:
        if config.source_version != "synthetic-fixture-v1":
            raise ValueError("Model bot accepts synthetic fixture source only")
        lock_path = self._lock_path()
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # Cross-process serialization covers journal append and PaperService idempotency.
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            return await self._tick(
                event_id,
                portfolio_id,
                artifact,
                approval,
                rules,
                proposal_policy,
                execution_policy,
                config=config,
                now=now,
            )
        finally:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
            os.close(fd)

    async def _tick(
        self,
        event_id,
        portfolio_id,
        artifact,
        approval,
        rules,
        proposal_policy,
        execution_policy,
        *,
        config,
        now,
    ) -> ModelBotResult:
        async with self.database.sessions() as session:
            matching = (
                await session.scalars(
                    select(Candle).where(
                        Candle.symbol == "BTC_USDT",
                        Candle.timeframe == "4h",
                        Candle.event_id == event_id,
                        Candle.final.is_(True),
                    )
                )
            ).all()
            if not matching:
                return ModelBotResult("no_event", "final_4h_event_missing")
            if len(matching) != 1:
                return ModelBotResult("blocked", "duplicate_final_event_id")
            current = matching[0]
            anchor = _utc(current.window_start) + HORIZON
            prior = await session.get(Candle, ("BTC_USDT", "4h", anchor - 2 * HORIZON))
            versions = (
                await session.scalars(
                    select(AnnouncementVersion).where(
                        AnnouncementVersion.first_seen_at <= anchor,
                        AnnouncementVersion.available_at <= anchor,
                    )
                )
            ).all()
            observations = (
                await session.scalars(
                    select(AnnouncementCoverageObservation)
                    .where(AnnouncementCoverageObservation.observed_at <= anchor)
                    .order_by(AnnouncementCoverageObservation.observed_at.desc())
                )
            ).all()
            # Mixed provenance is not an approved synthetic fixture.
            if (
                any(row.source != "synthetic" for row in (prior, current) if row is not None)
                or any(row.source != "synthetic" for row in versions)
                or any(row.source != "synthetic" for row in observations)
            ):
                return ModelBotResult("blocked", "non_synthetic_source")

            def candle(row):
                return (
                    CandleRecord(
                        event_id=str(row.event_id),
                        window_start=row.window_start,
                        source_time=row.source_time,
                        received_at=row.received_at,
                        close=row.payload.get("close", "") if isinstance(row.payload, dict) else "",
                        payload_hash=row.payload_hash,
                        source=row.source,
                        config_version=row.config_version,
                        final=row.final,
                        symbol=row.symbol,
                        timeframe=row.timeframe,
                    )
                    if row is not None
                    else None
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
                    gap_count=len(row.internal_gap_pages or []) + (row.unseen_page_count or 0),
                    page_shift_detected=row.page_shift_detected,
                    source_version=f"{row.source_version}:{row.config_version}",
                )
                for row in observations
            )
        context = build_forecast_context(
            candle(prior),
            candle(current),
            news,
            coverage,
            anchor=anchor,
            observed_at=anchor + timedelta(minutes=1),
            config=config,
        )
        outcome = evaluate_forecast(
            context,
            artifact,
            approval,
            rules,
            proposal_policy,
            app_env=self.settings.app_env,
        )
        try:
            write = await self.journal.append(outcome)
        except ValueError as exc:
            if str(exc) == "Forecast window is already sealed with another decision":
                return ModelBotResult("blocked", "forecast_already_sealed")
            raise
        if outcome.status != "candidate":
            return ModelBotResult("wait", outcome.reason, write.forecast_id)
        paper = await self.execution.submit(
            portfolio_id,
            write.forecast_id,
            outcome,
            artifact,
            approval,
            rules,
            proposal_policy,
            execution_policy,
            now=now,
        )
        return ModelBotResult(paper.status, paper.reason, write.forecast_id, paper)
