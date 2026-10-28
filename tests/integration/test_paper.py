from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import UUID, uuid4

import pytest
from ai_futures.funding import reconcile_funding
from ai_futures.market import MarketStore
from ai_futures.market_worker import MarketIngestion
from ai_futures.mexc import normalize_rules, payload_hash
from ai_futures.models import (
    AppState,
    Candle,
    FundingCoverage,
    FundingSettlement,
    MarketSnapshot,
    WorkerHeartbeat,
)
from ai_futures.paper import (
    CloseRequest,
    OrderRequest,
    PaperError,
    PaperService,
    PortfolioCreate,
    ResetRequest,
    RiskProfile,
    register_paper_routes,
)
from ai_futures.paper_models import (
    PaperEpoch,
    PaperFill,
    PaperLedger,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
)
from ai_futures.schemas import utc_now
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.integration.test_market import market_db  # noqa: F401


async def feed(database, config, price="100", volume="10000", stale=False):
    store = MarketStore(database, config.config_version)
    await store.rules(
        {
            "version": "v1",
            "state": 0,
            "contractSize": "0.01",
            "priceUnit": "0.01",
            "volUnit": "1",
            "minVol": "1",
            "maxVol": "1000000",
            "minLeverage": "1",
            "maxLeverage": "100",
            "takerFeeRate": "0.0004",
            "makerFeeRate": "0.0001",
            "feeRateMode": "NORMAL",
            "feeRateType": "BASE",
            "tieredFeeRates": [],
            "leverageFeeRates": [],
            "maintenanceMarginRate": "0.005",
            "liquidationFeeRate": "0.001",
            "riskLimitMode": "CUSTOM",
            "riskLimitType": "BY_VOLUME",
            "riskLimitCustom": [{"level": 1, "maxVol": "1000000", "mmr": "0.005",
                                 "imr": "0.01", "maxLeverage": "100"}],
        }
    )
    now = utc_now() - timedelta(seconds=10 if stale else 0)
    await store.snapshot("ticker", {"ask1": price, "bid1": price, "fairPrice": price}, now, "ws")
    await store.snapshot("depth", {
        "version": int(now.timestamp()) * 1000000 + now.microsecond,
        "asks": [[price, volume]], "bids": [[price, volume]],
    }, now)
    await store.snapshot("funding_history", {"settlements": []}, now)
    await store.quality("ok", "test")
    async with database.sessions.begin() as session:
        await session.merge(WorkerHeartbeat(
            name="trading", instance_id=uuid4(), status="idle", occurred_at=utc_now(),
        ))
        await session.merge(
            FundingCoverage(
                symbol="BTC_USDT",
                covered_from=now - timedelta(days=1),
                covered_until=now + timedelta(hours=8),
                status="verified",
                gap_time=None,
                source="synthetic_test_fixture",
                snapshot_hash=payload_hash({}),
                observed_at=now,
                config_version=config.config_version,
            )
        )


async def add_settlement(db, cfg, when, rate):
    row = {"settleTime": when.isoformat(), "fundingRate": rate, "collectCycle": 8}
    async with db.sessions.begin() as session:
        session.add(
            FundingSettlement(
                symbol="BTC_USDT",
                settle_time=when,
                rate=rate,
                collect_cycle=8,
                source="synthetic_test_fixture",
                raw=row,
                raw_hash=payload_hash(row),
                observed_at=utc_now(),
                config_version=cfg.config_version,
            )
        )
        coverage = await session.get(FundingCoverage, "BTC_USDT")
        rows = (
            await session.scalars(
                select(FundingSettlement).where(FundingSettlement.symbol == "BTC_USDT")
            )
        ).all()
        coverage.snapshot_hash = payload_hash(
            {item.settle_time.isoformat(): item.raw for item in rows} | {when.isoformat(): row}
        )


@pytest.fixture
async def paper(market_db):  # noqa: F811
    db, cfg = market_db
    async with db.sessions.begin() as s:
        s.add(AppState(config_version=cfg.config_version, kill_switch=False))
    await feed(db, cfg)
    svc = PaperService(db, cfg.config_version)
    portfolio = await svc.create(PortfolioCreate())
    return db, cfg, svc, UUID(portfolio["id"])


def request(key="open", **kwargs):
    return OrderRequest(
        idempotency_key=key, direction="LONG", quantity="10", stop_loss="99", **kwargs
    )


async def account(svc, pid):
    async with svc.database.sessions() as s:
        return await svc.summary(s, await s.get(PaperPortfolio, pid))


async def test_disconnect_and_heartbeat_only_stall_block_new_entry_but_kill_allows_close(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(pid, request("initial"))
    assert opened["risk"]["outcome"] == "ALLOW"
    async with db.sessions() as session:
        position_id = (await session.scalar(select(PaperPosition.id).where(
            PaperPosition.portfolio_id == pid, PaperPosition.closed_at.is_(None),
        )))
    assert position_id is not None

    ingestion = MarketIngestion(db, cfg, None)
    ingestion.connected = ingestion.reconciled = True
    await ingestion.disconnect("injected_ws_disconnect")
    disconnected = await svc.submit(pid, request("after-disconnect"))
    assert disconnected["risk"]["reason"] == "DATA_STALE"
    assert disconnected["order"] is None

    await MarketStore(db, cfg.config_version).quality("ok", "controlled_reconnect")
    async with db.sessions.begin() as session:
        heartbeat = await session.get(WorkerHeartbeat, "trading")
        heartbeat.occurred_at = utc_now() - timedelta(seconds=cfg.worker_stale_seconds + 1)
    stalled = await svc.submit(pid, request("heartbeat-only-stall"))
    assert stalled["risk"]["reason"] == "WORKER_STALE"
    assert stalled["order"] is None
    assert stalled["risk"]["snapshot"]["quality"]["trade_eligible"]

    await svc.kill(True)
    closed = await svc.submit(pid, CloseRequest(idempotency_key="close-under-kill"), position_id)
    assert closed["risk"]["outcome"] == "ALLOW"
    assert closed["risk"]["snapshot"]["reduce_only"] is True
    assert closed["order"]["status"] == "FILLED"
    killed = await svc.submit(pid, request("killed-entry"))
    assert killed["risk"]["reason"] == "KILL_SWITCH" and killed["order"] is None


async def test_position_history_route_returns_model_provenance(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(pid, request())
    assert opened["order"]["status"] == "FILLED"
    app = FastAPI()
    app.state.database = db
    app.state.settings = cfg
    register_paper_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/portfolios/{pid}/positions")
        assert response.status_code == 200
        positions = response.json()
        assert len(positions) == 1
        assert positions[0]["liquidation_provenance"]["model"] == "model_estimate"
        assert positions[0]["liquidation_provenance"]["rules_version"]
        assert positions[0]["liquidation_provenance"]["mark_event_id"]
        await svc.submit(pid, CloseRequest(idempotency_key="close"), UUID(positions[0]["id"]))
        closed = await client.get(f"/api/v1/portfolios/{pid}/positions")
        assert closed.status_code == 200
        assert closed.json()[0]["closed_at"] is not None
        assert closed.json()[0]["liquidation_provenance"]["reason"] == "POSITION_CLOSED"


@pytest.mark.parametrize(
    "direction,close_price,expected_pnl", [("LONG", "110", "0.996"), ("SHORT", "90", "0.996")]
)
async def test_exact_long_short_accounting(paper, direction, close_price, expected_pnl):
    db, cfg, svc, pid = paper
    req = OrderRequest(
        idempotency_key="open",
        direction=direction,
        quantity="10",
        leverage="2",
        stop_loss="99" if direction == "LONG" else "101",
    )
    opened = await svc.submit(pid, req)
    assert opened["risk"]["outcome"] == "ALLOW"
    assert opened["order"]["status"] == "FILLED"
    summary = await account(svc, pid)
    position = summary["positions"][0]
    entry = D("100.02") if direction == "LONG" else D("99.98")
    fee = entry * D("0.1") * D("0.0004")
    assert D(summary["wallet_balance"]) == D("1000") - fee
    assert summary["initial_deposit"] == "1000.000000000000"
    assert D(summary["realized_pnl_net"]) == -fee
    assert D(summary["reserved_margin"]) == entry * D("0.1") / 2
    assert D(summary["available_balance"]) == D(summary["wallet_balance"]) - D(
        summary["reserved_margin"]
    )
    assert D(summary["equity"]) == D(summary["wallet_balance"]) + (D("100") - entry) * D("0.1") * (
        1 if direction == "LONG" else -1
    )
    assert D(summary["total_pnl_net"]) == D(summary["equity"]) - D(summary["initial_deposit"])
    await feed(db, cfg, close_price)
    await svc.kill(True)
    closed = await svc.submit(pid, CloseRequest(idempotency_key="close"), UUID(position["id"]))
    assert closed["risk"]["outcome"] == "ALLOW"
    assert closed["risk"]["snapshot"]["reduce_only"]
    summary = await account(svc, pid)
    assert not summary["positions"]
    exit_price = D("109.97") if direction == "LONG" else D("90.02")
    pnl = (exit_price - entry) * D("0.1") * (1 if direction == "LONG" else -1)
    assert D(summary["wallet_balance"]) == D("1000") - fee + pnl - exit_price * D("0.1") * D(
        "0.0004"
    )
    assert D(summary["realized_pnl_net"]) == D(summary["total_pnl_net"])
    assert D(summary["realized_pnl_net"]) == -fee + pnl - exit_price * D("0.1") * D("0.0004")
    assert summary["available_balance"] == summary["wallet_balance"]
    async with db.sessions() as s:
        entries = (
            await s.scalars(select(PaperLedger).where(PaperLedger.portfolio_id == pid))
        ).all()
        assert all(e.wallet_delta + e.offset_delta == 0 for e in entries)
        assert len((await s.scalars(select(PaperFill))).all()) == 2


async def test_idempotency_isolation_reset_restart(paper):
    db, cfg, svc, pid = paper
    req = request()
    first = await svc.submit(pid, req)
    assert await PaperService(db, cfg.config_version).submit(pid, req) == first
    with pytest.raises(PaperError, match="") as exc:
        await svc.submit(pid, req.model_copy(update={"quantity": D("11")}))
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    other = await svc.create(PortfolioCreate(name="other"))
    other_id = UUID(other["id"])
    assert D((await account(svc, other_id))["wallet_balance"]) == D("1000")
    position_id = UUID((await account(svc, pid))["positions"][0]["id"])
    rejected = await svc.submit(other_id, CloseRequest(idempotency_key="cross"), position_id)
    assert rejected["risk"]["reason"] == "POSITION_NOT_FOUND"
    with pytest.raises(PaperError) as exc:
        await svc.reset(pid, ResetRequest())
    assert exc.value.code == "RESET_OPEN_POSITIONS"
    await svc.submit(pid, CloseRequest(idempotency_key="close"), position_id)
    before_reset = await account(svc, pid)
    assert D(before_reset["realized_pnl_net"]) < 0
    reset = await svc.reset(pid, ResetRequest(deposit="500"))
    assert reset["epoch"] == 2 and D(reset["wallet_balance"]) == 500
    assert reset["initial_deposit"] == "500.000000000000"
    assert reset["realized_pnl_net"] == "0.000000000000"
    assert reset["total_pnl_net"] == "0.000000000000"
    assert await svc.submit(pid, req) == first  # same old result across reset, never opens again
    async with db.sessions() as s:
        assert (
            len((await s.scalars(select(PaperEpoch).where(PaperEpoch.portfolio_id == pid))).all())
            == 2
        )
        assert (
            len((await s.scalars(select(PaperLedger).where(PaperLedger.portfolio_id == pid))).all())
            == 5
        )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"leverage": D("4")}, "LEVERAGE_LIMIT"),
        ({"stop_loss": None}, "STOP_LOSS_INVALID"),
        ({"stop_loss": D("101")}, "STOP_LOSS_INVALID"),
        ({"stop_loss": D("99.001")}, "PRICE_PRECISION"),
        ({"quantity": D("0.5")}, "CONTRACT_SIZE"),
        ({"quantity": D("10.5")}, "QUANTITY_PRECISION"),
        ({"quantity": D("2000")}, "INSUFFICIENT_MARGIN"),
        ({"quantity": D("100"), "stop_loss": D("90")}, "TRADE_RISK_LIMIT"),
    ],
)
async def test_risk_rejections(paper, changes, reason):
    _, _, svc, pid = paper
    result = await svc.submit(pid, request().model_copy(update=changes))
    assert result["risk"]["reason"] == reason and result["order"] is None


async def test_position_count_and_aggregate_risk_limits(paper):
    db, _, svc, _ = paper
    capped = await svc.create(PortfolioCreate(
        name="count-cap", risk_profile=RiskProfile(max_positions=1),
    ))
    capped_id = UUID(capped["id"])
    pending = await svc.submit(capped_id, OrderRequest(
        idempotency_key="first-pending", direction="LONG", quantity="10",
        kind="limit", limit_price="90", stop_loss="89",
    ))
    assert pending["order"]["status"] == "PENDING"
    # BTC_USDT is the only supported instrument. A second symbol is injected solely
    # to reach the general portfolio count branch without the same-symbol conflict.
    async with db.sessions.begin() as session:
        order = await session.get(PaperOrder, UUID(pending["order"]["id"]))
        order.symbol = "OTHER_TEST"
    count_blocked = await svc.submit(capped_id, request("count-cap-attempt"))
    assert count_blocked["risk"]["reason"] == "MAX_POSITIONS"
    assert count_blocked["order"] is None

    risk_capped = await svc.create(PortfolioCreate(
        name="total-risk-cap", risk_profile=RiskProfile(max_total_risk=D("0.00001")),
    ))
    risk_blocked = await svc.submit(UUID(risk_capped["id"]), request("total-risk-attempt"))
    assert risk_blocked["risk"]["reason"] == "TOTAL_RISK_LIMIT"
    assert risk_blocked["order"] is None


async def test_stale_kill_loss_drawdown(paper):
    db, cfg, svc, pid = paper
    await svc.kill(True)
    assert (await svc.submit(pid, request("kill")))["risk"]["reason"] == "KILL_SWITCH"
    await svc.kill(False)
    async with db.sessions.begin() as s:
        ticker = await s.get(MarketSnapshot, ("BTC_USDT", "ticker"))
        ticker.source_time = utc_now() - timedelta(seconds=10)
    assert (await svc.submit(pid, request("stale")))["risk"]["reason"] == "DATA_STALE"
    await feed(db, cfg)
    async with db.sessions.begin() as s:
        portfolio = await s.get(PaperPortfolio, pid)
        svc.post(s, portfolio, pid, "scenario-loss", "SCENARIO", D("-31"))
    assert (await svc.submit(pid, request("daily")))["risk"]["reason"] == "DAILY_LOSS_LIMIT"
    async with db.sessions.begin() as s:
        epoch = await s.get(PaperEpoch, (pid, 1))
        epoch.day_start_equity = D("969")
        epoch.peak_equity = D("1200")
    assert (await svc.submit(pid, request("dd")))["risk"]["reason"] == "DRAWDOWN_LIMIT"


async def test_partial_pending_cancel_recovery_and_sl(paper):
    db, cfg, svc, pid = paper
    await feed(db, cfg, volume="30")
    partial = await svc.submit(pid, request())
    assert partial["order"]["filled_quantity"] == "3"
    assert partial["order"]["status"] == "PARTIALLY_FILLED_CANCELED"
    pos = UUID((await account(svc, pid))["positions"][0]["id"])
    close = await svc.submit(pid, CloseRequest(idempotency_key="reduce", quantity="4"), pos)
    assert close["risk"]["reason"] == "REDUCE_ONLY_VIOLATION"
    await feed(db, cfg, price="98", volume="10000")
    await svc.kill(True)
    await PaperService(db, cfg.config_version).tick()
    assert not (await account(svc, pid))["positions"]
    await svc.kill(False)
    await feed(db, cfg)
    limit = await svc.submit(pid, request("limit", kind="limit", limit_price="99.5"))
    assert limit["order"]["status"] == "PENDING"
    assert D((await account(svc, pid))["pending_order_reserve"]) > 0
    await svc.cancel(pid, UUID(limit["order"]["id"]))
    assert D((await account(svc, pid))["pending_order_reserve"]) == 0
    limit = await svc.submit(pid, request("limit2", kind="limit", limit_price="99.5"))
    await feed(db, cfg, price="99.4")
    await PaperService(db, cfg.config_version).tick()
    assert (await account(svc, pid))["positions"][0]["quantity"] == "10"


def public_rules(taker, maker, tier_cap=120000, mode="NORMAL", state=0):
    return normalize_rules({
        "symbol": "BTC_USDT", "futureType": 1, "settleCoin": "USDT", "state": state,
        "contractSize": "0.01", "priceUnit": "0.01", "volUnit": 1,
        "minVol": 1, "maxVol": 1000000, "minLeverage": 1, "maxLeverage": 100,
        "makerFeeRate": maker, "takerFeeRate": taker,
        "maintenanceMarginRate": "0.005", "initialMarginRate": "0.01",
        "liquidationFeeRate": "0.001", "feeRateMode": mode, "feeRateType": "BASE",
        "tieredFeeRates": [], "leverageFeeRates": [],
        "riskLimitMode": "CUSTOM",
        "riskLimitType": "BY_VOLUME",
        "riskLimitCustom": [{"level": 1, "maxVol": tier_cap, "mmr": "0.005",
                             "imr": "0.01", "maxLeverage": "100"}],
    }, "BTC_USDT")


async def test_fee_version_ack_and_exact_fill_ledger(paper):
    db, cfg, svc, pid = paper
    store = MarketStore(db, cfg.config_version)
    old = public_rules("0.0002", "0")
    assert await store.rules(old)
    assert (await svc.submit(pid, request("before-ack")))["risk"]["outcome"] == "REJECT"
    await svc.acknowledge("BTC_USDT", old["version"])
    first = await svc.submit(pid, request("old-fee"))
    assert first["order"]["status"] == "FILLED"
    new = public_rules("0.0004", "0.0001", tier_cap=310000)
    assert await store.rules(new)
    other = UUID((await svc.create(PortfolioCreate(name="new-fee")))["id"])
    assert (await svc.submit(other, request("changed")))["risk"]["outcome"] == "REJECT"
    with pytest.raises(PaperError) as conflict:
        await svc.acknowledge("BTC_USDT", old["version"])
    assert conflict.value.code == "RULES_VERSION_CONFLICT"
    await svc.acknowledge("BTC_USDT", new["version"])
    second = await svc.submit(other, request("new-fee"))
    assert second["order"]["status"] == "FILLED"
    assert await PaperService(db, cfg.config_version).submit(other, request("new-fee")) == second
    async with db.sessions() as session:
        fills = (await session.scalars(select(PaperFill).order_by(PaperFill.occurred_at))).all()
        assert len(fills) == 2
        for fill, rules in zip(fills, (old, new), strict=True):
            provenance = fill.provenance
            assert provenance["model"] == "estimated_fill"
            assert provenance["rules_version"] == rules["version"]
            assert provenance["fee_source"] == "public_contract_rules_taker"
            assert provenance["fee_rate"] == rules["takerFeeRate"]
            assert provenance["public_maker_rate"] == rules["makerFeeRate"]
            assert provenance["fee_rate_mode"] == "NORMAL"
            assert fill.fee == (fill.quantity * D("0.01") * fill.price
                                * D(rules["takerFeeRate"])).quantize(D("0.000000000001"))
            ledger = await session.scalar(select(PaperLedger).where(
                PaperLedger.event_key == f"fee:{fill.id}"))
            assert ledger.wallet_delta == -fill.fee
            assert ledger.offset_delta == fill.fee
            assert ledger.provenance["rules_version"] == rules["version"]

    # A tier-only change is a new version; an unsupported tariff/state cannot be acknowledged.
    tier = public_rules("0.0004", "0.0001", tier_cap=480000)
    assert await store.rules(tier)
    third = UUID((await svc.create(PortfolioCreate(name="tier")))["id"])
    assert (await svc.submit(third, request("tier-block")))["risk"]["outcome"] == "REJECT"
    await svc.acknowledge("BTC_USDT", tier["version"])
    assert (await svc.submit(third, request("tier-allowed")))["risk"]["outcome"] == "ALLOW"
    for invalid in (public_rules("0.0004", "0.0001", mode="TIERED"),
                    public_rules("0.0004", "0.0001", state=1)):
        assert await store.rules(invalid)
        with pytest.raises(PaperError) as rejected:
            await svc.acknowledge("BTC_USDT", invalid["version"])
        assert rejected.value.code == "RULES_INVALID"


async def test_book_limit_partial_zero_gap_and_snapshot_retry(paper):
    db, cfg, svc, pid = paper
    store = MarketStore(db, cfg.config_version)
    now = utc_now()
    version = int(now.timestamp()) * 1000000 + now.microsecond + 100
    book = {"version": version, "asks": [["100", "30"], ["100.01", "70"]],
            "bids": [["99.99", "1000"]]}
    assert await store.snapshot("depth", book, now)
    market = await svc.submit(pid, request("two-level"))
    assert market["order"]["status"] == "FILLED"
    assert market["order"]["filled_quantity"] == "10"
    async with db.sessions() as session:
        fills = (await session.scalars(select(PaperFill))).all()
        assert [(fill.quantity, fill.price) for fill in fills] == [
            (D("3"), D("100.02")), (D("7"), D("100.04"))]
        assert all(fill.provenance["depth_version"] == version for fill in fills)
    assert not await store.snapshot("depth", book, now + timedelta(milliseconds=1))
    assert await svc.submit(pid, request("two-level")) == market
    async with db.sessions() as session:
        assert len((await session.scalars(select(PaperFill))).all()) == 2

    other = UUID((await svc.create(PortfolioCreate(name="limit")))["id"])
    passive = await svc.submit(other, request("passive", kind="limit", limit_price="99.99"))
    assert passive["order"]["status"] == "PENDING"
    assert passive["order"]["payload"]["limit_model"] == "crossing_rest_book_no_queue"
    assert not await store.snapshot("depth", book, now + timedelta(milliseconds=2))
    await svc.tick()
    async with db.sessions() as session:
        assert len((await session.scalars(select(PaperFill).join(PaperPosition).where(
            PaperPosition.portfolio_id == other))).all()) == 0
    await svc.cancel(other, UUID(passive["order"]["id"]))
    crossing = await svc.submit(other, request("crossing", kind="limit", limit_price="100.02"))
    assert crossing["order"]["filled_quantity"] == "3"
    assert crossing["order"]["status"] == "PARTIALLY_FILLED"
    assert D(crossing["order"]["average_price"]) <= D("100.02")
    async with db.sessions() as session:
        fill = await session.scalar(select(PaperFill).join(PaperPosition).where(
            PaperPosition.portfolio_id == other))
        assert fill.provenance["fee_source"] == "public_contract_rules_taker"
        assert fill.fee >= 0

    zero = UUID((await svc.create(PortfolioCreate(name="zero")))["id"])
    assert await store.snapshot("depth", {**book, "version": version + 1,
        "asks": [["100", "1"]]}, utc_now())
    assert (await svc.submit(zero, request("no-qty")))["order"]["status"] == "NO_LIQUIDITY"
    assert not await store.snapshot("depth", {**book, "version": version + 1,
        "asks": [["100.02", "100"]]}, utc_now())
    assert (await svc.submit(zero, request("gap")))["risk"]["reason"] == "DEPTH_STALE"
    assert await store.snapshot("depth", {**book, "version": version + 2}, utc_now())
    async with db.sessions.begin() as session:
        depth = await session.get(MarketSnapshot, ("BTC_USDT", "depth"))
        depth.source_time = utc_now() - timedelta(seconds=9)
    assert (await svc.submit(zero, request("stale-book")))["risk"]["reason"] == "DEPTH_STALE"
    for revision, asks in ((3, [["100.001", "100"]]), (4, [["100", "1.5"]])):
        assert await store.snapshot("depth", {**book, "version": version + revision,
            "asks": asks}, utc_now())
        assert (await svc.submit(zero, request(f"invalid-book-{revision}")))["risk"][
            "reason"] == "DEPTH_INVALID"


@pytest.mark.parametrize("direction,rate", [("LONG", "0.01"), ("SHORT", "0.01"), ("LONG", "-0.01")])
async def test_confirmed_funding_held_quantity_idempotence(paper, direction, rate):
    db, cfg, svc, pid = paper
    req = OrderRequest(
        idempotency_key="fund",
        direction=direction,
        quantity="10",
        stop_loss="99" if direction == "LONG" else "101",
    )
    await svc.submit(pid, req)
    position = (await account(svc, pid))["positions"][0]
    when = utc_now().replace(second=0, microsecond=0)
    async with db.sessions.begin() as s:
        pos = await s.get(PaperPosition, UUID(position["id"]))
        pos.opened_at = when - timedelta(minutes=2)
        fill = await s.scalar(select(PaperFill).where(PaperFill.position_id == pos.id))
        fill.occurred_at = when - timedelta(minutes=1)
        s.add(
            Candle(
                symbol="BTC_USDT",
                timeframe="1m",
                window_start=when - timedelta(minutes=1),
                source_time=when,
                received_at=when,
                payload={"close": "100"},
                payload_hash="x",
                final=True,
                source="rest",
                config_version=cfg.config_version,
            )
        )
    await add_settlement(db, cfg, when, rate)
    before = D((await account(svc, pid))["wallet_balance"])
    await svc.tick()
    await svc.tick()
    sign = -1 if direction == "LONG" else 1
    assert (
        D((await account(svc, pid))["wallet_balance"])
        == before + D("10") * D("0.01") * D("100") * D(rate) * sign
    )
    async with db.sessions() as s:
        entries = (
            await s.scalars(select(PaperLedger).where(PaperLedger.reason == "FUNDING"))
        ).all()
        assert len(entries) == 1 and entries[0].provenance["held_contracts"] == "10"
    app = FastAPI()
    app.state.database = db
    app.state.settings = cfg
    register_paper_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/portfolios/{pid}/ledger")
    assert response.status_code == 200
    funding_rows = [row for row in response.json() if row["reason"] == "FUNDING"]
    assert len(funding_rows) == 1
    assert funding_rows[0]["provenance"]["rate_source"] == "confirmed_public_funding_history"
    assert funding_rows[0]["provenance"]["price_source"] == "estimated_prior_final_1m_close"


async def test_broker_requires_allow_verdict(paper):
    from ai_futures.paper_models import PaperOrder

    db, _, svc, pid = paper
    rejected = await svc.submit(pid, request().model_copy(update={"leverage": D("4")}))
    async with db.sessions.begin() as s:
        portfolio = await s.get(PaperPortfolio, pid)
        order = PaperOrder(
            intent_id=UUID(rejected["intent"]["id"]),
            verdict_id=UUID(rejected["risk"]["id"]),
            portfolio_id=pid,
            epoch=1,
            symbol="BTC_USDT",
            direction="LONG",
            kind="market",
            status="PENDING",
            quantity=D("10"),
            filled_quantity=D(0),
            reserve=D(0),
            payload={"action": "open"},
            config_version="test",
        )
        with pytest.raises(PaperError) as exc:
            await svc.execute(s, portfolio, order)
        assert exc.value.code == "BROKER_RISK_VERDICT_REQUIRED"


async def test_depth_stale_pending_kill_and_funding_unknown(paper):
    db, cfg, svc, pid = paper
    async with db.sessions.begin() as s:
        depth = await s.get(MarketSnapshot, ("BTC_USDT", "depth"))
        depth.source_time = utc_now() - timedelta(seconds=9)
    assert (await svc.submit(pid, request("depth")))["risk"]["reason"] == "DEPTH_STALE"
    await feed(db, cfg)
    limit = await svc.submit(pid, request("pending", kind="limit", limit_price="99.5"))
    await svc.kill(True)
    assert not (await account(svc, pid))["pending_orders"]
    assert D((await account(svc, pid))["pending_order_reserve"]) == 0
    assert limit["order"]["status"] == "PENDING"
    assert await svc.submit(pid, request("pending", kind="limit", limit_price="99.5")) == limit


async def test_liquidation_estimate_separate_fee_and_priority(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(pid, request(leverage="3"))
    assert opened["risk"]["outcome"] == "ALLOW"
    before = D((await account(svc, pid))["wallet_balance"])
    await feed(db, cfg, price="60")
    await svc.kill(True)
    await svc.tick()
    summary = await account(svc, pid)
    assert not summary["positions"]
    async with db.sessions() as s:
        fills = (await s.scalars(select(PaperFill).order_by(PaperFill.occurred_at))).all()
        close = fills[-1]
        assert close.provenance["close_reason"] == "liquidation_estimate"
        assert close.provenance["liquidation_model"]["model"] == "model_estimate"
        assert close.provenance["liquidation_model"]["tier_level"] == 1
        assert close.price == D("59.98")
        assert close.realized_pnl == D("-4.004")
        assert D(close.provenance["trading_fee"]) == D("0.0023992")
        assert D(close.provenance["liquidation_fee"]) == D("0.005998")
        assert close.fee == D("0.0083972")
        ledger = (
            await s.scalars(select(PaperLedger).where(PaperLedger.reason == "LIQUIDATION_FEE"))
        ).all()
        assert len(ledger) == 1 and ledger[0].wallet_delta == D("-0.005998")
        assert D(summary["wallet_balance"]) == before - D("4.004") - close.fee


async def test_tier_change_requires_ack_and_restart_keeps_model_and_reduce_gate(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(pid, request("tier-open", leverage="2"))
    assert opened["risk"]["outcome"] == "ALLOW"
    before = (await account(svc, pid))["positions"][0]
    assert before["liquidation_provenance"]["model"] == "model_estimate"
    assert before["liquidation_provenance"]["tier_level"] == 1
    new = public_rules("0.0004", "0.0001", tier_cap=310000)
    store = MarketStore(db, cfg.config_version)
    assert await store.rules(new)
    assert (await account(svc, pid))["positions"][0]["liquidation_estimate"] is None
    other = UUID((await svc.create(PortfolioCreate(name="tier-change")))["id"])
    assert (await svc.submit(other, request("blocked")))["risk"]["outcome"] == "REJECT"
    await svc.acknowledge("BTC_USDT", new["version"])
    restarted = PaperService(db, cfg.config_version)
    after = (await account(restarted, pid))["positions"][0]
    assert after["liquidation_estimate"] is not None
    assert after["liquidation_provenance"]["rules_version"] == new["version"]
    await restarted.kill(True)
    closed = await restarted.submit(
        pid, CloseRequest(idempotency_key="tier-close"), UUID(after["id"])
    )
    assert closed["risk"]["outcome"] == "ALLOW"
    assert await restarted.submit(
        pid, CloseRequest(idempotency_key="tier-close"), UUID(after["id"])
    ) == closed
    assert not (await account(restarted, pid))["positions"]


async def test_unknown_tier_cannot_be_acknowledged_or_opened(paper):
    db, cfg, svc, pid = paper
    assert (await svc.submit(pid, request("known-tier")))["risk"]["outcome"] == "ALLOW"
    store = MarketStore(db, cfg.config_version)
    invalid = public_rules("0.0004", "0.0001")
    invalid["raw"]["riskLimitCustom"][0]["imr"] = None
    invalid = normalize_rules(invalid["raw"], "BTC_USDT")
    assert await store.rules(invalid)
    assert (await account(svc, pid))["positions"][0]["liquidation_estimate"] is None
    with pytest.raises(PaperError, match="RULES_INVALID"):
        await svc.acknowledge("BTC_USDT", invalid["version"])
    other = UUID((await svc.create(PortfolioCreate(name="unknown-tier")))["id"])
    assert (await svc.submit(other, request("unknown-tier")))["risk"]["outcome"] == "REJECT"


async def test_posted_funding_changes_isolated_estimate_and_wallet_once(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(pid, request("fund-model", leverage="2"))
    assert opened["risk"]["outcome"] == "ALLOW"
    initial = await account(svc, pid)
    before = initial["positions"][0]
    async with db.sessions.begin() as session:
        portfolio = await svc.lock(session, pid)
        position = await session.get(PaperPosition, UUID(before["id"]))
        svc.post(session, portfolio, position.id, "fund-model:one", "FUNDING", D("0.1"))
        position.funding += D("0.1")
    after = (await account(PaperService(db, cfg.config_version), pid))
    pos = after["positions"][0]
    assert D(after["wallet_balance"]) - D(initial["wallet_balance"]) == D("0.1")
    assert D(after["equity"]) - D(initial["equity"]) == D("0.1")
    assert D(pos["liquidation_estimate"]) < D(before["liquidation_estimate"])
    async with db.sessions() as session:
        row = await session.scalar(
            select(PaperLedger).where(PaperLedger.event_key == "fund-model:one")
        )
        assert row.wallet_delta + row.offset_delta == 0


async def test_liquidation_model_precedes_take_profit_with_isolated_funding_loss(paper):
    db, cfg, svc, pid = paper
    opened = await svc.submit(
        pid, OrderRequest(idempotency_key="tp-priority", direction="LONG", quantity="10",
                          leverage="3", stop_loss="99", take_profit="101")
    )
    assert opened["risk"]["outcome"] == "ALLOW"
    position_id = UUID((await account(svc, pid))["positions"][0]["id"])
    async with db.sessions.begin() as session:
        portfolio = await svc.lock(session, pid)
        position = await session.get(PaperPosition, position_id)
        svc.post(session, portfolio, position_id, "tp-priority:funding", "FUNDING", D("-5"))
        position.funding -= D("5")
    await feed(db, cfg, price="102")
    await svc.kill(True)
    await PaperService(db, cfg.config_version).tick()
    assert not (await account(svc, pid))["positions"]
    async with db.sessions() as session:
        fills = (await session.scalars(select(PaperFill).order_by(PaperFill.occurred_at))).all()
        assert fills[-1].provenance["close_reason"] == "liquidation_estimate"


async def test_confirmed_funding_without_historical_price_blocks_entry(paper):
    db, cfg, svc, pid = paper
    await svc.submit(pid, request())
    position = (await account(svc, pid))["positions"][0]
    when = utc_now().replace(second=0, microsecond=0)
    async with db.sessions.begin() as s:
        pos = await s.get(PaperPosition, UUID(position["id"]))
        pos.opened_at = when - timedelta(minutes=2)
        fill = await s.scalar(select(PaperFill).where(PaperFill.position_id == pos.id))
        fill.occurred_at = when - timedelta(minutes=1)
    await add_settlement(db, cfg, when, "0.01")
    await svc.tick()
    summary = await account(svc, pid)
    assert summary["pending_funding_status"] == "unresolved"
    assert summary["equity"] is None
    rejected = await svc.submit(pid, request("unknown_funding"))
    assert rejected["risk"]["reason"] == "FUNDING_UNRESOLVED"
    async with db.sessions() as s:
        assert not (
            await s.scalars(select(PaperLedger).where(PaperLedger.reason == "FUNDING"))
        ).all()


async def test_partial_limit_close_cancels_future_additions(paper):
    db, cfg, svc, pid = paper
    await feed(db, cfg, price="100", volume="30")
    limit = await svc.submit(pid, request("partial_limit", kind="limit", limit_price="100.1"))
    assert limit["order"]["status"] == "PARTIALLY_FILLED"
    assert limit["order"]["filled_quantity"] == "3"
    summary = await account(svc, pid)
    assert D(summary["pending_order_reserve"]) > 0
    await feed(db, cfg, volume="10000")
    await svc.submit(
        pid, CloseRequest(idempotency_key="close_partial"), UUID(summary["positions"][0]["id"])
    )
    await svc.tick()
    summary = await account(svc, pid)
    assert not summary["positions"] and not summary["pending_orders"]
    assert D(summary["pending_order_reserve"]) == 0


async def test_disconnected_quality_blocks_close_and_protective_tick(paper):
    db, cfg, svc, pid = paper
    await svc.submit(pid, request())
    position = (await account(svc, pid))["positions"][0]
    await feed(db, cfg, price="98")
    await MarketStore(db, cfg.config_version).quality("stale", "disconnected")
    summary = await account(svc, pid)
    assert summary["equity"] is None and summary["valuation_status"] == "DATA_STALE"
    assert summary["total_pnl_net"] is None
    assert D(summary["realized_pnl_net"]) < 0
    await svc.tick()
    assert len((await account(svc, pid))["positions"]) == 1
    rejected = await svc.submit(
        pid, CloseRequest(idempotency_key="unconfirmed"), UUID(position["id"])
    )
    assert rejected["risk"]["reason"] == "DATA_STALE" and rejected["order"] is None
    await MarketStore(db, cfg.config_version).quality("ok", "reconciled")
    await svc.kill(True)
    allowed = await svc.submit(pid, CloseRequest(idempotency_key="confirmed"), UUID(position["id"]))
    assert allowed["risk"]["outcome"] == "ALLOW"
    assert not (await account(svc, pid))["positions"]


async def test_broker_rejects_open_verdict_reused_for_close(paper):
    from ai_futures.paper_models import PaperOrder, PaperVerdict

    db, _, svc, pid = paper
    opened = await svc.submit(pid, request())
    position_id = UUID((await account(svc, pid))["positions"][0]["id"])
    async with db.sessions.begin() as session:
        portfolio = await session.get(PaperPortfolio, pid)
        position = await session.get(PaperPosition, position_id)
        verdict = await session.get(PaperVerdict, UUID(opened["risk"]["id"]))
        order = PaperOrder(
            intent_id=verdict.intent_id,
            verdict_id=verdict.id,
            portfolio_id=pid,
            epoch=portfolio.epoch,
            symbol="BTC_USDT",
            direction="LONG",
            kind="market",
            status="PENDING",
            quantity=D("1"),
            filled_quantity=D(0),
            reserve=D(0),
            payload={"action": "close", "position_id": str(position_id)},
            config_version="test",
        )
        with pytest.raises(PaperError) as exc:
            await svc.execute(session, portfolio, order, position)
        assert exc.value.code == "BROKER_RISK_VERDICT_REQUIRED"


class FundingPages:
    def __init__(self, rows, fault=None):
        self.rows, self.fault, self.calls = rows, fault, 0

    async def funding_history_page(self, symbol, page_num, page_size):
        assert symbol == "BTC_USDT"
        self.calls += 1
        rows = self.rows
        if self.fault == "shift" and self.calls >= 2:
            rows = [
                {
                    "settleTime": (utc_now() + timedelta(hours=1)).isoformat(),
                    "fundingRate": "0.01",
                    "collectCycle": 1,
                },
                *rows,
            ]
        if self.fault == "same_count_shift" and self.calls >= 2:
            rows = [rows[1], rows[0], *rows[2:]]
        start = (page_num - 1) * page_size
        page = rows[start : start + page_size]
        if self.fault == "duplicate" and page_num == 1:
            page = [page[0], page[0], *page[2:]]
        return {"rows": page, "total_count": len(rows), "page": page_num}


def funding_rows(count=27, *, gap=False, cycle_change=False):
    latest = utc_now().replace(minute=0, second=0, microsecond=0)
    rows = []
    when = latest
    for index in range(count):
        cycle = 2 if cycle_change and index >= 14 else 1
        rows.append(
            {
                "settleTime": when.isoformat(),
                "fundingRate": "0.01" if index % 2 else "-0.01",
                "collectCycle": cycle,
            }
        )
        when -= timedelta(hours=cycle)
    if gap:
        rows.pop(12)
    return rows


async def backdate_held_position(paper, rows):
    db, cfg, svc, pid = paper
    await svc.submit(pid, request("held"))
    position_id = UUID((await account(svc, pid))["positions"][0]["id"])
    oldest = rows[-1]
    held_since = datetime.fromisoformat(oldest["settleTime"]) + timedelta(minutes=30)
    async with db.sessions.begin() as session:
        pos = await session.get(PaperPosition, position_id)
        pos.opened_at = held_since
        fill = await session.scalar(select(PaperFill).where(PaperFill.position_id == pos.id))
        fill.occurred_at = held_since
        for row in rows[:-1]:
            when = datetime.fromisoformat(row["settleTime"])
            session.add(
                Candle(
                    symbol="BTC_USDT",
                    timeframe="1m",
                    window_start=when - timedelta(minutes=1),
                    source_time=when,
                    received_at=when,
                    payload={"close": "100"},
                    payload_hash="fixture",
                    final=True,
                    source="rest",
                    config_version=cfg.config_version,
                )
            )
    return held_since


async def test_funding_coverage_over_20_restart_exactly_once(paper, monkeypatch):
    monkeypatch.setattr("ai_futures.funding.PAGE_SIZE", 10)
    db, cfg, svc, pid = paper
    rows = funding_rows(cycle_change=True)
    held_since = await backdate_held_position(paper, rows)
    current = {
        "nextSettleTime": (
            datetime.fromisoformat(rows[0]["settleTime"]) + timedelta(hours=1)
        ).isoformat(),
        "collectCycle": 1,
    }
    before = await account(svc, pid)
    assert before["equity"] is None and before["funding_unresolved_reason"] == "COVERAGE_UNPROVEN"
    rejected = await svc.submit(pid, request("blocked_before_coverage"))
    assert rejected["risk"]["reason"] == "FUNDING_UNRESOLVED"
    adapter = FundingPages(rows)
    assert await reconcile_funding(db, adapter, "BTC_USDT", cfg.config_version, current)
    assert adapter.calls >= 4
    other = await svc.create(PortfolioCreate(name="isolated"))
    restarted = PaperService(db, cfg.config_version)
    await restarted.tick()
    await restarted.tick()
    summary = await account(restarted, pid)
    assert summary["equity"] is not None and summary["pending_funding_status"] == "reconciled"
    async with db.sessions() as session:
        settlements = (await session.scalars(select(FundingSettlement))).all()
        entries = (
            await session.scalars(select(PaperLedger).where(PaperLedger.reason == "FUNDING"))
        ).all()
    assert len(settlements) == 27 and len(entries) == 26
    assert all(entry.wallet_delta + entry.offset_delta == 0 for entry in entries)
    assert {entry.provenance["rate"] for entry in entries} == {"0.01", "-0.01"}
    assert all(
        entry.provenance["price_source"] == "estimated_prior_final_1m_close" for entry in entries
    )
    assert D((await account(restarted, UUID(other["id"])))["wallet_balance"]) == D("1000")
    assert held_since < datetime.fromisoformat(rows[0]["settleTime"])


@pytest.mark.parametrize("fault", ["shift", "same_count_shift", "duplicate", "gap"])
async def test_funding_coverage_faults_fail_closed(paper, monkeypatch, fault):
    monkeypatch.setattr("ai_futures.funding.PAGE_SIZE", 10)
    db, cfg, svc, pid = paper
    rows = funding_rows(gap=fault == "gap")
    await backdate_held_position(paper, rows)
    current = {
        "nextSettleTime": (
            datetime.fromisoformat(rows[0]["settleTime"]) + timedelta(hours=1)
        ).isoformat(),
        "collectCycle": 1,
    }
    assert not await reconcile_funding(
        db, FundingPages(rows, None if fault == "gap" else fault),
        "BTC_USDT", cfg.config_version, current,
    )
    summary = await account(svc, pid)
    assert summary["equity"] is None
    assert summary["valuation_status"] == "FUNDING_UNRESOLVED"
    assert summary["total_pnl_net"] is None
    with pytest.raises(PaperError) as error:
        await svc.reset(pid, ResetRequest())
    assert error.value.code in {"RESET_OPEN_POSITIONS", "FUNDING_UNRESOLVED"}


async def test_funding_closed_position_missing_price_recovery(paper, monkeypatch):
    monkeypatch.setattr("ai_futures.funding.PAGE_SIZE", 10)
    db, cfg, svc, pid = paper
    rows = funding_rows()
    await backdate_held_position(paper, rows)
    missing = datetime.fromisoformat(rows[5]["settleTime"])
    async with db.sessions.begin() as session:
        candle = await session.get(Candle, ("BTC_USDT", "1m", missing - timedelta(minutes=1)))
        await session.delete(candle)
        position = await session.scalar(
            select(PaperPosition).where(PaperPosition.portfolio_id == pid)
        )
        position.closed_at = utc_now()
    current = {
        "nextSettleTime": (
            datetime.fromisoformat(rows[0]["settleTime"]) + timedelta(hours=1)
        ).isoformat(),
        "collectCycle": 1,
    }
    assert await reconcile_funding(db, FundingPages(rows), "BTC_USDT", cfg.config_version, current)
    await svc.tick()
    unresolved = await account(svc, pid)
    assert unresolved["funding_gap_time"] == missing.isoformat()
    assert unresolved["equity"] is None
    with pytest.raises(PaperError, match="FUNDING_UNRESOLVED"):
        await svc.reset(pid, ResetRequest())
    async with db.sessions.begin() as session:
        session.add(
            Candle(
                symbol="BTC_USDT", timeframe="1m",
                window_start=missing - timedelta(minutes=1), source_time=missing,
                received_at=missing, payload={"close": "100"}, payload_hash="recovered",
                final=True, source="rest", config_version=cfg.config_version,
            )
        )
    await PaperService(db, cfg.config_version).tick()
    resolved = await account(svc, pid)
    assert resolved["equity"] is not None
    async with db.sessions() as session:
        entries = (
            await session.scalars(select(PaperLedger).where(PaperLedger.reason == "FUNDING"))
        ).all()
    assert len(entries) == 26


async def test_funding_page_shift_retry_restores_coverage_without_reposting(paper, monkeypatch):
    monkeypatch.setattr("ai_futures.funding.PAGE_SIZE", 10)
    db, cfg, svc, pid = paper
    rows = funding_rows()
    await backdate_held_position(paper, rows)
    current = {
        "nextSettleTime": (
            datetime.fromisoformat(rows[0]["settleTime"]) + timedelta(hours=1)
        ).isoformat(),
        "collectCycle": 1,
    }
    assert not await reconcile_funding(
        db, FundingPages(rows, "shift"), "BTC_USDT", cfg.config_version, current
    )
    assert (await account(svc, pid))["equity"] is None
    assert await reconcile_funding(db, FundingPages(rows), "BTC_USDT", cfg.config_version, current)
    await PaperService(db, cfg.config_version).tick()
    assert (await account(svc, pid))["equity"] is not None
    assert await reconcile_funding(db, FundingPages(rows), "BTC_USDT", cfg.config_version, current)
    await PaperService(db, cfg.config_version).tick()
    async with db.sessions() as session:
        entries = (
            await session.scalars(select(PaperLedger).where(PaperLedger.reason == "FUNDING"))
        ).all()
    assert len(entries) == 26
