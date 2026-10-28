from datetime import UTC, datetime
from uuid import UUID

import httpx
from ai_futures.bot_models import Bot, BotConfig
from ai_futures.bots import BotCreate, BotService
from ai_futures.live_pilot_settings import LivePilotBotConfig, LivePilotSettingsService
from ai_futures.main import create_app
from ai_futures.paper import RiskProfile
from ai_futures.paper_models import PaperPortfolio
from sqlalchemy import func, select

from tests.integration.test_bots import config
from tests.integration.test_market import market_db  # noqa: F401


def draft():
    return {
        "max_pairs": 3,
        "per_pair_notional": {"unit": "USDT", "value": "125.50"},
        "aggregate_notional": {"unit": "USDT", "value": "300"},
        "max_leverage": "2",
        "take_profit_pct": "4.25",
        "stop_loss_pct": "1.5",
        "max_daily_loss": {"unit": "PCT_EQUITY", "value": "5"},
        "max_round_trip_cost_pct": "0.8",
    }


async def test_per_bot_draft_api_validation_persistence_and_paper_isolation(market_db):  # noqa: F811
    database, settings = market_db
    paper = BotService(database, settings.config_version)
    bots = [
        await paper.create(
            BotCreate(name=f"bot-{i}", deposit="1000", risk_profile=RiskProfile(), config=config())
        )
        for i in range(2)
    ]
    first_id, second_id = (UUID(bot["id"]) for bot in bots)
    app = create_app(settings, database=database)
    app.state.settings = settings
    app.state.database = database
    path = f"/api/v1/bots/{first_id}/live-pilot-settings"
    other_path = f"/api/v1/bots/{second_id}/live-pilot-settings"
    auth = {"Authorization": "Bearer " + "x" * 48}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = (await client.get(path)).json()
        assert missing["draft"] is None and missing["version"] is None
        assert missing["reason"] == "PILOT_CONFIG_MISSING" and not missing["live_allowed"]
        assert (await client.put(path, json=draft())).status_code == 401

        for invalid in (
            {**draft(), "max_pairs": 4},
            {**draft(), "max_pairs": 2.5},
            {**draft(), "stop_loss_pct": "0"},
            {**draft(), "max_leverage": 2.0},
            {**draft(), "max_leverage": None},
            {**draft(), "take_profit_pct": "invalid"},
            {**draft(), "max_daily_loss": {"unit": "PCT_EQUITY", "value": "100"}},
            {**draft(), "aggregate_notional": {"unit": "USDT", "value": "100"}},
            {**draft(), "aggregate_notional": {"unit": "PCT_EQUITY", "value": "100"}},
            {key: value for key, value in draft().items() if key != "max_daily_loss"},
            {key: value for key, value in draft().items() if key != "max_round_trip_cost_pct"},
            {**draft(), "max_round_trip_cost_pct": "0"},
            {**draft(), "max_round_trip_cost_pct": 0.8},
        ):
            response = await client.put(path, json=invalid, headers=auth)
            assert response.status_code == 422, response.text
        assert (await client.get(path)).json()["draft"] is None

        saved = await client.put(path, json=draft(), headers=auth)
        assert saved.status_code == 200, saved.text
        first = saved.json()
        assert first["version"] == 1 and first["draft"] == draft()
        assert first["draft"]["max_daily_loss"]["value"] == "5"  # 5%, never 0.05.
        assert first["draft"]["max_round_trip_cost_pct"] == "0.8"
        assert datetime.fromisoformat(first["created_at"]).utcoffset() == UTC.utcoffset(None)
        assert first["config_version"] == settings.config_version
        assert first["reason"] == "LIVE_DISABLED" and not first["live_allowed"]
        assert (await client.get(other_path)).json()["reason"] == "PILOT_CONFIG_MISSING"

        updated = {
            **draft(),
            "max_pairs": 2,
            "aggregate_notional": {"unit": "USDT", "value": "250"},
        }
        second = (await client.put(path, json=updated, headers=auth)).json()
        assert second["version"] == 2 and second["draft"] == updated
        assert (await client.get(path)).json()["draft"] == updated

    restarted = LivePilotSettingsService(database, settings.config_version)
    assert (await restarted.get(first_id))["version"] == 2
    assert (await restarted.get(second_id))["draft"] is None
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(LivePilotBotConfig)
                .where(LivePilotBotConfig.bot_id == first_id)
                .order_by(LivePilotBotConfig.version)
            )
        ).all()
        assert [row.version for row in rows] == [1, 2]
        assert rows[0].payload == draft() and rows[1].payload == updated
        assert await session.scalar(select(func.count()).select_from(BotConfig)) == 2
        assert (await session.get(Bot, first_id)).mode == "paper"
        portfolio = await session.get(PaperPortfolio, UUID(bots[0]["portfolio_id"]))
        assert portfolio.risk_profile == RiskProfile().model_dump(mode="json")
