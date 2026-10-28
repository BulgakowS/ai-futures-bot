"""Deterministic synthetic-only one-slot sparse GKG classification research."""

import hashlib
import math
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path

from ai_futures.gdelt_dataset import HEX64, HORIZON, _aligned, _decimal, _hash, _utc
from ai_futures.gdelt_gkg_sparse import RESERVE_BYTES, SOURCE_VERSION
from ai_futures.gdelt_gkg_sparse_dataset import (
    SCHEMA,
    SLOT,
    Attempt,
    Config,
    Dataset,
    Market,
    Result,
    Sample,
    verify_dataset,
)
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION

ALGORITHM = "sparse-gkg-multinomial-sgd-v1"
CODE_VERSION = "s11-g7d-v1"
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEPENDENCY_VERSIONS = (
    f"python-{platform.python_version()}",
    *(f"{name}-{version(name)}" for name in ("SQLAlchemy", "pydantic")),
    f"sparse_dataset:{hashlib.sha256(Path(__file__).with_name('gdelt_gkg_sparse_dataset.py').read_bytes()).hexdigest()}",
)
CLASSES = ("DOWN", "NEUTRAL", "UP")
VARIANTS = ("market-only", "sparse-gkg+market")
SYNTHETIC_CONFIG_VERSION = "synthetic-sparse-fixture-v1"
SYNTHETIC_RULES_VERSION = "synthetic-rules-v1"
EXPECTED_VERSIONS = tuple(
    sorted(
        (
            SCHEMA,
            SOURCE_VERSION,
            MARKET_CANDLE_SOURCE_VERSION,
            f"{SOURCE_VERSION}:{SYNTHETIC_CONFIG_VERSION}",
            f"{MARKET_CANDLE_SOURCE_VERSION}:{SYNTHETIC_CONFIG_VERSION}:{SYNTHETIC_RULES_VERSION}",
        )
    )
)
SEED = 20261019
EPOCHS = 120
LEARNING_RATE = 0.03
L2 = 0.001
THRESHOLDS = (0.40, 0.50, 0.60, 0.70)
MIN_COUNTS = (24, 8, 8)


@dataclass(frozen=True)
class TrainingConfig:
    train_end: datetime
    validation_end: datetime
    test_end: datetime
    expected_source_checksum: str
    seed: int = SEED
    embargo: timedelta = HORIZON
    fee_bps: Decimal = Decimal("4")
    funding_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("2")


@dataclass(frozen=True)
class FeatureInput:
    anchor: datetime
    decision_at: datetime
    market_observation_ids: tuple[str, str]
    market_payload_hashes: tuple[str, str]
    market_rules_versions: tuple[str, str]
    market_config_versions: tuple[str, str]
    market_return_bps: str
    attempt_id: str
    result_id: str
    result_config_version: str
    target_batch_at: datetime
    archive_sha256: str
    total_rows: int
    btc_rows: int
    btc_share: str
    mean_btc_document_tone: str | None
    tone_missing: bool
    coverage_slots: int
    market_feature_hash: str
    aggregate_feature_hash: str
    feature_hash: str
    dataset_checksum: str
    dataset_config_checksum: str
    source_checksum: str
    source_versions: tuple[str, ...]


@dataclass(frozen=True)
class AsOfFeatureInput:
    """One future anchor, with no dataset, label, or target-candle fields."""

    anchor: datetime
    decision_at: datetime
    market_observation_ids: tuple[str, str]
    market_payload_hashes: tuple[str, str]
    market_rules_versions: tuple[str, str]
    market_config_versions: tuple[str, str]
    market_return_bps: str
    attempt_id: str
    result_id: str
    result_config_version: str
    target_batch_at: datetime
    archive_sha256: str
    total_rows: int
    btc_rows: int
    btc_share: str
    mean_btc_document_tone: str | None
    tone_missing: bool
    coverage_slots: int
    market_feature_hash: str
    aggregate_feature_hash: str
    feature_hash: str
    source_schema: str
    source_versions: tuple[str, ...]


AS_OF_FIELDS = (
    "anchor",
    "decision_at",
    "market_observation_ids",
    "market_payload_hashes",
    "market_rules_versions",
    "market_config_versions",
    "market_return_bps",
    "attempt_id",
    "result_id",
    "result_config_version",
    "target_batch_at",
    "archive_sha256",
    "total_rows",
    "btc_rows",
    "btc_share",
    "mean_btc_document_tone",
    "tone_missing",
    "coverage_slots",
    "market_feature_hash",
    "aggregate_feature_hash",
    "feature_hash",
)


@dataclass(frozen=True)
class ModelArtifact:
    variant: str
    algorithm: str
    code_version: str
    code_checksum: str
    dependency_versions: tuple[str, ...]
    dataset_checksum: str
    dataset_config_checksum: str
    source_checksum: str
    source_versions: tuple[str, ...]
    training_config_checksum: str
    seed: int
    train_end: datetime
    validation_end: datetime
    test_end: datetime
    embargo_seconds: int
    feature_names: tuple[str, ...]
    weights: tuple[tuple[str, ...], ...]
    threshold: str
    checksum: str


@dataclass(frozen=True)
class FrozenTrainingManifest:
    """Caller-held training seal; a future row has its own source observations."""

    source_schema: str
    dataset_checksum: str
    dataset_config_checksum: str
    source_checksum: str
    source_versions: tuple[str, ...]
    training_config_checksum: str
    artifact_checksums: tuple[tuple[str, str], ...]
    checksum: str


@dataclass(frozen=True)
class TrainingOutcome:
    status: str
    reason: str
    artifacts: tuple[ModelArtifact, ...]
    report: dict


def feature_input(row: Sample, dataset: Dataset) -> FeatureInput:
    """Copy the predeclared as-of fields; label and target are absent by type."""
    if not verify_dataset(dataset) or row not in dataset.samples:
        raise ValueError("Invalid sparse dataset or sample")
    return FeatureInput(
        **{name: getattr(row, name) for name in AS_OF_FIELDS},
        dataset_checksum=dataset.checksum,
        dataset_config_checksum=dataset.config_checksum,
        source_checksum=dataset.source_checksum,
        source_versions=dataset.source_versions,
    )


def project_as_of_features(
    attempt: Attempt,
    result: Result,
    market: tuple[Market, Market],
    *,
    as_of: datetime,
) -> AsOfFeatureInput:
    """Project one synthetic future anchor from visible G7a/M1 records only."""
    try:
        if type(attempt) is not Attempt or type(result) is not Result or type(market) is not tuple:
            raise ValueError("Invalid sparse source records")
        anchor = _utc(attempt.anchor_at)
        as_of = _utc(as_of)
        if (
            not _aligned(anchor, HORIZON)
            or not anchor <= as_of <= anchor + SLOT
            or attempt.target_batch_at != anchor - SLOT
            or not anchor <= _utc(attempt.attempted_at) <= as_of
            or attempt.reserved_bytes != RESERVE_BYTES
            or attempt.source_version != SOURCE_VERSION
            or attempt.config_version != SYNTHETIC_CONFIG_VERSION
            or not attempt.attempt_id
            or type(result.response_bytes) is not int
            or not 0 < result.response_bytes <= RESERVE_BYTES
            or result.attempt_id != attempt.attempt_id
            or not result.result_id
            or result.status != "ok"
            or result.error_code is not None
            or result.source_version != SOURCE_VERSION
            or result.config_version != SYNTHETIC_CONFIG_VERSION
            or not attempt.attempted_at <= _utc(result.first_seen_at) <= as_of
            or not isinstance(result.archive_sha256, str)
            or not HEX64.fullmatch(result.archive_sha256)
            or type(result.total_rows) is not int
            or result.total_rows <= 0
            or type(result.btc_rows) is not int
            or not 0 <= result.btc_rows <= result.total_rows
            or (result.btc_rows == 0) != (result.mean_btc_document_tone is None)
            or len(market) != 2
            or any(type(row) is not Market for row in market)
        ):
            raise ValueError("Missing, stale, or non-synthetic sparse source")
        if result.btc_rows:
            tone = Decimal(result.mean_btc_document_tone)
            if (
                not tone.is_finite()
                or len(tone.as_tuple().digits) > 64
                or abs(tone.adjusted()) > 30
            ):
                raise ValueError("Invalid sparse tone")
        before, now = market
        for row, window in zip(market, (anchor - 2 * HORIZON, anchor - HORIZON), strict=True):
            if (
                row.window_start != window
                or row.version != 1
                or not row.observation_id
                or not row.causal_id
                or row.source_version != MARKET_CANDLE_SOURCE_VERSION
                or row.source != "rest"
                or row.symbol != "BTC_USDT"
                or row.timeframe != "4h"
                or row.config_version != SYNTHETIC_CONFIG_VERSION
                or row.rules_version != SYNTHETIC_RULES_VERSION
                or not isinstance(row.payload_hash, str)
                or not HEX64.fullmatch(row.payload_hash)
                or _utc(row.first_seen_at) != _utc(row.observed_at)
                or not window + HORIZON <= _utc(row.source_time) <= as_of
                or not window + HORIZON <= row.observed_at <= as_of
            ):
                raise ValueError("Missing, revised, or stale market source")
            _decimal(row.close, positive=True)
        if before.observation_id == now.observation_id:
            raise ValueError("Duplicate market observation")
        decision_at = max(
            anchor,
            result.first_seen_at,
            *(row.observed_at for row in market),
            *(row.source_time for row in market),
        )
        if decision_at > as_of:
            raise ValueError("Decision is not yet observable")
        m = {
            "anchor": anchor,
            "decision_at": decision_at,
            "market_observation_ids": (before.observation_id, now.observation_id),
            "market_payload_hashes": (before.payload_hash, now.payload_hash),
            "market_rules_versions": (before.rules_version, now.rules_version),
            "market_config_versions": (before.config_version, now.config_version),
            "market_source_version": MARKET_CANDLE_SOURCE_VERSION,
            "market_return_bps": format(
                (_decimal(now.close) / _decimal(before.close) - 1) * 10000, "f"
            ),
        }
        a = {
            "anchor": anchor,
            "decision_at": decision_at,
            "attempt_id": attempt.attempt_id,
            "result_id": result.result_id,
            "result_config_version": result.config_version,
            "source_version": SOURCE_VERSION,
            "target_batch_at": attempt.target_batch_at,
            "archive_sha256": result.archive_sha256,
            "total_rows": result.total_rows,
            "btc_rows": result.btc_rows,
            "btc_share": format(Decimal(result.btc_rows) / Decimal(result.total_rows), "f"),
            "mean_btc_document_tone": result.mean_btc_document_tone,
            "tone_missing": result.btc_rows == 0,
            "coverage_slots": 1,
        }
        return AsOfFeatureInput(
            **{name: (m | a)[name] for name in AS_OF_FIELDS if name in m or name in a},
            market_feature_hash=_hash(m),
            aggregate_feature_hash=_hash(a),
            feature_hash=_hash({"market": m, "aggregate": a}),
            source_schema=SCHEMA,
            source_versions=EXPECTED_VERSIONS,
        )
    except (AttributeError, InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid sparse as-of source") from exc


def _valid_features(row: FeatureInput | AsOfFeatureInput) -> bool:
    try:
        if (
            type(row) not in (FeatureInput, AsOfFeatureInput)
            or row.source_versions != EXPECTED_VERSIONS
            or (type(row) is AsOfFeatureInput and row.source_schema != SCHEMA)
            or _utc(row.anchor) != row.anchor
            or _utc(row.decision_at) != row.decision_at
            or not _aligned(row.anchor, HORIZON)
            or not row.anchor <= row.decision_at <= row.anchor + SLOT
            or row.target_batch_at != row.anchor - SLOT
            or row.coverage_slots != 1
            or len(row.market_observation_ids) != 2
            or len(row.market_payload_hashes) != 2
            or len(row.market_rules_versions) != 2
            or len(row.market_config_versions) != 2
            or any(not value for value in row.market_observation_ids)
            or row.market_observation_ids[0] == row.market_observation_ids[1]
            or any(not HEX64.fullmatch(value) for value in row.market_payload_hashes)
            or not row.attempt_id
            or not row.result_id
            or row.result_config_version != SYNTHETIC_CONFIG_VERSION
            or any(value != SYNTHETIC_CONFIG_VERSION for value in row.market_config_versions)
            or any(value != SYNTHETIC_RULES_VERSION for value in row.market_rules_versions)
            or row.total_rows <= 0
            or not 0 <= row.btc_rows <= row.total_rows
            or type(row.total_rows) is not int
            or type(row.btc_rows) is not int
            or type(row.tone_missing) is not bool
            or row.tone_missing != (row.btc_rows == 0)
            or (row.mean_btc_document_tone is None) != row.tone_missing
            or not HEX64.fullmatch(row.archive_sha256)
            or (type(row) is FeatureInput and not HEX64.fullmatch(row.dataset_checksum))
            or (type(row) is FeatureInput and not HEX64.fullmatch(row.dataset_config_checksum))
            or (type(row) is FeatureInput and not HEX64.fullmatch(row.source_checksum))
        ):
            return False
        share = Decimal(row.btc_share)
        ret = Decimal(row.market_return_bps)
        tone = None if row.tone_missing else Decimal(row.mean_btc_document_tone)
        market = {
            "anchor": row.anchor,
            "decision_at": row.decision_at,
            "market_observation_ids": row.market_observation_ids,
            "market_payload_hashes": row.market_payload_hashes,
            "market_rules_versions": row.market_rules_versions,
            "market_config_versions": row.market_config_versions,
            "market_source_version": MARKET_CANDLE_SOURCE_VERSION,
            "market_return_bps": row.market_return_bps,
        }
        aggregate = {
            "anchor": row.anchor,
            "decision_at": row.decision_at,
            "attempt_id": row.attempt_id,
            "result_id": row.result_id,
            "result_config_version": row.result_config_version,
            "source_version": SOURCE_VERSION,
            "target_batch_at": row.target_batch_at,
            "archive_sha256": row.archive_sha256,
            "total_rows": row.total_rows,
            "btc_rows": row.btc_rows,
            "btc_share": row.btc_share,
            "mean_btc_document_tone": row.mean_btc_document_tone,
            "tone_missing": row.tone_missing,
            "coverage_slots": 1,
        }
        return (
            share.is_finite()
            and ret.is_finite()
            and (tone is None or tone.is_finite())
            and (tone is None or (len(tone.as_tuple().digits) <= 64 and abs(tone.adjusted()) <= 30))
            and share == Decimal(row.btc_rows) / Decimal(row.total_rows)
            and row.market_feature_hash == _hash(market)
            and row.aggregate_feature_hash == _hash(aggregate)
            and row.feature_hash == _hash({"market": market, "aggregate": aggregate})
        )
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return False


def _feature_names(variant: str) -> tuple[str, ...]:
    base = ("bias", "market_return_bps/100")
    return (
        base
        if variant == "market-only"
        else base
        + (
            "btc_rows/10",
            "btc_share",
            "mean_btc_document_tone/10",
            "tone_missing",
        )
    )


def _features(row: FeatureInput, variant: str) -> tuple[float, ...]:
    market = float(max(Decimal("-10"), min(Decimal("10"), Decimal(row.market_return_bps) / 100)))
    if variant == "market-only":
        return (1.0, market)
    tone = (
        0.0
        if row.tone_missing
        else float(
            max(
                Decimal("-10"),
                min(Decimal("10"), Decimal(row.mean_btc_document_tone) / 10),
            )
        )
    )
    return (
        1.0,
        market,
        min(row.btc_rows, 100) / 10,
        float(Decimal(row.btc_share)),
        tone,
        float(row.tone_missing),
    )


def _softmax(logits: tuple[float, ...]) -> tuple[float, ...]:
    top = max(logits)
    shifted = tuple(math.exp(value - top) for value in logits)
    total = sum(shifted)
    return tuple(value / total for value in shifted)


def _fit(rows: tuple[FeatureInput, ...], labels: tuple[str, ...], variant: str):
    width = len(_feature_names(variant))
    weights = []
    for klass in range(3):
        vector = []
        for feature in range(width):
            digest = hashlib.sha256(f"{SEED}:{variant}:{klass}:{feature}".encode()).digest()
            vector.append((int.from_bytes(digest[:8]) / 2**64 - 0.5) / 100)
        weights.append(vector)
    for _ in range(EPOCHS):
        for row, label in zip(rows, labels, strict=True):
            values = _features(row, variant)
            probabilities = _softmax(
                tuple(sum(w * x for w, x in zip(vector, values, strict=True)) for vector in weights)
            )
            for klass in range(3):
                error = probabilities[klass] - float(label == CLASSES[klass])
                for feature in range(width):
                    regularization = L2 * weights[klass][feature] if feature else 0.0
                    weights[klass][feature] -= LEARNING_RATE * (
                        error * values[feature] + regularization
                    )
    return tuple(tuple(format(value, ".17g") for value in vector) for vector in weights)


def _probabilities(weights, row: FeatureInput, variant: str) -> tuple[float, ...]:
    values = _features(row, variant)
    return _softmax(
        tuple(sum(float(w) * x for w, x in zip(vector, values, strict=True)) for vector in weights)
    )


def _decision(scores: tuple[float, ...], threshold: float) -> str:
    winner = max(range(3), key=lambda index: scores[index])
    return CLASSES[winner] if scores[winner] >= threshold else "WAIT"


def _threshold(weights, rows, labels, variant) -> float:
    scores = tuple(_probabilities(weights, row, variant) for row in rows)
    return min(
        THRESHOLDS,
        key=lambda threshold: (
            sum(
                0.25
                if (choice := _decision(score, threshold)) == "WAIT"
                else float(choice != label)
                for score, label in zip(scores, labels, strict=True)
            )
            / len(rows),
            -threshold,
        ),
    )


def _metrics(labels, scores, threshold) -> dict:
    count = len(labels)
    decisions = tuple(_decision(score, threshold) for score in scores)
    brier = (
        sum(
            sum((score[i] - float(label == CLASSES[i])) ** 2 for i in range(3))
            for label, score in zip(labels, scores, strict=True)
        )
        / count
    )
    log_loss = (
        -sum(
            math.log(max(score[CLASSES.index(label)], 1e-15))
            for label, score in zip(labels, scores, strict=True)
        )
        / count
    )
    ece = 0.0
    for low, high in ((0.0, 0.5), (0.5, 0.75), (0.75, 1.0000001)):
        members = [
            (label, score)
            for label, score in zip(labels, scores, strict=True)
            if low <= max(score) < high
        ]
        if members:
            accuracy = sum(
                CLASSES[max(range(3), key=lambda i: score[i])] == label for label, score in members
            ) / len(members)
            confidence = sum(max(score) for _, score in members) / len(members)
            ece += len(members) / count * abs(accuracy - confidence)
    return {
        "count": count,
        "brier": format(brier, ".8f"),
        "log_loss": format(log_loss, ".8f"),
        "calibration_ece_3bin": format(ece, ".8f"),
        "wait_count": decisions.count("WAIT"),
        "correct_nonwait": sum(
            choice == label for choice, label in zip(decisions, labels, strict=True)
        ),
    }


def _artifact(variant, dataset, config, weights, threshold) -> ModelArtifact:
    fields = {
        "variant": variant,
        "algorithm": ALGORITHM,
        "code_version": CODE_VERSION,
        "code_checksum": CODE_SHA256,
        "dependency_versions": DEPENDENCY_VERSIONS,
        "dataset_checksum": dataset.checksum,
        "dataset_config_checksum": dataset.config_checksum,
        "source_checksum": dataset.source_checksum,
        "source_versions": dataset.source_versions,
        "training_config_checksum": _hash(asdict(config)),
        "seed": config.seed,
        "train_end": config.train_end,
        "validation_end": config.validation_end,
        "test_end": config.test_end,
        "embargo_seconds": int(config.embargo.total_seconds()),
        "feature_names": _feature_names(variant),
        "weights": weights,
        "threshold": format(threshold, ".2f"),
    }
    return ModelArtifact(**fields, checksum=_hash(fields))


def verify_artifact(artifact: ModelArtifact) -> bool:
    try:
        fields = asdict(artifact)
        checksum = fields.pop("checksum")
        return (
            artifact.variant in VARIANTS
            and artifact.algorithm == ALGORITHM
            and artifact.code_version == CODE_VERSION
            and artifact.code_checksum == CODE_SHA256
            and artifact.dependency_versions == DEPENDENCY_VERSIONS
            and artifact.source_versions == EXPECTED_VERSIONS
            and all(
                HEX64.fullmatch(value)
                for value in (
                    artifact.dataset_checksum,
                    artifact.dataset_config_checksum,
                    artifact.source_checksum,
                    artifact.training_config_checksum,
                    artifact.code_checksum,
                    artifact.checksum,
                )
            )
            and artifact.seed == SEED
            and _utc(artifact.train_end) < _utc(artifact.validation_end) < _utc(artifact.test_end)
            and artifact.embargo_seconds >= int(HORIZON.total_seconds())
            and artifact.feature_names == _feature_names(artifact.variant)
            and len(artifact.weights) == 3
            and all(
                len(vector) == len(artifact.feature_names)
                and all(math.isfinite(float(value)) for value in vector)
                for vector in artifact.weights
            )
            and float(artifact.threshold) in THRESHOLDS
            and checksum == _hash(fields)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def freeze_training_manifest(outcome: TrainingOutcome) -> FrozenTrainingManifest:
    """Freeze the validated synthetic pair for caller-held offline inference."""
    artifacts = outcome.artifacts
    if (
        outcome.status != "evaluated-synthetic"
        or len(artifacts) != len(VARIANTS)
        or {artifact.variant for artifact in artifacts} != set(VARIANTS)
        or not all(verify_artifact(artifact) for artifact in artifacts)
    ):
        raise ValueError("No valid synthetic sparse artifact pair")
    first = artifacts[0]
    common = (
        "dataset_checksum",
        "dataset_config_checksum",
        "source_checksum",
        "source_versions",
        "training_config_checksum",
        "train_end",
        "validation_end",
        "test_end",
    )
    if any(
        getattr(artifact, name) != getattr(first, name) for artifact in artifacts for name in common
    ):
        raise ValueError("Sparse artifacts do not share a training source")
    fields = {
        "source_schema": SCHEMA,
        "dataset_checksum": first.dataset_checksum,
        "dataset_config_checksum": first.dataset_config_checksum,
        "source_checksum": first.source_checksum,
        "source_versions": first.source_versions,
        "training_config_checksum": first.training_config_checksum,
        "artifact_checksums": tuple(sorted((a.variant, a.checksum) for a in artifacts)),
    }
    return FrozenTrainingManifest(**fields, checksum=_hash(fields))


def verify_frozen_manifest(manifest: FrozenTrainingManifest) -> bool:
    try:
        if type(manifest) is not FrozenTrainingManifest:
            return False
        fields = asdict(manifest)
        checksum = fields.pop("checksum")
        return (
            manifest.source_schema == SCHEMA
            and manifest.source_versions == EXPECTED_VERSIONS
            and tuple(variant for variant, _ in manifest.artifact_checksums) == VARIANTS
            and all(
                HEX64.fullmatch(value)
                for value in (
                    manifest.dataset_checksum,
                    manifest.dataset_config_checksum,
                    manifest.source_checksum,
                    manifest.training_config_checksum,
                    manifest.checksum,
                    *(checksum for _, checksum in manifest.artifact_checksums),
                )
            )
            and checksum == _hash(fields)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def predict_as_of(
    artifact: ModelArtifact,
    manifest: FrozenTrainingManifest,
    row: AsOfFeatureInput,
) -> tuple[tuple[str, str, str], str]:
    """Offline synthetic inference; source events differ from the training cohort."""
    if (
        not verify_artifact(artifact)
        or not verify_frozen_manifest(manifest)
        or not _valid_features(row)
    ):
        raise ValueError("Invalid sparse inference input")
    if (
        type(row) is not AsOfFeatureInput
        or (
            artifact.variant,
            artifact.checksum,
        )
        not in manifest.artifact_checksums
    ):
        raise ValueError("Artifact is absent from frozen manifest")
    for name in (
        "dataset_checksum",
        "dataset_config_checksum",
        "source_checksum",
        "source_versions",
        "training_config_checksum",
    ):
        if getattr(artifact, name) != getattr(manifest, name):
            raise ValueError("Training manifest mismatch")
    if row.anchor <= artifact.test_end:
        raise ValueError("As-of anchor is not after training test boundary")
    scores = _probabilities(artifact.weights, row, artifact.variant)
    return tuple(format(score, ".8f") for score in scores), _decision(
        scores, float(artifact.threshold)
    )


def predict(artifact: ModelArtifact, row: FeatureInput) -> tuple[tuple[str, str, str], str]:
    if not verify_artifact(artifact) or not _valid_features(row):
        raise ValueError("Invalid sparse artifact or feature input")
    if (
        row.dataset_checksum != artifact.dataset_checksum
        or row.dataset_config_checksum != artifact.dataset_config_checksum
        or row.source_checksum != artifact.source_checksum
    ):
        raise ValueError("Sparse source or config mismatch")
    scores = _probabilities(artifact.weights, row, artifact.variant)
    return tuple(format(score, ".8f") for score in scores), _decision(
        scores, float(artifact.threshold)
    )


def _split(dataset: Dataset, config: TrainingConfig):
    train_end, validation_end, test_end = map(
        _utc, (config.train_end, config.validation_end, config.test_end)
    )
    if not train_end < validation_end < test_end or config.embargo < HORIZON:
        raise ValueError("Invalid chronological split")
    rows = tuple(sorted(dataset.samples, key=lambda row: row.anchor))
    return (
        tuple(row for row in rows if row.anchor <= train_end - config.embargo),
        tuple(
            row
            for row in rows
            if train_end + config.embargo <= row.anchor <= validation_end - config.embargo
        ),
        tuple(row for row in rows if validation_end + config.embargo <= row.anchor <= test_end),
    )


def train_and_evaluate(
    dataset: Dataset,
    dataset_config: Config,
    config: TrainingConfig,
) -> TrainingOutcome:
    """Evaluate only an explicitly synthetic sealed sparse cohort."""
    waits = {}
    for wait in dataset.waits:
        waits[wait.reason] = waits.get(wait.reason, 0) + 1
    report = {
        "algorithm": ALGORITHM,
        "dataset_checksum": dataset.checksum,
        "source_checksum": dataset.source_checksum,
        "source_versions": dataset.source_versions,
        "source_waits": waits,
        "assumptions": {
            "label_cost_bps": str(dataset_config.cost_bps),
            "fee_bps": str(config.fee_bps),
            "funding_bps": str(config.funding_bps),
            "slippage_bps": str(config.slippage_bps),
            "cost_formula": "2*fee+funding+slippage; synthetic scenario",
        },
        "promotion_verdict": "collect-only: synthetic fixture, no real forward evidence",
    }

    def collect(reason):
        return TrainingOutcome("collect-only", reason, (), report | {"reason": reason})

    if (
        config.seed != SEED
        or isinstance(config.seed, bool)
        or dataset_config.feature_version != SCHEMA
        or dataset_config.source_version != SOURCE_VERSION
        or dataset.source_versions != EXPECTED_VERSIONS
        or any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (config.fee_bps, config.funding_bps, config.slippage_bps)
        )
    ):
        return collect("invalid_or_non_synthetic_provenance")
    if not verify_dataset(dataset):
        return collect("invalid_dataset_manifest")
    if dataset.source_checksum != config.expected_source_checksum:
        return collect("source_checksum_mismatch")
    if dataset.config_checksum != _hash(asdict(dataset_config)):
        return collect("dataset_config_mismatch")
    if 2 * config.fee_bps + config.funding_bps + config.slippage_bps != dataset_config.cost_bps:
        return collect("cost_assumption_mismatch")
    if waits:
        return collect("source_quality_wait")
    if len({row.anchor for row in dataset.samples}) != len(dataset.samples):
        return collect("invalid_dataset_rows")
    try:
        if _utc(config.test_end) > _utc(dataset_config.last_anchor):
            return collect("invalid_dataset_or_split")
        train, validation, test = _split(dataset, config)
    except (TypeError, ValueError):
        return collect("invalid_dataset_or_split")
    if any(
        len(rows) < minimum
        for rows, minimum in zip(
            (train, validation, test),
            MIN_COUNTS,
            strict=True,
        )
    ):
        return collect("too_few_samples")
    if any(sum(row.label == klass for row in train) < 2 for klass in CLASSES):
        return collect("too_few_class_examples")
    try:
        features = {
            name: tuple(feature_input(row, dataset) for row in rows)
            for name, rows in (("train", train), ("validation", validation), ("test", test))
        }
    except ValueError:
        return collect("invalid_dataset_rows")
    labels = {
        name: tuple(row.label for row in rows)
        for name, rows in (
            ("train", train),
            ("validation", validation),
            ("test", test),
        )
    }
    report["split"] = {
        "train": len(train),
        "validation": len(validation),
        "test": len(test),
        "embargo_hours": config.embargo.total_seconds() / 3600,
        "train_end": config.train_end.isoformat(),
        "validation_end": config.validation_end.isoformat(),
        "test_end": config.test_end.isoformat(),
    }
    artifacts = []
    models = {}
    for variant in VARIANTS:
        weights = _fit(features["train"], labels["train"], variant)
        threshold = _threshold(weights, features["validation"], labels["validation"], variant)
        artifact = _artifact(variant, dataset, config, weights, threshold)
        artifacts.append(artifact)
        scores = {
            name: tuple(_probabilities(weights, row, variant) for row in features[name])
            for name in ("validation", "test")
        }
        middle = len(test) // 2
        models[variant] = {
            "artifact_checksum": artifact.checksum,
            "validation_selected_threshold": artifact.threshold,
            **{
                name: _metrics(labels[name], scores[name], threshold)
                for name in ("validation", "test")
            },
            "ordered_test_blocks": (
                _metrics(labels["test"][:middle], scores["test"][:middle], threshold),
                _metrics(labels["test"][middle:], scores["test"][middle:], threshold),
            ),
        }
    report["models"] = models
    report["paired_test"] = {
        "anchor_count": len(test),
        "same_anchor_set": True,
        "brier_delta_sparse_minus_market": format(
            float(models["sparse-gkg+market"]["test"]["brier"])
            - float(models["market-only"]["test"]["brier"]),
            ".8f",
        ),
    }
    report["limitations"] = (
        "Synthetic fixture only; no real sparse cohort or source attestation.",
        "Validation-only threshold; test metrics are descriptive, not forward evidence.",
        "No actual fee/funding/slippage, PnL, fills, or trading calls.",
    )
    return TrainingOutcome("evaluated-synthetic", "ok", tuple(artifacts), report)
