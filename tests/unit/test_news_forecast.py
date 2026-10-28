"""N4a feature-only inference and synthetic approval gates."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_futures.news_dataset import (
    CandleRecord,
    CoverageRecord,
    DatasetConfig,
    NewsRecord,
    _hash,
    build_dataset,
)
from ai_futures.news_forecast import (
    CandidateRules,
    ProposalPolicy,
    build_forecast_context,
    evaluate_forecast,
    issue_test_approval,
)
from ai_futures.news_training import ALGORITHM, ModelArtifact, predict

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
CONFIG = DatasetConfig(Decimal("10"), "synthetic-cost-v1")


def at(hours: int) -> datetime:
    return START + hours * HOUR


def market(index: int, close: str) -> CandleRecord:
    return CandleRecord(
        event_id=f"synthetic-market-{index}",
        window_start=at(4 * index),
        source_time=at(4 * index + 4),
        received_at=at(4 * index + 4),
        close=close,
        payload_hash=f"{index + 1:064x}",
        source="synthetic",
        config_version="fixture-v1",
    )


def notice(version: int = 1, *, seen_hour: int = 11, kind: str = "forward") -> NewsRecord:
    return NewsRecord(
        version_id=f"synthetic-news-v{version}",
        canonical_id="synthetic-news-one",
        version_number=version,
        title="Synthetic BTC futures notice",
        post_time=at(10),
        first_seen_at=at(seen_hour),
        available_at=at(max(10, seen_hour)),
        payload_hash=f"{version + 100:064x}",
        source="synthetic",
        schema_version="fixture-v1",
        config_version="fixture-v1",
        observation_kind=kind,
    )


def context(
    *,
    current: CandleRecord | None = None,
    news: tuple[NewsRecord, ...] | None = None,
    coverage: tuple[CoverageRecord, ...] | None = None,
    observed_at: datetime | None = None,
):
    return build_forecast_context(
        market(1, "100"),
        market(2, "101.05") if current is None else current,
        (notice(),) if news is None else news,
        (CoverageRecord(at(12), at(12), "ok"),) if coverage is None else coverage,
        anchor=at(12),
        observed_at=at(12) + timedelta(minutes=1) if observed_at is None else observed_at,
        config=CONFIG,
    )


def artifact(up_bias: float = 5.0) -> ModelArtifact:
    fields = {
        "variant": "market+news",
        "algorithm": ALGORITHM,
        "model_version": f"{ALGORITHM}/market+news",
        "runtime_version": "python-synthetic-test-stdlib",
        "dataset_checksum": "a" * 64,
        "dataset_config_checksum": _hash(asdict(CONFIG)),
        "source_checksum": "b" * 64,
        "source_versions": context().source_versions,
        "training_config_checksum": "c" * 64,
        "seed": 7,
        "train_end": at(4),
        "validation_end": at(8),
        "test_end": at(12),
        "feature_names": (
            "bias",
            "market_return_bps/100",
            "btc_titles/10",
            "futures_titles/10",
        ),
        "weights": (("-5", "0", "0", "0"), ("-5", "0", "0", "0"), (str(up_bias), "0", "0", "0")),
        "threshold": "0.40",
    }
    return ModelArtifact(**fields, checksum=_hash(fields))


def rules(*, validated: bool = True) -> CandidateRules:
    return CandidateRules("synthetic-rules-v1", at(12), Decimal("0.1"), Decimal("1"), validated)


def test_feature_only_context_matches_n2a_without_future_label_fields():
    prior = market(1, "100")
    current = market(2, "101.05")
    future = market(3, "103")
    dataset = build_dataset(
        (prior, current, future),
        (notice(),),
        (CoverageRecord(at(12), at(12), "ok"),),
        cutoff=at(16),
        config=CONFIG,
    )
    sample = next(row for row in dataset.samples if row.anchor == at(12))
    present = context()
    assert present.quality == "ok"
    assert present.feature_hash == sample.feature_hash
    assert present.market_event_ids == sample.market_event_ids
    assert present.news_version_ids == sample.news_version_ids
    assert not ({"label", "label_event_id", "label_payload_hash"} & set(asdict(present)))
    assert predict(artifact(), present) == predict(artifact(), sample)
    changed_future = replace(future, close="80", payload_hash="f" * 64)
    changed = build_dataset(
        (prior, current, changed_future),
        (notice(),),
        (CoverageRecord(at(12), at(12), "ok"),),
        cutoff=at(16),
        config=CONFIG,
    )
    assert changed.samples[0].label != sample.label
    assert context().feature_hash == present.feature_hash
    future_edit = replace(notice(version=2, seen_hour=15), title="Edited synthetic title")
    assert context(news=(notice(), future_edit)).feature_hash == present.feature_hash


def test_unapproved_and_non_test_artifacts_wait_then_test_approval_allows_candidate():
    present = context()
    model = artifact()
    policy = ProposalPolicy("synthetic-proposal-v1")
    assert evaluate_forecast(present, model, None, rules(), policy, app_env="test").reason == (
        "artifact_not_approved"
    )
    approval = issue_test_approval(model, approved_at=at(12), expires_at=at(13))
    assert evaluate_forecast(present, model, approval, rules(), policy).reason == (
        "synthetic_artifact_not_deployable"
    )
    result = evaluate_forecast(present, model, approval, rules(), policy, app_env="test")
    assert result.status == "candidate" and result.direction == "UP"
    assert result.proposal.direction == "LONG"
    assert (result.proposal.entry, result.proposal.stop_loss, result.proposal.take_profit) == (
        "101.1",
        "100.0",
        "103.2",
    )
    assert result.dataset_checksum == model.dataset_checksum
    assert sum(map(Decimal, result.probabilities)) >= Decimal("0.99999999")


def test_fail_closed_future_market_coverage_edit_backfill_artifact_and_rules():
    model = artifact()
    approval = issue_test_approval(model, approved_at=at(12), expires_at=at(13))
    policy = ProposalPolicy("synthetic-proposal-v1")

    def reason(value):
        return evaluate_forecast(value, model, approval, rules(), policy, app_env="test").reason

    assert reason(context(current=market(3, "102"))) == "market_gap_or_invalid"
    assert reason(context(observed_at=at(12) + timedelta(minutes=6))) == "context_stale_or_early"
    assert reason(context(coverage=())) == "news_coverage_missing"
    assert reason(context(coverage=(CoverageRecord(at(12), at(7), "ok"),))) == "news_coverage_stale"
    assert reason(context(coverage=(CoverageRecord(at(12), at(12), "gap", gap_count=1),))) == (
        "news_coverage_gap"
    )
    assert (
        reason(
            context(
                news=(
                    notice(),
                    replace(notice(version=2), first_seen_at=at(12), available_at=at(12)),
                )
            )
        )
        == "news_edit"
    )
    assert reason(context(news=(replace(notice(), observation_kind="backfill"),))) == (
        "news_late_or_backfill"
    )
    assert (
        evaluate_forecast(
            context(), replace(model, threshold="0.99"), approval, rules(), policy, app_env="test"
        ).reason
        == "artifact_invalid"
    )
    assert (
        evaluate_forecast(
            context(), model, approval, rules(validated=False), policy, app_env="test"
        ).reason
        == "rules_invalid_or_stale"
    )
    assert (
        evaluate_forecast(
            context(), model, replace(approval, checksum="0" * 64), rules(), policy, app_env="test"
        ).reason
        == "approval_invalid"
    )
    assert reason(replace(context(), feature_hash="0" * 64)) == "context_invalid_or_stale"
    assert reason(replace(context(), observed_at=at(12) + timedelta(minutes=6))) == (
        "context_invalid_or_stale"
    )
