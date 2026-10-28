"""G5 synthetic GKG v2 training gates; no external services."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.gdelt_dataset import MarketCandle
from ai_futures.gdelt_gkg_dataset import SCHEMA, Config, Release, Scan, Slot, _hash, build_dataset
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION
from ai_futures.gdelt_gkg_training import (
    SYNTHETIC_CONFIG_VERSION,
    TrainingConfig,
    _decision,
    feature_input,
    predict,
    train_and_evaluate,
    verify_artifact,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
SLOT = timedelta(minutes=15)
DATA_CONFIG = Config(
    START + 8 * HOUR,
    START + 364 * HOUR,
    Decimal("10"),
    "synthetic-cost-v1",
)


def at(hours):
    return START + hours * HOUR


def synthetic_dataset(*, future_override=None, gap_at=None, zero=False, count=90):
    price = Decimal("100")
    candles = []
    for index in range(count + 2):
        if index:
            price *= (Decimal("1.02"), Decimal("0.98"), Decimal("1.0005"))[index % 3]
        close = price * 2 if index == future_override else price
        candles.append(
            MarketCandle(
                f"synthetic-m-{index}",
                at(4 * index),
                at(4 * index + 4),
                at(4 * index + 4),
                format(close, "f"),
                f"{index + 1:064x}",
                "synthetic",
                SYNTHETIC_CONFIG_VERSION,
            )
        )
    releases, slots, scans = [], [], []
    for index in range(16 * count):
        batch = at(4) + index * SLOT
        seen = batch + SLOT
        scan_id = f"synthetic-scan-{index}"
        scans.append(Scan(scan_id, seen, batch, "ok", SOURCE_VERSION, SYNTHETIC_CONFIG_VERSION))
        slots.append(
            Slot(
                f"synthetic-slot-{index}",
                scan_id,
                batch,
                seen,
                "unknown" if index == gap_at else "ok",
                SOURCE_VERSION,
                SYNTHETIC_CONFIG_VERSION,
            )
        )
        btc = 0 if zero and (index // 16) % 7 == 0 else (index % 4) + 1
        releases.append(
            Release(
                f"synthetic-rev-{index}",
                scan_id,
                batch,
                seen,
                seen,
                1,
                100 + index % 9,
                btc,
                None if btc == 0 else str((index % 5) - 2),
                f"{index + 101:064x}",
                "a" * 32,
                1000,
                SOURCE_VERSION,
                SYNTHETIC_CONFIG_VERSION,
            )
        )
    data_config = replace(DATA_CONFIG, last_anchor=at(4 * (count + 1)))
    data = build_dataset(
        tuple(candles),
        tuple(releases),
        tuple(slots),
        tuple(scans),
        cutoff=at(4 * (count + 2)),
        config=data_config,
    )
    return data, data_config


@pytest.fixture(scope="module")
def cohort():
    return synthetic_dataset(zero=True)


def training_config(data):
    return TrainingConfig(7, at(160), at(280), at(364), data.source_checksum)


def test_deterministic_ablation_shared_windows_and_feature_only_inference(cohort):
    data, data_config = cohort
    config = training_config(data)
    first = train_and_evaluate(data, data_config, config)
    assert first == train_and_evaluate(data, data_config, config)
    assert first.status == "evaluated-synthetic"
    assert [a.variant for a in first.artifacts] == ["market-only", "gkg+market"]
    assert all(verify_artifact(a) for a in first.artifacts)
    changed_seed = train_and_evaluate(data, data_config, replace(config, seed=8))
    assert changed_seed.artifacts[0].weights != first.artifacts[0].weights
    assert changed_seed.artifacts[0].checksum != first.artifacts[0].checksum
    assert first.report["split"]["embargo_hours"] == 4
    assert first.report["split"]["train"] >= 24
    assert first.report["split"]["validation"] >= 8
    assert first.report["split"]["test"] >= 8
    assert all(
        first.report["models"][variant]["test"]["count"] == first.report["split"]["test"]
        for variant in ("market-only", "gkg+market")
    )
    for variant in first.report["models"].values():
        assert (
            sum(block["count"] for block in variant["rolling_heldout_test"])
            == (first.report["split"]["test"])
        )
        for name in ("validation", "test"):
            assert set(("brier", "log_loss", "calibration_ece_3bin", "wait_count")) <= (
                variant[name].keys()
            )
    zero_row = next(row for row in data.samples if row.tone_missing)
    assert zero_row.mean_btc_document_tone is None
    inputs = feature_input(zero_row, data)
    assert not hasattr(inputs, "label") and not hasattr(inputs, "label_event_id")
    assert predict(first.artifacts[1], inputs)[1] in {"UP", "DOWN", "NEUTRAL", "WAIT"}
    assert _decision((0.34, 0.33, 0.33), 0.4) == "WAIT"
    with pytest.raises(ValueError):
        predict(first.artifacts[1], zero_row)


def test_future_label_mutation_leaves_earlier_features_fit_threshold(cohort):
    data, data_config = cohort
    changed, _ = synthetic_dataset(future_override=75, zero=True)
    assert data.samples[0].feature_hash == changed.samples[0].feature_hash
    original = train_and_evaluate(data, data_config, training_config(data))
    modified = train_and_evaluate(changed, data_config, training_config(changed))
    assert modified.status == "evaluated-synthetic"
    for before, after in zip(original.artifacts, modified.artifacts, strict=True):
        assert before.weights == after.weights
        assert before.threshold == after.threshold
        assert before.checksum != after.checksum
    assert predict(original.artifacts[1], feature_input(data.samples[0], data)) == (
        predict(original.artifacts[1], feature_input(changed.samples[0], changed))
    )


def test_corruption_source_mismatch_gap_sparse_class_and_cost_wait(cohort):
    data, data_config = cohort
    config = training_config(data)
    assert train_and_evaluate(replace(data, checksum="0" * 64), data_config, config).reason == (
        "invalid_dataset_manifest"
    )
    assert train_and_evaluate(
        data, data_config, replace(config, expected_source_checksum="0" * 64)
    ).reason == ("source_checksum_mismatch")
    assert train_and_evaluate(data, replace(data_config, cost_version="wrong"), config).reason == (
        "dataset_config_mismatch"
    )
    assert (
        train_and_evaluate(data, data_config, replace(config, embargo=HOUR)).reason
        == "invalid_dataset_or_split"
    )
    assert train_and_evaluate(
        data, data_config, replace(config, slippage_bps=Decimal("3"))
    ).reason == ("cost_assumption_mismatch")
    gap, gap_config = synthetic_dataset(gap_at=100)
    assert train_and_evaluate(gap, gap_config, training_config(gap)).reason == "source_quality_wait"
    sparse, sparse_config = synthetic_dataset(count=12)
    assert train_and_evaluate(sparse, sparse_config, training_config(sparse)).reason == (
        "too_few_samples"
    )
    neutral = replace(data, samples=tuple(replace(row, label="NEUTRAL") for row in data.samples))
    # A label edit without resealing is corruption, not a valid class-poor cohort.
    assert train_and_evaluate(neutral, data_config, config).reason == "invalid_dataset_manifest"
    neutral = replace(
        neutral,
        checksum=_hash(
            {
                "schema": SCHEMA,
                "cutoff": neutral.cutoff,
                "source": neutral.source_checksum,
                "config": neutral.config_checksum,
                "versions": neutral.source_versions,
                "samples": tuple(asdict(row) for row in neutral.samples),
                "waits": tuple(asdict(wait) for wait in neutral.waits),
            }
        ),
    )
    assert train_and_evaluate(neutral, data_config, config).reason == "too_few_class_examples"
    artifact = train_and_evaluate(data, data_config, config).artifacts[1]
    tampered = replace(artifact, threshold="0.99")
    assert not verify_artifact(tampered)
    with pytest.raises(ValueError):
        predict(tampered, feature_input(data.samples[0], data))


def test_real_or_wrong_source_version_not_promoted(cohort):
    data, data_config = cohort
    wrong = replace(data, source_versions=("real",))
    outcome = train_and_evaluate(wrong, data_config, training_config(wrong))
    assert outcome.status == "collect-only"
    assert outcome.reason == "invalid_or_non_synthetic_provenance"
