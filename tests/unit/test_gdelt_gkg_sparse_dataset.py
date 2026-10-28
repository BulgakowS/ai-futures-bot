"""Pure sparse v3 as-of and WAIT invariants."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_futures.gdelt_gkg_sparse import RESERVE_BYTES, SOURCE_VERSION
from ai_futures.gdelt_gkg_sparse_dataset import (
    Attempt,
    Config,
    Market,
    Result,
    build_dataset,
    verify_dataset,
)
from ai_futures.market import MARKET_CANDLE_SOURCE_VERSION

ANCHOR = datetime(2026, 10, 19, 8, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def fixtures(*, btc=1):
    claim = Attempt(
        "claim", ANCHOR, ANCHOR - 15 * MINUTE, ANCHOR + MINUTE, RESERVE_BYTES, SOURCE_VERSION, "cfg"
    )
    result = Result(
        "result",
        "claim",
        ANCHOR + 2 * MINUTE,
        "ok",
        None,
        100,
        "a" * 64,
        10,
        btc,
        "-0.5" if btc else None,
        SOURCE_VERSION,
        "cfg",
    )
    market = tuple(
        Market(
            f"m{i}",
            f"c{i}",
            ANCHOR + (i - 2) * 4 * HOUR,
            1,
            ANCHOR + (i - 1) * 4 * HOUR + (MINUTE if i == 1 else timedelta()),
            ANCHOR + (i - 1) * 4 * HOUR + (MINUTE if i == 1 else timedelta()),
            ANCHOR + (i - 1) * 4 * HOUR,
            str(100 + i),
            f"{i + 1:064x}",
            MARKET_CANDLE_SOURCE_VERSION,
            "cfg",
            "rules",
        )
        for i in range(3)
    )
    config = Config(ANCHOR, ANCHOR, Decimal("10"), "scenario-v1")
    return (claim,), (result,), market, config


def build(claims, results, market, config, cutoff=ANCHOR + 5 * HOUR):
    return build_dataset(claims, results, market, cutoff=cutoff, config=config)


def test_one_slot_zero_and_future_label_never_enter_features():
    claims, results, market, config = fixtures(btc=0)
    base = build(claims, results, market, config)
    sample = base.samples[0]
    assert (
        sample.coverage_slots,
        sample.btc_rows,
        sample.tone_missing,
        sample.mean_btc_document_tone,
    ) == (1, 0, True, None)
    assert base.eligibility == "collect-only"
    assert verify_dataset(base)
    assert not verify_dataset(replace(base, samples=(replace(sample, btc_rows=7),)))
    future = replace(market[-1], close="80", payload_hash="e" * 64)
    changed = build(claims, results, market[:-1] + (future,), config)
    assert changed.samples[0].label != sample.label
    assert changed.samples[0].feature_hash == sample.feature_hash
    assert build(claims, results, market, config).checksum == base.checksum


def test_late_error_crash_and_missing_are_distinct_waits():
    claims, results, market, config = fixtures()
    cases = [
        ((), (), "sparse_claim_missing"),
        (claims, (), "sparse_claim_unknown"),
        (
            claims,
            (
                replace(
                    results[0],
                    status="error",
                    error_code="http",
                    archive_sha256=None,
                    total_rows=None,
                    btc_rows=None,
                    mean_btc_document_tone=None,
                ),
            ),
            "sparse_result_error",
        ),
        (claims, (replace(results[0], first_seen_at=ANCHOR + 16 * MINUTE),), "sparse_result_late"),
    ]
    for attempt_rows, result_rows, reason in cases:
        assert build(attempt_rows, result_rows, market, config).waits[0].reason == reason


def test_market_gap_predecision_correction_and_postdecision_stability():
    claims, results, market, config = fixtures()
    assert build(claims, results, market[:1] + market[2:], config).waits[0].reason == "market_gap"
    correction = replace(
        market[1],
        observation_id="m1-v2",
        version=2,
        observed_at=ANCHOR + 2 * MINUTE,
        close="101.5",
        payload_hash="d" * 64,
    )
    assert (
        build(claims, results, market + (correction,), config).waits[0].reason
        == "market_revision_conflict"
    )
    late = replace(correction, observed_at=ANCHOR + 3 * MINUTE)
    base = build(claims, results, market, config).samples[0]
    assert (
        build(claims, results, market + (late,), config).samples[0].feature_hash
        == base.feature_hash
    )


def test_label_cutoff_and_decision_limit():
    claims, results, market, config = fixtures()
    assert (
        build(claims, results, market, config, ANCHOR + 4 * HOUR).waits[0].reason
        == "label_unavailable"
    )
    late_label = replace(
        market[-1],
        first_seen_at=ANCHOR + 4 * HOUR + 16 * MINUTE,
        observed_at=ANCHOR + 4 * HOUR + 16 * MINUTE,
    )
    assert (
        build(claims, results, market[:-1] + (late_label,), config).waits[0].reason
        == "label_unavailable"
    )
    late_market = replace(
        market[1],
        first_seen_at=ANCHOR + 16 * MINUTE,
        observed_at=ANCHOR + 16 * MINUTE,
    )
    assert (
        build(claims, results, market[:1] + (late_market,) + market[2:], config).waits[0].reason
        == "market_gap"
    )


def test_label_correction_before_cutoff_changes_label_only():
    claims, results, market, config = fixtures()
    base = build(claims, results, market, config).samples[0]
    correction = replace(
        market[-1],
        observation_id="label-v2",
        version=2,
        observed_at=ANCHOR + 4 * HOUR + 10 * MINUTE,
        close="80",
        payload_hash="f" * 64,
    )
    changed = build(claims, results, market + (correction,), config).samples[0]
    assert changed.label != base.label
    assert changed.label_observation_id == "label-v2"
    assert changed.feature_hash == base.feature_hash
