"""Disposable synthetic 4h event -> persisted forecast -> Paper risk fixture."""

import asyncio
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from multiprocessing import get_context
from uuid import uuid4

import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.forecast_models import ForecastDecision, ForecastProposal
from ai_futures.main import create_app
from ai_futures.model_bot import ModelBotService
from ai_futures.model_paper import ModelPaperPolicy
from ai_futures.models import Candle, InstrumentRules, WorkerHeartbeat
from ai_futures.news_dataset import _hash
from ai_futures.news_forecast import CandidateRules, ProposalPolicy, issue_test_approval
from ai_futures.news_models import AnnouncementCoverageObservation, AnnouncementVersion
from ai_futures.news_training import ModelArtifact
from ai_futures.paper_models import PaperFill, PaperIntent, PaperOrder, PaperVerdict
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.integration.test_market import market_db  # noqa: F401
from tests.integration.test_paper import feed, paper  # noqa: F401
from tests.unit.test_news_forecast import CONFIG, artifact, at


class RedisStub:
    async def aclose(self):
        pass


async def seed_event(database, *, news=True, coverage=True):
    event_id = uuid4()
    async with database.sessions.begin() as session:
        for index, close in ((1, "100"), (2, "101.05")):
            session.add(
                Candle(
                    symbol="BTC_USDT",
                    timeframe="4h",
                    window_start=at(4 * index),
                    event_id=event_id if index == 2 else uuid4(),
                    source_time=at(4 * index + 4),
                    received_at=at(4 * index + 4),
                    payload={"close": close},
                    payload_hash=f"{index + 1:064x}",
                    final=True,
                    source="synthetic",
                    config_version="fixture-v1",
                )
            )
        if news:
            session.add(
                AnnouncementVersion(
                    canonical_id="synthetic-notice-one",
                    canonical_url="https://example.invalid/synthetic-notice-one",
                    version_number=1,
                    title="Synthetic BTC futures notice",
                    language="en-US",
                    post_time=at(10),
                    first_seen_at=at(11),
                    fetched_at=at(11),
                    available_at=at(11),
                    payload_hash="a" * 64,
                    observation_kind="forward",
                    source="synthetic",
                    schema_version="fixture-v1",
                    config_version="fixture-v1",
                )
            )
        if coverage:
            session.add(
                AnnouncementCoverageObservation(
                    source="synthetic",
                    language="en-US",
                    source_version="synthetic-coverage-v1",
                    config_version="fixture-v1",
                    scan_id="synthetic-scan-one",
                    observed_at=at(12),
                    status="ok",
                    reported_total_pages=1,
                    seen_pages=[1],
                    page_hashes={"1": "b" * 64},
                    internal_gap_pages=[],
                    unseen_page_count=0,
                    page_shift_detected=False,
                    last_success_at=at(12),
                    error_code=None,
                )
            )
    return event_id


async def setup(database, settings, *, news=True, coverage=True):
    await feed(database, settings, price="101.1")
    event_id = await seed_event(database, news=news, coverage=coverage)
    async with database.sessions() as session:
        rules_version = (await session.get(InstrumentRules, "BTC_USDT")).version
    enabled = settings.model_copy(
        update={
            "forecast_fixture_enabled": True,
            "model_paper_fixture_enabled": True,
            "model_bot_fixture_enabled": True,
        }
    )
    fields = asdict(artifact())
    fields.pop("checksum")
    fields["source_versions"] = tuple(
        sorted(
            {
                CONFIG.source_version,
                "synthetic:fixture-v1",
                "synthetic-coverage-v1:fixture-v1",
            }
            | ({"synthetic:fixture-v1:fixture-v1"} if news else set())
        )
    )
    model = ModelArtifact(**fields, checksum=_hash(fields))
    approval = issue_test_approval(model, approved_at=at(12), expires_at=at(13))
    rules = CandidateRules(rules_version, at(12), Decimal("0.1"), Decimal("1"), True)
    args = (
        event_id,
        model,
        approval,
        rules,
        ProposalPolicy("synthetic-proposal-v1"),
        ModelPaperPolicy(Decimal("10.9")),
    )
    return enabled, args


async def counts(database):
    async with database.sessions() as session:
        result = []
        for table in (
            ForecastDecision,
            ForecastProposal,
            PaperIntent,
            PaperVerdict,
            PaperOrder,
            PaperFill,
        ):
            result.append(await session.scalar(select(func.count()).select_from(table)))
        return tuple(result)


def child_tick(database_url, portfolio_id, args, queue):
    async def run():
        settings = Settings(
            _env_file=None,
            app_env="test",
            backend_auth_token="x" * 48,
            database_url=database_url,
            forecast_fixture_enabled=True,
            model_paper_fixture_enabled=True,
            model_bot_fixture_enabled=True,
        )
        database = Database(settings)
        try:
            result = await ModelBotService(database, settings).tick(
                args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
            )
            return result.status, result.reason, str(result.forecast_id)
        finally:
            await database.close()

    try:
        queue.put(asyncio.run(run()))
    except Exception as exc:  # pragma: no cover - delivered to parent assertion
        queue.put(("error", repr(exc), ""))


async def test_auto_tick_persisted_api_replay_restart_two_processes(paper):  # noqa: F811
    database, settings, _, portfolio_id = paper
    enabled, args = await setup(database, settings)
    # Separate spawned Python processes race to create the first forecast and Paper fill.
    spawn = get_context("spawn")
    queue = spawn.Queue()
    processes = [
        spawn.Process(
            target=child_tick,
            args=(enabled.database_url.get_secret_value(), portfolio_id, args, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert all(result[0] == "submitted" for result in results), results
    assert len({result[2] for result in results}) == 1
    assert await counts(database) == (1, 1, 1, 1, 1, 1)

    service = ModelBotService(database, enabled)
    first = await service.tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert first.status == "submitted" and first.paper.paper_result["risk"]["outcome"] == "ALLOW"
    assert await counts(database) == (1, 1, 1, 1, 1, 1)
    async with database.sessions.begin() as session:
        session.add(
            AnnouncementVersion(
                canonical_id="synthetic-notice-one",
                canonical_url="https://example.invalid/synthetic-notice-one",
                version_number=2,
                title="Edited after anchor",
                language="en-US",
                post_time=at(10),
                first_seen_at=at(15),
                fetched_at=at(15),
                available_at=at(15),
                payload_hash="c" * 64,
                observation_kind="forward",
                source="synthetic",
                schema_version="fixture-v1",
                config_version="fixture-v1",
            )
        )
        session.add(
            Candle(
                symbol="BTC_USDT",
                timeframe="4h",
                window_start=at(12),
                event_id=uuid4(),
                source_time=at(16),
                received_at=at(16),
                payload={"close": "999"},
                payload_hash="d" * 64,
                final=True,
                source="synthetic",
                config_version="fixture-v1",
            )
        )
    replay = await ModelBotService(database, enabled).tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert replay == first
    assert await counts(database) == (1, 1, 1, 1, 1, 1)

    app = create_app(enabled, database, RedisStub())
    app.state.database = database
    app.state.settings = enabled
    headers = {"Authorization": f"Bearer {enabled.backend_auth_token.get_secret_value()}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/research/forecasts/latest", headers=headers)
    assert response.status_code == 200
    view = response.json()
    assert view["mode"] == "collect-only" and not view["trading_enabled"]
    assert view["forecast"]["forecast_id"] == str(first.forecast_id)
    assert view["forecast"]["news_version_ids"]
    assert view["forecast"]["test_fixture"] is True
    async with database.sessions.begin() as session:
        current = await session.get(Candle, ("BTC_USDT", "4h", at(8)))
        current.payload = {"close": "999"}
        current.payload_hash = "e" * 64
    corrected = await ModelBotService(database, enabled).tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert corrected.reason == "forecast_already_sealed"
    assert await counts(database) == (1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("coverage", "news_coverage_missing"),
        ("artifact", "artifact_missing"),
        ("approval", "artifact_not_approved"),
        ("stale", "DATA_STALE"),
        ("kill", "KILL_SWITCH"),
        ("worker", "WORKER_STALE"),
        ("rules", "rules_mismatch"),
        ("corrupt", "artifact_invalid"),
        ("expired", "approval_invalid"),
        ("malformed", "market_invalid"),
        ("duplicate_event", "duplicate_final_event_id"),
    ],
)
async def test_auto_tick_fail_closed(paper, fault, expected):  # noqa: F811
    database, settings, paper_service, portfolio_id = paper
    enabled, args = await setup(database, settings, coverage=fault != "coverage")
    if fault == "artifact":
        args = (args[0], None, *args[2:])
    elif fault == "approval":
        args = (*args[:2], None, *args[3:])
    elif fault == "stale":
        from ai_futures.market import MarketStore

        await MarketStore(database, settings.config_version).quality("stale", "synthetic_fault")
    elif fault == "kill":
        await paper_service.kill(True)
    elif fault == "worker":
        async with database.sessions.begin() as session:
            heartbeat = await session.get(WorkerHeartbeat, "trading")
            from ai_futures.schemas import utc_now

            heartbeat.occurred_at = utc_now() - timedelta(minutes=1)
    elif fault == "rules":
        async with database.sessions.begin() as session:
            active = await session.get(InstrumentRules, "BTC_USDT")
            active.version = "changed-after-proposal"
    elif fault == "corrupt":
        args = (args[0], replace(args[1], threshold="0.99"), *args[2:])
    elif fault == "expired":
        args = (*args[:2], replace(args[2], expires_at=at(12)), *args[3:])
    elif fault == "malformed":
        async with database.sessions.begin() as session:
            current = await session.get(Candle, ("BTC_USDT", "4h", at(8)))
            current.payload = {}
    elif fault == "duplicate_event":
        async with database.sessions.begin() as session:
            session.add(
                Candle(
                    symbol="BTC_USDT",
                    timeframe="4h",
                    window_start=at(12),
                    event_id=args[0],
                    source_time=at(16),
                    received_at=at(16),
                    payload={"close": "102"},
                    payload_hash="d" * 64,
                    final=True,
                    source="synthetic",
                    config_version="fixture-v1",
                )
            )
    result = await ModelBotService(database, enabled).tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert result.reason == expected
    assert (await counts(database))[-2:] == (0, 0)


async def test_auto_tick_disabled_missing_event_and_unknown_order(paper):  # noqa: F811
    database, settings, _, portfolio_id = paper
    enabled, args = await setup(database, settings)
    with pytest.raises(PermissionError, match="hard-disabled"):
        await ModelBotService(database, settings).tick(
            args[0], portfolio_id, *args[1:], config=CONFIG
        )
    assert (
        await ModelBotService(database, enabled).tick(
            uuid4(), portfolio_id, *args[1:], config=CONFIG
        )
    ).status == "no_event"
    first = await ModelBotService(database, enabled).tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert first.status == "submitted"
    async with database.sessions.begin() as session:
        order = await session.scalar(select(PaperOrder))
        order.status = "UNKNOWN"
    blocked = await ModelBotService(database, enabled).tick(
        args[0], portfolio_id, *args[1:], config=CONFIG, now=at(12) + timedelta(minutes=1)
    )
    assert blocked.reason == "order_unknown"
    assert await counts(database) == (1, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError, match="test-only"):
        Settings(
            _env_file=None,
            app_env="local",
            backend_auth_token="x" * 48,
            database_url="postgresql+asyncpg://localhost/unused",
            model_bot_fixture_enabled=True,
        )
