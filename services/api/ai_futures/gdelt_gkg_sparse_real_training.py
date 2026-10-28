"""Fail-closed persisted sparse cohort inspection; never fit a real artifact."""

from dataclasses import asdict
from datetime import timedelta

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_dataset import HORIZON, _hash, _utc
from ai_futures.gdelt_gkg_sparse import SOURCE_VERSION
from ai_futures.gdelt_gkg_sparse_dataset import SCHEMA, Config, verify_dataset
from ai_futures.gdelt_gkg_sparse_export import export_sparse_dataset
from ai_futures.gdelt_gkg_sparse_training import (
    MIN_COUNTS,
    TrainingConfig,
    TrainingOutcome,
    _split,
)
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION


async def evaluate_persisted_sparse(
    database: Database,
    settings: Settings,
    *,
    dataset_config: Config,
    training_config: TrainingConfig,
) -> TrainingOutcome:
    """Inspect a disposable copy; missing real cost/rules proof always blocks fit."""
    exported = await export_sparse_dataset(
        database,
        settings,
        cutoff=dataset_config.last_anchor + HORIZON + timedelta(minutes=15),
        config=dataset_config,
    )
    dataset = exported.dataset
    waits = {}
    for wait in dataset.waits:
        waits[wait.reason] = waits.get(wait.reason, 0) + 1
    report = {
        "status": "collect-only",
        "dataset_checksum": dataset.checksum,
        "source_checksum": dataset.source_checksum,
        "persisted_export_checksum": exported.checksum,
        "export_schema": "persisted-gkg-sparse-market-export-v1",
        "attempt_ids": exported.attempt_ids,
        "result_ids": exported.result_ids,
        "market_observation_ids": exported.market_observation_ids,
        "source_versions": dataset.source_versions,
        "source_waits": waits,
        "sample_count": len(dataset.samples),
        "proposed_cost_bps": str(dataset_config.cost_bps),
        "cost_version_claim": dataset_config.cost_version,
        "training_config": asdict(training_config),
        "promotion_verdict": "collect-only",
    }

    def collect(reason):
        return TrainingOutcome("collect-only", reason, (), report | {"reason": reason})

    if not verify_dataset(dataset) or dataset.config_checksum != _hash(asdict(dataset_config)):
        return collect("invalid_dataset_manifest")
    if dataset.source_checksum != training_config.expected_source_checksum:
        return collect("source_checksum_mismatch")
    if _utc(training_config.test_end) > _utc(dataset_config.last_anchor):
        return collect("invalid_dataset_or_split")
    if (
        SCHEMA not in dataset.source_versions
        or SOURCE_VERSION not in dataset.source_versions
        or MARKET_CANDLE_SOURCE_VERSION not in dataset.source_versions
        or not any(
            value.startswith(f"{MARKET_CANDLE_SOURCE_VERSION}:")
            for value in dataset.source_versions
        )
        or any(
            "synthetic" in value.lower() or "title" in value.lower()
            for value in dataset.source_versions
        )
    ):
        return collect("invalid_real_source_provenance")
    if waits or not dataset.samples:
        return collect("incomplete_source_cohort")
    attempts = set(exported.attempt_ids)
    results = set(exported.result_ids)
    market = set(exported.market_observation_ids)
    if any(
        row.attempt_id not in attempts
        or row.result_id not in results
        or not set((*row.market_observation_ids, row.label_observation_id)) <= market
        for row in dataset.samples
    ):
        return collect("unlinked_persisted_provenance")
    try:
        train, validation, test = _split(dataset, training_config)
    except (TypeError, ValueError):
        return collect("invalid_dataset_or_split")
    report["split"] = {"train": len(train), "validation": len(validation), "test": len(test)}
    if any(
        len(rows) < minimum
        for rows, minimum in zip(
            (train, validation, test),
            MIN_COUNTS,
            strict=True,
        )
    ):
        return collect("too_few_samples")
    report["missing_provenance"] = (
        "effective_observed_instrument_rules",
        "account_fee_funding_slippage_schedule",
        "real_source_compatible_training_freeze",
    )
    return collect("missing_cost_rules_provenance")
