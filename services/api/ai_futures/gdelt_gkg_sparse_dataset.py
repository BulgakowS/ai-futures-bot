"""Pure one-slot GKG/immutable Hour4 research dataset; no trading path."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ai_futures.gdelt_dataset import HEX64, HORIZON, _aligned, _decimal, _hash, _utc
from ai_futures.gdelt_gkg_sparse import RESERVE_BYTES, SOURCE_VERSION
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION

SCHEMA = "gdelt-gkg-sparse-market-dataset-v3"
SLOT = timedelta(minutes=15)


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    anchor_at: datetime
    target_batch_at: datetime
    attempted_at: datetime
    reserved_bytes: int
    source_version: str
    config_version: str


@dataclass(frozen=True)
class Result:
    result_id: str
    attempt_id: str
    first_seen_at: datetime
    status: str
    error_code: str | None
    response_bytes: int
    archive_sha256: str | None
    total_rows: int | None
    btc_rows: int | None
    mean_btc_document_tone: str | None
    source_version: str
    config_version: str


@dataclass(frozen=True)
class Market:
    observation_id: str
    causal_id: str
    window_start: datetime
    version: int
    first_seen_at: datetime
    observed_at: datetime
    source_time: datetime
    close: str
    payload_hash: str
    source_version: str
    config_version: str
    rules_version: str
    source: str = "rest"
    symbol: str = "BTC_USDT"
    timeframe: str = "4h"


@dataclass(frozen=True)
class Config:
    first_anchor: datetime
    last_anchor: datetime
    cost_bps: Decimal
    cost_version: str
    feature_version: str = SCHEMA
    source_version: str = SOURCE_VERSION
    max_decision_delay: timedelta = SLOT
    label_observation_delay: timedelta = SLOT
    cost_provenance_frozen: bool = False


@dataclass(frozen=True)
class Sample:
    anchor: datetime
    decision_at: datetime
    target_close_at: datetime
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
    label: str
    label_observation_id: str
    label_payload_hash: str
    label_rules_version: str


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
    eligibility: str = "collect-only"
    eligibility_reason: str = "missing_cost_rules_provenance"


def _validate(attempts, results, market, config, cutoff):
    first, last = _utc(config.first_anchor), _utc(config.last_anchor)
    if (
        first > last
        or last > cutoff
        or not _aligned(first, HORIZON)
        or not _aligned(last, HORIZON)
        or (last - first) / HORIZON > 335
        or config.feature_version != SCHEMA
        or config.source_version != SOURCE_VERSION
        or not isinstance(config.cost_bps, Decimal)
        or not config.cost_bps.is_finite()
        or not Decimal(0) <= config.cost_bps < Decimal(10000)
        or not config.cost_version
        or not timedelta() <= config.max_decision_delay <= SLOT
        or config.label_observation_delay != SLOT
    ):
        raise ValueError("Invalid sparse dataset configuration")
    by_attempt = {}
    attempt_ids = set()
    for row in attempts:
        anchor = _utc(row.anchor_at)
        _utc(row.target_batch_at)
        _utc(row.attempted_at)
        if (
            not _aligned(anchor, HORIZON)
            or row.target_batch_at != anchor - SLOT
            or not anchor <= row.attempted_at <= anchor + SLOT
            or row.reserved_bytes != RESERVE_BYTES
            or row.source_version != SOURCE_VERSION
            or not row.config_version
            or not row.attempt_id
            or row.attempt_id in attempt_ids
            or anchor in by_attempt
        ):
            raise ValueError("Invalid sparse claim")
        by_attempt[anchor] = row
        attempt_ids.add(row.attempt_id)
    by_result = {}
    result_ids = set()
    for row in results:
        _utc(row.first_seen_at)
        if (
            row.attempt_id not in attempt_ids
            or row.attempt_id in by_result
            or not row.result_id
            or row.result_id in result_ids
            or row.status not in {"ok", "error"}
            or row.response_bytes < 0
            or row.response_bytes > RESERVE_BYTES
            or row.source_version != SOURCE_VERSION
            or not row.config_version
        ):
            raise ValueError("Invalid sparse result")
        attempt = next(a for a in attempts if a.attempt_id == row.attempt_id)
        if row.first_seen_at < attempt.attempted_at or row.config_version != attempt.config_version:
            raise ValueError("Invalid sparse result chronology")
        if row.status == "ok":
            if (
                row.error_code is not None
                or row.response_bytes <= 0
                or not row.archive_sha256
                or not HEX64.fullmatch(row.archive_sha256)
                or not isinstance(row.total_rows, int)
                or row.total_rows <= 0
                or not isinstance(row.btc_rows, int)
                or not 0 <= row.btc_rows <= row.total_rows
                or (row.btc_rows == 0) != (row.mean_btc_document_tone is None)
            ):
                raise ValueError("Invalid successful sparse result")
            if row.btc_rows:
                try:
                    tone = Decimal(row.mean_btc_document_tone)
                except (InvalidOperation, TypeError) as exc:
                    raise ValueError("Invalid BTC tone") from exc
                if (
                    not tone.is_finite()
                    or len(tone.as_tuple().digits) > 64
                    or abs(tone.adjusted()) > 30
                ):
                    raise ValueError("Invalid BTC tone")
        elif (
            row.error_code is None
            or row.archive_sha256 is not None
            or row.total_rows is not None
            or row.btc_rows is not None
            or row.mean_btc_document_tone is not None
        ):
            raise ValueError("Invalid failed sparse result")
        by_result[row.attempt_id] = row
        result_ids.add(row.result_id)
    by_window = {}
    for row in market:
        start = _utc(row.window_start)
        for stamp in (row.first_seen_at, row.observed_at, row.source_time):
            _utc(stamp)
        if (
            not _aligned(start, HORIZON)
            or row.version < 1
            or not row.observation_id
            or not row.causal_id
            or row.source_version != MARKET_CANDLE_SOURCE_VERSION
            or row.source != "rest"
            or row.symbol != "BTC_USDT"
            or row.timeframe != "4h"
            or not row.config_version
            or not row.rules_version
            or not HEX64.fullmatch(row.payload_hash)
            or row.source_time < start + HORIZON
            or row.observed_at < start + HORIZON
            or row.first_seen_at > row.observed_at
        ):
            raise ValueError("Invalid immutable Hour4 observation")
        _decimal(row.close, positive=True)
        by_window.setdefault(start, []).append(row)
    seen_ids = set()
    for rows in by_window.values():
        rows.sort(key=lambda row: row.version)
        if rows[0].first_seen_at != rows[0].observed_at:
            raise ValueError("Invalid immutable Hour4 first observation")
        for index, row in enumerate(rows, 1):
            if (
                row.version != index
                or row.observation_id in seen_ids
                or (
                    index > 1
                    and (
                        row.first_seen_at != rows[0].first_seen_at
                        or row.causal_id != rows[0].causal_id
                        or row.observed_at <= rows[index - 2].observed_at
                    )
                )
            ):
                raise ValueError("Invalid immutable Hour4 sequence")
            seen_ids.add(row.observation_id)
    return by_attempt, by_result, by_window


def build_dataset(
    attempts: tuple[Attempt, ...],
    results: tuple[Result, ...],
    market: tuple[Market, ...],
    *,
    cutoff: datetime,
    config: Config,
) -> Dataset:
    """Seal every scheduled anchor as one sample or a causal WAIT."""
    cutoff = _utc(cutoff)
    by_attempt, by_result, by_window = _validate(attempts, results, market, config, cutoff)
    samples, waits = [], []
    anchor = _utc(config.first_anchor)
    while anchor <= config.last_anchor:
        decision_limit = min(cutoff, anchor + config.max_decision_delay)
        claim = by_attempt.get(anchor)
        result = by_result.get(claim.attempt_id) if claim else None
        reason = None
        if claim is None or claim.attempted_at > decision_limit:
            reason = "sparse_claim_missing"
        elif result is None:
            reason = "sparse_claim_unknown"
        elif result.status == "error":
            reason = "sparse_result_error"
        elif result.first_seen_at > decision_limit:
            reason = "sparse_result_late"

        def visible(start, at):
            return [
                r for r in by_window.get(start, ()) if r.observed_at <= at and r.source_time <= at
            ]

        current_first = visible(anchor - HORIZON, decision_limit)
        if reason is None and not current_first:
            reason = "market_gap"
        decision_at = (
            max(
                anchor,
                result.first_seen_at,
                current_first[0].observed_at,
                current_first[0].source_time,
            )
            if reason is None
            else anchor
        )
        previous = visible(anchor - 2 * HORIZON, decision_at)
        current = visible(anchor - HORIZON, decision_at)
        if reason is None and (not previous or not current):
            reason = "market_gap"
        if reason is None and (len(previous) > 1 or len(current) > 1):
            reason = "market_revision_conflict"
        if reason is None and decision_at > decision_limit:
            reason = "decision_late"
        label_at = anchor + HORIZON + config.label_observation_delay
        label = visible(anchor, min(cutoff, label_at)) if cutoff >= label_at else []
        if reason is None and not label:
            reason = "label_unavailable"
        if reason is not None:
            waits.append(Wait(anchor, reason))
            anchor += HORIZON
            continue
        before, now, future = previous[0], current[0], label[-1]
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
            "attempt_id": claim.attempt_id,
            "result_id": result.result_id,
            "result_config_version": result.config_version,
            "source_version": result.source_version,
            "target_batch_at": claim.target_batch_at,
            "archive_sha256": result.archive_sha256,
            "total_rows": result.total_rows,
            "btc_rows": result.btc_rows,
            "btc_share": format(Decimal(result.btc_rows) / Decimal(result.total_rows), "f"),
            "mean_btc_document_tone": result.mean_btc_document_tone,
            "tone_missing": result.btc_rows == 0,
            "coverage_slots": 1,
        }
        change = (_decimal(future.close) / _decimal(now.close) - 1) * 10000
        direction = (
            "UP" if change > config.cost_bps else "DOWN" if change < -config.cost_bps else "NEUTRAL"
        )
        samples.append(
            Sample(
                anchor,
                decision_at,
                anchor + HORIZON,
                m["market_observation_ids"],
                m["market_payload_hashes"],
                m["market_rules_versions"],
                m["market_config_versions"],
                m["market_return_bps"],
                claim.attempt_id,
                result.result_id,
                result.config_version,
                claim.target_batch_at,
                result.archive_sha256,
                result.total_rows,
                result.btc_rows,
                a["btc_share"],
                result.mean_btc_document_tone,
                a["tone_missing"],
                1,
                _hash(m),
                _hash(a),
                _hash({"market": m, "aggregate": a}),
                direction,
                future.observation_id,
                future.payload_hash,
                future.rules_version,
            )
        )
        anchor += HORIZON
    visible_source = {
        "attempts": tuple(
            asdict(r)
            for r in sorted(attempts, key=lambda r: r.anchor_at)
            if r.attempted_at <= cutoff
        ),
        "results": tuple(
            asdict(r)
            for r in sorted(results, key=lambda r: r.attempt_id)
            if r.first_seen_at <= cutoff
        ),
        "market": tuple(
            asdict(r)
            for r in sorted(market, key=lambda r: (r.window_start, r.version))
            if r.observed_at <= cutoff
        ),
    }
    source_checksum = _hash(visible_source)
    config_checksum = _hash(asdict(config))
    versions = tuple(
        sorted(
            {SCHEMA, SOURCE_VERSION, MARKET_CANDLE_SOURCE_VERSION}
            | {
                f"{r.source_version}:{r.config_version}"
                for r in attempts
                if r.attempted_at <= cutoff
            }
            | {
                f"{r.source_version}:{r.config_version}:{r.rules_version}"
                for r in market
                if r.observed_at <= cutoff
            }
        )
    )
    checksum = _hash(
        {
            "schema": SCHEMA,
            "source": source_checksum,
            "config": config_checksum,
            "cutoff": cutoff,
            "versions": versions,
            "samples": tuple(asdict(r) for r in samples),
            "waits": tuple(asdict(r) for r in waits),
        }
    )
    return Dataset(
        tuple(samples), tuple(waits), checksum, config_checksum, source_checksum, cutoff, versions
    )


def verify_dataset(dataset: Dataset) -> bool:
    """Verify portable seals without treating a seal as real-source attestation."""
    try:
        if not all(
            HEX64.fullmatch(value)
            for value in (dataset.checksum, dataset.config_checksum, dataset.source_checksum)
        ):
            return False
        if dataset.eligibility != "collect-only":
            return False
        for row in dataset.samples:
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
                "coverage_slots": row.coverage_slots,
            }
            if (
                not row.anchor <= row.decision_at <= row.anchor + SLOT
                or row.target_close_at != row.anchor + HORIZON
                or row.coverage_slots != 1
                or row.market_feature_hash != _hash(market)
                or row.aggregate_feature_hash != _hash(aggregate)
                or row.feature_hash != _hash({"market": market, "aggregate": aggregate})
            ):
                return False
        return dataset.checksum == _hash(
            {
                "schema": SCHEMA,
                "source": dataset.source_checksum,
                "config": dataset.config_checksum,
                "cutoff": dataset.cutoff,
                "versions": dataset.source_versions,
                "samples": tuple(asdict(r) for r in dataset.samples),
                "waits": tuple(asdict(r) for r in dataset.waits),
            }
        )
    except (AttributeError, TypeError, ValueError):
        return False
