"""Read-only market import and isolated historical simulations."""

from datetime import datetime
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from ai_futures.backtest_models import BacktestDataset, BacktestRun
from ai_futures.backtesting import compare, digest, make_dataset, replay
from ai_futures.models import Candle, InstrumentRules, MarketSnapshot
from ai_futures.paper import RiskProfile
from ai_futures.schemas import ExactDecimal, Schema, UTCDateTime
from ai_futures.strategies import STEP, StrategyConfig


class ImportRequest(Schema):
    symbol: str = "BTC_USDT"
    timeframe: str = "1m"
    start_utc: UTCDateTime
    end_utc: UTCDateTime


class RunRequest(Schema):
    config: StrategyConfig
    risk_profile: RiskProfile = Field(default_factory=RiskProfile)
    deposit: ExactDecimal = Field(gt=0, le=1000000000)
    seed: int = 0


class CompareRequest(Schema):
    run_ids: list[UUID] = Field(min_length=2, max_length=20)


def _error(code, status=409):
    return JSONResponse(status_code=status, content={"code": code})


def register_backtest_routes(app):
    @app.post("/api/v1/backtests/datasets")
    async def import_dataset(body: ImportRequest, request: Request):
        if (
            body.symbol != "BTC_USDT"
            or body.timeframe not in STEP
            or body.start_utc >= body.end_utc
        ):
            return _error("INVALID_RANGE", 422)
        async with request.app.state.database.sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(Candle)
                    .where(
                        Candle.symbol == body.symbol,
                        Candle.timeframe == body.timeframe,
                        Candle.window_start >= body.start_utc,
                        Candle.window_start < body.end_utc,
                    )
                    .order_by(Candle.window_start)
                    .limit(2001)
                )
            ).all()
            if len(rows) < 2 or len(rows) > 2000:
                return _error("DATASET_SIZE", 422)
            rules = await session.get(InstrumentRules, body.symbol)
            if rules is None or not rules.validated:
                return _error("RULES_UNKNOWN", 422)
            history = await session.get(MarketSnapshot, (body.symbol, "funding_history"))
            funding = []
            if history:
                closes = {row.window_start + STEP[body.timeframe] for row in rows}
                funding = [
                    entry
                    for entry in history.payload.get("settlements", [])
                    if datetime.fromisoformat(entry["settleTime"]) in closes
                ]
                funding.sort(key=lambda x: x["settleTime"])
            try:
                payload = make_dataset(
                    rows,
                    rules.payload,
                    symbol=body.symbol,
                    timeframe=body.timeframe,
                    rules_version=rules.version,
                    funding=funding,
                )
            except (KeyError, ValueError, ArithmeticError):
                return _error("INVALID_SOURCE_DATA", 422)
            prior = await session.scalar(
                select(BacktestDataset).where(BacktestDataset.checksum == payload["checksum"])
            )
            if prior:
                return {
                    "id": str(prior.id),
                    "checksum": prior.checksum,
                    "start_utc": payload["start_utc"],
                    "end_utc": payload["end_utc"],
                    "gaps": payload["gaps"],
                    "funding_coverage": payload["funding_coverage"],
                }
            dataset = BacktestDataset(
                checksum=payload["checksum"],
                symbol=body.symbol,
                timeframe=body.timeframe,
                source=payload["source"],
                payload=payload,
                config_version=request.app.state.settings.config_version,
            )
            session.add(dataset)
            await session.flush()
            return {
                "id": str(dataset.id),
                "checksum": dataset.checksum,
                "start_utc": payload["start_utc"],
                "end_utc": payload["end_utc"],
                "gaps": payload["gaps"],
                "funding_coverage": payload["funding_coverage"],
            }

    @app.get("/api/v1/backtests/datasets/{dataset_id}")
    async def get_dataset(dataset_id: UUID, request: Request):
        async with request.app.state.database.sessions() as session:
            row = await session.get(BacktestDataset, dataset_id)
            if row is None:
                return _error("DATASET_NOT_FOUND", 404)
            return {"id": str(row.id), **row.payload}

    @app.post("/api/v1/backtests/datasets/{dataset_id}/runs")
    async def run_backtest(dataset_id: UUID, body: RunRequest, request: Request):
        async with request.app.state.database.sessions.begin() as session:
            dataset = await session.get(BacktestDataset, dataset_id)
            if dataset is None:
                return _error("DATASET_NOT_FOUND", 404)
            try:
                report = replay(
                    dataset.payload,
                    body.config,
                    body.risk_profile,
                    body.deposit,
                    seed=body.seed,
                    config_version=request.app.state.settings.config_version,
                )
            except (KeyError, ValueError, ArithmeticError):
                return _error("INVALID_BACKTEST_INPUT", 422)
            row = BacktestRun(
                dataset_id=dataset_id,
                report=report,
                report_checksum=digest(report),
                config_version=request.app.state.settings.config_version,
            )
            session.add(row)
            await session.flush()
            return {
                "id": str(row.id),
                "dataset_id": str(dataset_id),
                "report_checksum": row.report_checksum,
                "report": report,
            }

    @app.get("/api/v1/backtests/runs/{run_id}")
    async def get_run(run_id: UUID, request: Request):
        async with request.app.state.database.sessions() as session:
            row = await session.get(BacktestRun, run_id)
            if row is None:
                return _error("RUN_NOT_FOUND", 404)
            return {
                "id": str(row.id),
                "dataset_id": str(row.dataset_id),
                "report_checksum": row.report_checksum,
                "report": row.report,
            }

    @app.post("/api/v1/backtests/compare")
    async def compare_runs(body: CompareRequest, request: Request):
        async with request.app.state.database.sessions() as session:
            rows = [await session.get(BacktestRun, run_id) for run_id in body.run_ids]
            if any(row is None for row in rows):
                return _error("RUN_NOT_FOUND", 404)
            try:
                result = compare([row.report for row in rows])
            except ValueError:
                return _error("COMPARISON_MISMATCH", 422)
            return {"run_ids": [str(run_id) for run_id in body.run_ids], **result}
