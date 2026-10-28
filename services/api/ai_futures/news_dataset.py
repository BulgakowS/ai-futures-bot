"""Synthetic-only, pure event-time news/market dataset builder (N2a).

Callers must supply copied, persisted records. This module has no DB, HTTP, or broker access.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

HORIZON = timedelta(hours=4)
SCHEMA = "news-market-dataset-v1"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta():
        raise ValueError("Dataset timestamps must be UTC")
    return value.astimezone(UTC)


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, timedelta):
        return str(value.total_seconds())
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class CandleRecord:
    event_id: str
    window_start: datetime
    source_time: datetime
    received_at: datetime
    close: str
    payload_hash: str
    source: str
    config_version: str
    final: bool = True
    symbol: str = "BTC_USDT"
    timeframe: str = "4h"


@dataclass(frozen=True)
class NewsRecord:
    version_id: str
    canonical_id: str
    version_number: int
    title: str
    post_time: datetime
    first_seen_at: datetime
    available_at: datetime
    payload_hash: str
    source: str
    schema_version: str
    config_version: str
    observation_kind: str = "forward"


@dataclass(frozen=True)
class CoverageRecord:
    observed_at: datetime
    last_success_at: datetime | None
    status: str
    gap_count: int = 0
    page_shift_detected: bool = False
    source_version: str = "synthetic-coverage-v1"


@dataclass(frozen=True)
class DatasetConfig:
    cost_bps: Decimal
    cost_version: str
    feature_version: str = "title-keywords-v1"
    source_version: str = "synthetic-fixture-v1"
    max_coverage_age: timedelta = HORIZON


@dataclass(frozen=True)
class Sample:
    anchor: datetime
    market_event_ids: tuple[str, str]
    market_payload_hashes: tuple[str, str]
    news_version_ids: tuple[str, ...]
    news_payload_hashes: tuple[str, ...]
    news_canonical_ids: tuple[str, ...]
    market_return_bps: str
    btc_title_count: int
    futures_title_count: int
    feature_hash: str
    label: str
    label_event_id: str
    label_payload_hash: str


@dataclass(frozen=True)
class Wait:
    anchor: datetime
    reason: str


@dataclass(frozen=True)
class Dataset:
    samples: tuple[Sample, ...]
    waits: tuple[Wait, ...]
    checksum: str
    config_checksum: str
    source_checksum: str
    source_versions: tuple[str, ...]
    cutoff: datetime


def past_features(
    anchor: datetime,
    prior: CandleRecord,
    current: CandleRecord,
    recent: tuple[NewsRecord, ...],
) -> dict:
    """Features available at anchor; no label candle is accepted."""
    titles = [item.title.casefold() for item in recent]
    return {
        "anchor": _utc(anchor),
        "market_event_ids": (prior.event_id, current.event_id),
        "market_payload_hashes": (prior.payload_hash, current.payload_hash),
        "market_return_bps": format(
            (Decimal(current.close) / Decimal(prior.close) - 1) * 10000, "f"
        ),
        "news_version_ids": tuple(item.version_id for item in recent),
        "news_payload_hashes": tuple(item.payload_hash for item in recent),
        "btc_title_count": sum(bool(re.search(r"\b(btc|bitcoin)\b", title)) for title in titles),
        "futures_title_count": sum(
            bool(re.search(r"\b(futures|contract)\b", title)) for title in titles
        ),
    }


def build_dataset(
    candles: tuple[CandleRecord, ...],
    news: tuple[NewsRecord, ...],
    coverage: tuple[CoverageRecord, ...],
    *,
    cutoff: datetime,
    config: DatasetConfig,
) -> Dataset:
    """Create a frozen 4h snapshot; source records after cutoff are invisible."""
    cutoff = _utc(cutoff)
    if not config.cost_version or not config.feature_version or not config.source_version:
        raise ValueError("Dataset versions are required")
    if (
        not isinstance(config.cost_bps, Decimal)
        or not config.cost_bps.is_finite()
        or config.cost_bps < 0
        or config.cost_bps >= 10000
        or config.max_coverage_age <= timedelta()
    ):
        raise ValueError("Invalid cost or coverage configuration")
    by_window: dict[datetime, CandleRecord] = {}
    market_ids: dict[str, datetime] = {}
    for candle in candles:
        start = _utc(candle.window_start)
        _utc(candle.source_time)
        _utc(candle.received_at)
        if (
            candle.symbol != "BTC_USDT"
            or candle.timeframe != "4h"
            or int(start.timestamp()) % int(HORIZON.total_seconds())
            or not candle.event_id
            or not HEX64.fullmatch(candle.payload_hash)
            or not candle.source
            or not candle.config_version
        ):
            raise ValueError("Invalid 4h candle provenance")
        try:
            price = Decimal(candle.close)
        except InvalidOperation as exc:
            raise ValueError("Invalid candle close") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Invalid candle close")
        if candle.received_at < start + HORIZON or candle.source_time < start + HORIZON:
            raise ValueError("Candle was marked final before window close")
        previous = by_window.get(start)
        if previous and previous != candle:
            raise ValueError("Conflicting candle for one window")
        if candle.event_id in market_ids and market_ids[candle.event_id] != start:
            raise ValueError("Reused market event ID")
        by_window[start] = candle
        market_ids[candle.event_id] = start
    by_news: dict[tuple[str, int], NewsRecord] = {}
    news_ids: dict[str, tuple[str, int]] = {}
    for item in news:
        _utc(item.post_time)
        _utc(item.first_seen_at)
        _utc(item.available_at)
        if (
            item.available_at != max(item.post_time, item.first_seen_at)
            or item.version_number < 1
            or not item.version_id
            or not item.canonical_id
            or not item.title.strip()
            or not HEX64.fullmatch(item.payload_hash)
            or item.observation_kind not in {"forward", "backfill"}
            or not item.source
            or not item.schema_version
            or not item.config_version
        ):
            raise ValueError("Invalid announcement provenance")
        key = (item.canonical_id, item.version_number)
        previous = by_news.get(key)
        if previous and previous != item:
            raise ValueError("Conflicting announcement version")
        if item.version_id in news_ids and news_ids[item.version_id] != key:
            raise ValueError("Reused announcement version ID")
        by_news[key] = item
        news_ids[item.version_id] = key
    for canonical_id in {item.canonical_id for item in by_news.values()}:
        versions = sorted(
            (item for item in by_news.values() if item.canonical_id == canonical_id),
            key=lambda item: item.version_number,
        )
        if [item.version_number for item in versions] != list(range(1, len(versions) + 1)):
            raise ValueError("Announcement version sequence has a gap")
        if any(
            right.first_seen_at < left.first_seen_at
            for left, right in zip(versions, versions[1:], strict=False)
        ):
            raise ValueError("Announcement versions move backwards in observation time")
    for item in coverage:
        _utc(item.observed_at)
        if item.last_success_at is not None:
            _utc(item.last_success_at)
            if item.last_success_at > item.observed_at:
                raise ValueError("Future coverage success")
        if item.gap_count < 0 or not item.source_version:
            raise ValueError("Invalid coverage provenance")
    coverage_by_time = sorted(coverage, key=lambda item: item.observed_at)
    if len({item.observed_at for item in coverage_by_time}) != len(coverage_by_time):
        raise ValueError("Duplicate coverage observation")

    visible_candles = {
        start: item
        for start, item in by_window.items()
        if item.final and item.received_at <= cutoff and item.source_time <= cutoff
    }
    samples: list[Sample] = []
    waits: list[Wait] = []
    for start, current in sorted(visible_candles.items()):
        anchor = start + HORIZON
        if anchor + HORIZON > cutoff:
            continue
        prior = visible_candles.get(start - HORIZON)
        future = visible_candles.get(start + HORIZON)
        if prior is None or future is None:
            waits.append(Wait(anchor, "market_gap_or_unavailable_label"))
            continue
        if any(
            timestamp > anchor
            for timestamp in (
                current.received_at,
                current.source_time,
                prior.received_at,
                prior.source_time,
            )
        ):
            waits.append(Wait(anchor, "market_late"))
            continue
        latest_coverage = next(
            (item for item in reversed(coverage_by_time) if item.observed_at <= anchor), None
        )
        if latest_coverage is None or latest_coverage.last_success_at is None:
            waits.append(Wait(anchor, "news_coverage_missing"))
            continue
        if (
            latest_coverage.status != "ok"
            or latest_coverage.gap_count
            or latest_coverage.page_shift_detected
        ):
            waits.append(Wait(anchor, "news_coverage_gap"))
            continue
        if anchor - latest_coverage.last_success_at > config.max_coverage_age:
            waits.append(Wait(anchor, "news_coverage_stale"))
            continue
        selected: dict[str, NewsRecord] = {}
        for item in by_news.values():
            if item.available_at > anchor or item.first_seen_at > cutoff:
                continue
            previous = selected.get(item.canonical_id)
            if previous is None or item.version_number > previous.version_number:
                selected[item.canonical_id] = item
        recent = tuple(
            sorted(
                (
                    item
                    for item in selected.values()
                    if anchor - HORIZON < item.available_at <= anchor
                ),
                key=lambda item: (item.canonical_id, item.version_number),
            )
        )
        if any(
            item.observation_kind == "backfill" or item.first_seen_at - item.post_time > HORIZON
            for item in recent
        ):
            waits.append(Wait(anchor, "news_late_or_backfill"))
            continue
        if any(item.version_number > 1 for item in recent):
            waits.append(Wait(anchor, "news_edit"))
            continue
        features = past_features(anchor, prior, current, recent)
        change_bps = (Decimal(future.close) / Decimal(current.close) - 1) * 10000
        label = (
            "UP"
            if change_bps > config.cost_bps
            else "DOWN"
            if change_bps < -config.cost_bps
            else "NEUTRAL"
        )
        samples.append(
            Sample(
                anchor=anchor,
                market_event_ids=features["market_event_ids"],
                market_payload_hashes=features["market_payload_hashes"],
                news_version_ids=features["news_version_ids"],
                news_payload_hashes=features["news_payload_hashes"],
                news_canonical_ids=tuple(item.canonical_id for item in recent),
                market_return_bps=features["market_return_bps"],
                btc_title_count=features["btc_title_count"],
                futures_title_count=features["futures_title_count"],
                feature_hash=_hash(features),
                label=label,
                label_event_id=future.event_id,
                label_payload_hash=future.payload_hash,
            )
        )
    source_versions = tuple(
        sorted(
            {config.source_version}
            | {f"{item.source}:{item.config_version}" for item in visible_candles.values()}
            | {
                f"{item.source}:{item.schema_version}:{item.config_version}"
                for item in by_news.values()
                if item.first_seen_at <= cutoff
            }
            | {item.source_version for item in coverage_by_time if item.observed_at <= cutoff}
        )
    )
    config_checksum = _hash(asdict(config))
    source_checksum = _hash(
        {
            "candles": tuple(
                asdict(item)
                for item in sorted(visible_candles.values(), key=lambda item: item.window_start)
            ),
            "news": tuple(
                asdict(item)
                for item in sorted(
                    (item for item in by_news.values() if item.first_seen_at <= cutoff),
                    key=lambda item: (item.canonical_id, item.version_number),
                )
            ),
            "coverage": tuple(
                asdict(item) for item in coverage_by_time if item.observed_at <= cutoff
            ),
        }
    )
    checksum = _hash(
        {
            "schema": SCHEMA,
            "cutoff": cutoff,
            "config_checksum": config_checksum,
            "source_checksum": source_checksum,
            "source_versions": source_versions,
            "samples": tuple(asdict(item) for item in samples),
            "waits": tuple(asdict(item) for item in waits),
        }
    )
    return Dataset(
        tuple(samples),
        tuple(waits),
        checksum,
        config_checksum,
        source_checksum,
        source_versions,
        cutoff,
    )


def chronological_split(
    dataset: Dataset,
    *,
    train_end: datetime,
    validation_end: datetime,
    test_end: datetime,
    embargo: timedelta = HORIZON,
) -> tuple[tuple[Sample, ...], tuple[Sample, ...], tuple[Sample, ...]]:
    """Split by anchor with a symmetric embargo and forbid cross-split news groups."""
    train_end, validation_end, test_end = map(_utc, (train_end, validation_end, test_end))
    if not train_end < validation_end < test_end or embargo < HORIZON:
        raise ValueError("Invalid chronological split or embargo")
    groups: list[list[Sample]] = [[], [], []]
    for sample in dataset.samples:
        anchor = sample.anchor
        if anchor <= train_end - embargo:
            groups[0].append(sample)
        elif train_end + embargo <= anchor <= validation_end - embargo:
            groups[1].append(sample)
        elif validation_end + embargo <= anchor <= test_end:
            groups[2].append(sample)
    owner: dict[str, int] = {}
    for index, rows in enumerate(groups):
        for sample in rows:
            for canonical_id in sample.news_canonical_ids:
                if canonical_id in owner and owner[canonical_id] != index:
                    raise ValueError("Related announcements cross chronological splits")
                owner[canonical_id] = index
    return tuple(tuple(rows) for rows in groups)


def verify_dataset(dataset: Dataset) -> bool:
    """Check the portable snapshot seal and each sample's past-only feature seal."""
    try:
        if not all(
            isinstance(value, str) and HEX64.fullmatch(value)
            for value in (dataset.checksum, dataset.config_checksum, dataset.source_checksum)
        ):
            return False
        for row in dataset.samples:
            if row.feature_hash != _hash(
                {
                    "anchor": row.anchor,
                    "market_event_ids": row.market_event_ids,
                    "market_payload_hashes": row.market_payload_hashes,
                    "market_return_bps": row.market_return_bps,
                    "news_version_ids": row.news_version_ids,
                    "news_payload_hashes": row.news_payload_hashes,
                    "btc_title_count": row.btc_title_count,
                    "futures_title_count": row.futures_title_count,
                }
            ):
                return False
        return dataset.checksum == _hash(
            {
                "schema": SCHEMA,
                "cutoff": dataset.cutoff,
                "config_checksum": dataset.config_checksum,
                "source_checksum": dataset.source_checksum,
                "source_versions": dataset.source_versions,
                "samples": tuple(asdict(item) for item in dataset.samples),
                "waits": tuple(asdict(item) for item in dataset.waits),
            }
        )
    except (TypeError, ValueError, AttributeError):
        return False
