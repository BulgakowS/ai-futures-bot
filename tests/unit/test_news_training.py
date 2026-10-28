"""N3a synthetic-only training, leakage, and fail-closed evidence."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.news_dataset import (
    SCHEMA,
    CandleRecord,
    CoverageRecord,
    DatasetConfig,
    NewsRecord,
    _hash,
    build_dataset,
)
from ai_futures.news_training import TrainingConfig, predict, train_and_evaluate, verify_artifact

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DATA_CONFIG = DatasetConfig(Decimal("10"), "synthetic-cost-v1")
TRAIN_CONFIG = TrainingConfig(7, START + 160 * HOUR, START + 280 * HOUR, START + 400 * HOUR)


def at(hours: int) -> datetime:
    return START + hours * HOUR


def synthetic_dataset(
    count: int = 100,
    *,
    close_override: int | None = None,
    coverage_gap: int | None = None,
    coverage_stale: int | None = None,
):
    price = Decimal("100")
    candles: list[CandleRecord] = []
    news: list[NewsRecord] = []
    for index in range(count):
        if index:
            price *= (Decimal("1.02"), Decimal("0.98"), Decimal("1.0005"))[index % 3]
        anchor = 4 * index + 4
        recorded_close = price * 2 if index == close_override else price
        candles.append(
            CandleRecord(
                event_id=f"synthetic-market-{index}",
                window_start=at(4 * index),
                source_time=at(anchor),
                received_at=at(anchor),
                close=format(recorded_close, "f"),
                payload_hash="f" * 64 if index == close_override else f"{index + 1:064x}",
                source="synthetic",
                config_version="fixture-v1",
            )
        )
        if index:
            news.append(
                NewsRecord(
                    version_id=f"synthetic-news-{index}",
                    canonical_id=f"synthetic-canonical-{index}",
                    version_number=1,
                    title=("Synthetic BTC update", "Synthetic futures update", "Synthetic notice")[
                        index % 3
                    ],
                    post_time=at(anchor - 2),
                    first_seen_at=at(anchor - 1),
                    available_at=at(anchor - 1),
                    payload_hash=f"{index + 1000:064x}",
                    source="synthetic",
                    schema_version="fixture-v1",
                    config_version="fixture-v1",
                )
            )
    coverage = tuple(
        CoverageRecord(
            at(4 * index),
            at(4 * index - 8) if index == coverage_stale else at(4 * index),
            "gap" if index == coverage_gap else "ok",
            gap_count=1 if index == coverage_gap else 0,
        )
        for index in range(count + 1)
    )
    return build_dataset(
        tuple(candles), tuple(news), coverage, cutoff=at(count * 4), config=DATA_CONFIG
    )


def test_reproducible_three_models_manifest_metrics_and_rolling_blocks():
    dataset = synthetic_dataset()
    first = train_and_evaluate(dataset, DATA_CONFIG, TRAIN_CONFIG)
    second = train_and_evaluate(dataset, DATA_CONFIG, TRAIN_CONFIG)
    assert first == second
    assert first.status == "evaluated-synthetic"
    assert len(first.artifacts) == 3
    assert {artifact.variant for artifact in first.artifacts} == {
        "market-only",
        "news-only",
        "market+news",
    }
    assert all(verify_artifact(artifact) for artifact in first.artifacts)
    assert {
        key: value for key, value in first.report["split"].items() if key != "class_counts"
    } == {
        "train": 38,
        "validation": 29,
        "test": 29,
        "embargo_hours": 4.0,
        "train_end": at(160).isoformat(),
        "validation_end": at(280).isoformat(),
        "test_end": at(400).isoformat(),
    }
    assert sum(first.report["split"]["class_counts"]["train"].values()) == 38
    assert first.report["source_waits"] == {"market_gap_or_unavailable_label": 1}
    assert first.report["assumptions"]["funding_bps"] == "0"
    assert set(first.report["baseline"]) == {"always-neutral", "market-sign-rule"}
    assert first.report["promotion_verdict"].startswith("collect-only")
    assert set(first.report["news_contribution_descriptive"]) == {"validation", "test"}
    assert all(
        artifact.model_version.startswith("multinomial-sgd-v1/") for artifact in first.artifacts
    )
    for variant in first.report["models"].values():
        assert variant["test"]["count"] == 29
        assert sum(block["count"] for block in variant["rolling_heldout_test"]) == 29
        assert 0 <= float(variant["test"]["brier"]) <= 2
        assert 0 <= float(variant["test"]["calibration_ece_3bin"]) <= 1
    probabilities, decision = predict(first.artifacts[0], dataset.samples[0])
    assert (probabilities, decision) == predict(second.artifacts[0], dataset.samples[0])
    assert sum(map(float, probabilities)) == 1.0 or abs(sum(map(float, probabilities)) - 1) < 1e-7
    assert decision in {"UP", "DOWN", "NEUTRAL", "WAIT"}


def test_validation_only_threshold_and_untouched_test_labels():
    dataset = synthetic_dataset()
    original = train_and_evaluate(dataset, DATA_CONFIG, TRAIN_CONFIG)
    # A changed synthetic test candle cannot change train fit or validation threshold.
    modified = synthetic_dataset(close_override=75)
    result = train_and_evaluate(modified, DATA_CONFIG, TRAIN_CONFIG)
    assert result.status == "evaluated-synthetic"
    for before, after in zip(original.artifacts, result.artifacts, strict=True):
        assert before.weights == after.weights
        assert before.threshold == after.threshold
        assert before.checksum != after.checksum  # provenance seal includes changed dataset


def test_future_source_change_keeps_fixed_artifact_past_prediction():
    original = synthetic_dataset()
    changed = synthetic_dataset(close_override=12)
    outcome = train_and_evaluate(original, DATA_CONFIG, TRAIN_CONFIG)
    early = original.samples[0]
    assert early.feature_hash == changed.samples[0].feature_hash
    assert predict(outcome.artifacts[2], early) == predict(outcome.artifacts[2], changed.samples[0])
    assert original.checksum != changed.checksum


def test_collect_only_on_stale_too_few_non_synthetic_and_corrupt_manifest():
    dataset = synthetic_dataset()
    too_few = synthetic_dataset(12)
    assert train_and_evaluate(too_few, DATA_CONFIG, TRAIN_CONFIG).reason == "too_few_samples"
    stale = synthetic_dataset(coverage_gap=40)
    assert train_and_evaluate(stale, DATA_CONFIG, TRAIN_CONFIG).reason == "source_quality_wait"
    aged = synthetic_dataset(coverage_stale=40)
    assert "news_coverage_stale" in {wait.reason for wait in aged.waits}
    assert train_and_evaluate(aged, DATA_CONFIG, TRAIN_CONFIG).reason == "source_quality_wait"
    assert (
        train_and_evaluate(
            dataset, replace(DATA_CONFIG, source_version="mexc-real"), TRAIN_CONFIG
        ).status
        == "collect-only"
    )
    corrupt = replace(dataset, checksum="0" * 64)
    assert (
        train_and_evaluate(corrupt, DATA_CONFIG, TRAIN_CONFIG).reason == "invalid_dataset_manifest"
    )
    assert train_and_evaluate(dataset, DATA_CONFIG, replace(TRAIN_CONFIG, embargo=HOUR)).reason == (
        "invalid_dataset_or_split"
    )
    assert (
        train_and_evaluate(
            dataset, DATA_CONFIG, replace(TRAIN_CONFIG, slippage_bps=Decimal("3"))
        ).reason
        == "cost_assumption_mismatch"
    )


def test_training_rejects_related_announcement_cross_split():
    dataset = synthetic_dataset()
    early = next(row for row in dataset.samples if row.anchor == at(8))
    modified = replace(
        dataset,
        samples=tuple(
            replace(row, news_canonical_ids=early.news_canonical_ids)
            if row.anchor == at(300)
            else row
            for row in dataset.samples
        ),
    )
    modified = replace(
        modified,
        checksum=_hash(
            {
                "schema": SCHEMA,
                "cutoff": modified.cutoff,
                "config_checksum": modified.config_checksum,
                "source_checksum": modified.source_checksum,
                "source_versions": modified.source_versions,
                "samples": tuple(asdict(row) for row in modified.samples),
                "waits": tuple(asdict(row) for row in modified.waits),
            }
        ),
    )
    assert train_and_evaluate(modified, DATA_CONFIG, TRAIN_CONFIG).reason == (
        "invalid_dataset_or_split"
    )


def test_artifact_tamper_and_seed_change():
    dataset = synthetic_dataset()
    first = train_and_evaluate(dataset, DATA_CONFIG, TRAIN_CONFIG)
    other = train_and_evaluate(dataset, DATA_CONFIG, replace(TRAIN_CONFIG, seed=8))
    assert first.artifacts[0].checksum != other.artifacts[0].checksum
    assert first.artifacts[0].weights != other.artifacts[0].weights
    tampered = replace(first.artifacts[0], threshold="0.99")
    assert not verify_artifact(tampered)
    with pytest.raises(ValueError, match="checksum"):
        predict(tampered, dataset.samples[0])
