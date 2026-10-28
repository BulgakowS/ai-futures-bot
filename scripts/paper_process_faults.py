"""Isolated PostgreSQL process/fault gate for Paper and Bots. No exchange client is started.

Run: .venv/bin/python scripts/paper_process_faults.py
The temporary PostgreSQL cluster is always stopped and removed on exit.
"""

import asyncio
import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from ai_futures.alerts import monitor_once
from ai_futures.backtest_models import BacktestRun
from ai_futures.bot_models import BotDecision
from ai_futures.bots import BotCreate, BotService
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.main import create_app
from ai_futures.market import MarketStore
from ai_futures.mexc import payload_hash
from ai_futures.models import AlertState, AppState, FundingCoverage, OutboxEvent, WorkerHeartbeat
from ai_futures.paper import OrderRequest, PaperService, PortfolioCreate, RiskProfile
from ai_futures.paper_models import (
    PaperFill,
    PaperIntent,
    PaperLedger,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperVerdict,
)
from ai_futures.schemas import utc_now
from ai_futures.state import seed_state
from ai_futures.strategies import StrategyConfig
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
RULES = {
    "version": "f5-synthetic-v1",
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
    "riskLimitCustom": [
        {"level": 1, "maxVol": "1000000", "mmr": "0.005", "imr": "0.01", "maxLeverage": "100"}
    ],
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def deny_external_network():
    """Keep all test processes on loopback even if a future code path adds an adapter."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local(address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("F5 harness blocked an external network connection")

    def connect(sock, address):
        local(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        local(address)
        return original_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


def freeze_test_clock():
    """Use one completed-candle clock across processes and minute boundaries."""
    fixed = datetime.fromisoformat(os.environ["F5_CLOCK_UTC"])

    def clock():
        return fixed

    for name, module in tuple(sys.modules.items()):
        if name.startswith("ai_futures") and hasattr(module, "utc_now"):
            module.utc_now = clock
    globals()["utc_now"] = clock


def settings():
    return Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=os.environ["DATABASE_URL"],
    )


class RedisStub:
    async def ping(self):
        return True

    async def aclose(self):
        pass


async def child(role, target=None):
    deny_external_network()
    freeze_test_clock()
    cfg = settings()
    if role == "api":
        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(
                create_app(cfg, redis=RedisStub()),
                host="127.0.0.1",
                port=int(os.environ["F5_API_PORT"]),
                log_level="error",
            )
        )
        await server.serve()
        return
    db = Database(cfg)
    try:
        bots = BotService(db, cfg.config_version)
        if role == "worker":
            if target:
                decision_id = await bots._claim(UUID(target))
                if decision_id:
                    await bots._deliver(UUID(target), decision_id)
            else:
                await bots.tick()
        elif role == "crash":
            decision_id = await bots._claim(UUID(target))
            assert decision_id is not None
            original = bots.paper.submit

            async def crash_after_paper_commit(*args, **kwargs):
                await original(*args, **kwargs)
                os._exit(73)  # Hard process exit before BotDecision finalization.

            bots.paper.submit = crash_after_paper_commit
            await bots._deliver(UUID(target), decision_id)
            raise AssertionError("Fault injection did not fire")
        elif role == "alert":
            await monitor_once(db, cfg)
        else:
            raise ValueError(role)
    finally:
        await db.close()


async def feed(db, cfg, price="100", quality="ok"):
    store = MarketStore(db, cfg.config_version)
    await store.rules(RULES)
    now = utc_now()
    await store.snapshot("ticker", {"ask1": price, "bid1": price, "fairPrice": price}, now, "ws")
    await store.snapshot(
        "depth",
        {
            "version": time.monotonic_ns(),
            "asks": [[price, "10000"]],
            "bids": [[price, "10000"]],
        },
        now,
    )
    await store.quality(quality, "f5_controlled_stream")
    async with db.sessions.begin() as session:
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
                source="synthetic_f5_fixture",
                snapshot_hash=payload_hash({}),
                observed_at=now,
                config_version=cfg.config_version,
            )
        )


def candle(price):
    return {
        "open": str(price),
        "close": str(price),
        "high": str(Decimal(price) + 1),
        "low": str(Decimal(price) - 1),
        "vol": "100",
        "amount": str(Decimal(price) * 100),
    }


def strategy(quantity):
    return StrategyConfig(
        strategy="trend_following",
        timeframe="1m",
        quantity=quantity,
        leverage="1",
        stop_distance="0.01",
        fast_period=2,
        slow_period=3,
        breakout_period=2,
        breakout_buffer="0",
        min_volume_ratio="1",
    )


def process(role, env, target=None):
    command = [str(PYTHON), str(Path(__file__).resolve()), role]
    if target:
        command.append(str(target))
    return subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )


def completed(proc, expected=0):
    _, errors = proc.communicate(timeout=20)
    assert proc.returncode == expected, (proc.returncode, errors[-1500:])


async def api_ready(port):
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(80):
            try:
                if (
                    await client.get(f"http://127.0.0.1:{port}/api/v1/health/live")
                ).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError("API process did not start")


async def run_gate(env, ports):
    deny_external_network()
    freeze_test_clock()
    cfg = settings()
    db = Database(cfg)
    servers = []
    evidence = {
        "mode": "paper",
        "database": "temporary_local_postgresql_cluster",
        "processes": {"api": 2, "worker": 0},
        "private_mexc_calls": 0,
        "external_network_policy": "loopback_only_socket_guard",
        "deterministic_candle_clock": True,
        "market_source": "controlled_synthetic_final_stream",
    }
    try:
        await seed_state(db, cfg.config_version)
        paper = PaperService(db, cfg.config_version)
        await paper.kill(False)
        await feed(db, cfg)
        bots = BotService(db, cfg.config_version)
        start = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
        store = MarketStore(db, cfg.config_version)
        await store.candles(
            "1m",
            [
                (start + timedelta(minutes=i), candle(price), True)
                for i, price in enumerate((98, 98, 97))
            ],
            utc_now(),
        )
        created = []
        for qty in ("10", "11", "12"):
            row = await bots.create(
                BotCreate(
                    name=f"F5 bot {qty}",
                    deposit="1000",
                    risk_profile=RiskProfile(),
                    config=strategy(qty),
                )
            )
            bot_id = UUID(row["id"])
            created.append((bot_id, UUID(row["portfolio_id"])))
            await bots.transition(bot_id, "ready")
            await bots.transition(bot_id, "start")
        assert len({p for _, p in created}) == 3

        for port in ports:
            server_env = env | {"F5_API_PORT": str(port)}
            servers.append(process("api", server_env))
        await asyncio.gather(*(api_ready(port) for port in ports))

        final_open = start + timedelta(minutes=3)
        await store.candles("1m", [(final_open, candle(100), True)], utc_now())
        crashed = process("crash", env, created[0][0])
        completed(crashed, 73)
        async with db.sessions() as session:
            pending = await session.scalar(
                select(BotDecision).where(BotDecision.bot_id == created[0][0])
            )
            assert pending.status == "PROCESSING"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperIntent)
                    .where(PaperIntent.portfolio_id == created[0][1])
                )
                == 1
            )
        await feed(db, cfg)
        workers = [process("worker", env), process("worker", env)]
        for worker in workers:
            completed(worker)
        replay_worker = process("worker", env)
        completed(replay_worker)
        evidence["processes"]["worker"] += 4

        async with db.sessions() as session:
            decisions = (await session.scalars(select(BotDecision))).all()
            assert len(decisions) == 3
            assert all(d.action == "OPEN" and d.status == "DONE" for d in decisions)
            for bot_id, portfolio_id in created:
                own = [d for d in decisions if d.bot_id == bot_id]
                assert len(own) == 1 and own[0].intent_id and own[0].risk_id and own[0].order_id
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaperIntent)
                        .where(PaperIntent.portfolio_id == portfolio_id)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaperVerdict)
                        .join(PaperIntent, PaperVerdict.intent_id == PaperIntent.id)
                        .where(PaperIntent.portfolio_id == portfolio_id)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaperOrder)
                        .where(PaperOrder.portfolio_id == portfolio_id)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaperFill)
                        .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
                        .where(PaperOrder.portfolio_id == portfolio_id)
                    )
                    == 1
                )
        evidence["bot_restart"] = {
            "bots": 3,
            "recovered_processing": 1,
            "decisions": 3,
            "intents": 3,
            "fills": 3,
        }

        manual = await paper.create(PortfolioCreate(name="F5 duplicate"))
        pid = UUID(manual["id"])
        payload = OrderRequest(
            idempotency_key="f5-duplicate", direction="LONG", quantity="10", stop_loss="99"
        ).model_dump(mode="json")
        headers = {"Authorization": "Bearer " + "x" * 48}
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            replies = await asyncio.gather(
                *(
                    client.post(
                        f"http://127.0.0.1:{ports[i % 2]}/api/v1/portfolios/{pid}/orders",
                        json=payload,
                    )
                    for i in range(8)
                )
            )
        assert all(r.status_code == 200 for r in replies), [
            (r.status_code, r.text) for r in replies
        ]
        assert all(r.json() == replies[0].json() for r in replies)
        assert replies[0].json()["risk"]["outcome"] == "ALLOW"
        async with db.sessions() as session:
            counts = [
                await session.scalar(
                    select(func.count()).select_from(model).where(model.portfolio_id == pid)
                )
                for model in (PaperIntent, PaperOrder, PaperLedger)
            ]
            fills = await session.scalar(
                select(func.count())
                .select_from(PaperFill)
                .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
                .where(PaperOrder.portfolio_id == pid)
            )
            counts.insert(2, fills)
            assert counts == [1, 1, 1, 2], counts
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperVerdict)
                    .join(PaperIntent, PaperVerdict.intent_id == PaperIntent.id)
                    .where(PaperIntent.portfolio_id == pid)
                )
                == 1
            )
        evidence["duplicate_api_submit"] = {
            "independent_api_processes": 2,
            "requests": 8,
            "intent_order_fill_ledger": counts,
        }

        # Two independent API servers race an exclusive kill lock against a reduce-only close.
        async with db.sessions() as session:
            own_position = await session.scalar(
                select(PaperPosition).where(
                    PaperPosition.portfolio_id == pid, PaperPosition.closed_at.is_(None)
                )
            )
            assert own_position is not None
            position_id = own_position.id
        await feed(db, cfg)
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            kill_reply, close_reply = await asyncio.gather(
                client.post(
                    f"http://127.0.0.1:{ports[0]}/api/v1/risk/kill-switch",
                    json={"kill_switch": True},
                ),
                client.post(
                    f"http://127.0.0.1:{ports[1]}/api/v1/portfolios/{pid}"
                    f"/positions/{position_id}/close",
                    json={"idempotency_key": "f5-kill-close-race"},
                ),
            )
        assert kill_reply.status_code == 200 and close_reply.status_code == 200
        assert close_reply.json()["risk"]["outcome"] == "ALLOW"
        assert close_reply.json()["risk"]["snapshot"]["reduce_only"] is True
        evidence["kill_close_race"] = "ALLOW_REDUCE_ONLY"
        await paper.kill(False)

        pending_portfolio = await paper.create(PortfolioCreate(name="F5 pending"))
        pending_id = UUID(pending_portfolio["id"])
        pending = await paper.submit(
            pending_id,
            OrderRequest(
                idempotency_key="f5-pending",
                direction="LONG",
                kind="limit",
                quantity="10",
                limit_price="90",
                stop_loss="89",
            ),
        )
        assert pending["order"]["status"] == "PENDING", pending
        race_portfolio = await paper.create(PortfolioCreate(name="F5 kill/open race"))
        race_id = UUID(race_portfolio["id"])
        await feed(db, cfg)
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            kill_reply, open_reply = await asyncio.gather(
                client.post(
                    f"http://127.0.0.1:{ports[0]}/api/v1/risk/kill-switch",
                    json={"kill_switch": True},
                ),
                client.post(
                    f"http://127.0.0.1:{ports[1]}/api/v1/portfolios/{race_id}/orders",
                    json={**payload, "idempotency_key": "f5-kill-open-race"},
                ),
            )
        assert kill_reply.status_code == 200 and open_reply.status_code == 200
        assert open_reply.json()["risk"]["reason"] in {"RISK_OK", "KILL_SWITCH"}
        if open_reply.json()["risk"]["reason"] == "KILL_SWITCH":
            assert open_reply.json()["order"] is None
        evidence["kill_open_race"] = open_reply.json()["risk"]["reason"]
        async with db.sessions() as session:
            pending_order = await session.get(PaperOrder, UUID(pending["order"]["id"]))
            assert pending_order.status == "KILL_CANCELED"
        blocked = await paper.submit(
            pending_id,
            OrderRequest(
                idempotency_key="f5-killed-open", direction="LONG", quantity="10", stop_loss="99"
            ),
        )
        assert blocked["risk"]["reason"] == "KILL_SWITCH" and blocked["order"] is None

        final_close = final_open + timedelta(minutes=1)
        assert final_close + timedelta(minutes=1) <= utc_now()
        await store.candles("1m", [(final_close, candle(95), True)], utc_now())
        await feed(db, cfg, "95")
        close_worker = process("worker", env, created[0][0])
        completed(close_worker)
        evidence["processes"]["worker"] += 1
        await feed(db, cfg, "95", "stale")
        stale_worker = process("worker", env, created[1][0])
        completed(stale_worker)
        evidence["processes"]["worker"] += 1
        await feed(db, cfg, "95")
        final_retry = final_close + timedelta(minutes=1)
        # Only insert the next final candle after its minute has finished.
        if final_retry + timedelta(minutes=1) <= utc_now():
            await store.candles("1m", [(final_retry, candle(94), True)], utc_now())
            retry_worker = process("worker", env, created[1][0])
            completed(retry_worker)
            evidence["processes"]["worker"] += 1
        async with db.sessions() as session:
            close_decision = await session.scalar(
                select(BotDecision).where(
                    BotDecision.bot_id == created[0][0], BotDecision.action == "CLOSE"
                )
            )
            stale_decision = await session.scalar(
                select(BotDecision).where(
                    BotDecision.bot_id == created[1][0], BotDecision.window_start == final_close
                )
            )
            assert close_decision and close_decision.risk_reason == "RISK_OK"
            assert stale_decision and stale_decision.action == "CLOSE"
            assert stale_decision.risk_reason == "DATA_STALE"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperPosition)
                    .where(
                        PaperPosition.portfolio_id == created[0][1],
                        PaperPosition.closed_at.is_(None),
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperPosition)
                    .where(
                        PaperPosition.portfolio_id == created[1][1],
                        PaperPosition.closed_at.is_(None),
                    )
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperFill)
                    .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
                    .where(PaperOrder.portfolio_id == created[0][1])
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperFill)
                    .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
                    .where(PaperOrder.portfolio_id == created[1][1])
                )
                == 1
            )
            verdict = await session.get(PaperVerdict, close_decision.risk_id)
            assert verdict.snapshot["reduce_only"] is True
            ledgers = (await session.scalars(select(PaperLedger))).all()
            assert all(x.wallet_delta + x.offset_delta == 0 for x in ledgers)
            assert all(
                x.portfolio_id in {pid, pending_id, race_id, *(p for _, p in created)}
                for x in ledgers
            )
            assert all(
                x.config_version == cfg.config_version and x.occurred_at.tzinfo for x in ledgers
            )
            assert all(
                x.mode == "paper" for x in (await session.scalars(select(PaperPortfolio))).all()
            )
            assert await session.scalar(select(func.count()).select_from(BacktestRun)) == 0
            for portfolio in (await session.scalars(select(PaperPortfolio))).all():
                own_ledger = [
                    row.wallet_delta for row in ledgers if row.portfolio_id == portfolio.id
                ]
                summary = await paper.summary(session, portfolio)
                assert Decimal(summary["wallet_balance"]) == sum(own_ledger, Decimal(0))
            assert (await session.get(AppState, 1)).kill_switch
        evidence["invariants"] = {
            "paper_only_mode": True,
            "backtest_runs": 0,
            "ledger_wallet_projection": True,
            "utc_config_version": True,
            "per_bot_intent_verdict_order_fill": True,
        }
        evidence["kill_bot_close"] = {
            "fresh_close": "ALLOW",
            "stale_close": "DATA_STALE",
            "pending": "KILL_CANCELED",
            "new_open": "KILL_SWITCH",
            "reduce_only": True,
            "ledger_balanced": True,
        }
        gate_portfolio = await paper.create(PortfolioCreate(name="S10G operational gate"))
        gate_id = gate_portfolio["id"]
        await paper.kill(False)
        await feed(db, cfg)
        await store.quality("stale", "injected_ws_disconnect", "rest_fallback")
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            disconnected = await client.post(
                f"http://127.0.0.1:{ports[0]}/api/v1/portfolios/{gate_id}/orders",
                json={**payload, "idempotency_key": "s10g-disconnect"},
            )
        assert disconnected.status_code == 200
        assert disconnected.json()["risk"]["reason"] == "DATA_STALE"
        assert disconnected.json()["order"] is None
        await feed(db, cfg)
        async with db.sessions.begin() as session:
            heartbeat = await session.get(WorkerHeartbeat, "trading")
            heartbeat.occurred_at = utc_now() - timedelta(seconds=cfg.worker_stale_seconds + 1)
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            stalled = await client.post(
                f"http://127.0.0.1:{ports[1]}/api/v1/portfolios/{gate_id}/orders",
                json={**payload, "idempotency_key": "s10g-heartbeat-stall"},
            )
        assert stalled.status_code == 200
        assert stalled.json()["risk"]["reason"] == "WORKER_STALE"
        assert stalled.json()["order"] is None
        assert stalled.json()["risk"]["snapshot"]["quality"]["trade_eligible"]
        evidence["operational_gate"] = {
            "ws_disconnect": "DATA_STALE_NO_ORDER",
            "heartbeat_only_stall": "WORKER_STALE_NO_ORDER",
            "heartbeat_bound_seconds": cfg.worker_stale_seconds,
        }

        await feed(db, cfg)
        await store.quality("stale", "injected_alert_confirmation")
        await monitor_once(db, cfg)
        async with db.sessions.begin() as session:
            row = await session.get(AlertState, ("market_stale", cfg.market_symbol))
            assert row and not row.active and row.pending_at is not None
            row.pending_at = utc_now() - timedelta(seconds=16)
        before = 0
        async with db.sessions() as session:
            before = await session.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.topic == "alert.telegram"
            ))
        alert_workers = [process("alert", env), process("alert", env)]
        for worker in alert_workers:
            completed(worker)
        async with db.sessions() as session:
            after = await session.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.topic == "alert.telegram"
            ))
            row = await session.get(AlertState, ("market_stale", cfg.market_symbol))
            assert row.active and after - before == 1
        evidence["alert_confirmation_race"] = {
            "processes": 2, "new_outbox_rows": after - before,
        }
        return evidence
    finally:
        for server in servers:
            server.terminate()
            try:
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
        await db.close()


def orchestrate():
    with tempfile.TemporaryDirectory(prefix="ai-futures-f5-pg-") as temp:
        data = Path(temp) / "data"
        port = free_port()
        api_ports = (free_port(), free_port())
        pg16 = "--pg16" in sys.argv
        pg = ["pg_ctl", "-D", str(data)]
        container = f"ai-futures-f5-pg16-{port}"
        if pg16:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    container,
                    "-e",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "-p",
                    f"127.0.0.1:{port}:5432",
                    "postgres:16.9-bookworm",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for _ in range(100):
                ready = subprocess.run(
                    ["docker", "exec", container, "pg_isready", "-U", "postgres"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                subprocess.run(
                    ["docker", "rm", "-f", container],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                raise AssertionError("Temporary PostgreSQL 16 container did not start")
        else:
            subprocess.run(
                ["initdb", "-A", "trust", "--no-instructions", "-D", str(data)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                pg + ["-o", f"-h 127.0.0.1 -p {port} -k {temp}", "-w", "start"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        try:
            env = os.environ | {
                "APP_ENV": "test",
                "BACKEND_AUTH_TOKEN": "x" * 48,
                "DATABASE_URL": (
                    f"postgresql+asyncpg://{'postgres' if pg16 else getpass.getuser()}"
                    f"@127.0.0.1:{port}/postgres"
                ),
                "AI_ENABLED": "false",
                "LIVE_TRADING_ENABLED": "false",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
            }
            env["F5_CLOCK_UTC"] = datetime.now(UTC).replace(second=20, microsecond=0).isoformat()
            os.environ.update(env)
            subprocess.run(
                [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            result = asyncio.run(run_gate(env, api_ports))
            result["database"] = (
                "temporary_postgres_16_9_docker_container"
                if pg16
                else "temporary_local_postgresql_18_cluster"
            )
        finally:
            if pg16:
                subprocess.run(
                    ["docker", "rm", "-f", container],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            else:
                subprocess.run(
                    pg + ["-m", "immediate", "-w", "stop"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
        result["temporary_postgres_stopped"] = True
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"api", "worker", "crash", "alert"}:
        asyncio.run(child(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        orchestrate()
