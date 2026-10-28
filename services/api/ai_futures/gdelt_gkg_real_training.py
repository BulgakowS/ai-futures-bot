"""Fail-closed, offline eligibility check for persisted GKG/M1 training."""

from dataclasses import asdict
from datetime import timedelta

from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_dataset import (
    HORIZON,
    SCHEMA,
    Config,
    _hash,
    chronological_split,
    verify_dataset,
)
from ai_futures.gdelt_gkg_export import export_persisted_gkg_dataset
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION
from ai_futures.gdelt_gkg_training import (
    BLOCKING_WAITS,
    MIN_COUNTS,
    TrainingConfig,
    TrainingOutcome,
)
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION

LABEL_DELAY = timedelta(minutes=15)


async def evaluate_persisted_real_gkg(
    database: Database,
    settings: Settings,
    *,
    dataset_config: Config,
    training_config: TrainingConfig,
) -> TrainingOutcome:
    """Inspect a disposable persisted cohort; never issue a model or trading approval.

    Config.cost_bps is a proposed label threshold, not proof of a historical
    fee/funding/slippage schedule. Until an immutable effective/observed cost
    source exists, fitting those labels would make a false real-source claim.
    """
    exported = await export_persisted_gkg_dataset(
        database,
        settings,
        cutoff=dataset_config.last_anchor + HORIZON + LABEL_DELAY,
        config=dataset_config,
        label_observation_delay=LABEL_DELAY,
    )
    dataset = exported.dataset
    waits: dict[str, int] = {}
    for wait in dataset.waits:
        waits[wait.reason] = waits.get(wait.reason, 0) + 1
    report = {
        "status": "collect-only",
        "dataset_checksum": dataset.checksum,
        "source_checksum": dataset.source_checksum,
        "persisted_export_checksum": exported.checksum,
        "export_schema": "persisted-gkg-market-export-v3",
        "source_versions": dataset.source_versions,
        "snapshot_revision_ids": exported.snapshot_revision_ids,
        "snapshot_slot_observation_ids": exported.snapshot_observation_ids,
        "market_observation_ids": exported.market_observation_ids,
        "market_rules_versions": exported.market_rules_versions,
        "source_waits": waits,
        "sample_count": len(dataset.samples),
        "label_cutoff_rule": "target_close_at+15m",
        "proposed_cost_bps": str(dataset_config.cost_bps),
        "cost_version_claim": dataset_config.cost_version,
        "training_config": asdict(training_config),
        "promotion_verdict": "collect-only",
    }

    def collect(reason: str) -> TrainingOutcome:
        return TrainingOutcome("collect-only", reason, (), report | {"reason": reason})

    if not verify_dataset(dataset) or dataset.config_checksum != _hash(asdict(dataset_config)):
        return collect("invalid_dataset_manifest")
    if training_config.test_end > dataset_config.last_anchor:
        return collect("invalid_dataset_or_split")
    if dataset.source_checksum != training_config.expected_source_checksum:
        return collect("source_checksum_mismatch")
    if (
        dataset_config.feature_version != SCHEMA
        or dataset_config.source_version != SOURCE_VERSION
        or SCHEMA not in dataset.source_versions
        or SOURCE_VERSION not in dataset.source_versions
        or not any(
            value.startswith(f"{MARKET_CANDLE_SOURCE_VERSION}:")
            for value in dataset.source_versions
        )
        or any(
            "synthetic" in value.lower() or "title" in value.lower() or "doc-" in value.lower()
            for value in dataset.source_versions
        )
        or not exported.snapshot_revision_ids
        or not exported.snapshot_observation_ids
        or not exported.market_observation_ids
        or not exported.market_rules_versions
    ):
        return collect("invalid_real_source_provenance")
    if exported.market_corrections or exported.gkg_corrections:
        return collect("corrected_source_wait")
    if exported.late_releases or any(reason in BLOCKING_WAITS for reason in waits):
        return collect("source_quality_wait")
    if waits:
        return collect("incomplete_source_cohort")
    revision_ids = set(exported.snapshot_revision_ids)
    slot_ids = set(exported.snapshot_observation_ids)
    market_ids = set(exported.market_observation_ids)
    if any(
        not set(row.release_revision_ids) <= revision_ids
        or not set(row.slot_observation_ids) <= slot_ids
        or not set((*row.market_event_ids, row.label_event_id)) <= market_ids
        for row in dataset.samples
    ):
        return collect("unlinked_persisted_provenance")
    try:
        train, validation, test = chronological_split(
            dataset,
            train_end=training_config.train_end,
            validation_end=training_config.validation_end,
            test_end=training_config.test_end,
            embargo=training_config.embargo,
        )
    except (TypeError, ValueError):
        return collect("invalid_dataset_or_split")
    report["split"] = {"train": len(train), "validation": len(validation), "test": len(test)}
    if any(
        len(rows) < minimum
        for rows, minimum in zip((train, validation, test), MIN_COUNTS, strict=True)
    ):
        return collect("too_few_samples")
    # M1 stores a rules_version string, but not the effective/observed rules
    # schedule or historical cost components. Neither field can be inferred
    # from Config.cost_bps or today's exchange metadata.
    report["missing_provenance"] = (
        "effective_observed_instrument_rules",
        "immutable_fee_funding_slippage_schedule",
    )
    return collect("missing_cost_provenance")
