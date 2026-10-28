"""Offline, source-specific GKG numeric 4h dataset; no model or trading wiring."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ai_futures.gdelt_dataset import (
    HEX64,
    HORIZON,
    MarketCandle,
    _aligned,
    _decimal,
    _hash,
    _utc,
)
from ai_futures.gdelt_gkg_forward import SOURCE_VERSION

SLOT = timedelta(minutes=15)
SCHEMA = "gdelt-gkg-market-dataset-v2"


@dataclass(frozen=True)
class Release:
    revision_id: str
    scan_id: str
    batch_at: datetime
    first_seen_at: datetime
    observed_at: datetime
    revision: int
    total_rows: int
    btc_rows: int
    mean_btc_document_tone: str | None
    archive_sha256: str
    manifest_md5: str
    manifest_size: int
    source_version: str
    config_version: str


@dataclass(frozen=True)
class Slot:
    observation_id: str
    scan_id: str
    slot_at: datetime
    observed_at: datetime
    status: str
    source_version: str
    config_version: str


@dataclass(frozen=True)
class Scan:
    scan_id: str
    attempted_at: datetime
    batch_at: datetime | None
    status: str
    source_version: str
    config_version: str


@dataclass(frozen=True)
class Config:
    first_anchor: datetime
    last_anchor: datetime
    cost_bps: Decimal
    cost_version: str
    feature_version: str = SCHEMA
    source_version: str = SOURCE_VERSION
    max_release_lag: timedelta = timedelta(minutes=30)
    max_decision_delay: timedelta = timedelta(minutes=15)


@dataclass(frozen=True)
class Sample:
    anchor: datetime
    decision_at: datetime
    target_close_at: datetime
    time_to_target_seconds: str
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


def _duration_seconds(start: datetime, end: datetime) -> str:
    delta = end - start
    microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return format(Decimal(microseconds) / Decimal(1_000_000), "f")


def _validate(candles, releases, slots, scans, config, cutoff):
    first, last = _utc(config.first_anchor), _utc(config.last_anchor)
    if (
        first > last
        or last > cutoff
        or not _aligned(first, HORIZON)
        or not _aligned(last, HORIZON)
        or (last - first) / HORIZON > 255
        or not isinstance(config.cost_bps, Decimal)
        or not config.cost_bps.is_finite()
        or not 0 <= config.cost_bps < 10000
        or not config.cost_version
        or config.feature_version != SCHEMA
        or config.source_version != SOURCE_VERSION
        or not timedelta() <= config.max_release_lag <= HORIZON
        or not timedelta() <= config.max_decision_delay <= SLOT
    ):
        raise ValueError("Invalid GKG dataset configuration")
    market = {}
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
            or row.source_time < start + HORIZON
            or row.received_at < start + HORIZON
        ):
            raise ValueError("Invalid market provenance")
        _decimal(row.close, positive=True)
        if start in market and market[start] != row:
            raise ValueError("Conflicting market candle")
        market[start] = row
    scan_map = {}
    for row in scans:
        _utc(row.attempted_at)
        if row.batch_at is not None:
            _utc(row.batch_at)
        if (
            not row.scan_id
            or row.status not in {"ok", "repeat", "error"}
            or row.source_version != SOURCE_VERSION
            or not row.config_version
            or row.scan_id in scan_map
        ):
            raise ValueError("Invalid GKG scan")
        scan_map[row.scan_id] = row
    release_map = {}
    ids = set()
    for row in releases:
        batch, seen, observed = map(_utc, (row.batch_at, row.first_seen_at, row.observed_at))
        scan = scan_map.get(row.scan_id)
        if (
            not _aligned(batch, SLOT)
            or seen > observed
            or row.revision < 1
            or not row.revision_id
            or row.revision_id in ids
            or scan is None
            or scan.status != "ok"
            or scan.batch_at != batch
            or scan.attempted_at > observed
            or row.source_version != SOURCE_VERSION
            or row.config_version != scan.config_version
            or not row.config_version
            or not isinstance(row.total_rows, int)
            or not isinstance(row.btc_rows, int)
            or not 0 <= row.btc_rows <= row.total_rows
            or row.total_rows == 0
            or not HEX64.fullmatch(row.archive_sha256)
            or len(row.manifest_md5) != 32
            or any(c not in "0123456789abcdef" for c in row.manifest_md5)
            or row.manifest_size <= 0
        ):
            raise ValueError("Invalid GKG release")
        if row.btc_rows == 0:
            if row.mean_btc_document_tone is not None:
                raise ValueError("Zero BTC cannot have tone")
        elif row.mean_btc_document_tone is None:
            raise ValueError("BTC tone missing")
        else:
            if not isinstance(row.mean_btc_document_tone, str):
                raise ValueError("BTC tone must be Decimal text")
            try:
                tone = Decimal(row.mean_btc_document_tone)
            except InvalidOperation as exc:
                raise ValueError("Invalid BTC tone") from exc
            if (
                not tone.is_finite()
                or len(tone.as_tuple().digits) > 64
                or abs(tone.adjusted()) > 30
            ):
                raise ValueError("Invalid BTC tone")
        ids.add(row.revision_id)
        release_map.setdefault(batch, []).append(row)
    for rows in release_map.values():
        rows.sort(key=lambda row: row.revision)
        if (
            [r.revision for r in rows] != list(range(1, len(rows) + 1))
            or rows[0].first_seen_at != rows[0].observed_at
            or len({r.first_seen_at for r in rows}) != 1
            or any(b.observed_at <= a.observed_at for a, b in zip(rows, rows[1:], strict=False))
        ):
            raise ValueError("GKG revision sequence conflict")
    slot_map = {}
    slot_ids = set()
    for row in slots:
        scan = scan_map.get(row.scan_id)
        if (
            not row.observation_id
            or row.observation_id in slot_ids
            or not _aligned(_utc(row.slot_at), SLOT)
            or not scan
            or _utc(row.observed_at) < scan.attempted_at
            or row.status not in {"ok", "unknown", "error", "repeat"}
            or row.source_version != SOURCE_VERSION
            or row.config_version != scan.config_version
            or (row.status == "ok" and (scan.status != "ok" or scan.batch_at != row.slot_at))
            or (
                row.status == "repeat" and (scan.status != "repeat" or scan.batch_at != row.slot_at)
            )
            or (row.status == "error" and scan.status != "error")
        ):
            raise ValueError("Invalid GKG slot observation")
        slot_ids.add(row.observation_id)
        slot_map.setdefault(row.slot_at, []).append(row)
    for rows in slot_map.values():
        rows.sort(key=lambda row: (row.observed_at, row.observation_id))
    return market, release_map, slot_map


def build_dataset(
    candles: tuple[MarketCandle, ...],
    releases: tuple[Release, ...],
    slots: tuple[Slot, ...],
    scans: tuple[Scan, ...],
    *,
    cutoff: datetime,
    config: Config,
) -> Dataset:
    """Build one sample or WAIT per scheduled anchor from an as-of frozen copy."""
    cutoff = _utc(cutoff)
    market, by_release, by_slot = _validate(candles, releases, slots, scans, config, cutoff)
    samples, waits = [], []
    anchor = config.first_anchor
    while anchor <= config.last_anchor:
        previous, current, future = (
            market.get(anchor - 2 * HORIZON),
            market.get(anchor - HORIZON),
            market.get(anchor),
        )
        reason = None
        if previous is None or current is None:
            reason = "market_gap"
        elif not previous.final or not current.final:
            reason = "market_nonfinal"
        elif any(t > anchor for t in (previous.source_time, previous.received_at)):
            reason = "market_late_or_corrected"
        elif max(current.source_time, current.received_at) > anchor + config.max_decision_delay:
            reason = "market_late_or_corrected"
        decision_at = max(anchor, current.source_time, current.received_at) if current else anchor
        selected, observations = [], []
        if reason is None and decision_at > cutoff:
            reason = "decision_unavailable"
        if reason is None:
            deadline = min(cutoff, anchor + config.max_decision_delay)
            for index in range(16):
                batch = anchor - HORIZON + index * SLOT
                candidates = [r for r in by_release.get(batch, ()) if r.observed_at <= deadline]
                observed = [r for r in by_slot.get(batch, ()) if r.observed_at <= deadline]
                ready = [r for r in observed if r.status == "ok"]
                if not candidates or not ready:
                    if observed and observed[-1].status in {"unknown", "error"}:
                        reason = "release_slot_unknown"
                    else:
                        reason = "release_slot_missing"
                    break
                decision_at = max(decision_at, candidates[0].observed_at, ready[0].observed_at)
        if reason is None and decision_at > cutoff:
            reason = "decision_unavailable"
        if reason is None:
            selected, observations = [], []
            for index in range(16):
                batch = anchor - HORIZON + index * SLOT
                candidates = [r for r in by_release.get(batch, ()) if r.observed_at <= decision_at]
                observed = [r for r in by_slot.get(batch, ()) if r.observed_at <= decision_at]
                if not candidates or not observed:
                    reason = "release_slot_missing"
                    break
                release, observation = candidates[-1], observed[-1]
                if observation.status in {"unknown", "error"}:
                    reason = "release_slot_unknown"
                    break
                if release.revision > 1 or len(candidates) > 1:
                    reason = "release_revision_conflict"
                    break
                if (
                    release.first_seen_at > decision_at
                    or max(batch, release.first_seen_at) > decision_at
                    or release.first_seen_at - batch > config.max_release_lag
                ):
                    reason = "release_slot_late"
                    break
                if observation.status == "ok" and observation.scan_id != release.scan_id:
                    reason = "release_observation_conflict"
                    break
                selected.append(release)
                observations.append(observation)
        if reason is None:
            if anchor + HORIZON > cutoff or future is None:
                reason = "label_missing"
            elif not future.final:
                reason = "label_nonfinal"
            elif future.source_time > cutoff or future.received_at > cutoff:
                reason = "label_unavailable"
        if reason:
            waits.append(Wait(anchor, reason))
            anchor += HORIZON
            continue
        m = {
            "anchor": anchor,
            "decision_at": decision_at,
            "market_event_ids": (previous.event_id, current.event_id),
            "market_payload_hashes": (previous.payload_hash, current.payload_hash),
            "market_return_bps": format(
                (_decimal(current.close) / _decimal(previous.close) - 1) * 10000, "f"
            ),
        }
        btc = sum(r.btc_rows for r in selected)
        denominator = sum(r.total_rows for r in selected)
        tone_sum = sum(
            Decimal(r.mean_btc_document_tone) * r.btc_rows for r in selected if r.btc_rows
        )
        a = {
            "anchor": anchor,
            "decision_at": decision_at,
            "btc_theme_count": btc,
            "denominator_rows": denominator,
            "btc_share": format(Decimal(btc) / Decimal(denominator), "f"),
            "mean_btc_document_tone": format(tone_sum / Decimal(btc), "f") if btc else None,
            "tone_missing": btc == 0,
            "release_revision_ids": tuple(r.revision_id for r in selected),
            "slot_observation_ids": tuple(r.observation_id for r in observations),
            "coverage_slots": 16,
        }
        change = (_decimal(future.close) / _decimal(current.close) - 1) * 10000
        label = (
            "UP" if change > config.cost_bps else "DOWN" if change < -config.cost_bps else "NEUTRAL"
        )
        samples.append(
            Sample(
                anchor,
                decision_at,
                anchor + HORIZON,
                _duration_seconds(decision_at, anchor + HORIZON),
                m["market_event_ids"],
                m["market_payload_hashes"],
                m["market_return_bps"],
                btc,
                denominator,
                a["btc_share"],
                a["mean_btc_document_tone"],
                a["tone_missing"],
                a["release_revision_ids"],
                a["slot_observation_ids"],
                16,
                _hash(m),
                _hash(a),
                _hash({"market": m, "aggregate": a}),
                label,
                future.event_id,
                future.payload_hash,
            )
        )
        anchor += HORIZON
    visible = {
        "market": tuple(
            asdict(r)
            for r in sorted(candles, key=lambda r: r.window_start)
            if r.received_at <= cutoff
        ),
        "releases": tuple(
            asdict(r)
            for r in sorted(releases, key=lambda r: (r.batch_at, r.revision))
            if r.observed_at <= cutoff
        ),
        "slots": tuple(
            asdict(r)
            for r in sorted(slots, key=lambda r: (r.slot_at, r.observed_at, r.observation_id))
            if r.observed_at <= cutoff
        ),
        "scans": tuple(
            asdict(r)
            for r in sorted(scans, key=lambda r: (r.attempted_at, r.scan_id))
            if r.attempted_at <= cutoff
        ),
    }
    source_checksum, config_checksum = _hash(visible), _hash(asdict(config))
    versions = tuple(
        sorted(
            {SCHEMA, SOURCE_VERSION}
            | {f"{r.source}:{r.config_version}" for r in candles if r.received_at <= cutoff}
            | {
                f"{r.source_version}:{r.config_version}"
                for r in releases
                if r.observed_at <= cutoff
            }
            | {f"{r.source_version}:{r.config_version}" for r in slots if r.observed_at <= cutoff}
            | {f"{r.source_version}:{r.config_version}" for r in scans if r.attempted_at <= cutoff}
        )
    )
    checksum = _hash(
        {
            "schema": SCHEMA,
            "cutoff": cutoff,
            "source": source_checksum,
            "config": config_checksum,
            "versions": versions,
            "samples": tuple(asdict(r) for r in samples),
            "waits": tuple(asdict(r) for r in waits),
        }
    )
    return Dataset(
        tuple(samples), tuple(waits), checksum, config_checksum, source_checksum, cutoff, versions
    )


def build_versioned_market_dataset(
    candles: tuple[MarketCandle, ...],
    releases: tuple[Release, ...],
    slots: tuple[Slot, ...],
    scans: tuple[Scan, ...],
    *,
    cutoff: datetime,
    config: Config,
    market_metadata: dict[str, tuple[str, str, str]] | None = None,
    label_observation_delay: timedelta | None = None,
) -> Dataset:
    """Choose each Hour4 version as of the anchor's actual GKG decision time."""
    cutoff = _utc(cutoff)
    if label_observation_delay is not None and not timedelta() <= label_observation_delay <= SLOT:
        raise ValueError("Invalid label observation delay")
    _validate((), releases, slots, scans, config, cutoff)
    by_window: dict[datetime, list[MarketCandle]] = {}
    for row in candles:
        if row.source != "rest" or not row.final:
            raise ValueError("Versioned GKG market requires final REST observations")
        by_window.setdefault(row.window_start, []).append(row)
    for versions in by_window.values():
        versions.sort(key=lambda row: (row.received_at, row.event_id))

    def visible(start: datetime, at: datetime, first: bool = False) -> MarketCandle | None:
        versions = [
            row
            for row in by_window.get(start, ())
            if row.received_at <= at and row.source_time <= at
        ]
        return (versions[0] if first else versions[-1]) if versions else None

    samples, waits, source_seals, source_versions = [], [], [], set()
    anchor = _utc(config.first_anchor)
    while anchor <= config.last_anchor:
        previous = visible(anchor - 2 * HORIZON, anchor)
        first_current = visible(
            anchor - HORIZON, min(cutoff, anchor + config.max_decision_delay), True
        )
        label_cutoff = (
            cutoff
            if label_observation_delay is None
            else min(cutoff, anchor + HORIZON + label_observation_delay)
        )
        future = visible(anchor, label_cutoff)
        selected = tuple(row for row in (previous, first_current, future) if row is not None)
        one = Config(
            anchor,
            anchor,
            config.cost_bps,
            config.cost_version,
            config.feature_version,
            config.source_version,
            config.max_release_lag,
            config.max_decision_delay,
        )
        provisional = build_dataset(selected, releases, slots, scans, cutoff=cutoff, config=one)
        if (
            label_observation_delay is not None
            and cutoff < anchor + HORIZON + label_observation_delay
            and provisional.samples
        ):
            result = Dataset(
                (),
                (Wait(anchor, "label_unavailable"),),
                provisional.checksum,
                provisional.config_checksum,
                provisional.source_checksum,
                cutoff,
                provisional.source_versions,
            )
        elif provisional.samples:
            decision_at = provisional.samples[0].decision_at
            feature_versions = tuple(
                row
                for start in (anchor - 2 * HORIZON, anchor - HORIZON)
                for row in by_window.get(start, ())
                if row.received_at <= decision_at and row.source_time <= decision_at
            )
            if len(feature_versions) > 2:
                result = Dataset(
                    (),
                    (Wait(anchor, "market_revision_conflict"),),
                    provisional.checksum,
                    provisional.config_checksum,
                    _hash(
                        {
                            "base": provisional.source_checksum,
                            "feature_versions": tuple(asdict(row) for row in feature_versions),
                        }
                    ),
                    cutoff,
                    provisional.source_versions,
                )
            else:
                selected = tuple(
                    row
                    for row in (
                        visible(anchor - 2 * HORIZON, decision_at),
                        visible(anchor - HORIZON, decision_at),
                        future,
                    )
                    if row is not None
                )
                result = build_dataset(selected, releases, slots, scans, cutoff=cutoff, config=one)
        else:
            result = provisional
        if (
            label_observation_delay is not None
            and result.waits
            and result.waits[0].reason == "label_missing"
        ):
            result = Dataset(
                (),
                (Wait(anchor, "label_unavailable"),),
                result.checksum,
                result.config_checksum,
                result.source_checksum,
                cutoff,
                result.source_versions,
            )
        samples.extend(result.samples)
        waits.extend(result.waits)
        if result.samples and market_metadata is not None:
            sample = result.samples[0]
            identifiers = (*sample.market_event_ids, sample.label_event_id)
            if any(identifier not in market_metadata for identifier in identifiers):
                raise ValueError("Missing market source/rules provenance")
            source_seals.append(
                _hash(
                    {
                        "source": result.source_checksum,
                        "market_metadata": tuple(
                            (identifier, market_metadata[identifier]) for identifier in identifiers
                        ),
                    }
                )
            )
            source_versions.update(
                f"{market_metadata[identifier][0]}:{market_metadata[identifier][1]}"
                for identifier in identifiers
            )
        else:
            source_seals.append(result.source_checksum)
        source_versions.update(result.source_versions)
        anchor += HORIZON
    source_checksum = _hash(tuple(source_seals))
    config_checksum = _hash(asdict(config))
    versions = tuple(sorted(source_versions))
    checksum = _hash(
        {
            "schema": SCHEMA,
            "cutoff": cutoff,
            "source": source_checksum,
            "config": config_checksum,
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
):
    train_end, validation_end, test_end = map(_utc, (train_end, validation_end, test_end))
    if not train_end < validation_end < test_end or embargo < HORIZON:
        raise ValueError("Invalid split or embargo")
    return (
        tuple(r for r in dataset.samples if r.anchor <= train_end - embargo),
        tuple(
            r
            for r in dataset.samples
            if train_end + embargo <= r.anchor <= validation_end - embargo
        ),
        tuple(r for r in dataset.samples if validation_end + embargo <= r.anchor <= test_end),
    )


def verify_dataset(dataset: Dataset) -> bool:
    """Verify the portable seal and that every feature hash excludes its label."""
    try:
        if not all(
            HEX64.fullmatch(value)
            for value in (dataset.checksum, dataset.config_checksum, dataset.source_checksum)
        ):
            return False
        for row in dataset.samples:
            market = {
                "anchor": row.anchor,
                "decision_at": row.decision_at,
                "market_event_ids": row.market_event_ids,
                "market_payload_hashes": row.market_payload_hashes,
                "market_return_bps": row.market_return_bps,
            }
            aggregate = {
                "anchor": row.anchor,
                "decision_at": row.decision_at,
                "btc_theme_count": row.btc_theme_count,
                "denominator_rows": row.denominator_rows,
                "btc_share": row.btc_share,
                "mean_btc_document_tone": row.mean_btc_document_tone,
                "tone_missing": row.tone_missing,
                "release_revision_ids": row.release_revision_ids,
                "slot_observation_ids": row.slot_observation_ids,
                "coverage_slots": row.coverage_slots,
            }
            if (
                not row.anchor <= row.decision_at <= row.anchor + SLOT
                or row.target_close_at != row.anchor + HORIZON
                or row.time_to_target_seconds
                != _duration_seconds(row.decision_at, row.target_close_at)
                or row.market_feature_hash != _hash(market)
                or row.aggregate_feature_hash != _hash(aggregate)
                or row.feature_hash != _hash({"market": market, "aggregate": aggregate})
            ):
                return False
        return dataset.checksum == _hash(
            {
                "schema": SCHEMA,
                "cutoff": dataset.cutoff,
                "source": dataset.source_checksum,
                "config": dataset.config_checksum,
                "versions": dataset.source_versions,
                "samples": tuple(asdict(r) for r in dataset.samples),
                "waits": tuple(asdict(r) for r in dataset.waits),
            }
        )
    except (AttributeError, TypeError, ValueError):
        return False
