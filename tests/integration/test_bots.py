import asyncio
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from ai_futures.bot_models import Bot, BotConfig, BotDecision
from ai_futures.bots import BotCreate, BotError, BotService
from ai_futures.main import create_app
from ai_futures.market import MarketStore
from ai_futures.market_worker import MarketIngestion
from ai_futures.models import AppState, Candle, InstrumentRules
from ai_futures.paper import RiskProfile
from ai_futures.paper_models import (
    PaperFill,
    PaperIntent,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
)
from ai_futures.schemas import utc_now
from ai_futures.strategies import StrategyConfig
from sqlalchemy import func, select

from tests.integration.test_market import market_db, ticker_message  # noqa: F401
from tests.integration.test_paper import feed


def config(strategy="trend_following"):
    return StrategyConfig(
        strategy=strategy,
        timeframe="1m",
        quantity="10",
        leverage="1",
        stop_distance="0.01",
        fast_period=2,
        slow_period=3,
        breakout_period=2,
        breakout_buffer="0",
        min_volume_ratio="1",
    )


@pytest.fixture
async def bots(market_db):  # noqa: F811
    db, cfg = market_db
    async with db.sessions.begin() as session:
        session.add(AppState(config_version=cfg.config_version, kill_switch=False))
    await feed(db, cfg)
    return db, cfg, BotService(db, cfg.config_version)


async def add_candles(db, cfg, closes):
    now = utc_now().replace(second=0, microsecond=0)
    start = now - timedelta(minutes=len(closes) + 2)
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            (
                start + timedelta(minutes=i),
                {
                    "open": str(close),
                    "close": str(close),
                    "high": str(close + 1),
                    "low": str(close - 1),
                    "vol": "100",
                    "amount": str(close * 100),
                },
                True,
            )
        )
    await MarketStore(db, cfg.config_version).candles("1m", rows, utc_now())
    return rows


async def create_bot(svc, strategy="trend_following"):
    row = await svc.create(
        BotCreate(
            name=strategy,
            deposit="1000",
            risk_profile=RiskProfile(),
            config=config(strategy),
        )
    )
    return UUID(row["id"]), UUID(row["portfolio_id"])


async def test_paper_bot_replay_restart_and_journal(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, portfolio_id = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m",
        [(last, {**rows[-1][1], "open": "100", "close": "100", "high": "101", "low": "99"}, True)],
        utc_now(),
    )
    await svc.tick()
    decisions = await svc.decisions(bot_id)
    assert len(decisions) == 1
    assert decisions[0]["action"] == "OPEN"
    assert decisions[0]["reason"] == "FAST_CROSSED_ABOVE_SLOW"
    assert decisions[0]["risk_reason"] == "RISK_OK"
    assert decisions[0]["status"] == "DONE"
    assert decisions[0]["intent_id"] and decisions[0]["risk_id"] and decisions[0]["order_id"]
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperFill)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperPosition)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1
    restarted = BotService(db, cfg.config_version)
    await restarted.tick()
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "101"}, True)], utc_now()
    )
    await restarted.tick()
    assert len(await restarted.decisions(bot_id)) == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperFill)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1
        assert (await session.scalar(select(BotDecision))).portfolio_id == portfolio_id


async def test_three_bots_independent_and_pause_kill(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    created = [
        await create_bot(svc, strategy) for strategy in ("trend_following", "pullback", "breakout")
    ]
    assert len({portfolio_id for _, portfolio_id in created}) == 3
    for bot_id, _ in created:
        await svc.transition(bot_id, "ready")
        await svc.transition(bot_id, "start")
    await svc.transition(created[1][0], "pause")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "100", "high": "101", "low": "99"}, True)], utc_now()
    )
    await svc.tick()
    assert len(await svc.decisions(created[0][0])) == 1
    assert not await svc.decisions(created[1][0])
    assert len(await svc.decisions(created[2][0])) == 1
    await svc.paper.kill(True)
    with pytest.raises(BotError) as exc:
        await svc.transition(created[1][0], "start")
    assert exc.value.code == "KILL_SWITCH"
    next_window = last + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(next_window, {**rows[-1][1], "close": "100"}, True)], utc_now()
    )
    await svc.tick()
    assert (await svc.get(created[0][0]))["status"] == "RISK_HALTED"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperPosition)) <= 2


async def test_processing_recovered_after_failed_finalization(bots, monkeypatch):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "100", "high": "101", "low": "99"}, True)], utc_now()
    )
    decision_id = await svc._claim(bot_id)
    assert decision_id
    original = svc.paper.submit

    async def crash_after_submit(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected crash after paper commit")

    monkeypatch.setattr(svc.paper, "submit", crash_after_submit)
    with pytest.raises(RuntimeError):
        await svc._deliver(bot_id, decision_id)
    restarted = BotService(db, cfg.config_version)
    await restarted.tick()
    assert (await restarted.decisions(bot_id))[0]["status"] == "DONE"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperFill)) == 1


async def test_stale_and_pause_skip_events(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    await MarketStore(db, cfg.config_version).quality("stale", "injected_disconnect")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "100"}, True)], utc_now()
    )
    await svc.tick()
    assert (await svc.decisions(bot_id))[0]["reason"] == "DATA_STALE"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
    await feed(db, cfg)
    await svc.transition(bot_id, "pause")
    paused_window = last + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(paused_window, {**rows[-1][1], "close": "100"}, True)], utc_now()
    )
    await svc.tick()
    assert len(await svc.decisions(bot_id)) == 1
    await svc.transition(bot_id, "start")
    await svc.tick()
    assert len(await svc.decisions(bot_id)) == 1


async def test_disconnect_gap_reconciliation_replay_does_not_redecide(bots, monkeypatch):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    gap_window = rows[-1][0] + timedelta(minutes=2)  # One missing final window.
    gap_payload = {**rows[-1][1], "close": "100", "high": "101", "low": "96"}
    worker = MarketIngestion(db, cfg, None)
    await worker.disconnect("injected_public_ws_disconnect")
    await worker.store.candles("1m", [(gap_window, gap_payload, True)], utc_now(), "rest")
    await svc.tick()
    decisions = await svc.decisions(bot_id)
    assert len(decisions) == 1
    assert decisions[0]["reason"] == "DATA_STALE"
    assert decisions[0]["action"] == "WAIT"
    assert decisions[0]["window_start"] == gap_window.isoformat()
    async with db.sessions() as session:
        rules = await session.get(InstrumentRules, "BTC_USDT")
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0

    class ControlledPublic:
        async def server_time(self):
            return utc_now()

        async def contract_rules(self, _symbol):
            return rules.payload

        async def ticker(self, _symbol):
            return {"lastPrice": "100"}, utc_now()

    async def replay_backfill():
        for _ in range(2):
            await worker.store.candles("1m", [(gap_window, gap_payload, True)], utc_now(), "rest")

    worker.adapter = ControlledPublic()
    monkeypatch.setattr(worker, "refresh_context", replay_backfill)
    worker.connected = True
    await worker.reconcile()
    await worker.handle(ticker_message())
    await BotService(db, cfg.config_version).tick()
    await svc.tick()
    assert len(await svc.decisions(bot_id)) == 1
    async with db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Candle).where(Candle.window_start == gap_window)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0


async def test_unknown_order_state_blocks_new_entry(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "100", "high": "101"}, True)], utc_now()
    )
    await svc.tick()
    async with db.sessions.begin() as session:
        order = await session.scalar(select(PaperOrder))
        order.status = "BROKER_UNKNOWN"
    next_window = last + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(next_window, {**rows[-1][1], "close": "100"}, True)], utc_now()
    )
    await svc.tick()
    assert (await svc.decisions(bot_id))[0]["reason"] == "ORDER_UNRESOLVED"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1


async def test_future_final_candle_never_enters(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    future_window = rows[-1][0] + timedelta(minutes=4)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(future_window, {**rows[-1][1], "close": "100"}, True)], utc_now()
    )
    await svc.tick()
    assert (await svc.decisions(bot_id))[0]["reason"] == "CANDLE_FUTURE"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0


async def test_concurrent_scheduler_calls_do_not_duplicate(bots):
    db, cfg, svc = bots
    rows = await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    await svc.transition(bot_id, "ready")
    await svc.transition(bot_id, "start")
    last = rows[-1][0] + timedelta(minutes=1)
    await MarketStore(db, cfg.config_version).candles(
        "1m", [(last, {**rows[-1][1], "close": "100", "high": "101"}, True)], utc_now()
    )
    await asyncio.gather(svc.tick(), BotService(db, cfg.config_version).tick())
    await svc.tick()
    assert len(await svc.decisions(bot_id)) == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperFill)) == 1


async def test_config_versions_apply_only_at_start(bots):
    db, cfg, svc = bots
    await add_candles(db, cfg, [98, 98, 97])
    bot_id, _ = await create_bot(svc)
    updated = await svc.configure(bot_id, config("breakout"))
    assert updated["latest_version"] == 2
    await svc.transition(bot_id, "ready")
    started = await svc.transition(bot_id, "start")
    assert started["active_version"] == 2
    with pytest.raises(BotError) as exc:
        await svc.configure(bot_id, config("pullback"))
    assert exc.value.code == "BOT_CONFIG_LOCKED"
    await svc.transition(bot_id, "pause")
    paused = await svc.configure(bot_id, config("pullback"))
    assert paused["latest_version"] == 3 and paused["active_version"] == 2
    resumed = await svc.transition(bot_id, "start")
    assert resumed["active_version"] == 3


async def test_learned_paper_draft_api_roundtrip_validation_and_closed_gate(bots):
    db, cfg, svc = bots
    app = create_app(cfg, database=db)
    app.state.settings = cfg
    app.state.database = db
    auth = {"Authorization": "Bearer " + "x" * 48}
    body = {
        "name": "Learning draft",
        "deposit": "1000",
        "config": {
            "kind": "learned_paper",
            "max_position_notional_usdt": "1500.25",
            "leverage": "2.5",
            "stop_loss_pct": "1.5",
            "take_profit_pct": "3.25",
        },
    }
    for boundary in ("1", "3"):
        parsed = BotCreate.model_validate(
            {**body, "config": {**body["config"], "leverage": boundary}}
        )
        assert parsed.config.model_dump(mode="json")["leverage"] == boundary
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for bad_config in (
            {**body["config"], "kind": "unknown"},
            {**body["config"], "max_position_notional_usdt": "0"},
            {**body["config"], "max_position_notional_usdt": 1.5},
            {key: value for key, value in body["config"].items() if key != "leverage"},
            {**body["config"], "leverage": "0.999"},
            {**body["config"], "leverage": "3.001"},
            {**body["config"], "leverage": "NaN"},
            {**body["config"], "leverage": 2.5},
            {**body["config"], "stop_loss_pct": "0"},
            {**body["config"], "stop_loss_pct": "100"},
            {**body["config"], "take_profit_pct": "0"},
            {**body["config"], "take_profit_pct": "100"},
            {**body["config"], "take_profit_pct": "101"},
            {**body["config"], "quantity": "10"},
        ):
            response = await client.post(
                "/api/v1/bots", json={**body, "config": bad_config}, headers=auth
            )
            assert response.status_code == 422, response.text
        response = await client.post("/api/v1/bots", json=body, headers=auth)
        assert response.status_code == 200, response.text
        created = response.json()
        bot_id = UUID(created["id"])
        assert created["status"] == "DRAFT"
        assert created["mode"] == "paper"
        assert created["bot_kind"] == "learned_paper"
        assert created["readiness_code"] == "MODEL_NOT_APPROVED"
        assert created["config"] == body["config"]
        assert (await client.get(f"/api/v1/bots/{bot_id}")).json()["config"] == body["config"]
        listed = (await client.get("/api/v1/bots")).json()
        assert listed[0]["bot_kind"] == "learned_paper"
        assert listed[0]["config"] == body["config"]
        for action in ("ready", "start"):
            rejected = await client.post(f"/api/v1/bots/{bot_id}/{action}", headers=auth)
            assert rejected.status_code == 409
            assert rejected.json()["code"] == "MODEL_NOT_APPROVED"
        rejected = await client.post(
            f"/api/v1/bots/{bot_id}/config", json=config().model_dump(mode="json"), headers=auth
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "MODEL_NOT_APPROVED"
    async with db.sessions() as session:
        portfolio = await session.get(PaperPortfolio, UUID(created["portfolio_id"]))
        assert portfolio.name == body["name"]
        assert portfolio.risk_profile == RiskProfile().model_dump(mode="json")
        assert (await session.get(BotConfig, (bot_id, 1))).payload == body["config"]
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    assert (await BotService(db, cfg.config_version).get(bot_id))["bot_kind"] == "learned_paper"


async def test_forced_running_learned_draft_tick_cannot_submit(bots):
    db, cfg, svc = bots
    await add_candles(db, cfg, [98, 98, 97, 100])
    created = await svc.create(
        BotCreate(
            name="blocked draft",
            deposit="1000",
            config={
                "kind": "learned_paper",
                "max_position_notional_usdt": "500",
                "leverage": "2",
                "stop_loss_pct": "2",
                "take_profit_pct": "4",
            },
        )
    )
    bot_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        bot = await session.get(Bot, bot_id)
        bot.status = "RUNNING"
        bot.active_version = 1
    await svc.tick()
    await BotService(db, cfg.config_version).tick()
    assert (await svc.get(bot_id))["status"] == "DRAFT"
    assert (await svc.get(bot_id))["readiness_code"] == "MODEL_NOT_APPROVED"
    assert await svc.decisions(bot_id) == []
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
