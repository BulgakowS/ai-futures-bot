"""Deterministic signals from contiguous REST-final candles only."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from ai_futures.schemas import ExactDecimal, Schema

D = Decimal
STEP = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}


class StrategyConfig(Schema):
    strategy: Literal["trend_following", "pullback", "breakout"]
    timeframe: Literal["1m", "5m", "15m"]
    quantity: ExactDecimal = Field(gt=0)
    leverage: ExactDecimal = Field(ge=1, le=3)
    stop_distance: ExactDecimal = Field(gt=0, lt=D("0.5"))
    fast_period: int = Field(ge=2, le=200)
    slow_period: int = Field(ge=3, le=400)
    breakout_period: int = Field(ge=2, le=200)
    breakout_buffer: ExactDecimal = Field(ge=0, le=D("0.1"))
    min_volume_ratio: ExactDecimal = Field(ge=0, le=10)

    @model_validator(mode="after")
    def valid_periods(self):
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be below slow_period")
        return self

    @property
    def history(self):
        return max(self.slow_period + 1, self.breakout_period + 1)


@dataclass(frozen=True)
class Signal:
    action: Literal["WAIT", "OPEN", "CLOSE"]
    reason: str
    direction: Literal["LONG", "SHORT"] | None = None


def _mean(values):
    return sum(values, D(0)) / len(values)


def evaluate(config: StrategyConfig, candles, position=None) -> Signal:
    if len(candles) < config.history:
        return Signal("WAIT", "INSUFFICIENT_HISTORY")
    candles = candles[-config.history :]
    if any(not c.final or c.source != "rest" for c in candles):
        return Signal("WAIT", "NONFINAL_CANDLE")
    if any(
        b.window_start - a.window_start != STEP[config.timeframe]
        for a, b in zip(candles, candles[1:], strict=False)
    ):
        return Signal("WAIT", "CANDLE_GAP")
    try:
        closes = [D(c.payload["close"]) for c in candles]
        highs = [D(c.payload["high"]) for c in candles]
        lows = [D(c.payload["low"]) for c in candles]
        volumes = [D(c.payload["vol"]) for c in candles]
        if any(x <= 0 for x in closes + highs + lows) or any(x < 0 for x in volumes):
            return Signal("WAIT", "INVALID_CANDLE")
    except (KeyError, ValueError, ArithmeticError):
        return Signal("WAIT", "INVALID_CANDLE")
    fast = _mean(closes[-config.fast_period :])
    fast_previous = _mean(closes[-config.fast_period - 1 : -1])
    slow = _mean(closes[-config.slow_period :])
    slow_previous = _mean(closes[-config.slow_period - 1 : -1])
    if position:
        side = position.direction
        if (side == "LONG" and closes[-1] < slow) or (side == "SHORT" and closes[-1] > slow):
            return Signal("CLOSE", "TREND_INVALIDATED", side)
        return Signal("WAIT", "POSITION_HELD")
    if config.strategy == "trend_following":
        if fast_previous <= slow_previous and fast > slow:
            return Signal("OPEN", "FAST_CROSSED_ABOVE_SLOW", "LONG")
        if fast_previous >= slow_previous and fast < slow:
            return Signal("OPEN", "FAST_CROSSED_BELOW_SLOW", "SHORT")
    elif config.strategy == "pullback":
        if slow > slow_previous and closes[-2] < fast_previous and closes[-1] > fast:
            return Signal("OPEN", "PULLBACK_RECOVERED", "LONG")
        if slow < slow_previous and closes[-2] > fast_previous and closes[-1] < fast:
            return Signal("OPEN", "PULLBACK_RECOVERED", "SHORT")
    else:
        threshold = D(1) + config.breakout_buffer
        prior_volume = _mean(volumes[-config.breakout_period - 1 : -1])
        if volumes[-1] < prior_volume * config.min_volume_ratio:
            return Signal("WAIT", "VOLUME_FILTER")
        if closes[-1] > max(highs[-config.breakout_period - 1 : -1]) * threshold:
            return Signal("OPEN", "RANGE_BROKEN", "LONG")
        if closes[-1] < min(lows[-config.breakout_period - 1 : -1]) / threshold:
            return Signal("OPEN", "RANGE_BROKEN", "SHORT")
    return Signal("WAIT", "NO_SIGNAL")
