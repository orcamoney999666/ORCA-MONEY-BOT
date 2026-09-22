#!/usr/bin/env python3
"""Validated market-data Oracle for ORCA-MONEY-BOT.

The Oracle does not make trading decisions. It only accepts exchange candles when
schema, ordering, OHLC relationships, freshness, and continuity are valid.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Sequence

from binance_trading_bot import Candle


class OracleError(RuntimeError):
    """Raised when market data cannot be trusted for a decision."""


@dataclass(frozen=True)
class OracleConfig:
    max_age_seconds: int = 300
    expected_interval_seconds: int = 3600
    max_gap_intervals: int = 2


class MarketOracle:
    def __init__(self, fetcher: Callable[[str, str, int], list[Candle]], config: OracleConfig | None = None):
        self.fetcher = fetcher
        self.config = config or OracleConfig()

    def validate(self, candles: Sequence[Candle], now_ms: int | None = None) -> list[Candle]:
        if not candles:
            raise OracleError("oracle returned no candles")
        if len(candles) < 2:
            raise OracleError("oracle requires at least two candles")
        previous = None
        for candle in candles:
            values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
            if any(value != value for value in values):
                raise OracleError("oracle rejected NaN candle")
            if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0:
                raise OracleError("oracle rejected non-positive OHLC or negative volume")
            if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
                raise OracleError("oracle rejected inconsistent OHLC candle")
            if previous is not None:
                delta = candle.timestamp - previous.timestamp
                if delta <= 0:
                    raise OracleError("oracle rejected non-monotonic timestamps")
                if delta > self.config.expected_interval_seconds * 1000 * self.config.max_gap_intervals:
                    raise OracleError("oracle rejected a data gap")
            previous = candle
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        age = now_ms - candles[-1].timestamp
        if age < -self.config.expected_interval_seconds * 1000 or age > self.config.max_age_seconds * 1000:
            raise OracleError(f"oracle rejected stale or future data: age_ms={age}")
        return list(candles)

    def candles(self, symbol: str, interval: str = "1h", limit: int = 300, now_ms: int | None = None) -> list[Candle]:
        return self.validate(self.fetcher(symbol, interval, limit), now_ms=now_ms)
