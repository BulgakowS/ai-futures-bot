"""Synthetic-only sparse one-slot model contract and leakage regression."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ai_futures.gdelt_gkg_sparse import RESERVE_BYTES, SOURCE_VERSION
from ai_futures.gdelt_gkg_sparse_dataset import (
    Attempt,
    Config,
    Market,
    Result,
    build_dataset,
)
from ai_futures.gdelt_gkg_sparse_real_training import evaluate_persisted_sparse
from ai_futures.gdelt_gkg_sparse_training import (
    SYNTHETIC_CONFIG_VERSION,
    SYNTHETIC_RULES_VERSION,
    TrainingConfig,
    feature_input,
    freeze_training_manifest,
    predict,
    predict_as_of,
    project_as_of_features,
    train_and_evaluate,
    verify_artifact,
    verify_frozen_manifest,
)
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION

FIRST = datetime(2026, 9, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def cohort(count=56):
    anchors = tuple(FIRST + i * 4 * HOUR for i in range(count))
    attempts = tuple(
        Attempt(
            f"a{i}",
            anchor,
            anchor - 15 * MINUTE,
            anchor + MINUTE,
            RESERVE_BYTES,
            SOURCE_VERSION,
            SYNTHETIC_CONFIG_VERSION,
        )
        for i, anchor in enumerate(anchors)
    )
    results = tuple(
        Result(
            f"r{i}",
            f"a{i}",
            anchor + 2 * MINUTE,
            "ok",
            None,
            100,
            f"{i + 1:064x}",
            10,
            0 if i % 5 == 0 else i % 4 + 1,
            None if i % 5 == 0 else "-0.5",
            SOURCE_VERSION,
            SYNTHETIC_CONFIG_VERSION,
        )
        for i, anchor in enumerate(anchors)
    )
    market = tuple(
        Market(
            f"m{j}",
            f"c{j}",
            FIRST + j * 4 * HOUR,
            1,
            FIRST + (j + 1) * 4 * HOUR + MINUTE,
            FIRST + (j + 1) * 4 * HOUR + MINUTE,
            FIRST + (j + 1) * 4 * HOUR,
            str((100, 102, 102)[j % 3]),
            f"{j + 3:064x}",
            MARKET_CANDLE_SOURCE_VERSION,
            SYNTHETIC_CONFIG_VERSION,
            SYNTHETIC_RULES_VERSION,
        )
        for j in range(-2, count)
    )
    data_config = Config(anchors[0], anchors[-1], Decimal("10"), "scenario-v1")
    data = build_dataset(
        attempts, results, market, cutoff=anchors[-1] + 4 * HOUR + 15 * MINUTE, config=data_config
    )
    training = TrainingConfig(
        anchors[27],
        anchors[42],
        anchors[55],
        data.source_checksum,
    )
    return data, data_config, training, attempts, results, market


def test_synthetic_pair_reproducible_and_feature_only():
    data, data_config, training, *_ = cohort()
    one = train_and_evaluate(data, data_config, training)
    two = train_and_evaluate(data, data_config, training)
    assert one.status == "evaluated-synthetic"
    assert one.artifacts == two.artifacts
    assert len(one.artifacts) == 2
    assert {a.variant for a in one.artifacts} == {"market-only", "sparse-gkg+market"}
    assert all(verify_artifact(a) for a in one.artifacts)
    assert one.report["split"]["train"] >= 24
    assert one.report["split"]["validation"] >= 8
    assert one.report["split"]["test"] >= 8
    assert one.report["paired_test"]["same_anchor_set"]
    features = feature_input(data.samples[0], data)
    assert "label" not in features.__dataclass_fields__
    assert "target_close_at" not in features.__dataclass_fields__
    assert "label_observation_id" not in features.__dataclass_fields__
    assert features.tone_missing and features.mean_btc_document_tone is None
    assert len(predict(one.artifacts[1], features)[0]) == 3
    with pytest.raises(ValueError):
        predict(one.artifacts[1], replace(features, source_checksum="0" * 64))
    with pytest.raises(ValueError):
        predict(one.artifacts[1], data.samples[0])


def test_test_label_changes_do_not_refit_or_reselect_threshold():
    data, data_config, training, attempts, results, market = cohort()
    base = train_and_evaluate(data, data_config, training)
    # Change only the final label candle, after all train and validation anchors.
    final = replace(market[-1], close="80", payload_hash="f" * 64)
    changed = build_dataset(
        attempts, results, market[:-1] + (final,), cutoff=data.cutoff, config=data_config
    )
    later = train_and_evaluate(
        changed, data_config, replace(training, expected_source_checksum=changed.source_checksum)
    )
    assert later.status == "evaluated-synthetic"
    assert changed.samples[0].feature_hash == data.samples[0].feature_hash
    assert [(a.weights, a.threshold) for a in later.artifacts] == [
        (a.weights, a.threshold) for a in base.artifacts
    ]


def test_tamper_source_version_wait_and_embargo_fail_closed():
    data, data_config, training, *_ = cohort()
    assert (
        train_and_evaluate(replace(data, checksum="0" * 64), data_config, training).reason
        == "invalid_dataset_manifest"
    )
    assert (
        train_and_evaluate(replace(data, source_versions=("real",)), data_config, training).reason
        == "invalid_or_non_synthetic_provenance"
    )
    assert (
        train_and_evaluate(
            data, data_config, replace(training, expected_source_checksum="0" * 64)
        ).reason
        == "source_checksum_mismatch"
    )
    assert (
        train_and_evaluate(data, data_config, replace(training, embargo=timedelta())).reason
        == "invalid_dataset_or_split"
    )
    assert (
        train_and_evaluate(data, data_config, replace(training, seed=7)).reason
        == "invalid_or_non_synthetic_provenance"
    )
    missing = replace(
        data,
        samples=data.samples[1:],
        waits=data.waits + (type("W", (), {"anchor": FIRST, "reason": "sparse_claim_unknown"})(),),
    )
    # A forged ad hoc wait cannot preserve the portable seal.
    assert train_and_evaluate(missing, data_config, training).reason == "invalid_dataset_manifest"
    artifact = train_and_evaluate(data, data_config, training).artifacts[1]
    assert not verify_artifact(replace(artifact, threshold="0.99"))


def test_missing_sparse_slot_is_not_zero():
    data, data_config, training, attempts, results, market = cohort()
    missing = build_dataset(attempts, results[1:], market, cutoff=data.cutoff, config=data_config)
    assert missing.waits[0].reason == "sparse_claim_unknown"
    assert missing.samples[0].anchor == FIRST + 4 * HOUR
    assert (
        train_and_evaluate(
            missing,
            data_config,
            replace(training, expected_source_checksum=missing.source_checksum),
        ).reason
        == "source_quality_wait"
    )


def test_future_as_of_projection_uses_frozen_training_manifest_without_new_dataset_checksum():
    data, data_config, training, attempts, results, market = cohort()
    outcome = train_and_evaluate(data, data_config, training)
    manifest = freeze_training_manifest(outcome)
    assert verify_frozen_manifest(manifest)
    anchor = FIRST + 56 * 4 * HOUR
    claim = replace(
        attempts[-1],
        attempt_id="future-claim",
        anchor_at=anchor,
        target_batch_at=anchor - 15 * MINUTE,
        attempted_at=anchor + MINUTE,
    )
    result = replace(
        results[-1],
        result_id="future-result",
        attempt_id=claim.attempt_id,
        first_seen_at=anchor + 2 * MINUTE,
    )
    projected = project_as_of_features(claim, result, market[-2:], as_of=anchor + 3 * MINUTE)
    assert "label" not in projected.__dataclass_fields__
    assert "target_close_at" not in projected.__dataclass_fields__
    assert "dataset_checksum" not in projected.__dataclass_fields__
    assert {a.checksum for a in outcome.artifacts} == {v for _, v in manifest.artifact_checksums}
    # A matured label creates a different future dataset, but cannot change the as-of projection.
    label = replace(
        market[-1],
        observation_id="future-label",
        causal_id="future-label-causal",
        window_start=anchor,
        first_seen_at=anchor + 4 * HOUR + MINUTE,
        observed_at=anchor + 4 * HOUR + MINUTE,
        source_time=anchor + 4 * HOUR,
    )
    later = build_dataset(
        attempts + (claim,),
        results + (result,),
        market + (label,),
        cutoff=anchor + 5 * HOUR,
        config=replace(data_config, last_anchor=anchor),
    )
    assert later.checksum != data.checksum
    assert later.source_checksum != data.source_checksum
    assert projected.feature_hash == later.samples[-1].feature_hash
    assert projected.market_feature_hash == later.samples[-1].market_feature_hash
    assert projected.aggregate_feature_hash == later.samples[-1].aggregate_feature_hash
    assert len(predict_as_of(outcome.artifacts[1], manifest, projected)[0]) == 3
    with pytest.raises(ValueError):
        predict(outcome.artifacts[1], feature_input(later.samples[-1], later))


def test_future_as_of_rejects_tamper_stale_unknown_and_non_synthetic_source():
    data, data_config, training, attempts, results, market = cohort()
    outcome = train_and_evaluate(data, data_config, training)
    manifest = freeze_training_manifest(outcome)
    anchor = FIRST + 56 * 4 * HOUR
    claim = replace(
        attempts[-1],
        attempt_id="future-claim",
        anchor_at=anchor,
        target_batch_at=anchor - 15 * MINUTE,
        attempted_at=anchor + MINUTE,
    )
    result = replace(
        results[-1],
        result_id="future-result",
        attempt_id=claim.attempt_id,
        first_seen_at=anchor + 2 * MINUTE,
    )
    source_market = market[-2:]
    row = project_as_of_features(claim, result, source_market, as_of=anchor + 3 * MINUTE)
    artifact = outcome.artifacts[1]
    for bad in (
        replace(row, source_schema="unknown"),
        replace(row, feature_hash="0" * 64),
        replace(row, source_versions=("real",)),
        replace(row, anchor=artifact.test_end),
        data.samples[-1],
    ):
        with pytest.raises(ValueError):
            predict_as_of(artifact, manifest, bad)
    for bad in (
        replace(manifest, checksum="0" * 64),
        replace(manifest, source_checksum="0" * 64),
    ):
        with pytest.raises(ValueError):
            predict_as_of(artifact, bad, row)
    with pytest.raises(ValueError):
        predict_as_of(replace(artifact, weights=(("0",),) * 3), manifest, row)
    for bad_claim, bad_result, bad_market, as_of in (
        (claim, result, source_market, anchor + MINUTE),
        (claim, replace(result, status="error"), source_market, anchor + 3 * MINUTE),
        (claim, replace(result, config_version="real"), source_market, anchor + 3 * MINUTE),
        (claim, result, source_market[:1], anchor + 3 * MINUTE),
        (
            claim,
            result,
            (source_market[0], replace(source_market[1], version=2)),
            anchor + 3 * MINUTE,
        ),
        (claim, result, source_market, anchor + 16 * MINUTE),
    ):
        with pytest.raises(ValueError):
            project_as_of_features(bad_claim, bad_result, bad_market, as_of=as_of)


@pytest.mark.asyncio
async def test_complete_mock_persisted_cohort_still_has_no_real_artifacts(monkeypatch):
    _, data_config, training, attempts, results, market = cohort()
    config_version = "real-sparse-v1"
    real_attempts = tuple(replace(row, config_version=config_version) for row in attempts)
    real_results = tuple(replace(row, config_version=config_version) for row in results)
    real_market = tuple(
        replace(row, config_version=config_version, rules_version="real-rules-v1") for row in market
    )
    real_config = replace(data_config, cost_provenance_frozen=True)
    dataset = build_dataset(
        real_attempts,
        real_results,
        real_market,
        cutoff=FIRST + 56 * 4 * HOUR + 15 * MINUTE,
        config=real_config,
    )
    export = SimpleNamespace(
        dataset=dataset,
        checksum="a" * 64,
        attempt_ids=tuple(row.attempt_id for row in real_attempts),
        result_ids=tuple(row.result_id for row in real_results),
        market_observation_ids=tuple(row.observation_id for row in real_market),
    )

    async def fake_export(*args, **kwargs):
        return export

    monkeypatch.setattr(
        "ai_futures.gdelt_gkg_sparse_real_training.export_sparse_dataset",
        fake_export,
    )
    outcome = await evaluate_persisted_sparse(
        None,
        None,
        dataset_config=real_config,
        training_config=replace(training, expected_source_checksum=dataset.source_checksum),
    )
    assert outcome.status == "collect-only"
    assert outcome.reason == "missing_cost_rules_provenance"
    assert outcome.artifacts == ()
    assert outcome.report["split"]["train"] >= 24
