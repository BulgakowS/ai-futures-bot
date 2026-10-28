"""Test-only model-to-Paper orchestration; PaperService owns risk and execution."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.forecast_models import ForecastDecision, ForecastProposal
from ai_futures.models import InstrumentRules
from ai_futures.news_dataset import _hash, _utc
from ai_futures.news_forecast import (
    ArtifactApproval,
    CandidateRules,
    ForecastOutcome,
    ProposalPolicy,
    evaluate_forecast,
)
from ai_futures.news_training import ModelArtifact
from ai_futures.paper import OrderRequest, PaperService
from ai_futures.paper_models import PaperIntent, PaperOrder, PaperPortfolio
from ai_futures.schemas import utc_now

MAX_EXECUTION_AGE = timedelta(minutes=5)
UNKNOWN_ORDER_STATES = {"UNKNOWN", "BROKER_UNKNOWN"}


@dataclass(frozen=True)
class ModelPaperPolicy:
    quantity: Decimal
    leverage: Decimal = Decimal(1)


@dataclass(frozen=True)
class ModelPaperResult:
    status: str
    reason: str
    paper_result: dict | None = None


class ModelPaperService:
    """Only synthetic approved forecasts may reach the existing Paper risk gate."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.paper = PaperService(database, settings.config_version)
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def submit(
        self,
        portfolio_id: UUID,
        forecast_id: UUID,
        outcome: ForecastOutcome,
        artifact: ModelArtifact | None,
        approval: ArtifactApproval | None,
        rules: CandidateRules | None,
        proposal_policy: ProposalPolicy,
        execution_policy: ModelPaperPolicy,
        *,
        now=None,
    ) -> ModelPaperResult:
        if (
            self.settings.app_env != "test"
            or not self.settings.forecast_fixture_enabled
            or not self.settings.model_paper_fixture_enabled
            or self.database.settings.app_env != "test"
            or not self.database.settings.database_url.get_secret_value().startswith(
                "sqlite+aiosqlite:///"
            )
            or self.settings.database_url.get_secret_value()
            != self.database.settings.database_url.get_secret_value()
        ):
            raise PermissionError("Model Paper execution is hard-disabled outside test fixtures")
        lock = self._locks.setdefault(forecast_id, asyncio.Lock())
        async with lock:
            return await self._submit(
                portfolio_id,
                forecast_id,
                outcome,
                artifact,
                approval,
                rules,
                proposal_policy,
                execution_policy,
                now=now,
            )

    async def _submit(
        self,
        portfolio_id,
        forecast_id,
        outcome,
        artifact,
        approval,
        rules,
        proposal_policy,
        execution_policy,
        *,
        now,
    ):
        observed_now = _utc(now or utc_now())
        context = outcome.context
        if (
            outcome.status != "candidate"
            or outcome.proposal is None
            or context.quality != "ok"
            or context.observed_at > observed_now
            or observed_now - context.observed_at > MAX_EXECUTION_AGE
        ):
            return ModelPaperResult("blocked", "forecast_wait_or_stale")
        if approval is None or not approval.approved_at <= observed_now < approval.expires_at:
            return ModelPaperResult("blocked", "approval_not_current")
        verified = evaluate_forecast(
            context, artifact, approval, rules, proposal_policy, app_env=self.settings.app_env
        )
        if verified.status != "candidate" or verified != outcome:
            reason = verified.reason if verified.status != "candidate" else "forecast_mismatch"
            return ModelPaperResult("blocked", reason)
        key = f"model:{forecast_id}"
        async with self.database.sessions() as session:
            forecast = await session.get(ForecastDecision, forecast_id)
            proposal = await session.scalar(
                select(ForecastProposal).where(ForecastProposal.forecast_id == forecast_id)
            )
            portfolio = await session.get(PaperPortfolio, portfolio_id)
            active_rules = await session.get(InstrumentRules, "BTC_USDT")
            prior = await session.scalar(
                select(PaperIntent).where(
                    PaperIntent.portfolio_id == portfolio_id,
                    PaperIntent.idempotency_key == key,
                )
            )
            prior_order = (
                await session.scalar(select(PaperOrder).where(PaperOrder.intent_id == prior.id))
                if prior
                else None
            )
            if prior_order and prior_order.status in UNKNOWN_ORDER_STATES:
                return ModelPaperResult("blocked", "order_unknown")
            if portfolio is None or portfolio.mode != "paper":
                return ModelPaperResult("blocked", "paper_portfolio_missing")
            candidate = outcome.proposal
            if (
                forecast is None
                or proposal is None
                or proposal.proposal_id is None
                or forecast.symbol != "BTC_USDT"
                or forecast.horizon_hours != 4
                or forecast.status != "candidate"
                or forecast.quality != "ok"
                or forecast.direction != outcome.direction
                or forecast.correlation_id is None
                or forecast.model_version != outcome.model_version
                or forecast.artifact_checksum != outcome.artifact_checksum
                or forecast.dataset_checksum != outcome.dataset_checksum
                or forecast.config_checksum != context.config_checksum
                or forecast.source_versions != list(context.source_versions)
                or forecast.approval_version != outcome.approval_version
                or forecast.market_event_ids != list(context.market_event_ids)
                or forecast.news_version_ids != list(context.news_version_ids)
                or forecast.feature_hash != context.feature_hash
                or forecast.context_hash != _hash(asdict(context))
                or forecast.decision_hash != _hash(asdict(outcome))
                or proposal.proposal_hash != _hash(asdict(candidate))
                or proposal.direction != candidate.direction
                or (proposal.entry, proposal.stop_loss, proposal.take_profit)
                != (candidate.entry, candidate.stop_loss, candidate.take_profit)
                or proposal.policy_version != candidate.policy_version
                or proposal.rules_version != candidate.rules_version
            ):
                return ModelPaperResult("blocked", "forecast_not_sealed_or_mismatch")
            if (
                active_rules is None
                or not active_rules.validated
                or active_rules.version != candidate.rules_version
            ):
                return ModelPaperResult("blocked", "rules_mismatch")
            try:
                step = Decimal(active_rules.payload["volUnit"])
                minimum = Decimal(active_rules.payload["minVol"])
                maximum = Decimal(active_rules.payload["maxVol"])
                quantity = execution_policy.quantity
                if (
                    not isinstance(quantity, Decimal)
                    or not quantity.is_finite()
                    or not isinstance(execution_policy.leverage, Decimal)
                    or not execution_policy.leverage.is_finite()
                    or step <= 0
                    or minimum <= 0
                    or maximum < minimum
                ):
                    return ModelPaperResult("blocked", "quantity_or_rules_invalid")
                quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
                if not minimum <= quantity <= maximum:
                    return ModelPaperResult("blocked", "quantity_outside_contract")
                request = OrderRequest(
                    idempotency_key=key,
                    direction=candidate.direction,
                    kind="market",
                    quantity=quantity,
                    leverage=execution_policy.leverage,
                    stop_loss=Decimal(candidate.stop_loss),
                    take_profit=Decimal(candidate.take_profit),
                    source=f"model:{forecast.correlation_id}",
                )
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return ModelPaperResult("blocked", "quantity_or_rules_invalid")
        result = await self.paper.submit(portfolio_id, request)
        risk = result["risk"]
        return ModelPaperResult(
            "submitted" if risk["outcome"] == "ALLOW" else "risk_rejected",
            risk["reason"],
            result,
        )
