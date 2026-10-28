"""Pure, offline GDELT Volume Intensity and BTC_USDT 4h dataset contract.

Inputs are copies of persisted G1 revision/scan and Candle rows. No DB, HTTP,
model, or broker access is available here. A scheduled anchor always yields
exactly one sample or WAIT; missing GDELT buckets are never filled with zero.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

HORIZON = timedelta(hours=4)
BUCKET = timedelta(minutes=15)
SCHEMA = "gdelt-volume-market-dataset-v1"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta():
        raise ValueError("UTC timestamp required")
    return value.astimezone(UTC)


def _decimal(value: str, *, positive: bool = False, maximum: Decimal | None = None) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Decimal must be a string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid Decimal") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError("Invalid Decimal range")
    if maximum is not None and number > maximum:
        raise ValueError("Invalid Decimal range")
    return number


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, timedelta):
        return str(int(value.total_seconds()))
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class MarketCandle:
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
class VolumeRevision:
    revision_id: str
    scan_id: str
    point_at: datetime
    first_seen_at: datetime
    observed_at: datetime
    revision: int
    value: str
    source_version: str
    config_version: str


@dataclass(frozen=True)
class VolumeScan:
    scan_id: str
    attempted_at: datetime
    status: str
    point_count: int
    missing_bucket_count: int
    source_version: str
    config_version: str


@dataclass(frozen=True)
class DatasetConfig:
    first_anchor: datetime
    last_anchor: datetime
    cost_bps: Decimal
    cost_version: str
    feature_version: str = SCHEMA
    source_version: str = "gdelt-doc-timelinevol-bitcoin-v1"
    max_scan_age: timedelta = timedelta(minutes=30)
    max_point_lag: timedelta = timedelta(minutes=30)


@dataclass(frozen=True)
class Sample:
    anchor: datetime
    market_event_ids: tuple[str, str]
    market_payload_hashes: tuple[str, str]
    market_return_bps: str
    volume_mean: str
    volume_sum: str
    volume_nonzero_count: int
    volume_revision_ids: tuple[str, ...]
    volume_scan_id: str
    coverage_slots: int
    market_feature_hash: str
    aggregate_feature_hash: str
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
    cutoff: datetime
    source_versions: tuple[str, ...]


def _aligned(stamp: datetime, period: timedelta) -> bool:
    return int(stamp.timestamp()) % int(period.total_seconds()) == 0


def _validated_inputs(candles, revisions, scans, config, cutoff):
    first, last = _utc(config.first_anchor), _utc(config.last_anchor)
    if (
        first > last
        or not _aligned(first, HORIZON)
        or not _aligned(last, HORIZON)
        or last > cutoff
        or not config.cost_version
        or not config.feature_version
        or not config.source_version
        or not isinstance(config.cost_bps, Decimal)
        or not config.cost_bps.is_finite()
        or not 0 <= config.cost_bps < 10000
        or not timedelta() < config.max_scan_age <= HORIZON
        or not timedelta() <= config.max_point_lag <= HORIZON
    ):
        raise ValueError("Invalid dataset configuration")
    by_market = {}
    for row in candles:
        start = _utc(row.window_start)
        _utc(row.source_time)
        _utc(row.received_at)
        if (
            row.symbol != "BTC_USDT"
            or row.timeframe != "4h"
            or not _aligned(start, HORIZON)
            or not row.event_id
            or not HEX64.fullmatch(row.payload_hash)
            or not row.source
            or not row.config_version
            or not isinstance(row.final, bool)
        ):
            raise ValueError("Invalid market provenance")
        _decimal(row.close, positive=True)
        if row.source_time < start + HORIZON or row.received_at < start + HORIZON:
            raise ValueError("Market candle predates window close")
        if start in by_market and by_market[start] != row:
            raise ValueError("Conflicting market candle")
        by_market[start] = row
    by_point = {}
    revision_ids = set()
    for row in revisions:
        point, seen, observed = map(_utc, (row.point_at, row.first_seen_at, row.observed_at))
        if (
            not _aligned(point, BUCKET)
            or seen > observed
            or row.revision < 1
            or not row.revision_id
            or row.revision_id in revision_ids
            or not row.scan_id
            or not row.source_version
            or not row.config_version
        ):
            raise ValueError("Invalid GDELT revision provenance")
        if _decimal(row.value, maximum=Decimal(100)).as_tuple().exponent < -6:
            raise ValueError("Invalid GDELT value precision")
        revision_ids.add(row.revision_id)
        by_point.setdefault(point, []).append(row)
    for rows in by_point.values():
        rows.sort(key=lambda row: row.revision)
        if [row.revision for row in rows] != list(range(1, len(rows) + 1)):
            raise ValueError("GDELT revision sequence gap")
        if len({row.first_seen_at for row in rows}) != 1 or any(
            right.observed_at <= left.observed_at
            for left, right in zip(rows, rows[1:], strict=False)
        ):
            raise ValueError("GDELT revision time conflict")
        if rows[0].observed_at != rows[0].first_seen_at:
            raise ValueError("First revision must be first observation")
    by_scan = {}
    for row in scans:
        _utc(row.attempted_at)
        if (
            not row.scan_id
            or row.status not in {"ok", "gap", "error"}
            or not 0 <= row.point_count <= 96
            or not 0 <= row.missing_bucket_count <= 96
            or not row.source_version
            or not row.config_version
        ):
            raise ValueError("Invalid GDELT scan provenance")
        if row.scan_id in by_scan and by_scan[row.scan_id] != row:
            raise ValueError("Conflicting GDELT scan")
        by_scan[row.scan_id] = row
    for rows in by_point.values():
        for row in rows:
            scan = by_scan.get(row.scan_id)
            if scan is None or scan.attempted_at != row.observed_at or scan.status == "error":
                raise ValueError("Revision has no matching successful scan")
            if (
                row.source_version != config.source_version
                or scan.source_version != config.source_version
            ):
                raise ValueError("Unexpected GDELT source")
    return by_market, by_point, tuple(sorted(by_scan.values(), key=lambda row: row.attempted_at))


def build_dataset(
    candles: tuple[MarketCandle, ...],
    revisions: tuple[VolumeRevision, ...],
    scans: tuple[VolumeScan, ...],
    *,
    cutoff: datetime,
    config: DatasetConfig,
) -> Dataset:
    """Freeze all scheduled anchors; features use as-of state, labels use next 4h."""
    cutoff = _utc(cutoff)
    market, points, scan_rows = _validated_inputs(candles, revisions, scans, config, cutoff)
    samples, waits = [], []
    anchor = config.first_anchor
    while anchor <= config.last_anchor:
        prior = market.get(anchor - 2 * HORIZON)
        current = market.get(anchor - HORIZON)
        future = market.get(anchor)
        reason = None
        if prior is None or current is None:
            reason = "market_gap"
        elif not prior.final or not current.final:
            reason = "market_nonfinal"
        elif any(
            time > anchor for row in (prior, current) for time in (row.source_time, row.received_at)
        ):
            reason = "market_late_or_corrected"
        elif any(row.source_time > cutoff or row.received_at > cutoff for row in (prior, current)):
            reason = "market_unavailable"
        elif anchor + HORIZON > cutoff or future is None:
            reason = "label_missing"
        elif not future.final:
            reason = "label_nonfinal"
        elif future.source_time > cutoff or future.received_at > cutoff:
            reason = "label_unavailable"
        if reason:
            waits.append(Wait(anchor, reason))
            anchor += HORIZON
            continue
        latest_scan = next((row for row in reversed(scan_rows) if row.attempted_at <= anchor), None)
        if latest_scan is None:
            reason = "coverage_missing"
        elif anchor - latest_scan.attempted_at > config.max_scan_age:
            reason = "coverage_stale"
        elif latest_scan.status != "ok" or latest_scan.missing_bucket_count:
            reason = "coverage_gap"
        elif latest_scan.source_version != config.source_version:
            reason = "coverage_source_mismatch"
        if reason:
            waits.append(Wait(anchor, reason))
            anchor += HORIZON
            continue
        selected = []
        for index in range(16):
            point_at = anchor - HORIZON + index * BUCKET
            available = [row for row in points.get(point_at, ()) if row.observed_at <= anchor]
            if not available:
                reason = "volume_slot_missing"
                break
            row = available[-1]
            if row.first_seen_at > anchor or max(point_at, row.first_seen_at) > anchor:
                reason = "volume_slot_late"
                break
            if row.first_seen_at - point_at > config.max_point_lag:
                reason = "volume_slot_late"
                break
            if row.revision > 1:
                reason = "volume_revision_conflict"
                break
            selected.append(row)
        if reason:
            waits.append(Wait(anchor, reason))
            anchor += HORIZON
            continue
        market_features = {
            "anchor": anchor,
            "market_event_ids": (prior.event_id, current.event_id),
            "market_payload_hashes": (prior.payload_hash, current.payload_hash),
            "market_return_bps": format(
                (_decimal(current.close) / _decimal(prior.close) - 1) * 10000, "f"
            ),
        }
        values = tuple(_decimal(row.value) for row in selected)
        aggregate_features = {
            "anchor": anchor,
            "volume_revision_ids": tuple(row.revision_id for row in selected),
            "volume_mean": format(sum(values) / len(values), "f"),
            "volume_sum": format(sum(values), "f"),
            "volume_nonzero_count": sum(value > 0 for value in values),
            "volume_scan_id": latest_scan.scan_id,
            "coverage_slots": len(selected),
        }
        change = (_decimal(future.close) / _decimal(current.close) - 1) * 10000
        label = (
            "UP" if change > config.cost_bps else "DOWN" if change < -config.cost_bps else "NEUTRAL"
        )
        samples.append(
            Sample(
                anchor=anchor,
                market_event_ids=market_features["market_event_ids"],
                market_payload_hashes=market_features["market_payload_hashes"],
                market_return_bps=market_features["market_return_bps"],
                volume_mean=aggregate_features["volume_mean"],
                volume_sum=aggregate_features["volume_sum"],
                volume_nonzero_count=aggregate_features["volume_nonzero_count"],
                volume_revision_ids=aggregate_features["volume_revision_ids"],
                volume_scan_id=latest_scan.scan_id,
                coverage_slots=len(selected),
                market_feature_hash=_hash(market_features),
                aggregate_feature_hash=_hash(aggregate_features),
                feature_hash=_hash({"market": market_features, "aggregate": aggregate_features}),
                label=label,
                label_event_id=future.event_id,
                label_payload_hash=future.payload_hash,
            )
        )
        anchor += HORIZON
    visible_market = tuple(
        asdict(row)
        for row in sorted(
            (row for row in market.values() if row.received_at <= cutoff),
            key=lambda row: row.window_start,
        )
    )
    visible_revisions = tuple(
        asdict(row)
        for row in sorted(
            (row for rows in points.values() for row in rows if row.observed_at <= cutoff),
            key=lambda row: (row.point_at, row.revision),
        )
    )
    visible_scans = tuple(asdict(row) for row in scan_rows if row.attempted_at <= cutoff)
    source_checksum = _hash(
        {
            "market": visible_market,
            "revisions": visible_revisions,
            "scans": visible_scans,
        }
    )
    config_checksum = _hash(asdict(config))
    versions = tuple(
        sorted(
            {config.source_version}
            | {
                f"{row.source}:{row.config_version}"
                for row in market.values()
                if row.received_at <= cutoff
            }
            | {
                f"{row.source_version}:{row.config_version}"
                for rows in points.values()
                for row in rows
                if row.observed_at <= cutoff
            }
            | {
                f"{row.source_version}:{row.config_version}"
                for row in scan_rows
                if row.attempted_at <= cutoff
            }
        )
    )
    checksum = _hash(
        {
            "schema": SCHEMA,
            "cutoff": cutoff,
            "config": config_checksum,
            "source": source_checksum,
            "versions": versions,
            "samples": tuple(asdict(row) for row in samples),
            "waits": tuple(asdict(row) for row in waits),
        }
    )
    return Dataset(
        tuple(samples), tuple(waits), checksum, config_checksum, source_checksum, cutoff, versions
    )


def chronological_split(
    dataset: Dataset,
    *,
    train_end: datetime,
    validation_end: datetime,
    test_end: datetime,
    embargo: timedelta = HORIZON,
) -> tuple[tuple[Sample, ...], ...]:
    train_end, validation_end, test_end = map(_utc, (train_end, validation_end, test_end))
    if not train_end < validation_end < test_end or embargo < HORIZON:
        raise ValueError("Invalid split or embargo")
    groups: list[list[Sample]] = [[], [], []]
    for row in dataset.samples:
        anchor = row.anchor
        if anchor <= train_end - embargo:
            groups[0].append(row)
        elif train_end + embargo <= anchor <= validation_end - embargo:
            groups[1].append(row)
        elif validation_end + embargo <= anchor <= test_end:
            groups[2].append(row)
    return tuple(tuple(group) for group in groups)


def verify_dataset(dataset: Dataset) -> bool:
    try:
        if not all(
            HEX64.fullmatch(value)
            for value in (dataset.checksum, dataset.config_checksum, dataset.source_checksum)
        ):
            return False
        for row in dataset.samples:
            market = {
                "anchor": row.anchor,
                "market_event_ids": row.market_event_ids,
                "market_payload_hashes": row.market_payload_hashes,
                "market_return_bps": row.market_return_bps,
            }
            aggregate = {
                "anchor": row.anchor,
                "volume_revision_ids": row.volume_revision_ids,
                "volume_mean": row.volume_mean,
                "volume_sum": row.volume_sum,
                "volume_nonzero_count": row.volume_nonzero_count,
                "volume_scan_id": row.volume_scan_id,
                "coverage_slots": row.coverage_slots,
            }
            if (
                row.market_feature_hash != _hash(market)
                or row.aggregate_feature_hash != _hash(aggregate)
                or row.feature_hash != _hash({"market": market, "aggregate": aggregate})
            ):
                return False
        return dataset.checksum == _hash(
            {
                "schema": SCHEMA,
                "cutoff": dataset.cutoff,
                "config": dataset.config_checksum,
                "source": dataset.source_checksum,
                "versions": dataset.source_versions,
                "samples": tuple(asdict(row) for row in dataset.samples),
                "waits": tuple(asdict(row) for row in dataset.waits),
            }
        )
    except (AttributeError, TypeError, ValueError):
        return False
