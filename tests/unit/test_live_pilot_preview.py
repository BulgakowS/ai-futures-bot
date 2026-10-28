"""Boundaries and denial behavior of the inert Live pilot exposure preview."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.live_pilot_preview import evaluate_live_pilot_preview

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def draft(unit="USDT"):
    return {
        "max_pairs": 3,
        "per_pair_notional": {"unit": unit, "value": "500" if unit == "USDT" else "5"},
        "aggregate_notional": {"unit": unit, "value": "1000" if unit == "USDT" else "10"},
        "max_leverage": "5",
        "take_profit_pct": "2",
        "stop_loss_pct": "1",
        "max_daily_loss": {"unit": unit, "value": "100" if unit == "USDT" else "1"},
        "max_round_trip_cost_pct": "0.8",
    }


def evidence(**extra):
    return {"observed_at": NOW, "source_id": "verified-snapshot", **extra}


def rule(symbol, price="100", maximum="10", size="1"):
    return evidence(
        symbol=symbol,
        contract_size=size,
        reference_price=price,
        max_leverage=maximum,
        validated=True,
    )


def exposure(symbol, contracts, price="100", leverage="2"):
    return {
        "symbol": symbol,
        "remaining_contracts": contracts,
        "recorded_price": price,
        "leverage": leverage,
    }


def snapshot():
    return {
        "snapshot_id": "account-42",
        "equity": evidence(value="10000"),
        "daily_loss": evidence(value="0"),
        "positions": evidence(entries=[], complete=True, states_known=True),
        "pending_entries": evidence(entries=[], complete=True, states_known=True),
        "rules": [rule("BTC_USDT")],
    }


def candidate(symbol="BTC_USDT", contracts="1", side="LONG"):
    return evidence(
        symbol=symbol,
        side=side,
        contracts=contracts,
        entry_price="100",
        leverage="2",
        stop_price="99" if side == "LONG" else "101",
        take_price="102" if side == "LONG" else "98",
    )


def evaluate(config=None, state=None, intent=None):
    return evaluate_live_pilot_preview(
        draft() if config is None else config,
        config_version="pilot-v2",
        snapshot=snapshot() if state is None else state,
        candidate=candidate() if intent is None else intent,
        now=NOW,
        max_age=timedelta(seconds=20),
    )


def test_exact_boundary_passes_but_live_remains_disabled():
    state = snapshot()
    state["positions"]["entries"] = [exposure("BTC_USDT", "3")]
    state["pending_entries"]["entries"] = [exposure("BTC_USDT", "1")]
    result = evaluate(state=state)
    assert result.preview_passed
    assert not result.live_allowed
    assert result.reasons == ("PREVIEW_PASS_LIVE_DISABLED",)
    assert result.pair_notional_usdt == result.aggregate_notional_usdt == Decimal("500")
    assert result.distinct_pairs == 1


def test_positions_pending_and_candidate_sum_without_netting():
    state = snapshot()
    state["positions"]["entries"] = [exposure("BTC_USDT", "2")]
    state["pending_entries"]["entries"] = [exposure("BTC_USDT", "2")]
    intent = candidate(contracts="2")
    result = evaluate(state=state, intent=intent)
    assert not result.preview_passed
    assert "PER_PAIR_NOTIONAL_BREACH" in result.reasons
    assert result.pair_notional_usdt == Decimal("600")


def test_second_intent_same_pair_counts_exposure_once_but_both_notionals():
    state = snapshot()
    state["pending_entries"]["entries"] = [exposure("BTC_USDT", "4")]
    result = evaluate(state=state, intent=candidate(contracts="2"))
    assert result.distinct_pairs == 1
    assert result.pair_notional_usdt == Decimal("600")


def test_three_pair_limit_and_aggregate_limit():
    state = snapshot()
    state["rules"].extend([rule("ETH_USDT"), rule("SOL_USDT"), rule("XRP_USDT")])
    state["positions"]["entries"] = [exposure("ETH_USDT", "4"), exposure("SOL_USDT", "4")]
    state["pending_entries"]["entries"] = [exposure("XRP_USDT", "2")]
    result = evaluate(state=state, intent=candidate(contracts="1"))
    assert "MAX_PAIRS_BREACH" in result.reasons
    assert result.distinct_pairs == 4
    assert result.aggregate_notional_usdt == Decimal("1100")
    assert "AGGREGATE_NOTIONAL_BREACH" in result.reasons


def test_percent_equity_is_whole_percent_and_full_notional_includes_leverage():
    state = snapshot()
    state["equity"]["value"] = "1000"
    intent = candidate(contracts="0.5")
    intent["leverage"] = "5"
    result = evaluate(config=draft("PCT_EQUITY"), state=state, intent=intent)
    assert result.preview_passed
    assert result.pair_notional_usdt == Decimal("50")
    intent["contracts"] = "0.50000000000000000000000000001"
    assert (
        "PER_PAIR_NOTIONAL_BREACH"
        in evaluate(config=draft("PCT_EQUITY"), state=state, intent=intent).reasons
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda s, i: s["equity"].update(value="0"), "ACCOUNT_VALUE_INVALID"),
        (
            lambda s, i: s["equity"].update(observed_at=NOW - timedelta(minutes=1)),
            "SOURCE_STALE_OR_UNKNOWN",
        ),
        (lambda s, i: s["positions"].update(complete=False), "EXPOSURE_INCOMPLETE"),
        (lambda s, i: s["pending_entries"].update(states_known=False), "ORDER_STATE_UNKNOWN"),
        (lambda s, i: s["rules"][0].update(validated=False), "RULE_STALE_OR_UNVALIDATED"),
        (lambda s, i: s["rules"].clear(), "RULE_MISSING"),
        (lambda s, i: s["daily_loss"].update(value="100"), "DAILY_LOSS_BREACH"),
        (lambda s, i: i.update(leverage="11"), "LEVERAGE_BREACH"),
        (lambda s, i: i.update(stop_price="101"), "PROTECTION_DIRECTION_INVALID"),
        (lambda s, i: i.update(stop_price="98"), "STOP_LOSS_BREACH"),
        (lambda s, i: i.update(take_price="101"), "TAKE_PROFIT_BREACH"),
    ],
)
def test_fail_closed_conditions(change, reason):
    state, intent = snapshot(), candidate()
    change(state, intent)
    result = evaluate(state=state, intent=intent)
    assert not result.preview_passed
    assert not result.live_allowed
    assert reason in result.reasons


def test_short_protection_and_instrument_leverage():
    state = snapshot()
    state["rules"][0]["max_leverage"] = "1"
    result = evaluate(state=state, intent=candidate(side="SHORT"))
    assert "LEVERAGE_BREACH" in result.reasons
    state["rules"][0]["max_leverage"] = "2"
    assert evaluate(state=state, intent=candidate(side="SHORT")).preview_passed


def test_existing_leverage_breach_and_percent_daily_loss_boundary():
    state = snapshot()
    state["positions"]["entries"] = [exposure("BTC_USDT", "1", leverage="6")]
    assert "EXISTING_LEVERAGE_BREACH" in evaluate(state=state).reasons
    state["positions"]["entries"] = []
    state["equity"]["value"] = "1000"
    state["daily_loss"]["value"] = "10"
    assert "DAILY_LOSS_BREACH" in evaluate(config=draft("PCT_EQUITY"), state=state).reasons


def test_conservative_price_and_unfilled_remainder_only():
    state = snapshot()
    state["pending_entries"]["entries"] = [exposure("BTC_USDT", "3", "110")]
    state["positions"]["entries"] = [exposure("BTC_USDT", "1")]
    result = evaluate(state=state)
    assert result.pair_notional_usdt == Decimal("530")
    assert "PER_PAIR_NOTIONAL_BREACH" in result.reasons


def test_missing_invalid_or_binary_float_fails_closed():
    assert evaluate_live_pilot_preview(
        None,
        config_version=None,
        snapshot=snapshot(),
        candidate=candidate(),
        now=NOW,
        max_age=timedelta(seconds=20),
    ).reasons == ("PILOT_CONFIG_MISSING",)
    state = snapshot()
    state["rules"][0]["contract_size"] = 1.0
    assert evaluate(state=state).reasons == ("SNAPSHOT_OR_INTENT_INVALID",)
    state = snapshot()
    state["pending_entries"]["states_known"] = "true"
    assert evaluate(state=state).reasons == ("SNAPSHOT_OR_INTENT_INVALID",)
    bad = deepcopy(draft())
    bad["max_pairs"] = 4
    assert evaluate(config=bad).reasons == ("PILOT_CONFIG_INVALID",)
    assert evaluate(intent=candidate(symbol="BTC_USD")).reasons == ("SNAPSHOT_OR_INTENT_INVALID",)
    assert (
        "CONFIG_VERSION_MISSING"
        in evaluate_live_pilot_preview(
            draft(),
            config_version=" ",
            snapshot=snapshot(),
            candidate=candidate(),
            now=NOW,
            max_age=timedelta(seconds=20),
        ).reasons
    )
