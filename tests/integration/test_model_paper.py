"""N4c synthetic model-to-Paper orchestration through the existing risk gate."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from ai_futures.config import Settings
from ai_futures.forecast_journal import ForecastJournal
from ai_futures.model_paper import ModelPaperPolicy, ModelPaperService
from ai_futures.models import InstrumentRules, WorkerHeartbeat
from ai_futures.news_forecast import (
    CandidateRules,
    ProposalPolicy,
    evaluate_forecast,
    issue_test_approval,
)
from ai_futures.paper_models import PaperFill, PaperIntent, PaperOrder, PaperVerdict
from ai_futures.schemas import utc_now
from sqlalchemy import func, select

from tests.integration.test_market import market_db  # noqa: F401
from tests.integration.test_paper import feed, paper  # noqa: F401
from tests.unit.test_news_forecast import artifact, at, context


async def setup_candidate(database, settings):
    await feed(database, settings, price="101.1")
    async with database.sessions() as session:
        active = await session.get(InstrumentRules, "BTC_USDT")
        version = active.version
    enabled = settings.model_copy(
        update={"forecast_fixture_enabled": True, "model_paper_fixture_enabled": True}
    )
    model = artifact()
    approval = issue_test_approval(model, approved_at=at(12), expires_at=at(13))
    rules = CandidateRules(version, at(12), Decimal("0.1"), Decimal("1"), True)
    policy = ProposalPolicy("synthetic-proposal-v1")
    outcome = evaluate_forecast(context(), model, approval, rules, policy, app_env="test")
    assert outcome.status == "candidate"
    write = await ForecastJournal(database, enabled).append(outcome)
    return enabled, write.forecast_id, outcome, model, approval, rules, policy


async def counts(database):
    async with database.sessions() as session:
        result = []
        for table in (PaperIntent, PaperVerdict, PaperOrder, PaperFill):
            result.append(await session.scalar(select(func.count()).select_from(table)))
        return tuple(result)


async def test_positive_risk_path_duplicate_restart_race_and_unknown_order(paper):  # noqa: F811
    database, settings, _paper_service, portfolio_id = paper
    enabled, forecast_id, outcome, model, approval, rules, policy = await setup_candidate(
        database, settings
    )
    service = ModelPaperService(database, enabled)
    args = (
        portfolio_id,
        forecast_id,
        outcome,
        model,
        approval,
        rules,
        policy,
        ModelPaperPolicy(Decimal("10.9")),
    )
    first, race_replay = await asyncio.gather(
        service.submit(*args, now=at(12) + timedelta(minutes=1)),
        service.submit(*args, now=at(12) + timedelta(minutes=1)),
    )
    assert first == race_replay
    assert first.status == "submitted"
    assert first.paper_result["risk"]["outcome"] == "ALLOW"
    assert first.paper_result["order"]["status"] == "FILLED"
    assert first.paper_result["order"]["quantity"] == "10"
    restarted = ModelPaperService(database, enabled)
    assert await restarted.submit(*args, now=at(12) + timedelta(minutes=1)) == first
    assert await counts(database) == (1, 1, 1, 1)
    async with database.sessions.begin() as session:
        order = await session.scalar(select(PaperOrder))
        order.status = "UNKNOWN"
    blocked = await restarted.submit(*args, now=at(12) + timedelta(minutes=1))
    assert blocked.status == "blocked" and blocked.reason == "order_unknown"
    assert await counts(database) == (1, 1, 1, 1)


async def test_wait_unapproved_corrupt_expired_disabled_and_quantity_block_before_paper(paper):  # noqa: F811
    database, settings, _paper_service, portfolio_id = paper
    enabled, forecast_id, outcome, model, approval, rules, policy = await setup_candidate(
        database, settings
    )
    service = ModelPaperService(database, enabled)
    base = (portfolio_id, forecast_id, outcome, model, approval, rules, policy)
    now = at(12) + timedelta(minutes=1)
    blocked_cases = (
        (portfolio_id, uuid4(), *base[2:]),
        (*base[:2], replace(outcome, status="wait", proposal=None), *base[3:]),
        (*base[:4], None, *base[5:]),
        (*base[:3], replace(model, threshold="0.99"), *base[4:]),
        (
            *base[:2],
            replace(outcome, proposal=replace(outcome.proposal, entry="999")),
            *base[3:],
        ),
    )
    for args in blocked_cases:
        result = await service.submit(*args, ModelPaperPolicy(Decimal("10")), now=now)
        assert result.status == "blocked"
    expired = replace(approval, expires_at=now - timedelta(seconds=1))
    assert (
        await service.submit(
            *base[:4], expired, *base[5:], ModelPaperPolicy(Decimal("10")), now=now
        )
    ).reason == "approval_not_current"
    assert (
        await service.submit(*base, ModelPaperPolicy(Decimal("10")), now=at(13))
    ).reason == "forecast_wait_or_stale"
    assert (
        await service.submit(
            *base[:5],
            replace(rules, version="changed"),
            base[6],
            ModelPaperPolicy(Decimal("10")),
            now=now,
        )
    ).status == "blocked"
    for quantity in (Decimal("0.5"), Decimal("1000001")):
        result = await service.submit(*base, ModelPaperPolicy(quantity), now=now)
        assert result.status == "blocked" and result.reason == "quantity_outside_contract"
    disabled = enabled.model_copy(update={"model_paper_fixture_enabled": False})
    with pytest.raises(PermissionError, match="hard-disabled"):
        await ModelPaperService(database, disabled).submit(
            *base, ModelPaperPolicy(Decimal("10")), now=now
        )
    with pytest.raises(ValueError, match="test-only"):
        Settings(
            _env_file=None,
            app_env="local",
            model_paper_fixture_enabled=True,
            backend_auth_token="x" * 48,
            database_url="postgresql+asyncpg://localhost/unused",
        )
    assert await counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("kill", "KILL_SWITCH"),
        ("market_stale", "DATA_STALE"),
        ("worker_stale", "WORKER_STALE"),
        ("rules_stale", "DATA_STALE"),
    ],
)
async def test_paper_risk_rejects_faults_without_order_or_fill(paper, fault, reason):  # noqa: F811
    database, settings, paper_service, portfolio_id = paper
    enabled, forecast_id, outcome, model, approval, rules, policy = await setup_candidate(
        database, settings
    )
    if fault == "kill":
        await paper_service.kill(True)
    elif fault == "market_stale":
        from ai_futures.market import MarketStore

        await MarketStore(database, settings.config_version).quality("stale", "synthetic_fault")
    elif fault == "worker_stale":
        async with database.sessions.begin() as session:
            heartbeat = await session.get(WorkerHeartbeat, "trading")
            heartbeat.occurred_at = utc_now() - timedelta(seconds=settings.worker_stale_seconds + 1)
    else:
        async with database.sessions.begin() as session:
            active = await session.get(InstrumentRules, "BTC_USDT")
            active.received_at = utc_now() - timedelta(hours=2)
    result = await ModelPaperService(database, enabled).submit(
        portfolio_id,
        forecast_id,
        outcome,
        model,
        approval,
        rules,
        policy,
        ModelPaperPolicy(Decimal("10")),
        now=at(12) + timedelta(minutes=1),
    )
    assert result.status == "risk_rejected" and result.reason == reason
    assert await counts(database) == (1, 1, 0, 0)
