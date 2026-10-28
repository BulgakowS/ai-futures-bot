from datetime import timedelta

from ai_futures.backtest_models import BacktestDataset, BacktestRun
from ai_futures.main import create_app
from ai_futures.models import Candle, InstrumentRules
from ai_futures.paper_models import PaperOrder, PaperPortfolio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.integration.test_market import market_db  # noqa: F401
from tests.unit.test_backtesting import RULES, START, candles, config


class RedisStub:
    async def aclose(self):
        pass


async def test_api_import_run_compare_and_paper_mode_isolation(market_db):  # noqa: F811
    db, settings = market_db
    source = candles([98, 98, 97, 100, 101, 101, 98, 97])
    async with db.sessions.begin() as session:
        session.add(
            InstrumentRules(
                symbol="BTC_USDT",
                exchange="mexc",
                validated=True,
                version="synthetic-rules-v1",
                payload=RULES,
                received_at=START,
                config_version=settings.config_version,
            )
        )
        session.add(
            PaperPortfolio(
                name="untouched", config_version=settings.config_version, risk_profile={}
            )
        )
        for row in source:
            session.add(Candle(**vars(row), config_version=settings.config_version))
    app = create_app(settings, db, RedisStub())
    headers = {"Authorization": f"Bearer {settings.backend_auth_token.get_secret_value()}"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            imported = await client.post(
                "/api/v1/backtests/datasets",
                json={
                    "symbol": "BTC_USDT",
                    "timeframe": "1m",
                    "start_utc": START.isoformat(),
                    "end_utc": (START + timedelta(minutes=8)).isoformat(),
                },
                headers=headers,
            )
            assert imported.status_code == 200, imported.text
            dataset_id = imported.json()["id"]
            assert imported.json()["gaps"] == []
            assert (await client.get(f"/api/v1/backtests/datasets/{dataset_id}")).status_code == 200
            body = {"config": config().model_dump(mode="json"), "deposit": "1000", "seed": 1}
            first = await client.post(
                f"/api/v1/backtests/datasets/{dataset_id}/runs", json=body, headers=headers
            )
            second = await client.post(
                f"/api/v1/backtests/datasets/{dataset_id}/runs", json=body, headers=headers
            )
            assert first.status_code == second.status_code == 200, first.text
            assert first.json()["report_checksum"] == second.json()["report_checksum"]
            assert first.json()["report"]["account"]["mode"] == "backtest"
            compared = await client.post(
                "/api/v1/backtests/compare",
                json={"run_ids": [first.json()["id"], second.json()["id"]]},
                headers=headers,
            )
            assert compared.status_code == 200, compared.text
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BacktestDataset)) == 1
        assert await session.scalar(select(func.count()).select_from(BacktestRun)) == 2
        assert await session.scalar(select(func.count()).select_from(PaperPortfolio)) == 1
        assert await session.scalar(select(func.count()).select_from(PaperOrder)) == 0
