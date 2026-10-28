"""Test-only durable append-only forecast/proposal journal; no execution path."""

from dataclasses import asdict, dataclass
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.forecast_models import ForecastDecision, ForecastProposal
from ai_futures.news_dataset import _hash, _utc
from ai_futures.news_forecast import ForecastOutcome
from ai_futures.schemas import utc_now

NAMESPACE = UUID("4a1515ad-d91b-4b3e-ae76-5c0ce3cda5e8")


@dataclass(frozen=True)
class JournalWrite:
    forecast_id: UUID
    correlation_id: UUID
    proposal_id: UUID | None
    inserted: bool


class ForecastJournal:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    async def append(self, outcome: ForecastOutcome) -> JournalWrite:
        if self.settings.app_env != "test" or not self.settings.forecast_fixture_enabled:
            raise PermissionError("Forecast journal is disabled outside explicit test fixtures")
        context = outcome.context
        anchor = _utc(context.anchor)
        _utc(context.observed_at)
        if int(anchor.timestamp()) % (4 * 60 * 60):
            raise ValueError("Forecast anchor must align to a 4h UTC window")
        if (
            outcome.status not in {"wait", "candidate"}
            or outcome.direction not in {"UP", "DOWN", "WAIT"}
            or (outcome.status == "candidate") != (outcome.proposal is not None)
            or (outcome.status == "candidate" and outcome.direction == "WAIT")
            or (outcome.status == "wait" and outcome.direction != "WAIT")
        ):
            raise ValueError("Invalid forecast outcome for journal")
        if outcome.status == "candidate" and (
            context.quality != "ok"
            or context.feature_hash is None
            or not context.market_event_ids
            or outcome.probabilities is None
            or not outcome.model_version
            or not outcome.artifact_checksum
            or not outcome.dataset_checksum
            or outcome.approval_version != "synthetic-approval-v1"
            or outcome.proposal.direction != ("LONG" if outcome.direction == "UP" else "SHORT")
        ):
            raise ValueError("Invalid candidate provenance for journal")
        forecast_id = uuid5(NAMESPACE, f"forecast:BTC_USDT:{anchor.isoformat()}")
        correlation_id = uuid5(NAMESPACE, f"correlation:BTC_USDT:{anchor.isoformat()}")
        proposal_id = uuid5(NAMESPACE, f"proposal:{forecast_id}") if outcome.proposal else None
        decision_hash = _hash(asdict(outcome))

        async def existing_write() -> JournalWrite | None:
            async with self.database.sessions() as session:
                existing = await session.scalar(
                    select(ForecastDecision).where(
                        ForecastDecision.symbol == "BTC_USDT", ForecastDecision.anchor == anchor
                    )
                )
                if existing is None:
                    return None
                if existing.decision_hash != decision_hash:
                    raise ValueError("Forecast window is already sealed with another decision")
                return JournalWrite(
                    existing.forecast_id,
                    existing.correlation_id,
                    proposal_id if outcome.proposal else None,
                    False,
                )

        prior = await existing_write()
        if prior is not None:
            return prior
        try:
            async with self.database.sessions.begin() as session:
                created_at = utc_now()
                session.add(
                    ForecastDecision(
                        forecast_id=forecast_id,
                        correlation_id=correlation_id,
                        symbol="BTC_USDT",
                        anchor=anchor,
                        horizon_hours=4,
                        status=outcome.status,
                        direction=outcome.direction,
                        reason=outcome.reason,
                        quality=context.quality,
                        probabilities=dict(
                            zip(("DOWN", "NEUTRAL", "UP"), outcome.probabilities, strict=True)
                        )
                        if outcome.probabilities
                        else None,
                        model_version=outcome.model_version,
                        artifact_checksum=outcome.artifact_checksum,
                        dataset_checksum=outcome.dataset_checksum,
                        config_checksum=context.config_checksum,
                        source_versions=list(context.source_versions),
                        market_event_ids=list(context.market_event_ids),
                        news_version_ids=list(context.news_version_ids),
                        feature_hash=context.feature_hash,
                        context_hash=_hash(asdict(context)),
                        decision_hash=decision_hash,
                        approval_version=outcome.approval_version,
                        created_at=created_at,
                    )
                )
                if outcome.proposal:
                    proposal = outcome.proposal
                    session.add(
                        ForecastProposal(
                            proposal_id=proposal_id,
                            forecast_id=forecast_id,
                            direction=proposal.direction,
                            entry=proposal.entry,
                            stop_loss=proposal.stop_loss,
                            take_profit=proposal.take_profit,
                            rules_version=proposal.rules_version,
                            policy_version=proposal.policy_version,
                            proposal_hash=_hash(asdict(proposal)),
                            created_at=created_at,
                        )
                    )
            return JournalWrite(forecast_id, correlation_id, proposal_id, True)
        except IntegrityError:
            # A concurrent identical insert is idempotent; a differing one remains sealed.
            prior = await existing_write()
            if prior is not None:
                return prior
            raise
