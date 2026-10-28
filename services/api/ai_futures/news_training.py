"""Offline synthetic-only probabilistic research engine; no runtime inference wiring."""

import hashlib
import math
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from ai_futures.news_dataset import (
    HORIZON,
    Dataset,
    DatasetConfig,
    Sample,
    _hash,
    _utc,
    chronological_split,
    verify_dataset,
)


class FeatureInput(Protocol):
    """Prediction needs normalized past features only, never a future label."""

    market_return_bps: str
    btc_title_count: int
    futures_title_count: int


ALGORITHM = "multinomial-sgd-v1"
CLASSES = ("DOWN", "NEUTRAL", "UP")
VARIANTS = ("market-only", "news-only", "market+news")
EPOCHS = 160
LEARNING_RATE = 0.03
L2 = 0.001
THRESHOLDS = (0.40, 0.50, 0.60, 0.70)
MIN_COUNTS = (24, 8, 8)
BLOCKING_WAITS = {
    "news_coverage_missing",
    "news_coverage_gap",
    "news_coverage_stale",
    "market_late",
    "news_late_or_backfill",
    "news_edit",
}


@dataclass(frozen=True)
class TrainingConfig:
    seed: int
    train_end: datetime
    validation_end: datetime
    test_end: datetime
    embargo: timedelta = HORIZON
    fee_bps: Decimal = Decimal("4")
    funding_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("2")


@dataclass(frozen=True)
class ModelArtifact:
    variant: str
    algorithm: str
    model_version: str
    runtime_version: str
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


def _features(sample: FeatureInput, variant: str) -> tuple[float, ...]:
    market = max(-10.0, min(10.0, float(Decimal(sample.market_return_bps) / 100)))
    news = (min(sample.btc_title_count, 10) / 10, min(sample.futures_title_count, 10) / 10)
    return (
        (1.0,)
        + ((market,) if variant != "news-only" else ())
        + (news if variant != "market-only" else ())
    )


def _feature_names(variant: str) -> tuple[str, ...]:
    return (
        ("bias",)
        + (("market_return_bps/100",) if variant != "news-only" else ())
        + (("btc_titles/10", "futures_titles/10") if variant != "market-only" else ())
    )


def _softmax(logits: tuple[float, ...]) -> tuple[float, ...]:
    shift = max(logits)
    values = tuple(math.exp(value - shift) for value in logits)
    total = sum(values)
    return tuple(value / total for value in values)


def _initial(seed: int, variant: str, klass: int, feature: int) -> float:
    digest = hashlib.sha256(f"{seed}:{variant}:{klass}:{feature}".encode()).digest()
    return (int.from_bytes(digest[:8]) / 2**64 - 0.5) / 100


def _fit(rows: tuple[Sample, ...], variant: str, seed: int) -> tuple[tuple[str, ...], ...]:
    width = len(_feature_names(variant))
    weights = [
        [_initial(seed, variant, klass, feature) for feature in range(width)] for klass in range(3)
    ]
    for _ in range(EPOCHS):
        for row in rows:
            values = _features(row, variant)
            probabilities = _softmax(
                tuple(
                    sum(weight * value for weight, value in zip(vector, values, strict=True))
                    for vector in weights
                )
            )
            for klass in range(3):
                error = probabilities[klass] - float(row.label == CLASSES[klass])
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
        tuple(
            sum(float(weight) * value for weight, value in zip(vector, values, strict=True))
            for vector in weights
        )
    )


def _decision(probabilities: tuple[float, ...], threshold: float) -> str:
    highest = max(range(3), key=lambda index: probabilities[index])
    return CLASSES[highest] if probabilities[highest] >= threshold else "WAIT"


def _select_threshold(
    weights: tuple[tuple[str, ...], ...], rows: tuple[Sample, ...], variant: str
) -> float:
    probabilities = tuple(_probabilities(weights, row, variant) for row in rows)
    # Fixed WAIT cost, fixed candidate grid; no test labels enter selection.
    return min(
        THRESHOLDS,
        key=lambda threshold: (
            sum(
                0.25
                if (decision := _decision(probability, threshold)) == "WAIT"
                else float(decision != row.label)
                for row, probability in zip(rows, probabilities, strict=True)
            )
            / len(rows),
            -threshold,
        ),
    )


def _fmt(value: float) -> str:
    return format(value, ".8f")


def _wilson(correct: int, count: int) -> tuple[str, str]:
    z = 1.96
    proportion = correct / count
    denominator = 1 + z * z / count
    centre = (proportion + z * z / (2 * count)) / denominator
    radius = (
        z
        * math.sqrt(proportion * (1 - proportion) / count + z * z / (4 * count * count))
        / denominator
    )
    return _fmt(max(0.0, centre - radius)), _fmt(min(1.0, centre + radius))


def _metrics(
    rows: tuple[Sample, ...], probabilities: tuple[tuple[float, ...], ...], threshold: float
) -> dict:
    count = len(rows)
    if count == 0:
        raise ValueError("Empty evaluation block")
    brier = (
        sum(
            sum(
                (value - float(row.label == CLASSES[index])) ** 2
                for index, value in enumerate(probability)
            )
            for row, probability in zip(rows, probabilities, strict=True)
        )
        / count
    )
    log_loss = (
        -sum(
            math.log(max(probability[CLASSES.index(row.label)], 1e-15))
            for row, probability in zip(rows, probabilities, strict=True)
        )
        / count
    )
    decisions = tuple(_decision(probability, threshold) for probability in probabilities)
    evaluated = sum(decision != "WAIT" for decision in decisions)
    correct = sum(decision == row.label for decision, row in zip(decisions, rows, strict=True))
    # Three fixed confidence bins; descriptive only for small, correlated samples.
    bins = ((0.0, 0.5), (0.5, 0.75), (0.75, 1.0000001))
    ece = 0.0
    for low, high in bins:
        members = [
            (row, probability)
            for row, probability in zip(rows, probabilities, strict=True)
            if low <= max(probability) < high
        ]
        if members:
            accuracy = sum(
                CLASSES[max(range(3), key=lambda index: probability[index])] == row.label
                for row, probability in members
            ) / len(members)
            confidence = sum(max(probability) for _, probability in members) / len(members)
            ece += len(members) / count * abs(accuracy - confidence)
    return {
        "count": count,
        "brier": _fmt(brier),
        "log_loss": _fmt(log_loss),
        "calibration_ece_3bin": _fmt(ece),
        "decision_coverage": _fmt(evaluated / count),
        "wait_count": count - evaluated,
        "correct_nonwait": correct,
        "accuracy_all_rows": _fmt(correct / count),
        "accuracy_nonwait": _fmt(correct / evaluated) if evaluated else None,
        "accuracy_wilson_95_all_rows": _wilson(correct, count),
    }


def _artifact(
    variant: str, dataset: Dataset, config: TrainingConfig, weights, threshold
) -> ModelArtifact:
    fields = {
        "variant": variant,
        "algorithm": ALGORITHM,
        "model_version": f"{ALGORITHM}/{variant}",
        "runtime_version": f"python-{platform.python_version()}-stdlib",
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
            and artifact.model_version == f"{ALGORITHM}/{artifact.variant}"
            and artifact.feature_names == _feature_names(artifact.variant)
            and len(artifact.weights) == len(CLASSES)
            and all(
                len(vector) == len(artifact.feature_names)
                and all(math.isfinite(float(weight)) for weight in vector)
                for vector in artifact.weights
            )
            and float(artifact.threshold) in THRESHOLDS
            and checksum == _hash(fields)
        )
    except (TypeError, ValueError, AttributeError):
        return False


def predict(artifact: ModelArtifact, row: FeatureInput) -> tuple[tuple[str, str, str], str]:
    """Pure prediction from three past-only fields; quality/approval gate is external."""
    if not verify_artifact(artifact):
        raise ValueError("Artifact checksum mismatch")
    probabilities = _probabilities(artifact.weights, row, artifact.variant)
    return tuple(_fmt(value) for value in probabilities), _decision(
        probabilities, float(artifact.threshold)
    )


def _baseline(rows: tuple[Sample, ...], name: str, cost_bps: Decimal) -> dict:
    if name == "always-neutral":
        probabilities = tuple((0.1, 0.8, 0.1) for _ in rows)
    else:
        probabilities = tuple(
            tuple(
                0.8
                if index
                == CLASSES.index(
                    "UP"
                    if Decimal(row.market_return_bps) > cost_bps
                    else "DOWN"
                    if Decimal(row.market_return_bps) < -cost_bps
                    else "NEUTRAL"
                )
                else 0.1
                for index in range(3)
            )
            for row in rows
        )
    return _metrics(rows, probabilities, 0.4)


def train_and_evaluate(
    dataset: Dataset, dataset_config: DatasetConfig, config: TrainingConfig
) -> TrainingOutcome:
    """Fit on train only, select thresholds on validation, then score untouched test."""
    waits_by_reason: dict[str, int] = {}
    for wait in dataset.waits:
        waits_by_reason[wait.reason] = waits_by_reason.get(wait.reason, 0) + 1
    base_report = {
        "algorithm": ALGORITHM,
        "dataset_checksum": dataset.checksum,
        "source_checksum": dataset.source_checksum,
        "dataset_config_checksum": dataset.config_checksum,
        "seed": config.seed,
        "source_waits": waits_by_reason,
        "source_wait_rate": _fmt(
            len(dataset.waits) / max(1, len(dataset.samples) + len(dataset.waits))
        ),
        "assumptions": {
            "label_cost_bps": str(dataset_config.cost_bps),
            "label_cost_version": dataset_config.cost_version,
            "fee_bps": str(config.fee_bps),
            "funding_bps": str(config.funding_bps),
            "slippage_bps": str(config.slippage_bps),
            "cost_formula": "2*fee_bps + slippage_bps + funding_bps; round-trip synthetic",
            "funding": "synthetic fixed assumption; no historical funding coverage",
            "pnl": "not calculated; classification research only",
        },
    }

    def collect(reason: str) -> TrainingOutcome:
        return TrainingOutcome("collect-only", reason, (), base_report | {"reason": reason})

    if (
        not isinstance(config.seed, int)
        or isinstance(config.seed, bool)
        or config.seed < 0
        or any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (config.fee_bps, config.funding_bps, config.slippage_bps)
        )
        or not dataset_config.source_version.startswith("synthetic")
        or any(not version.startswith("synthetic") for version in dataset.source_versions)
    ):
        return collect("invalid_or_non_synthetic_config")
    if _hash(asdict(dataset_config)) != dataset.config_checksum:
        return collect("dataset_config_mismatch")
    if 2 * config.fee_bps + config.slippage_bps + config.funding_bps != dataset_config.cost_bps:
        return collect("cost_assumption_mismatch")
    if not verify_dataset(dataset):
        return collect("invalid_dataset_manifest")
    if any(reason in BLOCKING_WAITS for reason in waits_by_reason):
        return collect("source_quality_wait")
    try:
        _utc(dataset.cutoff)
        if not all(
            isinstance(value, str) and len(value) == 64
            for value in (dataset.checksum, dataset.source_checksum)
        ):
            return collect("invalid_dataset_manifest")
        if any(row.anchor + HORIZON > dataset.cutoff for row in dataset.samples):
            return collect("test_label_not_final_at_cutoff")
        if len({row.anchor for row in dataset.samples}) != len(dataset.samples) or any(
            row.label not in CLASSES or row.anchor > dataset.cutoff for row in dataset.samples
        ):
            return collect("invalid_dataset_rows")
        train, validation, test = chronological_split(
            dataset,
            train_end=config.train_end,
            validation_end=config.validation_end,
            test_end=config.test_end,
            embargo=config.embargo,
        )
    except (TypeError, ValueError, AttributeError):
        return collect("invalid_dataset_or_split")
    if any(
        len(rows) < minimum
        for rows, minimum in zip((train, validation, test), MIN_COUNTS, strict=True)
    ):
        return collect("too_few_samples")
    if any(sum(row.label == klass for row in train) < 2 for klass in CLASSES):
        return collect("too_few_class_examples")
    if len(test) < 8:
        return collect("too_few_rolling_samples")
    base_report["split"] = {
        "train": len(train),
        "validation": len(validation),
        "test": len(test),
        "embargo_hours": config.embargo.total_seconds() / 3600,
        "train_end": config.train_end.isoformat(),
        "validation_end": config.validation_end.isoformat(),
        "test_end": config.test_end.isoformat(),
        "class_counts": {
            name: {klass: sum(row.label == klass for row in rows) for klass in CLASSES}
            for name, rows in (("train", train), ("validation", validation), ("test", test))
        },
    }
    base_report["baseline"] = {
        name: {
            "validation": _baseline(validation, name, dataset_config.cost_bps),
            "test": _baseline(test, name, dataset_config.cost_bps),
        }
        for name in ("always-neutral", "market-sign-rule")
    }
    artifacts: list[ModelArtifact] = []
    evaluations: dict[str, dict] = {}
    midpoint = len(test) // 2
    for variant in VARIANTS:
        weights = _fit(train, variant, config.seed)
        threshold = _select_threshold(weights, validation, variant)
        artifact = _artifact(variant, dataset, config, weights, threshold)
        artifacts.append(artifact)
        scores = {
            name: tuple(_probabilities(weights, row, variant) for row in rows)
            for name, rows in (("train", train), ("validation", validation), ("test", test))
        }
        evaluations[variant] = {
            "artifact_checksum": artifact.checksum,
            "validation_selected_threshold": artifact.threshold,
            "train": _metrics(train, scores["train"], threshold),
            "validation": _metrics(validation, scores["validation"], threshold),
            "test": _metrics(test, scores["test"], threshold),
            "rolling_heldout_test": (
                {
                    "from_utc": test[0].anchor.isoformat(),
                    "through_utc": test[midpoint - 1].anchor.isoformat(),
                    **_metrics(test[:midpoint], scores["test"][:midpoint], threshold),
                },
                {
                    "from_utc": test[midpoint].anchor.isoformat(),
                    "through_utc": test[-1].anchor.isoformat(),
                    **_metrics(test[midpoint:], scores["test"][midpoint:], threshold),
                },
            ),
        }
    base_report["models"] = evaluations
    base_report["news_contribution_descriptive"] = {
        split: _fmt(
            float(evaluations["market+news"][split]["brier"])
            - float(evaluations["market-only"][split]["brier"])
        )
        for split in ("validation", "test")
    }
    base_report["promotion_verdict"] = "collect-only: synthetic data and no forward evidence"
    base_report["limitations"] = (
        "Synthetic fixtures only; no MEXC forward/news rights or model promotion.",
        "Two ordered held-out test blocks, not a live forward test; no test tuning.",
        "Wilson interval is descriptive under overlapping/time-correlated samples.",
        "No PnL, order fills, historical fees/funding/slippage or broker calls.",
    )
    return TrainingOutcome("evaluated-synthetic", "ok", tuple(artifacts), base_report)
