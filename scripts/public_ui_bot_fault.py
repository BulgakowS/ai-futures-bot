"""Bounded, isolated public WS fault through persisted API, Web UI and bot."""

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from ai_futures.bot_models import Bot, BotDecision
from ai_futures.bots import BotCreate, BotService
from ai_futures.config import Settings
from ai_futures.db import Base, Database
from ai_futures.health import expected_revisions
from ai_futures.main import create_app
from ai_futures.market_worker import MarketIngestion
from ai_futures.mexc import WS_URL
from ai_futures.models import AppState, Candle, WorkerHeartbeat
from ai_futures.paper import RiskProfile
from ai_futures.paper_models import PaperIntent, PaperOrder
from ai_futures.schemas import utc_now
from ai_futures.strategies import StrategyConfig
from public_ws_fault import CountedPublic, ObservedSocket, observe, until
from sqlalchemy import func, select, text
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
BROWSE = Path.home() / ".agents/skills/gstack/browse/src/cli.ts"
BUN = Path.home() / ".bun/bin/bun"
WEB_DIST = ROOT / "apps/web/.next-s10o"


class LocalRedis:
    async def ping(self):
        return True

    async def aclose(self):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def command(*args, limit_seconds=20):
    argv = (BUN, "run", *args) if args[0] == BROWSE else args
    process = await asyncio.create_subprocess_exec(
        *map(str, argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), limit_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    output = out.decode(errors="replace")
    if process.returncode:
        raise RuntimeError(f"command_failed:{Path(args[0]).name}:{output[-300:]}")
    return output


async def browser_assert(expression):
    last_error = ""
    for _ in range(12):
        try:
            output = await command(BROWSE, "js", expression, limit_seconds=8)
            if "S10O_OK" in output:
                return
            last_error = output[-300:]
        except RuntimeError as exc:
            last_error = str(exc)[-300:]
        await asyncio.sleep(0.5)  # Browser/React state settles after the BFF fetches.
    raise AssertionError(f"browser_assert_failed:{last_error}")


async def browser_refresh():
    await command(
        BROWSE,
        "js",
        "(()=>{const b=[...document.querySelectorAll('button')]"
        ".find(x=>x.textContent.trim()==='Оновити');"
        "if(!b)throw Error('refresh_missing');b.click();return 'S10O_OK'})()",
    )
    await asyncio.sleep(1.2)


async def api_state(client, base):
    result = {}
    for path in ("health", "markets/BTC_USDT/ticker", "markets/BTC_USDT/quality"):
        response = await client.get(f"{base}/api/v1/{path}")
        result[path] = {"http": response.status_code, "body": response.json()}
    return result


async def bot_counts(database, bot_id):
    async with database.sessions() as session:
        decisions = (
            await session.scalars(select(BotDecision).where(BotDecision.bot_id == bot_id))
        ).all()
        return {
            "decisions": [
                {
                    "window_start": row.window_start.isoformat(),
                    "action": row.action,
                    "reason": row.reason,
                    "status": row.status,
                    "intent_id": str(row.intent_id) if row.intent_id else None,
                }
                for row in decisions
            ],
            "intents": await session.scalar(select(func.count()).select_from(PaperIntent)),
            "orders": await session.scalar(select(func.count()).select_from(PaperOrder)),
        }


async def run():
    evidence = {
        "started_at": utc_now().isoformat(),
        "scope": "disposable_sqlite_api_web_bot_real_public_mexc",
        "symbol": "BTC_USDT",
        "max_seconds": 230,
        "max_public_rest_operations": 60,
        "private_requests": 0,
        "telegram_sends": 0,
        "active_compose_mutations": 0,
        "result": "blocked_or_failed",
    }
    web = None
    web_log = None
    api_task = None
    market_task = None
    heartbeat_task = None
    database = None
    adapter = None
    browser_was_running = False
    browser_started = False
    stop = asyncio.Event()
    allow_reconnect = asyncio.Event()
    allow_second_ticker = asyncio.Event()
    try:
        async with asyncio.timeout(230):
            if WEB_DIST.exists():
                raise RuntimeError("isolated Next dist directory already exists")
            if not BROWSE.is_file() or not BUN.is_file():
                raise RuntimeError("gstack browse source CLI unavailable")
            browser_was_running = (ROOT / ".gstack/browse.json").exists()
            await command(BROWSE, "status", limit_seconds=30)
            with tempfile.TemporaryDirectory(prefix="s10o-public-ui-bot-") as directory:
                temp = Path(directory)
                api_port, web_port = free_port(), free_port()
                assert api_port != web_port
                token = secrets.token_hex(32)
                settings = Settings(
                    _env_file=None,
                    app_env="test",
                    app_mode="paper",
                    live_trading_enabled=False,
                    ai_enabled=False,
                    telegram_delivery_enabled=False,
                    telegram_bot_token=None,
                    telegram_chat_id=None,
                    openai_api_key=None,
                    backend_auth_token=token,
                    database_url=f"sqlite+aiosqlite:///{temp / 's10o.db'}",
                )
                database = Database(settings)
                async with database.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(
                        text(
                            "CREATE TABLE alembic_version "
                            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                        )
                    )
                    heads = expected_revisions()
                    assert len(heads) == 1
                    await connection.execute(
                        text("INSERT INTO alembic_version VALUES (:v)"), {"v": next(iter(heads))}
                    )
                app = create_app(settings, database, LocalRedis())
                server = uvicorn.Server(
                    uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
                )
                api_task = asyncio.create_task(server.serve())
                async with asyncio.timeout(12):
                    while not server.started:  # noqa: ASYNC110 - uvicorn exposes polling state only.
                        await asyncio.sleep(0.1)
                async with database.sessions.begin() as session:
                    state = await session.get(AppState, 1)
                    state.kill_switch = False
                    session.add(
                        WorkerHeartbeat(
                            name="trading",
                            instance_id=uuid4(),
                            status="idle",
                            occurred_at=utc_now(),
                        )
                    )

                async def heartbeat():
                    while not stop.is_set():
                        async with database.sessions.begin() as session:
                            row = await session.get(WorkerHeartbeat, "trading")
                            row.occurred_at = utc_now()
                        await asyncio.sleep(2)

                heartbeat_task = asyncio.create_task(heartbeat())
                web_env = os.environ.copy()
                web_env.update(
                    {
                        "APP_ENV": "test",
                        "AF_BFF_TEST_PORT": str(web_port),
                        "AF_TEST_DIST_DIR": ".next-s10o",
                        "BACKEND_URL": f"http://127.0.0.1:{api_port}",
                        "BACKEND_AUTH_TOKEN": token,
                    }
                )
                web_log = (temp / "web.log").open("w")
                web = await asyncio.create_subprocess_exec(
                    str(ROOT / "apps/web/node_modules/.bin/next"),
                    "dev",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    cwd=ROOT / "apps/web",
                    env=web_env,
                    stdout=web_log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                async with httpx.AsyncClient(timeout=10) as client:
                    async with asyncio.timeout(35):
                        while True:
                            try:
                                response = await client.get(
                                    f"http://127.0.0.1:{web_port}/api/backend/health"
                                )
                                if response.status_code in (200, 503):
                                    break
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.4)
                    assert web.returncode is None
                    assert (
                        await client.get(
                            f"http://127.0.0.1:{web_port}/api/backend/markets/BTC_USDT/quality"
                        )
                    ).status_code == 200
                    nonlocal_response = await client.get(
                        f"http://127.0.0.1:{web_port}/api/backend/health",
                        headers={"Host": "example.com"},
                    )
                    assert nonlocal_response.status_code == 403
                    evidence["bff_loopback_guard"] = (
                        "configured_test_port_accepted_nonlocal_host_403"
                    )

                    adapter = CountedPublic()
                    sockets = []
                    second_reconciled = asyncio.Event()
                    barrier = {"at": None}

                    @asynccontextmanager
                    async def connector(url, **kwargs):
                        assert url == WS_URL
                        generation = len(sockets) + 1
                        if generation == 2:
                            await allow_reconnect.wait()
                        if generation > 2:
                            raise RuntimeError("WS connection bound exceeded")
                        async with connect(url, **kwargs) as raw:
                            socket = ObservedSocket(raw, generation, allow_second_ticker, barrier)
                            sockets.append(socket)
                            yield socket

                    market = MarketIngestion(database, settings, adapter, connector)
                    original_reconcile = market.reconcile
                    reconciliations = 0

                    async def tracked_reconcile():
                        nonlocal reconciliations
                        await original_reconcile()
                        reconciliations += 1
                        if reconciliations == 2:
                            barrier["at"] = utc_now()
                            second_reconciled.set()

                    market.reconcile = tracked_reconcile
                    market_task = asyncio.create_task(market.run(stop))
                    evidence["before"] = await until(
                        database, lambda s: s["trade_eligible"] and s["ticker_source"] == "ws", 75
                    )

                    config = StrategyConfig(
                        strategy="trend_following",
                        timeframe="1m",
                        quantity="1",
                        leverage="1",
                        stop_distance="0.01",
                        fast_period=2,
                        slow_period=3,
                        breakout_period=2,
                        breakout_buffer="0",
                        min_volume_ratio="1",
                    )
                    bots = BotService(database, settings.config_version)
                    running = await bots.create(
                        BotCreate(
                            name="S10O running",
                            deposit="1000",
                            risk_profile=RiskProfile(),
                            config=config,
                        )
                    )
                    ready = await bots.create(
                        BotCreate(
                            name="S10O ready",
                            deposit="1000",
                            risk_profile=RiskProfile(),
                            config=config,
                        )
                    )
                    running_id = UUID(running["id"])
                    await bots.transition(running_id, "ready")
                    await bots.transition(running_id, "start")
                    await bots.transition(UUID(ready["id"]), "ready")
                    browser_started = True
                    await command(BROWSE, "goto", f"http://localhost:{web_port}", limit_seconds=25)
                    await command(
                        BROWSE,
                        "js",
                        "(()=>{const b=document.querySelector('nav button:nth-child(2)');"
                        "if(!b)throw Error('bots_nav_missing');b.click();"
                        "return 'S10O_OK'})()",
                    )
                    await asyncio.sleep(1)
                    evidence["before_api"] = await api_state(client, f"http://127.0.0.1:{api_port}")

                    await sockets[0].close()
                    evidence["injected_close_at"] = utc_now().isoformat()
                    evidence["fault"] = await until(
                        database,
                        lambda s: s["quality_status"] == "stale" and not s["trade_eligible"],
                        15,
                    )
                    assert evidence["fault"]["transport"] == "rest_fallback"
                    evidence["fallback"] = await until(
                        database,
                        lambda s: s["ticker_source"] == "rest_fallback" and not s["trade_eligible"],
                        20,
                    )
                    evidence["fault_api"] = await api_state(client, f"http://127.0.0.1:{api_port}")
                    assert (
                        evidence["fault_api"]["markets/BTC_USDT/quality"]["body"]["trade_eligible"]
                        is False
                    )
                    await browser_refresh()
                    await browser_assert(
                        "(()=>{const t=document.body.innerText;"
                        "const b=[...document.querySelectorAll('button')]"
                        ".find(x=>x.textContent.trim()==='Запустити');"
                        "if(!t.includes('Дані обмежені')||"
                        "!t.includes('disconnect_or_schema_error')||!b?.disabled)"
                        "throw Error('stale_ui_gate:'+"
                        "[t.includes('Дані обмежені'),"
                        "t.includes('disconnect_or_schema_error'),b?.disabled].join(','));"
                        "return 'S10O_OK'})()"
                    )
                    evidence["fault_ui"] = "real_backend_stale_banner_and_ready_start_disabled"
                    async with database.sessions.begin() as session:
                        latest = await session.scalar(
                            select(Candle)
                            .where(Candle.timeframe == "1m", Candle.final)
                            .order_by(Candle.window_start.desc())
                            .limit(1)
                        )
                        assert latest
                        previous = await session.scalar(
                            select(Candle)
                            .where(
                                Candle.timeframe == "1m",
                                Candle.final,
                                Candle.window_start < latest.window_start,
                            )
                            .order_by(Candle.window_start.desc())
                            .limit(1)
                        )
                        assert previous
                        bot = await session.get(Bot, running_id)
                        bot.last_window = previous.window_start
                        evidence["controlled_bot_cursor"] = {
                            "last_window": previous.window_start.isoformat(),
                            "target_real_final_window": latest.window_start.isoformat(),
                            "target_candle_event_id": str(latest.event_id),
                        }
                    await bots.tick()
                    await BotService(database, settings.config_version).tick()
                    evidence["fault_bot"] = await bot_counts(database, running_id)
                    assert evidence["fault_bot"]["intents"] == 0
                    assert evidence["fault_bot"]["orders"] == 0
                    assert any(
                        d["reason"] == "DATA_STALE" and d["action"] == "WAIT"
                        for d in evidence["fault_bot"]["decisions"]
                    )
                    assert len(evidence["fault_bot"]["decisions"]) == 1

                    allow_reconnect.set()
                    await asyncio.wait_for(second_reconciled.wait(), 65)
                    evidence["second_reconciled_at"] = barrier["at"].isoformat()
                    evidence["before_new_ticker"] = await observe(database)
                    assert (
                        not evidence["before_new_ticker"]["trade_eligible"]
                        and sockets[1].tickers == 0
                    )
                    allow_second_ticker.set()
                    evidence["recovered"] = await until(
                        database, lambda s: s["trade_eligible"] and s["ticker_source"] == "ws", 25
                    )
                    assert (
                        evidence["recovered"]["ticker_source_time"]
                        > evidence["second_reconciled_at"]
                    )
                    evidence["recovered_api"] = await api_state(
                        client, f"http://127.0.0.1:{api_port}"
                    )
                    await browser_refresh()
                    await browser_assert(
                        "(()=>{const t=document.body.innerText;"
                        "if(!t.includes('Дані актуальні'))throw Error('ui_not_recovered');"
                        "return 'S10O_OK'})()"
                    )
                    evidence["recovered_ui"] = "real_backend_active_pill"
                    await BotService(database, settings.config_version).tick()
                    evidence["recovered_bot"] = await bot_counts(database, running_id)
                    target = evidence["controlled_bot_cursor"]["target_real_final_window"]
                    assert (
                        sum(
                            d["window_start"] == target
                            for d in evidence["recovered_bot"]["decisions"]
                        )
                        == 1
                    )
                    evidence["ws_connections"] = len(sockets)
                    evidence["reconciliations"] = reconciliations
                    evidence["public_rest_operations"] = sum(adapter.requests.values())
                    evidence["public_rest_paths"] = dict(sorted(adapter.requests.items()))
                    evidence["ws_tickers_skipped_after_pause"] = [
                        s.skipped_stale_tickers for s in sockets
                    ]
                    evidence["result"] = "pass"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        evidence["error_message"] = str(exc)[:220]
    finally:
        stop.set()
        allow_reconnect.set()
        allow_second_ticker.set()
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if market_task:
            market_task.cancel()
            await asyncio.gather(market_task, return_exceptions=True)
        if web and web.returncode is None:
            os.killpg(web.pid, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(web.wait(), 6)
            if web.returncode is None:
                os.killpg(web.pid, signal.SIGKILL)
                await web.wait()
        if web_log:
            web_log.close()
        if api_task:
            server.should_exit = True
            with suppress(TimeoutError):
                await asyncio.wait_for(api_task, 8)
            if not api_task.done():
                api_task.cancel()
                await asyncio.gather(api_task, return_exceptions=True)
        if adapter:
            await adapter.close()
        if database:
            await database.close()
        if browser_started and not browser_was_running:
            with suppress(Exception):
                await command(BROWSE, "stop", limit_seconds=8)
        if WEB_DIST.exists():
            shutil.rmtree(WEB_DIST)
        evidence["finished_at"] = utc_now().isoformat()
        evidence["temporary_resources_cleaned"] = True
    path = ROOT / "docs/evidence/public-ui-bot-fault-2026-10-17.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"{evidence['result'].upper()} evidence={path}")
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
