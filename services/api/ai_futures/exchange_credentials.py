"""Encrypted, inert exchange credentials and versioned bot selections."""

import base64
import json
import os
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr
from sqlalchemy import ForeignKey, Integer, LargeBinary, String, Uuid, select
from sqlalchemy.orm import Mapped, mapped_column

from ai_futures.auth import require_backend_token
from ai_futures.bot_models import Bot
from ai_futures.db import Base, UTCDateTimeType
from ai_futures.schemas import Schema, utc_now

SUPPORTED_EXCHANGES = {"mexc_futures"}


class CredentialError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status


class CredentialCreate(Schema):
    exchange: Literal["mexc_futures"]
    label: str = Field(min_length=1, max_length=100)
    api_key: SecretStr = Field(min_length=8)
    api_secret: SecretStr = Field(min_length=1)

    @property
    def safe_label(self) -> str:
        label = self.label.strip()
        if (
            not label
            or any(ord(char) < 32 for char in label)
            or self.api_key.get_secret_value() in label
            or self.api_secret.get_secret_value() in label
        ):
            raise CredentialError("CREDENTIAL_LABEL_INVALID", 422)
        return label


class CredentialRotate(Schema):
    api_key: SecretStr = Field(min_length=8)
    api_secret: SecretStr = Field(min_length=1)
    expected_version: int = Field(ge=1)


class CredentialSelect(Schema):
    credential_id: UUID
    expected_version: int | None = Field(ge=1)


class ExchangeCredential(Base):
    __tablename__ = "exchange_credential"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exchange: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(100))
    key_hint: Mapped[str] = mapped_column(String(4))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    config_version: Mapped[str] = mapped_column(String(128))


class BotCredentialSelection(Base):
    __tablename__ = "bot_credential_selection"

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bot.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("exchange_credential.id"))
    credential_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utc_now)
    config_version: Mapped[str] = mapped_column(String(128))


def _master_key(settings) -> bytes:
    value = settings.credential_master_key
    try:
        encoded = value.get_secret_value() if value else ""
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError
        return key
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise CredentialError("CREDENTIAL_VAULT_UNAVAILABLE", 503) from exc


def _aad(row: ExchangeCredential) -> bytes:
    return f"exchange-credential:v1:{row.id}:{row.exchange}:{row.version}".encode()


def _encrypt(key: bytes, row: ExchangeCredential, api_key: str, api_secret: str) -> None:
    row.nonce = os.urandom(12)
    material = json.dumps({"api_key": api_key, "api_secret": api_secret}).encode()
    row.ciphertext = AESGCM(key).encrypt(row.nonce, material, _aad(row))
    row.key_hint = api_key[-4:]


def _verify(key: bytes, row: ExchangeCredential) -> None:
    try:
        AESGCM(key).decrypt(row.nonce, row.ciphertext, _aad(row))
    except (InvalidTag, ValueError) as exc:
        raise CredentialError("CREDENTIAL_VAULT_UNAVAILABLE", 503) from exc


class CredentialService:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    async def _ready(self, session):
        key = _master_key(self.settings)
        active = await session.scalars(
            select(ExchangeCredential).where(ExchangeCredential.status == "active")
        )
        for existing in active:
            _verify(key, existing)
        return key

    @staticmethod
    def describe(row: ExchangeCredential):
        return {
            "id": str(row.id),
            "exchange": row.exchange,
            "label": row.label,
            "api_key_masked": "••••" + row.key_hint,
            "version": row.version,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "revoked_at": row.revoked_at,
            "config_version": row.config_version,
            "live_allowed": False,
        }

    async def create(self, body: CredentialCreate):
        async with self.database.sessions.begin() as session:
            key = await self._ready(session)
            row = ExchangeCredential(
                id=uuid4(),
                exchange=body.exchange,
                label=body.safe_label,
                version=1,
                config_version=self.settings.config_version,
            )
            _encrypt(key, row, body.api_key.get_secret_value(), body.api_secret.get_secret_value())
            session.add(row)
            await session.flush()
            return self.describe(row)

    async def list(self):
        async with self.database.sessions() as session:
            await self._ready(session)
            rows = (
                await session.scalars(
                    select(ExchangeCredential).order_by(ExchangeCredential.created_at)
                )
            ).all()
            return [self.describe(row) for row in rows]

    async def get(self, credential_id: UUID):
        async with self.database.sessions() as session:
            await self._ready(session)
            row = await session.get(ExchangeCredential, credential_id)
            if row is None:
                raise CredentialError("CREDENTIAL_NOT_FOUND", 404)
            return self.describe(row)

    async def rotate(self, credential_id: UUID, body: CredentialRotate):
        async with self.database.sessions.begin() as session:
            key = await self._ready(session)
            row = await session.scalar(
                select(ExchangeCredential)
                .where(ExchangeCredential.id == credential_id)
                .with_for_update()
            )
            if row is None:
                raise CredentialError("CREDENTIAL_NOT_FOUND", 404)
            if row.status != "active":
                raise CredentialError("CREDENTIAL_REVOKED")
            if row.version != body.expected_version:
                raise CredentialError("CREDENTIAL_VERSION_CONFLICT")
            row.version += 1
            row.updated_at = utc_now()
            row.config_version = self.settings.config_version
            _encrypt(key, row, body.api_key.get_secret_value(), body.api_secret.get_secret_value())
            return self.describe(row)

    async def revoke(self, credential_id: UUID):
        async with self.database.sessions.begin() as session:
            await self._ready(session)
            row = await session.scalar(
                select(ExchangeCredential)
                .where(ExchangeCredential.id == credential_id)
                .with_for_update()
            )
            if row is None:
                raise CredentialError("CREDENTIAL_NOT_FOUND", 404)
            if row.status == "active":
                row.status = "revoked"
                row.revoked_at = row.updated_at = utc_now()
                row.nonce = b""
                row.ciphertext = b""
                row.key_hint = ""
            return self.describe(row)

    @staticmethod
    def describe_selection(bot_id, selected, credential):
        valid = bool(
            selected
            and credential
            and credential.status == "active"
            and credential.exchange in SUPPORTED_EXCHANGES
            and credential.version == selected.credential_version
        )
        return {
            "bot_id": str(bot_id),
            "version": selected.version if selected else None,
            "credential_id": str(selected.credential_id) if selected else None,
            "credential_version": selected.credential_version if selected else None,
            "credential": CredentialService.describe(credential) if credential else None,
            "selection_valid": valid,
            "reason": "LIVE_DISABLED" if valid else "CREDENTIAL_SELECTION_INVALID",
            "created_at": selected.created_at if selected else None,
            "config_version": selected.config_version if selected else None,
            "live_allowed": False,
        }

    async def selection(self, bot_id: UUID):
        async with self.database.sessions() as session:
            await self._ready(session)
            if await session.get(Bot, bot_id) is None:
                raise CredentialError("BOT_NOT_FOUND", 404)
            selected = await session.scalar(
                select(BotCredentialSelection)
                .where(BotCredentialSelection.bot_id == bot_id)
                .order_by(BotCredentialSelection.version.desc())
                .limit(1)
            )
            credential = (
                await session.get(ExchangeCredential, selected.credential_id) if selected else None
            )
            return self.describe_selection(bot_id, selected, credential)

    async def select(self, bot_id: UUID, body: CredentialSelect):
        async with self.database.sessions.begin() as session:
            await self._ready(session)
            bot = await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
            if bot is None:
                raise CredentialError("BOT_NOT_FOUND", 404)
            previous = await session.scalar(
                select(BotCredentialSelection)
                .where(BotCredentialSelection.bot_id == bot_id)
                .order_by(BotCredentialSelection.version.desc())
                .limit(1)
            )
            if (previous.version if previous else None) != body.expected_version:
                raise CredentialError("CREDENTIAL_SELECTION_VERSION_CONFLICT")
            credential = await session.scalar(
                select(ExchangeCredential)
                .where(ExchangeCredential.id == body.credential_id)
                .with_for_update()
            )
            if (
                credential is None
                or credential.status != "active"
                or credential.exchange not in SUPPORTED_EXCHANGES
            ):
                raise CredentialError("CREDENTIAL_NOT_SELECTABLE")
            _verify(_master_key(self.settings), credential)
            selected = BotCredentialSelection(
                bot_id=bot_id,
                version=(previous.version + 1 if previous else 1),
                credential_id=credential.id,
                credential_version=credential.version,
                config_version=self.settings.config_version,
            )
            session.add(selected)
            await session.flush()
            return self.describe_selection(bot_id, selected, credential)


def register_credential_routes(app):
    def service():
        return CredentialService(app.state.database, app.state.settings)

    @app.exception_handler(CredentialError)
    async def credential_error(request: Request, exc: CredentialError):
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": exc.code,
                "message": exc.code,
                "details": {},
                "request_id": str(request.state.request_id),
                "retryable": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/exchange-credentials", dependencies=[Depends(require_backend_token)])
    async def list_credentials():
        return await service().list()

    @app.post("/api/v1/exchange-credentials")
    async def create_credential(body: CredentialCreate):
        return await service().create(body)

    @app.get(
        "/api/v1/exchange-credentials/{credential_id}",
        dependencies=[Depends(require_backend_token)],
    )
    async def get_credential(credential_id: UUID):
        return await service().get(credential_id)

    @app.put("/api/v1/exchange-credentials/{credential_id}/rotate")
    async def rotate_credential(credential_id: UUID, body: CredentialRotate):
        return await service().rotate(credential_id, body)

    @app.post("/api/v1/exchange-credentials/{credential_id}/revoke")
    async def revoke_credential(credential_id: UUID):
        return await service().revoke(credential_id)

    @app.get("/api/v1/bots/{bot_id}/live-credential", dependencies=[Depends(require_backend_token)])
    async def get_bot_credential(bot_id: UUID):
        return await service().selection(bot_id)

    @app.put("/api/v1/bots/{bot_id}/live-credential")
    async def select_bot_credential(bot_id: UUID, body: CredentialSelect):
        return await service().select(bot_id, body)
