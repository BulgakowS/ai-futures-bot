from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_futures.strategies import StrategyConfig, evaluate


def config(strategy):
    return StrategyConfig(
        strategy=strategy,
        timeframe="1m",
        quantity="10",
        leverage="1",
        stop_distance="0.01",
        fast_period=2,
        slow_period=3,
        breakout_period=2,
        breakout_buffer="0",
        min_volume_ratio="1",
    )


def candles(closes, volumes=None):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    volumes = volumes or [10] * len(closes)
    return [
        SimpleNamespace(
            window_start=start + timedelta(minutes=i),
            final=True,
            source="rest",
            payload={
                "close": str(close),
                "high": str(close + 1),
                "low": str(close - 1),
                "vol": str(volumes[i]),
            },
        )
        for i, close in enumerate(closes)
    ]


@pytest.mark.parametrize(
    "strategy,closes,reason",
    [
        ("trend_following", [98, 98, 97, 100], "FAST_CROSSED_ABOVE_SLOW"),
        ("pullback", [97, 99, 96, 100], "PULLBACK_RECOVERED"),
        ("breakout", [96, 97, 98, 100], "RANGE_BROKEN"),
    ],
)
def test_three_deterministic_signals(strategy, closes, reason):
    rows = candles(closes)
    assert evaluate(config(strategy), rows).reason == reason
    assert evaluate(config(strategy), rows).action == "OPEN"
    rows[-1].final = False
    assert evaluate(config(strategy), rows).reason == "NONFINAL_CANDLE"
    rows[-1].final = True
    rows[-1].window_start += timedelta(minutes=1)
    assert evaluate(config(strategy), rows).reason == "CANDLE_GAP"


def test_wait_volume_and_position_exit():
    rows = candles([96, 97, 98, 100], [10, 10, 10, 0])
    assert evaluate(config("breakout"), rows).reason == "VOLUME_FILTER"
    held = SimpleNamespace(direction="LONG")
    assert evaluate(config("breakout"), candles([100, 100, 100, 95]), held).action == "CLOSE"


def test_explicit_parameters_required():
    with pytest.raises(ValueError):
        StrategyConfig.model_validate(
            config("trend_following").model_dump() | {"fast_period": 5}
        )
