"""Synthetic fixtures exercise the replay model; no fabricated production feed."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from ai_futures.backtesting import compare, digest, make_dataset, replay
from ai_futures.paper import RiskProfile
from ai_futures.strategies import StrategyConfig

D = Decimal
START = datetime(2025, 1, 1, tzinfo=UTC)
RULES = {
    "contractSize": "1",
    "priceUnit": "0.1",
    "volUnit": "1",
    "minVol": "1",
    "maxVol": "100",
    "maxLeverage": "3",
    "takerFeeRate": "0.001",
}


def config(strategy="trend_following"):
    return StrategyConfig(
        strategy=strategy,
        timeframe="1m",
        quantity="1",
        leverage="1",
        stop_distance="0.01",
        fast_period=2,
        slow_period=3,
        breakout_period=2,
        breakout_buffer="0",
        min_volume_ratio="1",
    )


def candles(closes):
    rows = []
    for i, value in enumerate(closes):
        start = START + timedelta(minutes=i)
        rows.append(
            SimpleNamespace(
                symbol="BTC_USDT",
                timeframe="1m",
                window_start=start,
                source_time=start + timedelta(minutes=1),
                received_at=start + timedelta(minutes=1),
                event_id=uuid5(NAMESPACE_URL, f"synthetic:{i}"),
                payload_hash=f"synthetic-{i}",
                final=True,
                source="rest",
                payload={
                    "open": str(value),
                    "high": str(value + 1),
                    "low": str(value - 1),
                    "close": str(value),
                    "vol": "100",
                },
            )
        )
    return rows


def dataset(closes=(98, 98, 97, 100, 101, 101, 98, 97), funding=()):
    return make_dataset(
        candles(closes),
        RULES,
        symbol="BTC_USDT",
        timeframe="1m",
        rules_version="synthetic-rules-v1",
        funding=funding,
    )


def test_repeatable_next_final_fill_and_causal_risk_chain():
    ds = dataset()
    first = replay(ds, config(), RiskProfile(), D("1000"), seed=7)
    second = replay(ds, config(), RiskProfile(), D("1000"), seed=7)
    assert first == second and digest(first) == digest(second)
    opened = next(x for x in first["decisions"] if x["action"] == "OPEN")
    assert any(x["reason"] == "STOP_BREACHED_CLOSE" for x in first["decisions"])
    assert (
        first["fills"][0]["occurred_at"]
        == (datetime.fromisoformat(opened["occurred_at"]) + timedelta(minutes=1)).isoformat()
    )
    assert (
        len(first["intents"])
        == len(first["verdicts"])
        == len(first["orders"])
        == len(first["fills"])
        == 2
    )
    for intent, verdict, order, fill in zip(
        first["intents"], first["verdicts"], first["orders"], first["fills"], strict=True
    ):
        assert verdict["intent_id"] == order["intent_id"] == intent["id"]
        assert order["verdict_id"] == verdict["id"] and fill["order_id"] == order["id"]
        assert verdict["outcome"] == "ALLOW" and verdict["occurred_at"] == fill["occurred_at"]
    assert all(D(x["wallet_delta"]) + D(x["offset_delta"]) == 0 for x in first["ledger"])
    assert first["positions"] == [] and first["account"]["mode"] == "backtest"


def test_future_extreme_and_nonfinal_candle_cannot_change_past_decisions():
    original = dataset()
    changed = dataset((98, 98, 97, 100, 101, 101, 98, 10000))
    a = replay(original, config(), RiskProfile(), D("1000"))
    b = replay(changed, config(), RiskProfile(), D("1000"))
    assert [(x["action"], x["reason"]) for x in a["decisions"][:7]] == [
        (x["action"], x["reason"]) for x in b["decisions"][:7]
    ]
    nonfinal = candles([98, 98, 97, 100])
    nonfinal[-1].final = False
    with pytest.raises(ValueError, match="REST-final"):
        make_dataset(
            nonfinal, RULES, symbol="BTC_USDT", timeframe="1m", rules_version="synthetic-rules-v1"
        )


def test_gap_cancels_pending_and_dataset_checksum_detects_change():
    rows = candles([98, 98, 97, 100, 101, 101])
    rows.pop(4)  # Signal at index 3 must not fill across missing next minute.
    ds = make_dataset(
        rows, RULES, symbol="BTC_USDT", timeframe="1m", rules_version="synthetic-rules-v1"
    )
    result = replay(ds, config(), RiskProfile(), D("1000"))
    assert ds["gaps"] and not result["fills"]
    assert any(x["reason"] == "CANDLE_GAP" for x in result["decisions"])
    altered = deepcopy(ds)
    altered["candles"][-1]["payload"]["close"] = "200"
    with pytest.raises(ValueError, match="checksum"):
        replay(altered, config(), RiskProfile(), D("1000"))


def test_decimal_costs_funding_and_comparison_contract():
    settlement = START + timedelta(minutes=6)
    ds = dataset(funding=[{"settleTime": settlement.isoformat(), "fundingRate": "0.001"}])
    report = replay(ds, config(), RiskProfile(), D("1000"))
    assert D(report["metrics"]["fees"]) == sum(D(x["fee"]) for x in report["fills"])
    assert D(report["metrics"]["funding"]) == D("-0.101000000000")
    assert D(report["metrics"]["slippage_cost"]) > 0
    assert D(report["metrics"]["net_pnl"]) == (
        D(report["metrics"]["gross_realized_pnl"])
        - D(report["metrics"]["fees"])
        + D(report["metrics"]["funding"])
    )
    assert any(x["reason"] == "FUNDING" for x in report["ledger"])
    other = replay(ds, config("breakout"), RiskProfile(), D("1000"))
    assert len(compare([report, other])["runs"]) == 2
    with pytest.raises(ValueError, match="Comparison mismatch"):
        compare([report, replay(ds, config(), RiskProfile(), D("900"))])
    with pytest.raises(ValueError, match="Comparison mismatch"):
        compare([report, replay(ds, config(), RiskProfile(), D("1000"), seed=1)])


def test_rejected_intent_has_verdict_without_order():
    cfg = config()
    cfg.quantity = D("100")
    result = replay(dataset(), cfg, RiskProfile(), D("1000"))
    assert result["intents"] and result["verdicts"][0]["outcome"] == "REJECT"
    assert not result["orders"] and not result["fills"]
