"""N4a forecast journal on a disposable SQLite database only."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.forecast_journal import ForecastJournal
from ai_futures.forecast_models import ForecastDecision, ForecastProposal
from ai_futures.news_forecast import CandidateProposal, ForecastContext, ForecastOutcome
from ai_futures.paper_models import PaperIntent, PaperOrder
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

ANCHOR = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.fixture
async def forecast_db(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        forecast_fixture_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'forecast.db'}",
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


def outcome(*, candidate=False):
    context = ForecastContext(
        anchor=ANCHOR,
        observed_at=ANCHOR,
        quality="ok" if candidate else "wait",
        reason="ok" if candidate else "news_coverage_missing",
        close="101.05" if candidate else None,
        market_event_ids=("synthetic-market-1", "synthetic-market-2") if candidate else (),
        market_payload_hashes=("1" * 64, "2" * 64) if candidate else (),
        news_version_ids=("synthetic-news-1",) if candidate else (),
        news_payload_hashes=("3" * 64,) if candidate else (),
        market_return_bps="105" if candidate else "0",
        btc_title_count=1 if candidate else 0,
        futures_title_count=1 if candidate else 0,
        feature_hash="4" * 64 if candidate else None,
        config_checksum="5" * 64,
        source_versions=("synthetic-fixture-v1",),
    )
    proposal = (
        CandidateProposal(
            "LONG", "101.1", "100.0", "103.2", "synthetic-rules-v1", "synthetic-policy-v1"
        )
        if candidate
        else None
    )
    return ForecastOutcome(
        context=context,
        status="candidate" if candidate else "wait",
        direction="UP" if candidate else "WAIT",
        reason="approved_synthetic_candidate" if candidate else "news_coverage_missing",
        probabilities=("0.01", "0.01", "0.98") if candidate else None,
        model_version="synthetic-model-v1" if candidate else None,
        artifact_checksum="6" * 64 if candidate else None,
        dataset_checksum="7" * 64 if candidate else None,
        approval_version="synthetic-approval-v1" if candidate else None,
        proposal=proposal,
    )


async def test_wait_is_durable_idempotent_sealed_and_default_disabled(forecast_db):
    database, settings = forecast_db
    journal = ForecastJournal(database, settings)
    first = await journal.append(outcome())
    assert first.inserted and first.proposal_id is None
    restarted = Database(settings)
    try:
        same = await ForecastJournal(restarted, settings).append(outcome())
        assert not same.inserted and same.forecast_id == first.forecast_id
        with pytest.raises(ValueError, match="sealed"):
            await ForecastJournal(restarted, settings).append(
                replace(outcome(), reason="changed-after-restart")
            )
    finally:
        await restarted.close()
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ForecastDecision)) == 1
        assert await session.scalar(select(func.count()).select_from(ForecastProposal)) == 0
        row = await session.scalar(select(ForecastDecision))
        assert row.horizon_hours == 4 and row.status == "wait" and row.direction == "WAIT"
        assert row.created_at.utcoffset().total_seconds() == 0
        assert isinstance(row.forecast_id, UUID) and isinstance(row.correlation_id, UUID)
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE news_forecast SET reason='mutated'"))
            await session.commit()
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(text("DELETE FROM news_forecast"))
            await session.commit()
        await session.rollback()
    disabled = settings.model_copy(update={"forecast_fixture_enabled": False})
    with pytest.raises(PermissionError, match="disabled"):
        await ForecastJournal(database, disabled).append(outcome())
    with pytest.raises(ValueError, match="test-only"):
        Settings(
            _env_file=None,
            app_env="local",
            forecast_fixture_enabled=True,
            backend_auth_token="x" * 48,
            database_url="postgresql+asyncpg://test:test@localhost/test",
        )


async def test_candidate_proposal_is_atomic_append_only_and_causal(forecast_db):
    database, settings = forecast_db
    candidate = replace(
        outcome(candidate=True),
        context=replace(
            outcome(candidate=True).context,
            anchor=ANCHOR.replace(hour=16),
            observed_at=ANCHOR.replace(hour=16),
        ),
    )
    write = await ForecastJournal(database, settings).append(candidate)
    assert write.inserted and write.proposal_id is not None
    async with database.sessions() as session:
        row = await session.scalar(
            select(ForecastDecision).where(ForecastDecision.forecast_id == write.forecast_id)
        )
        proposal = await session.scalar(
            select(ForecastProposal).where(ForecastProposal.forecast_id == write.forecast_id)
        )
        assert row.correlation_id == write.correlation_id
        assert row.horizon_hours == 4 and row.direction == "UP" and row.quality == "ok"
        assert row.market_event_ids == ["synthetic-market-1", "synthetic-market-2"]
        assert row.news_version_ids == ["synthetic-news-1"]
        assert row.probabilities == {"DOWN": "0.01", "NEUTRAL": "0.01", "UP": "0.98"}
        assert (
            row.model_version == "synthetic-model-v1"
            and row.approval_version == "synthetic-approval-v1"
        )
        assert proposal.proposal_id == write.proposal_id
        assert (proposal.entry, proposal.stop_loss, proposal.take_profit) == (
            "101.1",
            "100.0",
            "103.2",
        )
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE news_forecast_proposal SET entry='1'"))
            await session.commit()
        await session.rollback()
    with pytest.raises(ValueError, match="Invalid forecast"):
        await ForecastJournal(database, settings).append(replace(candidate, proposal=None))
    with pytest.raises(ValueError, match="Invalid candidate provenance"):
        await ForecastJournal(database, settings).append(replace(candidate, approval_version=None))
