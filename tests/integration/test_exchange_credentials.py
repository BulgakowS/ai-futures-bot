import base64
import os
import shutil
import sys
from pathlib import Path
from uuid import UUID

import httpx
from ai_futures.bot_models import Bot
from ai_futures.bots import BotCreate, BotService
from ai_futures.exchange_credentials import BotCredentialSelection, ExchangeCredential
from ai_futures.main import create_app
from ai_futures.paper import RiskProfile
from pydantic import SecretStr
from sqlalchemy import select, text

from tests.integration.test_bots import config
from tests.integration.test_market import market_db  # noqa: F401

AUTH = {"Authorization": "Bearer " + "x" * 48}
KEY = base64.urlsafe_b64encode(b"K" * 32).decode()


def client_for(database, settings):
    app = create_app(settings, database=database)
    app.state.settings = settings
    app.state.database = database
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_credential_vault_multi_record_rotation_revocation_and_bot_binding(market_db):  # noqa: F811
    database, base_settings = market_db
    settings = base_settings.model_copy(update={"credential_master_key": SecretStr(KEY)})
    bots = [
        await BotService(database, settings.config_version).create(
            BotCreate(
                name=f"vault-bot-{i}", deposit="1000", risk_profile=RiskProfile(), config=config()
            )
        )
        for i in range(2)
    ]
    bot_a, bot_b = (UUID(bot["id"]) for bot in bots)
    path = "/api/v1/exchange-credentials"
    async with client_for(database, settings) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path)).headers["cache-control"] == "no-store"
        assert (await client.get(path, headers=AUTH)).json() == []
        assert (
            await client.post(
                path,
                json={
                    "exchange": "binance",
                    "label": "x",
                    "api_key": "abcdefgh",
                    "api_secret": "secret",
                },
                headers=AUTH,
            )
        ).status_code == 422
        records = []
        for suffix in ("A", "B"):
            result = await client.post(
                path,
                json={
                    "exchange": "mexc_futures",
                    "label": f"Account {suffix}",
                    "api_key": f"key-{suffix}-123456",
                    "api_secret": f"secret-{suffix}-private",
                },
                headers=AUTH,
            )
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            assert "secret-" not in result.text and "key-A-123456" not in result.text
            records.append(result.json())
        first, second = records
        assert first["api_key_masked"] == "••••3456"
        assert first["live_allowed"] is False
        listed = await client.get(path, headers=AUTH)
        assert len(listed.json()) == 2
        assert "secret-" not in listed.text
        assert (await client.get(f"{path}/{first['id']}", headers=AUTH)).json()[
            "label"
        ] == "Account A"

        selected_path = f"/api/v1/bots/{bot_a}/live-credential"
        assert (await client.get(selected_path)).status_code == 401
        initial = (await client.get(selected_path, headers=AUTH)).json()
        assert initial["version"] is None and not initial["selection_valid"]
        chosen = await client.put(
            selected_path,
            json={"credential_id": first["id"], "expected_version": None},
            headers=AUTH,
        )
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["version"] == 1 and chosen.json()["selection_valid"]
        assert chosen.json()["live_allowed"] is False
        assert (await client.get(f"/api/v1/bots/{bot_b}/live-credential", headers=AUTH)).json()[
            "version"
        ] is None
        duplicate = await client.put(
            selected_path,
            json={"credential_id": second["id"], "expected_version": None},
            headers=AUTH,
        )
        assert duplicate.status_code == 409

        rotated = await client.put(
            f"{path}/{first['id']}/rotate",
            json={
                "api_key": "rotated-key-9876",
                "api_secret": "rotated-private-secret",
                "expected_version": 1,
            },
            headers=AUTH,
        )
        assert rotated.status_code == 200 and rotated.json()["version"] == 2
        assert not (await client.get(selected_path, headers=AUTH)).json()["selection_valid"]
        reselected = await client.put(
            selected_path, json={"credential_id": first["id"], "expected_version": 1}, headers=AUTH
        )
        assert reselected.status_code == 200 and reselected.json()["version"] == 2
        assert reselected.json()["credential_version"] == 2
        revoked = await client.post(f"{path}/{first['id']}/revoke", headers=AUTH)
        assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
        assert revoked.json()["api_key_masked"] == "••••"
        assert not (await client.get(selected_path, headers=AUTH)).json()["selection_valid"]
        assert (
            await client.put(
                selected_path,
                json={"credential_id": first["id"], "expected_version": 2},
                headers=AUTH,
            )
        ).status_code == 409
        assert (
            await client.put(
                f"{path}/{first['id']}/rotate",
                json={
                    "api_key": "other-12345678",
                    "api_secret": "other-secret",
                    "expected_version": 2,
                },
                headers=AUTH,
            )
        ).status_code == 409
        switched = await client.put(
            selected_path, json={"credential_id": second["id"], "expected_version": 2}, headers=AUTH
        )
        assert switched.status_code == 200 and switched.json()["version"] == 3

        invalid_label = await client.post(
            path,
            json={
                "exchange": "mexc_futures",
                "label": "private-secret",
                "api_key": "abcdefgh",
                "api_secret": "private-secret",
            },
            headers=AUTH,
        )
        assert invalid_label.status_code == 422 and "private-secret" not in invalid_label.text
        invalid_body = await client.post(
            path,
            json={
                "exchange": "mexc_futures",
                "label": "ok",
                "api_key": "abcdefgh",
                "api_secret": None,
            },
            headers=AUTH,
        )
        assert invalid_body.status_code == 422 and "abcdefgh" not in invalid_body.text

    async with database.sessions() as session:
        rows = (
            await session.scalars(select(ExchangeCredential).order_by(ExchangeCredential.label))
        ).all()
        assert len(rows) == 2 and rows[0].ciphertext == b"" and rows[0].nonce == b""
        assert b"secret-B-private" not in rows[1].ciphertext
        assert b"key-B-123456" not in rows[1].ciphertext
        assert rows[1].ciphertext != rows[0].ciphertext
        bindings = (
            await session.scalars(
                select(BotCredentialSelection)
                .where(BotCredentialSelection.bot_id == bot_a)
                .order_by(BotCredentialSelection.version)
            )
        ).all()
        assert [row.version for row in bindings] == [1, 2, 3]
        assert (await session.get(Bot, bot_a)).mode == "paper"


async def test_missing_invalid_and_wrong_master_key_fail_closed(market_db):  # noqa: F811
    database, settings = market_db
    payload = {
        "exchange": "mexc_futures",
        "label": "Main",
        "api_key": "abcdefgh",
        "api_secret": "secret-value",
    }
    async with client_for(database, settings) as client:
        missing = await client.post("/api/v1/exchange-credentials", json=payload, headers=AUTH)
        assert (
            missing.status_code == 503 and missing.json()["code"] == "CREDENTIAL_VAULT_UNAVAILABLE"
        )
        assert "secret-value" not in missing.text
    bad = settings.model_copy(update={"credential_master_key": SecretStr("bad")})
    async with client_for(database, bad) as client:
        assert (
            await client.post("/api/v1/exchange-credentials", json=payload, headers=AUTH)
        ).status_code == 503
    good = settings.model_copy(update={"credential_master_key": SecretStr(KEY)})
    async with client_for(database, good) as client:
        assert (
            await client.post("/api/v1/exchange-credentials", json=payload, headers=AUTH)
        ).status_code == 200
    wrong = settings.model_copy(
        update={"credential_master_key": SecretStr(base64.urlsafe_b64encode(b"W" * 32).decode())}
    )
    async with client_for(database, wrong) as client:
        response = await client.get("/api/v1/exchange-credentials", headers=AUTH)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "secret-value" not in response.text


async def test_vault_migration_upgrade_downgrade(tmp_path):
    import asyncio

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'vault.db'}"
    env = os.environ | {"APP_ENV": "test", "DATABASE_URL": db_url, "BACKEND_AUTH_TOKEN": "x" * 48}
    for command in (("upgrade", "head"), ("downgrade", "82f0d48aa123"), ("upgrade", "head")):
        proc = await asyncio.create_subprocess_exec(
            ".venv/bin/alembic",
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output = await proc.communicate()
        assert proc.returncode == 0, output
    from ai_futures.config import Settings
    from ai_futures.db import Database

    database = Database(
        Settings(_env_file=None, app_env="test", database_url=db_url, backend_auth_token="x" * 48)
    )
    async with database.sessions() as session:
        assert (
            await session.execute(
                text("SELECT name FROM sqlite_master WHERE name='exchange_credential'")
            )
        ).first()
        assert (
            await session.execute(
                text("SELECT name FROM sqlite_master WHERE name='bot_credential_selection'")
            )
        ).first()
        assert (
            await session.execute(
                text("SELECT name FROM sqlite_master WHERE name=:name"),
                {"name": "immutable_bot_credential_selection_update"},
            )
        ).first()
    await database.close()


async def test_local_setup_creates_api_only_key_once(tmp_path):
    import asyncio

    root = Path(__file__).resolve().parents[2]
    shutil.copy(root / ".env.example", tmp_path / ".env.example")
    script = root / "scripts/setup.py"
    prior = None
    for _ in range(2):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr
        assert b"CREDENTIAL_MASTER_KEY=" not in stdout
        current = (tmp_path / ".env.credentials").read_text()
        if prior is not None:
            assert current == prior
        prior = current
    vault = tmp_path / ".env.credentials"
    assert vault.stat().st_mode & 0o777 == 0o600
    content = vault.read_text().strip()
    assert content.startswith("CREDENTIAL_MASTER_KEY=")
    assert len(base64.urlsafe_b64decode(content.partition("=")[2])) == 32
    assert "CREDENTIAL_MASTER_KEY=" not in (tmp_path / ".env").read_text()
