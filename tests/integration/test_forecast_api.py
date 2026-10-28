"""Authenticated GET-only N4b API on a disposable forecast journal."""

from dataclasses import replace
from uuid import uuid4

from ai_futures.forecast_journal import ForecastJournal
from ai_futures.main import create_app
from ai_futures.paper_models import PaperIntent, PaperOrder
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.integration.test_forecast_journal import ANCHOR, forecast_db, outcome  # noqa: F401


class RedisStub:
    async def aclose(self):
        pass


async def test_forecast_get_auth_empty_list_latest_detail_and_no_paper(forecast_db):  # noqa: F811
    database, settings = forecast_db
    app = create_app(settings, database, RedisStub())
    headers = {"Authorization": f"Bearer {settings.backend_auth_token.get_secret_value()}"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/v1/research/forecasts"
            for suffix in ("", "/latest", f"/{uuid4()}"):
                assert (await client.get(path + suffix)).status_code == 401
                assert (
                    await client.get(path + suffix, headers={"Authorization": "Bearer wrong"})
                ).status_code == 401
            empty = (await client.get(path, headers=headers)).json()
            assert empty == {"mode": "collect-only", "trading_enabled": False, "items": []}
            assert (await client.get(path + "/latest", headers=headers)).json()["forecast"] is None
            assert (
                await client.get(path, params={"limit": 51}, headers=headers)
            ).status_code == 422

            wait = await ForecastJournal(database, settings).append(outcome())
            candidate = outcome(candidate=True)
            candidate = replace(
                candidate,
                context=replace(
                    candidate.context,
                    anchor=ANCHOR.replace(hour=16),
                    observed_at=ANCHOR.replace(hour=16),
                ),
            )
            approved = await ForecastJournal(database, settings).append(candidate)
            listed = (await client.get(path, headers=headers)).json()
            assert [row["forecast_id"] for row in listed["items"]] == [
                str(approved.forecast_id),
                str(wait.forecast_id),
            ]
            assert listed["trading_enabled"] is False
            latest = (await client.get(path + "/latest", headers=headers)).json()["forecast"]
            assert latest["forecast_id"] == str(approved.forecast_id)
            assert latest["correlation_id"] == str(approved.correlation_id)
            assert latest["anchor"].endswith("+00:00")
            assert latest["created_at"].endswith("+00:00")
            assert latest["horizon_hours"] == 4 and latest["test_fixture"] is True
            assert latest["probabilities"] == {"DOWN": "0.01", "NEUTRAL": "0.01", "UP": "0.98"}
            assert latest["proposal"]["proposal_id"] == str(approved.proposal_id)
            assert latest["model_version"] and latest["dataset_checksum"]
            assert latest["config_checksum"] and latest["source_versions"]
            assert latest["market_event_ids"] and latest["news_version_ids"]
            detail = (await client.get(path + f"/{wait.forecast_id}", headers=headers)).json()[
                "forecast"
            ]
            assert detail["direction"] == "WAIT" and detail["proposal"] is None
            assert detail["reason"] == "news_coverage_missing"
            assert (await client.get(path + f"/{uuid4()}", headers=headers)).status_code == 404
            assert (await client.post(path, headers=headers, json={})).status_code == 405
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
