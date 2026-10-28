"""Bounded real-public MEXC final-candle gap/replay proof in disposable SQLite."""

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from ai_futures.bot_models import Bot, BotDecision
from ai_futures.bots import BotCreate, BotService
from ai_futures.config import Settings
from ai_futures.db import Base, Database
from ai_futures.market_worker import MarketIngestion
from ai_futures.mexc import WS_URL, payload_hash
from ai_futures.models import AppState, Candle
from ai_futures.paper import RiskProfile
from ai_futures.paper_models import PaperIntent, PaperOrder
from ai_futures.schemas import utc_now
from ai_futures.strategies import StrategyConfig
from public_ws_fault import CountedPublic, until
from sqlalchemy import func, select
from websockets.asyncio.client import connect

SYMBOL = "BTC_USDT"
STEP = timedelta(minutes=1)
EVIDENCE = Path("docs/evidence/public-gap-replay-2026-10-17.json")


class TrackingPublic(CountedPublic):
    def __init__(self):
        super().__init__()
        self.target_start = None
        self.replayed = []

    async def candles(self, symbol, timeframe, server_time, start=None, end=None):
        rows = await super().candles(symbol, timeframe, server_time, start, end)
        if timeframe == "1m" and self.target_start is not None:
            self.replayed.extend(
                {"window_start": window.isoformat(), "payload_hash": payload_hash(payload)}
                for window, payload, final in rows
                if window == self.target_start and final
            )
        return rows


async def counts(database, target, bot_id):
    async with database.sessions() as session:
        target_rows = (
            await session.scalars(
                select(Candle).where(
                    Candle.symbol == SYMBOL,
                    Candle.timeframe == "1m",
                    Candle.window_start == target,
                    Candle.final,
                )
            )
        ).all()
        decisions = (
            await session.scalars(select(BotDecision).where(BotDecision.bot_id == bot_id))
        ).all()
        all_final = (await session.scalars(select(Candle).where(Candle.final))).all()
        return {
            "target_final_rows": len(target_rows),
            "target_event_id": str(target_rows[0].event_id) if target_rows else None,
            "target_payload_hash": target_rows[0].payload_hash if target_rows else None,
            "target_source": target_rows[0].source if target_rows else None,
            "final_rows": len(all_final),
            "unique_final_keys": len(
                {(row.symbol, row.timeframe, row.window_start) for row in all_final}
            ),
            "decisions": [
                {
                    "id": str(row.id),
                    "window_start": row.window_start.isoformat(),
                    "action": row.action,
                    "reason": row.reason,
                    "intent_id": str(row.intent_id) if row.intent_id else None,
                }
                for row in decisions
            ],
            "paper_intents": await session.scalar(select(func.count()).select_from(PaperIntent)),
            "paper_orders": await session.scalar(select(func.count()).select_from(PaperOrder)),
        }


async def window_for_gap(database):
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(Candle)
                .where(Candle.symbol == SYMBOL, Candle.timeframe == "1m", Candle.final)
                .order_by(Candle.window_start.desc())
                .limit(3)
            )
        ).all()
    assert len(rows) == 3
    newest, previous, before_previous = rows
    assert newest.source == previous.source == before_previous.source == "rest"
    assert newest.window_start - previous.window_start == STEP
    assert previous.window_start - before_previous.window_start == STEP
    assert 0 <= (utc_now() - (newest.window_start + STEP)).total_seconds() < 90
    return newest, previous, before_previous


async def run():
    evidence = {
        "started_at": utc_now().isoformat(),
        "scope": "disposable_sqlite_real_public_ws_rest_test_only_gap",
        "symbol": SYMBOL,
        "max_seconds": 170,
        "max_public_rest_operations": 60,
        "private_requests": 0,
        "telegram_sends": 0,
        "active_compose_mutations": 0,
        "result": "blocked_or_failed",
    }
    async with asyncio.timeout(170):
        with tempfile.TemporaryDirectory(prefix="public-gap-replay-") as directory:
            settings = Settings(
                _env_file=None,
                app_env="test",
                app_mode="paper",
                live_trading_enabled=False,
                backend_auth_token="x" * 48,
                database_url=f"sqlite+aiosqlite:///{directory}/gap.db",
                ai_enabled=False,
                openai_api_key=None,
                telegram_bot_token=None,
                telegram_chat_id=None,
                telegram_delivery_enabled=False,
            )
            database = Database(settings)
            adapter = TrackingPublic()
            stop = asyncio.Event()
            sockets = []

            @asynccontextmanager
            async def connector(url, **kwargs):
                assert url == WS_URL
                if sockets:
                    raise RuntimeError("WS connection bound exceeded")
                async with connect(url, **kwargs) as socket:
                    sockets.append(socket)
                    yield socket

            worker = MarketIngestion(database, settings, adapter, connector)
            original_reconcile = worker.reconcile
            reconciliations = 0

            async def tracked_reconcile():
                nonlocal reconciliations
                await original_reconcile()
                reconciliations += 1

            worker.reconcile = tracked_reconcile
            task = None
            try:
                async with database.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with database.sessions.begin() as session:
                    session.add(AppState(config_version=settings.config_version, kill_switch=False))
                task = asyncio.create_task(worker.websocket(stop))
                evidence["initial_market"] = await until(
                    database,
                    lambda state: state["trade_eligible"] and state["ticker_source"] == "ws",
                    75,
                )

                # Stay inside one final-candle interval during the gap/repair proof.
                second = utc_now().second
                if second < 6:
                    await asyncio.sleep(6 - second)
                elif second > 35:
                    await asyncio.sleep(66 - second)
                server = worker.server_now()
                fresh_rows = await adapter.candles(
                    SYMBOL,
                    "1m",
                    server,
                    int((server - timedelta(minutes=4)).timestamp()),
                    int(server.timestamp()),
                )
                await worker.store.candles("1m", fresh_rows, worker.server_now(), "rest")
                target, skipped, cursor = await window_for_gap(database)
                evidence["real_market_candle"] = {
                    "target_window": target.window_start.isoformat(),
                    "target_event_id_before_test_delete": str(target.event_id),
                    "target_payload_hash": target.payload_hash,
                    "target_source": target.source,
                    "target_source_time": target.source_time.isoformat(),
                    "skipped_consumer_window": skipped.window_start.isoformat(),
                    "consumer_cursor_window": cursor.window_start.isoformat(),
                    "rest_payload": "real_public_MEXC_final_1m",
                }
                target_window = target.window_start
                target_hash = target.payload_hash

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
                created = await bots.create(
                    BotCreate(
                        name="S10P gap replay",
                        deposit="1000",
                        risk_profile=RiskProfile(),
                        config=config,
                    )
                )
                bot_id = UUID(created["id"])
                await bots.transition(bot_id, "ready")
                await bots.transition(bot_id, "start")
                evidence["before_gap"] = await counts(database, target_window, bot_id)
                assert evidence["before_gap"]["target_final_rows"] == 1

                # Test-only loss: delete a real final row from this disposable database.
                # Consumer cursor independently skips the prior real final window.
                async with database.sessions.begin() as session:
                    row = await session.get(Candle, (SYMBOL, "1m", target_window))
                    await session.delete(row)
                    bot = await session.get(Bot, bot_id)
                    bot.last_window = cursor.window_start
                evidence["test_only_fault"] = {
                    "deleted_disposable_final_window": target_window.isoformat(),
                    "bot_last_window": cursor.window_start.isoformat(),
                    "consumer_skipped_window": skipped.window_start.isoformat(),
                }
                evidence["after_gap"] = await counts(database, target_window, bot_id)
                assert evidence["after_gap"]["target_final_rows"] == 0
                assert (
                    evidence["after_gap"]["final_rows"] == evidence["before_gap"]["final_rows"] - 1
                )

                adapter.target_start = target_window
                await worker.reconcile()
                evidence["reconciled_at"] = utc_now().isoformat()
                assert len(adapter.replayed) >= 1
                assert all(hit["payload_hash"] == target_hash for hit in adapter.replayed)
                evidence["rest_backfill_target_hits"] = adapter.replayed
                evidence["after_reconciliation"] = await counts(database, target_window, bot_id)
                assert evidence["after_reconciliation"]["target_final_rows"] == 1
                assert evidence["after_reconciliation"]["target_source"] == "rest"
                assert evidence["after_reconciliation"]["target_payload_hash"] == target_hash
                assert (
                    evidence["after_reconciliation"]["final_rows"]
                    == evidence["before_gap"]["final_rows"]
                )
                assert (
                    evidence["after_reconciliation"]["final_rows"]
                    == evidence["after_reconciliation"]["unique_final_keys"]
                )

                restored_id = evidence["after_reconciliation"]["target_event_id"]
                async with database.sessions() as session:
                    restored = await session.get(Candle, (SYMBOL, "1m", target_window))
                    payload, source_time = restored.payload, restored.source_time
                for _ in range(2):
                    await worker.store.candles(
                        "1m", [(target_window, payload, True)], source_time, "rest"
                    )
                evidence["after_duplicate_replay"] = await counts(database, target_window, bot_id)
                assert evidence["after_duplicate_replay"]["target_event_id"] == restored_id
                assert (
                    evidence["after_duplicate_replay"]["final_rows"]
                    == evidence["before_gap"]["final_rows"]
                )

                evidence["recovered_market"] = await until(
                    database,
                    lambda state: state["trade_eligible"] and state["ticker_source"] == "ws",
                    25,
                )
                async with database.sessions() as session:
                    latest = await session.scalar(
                        select(Candle)
                        .where(Candle.symbol == SYMBOL, Candle.timeframe == "1m", Candle.final)
                        .order_by(Candle.window_start.desc())
                        .limit(1)
                    )
                assert latest.window_start == target_window
                await bots.tick()
                evidence["first_bot_tick"] = await counts(database, target_window, bot_id)
                assert len(evidence["first_bot_tick"]["decisions"]) == 1
                decision = evidence["first_bot_tick"]["decisions"][0]
                assert decision["window_start"] == target_window.isoformat()
                assert decision["action"] == "WAIT" and decision["reason"] == "EVENT_GAP"
                assert evidence["first_bot_tick"]["paper_intents"] == 0
                assert evidence["first_bot_tick"]["paper_orders"] == 0

                restarted = BotService(database, settings.config_version)
                await restarted.tick()
                await restarted.tick()
                evidence["after_bot_restart"] = await counts(database, target_window, bot_id)
                assert (
                    evidence["after_bot_restart"]["decisions"]
                    == evidence["first_bot_tick"]["decisions"]
                )
                assert evidence["after_bot_restart"]["paper_intents"] == 0
                assert evidence["after_bot_restart"]["paper_orders"] == 0
                evidence["reconciliations"] = reconciliations
                evidence["ws_connections"] = len(sockets)
                evidence["public_rest_operations"] = sum(adapter.requests.values())
                evidence["public_rest_paths"] = dict(sorted(adapter.requests.items()))
                evidence["result"] = "pass"
            except Exception as exc:
                evidence["error_type"] = type(exc).__name__
                evidence["error_message"] = str(exc)[:200]
            finally:
                stop.set()
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                evidence["finished_at"] = utc_now().isoformat()
                evidence["reconciliations"] = reconciliations
                evidence["ws_connections"] = len(sockets)
                evidence["public_rest_operations"] = sum(adapter.requests.values())
                evidence["public_rest_paths"] = dict(sorted(adapter.requests.items()))
                await adapter.close()
                await database.close()
    evidence["temporary_resources_cleaned"] = True
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"{evidence['result'].upper()} evidence={EVIDENCE}")
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
