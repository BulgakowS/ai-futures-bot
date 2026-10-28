"""Fail-closed boundaries of the inert Post Only cost preview."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.live_cost_preview import evaluate_live_cost_preview

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def inputs():
    draft = {
        "max_pairs": 3,
        "per_pair_notional": {"unit": "USDT", "value": "100"},
        "aggregate_notional": {"unit": "USDT", "value": "300"},
        "max_leverage": "2",
        "take_profit_pct": "2",
        "stop_loss_pct": "1",
        "max_daily_loss": {"unit": "USDT", "value": "10"},
        "max_round_trip_cost_pct": "0.35",
    }
    candidate = {"symbol": "BTC_USDT", "notional_usdt": "100", "order_type": "POST_ONLY"}
    fee = {
        "symbol": "BTC_USDT",
        "observed_at": NOW,
        "source_id": "authenticated-account-fee",
        "authenticated": True,
        "maker_rate": "0.0001",
        "taker_rate": "0.0004",
    }
    market = {
        "symbol": "BTC_USDT",
        "observed_at": NOW,
        "source_id": "validated-depth",
        "validated": True,
        "bid": "99",
        "ask": "101",
        "executable_notional_usdt": "100",
    }
    reserves = {"exit_slippage_pct": "0.1", "funding_reserve_pct": "0.1"}
    return draft, candidate, fee, market, reserves


def evaluate(parts=None, **overrides):
    draft, candidate, fee, market, reserves = parts or inputs()
    return evaluate_live_cost_preview(
        draft,
        config_version=overrides.get("config_version", "v1"),
        candidate=candidate,
        account_fee=fee,
        market=market,
        reserves=reserves,
        now=overrides.get("now", NOW),
        max_age=overrides.get("max_age", timedelta(seconds=20)),
    )


def test_full_spread_fees_reserves_and_boundary_still_disable_live():
    parts = inputs()
    parts[0]["max_round_trip_cost_pct"] = "2.25"
    result = evaluate(parts)
    assert result.preview_passed and not result.live_allowed
    assert result.estimated_round_trip_cost_pct == Decimal("2.25")
    assert result.reasons == ("COST_PREVIEW_PASS_LIVE_DISABLED",)


def test_over_budget_and_rebate_cannot_subsidize_exit():
    parts = inputs()
    parts[2]["maker_rate"] = "-0.001"
    parts[0]["max_round_trip_cost_pct"] = "2.23"
    result = evaluate(parts)
    assert result.estimated_round_trip_cost_pct == Decimal("2.24")
    assert result.reasons == ("ROUND_TRIP_COST_OVER_BUDGET",)
    assert not result.live_allowed


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda p: p[0].pop("max_round_trip_cost_pct"), "COST_CONFIG_INVALID"),
        (lambda p: p[1].update(order_type="MARKET"), "POST_ONLY_REQUIRED"),
        (lambda p: p[2].update(symbol="ETH_USDT"), "EVIDENCE_SYMBOL_MISMATCH"),
        (lambda p: p[2].update(authenticated=False), "ACCOUNT_FEE_UNKNOWN"),
        (lambda p: p[2].update(observed_at=NOW - timedelta(minutes=1)), "ACCOUNT_FEE_STALE"),
        (lambda p: p[3].update(observed_at=NOW + timedelta(seconds=1)), "MARKET_COST_STALE"),
        (lambda p: p[3].update(validated=False), "MARKET_COST_UNKNOWN"),
        (lambda p: p[3].update(bid="101"), "MARKET_COST_UNKNOWN"),
        (lambda p: p[3].update(executable_notional_usdt="99"), "LIQUIDITY_INSUFFICIENT"),
        (lambda p: p[4].update(exit_slippage_pct="-0.1"), "COST_RESERVES_UNKNOWN"),
        (lambda p: p[2].update(maker_rate=0.0001), "ACCOUNT_FEE_UNKNOWN"),
    ],
)
def test_unknown_or_inconsistent_evidence_denies(change, reason):
    parts = inputs()
    change(parts)
    result = evaluate(parts)
    assert reason in result.reasons
    assert not result.preview_passed and not result.live_allowed
    assert result.estimated_round_trip_cost_pct is None


def test_missing_inputs_and_version_deny():
    for index, reason in (
        (1, "CANDIDATE_MISSING"),
        (2, "ACCOUNT_FEE_MISSING"),
        (3, "MARKET_COST_MISSING"),
        (4, "COST_RESERVES_MISSING"),
    ):
        parts = list(inputs())
        parts[index] = None
        assert reason in evaluate(parts).reasons
    assert evaluate(config_version=None).reasons == ("CONFIG_VERSION_MISSING",)
