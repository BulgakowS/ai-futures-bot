"""Bounded offline faults against real API and trading worker processes on temporary PostgreSQL.

Run: .venv/bin/python scripts/paper_worker_faults.py
Exchange and Redis calls use loopback-only fixtures. The temporary database is removed on exit.
"""

import asyncio
import getpass
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
from ai_futures.alerts import monitor_once
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.market import MarketStore
from ai_futures.mexc import payload_hash
from ai_futures.models import AlertState, FundingCoverage, OutboxEvent, WorkerHeartbeat
from ai_futures.paper import PaperService, PortfolioCreate
from ai_futures.paper_models import PaperFill, PaperIntent, PaperLedger, PaperOrder
from ai_futures.schemas import utc_now
from ai_futures.state import seed_state
from paper_process_faults import RULES, RedisStub, deny_external_network, free_port
from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"


def settings():
    return Settings(
        _env_file=None,
        app_env="test",
        backend_auth_token="x" * 48,
        database_url=os.environ["DATABASE_URL"],
        worker_heartbeat_seconds=1,
        worker_stale_seconds=4,
        ai_enabled=False,
    )


def mode(path):
    return Path(path).read_text().strip()


async def child(role):
    deny_external_network()
    cfg = settings()
    if role == "api":
        import uvicorn
        from ai_futures.main import create_app

        await uvicorn.Server(
            uvicorn.Config(
                create_app(cfg, redis=RedisStub()),
                host="127.0.0.1",
                port=int(os.environ["FAULT_API_PORT"]),
                log_level="error",
            )
        ).serve()
        return

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fault_trading_worker", ROOT / "services/trading/worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    control = os.environ["FAULT_CONTROL"]

    class FakeAdapter:
        async def close(self):
            pass

    class FakeIngestion:
        def __init__(self, database, config, adapter):
            self.store = MarketStore(database, config.config_version, config.market_symbol)
            self.last_ticker = None

        async def run(self, stop):
            while not stop.is_set():
                if mode(control) != "ingestion":
                    price = (
                        "111"
                        if mode(control) == "cross"
                        else ("110" if mode(control) == "paper_cross" else "100")
                    )
                    now = utc_now()
                    await self.store.snapshot(
                        "ticker", {"ask1": price, "bid1": price, "fairPrice": price}, now, "ws"
                    )
                    await self.store.snapshot(
                        "depth",
                        {
                            "version": time.monotonic_ns(),
                            "asks": [[price, "10000"]],
                            "bids": [[price, "10000"]],
                        },
                        now,
                    )
                    await self.store.quality("ok", "controlled_public_fixture")
                    self.last_ticker = utc_now()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.4)
                except TimeoutError:
                    pass

    class ControlledRedis(RedisStub):
        async def ping(self):
            if mode(control) == "redis_delay":
                await asyncio.sleep(8)
            return True

    from ai_futures.bots import BotService

    paper_tick = PaperService.tick
    bot_tick = BotService.tick

    async def paper_fault(self):
        while mode(control) in {"paper", "paper_cross"}:  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        return await paper_tick(self)

    async def bot_fault(self):
        while mode(control) == "bot":  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        return await bot_tick(self)

    worker.MexcPublic = FakeAdapter
    worker.MarketIngestion = FakeIngestion
    worker.Redis.from_url = lambda *args, **kwargs: ControlledRedis()
    PaperService.tick = paper_fault
    BotService.tick = bot_fault
    worker.get_settings = lambda: cfg
    await worker.main()


async def wait_until(predicate, seconds=12):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        result = await predicate()
        if result:
            return {"utc": utc_now().isoformat(), "elapsed_s": round(time.monotonic() - started, 3)}
        await asyncio.sleep(0.2)
    raise AssertionError(f"Condition not reached within {seconds}s")


async def run_gate(env, api_port, control, temp):
    deny_external_network()
    cfg = settings()
    db = Database(cfg)
    children = []

    def spawn(role):
        log = Path(temp) / f"{role}-{len(children)}.log"
        handle = log.open("w")
        proc = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), role],
            cwd=ROOT,
            env=env | {"FAULT_API_PORT": str(api_port), "FAULT_CONTROL": str(control)},
            stdout=subprocess.DEVNULL,
            stderr=handle,
        )
        handle.close()
        children.append(proc)
        return proc

    async def heartbeat():
        async with db.sessions() as session:
            return await session.get(WorkerHeartbeat, "trading")

    async def worker_status(expected):
        row = await heartbeat()
        age = (utc_now() - row.occurred_at).total_seconds() if row else None
        if expected == "idle":
            return row is not None and row.status == "idle" and 0 <= age < 4
        return row is not None and (row.status == "degraded" or age >= 4)

    async def alert_active(kind):
        async with db.sessions() as session:
            row = await session.get(
                AlertState, (kind, "trading" if kind == "worker_down" else cfg.market_symbol)
            )
            return bool(row and row.active)

    async def counts(portfolio_id):
        portfolio_id = UUID(str(portfolio_id))
        async with db.sessions() as session:
            return {
                "intent": await session.scalar(
                    select(func.count())
                    .select_from(PaperIntent)
                    .where(PaperIntent.portfolio_id == portfolio_id)
                ),
                "order": await session.scalar(
                    select(func.count())
                    .select_from(PaperOrder)
                    .where(PaperOrder.portfolio_id == portfolio_id)
                ),
                "fill": await session.scalar(
                    select(func.count())
                    .select_from(PaperFill)
                    .join(PaperOrder)
                    .where(PaperOrder.portfolio_id == portfolio_id)
                ),
            }

    async def api(client, method, path, payload=None):
        response = await client.request(method, f"http://127.0.0.1:{api_port}{path}", json=payload)
        assert response.status_code in {200, 201, 503}, (
            path,
            response.status_code,
            response.text[:400],
        )
        return response.json()

    async def check_block(client, portfolio_id, key):
        before = await counts(portfolio_id)
        result = await api(
            client,
            "POST",
            f"/api/v1/portfolios/{portfolio_id}/orders",
            {
                "idempotency_key": key,
                "direction": "LONG",
                "quantity": "1",
                "leverage": "1",
                "stop_loss": "99",
            },
        )
        after = await counts(portfolio_id)
        reason = result["risk"]["reason"]
        assert reason in {"WORKER_STALE", "DATA_STALE"}, result
        assert (
            result["order"] is None
            and after["order"] == before["order"]
            and after["fill"] == before["fill"]
        )
        return {"reason": reason, "no_order_or_fill": True}

    evidence = {
        "mode": "paper",
        "market_source": "synthetic_loopback_only",
        "database": "temporary_local_postgresql_cluster",
        "worker_process": "run_worker",
        "api_process": "uvicorn_create_app",
        "worker_heartbeat_seconds": 1,
        "worker_stale_seconds": 4,
        "alert_confirm_seconds": {"worker_down": 10, "market_stale": 15},
        "private_mexc_calls": 0,
        "external_messages": 0,
    }
    try:
        await seed_state(db, cfg.config_version)
        paper = PaperService(db, cfg.config_version)
        await paper.kill(False)
        store = MarketStore(db, cfg.config_version)
        await store.rules(RULES)
        now = utc_now()
        async with db.sessions.begin() as session:
            await session.merge(
                FundingCoverage(
                    symbol=cfg.market_symbol,
                    covered_from=now - timedelta(days=1),
                    covered_until=now + timedelta(hours=8),
                    status="verified",
                    gap_time=None,
                    source="synthetic_fault_fixture",
                    snapshot_hash=payload_hash({}),
                    observed_at=now,
                    config_version=cfg.config_version,
                )
            )
        first = await paper.create(PortfolioCreate(name="S10H close", deposit="1000"))
        second = await paper.create(PortfolioCreate(name="S10H pending", deposit="1000"))
        first_id, second_id = first["id"], second["id"]
        worker_proc = spawn("worker")
        evidence["startup"] = await wait_until(lambda: worker_status("idle"))
        api_proc = spawn("api")
        async with httpx.AsyncClient(
            timeout=4, headers={"Authorization": "Bearer " + "x" * 48}
        ) as client:
            await wait_until(lambda: api_live(client, api_port))
            await monitor_once(db, cfg)
            base = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{first_id}/orders",
                {
                    "idempotency_key": "s10h-base-open",
                    "direction": "LONG",
                    "quantity": "1",
                    "leverage": "1",
                    "stop_loss": "99",
                },
            )
            assert base["risk"]["outcome"] == "ALLOW" and base["order"] is not None, base
            position_id = base["order"].get("position_id")
            if position_id is None:
                async with db.sessions() as session:
                    from ai_futures.paper_models import PaperPosition

                    position_id = str(
                        (
                            await session.scalar(
                                select(PaperPosition).where(
                                    PaperPosition.portfolio_id == UUID(first_id)
                                )
                            )
                        ).id
                    )
            pending = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{second_id}/orders",
                {
                    "idempotency_key": "s10h-pending",
                    "direction": "SHORT",
                    "kind": "limit",
                    "quantity": "1",
                    "leverage": "1",
                    "limit_price": "110",
                    "stop_loss": "112",
                },
            )
            assert (
                pending["risk"]["outcome"] == "ALLOW" and pending["order"]["status"] == "PENDING"
            ), pending
            pending_counts = await counts(second_id)
            assert not await alert_active("worker_down")

            control.write_text("paper_cross")
            fault_at = utc_now()
            degraded = await wait_until(lambda: worker_status("degraded"), 8)
            ready = await api(client, "GET", "/api/v1/health/ready")
            assert ready["dependencies"]["trading"] == "stale", ready
            paper_block = await check_block(client, second_id, "s10h-paper-block")
            await (
                paper.tick()
            )  # Independent retry must not fill a pending order during degraded worker health.
            assert (await counts(second_id))["fill"] == pending_counts["fill"]
            async with db.sessions() as session:
                halted_order = await session.get(PaperOrder, UUID(pending["order"]["id"]))
                assert halted_order.status == "RISK_CANCELED"
            evidence["paper_stall"] = {
                "fault_utc": fault_at.isoformat(),
                "degraded": degraded,
                "blocked_open": paper_block,
                "pending_no_fill": True,
                "pending_status": "RISK_CANCELED",
                "readiness_trading": ready["dependencies"]["trading"],
            }
            evidence["worker_alert"] = await wait_until(lambda: alert_active("worker_down"), 22)
            await api(client, "POST", "/api/v1/risk/kill-switch", {"kill_switch": True})
            closed = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{first_id}/positions/{position_id}/close",
                {
                    "idempotency_key": "s10h-kill-close",
                    "reason": "manual",
                },
            )
            assert closed["risk"]["outcome"] == "ALLOW" and closed["order"] is not None, closed
            assert (await counts(first_id))["fill"] == 2
            evidence["kill_close_fresh"] = {
                "risk": closed["risk"]["outcome"],
                "fill": True,
            }
            await api(client, "POST", "/api/v1/risk/kill-switch", {"kill_switch": False})
            control.write_text("normal")
            evidence["paper_recovery"] = await wait_until(lambda: worker_status("idle"), 8)

            reopened = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{first_id}/orders",
                {
                    "idempotency_key": "s10h-reopen",
                    "direction": "LONG",
                    "quantity": "1",
                    "leverage": "1",
                    "stop_loss": "99",
                },
            )
            assert reopened["risk"]["outcome"] == "ALLOW", reopened
            async with db.sessions() as session:
                from ai_futures.paper_models import PaperPosition

                open_position = await session.scalar(
                    select(PaperPosition).where(
                        PaperPosition.portfolio_id == UUID(first_id),
                        PaperPosition.closed_at.is_(None),
                    )
                )
                assert open_position is not None
                fresh_position_id = str(open_position.id)

            control.write_text("bot")
            bot_at = utc_now()
            degraded = await wait_until(lambda: worker_status("degraded"), 8)
            evidence["bot_stall"] = {
                "fault_utc": bot_at.isoformat(),
                "degraded": degraded,
                "blocked_open": await check_block(client, second_id, "s10h-bot-block"),
            }
            control.write_text("normal")
            await wait_until(lambda: worker_status("idle"), 8)

            control.write_text("ingestion")
            ingestion_at = utc_now()
            degraded = await wait_until(lambda: worker_status("degraded"), 8)
            evidence["ingestion_stall"] = {
                "fault_utc": ingestion_at.isoformat(),
                "degraded": degraded,
                "blocked_open": await check_block(client, second_id, "s10h-ingestion-block"),
            }
            await asyncio.sleep(2)
            stale_close = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{first_id}/positions/{fresh_position_id}/close",
                {
                    "idempotency_key": "s10h-stale-close",
                    "reason": "manual",
                },
            )
            assert stale_close["risk"]["reason"] == "DATA_STALE" and stale_close["order"] is None, (
                stale_close
            )
            evidence["ingestion_stall"]["stale_reduce_close"] = "DATA_STALE"
            evidence["market_alert"] = await wait_until(lambda: alert_active("market_stale"), 22)
            control.write_text("normal")
            await wait_until(lambda: worker_status("idle"), 8)

            recovered_pending = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{second_id}/orders",
                {
                    "idempotency_key": "s10h-pending-after-recovery",
                    "direction": "SHORT",
                    "kind": "limit",
                    "quantity": "1",
                    "leverage": "1",
                    "limit_price": "110",
                    "stop_loss": "112",
                },
            )
            assert recovered_pending["risk"]["outcome"] == "ALLOW"
            assert recovered_pending["order"]["status"] == "PENDING"
            control.write_text("cross")
            evidence["new_pending_after_recovery_fill"] = await wait_until(
                lambda: fill_count_is(counts, second_id, 1), 8
            )
            control.write_text("normal")

            lock_started = utc_now()
            lock_monotonic = time.monotonic()
            async with db.sessions.begin() as session:
                await session.execute(
                    text("SELECT name FROM worker_heartbeat WHERE name='trading' FOR UPDATE")
                )
                locked = await wait_until(lambda: worker_status("degraded"), 8)
                ready = await api(client, "GET", "/api/v1/health/ready")
                assert ready["dependencies"]["trading"] == "stale", ready
                db_block = await check_block(client, second_id, "s10h-db-block")
                evidence["db_row_delay"] = {
                    "fault_utc": lock_started.isoformat(),
                    "stale": locked,
                    "blocked_open": db_block,
                    "readiness_trading": ready["dependencies"]["trading"],
                }
            evidence["db_row_delay"]["released_utc"] = utc_now().isoformat()
            evidence["db_row_delay"]["hold_elapsed_s"] = round(time.monotonic() - lock_monotonic, 3)
            await wait_until(lambda: worker_status("idle"), 8)

            before = await counts(second_id)
            worker_proc.send_signal(signal.SIGTERM)
            await wait_until(lambda: process_exited(worker_proc), 8)
            worker_proc = spawn("worker")
            await wait_until(lambda: worker_status("idle"), 8)
            replay = await api(
                client,
                "POST",
                f"/api/v1/portfolios/{second_id}/orders",
                {
                    "idempotency_key": "s10h-pending-after-recovery",
                    "direction": "SHORT",
                    "kind": "limit",
                    "quantity": "1",
                    "leverage": "1",
                    "limit_price": "110",
                    "stop_loss": "112",
                },
            )
            after = await counts(second_id)
            assert (
                replay["intent"]["id"] == recovered_pending["intent"]["id"]
                and after == before
                and after["fill"] == 1
            )
            async with db.sessions() as session:
                ledger = (await session.scalars(select(PaperLedger))).all()
                assert ledger and all(
                    Decimal(row.wallet_delta) + Decimal(row.offset_delta) == 0 for row in ledger
                )
                outbox = (await session.scalars(select(OutboxEvent))).all()
                evidence["alert_outbox"] = {
                    kind: sum(
                        event.topic == "alert.telegram" and event.payload.get("kind") == kind
                        for event in outbox
                    )
                    for kind in ("worker_down", "market_stale", "kill_switch")
                }
                evidence["ledger"] = {"rows": len(ledger), "balanced": True}
            evidence["restart_replay"] = {
                "same_intent": True,
                "counts_before": before,
                "counts_after": after,
            }
            evidence["processes"] = {"api": 1, "trading_worker": 2}
            assert api_proc.poll() is None and worker_proc.poll() is None
        return evidence
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        await db.close()


async def api_live(client, port):
    try:
        response = await client.get(f"http://127.0.0.1:{port}/api/v1/health/live")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def process_exited(proc):
    return proc.poll() is not None


async def fill_count_is(counts, portfolio_id, expected):
    return (await counts(portfolio_id))["fill"] == expected


async def run_redis_gate(env, api_port, control, temp):
    """Single Redis timeout fault; does not repeat the S10H partial-loop matrix."""
    deny_external_network()
    cfg = settings()
    db = Database(cfg)
    children = []

    def spawn(role):
        log = Path(temp) / f"redis-{role}-{len(children)}.log"
        handle = log.open("w")
        proc = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), role],
            cwd=ROOT,
            env=env | {"FAULT_API_PORT": str(api_port), "FAULT_CONTROL": str(control)},
            stdout=subprocess.DEVNULL,
            stderr=handle,
        )
        handle.close()
        children.append(proc)
        return proc, log

    async def healthy(expected):
        async with db.sessions() as session:
            row = await session.get(WorkerHeartbeat, "trading")
        age = (utc_now() - row.occurred_at).total_seconds() if row else None
        return bool(
            row
            and (
                (expected == "idle" and row.status == "idle" and 0 <= age < 4)
                or (expected == "stale" and (row.status != "idle" or age >= 4))
            )
        )

    async def counts(portfolio_id):
        portfolio_id = UUID(portfolio_id)
        async with db.sessions() as session:
            return {
                "intent": await session.scalar(
                    select(func.count())
                    .select_from(PaperIntent)
                    .where(PaperIntent.portfolio_id == portfolio_id)
                ),
                "order": await session.scalar(
                    select(func.count())
                    .select_from(PaperOrder)
                    .where(PaperOrder.portfolio_id == portfolio_id)
                ),
                "fill": await session.scalar(
                    select(func.count())
                    .select_from(PaperFill)
                    .join(PaperOrder)
                    .where(PaperOrder.portfolio_id == portfolio_id)
                ),
            }

    evidence = {
        "mode": "paper",
        "fault": "controlled_worker_redis_ping_delay",
        "database": "temporary_local_postgresql_cluster",
        "api_process": "uvicorn_create_app",
        "worker_process": "run_worker",
        "redis_ping_delay_seconds": 8,
        "worker_ping_timeout_seconds": 5,
        "worker_heartbeat_seconds": 1,
        "worker_stale_seconds": 4,
        "market_source": "synthetic_loopback_only",
        "external_messages": 0,
        "private_mexc_calls": 0,
    }
    try:
        await seed_state(db, cfg.config_version)
        paper = PaperService(db, cfg.config_version)
        await paper.kill(False)
        store = MarketStore(db, cfg.config_version)
        await store.rules(RULES)
        now = utc_now()
        async with db.sessions.begin() as session:
            await session.merge(
                FundingCoverage(
                    symbol=cfg.market_symbol,
                    covered_from=now - timedelta(days=1),
                    covered_until=now + timedelta(hours=8),
                    status="verified",
                    gap_time=None,
                    source="synthetic_redis_fault_fixture",
                    snapshot_hash=payload_hash({}),
                    observed_at=now,
                    config_version=cfg.config_version,
                )
            )
        portfolio = await paper.create(PortfolioCreate(name="S10I Redis", deposit="1000"))
        portfolio_id = portfolio["id"]
        worker, worker_log = spawn("worker")
        evidence["startup"] = await wait_until(lambda: healthy("idle"), 12)
        api, _ = spawn("api")
        async with httpx.AsyncClient(
            timeout=4, headers={"Authorization": "Bearer " + "x" * 48}
        ) as client:
            await wait_until(lambda: api_live(client, api_port), 12)
            route = f"http://127.0.0.1:{api_port}/api/v1/portfolios/{portfolio_id}/orders"
            pending_body = {
                "idempotency_key": "s10i-pending",
                "direction": "SHORT",
                "kind": "limit",
                "quantity": "1",
                "leverage": "1",
                "limit_price": "110",
                "stop_loss": "112",
            }
            pending = (await client.post(route, json=pending_body)).json()
            assert pending["risk"]["outcome"] == "ALLOW"
            assert pending["order"]["status"] == "PENDING"
            before = await counts(portfolio_id)

            control.write_text("redis_delay")
            fault_at = utc_now()
            evidence["heartbeat_stale"] = await wait_until(lambda: healthy("stale"), 9)
            evidence["ping_timeout_logged"] = await wait_until(
                lambda: worker_warning_logged(worker_log), 8
            )
            ready_response = await client.get(f"http://127.0.0.1:{api_port}/api/v1/health/ready")
            ready = ready_response.json()
            assert ready_response.status_code == 503
            assert ready["dependencies"]["trading"] == "stale"
            assert ready["dependencies"]["redis"] == "ok"
            blocked_body = {
                "idempotency_key": "s10i-redis-blocked",
                "direction": "LONG",
                "quantity": "1",
                "leverage": "1",
                "stop_loss": "99",
            }
            blocked = (await client.post(route, json=blocked_body)).json()
            assert blocked["risk"]["reason"] == "WORKER_STALE" and blocked["order"] is None
            await paper.tick()
            after_fault = await counts(portfolio_id)
            assert after_fault["order"] == before["order"] and after_fault["fill"] == 0
            async with db.sessions() as session:
                halted = await session.get(PaperOrder, UUID(pending["order"]["id"]))
                assert halted.status == "RISK_CANCELED"
            evidence["fault_utc"] = fault_at.isoformat()
            evidence["fault_readiness"] = {
                "trading": ready["dependencies"]["trading"],
                "redis": ready["dependencies"]["redis"],
            }
            evidence["blocked_entry"] = {
                "reason": blocked["risk"]["reason"],
                "no_new_order_or_fill": True,
                "pending_status": halted.status,
            }

            control.write_text("normal")
            evidence["recovered"] = await wait_until(lambda: healthy("idle"), 9)
            good_body = {
                "idempotency_key": "s10i-after-recovery",
                "direction": "LONG",
                "quantity": "1",
                "leverage": "1",
                "stop_loss": "99",
            }
            accepted = (await client.post(route, json=good_body)).json()
            assert accepted["risk"]["outcome"] == "ALLOW" and accepted["order"] is not None
            before_restart = await counts(portfolio_id)
            assert before_restart["fill"] == 1
            worker.send_signal(signal.SIGTERM)
            await wait_until(lambda: process_exited(worker), 8)
            worker, _ = spawn("worker")
            await wait_until(lambda: healthy("idle"), 9)
            replay = (await client.post(route, json=good_body)).json()
            after_restart = await counts(portfolio_id)
            assert replay["intent"]["id"] == accepted["intent"]["id"]
            assert after_restart == before_restart
            async with db.sessions() as session:
                ledger = (
                    await session.scalars(
                        select(PaperLedger).where(PaperLedger.portfolio_id == UUID(portfolio_id))
                    )
                ).all()
                assert ledger and all(
                    Decimal(row.wallet_delta) + Decimal(row.offset_delta) == 0 for row in ledger
                )
            evidence["restart_replay"] = {
                "same_intent": True,
                "counts_before": before_restart,
                "counts_after": after_restart,
                "ledger_rows": len(ledger),
                "ledger_balanced": True,
            }
            assert api.poll() is None and worker.poll() is None
        evidence["processes"] = {"api": 1, "trading_worker": 2}
        return evidence
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        await db.close()


async def worker_warning_logged(path):
    return "worker_dependencies_unavailable" in path.read_text()


def orchestrate():
    with tempfile.TemporaryDirectory(prefix="ai-futures-s10h-pg-") as temp:
        data = Path(temp) / "data"
        port, api_port = free_port(), free_port()
        control = Path(temp) / "control"
        control.write_text("normal")
        pg = ["pg_ctl", "-D", str(data)]
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
                "DATABASE_URL": f"postgresql+asyncpg://{getpass.getuser()}@127.0.0.1:{port}/postgres",
                "WORKER_HEARTBEAT_SECONDS": "1",
                "WORKER_STALE_SECONDS": "4",
                "AI_ENABLED": "false",
                "LIVE_TRADING_ENABLED": "false",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
            }
            os.environ.update(env)
            subprocess.run(
                [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            gate = run_redis_gate if "--redis-only" in sys.argv else run_gate
            evidence = asyncio.run(gate(env, api_port, control, temp))
        finally:
            subprocess.run(
                pg + ["-m", "immediate", "-w", "stop"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        evidence["temporary_postgres_stopped"] = True
        print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"api", "worker"}:
        asyncio.run(child(sys.argv[1]))
    else:
        orchestrate()
