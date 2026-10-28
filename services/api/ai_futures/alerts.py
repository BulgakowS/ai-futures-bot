"""Persist paper incidents and optionally deliver fixed Telegram messages."""

import asyncio
import logging
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ai_futures.market import market_quality
from ai_futures.models import AlertState, MarketSnapshot, OutboxEvent, WorkerHeartbeat
from ai_futures.schemas import utc_now

logger = logging.getLogger("ai_futures.alerts")
TOPIC = "alert.telegram"
RECOVERY_STABLE_SECONDS = 30
ACTIVATION_CONFIRM_SECONDS = {"market_stale": 15, "worker_down": 10}
MESSAGES = {
    "kill_switch": "AI Futures paper: kill switch activated. New entries are blocked.",
    "market_stale": "AI Futures paper: market data stale. New entries are blocked.",
    "worker_down": "AI Futures paper: trading worker unavailable or degraded. Check readiness.",
}


async def observe(session, kind: str, subject: str, active: bool, config_version: str):
    """One outbox row per incident transition, committed with its persistent state."""
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    await session.execute(
        insert(AlertState)
        .values(kind=kind, subject=subject, active=False, changed_at=utc_now())
        .on_conflict_do_nothing(index_elements=["kind", "subject"])
    )
    row = await session.scalar(
        select(AlertState)
        .where(AlertState.kind == kind, AlertState.subject == subject)
        .with_for_update()
    )
    now = utc_now()
    if active:
        row.recovered_at = None
        if row.active:
            return False
        if row.pending_at is None:
            row.pending_at = now
            return False if ACTIVATION_CONFIRM_SECONDS.get(kind, 0) else _activate(
                session, row, now, config_version
            )
        if now - row.pending_at < timedelta(seconds=ACTIVATION_CONFIRM_SECONDS.get(kind, 0)):
            return False
    else:
        if not row.active:
            row.pending_at = None
            return False
        if row.recovered_at is None:
            row.recovered_at = now
            return False
        if now - row.recovered_at < timedelta(seconds=RECOVERY_STABLE_SECONDS):
            return False
    row.active, row.changed_at, row.recovered_at, row.pending_at = active, now, None, None
    if active:
        _enqueue(session, kind, subject, config_version)
    return True


def _enqueue(session, kind, subject, config_version):
    session.add(OutboxEvent(
        correlation_id=uuid4(), config_version=config_version, topic=TOPIC,
        payload={"kind": kind, "subject": subject},
    ))


def _activate(session, row, now, config_version):
    row.active, row.changed_at, row.pending_at = True, now, None
    _enqueue(session, row.kind, row.subject, config_version)
    return True


async def monitor_once(database, settings):
    now = utc_now()
    async with database.sessions.begin() as session:
        heartbeat = await session.get(WorkerHeartbeat, "trading")
        down = heartbeat is None or heartbeat.status != "idle" or not 0 <= (
            now - heartbeat.occurred_at
        ).total_seconds() < settings.worker_stale_seconds
        await observe(session, "worker_down", "trading", down, settings.config_version)
        quality = await session.get(MarketSnapshot, (settings.market_symbol, "quality"))
        if quality is not None:
            status = await market_quality(session, settings.market_symbol, now)
            await observe(
                session,
                "market_stale",
                settings.market_symbol,
                not status["trade_eligible"],
                settings.config_version,
            )


class TelegramSender:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient | None = None):
        # httpx logs request URLs at INFO; Bot API URLs contain the token.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.AsyncClient(timeout=5)
        self.owns_client = client is None

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def send(self, kind: str, subject: str) -> bool:
        if kind not in MESSAGES or len(subject) > 64:
            return False
        # Only fixed text reaches Telegram. No payload, account details or secrets are interpolated.
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": MESSAGES[kind]},
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return body.get("ok") is True and isinstance(body.get("result"), dict)


async def deliver_once(database, sender: TelegramSender, cutoff_utc: datetime):
    if (
        not isinstance(cutoff_utc, datetime)
        or cutoff_utc.tzinfo is None
        or cutoff_utc.utcoffset() != timedelta(0)
        or cutoff_utc > utc_now()
    ):
        raise ValueError("Telegram delivery requires a nonfuture UTC cutoff")
    delivered = 0
    for _ in range(10):
        async with database.sessions.begin() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.topic == TOPIC,
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.occurred_at >= cutoff_utc,
                )
                .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                break
            try:
                sent = await sender.send(event.payload["kind"], event.payload["subject"])
            except Exception:
                # Transport errors can contain the token-bearing URL. Never log exception text.
                logger.warning("telegram_delivery_failed event_id=%s", event.event_id)
                break
            if not sent:
                logger.warning("telegram_delivery_rejected event_id=%s", event.event_id)
                break
            event.published_at = utc_now()
            delivered += 1
    return delivered


async def run_alerts(stop: asyncio.Event, database, settings):
    sender = None
    if settings.telegram_delivery_enabled:
        sender = TelegramSender(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    try:
        while not stop.is_set():
            try:
                async with asyncio.timeout(5):
                    await monitor_once(database, settings)
                if sender:
                    await deliver_once(database, sender, settings.telegram_delivery_cutoff_utc)
            except Exception:
                logger.warning("alert_cycle_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
    finally:
        if sender:
            await sender.close()
