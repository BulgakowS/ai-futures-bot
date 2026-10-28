"""Loopback disposable synthetic DB/API fixture for N4d browser read-only proof."""

import argparse
import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import uvicorn
from ai_futures.config import Settings
from ai_futures.db import Base, Database
from ai_futures.main import create_app
from ai_futures.model_bot import ModelBotService
from ai_futures.models import AppState
from ai_futures.paper import PaperService, PortfolioCreate

from tests.integration.test_model_bot import setup
from tests.integration.test_paper import feed
from tests.unit.test_news_forecast import CONFIG, at


async def run(port: int) -> None:
    with tempfile.TemporaryDirectory(prefix="s11-n4d-ui-") as directory:
        db_path = Path(directory) / "fixture.db"
        settings = Settings(
            _env_file=None,
            app_env="test",
            backend_auth_token="x" * 48,
            database_url=f"sqlite+aiosqlite:///{db_path}",
            forecast_fixture_enabled=True,
            model_paper_fixture_enabled=True,
            model_bot_fixture_enabled=True,
        )
        database = Database(settings)
        try:
            async with database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with database.sessions.begin() as session:
                session.add(AppState(config_version=settings.config_version, kill_switch=False))
            await feed(database, settings, price="101.1")
            paper = PaperService(database, settings.config_version)
            portfolio = await paper.create(PortfolioCreate())
            enabled, args = await setup(database, settings)
            result = await ModelBotService(database, enabled).tick(
                args[0],
                UUID(portfolio["id"]),
                *args[1:],
                config=CONFIG,
                now=at(12) + timedelta(minutes=1),
            )
            if result.status != "submitted":
                raise RuntimeError(f"Synthetic fixture did not submit: {result.reason}")
            print(f"SYNTHETIC_READY port={port} forecast_id={result.forecast_id}", flush=True)
            app = create_app(enabled, database)
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            ).serve()
        finally:
            await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8137)
    arguments = parser.parse_args()
    if not 1024 <= arguments.port <= 65535:
        parser.error("port must be unprivileged")
    asyncio.run(run(arguments.port))
