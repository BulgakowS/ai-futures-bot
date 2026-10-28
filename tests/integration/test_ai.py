import json
from datetime import timedelta
from decimal import Decimal as D
from uuid import UUID, uuid4

import httpx
import pytest
from ai_futures.ai import AiFailure, AiPolicy, AiRequest, AiService, digest
from ai_futures.ai_models import AiAnalysis, AiUsage
from ai_futures.market import MarketStore
from ai_futures.models import AppState, MarketSnapshot
from ai_futures.paper import OrderRequest, PaperService, PortfolioCreate
from ai_futures.paper_models import PaperIntent, PaperOrder, PaperVerdict
from ai_futures.schemas import utc_now
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.integration.test_market import market_db  # noqa: F401
from tests.integration.test_paper import feed


class FakeAdapter:
    def __init__(self, failure=None, action="LONG"):
        self.failure = failure
        self.action = action
        self.calls = 0

    async def propose(self, context, policy):
        self.calls += 1
        if self.failure:
            raise self.failure
        return {
            "proposal": {
                "action": self.action, "confidence": "0.6", "horizon": policy.timeframe,
                "reason_codes": ["TEST_SIGNAL"], "explanation": "Fixture signal",
                "invalidation": "Price crosses stop", "entry": "100",
                "stop_loss": "99", "take_profit": None,
                "context_id": context.context_id,
            },
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }


def policy(**changes):
    base = {
        "model": "test-model", "prompt_version": "prompt-v1",
        "system_prompt": "Analyze bounded market data. Return a proposal.",
        "quantity": "10", "leverage": "1", "daily_usd": "1",
        "monthly_usd": "10", "input_usd_per_million": "1",
        "output_usd_per_million": "2", "submit_to_paper": True,
    }
    return AiPolicy(**{**base, **changes})


@pytest.fixture
async def ai_market(market_db):  # noqa: F811
    db, cfg = market_db
    async with db.sessions.begin() as session:
        session.add(AppState(config_version=cfg.config_version, kill_switch=False))
    await feed(db, cfg)
    end = utc_now().replace(second=0, microsecond=0)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(end - timedelta(minutes=1), {
            "open": "100", "high": "101", "low": "99", "close": "100",
            "vol": "100", "amount": "10000",
        }, True)], utc_now(),
    )
    paper = PaperService(db, cfg.config_version)
    portfolio = await paper.create(PortfolioCreate())
    enabled = cfg.model_copy(update={"ai_enabled": True, "openai_api_key": SecretStr("fake-key")})
    return db, cfg, enabled, paper, UUID(portfolio["id"])


async def test_valid_proposal_only_reaches_paper_through_risk_and_dedupes(ai_market):
    db, _, settings, _, pid = ai_market
    fake = FakeAdapter()
    service = AiService(db, settings, fake)
    request = AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    first = await service.analyze(request)
    assert first["status"] == "DONE" and first["action"] == "LONG"
    assert first["intent_id"] and first["risk_id"] and first["order_id"]
    assert first["risk_reason"] == "RISK_OK"
    assert await AiService(db, settings, fake).analyze(request) == first
    assert fake.calls == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperVerdict)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 1
        usage = await session.get(AiUsage, request.analysis_id)
        assert usage.input_tokens == 100 and usage.output_tokens == 50
        assert usage.estimated_usd == D("0.0002")
        analysis = await session.get(AiAnalysis, request.analysis_id)
        assert analysis.context["version"] == "market-context-v1"
        assert len(analysis.context["candles"]) == 1
        assert "system_prompt" not in analysis.policy


async def test_no_key_wait_and_rule_based_paper_still_works(ai_market):
    db, cfg, settings, paper, pid = ai_market
    missing = settings.model_copy(update={"openai_api_key": None})
    fake = FakeAdapter()
    result = await AiService(db, missing, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert result["action"] == "WAIT" and result["reason"] == "AI_DISABLED"
    assert fake.calls == 0
    direct = await paper.submit(pid, OrderRequest(
        idempotency_key="rule-without-ai", direction="LONG", quantity="10", stop_loss="99",
    ))
    assert direct["risk"]["outcome"] == "ALLOW"
    assert direct["order"] is not None
    assert cfg.openai_api_key is None


@pytest.mark.parametrize("failure,reason", [
    (httpx.ReadTimeout("fixture"), "TIMEOUT"),
    (httpx.HTTPStatusError("429", request=httpx.Request("POST", "https://example.test"),
                            response=httpx.Response(429)), "PROVIDER_ERROR"),
    (AiFailure("RATE_LIMIT"), "RATE_LIMIT"),
    (ValueError("bad schema"), "SCHEMA_ERROR"),
])
async def test_provider_failures_wait_without_order(ai_market, failure, reason):
    db, _, settings, _, pid = ai_market
    fake = FakeAdapter(failure=failure)
    result = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert result["action"] == "WAIT" and result["reason"] == reason
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0


async def test_stale_budget_and_invalid_response_fail_closed(ai_market):
    db, cfg, settings, _, pid = ai_market
    fake = FakeAdapter()
    await MarketStore(db, cfg.config_version).quality("stale", "fixture")
    stale = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert stale["action"] == "WAIT" and stale["reason"] == "STALE_CONTEXT"
    assert fake.calls == 0
    await MarketStore(db, cfg.config_version).quality("ok", "fixture")
    budget = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy(daily_usd="0.0001"))
    )
    assert budget["action"] == "WAIT" and budget["reason"] == "BUDGET_EXHAUSTED"
    assert fake.calls == 0
    fake.action = "WRONG"
    invalid_id = uuid4()
    invalid = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=invalid_id, portfolio_id=pid, policy=policy())
    )
    assert invalid["action"] == "WAIT" and invalid["reason"] == "SCHEMA_ERROR"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        assert (await session.get(AiUsage, invalid_id)).input_tokens == 100


async def test_candle_gap_waits_before_provider_call(ai_market):
    db, cfg, settings, _, pid = ai_market
    fake = FakeAdapter()
    end = utc_now().replace(second=0, microsecond=0)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(end - timedelta(minutes=3), {
            "open": "100", "high": "101", "low": "99", "close": "100",
            "vol": "100", "amount": "10000",
        }, True)], utc_now(),
    )
    result = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert result["action"] == "WAIT" and result["reason"] == "CANDLE_GAP"
    assert fake.calls == 0


async def test_malformed_quality_waits_before_provider_call(ai_market):
    db, _, settings, _, pid = ai_market
    fake = FakeAdapter()
    async with db.sessions.begin() as session:
        quality = await session.get(MarketSnapshot, ("BTC_USDT", "quality"))
        quality.status = "stale"
        quality.payload = {}
    result = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert result["action"] == "WAIT" and result["reason"] == "INVALID_CONTEXT"
    assert fake.calls == 0


async def test_kill_switch_forces_ai_proposal_through_risk(ai_market):
    db, _, settings, paper, pid = ai_market
    await paper.kill(True)
    result = await AiService(db, settings, FakeAdapter()).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    )
    assert result["risk_id"] and result["risk_reason"] == "KILL_SWITCH"
    assert result["order_id"] is None


async def test_monthly_request_cap_and_uncertain_request_recovery(ai_market):
    db, _, settings, _, pid = ai_market
    fake = FakeAdapter(action="WAIT")
    cap_policy = policy(submit_to_paper=False, daily_requests=1, monthly_requests=1)
    first_id = uuid4()
    await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=first_id, portfolio_id=pid, policy=cap_policy)
    )
    async with db.sessions.begin() as session:
        usage = await session.get(AiUsage, first_id)
        usage.occurred_at = utc_now() - timedelta(days=1)
    blocked = await AiService(db, settings, fake).analyze(
        AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=cap_policy)
    )
    assert blocked["action"] == "WAIT" and blocked["reason"] == "BUDGET_EXHAUSTED"
    assert fake.calls == 1
    uncertain_id = uuid4()
    request = AiRequest(analysis_id=uncertain_id, portfolio_id=pid, policy=cap_policy)
    async with db.sessions.begin() as session:
        session.add(AiAnalysis(
            id=uncertain_id, portfolio_id=pid,
            request_hash=digest(request.model_dump(mode="json")),
            status="REQUESTED", action="WAIT", reason="IN_PROGRESS",
            model=cap_policy.model, prompt_version=cap_policy.prompt_version,
            policy={}, occurred_at=utc_now() - timedelta(minutes=1),
            config_version=settings.config_version,
        ))
        session.add(AiUsage(
            analysis_id=uncertain_id, portfolio_id=pid, attempted=True,
            reserved_usd=cap_policy.reservation(),
            occurred_at=utc_now() - timedelta(minutes=1),
            config_version=settings.config_version,
        ))
    result = await AiService(db, settings, fake).analyze(request)
    assert result["status"] == "DONE" and result["reason"] == "REQUEST_UNCERTAIN"
    assert fake.calls == 1


async def test_secret_absent_from_journal_and_public_result(ai_market):
    db, _, settings, _, pid = ai_market
    secret = "fake-key"
    fake = FakeAdapter()
    request = AiRequest(analysis_id=uuid4(), portfolio_id=pid, policy=policy())
    result = await AiService(db, settings, fake).analyze(request)
    async with db.sessions() as session:
        row = await session.get(AiAnalysis, request.analysis_id)
        usage = await session.get(AiUsage, request.analysis_id)
        serialized = json.dumps({
            "result": result, "analysis": row.policy, "context": row.context,
            "proposal": row.proposal, "usage": str(usage.estimated_usd),
        })
    assert secret not in serialized and "OPENAI_API_KEY" not in serialized
