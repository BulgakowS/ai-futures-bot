"""Bounded disposable SQLite snapshot for the separate sparse v3 dataset."""

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlparse

from sqlalchemy import select, text

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_dataset import HORIZON, _hash, _utc
from ai_futures.gdelt_gkg_sparse_dataset import (
    Attempt,
    Config,
    Dataset,
    Market,
    Result,
    build_dataset,
)
from ai_futures.gdelt_gkg_sparse_models import SparseGkgAttempt, SparseGkgResult
from ai_futures.models import MarketCandleObservation

MAX_ROWS = 4096


def _disposable_sqlite(settings: Settings) -> None:
    if settings.app_env != "test":
        raise PermissionError("Sparse export requires test environment")
    url = urlparse(settings.database_url.get_secret_value())
    if url.scheme != "sqlite+aiosqlite" or not url.path or url.path in {"/:memory:", "/"}:
        raise PermissionError("Sparse export requires disposable SQLite file")
    path = os.path.realpath(unquote(url.path))
    temporary = os.path.realpath(tempfile.gettempdir())
    if os.path.commonpath((path, temporary)) != temporary:
        raise PermissionError("Sparse export requires SQLite file in temporary directory")


@dataclass(frozen=True)
class SparseExport:
    dataset: Dataset
    checksum: str
    attempt_ids: tuple[str, ...]
    result_ids: tuple[str, ...]
    market_observation_ids: tuple[str, ...]
    eligibility: str = "collect-only"


async def export_sparse_dataset(
    database: Database,
    settings: Settings,
    *,
    cutoff: datetime,
    config: Config,
) -> SparseExport:
    """Read only the one-slot G7a journal and immutable M1 market observations."""
    _disposable_sqlite(settings)
    if (
        database.settings.database_url.get_secret_value()
        != settings.database_url.get_secret_value()
    ):
        raise PermissionError("Export settings/database mismatch")
    cutoff = _utc(cutoff)
    first, last = _utc(config.first_anchor), _utc(config.last_anchor)
    if first > last or last > cutoff or (last - first) / HORIZON > 335:
        raise ValueError("Invalid sparse export range")
    async with database.sessions() as session:
        await session.execute(text("BEGIN"))

        async def bounded(statement):
            rows = (await session.scalars(statement.limit(MAX_ROWS + 1))).all()
            if len(rows) > MAX_ROWS:
                raise ValueError("Sparse export row cap exceeded")
            return rows

        claim_rows = await bounded(
            select(SparseGkgAttempt)
            .where(
                SparseGkgAttempt.anchor_at >= first,
                SparseGkgAttempt.anchor_at <= last,
                SparseGkgAttempt.attempted_at <= cutoff,
            )
            .order_by(SparseGkgAttempt.anchor_at)
        )
        ids = [r.attempt_id for r in claim_rows]
        result_rows = (
            await bounded(
                select(SparseGkgResult)
                .where(
                    SparseGkgResult.attempt_id.in_(ids),
                    SparseGkgResult.first_seen_at <= cutoff,
                )
                .order_by(SparseGkgResult.attempt_id)
            )
            if ids
            else []
        )
        market_rows = (
            await bounded(
                select(MarketCandleObservation)
                .where(
                    MarketCandleObservation.symbol == "BTC_USDT",
                    MarketCandleObservation.timeframe == "4h",
                    MarketCandleObservation.window_start >= first - 2 * HORIZON,
                    MarketCandleObservation.window_start <= last,
                    MarketCandleObservation.observed_at <= cutoff,
                )
                .order_by(MarketCandleObservation.window_start, MarketCandleObservation.version)
            )
            if settings.market_candle_journal_enabled
            else []
        )
        attempts = tuple(
            Attempt(
                str(r.attempt_id),
                r.anchor_at,
                r.target_batch_at,
                r.attempted_at,
                r.reserved_bytes,
                r.source_version,
                r.config_version,
            )
            for r in claim_rows
        )
        results = tuple(
            Result(
                str(r.result_id),
                str(r.attempt_id),
                r.first_seen_at,
                r.status,
                r.error_code,
                r.response_bytes,
                r.archive_sha256,
                r.total_rows,
                r.btc_rows,
                r.mean_btc_document_tone,
                r.source_version,
                r.config_version,
            )
            for r in result_rows
        )
        market = tuple(
            Market(
                str(r.observation_id),
                str(r.causal_id),
                r.window_start,
                r.version,
                r.first_seen_at,
                r.observed_at,
                r.source_time,
                r.close,
                r.payload_hash,
                r.source_version,
                r.config_version,
                r.rules_version,
                r.source,
                r.symbol,
                r.timeframe,
            )
            for r in market_rows
        )
        await session.rollback()
    dataset = build_dataset(attempts, results, market, cutoff=cutoff, config=config)
    checksum = _hash(
        {
            "schema": "persisted-gkg-sparse-market-export-v1",
            "dataset": dataset.checksum,
            "attempt_ids": tuple(r.attempt_id for r in attempts),
            "result_ids": tuple(r.result_id for r in results),
            "market_observations": tuple(
                (
                    r.observation_id,
                    r.causal_id,
                    r.version,
                    r.source_version,
                    r.rules_version,
                    r.config_version,
                    r.payload_hash,
                )
                for r in market
            ),
        }
    )
    return SparseExport(
        dataset,
        checksum,
        tuple(r.attempt_id for r in attempts),
        tuple(r.result_id for r in results),
        tuple(r.observation_id for r in market),
    )
