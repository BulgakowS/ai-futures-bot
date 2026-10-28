"""Authenticated read-only research views of the offline forecast journal."""

from uuid import UUID

from fastapi import HTTPException, Query, Request
from sqlalchemy import select

from ai_futures.auth import require_backend_token
from ai_futures.forecast_models import ForecastDecision, ForecastProposal


def _view(row: ForecastDecision, proposal: ForecastProposal | None) -> dict:
    return {
        "forecast_id": str(row.forecast_id),
        "correlation_id": str(row.correlation_id),
        "symbol": row.symbol,
        "anchor": row.anchor.isoformat(),
        "created_at": row.created_at.isoformat(),
        "horizon_hours": row.horizon_hours,
        "status": row.status,
        "direction": row.direction,
        "reason": row.reason,
        "quality": row.quality,
        "probabilities": row.probabilities,
        "model_version": row.model_version,
        "artifact_checksum": row.artifact_checksum,
        "dataset_checksum": row.dataset_checksum,
        "config_checksum": row.config_checksum,
        "source_versions": row.source_versions,
        "market_event_ids": row.market_event_ids,
        "news_version_ids": row.news_version_ids,
        "feature_hash": row.feature_hash,
        "context_hash": row.context_hash,
        "decision_hash": row.decision_hash,
        "approval_version": row.approval_version,
        "test_fixture": row.approval_version == "synthetic-approval-v1",
        "proposal": (
            {
                "proposal_id": str(proposal.proposal_id),
                "direction": proposal.direction,
                "entry": proposal.entry,
                "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit,
                "rules_version": proposal.rules_version,
                "policy_version": proposal.policy_version,
                "proposal_hash": proposal.proposal_hash,
                "created_at": proposal.created_at.isoformat(),
            }
            if proposal
            else None
        ),
    }


async def _views(session, rows: list[ForecastDecision]) -> list[dict]:
    if not rows:
        return []
    proposals = (
        await session.scalars(
            select(ForecastProposal).where(
                ForecastProposal.forecast_id.in_([row.forecast_id for row in rows])
            )
        )
    ).all()
    by_forecast = {proposal.forecast_id: proposal for proposal in proposals}
    return [_view(row, by_forecast.get(row.forecast_id)) for row in rows]


def register_forecast_routes(app):
    @app.get("/api/v1/research/forecasts")
    async def forecasts(request: Request, limit: int = Query(default=20, ge=1, le=50)):
        require_backend_token(request)
        async with request.app.state.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ForecastDecision)
                    .where(ForecastDecision.symbol == "BTC_USDT")
                    .order_by(ForecastDecision.anchor.desc())
                    .limit(limit)
                )
            ).all()
            items = await _views(session, rows)
        return {"mode": "collect-only", "trading_enabled": False, "items": items}

    @app.get("/api/v1/research/forecasts/latest")
    async def latest_forecast(request: Request):
        require_backend_token(request)
        async with request.app.state.database.sessions() as session:
            row = await session.scalar(
                select(ForecastDecision)
                .where(ForecastDecision.symbol == "BTC_USDT")
                .order_by(ForecastDecision.anchor.desc())
                .limit(1)
            )
            items = await _views(session, [row] if row else [])
        return {
            "mode": "collect-only",
            "trading_enabled": False,
            "forecast": items[0] if items else None,
        }

    @app.get("/api/v1/research/forecasts/{forecast_id}")
    async def forecast_detail(forecast_id: UUID, request: Request):
        require_backend_token(request)
        async with request.app.state.database.sessions() as session:
            row = await session.get(ForecastDecision, forecast_id)
            if row is None or row.symbol != "BTC_USDT":
                raise HTTPException(404, "Forecast not found")
            items = await _views(session, [row])
        return {"mode": "collect-only", "trading_enabled": False, "forecast": items[0]}
