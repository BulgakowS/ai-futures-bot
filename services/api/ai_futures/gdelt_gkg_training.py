"""Deterministic, synthetic-only GKG v2 classification research; no runtime wiring."""

import hashlib
import math
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path

from ai_futures.gdelt_gkg_dataset import (
    HORIZON,
    SCHEMA,
    SLOT,
    Config,
    Dataset,
    Sample,
    _hash,
    _utc,
    chronological_split,
    verify_dataset,
)
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION

ALGORITHM = "gkg-multinomial-sgd-v1"
CODE_VERSION = "s11-g5-v1"
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEPENDENCY_VERSIONS = (
    f"python-{platform.python_version()}",
    *(
        f"{name}-{version(name)}"
        for name in ("httpx", "SQLAlchemy", "pydantic", "pydantic-settings")
    ),
    f"gdelt_gkg_dataset:{hashlib.sha256(Path(__file__).with_name('gdelt_gkg_dataset.py').read_bytes()).hexdigest()}",
    f"gdelt_gkg_forward:{hashlib.sha256(Path(__file__).with_name('gdelt_gkg_forward.py').read_bytes()).hexdigest()}",
)
CLASSES = ("DOWN", "NEUTRAL", "UP")
VARIANTS = ("market-only", "gkg+market")
EPOCHS = 160
LEARNING_RATE = 0.03
L2 = 0.001
THRESHOLDS = (0.40, 0.50, 0.60, 0.70)
MIN_COUNTS = (24, 8, 8)
SYNTHETIC_CONFIG_VERSION = "synthetic-gkg-fixture-v1"
EXPECTED_VERSIONS = tuple(
    sorted(
        (
            SCHEMA,
            SOURCE_VERSION,
            f"synthetic:{SYNTHETIC_CONFIG_VERSION}",
            f"{SOURCE_VERSION}:{SYNTHETIC_CONFIG_VERSION}",
        )
    )
)
BLOCKING_WAITS = {
    "release_slot_missing",
    "release_slot_unknown",
    "release_slot_late",
    "release_revision_conflict",
    "market_late_or_corrected",
    "market_nonfinal",
    "market_gap",
    "market_missing",
    "market_correction",
    "decision_unavailable",
    "release_observation_conflict",
}


@dataclass(frozen=True)
class TrainingConfig:
    seed: int
    train_end: datetime
    validation_end: datetime
    test_end: datetime
    expected_source_checksum: str
    embargo: timedelta = HORIZON
    fee_bps: Decimal = Decimal("4")
    funding_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("2")


@dataclass(frozen=True)
class FeatureInput:
    """Only as-of numeric features and causal IDs; no label or future candle."""

    anchor: datetime
    decision_at: datetime
    market_event_ids: tuple[str, str]
    market_payload_hashes: tuple[str, str]
    market_return_bps: str
    btc_theme_count: int
    denominator_rows: int
    btc_share: str
    mean_btc_document_tone: str | None
    tone_missing: bool
    release_revision_ids: tuple[str, ...]
    slot_observation_ids: tuple[str, ...]
    coverage_slots: int
    market_feature_hash: str
    aggregate_feature_hash: str
    feature_hash: str
    dataset_config_checksum: str
    source_versions: tuple[str, ...]


@dataclass(frozen=True)
class ModelArtifact:
    variant: str
    algorithm: str
    code_version: str
    code_checksum: str
    runtime_version: str
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
    feature_names: tuple[str, ...]
    weights: tuple[tuple[str, ...], ...]
    threshold: str
    checksum: str


@dataclass(frozen=True)
class TrainingOutcome:
    status: str
    reason: str
    artifacts: tuple[ModelArtifact, ...]
    report: dict


def feature_input(row: Sample, dataset: Dataset) -> FeatureInput:
    """Project a sealed sample to the label-free prediction contract."""
    if not verify_dataset(dataset) or row not in dataset.samples:
        raise ValueError("Invalid GKG dataset or sample")
    names = tuple(FeatureInput.__dataclass_fields__)[:-2]
    values = {name: getattr(row, name) for name in names}
    return FeatureInput(
        **values,
        dataset_config_checksum=dataset.config_checksum,
        source_versions=dataset.source_versions,
    )


def _valid_features(row: FeatureInput) -> bool:
    try:
        if (
            type(row) is not FeatureInput
            or row.source_versions != EXPECTED_VERSIONS
            or not row.anchor <= row.decision_at <= row.anchor + SLOT
            or row.coverage_slots != 16
            or len(row.release_revision_ids) != 16
            or len(row.slot_observation_ids) != 16
            or len(row.market_event_ids) != 2
            or len(row.market_payload_hashes) != 2
            or row.btc_theme_count < 0
            or row.denominator_rows <= 0
            or row.btc_theme_count > row.denominator_rows
            or row.tone_missing != (row.btc_theme_count == 0)
            or (row.mean_btc_document_tone is None) != row.tone_missing
        ):
            return False
        market = {
            name: getattr(row, name)
            for name in (
                "anchor",
                "decision_at",
                "market_event_ids",
                "market_payload_hashes",
                "market_return_bps",
            )
        }
        aggregate = {
            name: getattr(row, name)
            for name in (
                "anchor",
                "decision_at",
                "btc_theme_count",
                "denominator_rows",
                "btc_share",
                "mean_btc_document_tone",
                "tone_missing",
                "release_revision_ids",
                "slot_observation_ids",
                "coverage_slots",
            )
        }
        share = Decimal(row.btc_share)
        ret = Decimal(row.market_return_bps)
        tone = None if row.tone_missing else Decimal(row.mean_btc_document_tone)
        return (
            share.is_finite()
            and ret.is_finite()
            and (tone is None or tone.is_finite())
            and share == Decimal(row.btc_theme_count) / Decimal(row.denominator_rows)
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
            "btc_theme_count/16",
            "btc_share",
            "mean_btc_document_tone/10",
            "tone_missing",
        )
    )


def _features(row: FeatureInput, variant: str) -> tuple[float, ...]:
    market = float(max(Decimal("-10"), min(Decimal("10"), Decimal(row.market_return_bps) / 100)))
    if variant == "market-only":
        return 1.0, market
    tone = (
        0.0
        if row.tone_missing
        else float(
            max(Decimal("-10"), min(Decimal("10"), Decimal(row.mean_btc_document_tone) / 10))
        )
    )
    return (
        1.0,
        market,
        min(row.btc_theme_count, 160) / 16,
        float(Decimal(row.btc_share)),
        tone,
        float(row.tone_missing),
    )


def _softmax(logits: tuple[float, ...]) -> tuple[float, ...]:
    shifted = tuple(math.exp(value - max(logits)) for value in logits)
    return tuple(value / sum(shifted) for value in shifted)


def _initial(seed: int, variant: str, klass: int, feature: int) -> float:
    digest = hashlib.sha256(f"{seed}:{variant}:{klass}:{feature}".encode()).digest()
    return (int.from_bytes(digest[:8]) / 2**64 - 0.5) / 100


def _fit(
    rows: tuple[FeatureInput, ...], labels: tuple[str, ...], variant: str, seed: int
) -> tuple[tuple[str, ...], ...]:
    width = len(_feature_names(variant))
    weights = [
        [_initial(seed, variant, klass, feature) for feature in range(width)] for klass in range(3)
    ]
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


def _probabilities(
    weights: tuple[tuple[str, ...], ...], row: FeatureInput, variant: str
) -> tuple[float, ...]:
    values = _features(row, variant)
    return _softmax(
        tuple(sum(float(w) * x for w, x in zip(vector, values, strict=True)) for vector in weights)
    )


def _decision(probabilities: tuple[float, ...], threshold: float) -> str:
    winner = max(range(3), key=lambda index: probabilities[index])
    return CLASSES[winner] if probabilities[winner] >= threshold else "WAIT"


def _threshold(weights, rows, labels, variant) -> float:
    scores = tuple(_probabilities(weights, row, variant) for row in rows)
    return min(
        THRESHOLDS,
        key=lambda threshold: (
            sum(
                0.25
                if (decision := _decision(score, threshold)) == "WAIT"
                else float(decision != label)
                for score, label in zip(scores, labels, strict=True)
            )
            / len(rows),
            -threshold,
        ),
    )


def _fmt(number: float) -> str:
    return format(number, ".8f")


def _metrics(
    labels: tuple[str, ...], scores: tuple[tuple[float, ...], ...], threshold: float
) -> dict:
    count = len(labels)
    if not count:
        raise ValueError("Empty evaluation block")
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
    nonwait = sum(decision != "WAIT" for decision in decisions)
    return {
        "count": count,
        "brier": _fmt(brier),
        "log_loss": _fmt(log_loss),
        "calibration_ece_3bin": _fmt(ece),
        "wait_count": count - nonwait,
        "decision_coverage": _fmt(nonwait / count),
        "correct_nonwait": sum(d == label for d, label in zip(decisions, labels, strict=True)),
    }


def _artifact(variant, dataset, config, weights, threshold) -> ModelArtifact:
    fields = {
        "variant": variant,
        "algorithm": ALGORITHM,
        "code_version": CODE_VERSION,
        "code_checksum": CODE_SHA256,
        "runtime_version": f"python-{platform.python_version()}-stdlib",
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
            and artifact.runtime_version == f"python-{platform.python_version()}-stdlib"
            and artifact.dependency_versions == DEPENDENCY_VERSIONS
            and artifact.source_versions == EXPECTED_VERSIONS
            and artifact.feature_names == _feature_names(artifact.variant)
            and len(artifact.weights) == 3
            and all(
                len(vector) == len(artifact.feature_names)
                and all(math.isfinite(float(weight)) for weight in vector)
                for vector in artifact.weights
            )
            and float(artifact.threshold) in THRESHOLDS
            and checksum == _hash(fields)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def predict(artifact: ModelArtifact, row: FeatureInput) -> tuple[tuple[str, str, str], str]:
    if not verify_artifact(artifact) or not _valid_features(row):
        raise ValueError("Invalid GKG artifact or feature input")
    if row.dataset_config_checksum != artifact.dataset_config_checksum:
        raise ValueError("GKG config mismatch")
    scores = _probabilities(artifact.weights, row, artifact.variant)
    return tuple(_fmt(score) for score in scores), _decision(scores, float(artifact.threshold))


def train_and_evaluate(
    dataset: Dataset, dataset_config: Config, config: TrainingConfig
) -> TrainingOutcome:
    waits: dict[str, int] = {}
    for wait in dataset.waits:
        waits[wait.reason] = waits.get(wait.reason, 0) + 1
    report = {
        "algorithm": ALGORITHM,
        "code_version": CODE_VERSION,
        "dataset_checksum": dataset.checksum,
        "source_checksum": dataset.source_checksum,
        "dataset_config_checksum": dataset.config_checksum,
        "source_versions": dataset.source_versions,
        "seed": config.seed,
        "source_waits": waits,
        "source_wait_rate": _fmt(
            len(dataset.waits) / max(1, len(dataset.samples) + len(dataset.waits))
        ),
        "assumptions": {
            "label_cost_bps": str(dataset_config.cost_bps),
            "label_cost_version": dataset_config.cost_version,
            "fee_bps": str(config.fee_bps),
            "funding_bps": str(config.funding_bps),
            "slippage_bps": str(config.slippage_bps),
            "cost_formula": "2*fee_bps + funding_bps + slippage_bps; synthetic round-trip",
            "pnl": "not calculated; classification research only",
        },
        "promotion_verdict": "collect-only: synthetic fixture and no real forward evidence",
    }

    def collect(reason: str) -> TrainingOutcome:
        return TrainingOutcome("collect-only", reason, (), report | {"reason": reason})

    if (
        not isinstance(config.seed, int)
        or isinstance(config.seed, bool)
        or config.seed < 0
        or any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (config.fee_bps, config.funding_bps, config.slippage_bps)
        )
        or dataset_config.feature_version != SCHEMA
        or dataset_config.source_version != SOURCE_VERSION
        or dataset.source_versions != EXPECTED_VERSIONS
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
    if any(reason in BLOCKING_WAITS for reason in waits):
        return collect("source_quality_wait")
    try:
        _utc(dataset.cutoff)
        if len({row.anchor for row in dataset.samples}) != len(dataset.samples) or any(
            row.label not in CLASSES
            or row.target_close_at > dataset.cutoff
            or row.decision_at > dataset.cutoff
            for row in dataset.samples
        ):
            return collect("invalid_dataset_rows")
        train, validation, test = chronological_split(
            dataset,
            train_end=config.train_end,
            validation_end=config.validation_end,
            test_end=config.test_end,
            embargo=config.embargo,
        )
    except (AttributeError, TypeError, ValueError):
        return collect("invalid_dataset_or_split")
    if any(
        len(rows) < minimum
        for rows, minimum in zip((train, validation, test), MIN_COUNTS, strict=True)
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
        for name, rows in (("train", train), ("validation", validation), ("test", test))
    }
    report["split"] = {
        "train": len(train),
        "validation": len(validation),
        "test": len(test),
        "embargo_hours": config.embargo.total_seconds() / 3600,
        "train_end": config.train_end.isoformat(),
        "validation_end": config.validation_end.isoformat(),
        "test_end": config.test_end.isoformat(),
        "class_counts": {
            name: {klass: values.count(klass) for klass in CLASSES}
            for name, values in labels.items()
        },
    }
    artifacts = []
    models = {}
    middle = len(test) // 2
    for variant in VARIANTS:
        weights = _fit(features["train"], labels["train"], variant, config.seed)
        threshold = _threshold(weights, features["validation"], labels["validation"], variant)
        artifact = _artifact(variant, dataset, config, weights, threshold)
        artifacts.append(artifact)
        scores = {
            name: tuple(_probabilities(weights, row, variant) for row in rows)
            for name, rows in features.items()
        }
        models[variant] = {
            "artifact_checksum": artifact.checksum,
            "validation_selected_threshold": artifact.threshold,
            **{
                name: _metrics(labels[name], scores[name], threshold)
                for name in ("train", "validation", "test")
            },
            "rolling_heldout_test": (
                {
                    "from_utc": test[0].anchor.isoformat(),
                    "through_utc": test[middle - 1].anchor.isoformat(),
                    **_metrics(labels["test"][:middle], scores["test"][:middle], threshold),
                },
                {
                    "from_utc": test[middle].anchor.isoformat(),
                    "through_utc": test[-1].anchor.isoformat(),
                    **_metrics(labels["test"][middle:], scores["test"][middle:], threshold),
                },
            ),
        }
    report["models"] = models
    report["gkg_contribution_descriptive"] = {
        split: _fmt(
            float(models["gkg+market"][split]["brier"])
            - float(models["market-only"][split]["brier"])
        )
        for split in ("validation", "test")
    }
    report["limitations"] = (
        "Synthetic GKG and market fixture only; no real source cohort or promotion.",
        "Two ordered held-out test blocks are not a live forward evaluation.",
        "No PnL, order fills, actual fees/funding/slippage, or broker calls.",
        "Source checksum is a portable seal, not independent source attestation.",
    )
    return TrainingOutcome("evaluated-synthetic", "ok", tuple(artifacts), report)
