"""N4a offline, feature-only 4h forecast contract; no broker or API wiring."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

from ai_futures.news_dataset import (
    HEX64,
    HORIZON,
    CandleRecord,
    CoverageRecord,
    DatasetConfig,
    NewsRecord,
    _hash,
    _utc,
    past_features,
)
from ai_futures.news_training import ModelArtifact, predict, verify_artifact

MAX_CONTEXT_AGE = timedelta(minutes=5)
MAX_RULES_AGE = timedelta(hours=1)
APPROVAL_SCOPE = "test-fixture-only"


@dataclass(frozen=True)
class ForecastContext:
    anchor: datetime
    observed_at: datetime
    quality: str
    reason: str
    close: str | None
    market_event_ids: tuple[str, ...]
    market_payload_hashes: tuple[str, ...]
    news_version_ids: tuple[str, ...]
    news_payload_hashes: tuple[str, ...]
    market_return_bps: str
    btc_title_count: int
    futures_title_count: int
    feature_hash: str | None
    config_checksum: str
    source_versions: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactApproval:
    approval_version: str
    scope: str
    artifact_checksum: str
    dataset_checksum: str
    model_version: str
    approved_at: datetime
    expires_at: datetime
    checksum: str


@dataclass(frozen=True)
class CandidateRules:
    version: str
    observed_at: datetime
    tick_size: Decimal
    min_price: Decimal
    validated: bool


@dataclass(frozen=True)
class ProposalPolicy:
    version: str
    stop_bps: Decimal = Decimal("100")
    take_bps: Decimal = Decimal("200")


@dataclass(frozen=True)
class CandidateProposal:
    direction: str
    entry: str
    stop_loss: str
    take_profit: str
    rules_version: str
    policy_version: str


@dataclass(frozen=True)
class ForecastOutcome:
    context: ForecastContext
    status: str
    direction: str
    reason: str
    probabilities: tuple[str, str, str] | None
    model_version: str | None
    artifact_checksum: str | None
    dataset_checksum: str | None
    approval_version: str | None
    proposal: CandidateProposal | None


def _waiting(
    anchor: datetime, observed_at: datetime, reason: str, config: DatasetConfig
) -> ForecastContext:
    return ForecastContext(
        anchor=anchor,
        observed_at=observed_at,
        quality="wait",
        reason=reason,
        close=None,
        market_event_ids=(),
        market_payload_hashes=(),
        news_version_ids=(),
        news_payload_hashes=(),
        market_return_bps="0",
        btc_title_count=0,
        futures_title_count=0,
        feature_hash=None,
        config_checksum=_hash(asdict(config)),
        source_versions=(config.source_version,),
    )


def build_forecast_context(
    prior: CandleRecord | None,
    current: CandleRecord | None,
    news_versions: tuple[NewsRecord, ...],
    coverage: tuple[CoverageRecord, ...],
    *,
    anchor: datetime,
    observed_at: datetime,
    config: DatasetConfig,
) -> ForecastContext:
    """Build as-of features from exactly two past candles; future label is not an input."""
    anchor, observed_at = _utc(anchor), _utc(observed_at)
    if int(anchor.timestamp()) % int(HORIZON.total_seconds()):
        return _waiting(anchor, observed_at, "anchor_unaligned", config)
    if observed_at < anchor or observed_at - anchor > MAX_CONTEXT_AGE:
        return _waiting(anchor, observed_at, "context_stale_or_early", config)
    if prior is None or current is None:
        return _waiting(anchor, observed_at, "market_gap", config)
    try:
        for row, expected_start in ((prior, anchor - 2 * HORIZON), (current, anchor - HORIZON)):
            if (
                row.symbol != "BTC_USDT"
                or row.timeframe != "4h"
                or not row.final
                or _utc(row.window_start) != expected_start
                or not row.event_id
                or not HEX64.fullmatch(row.payload_hash)
                or not row.source
                or not row.config_version
            ):
                return _waiting(anchor, observed_at, "market_gap_or_invalid", config)
            price = Decimal(row.close)
            if not price.is_finite() or price <= 0:
                return _waiting(anchor, observed_at, "market_invalid_price", config)
            if (
                _utc(row.source_time) < expected_start + HORIZON
                or _utc(row.received_at) < expected_start + HORIZON
            ):
                return _waiting(anchor, observed_at, "market_invalid_finality", config)
            if row.source_time > anchor or row.received_at > anchor:
                return _waiting(anchor, observed_at, "market_late", config)
        if prior.event_id == current.event_id:
            return _waiting(anchor, observed_at, "market_duplicate_event", config)
    except (InvalidOperation, TypeError, ValueError):
        return _waiting(anchor, observed_at, "market_invalid", config)
    try:
        selected_coverage = next(
            (
                row
                for row in sorted(coverage, key=lambda item: _utc(item.observed_at), reverse=True)
                if _utc(row.observed_at) <= anchor
            ),
            None,
        )
    except (TypeError, ValueError):
        return _waiting(anchor, observed_at, "news_coverage_invalid", config)
    if selected_coverage is None or selected_coverage.last_success_at is None:
        return _waiting(anchor, observed_at, "news_coverage_missing", config)
    try:
        if (
            selected_coverage.status != "ok"
            or selected_coverage.gap_count
            or selected_coverage.page_shift_detected
            or not selected_coverage.source_version
        ):
            return _waiting(anchor, observed_at, "news_coverage_gap", config)
        if (
            _utc(selected_coverage.observed_at) > anchor
            or _utc(selected_coverage.last_success_at) > anchor
            or anchor - selected_coverage.last_success_at > config.max_coverage_age
        ):
            return _waiting(anchor, observed_at, "news_coverage_stale", config)
    except (TypeError, ValueError):
        return _waiting(anchor, observed_at, "news_coverage_invalid", config)
    selected: dict[str, NewsRecord] = {}
    try:
        for item in news_versions:
            if _utc(item.first_seen_at) > anchor or _utc(item.available_at) > anchor:
                continue
            if (
                item.available_at != max(_utc(item.post_time), item.first_seen_at)
                or item.version_number < 1
                or not item.canonical_id
                or not item.version_id
                or not item.title.strip()
                or not HEX64.fullmatch(item.payload_hash)
                or item.observation_kind not in {"forward", "backfill"}
                or not item.source
                or not item.schema_version
                or not item.config_version
            ):
                return _waiting(anchor, observed_at, "news_invalid", config)
            previous = selected.get(item.canonical_id)
            if previous is None or item.version_number > previous.version_number:
                selected[item.canonical_id] = item
            elif item.version_number == previous.version_number and item != previous:
                return _waiting(anchor, observed_at, "news_conflicting_duplicate", config)
    except (TypeError, ValueError, AttributeError):
        return _waiting(anchor, observed_at, "news_invalid", config)
    recent = tuple(
        sorted(
            (item for item in selected.values() if anchor - HORIZON < item.available_at <= anchor),
            key=lambda item: (item.canonical_id, item.version_number),
        )
    )
    if any(
        item.observation_kind == "backfill" or item.first_seen_at - item.post_time > HORIZON
        for item in recent
    ):
        return _waiting(anchor, observed_at, "news_late_or_backfill", config)
    if any(item.version_number > 1 for item in recent):
        return _waiting(anchor, observed_at, "news_edit", config)
    features = past_features(anchor, prior, current, recent)
    versions = tuple(
        sorted(
            {
                config.source_version,
                selected_coverage.source_version,
                f"{prior.source}:{prior.config_version}",
                f"{current.source}:{current.config_version}",
            }
            | {f"{item.source}:{item.schema_version}:{item.config_version}" for item in recent}
        )
    )
    return ForecastContext(
        anchor=anchor,
        observed_at=observed_at,
        quality="ok",
        reason="ok",
        close=current.close,
        market_event_ids=features["market_event_ids"],
        market_payload_hashes=features["market_payload_hashes"],
        news_version_ids=features["news_version_ids"],
        news_payload_hashes=features["news_payload_hashes"],
        market_return_bps=features["market_return_bps"],
        btc_title_count=features["btc_title_count"],
        futures_title_count=features["futures_title_count"],
        feature_hash=_hash(features),
        config_checksum=_hash(asdict(config)),
        source_versions=versions,
    )


def issue_test_approval(
    artifact: ModelArtifact, *, approved_at: datetime, expires_at: datetime
) -> ArtifactApproval:
    """Construct a synthetic fixture approval; production loading still rejects it."""
    fields = {
        "approval_version": "synthetic-approval-v1",
        "scope": APPROVAL_SCOPE,
        "artifact_checksum": artifact.checksum,
        "dataset_checksum": artifact.dataset_checksum,
        "model_version": artifact.model_version,
        "approved_at": _utc(approved_at),
        "expires_at": _utc(expires_at),
    }
    if fields["expires_at"] <= fields["approved_at"]:
        raise ValueError("Approval must expire after issue")
    return ArtifactApproval(**fields, checksum=_hash(fields))


def load_approved_artifact(
    artifact: ModelArtifact | None,
    approval: ArtifactApproval | None,
    *,
    context: ForecastContext,
    app_env: str,
) -> tuple[ModelArtifact | None, str]:
    """N4a has no production approval path; only explicit test fixture approval can load."""
    if artifact is None:
        return None, "artifact_missing"
    if not verify_artifact(artifact):
        return None, "artifact_invalid"
    if app_env != "test":
        return None, "synthetic_artifact_not_deployable"
    if (
        artifact.variant != "market+news"
        or artifact.dataset_config_checksum != context.config_checksum
        or not all(version.startswith("synthetic") for version in artifact.source_versions)
        or not set(context.source_versions).issubset(artifact.source_versions)
    ):
        return None, "artifact_context_mismatch"
    if approval is None:
        return None, "artifact_not_approved"
    fields = asdict(approval)
    checksum = fields.pop("checksum")
    if (
        checksum != _hash(fields)
        or approval.scope != APPROVAL_SCOPE
        or approval.approval_version != "synthetic-approval-v1"
        or approval.artifact_checksum != artifact.checksum
        or approval.dataset_checksum != artifact.dataset_checksum
        or approval.model_version != artifact.model_version
    ):
        return None, "approval_invalid"
    try:
        if not _utc(approval.approved_at) <= context.observed_at < _utc(approval.expires_at):
            return None, "approval_not_current"
    except ValueError:
        return None, "approval_invalid"
    return artifact, "ok"


def _round_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _candidate(
    context: ForecastContext,
    direction: str,
    rules: CandidateRules | None,
    policy: ProposalPolicy,
) -> tuple[CandidateProposal | None, str]:
    if rules is None:
        return None, "rules_missing"
    try:
        if (
            not rules.validated
            or not rules.version
            or not policy.version
            or not rules.tick_size.is_finite()
            or not rules.min_price.is_finite()
            or not policy.stop_bps.is_finite()
            or not policy.take_bps.is_finite()
            or rules.tick_size <= 0
            or rules.min_price <= 0
            or not 0 < policy.stop_bps < 10000
            or not 0 < policy.take_bps < 10000
            or _utc(rules.observed_at) > context.anchor
            or context.anchor - rules.observed_at > MAX_RULES_AGE
        ):
            return None, "rules_invalid_or_stale"
        entry = _round_tick(Decimal(context.close), rules.tick_size, ROUND_HALF_UP)
        if entry < rules.min_price:
            return None, "entry_below_min_price"
        stop_fraction = policy.stop_bps / Decimal(10000)
        take_fraction = policy.take_bps / Decimal(10000)
        if direction == "UP":
            stop = _round_tick(entry * (1 - stop_fraction), rules.tick_size, ROUND_DOWN)
            take = _round_tick(entry * (1 + take_fraction), rules.tick_size, ROUND_UP)
            side = "LONG"
        else:
            stop = _round_tick(entry * (1 + stop_fraction), rules.tick_size, ROUND_UP)
            take = _round_tick(entry * (1 - take_fraction), rules.tick_size, ROUND_DOWN)
            side = "SHORT"
        if (
            stop <= 0
            or take <= 0
            or (side == "LONG" and not stop < entry < take)
            or (side == "SHORT" and not take < entry < stop)
        ):
            return None, "candidate_invalid_prices"
        return CandidateProposal(
            direction=side,
            entry=format(entry, "f"),
            stop_loss=format(stop, "f"),
            take_profit=format(take, "f"),
            rules_version=rules.version,
            policy_version=policy.version,
        ), "ok"
    except (InvalidOperation, TypeError, ValueError):
        return None, "rules_invalid_or_stale"


def evaluate_forecast(
    context: ForecastContext,
    artifact: ModelArtifact | None,
    approval: ArtifactApproval | None,
    rules: CandidateRules | None,
    policy: ProposalPolicy,
    *,
    app_env: str = "local",
) -> ForecastOutcome:
    """Return a read-only candidate or WAIT; never submits an order."""
    base = {
        "context": context,
        "model_version": artifact.model_version if artifact else None,
        "artifact_checksum": artifact.checksum if artifact else None,
        "dataset_checksum": artifact.dataset_checksum if artifact else None,
    }

    def wait(reason: str, probabilities=None, approval_version=None) -> ForecastOutcome:
        return ForecastOutcome(
            **base,
            status="wait",
            direction="WAIT",
            reason=reason,
            probabilities=probabilities,
            approval_version=approval_version,
            proposal=None,
        )

    if context.quality != "ok" or context.feature_hash is None:
        return wait(context.reason)
    try:
        if (
            _utc(context.anchor) != context.anchor
            or _utc(context.observed_at) < context.anchor
            or context.observed_at - context.anchor > MAX_CONTEXT_AGE
            or len(context.market_event_ids) != 2
            or len(context.market_payload_hashes) != 2
            or len(context.news_version_ids) != len(context.news_payload_hashes)
            or not context.close
            or Decimal(context.close) <= 0
            or context.feature_hash
            != _hash(
                {
                    "anchor": context.anchor,
                    "market_event_ids": context.market_event_ids,
                    "market_payload_hashes": context.market_payload_hashes,
                    "market_return_bps": context.market_return_bps,
                    "news_version_ids": context.news_version_ids,
                    "news_payload_hashes": context.news_payload_hashes,
                    "btc_title_count": context.btc_title_count,
                    "futures_title_count": context.futures_title_count,
                }
            )
        ):
            return wait("context_invalid_or_stale")
    except (InvalidOperation, TypeError, ValueError):
        return wait("context_invalid_or_stale")
    loaded, reason = load_approved_artifact(artifact, approval, context=context, app_env=app_env)
    if loaded is None:
        return wait(reason)
    probabilities, direction = predict(loaded, context)
    if direction in {"WAIT", "NEUTRAL"}:
        return wait("model_wait_or_neutral", probabilities, approval.approval_version)
    proposal, reason = _candidate(context, direction, rules, policy)
    if proposal is None:
        return wait(reason, probabilities, approval.approval_version)
    return ForecastOutcome(
        **base,
        status="candidate",
        direction=direction,
        reason="approved_synthetic_candidate",
        probabilities=probabilities,
        approval_version=approval.approval_version,
        proposal=proposal,
    )
