"""Frozen MACD and four-line stochastic indicator definitions.

The module exposes causal state machines that backtest engines feed one
completed bar at a time.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Deque, Iterable

from .calendar import TradingSessionCalendar
from .configuration import (
    QUAD_STOCH_BOUNCE,
    QUAD_STOCH_LINES,
    QUAD_STOCH_MAX_BARS_FROM_ARM,
    QUAD_STOCH_MIN_DIVERGENCE,
    QUAD_STOCH_OVERSOLD,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
)
from .models import DailyBar, IntradayBar, QuadStochasticTrigger, MacdSetup, require_finite_number


class Ema:
    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None
        self.count = 0

    def update(self, value: float) -> float | None:
        require_finite_number(value, "EMA input")
        self.value = value if self.value is None else self.alpha * value + (1.0 - self.alpha) * self.value
        self.count += 1
        return self.value if self.count >= self.period else None


@dataclass
class _Wave:
    sign: int
    start_session: date
    end_session: date
    hist_extreme: float
    price_extreme: float
    length: int = 1
    contraction_seen: bool = False
    fired: bool = False


class ThreeWaveHistogramDetector:
    """Detect a long three-negative-wave price/histogram divergence."""

    def __init__(
        self, ticker: str, security_id: str, price_basis_id: str, not_before: date | None = None
    ) -> None:
        self.ticker = ticker
        self.security_id = security_id
        self.price_basis_id = price_basis_id
        self.not_before = not_before
        self.segments: list[_Wave] = []
        self.current: _Wave | None = None
        self.previous_histogram: float | None = None

    def update(self, session: date, high: float, low: float, histogram: float) -> MacdSetup | None:
        if not math.isfinite(histogram):
            return None
        sign = -1 if histogram < 0 else 1
        if self.current is None or self.current.sign != sign:
            if self.current is not None:
                self.segments.append(self.current)
            self.current = _Wave(
                sign=sign,
                start_session=session,
                end_session=session,
                hist_extreme=histogram,
                price_extreme=low if sign == -1 else high,
            )
        else:
            self.current.end_session = session
            self.current.length += 1
            if sign == -1:
                self.current.hist_extreme = min(self.current.hist_extreme, histogram)
                self.current.price_extreme = min(self.current.price_extreme, low)
            else:
                self.current.hist_extreme = max(self.current.hist_extreme, histogram)
                self.current.price_extreme = max(self.current.price_extreme, high)

        result: MacdSetup | None = None
        current = self.current
        same_segment_previous = self.previous_histogram is not None and current.length >= 2
        contracting = same_segment_previous and (
            histogram > self.previous_histogram if sign == -1 else histogram < self.previous_histogram
        )
        first_contraction = bool(contracting and not current.contraction_seen)
        if contracting:
            current.contraction_seen = True

        if sign == -1 and first_contraction and not current.fired:
            prior_negative = [segment for segment in self.segments if segment.sign == -1]
            if len(prior_negative) >= 2:
                waves = prior_negative[-2:] + [current]
                prices = [wave.price_extreme for wave in waves]
                histograms = [wave.hist_extreme for wave in waves]
                price_ok = prices[0] > prices[1] > prices[2]
                histogram_ok = histograms[0] < histograms[1] < histograms[2]
                date_ok = self.not_before is None or session >= self.not_before
                if price_ok and histogram_ok and date_ok:
                    result = MacdSetup(
                        ticker=self.ticker,
                        security_id=self.security_id,
                        price_basis_id=self.price_basis_id,
                        signal_session=session,
                        third_wave_start_session=current.start_session,
                        third_wave_low=current.price_extreme,
                        signal_histogram=histogram,
                    )
                    current.fired = True

        self.previous_histogram = histogram
        return result


class MacdThreeWaveDetector:
    """MACD(12,26,9) wrapper around :class:`ThreeWaveHistogramDetector`."""

    def __init__(self, ticker: str, security_id: str, not_before: date | None = None) -> None:
        self.ticker = ticker
        self.security_id = security_id
        self.not_before = not_before
        self.price_basis_id: str | None = None
        self.fast = Ema(MACD_FAST)
        self.slow = Ema(MACD_SLOW)
        self.signal = Ema(MACD_SIGNAL)
        self.wave: ThreeWaveHistogramDetector | None = None
        self.last_session: date | None = None

    def _reset_for_price_basis(self, ticker: str, price_basis_id: str) -> None:
        self.price_basis_id = price_basis_id
        self.fast = Ema(MACD_FAST)
        self.slow = Ema(MACD_SLOW)
        self.signal = Ema(MACD_SIGNAL)
        self.wave = ThreeWaveHistogramDetector(
            ticker, self.security_id, price_basis_id, not_before=self.not_before
        )

    def update(self, bar: DailyBar) -> MacdSetup | None:
        if bar.security_id != self.security_id:
            raise ValueError("MACD detector is bound to one stable security ID")
        if self.last_session is not None and bar.session <= self.last_session:
            raise ValueError("daily bars must be strictly increasing and unique")
        self.last_session = bar.session
        if self.price_basis_id != bar.price_basis_id:
            self._reset_for_price_basis(bar.ticker, bar.price_basis_id)
        assert self.wave is not None
        self.wave.ticker = bar.ticker
        fast = self.fast.update(bar.close)
        slow = self.slow.update(bar.close)
        if fast is None or slow is None:
            return None
        macd = fast - slow
        signal = self.signal.update(macd)
        if signal is None:
            return None
        return self.wave.update(bar.session, bar.high, bar.low, macd - signal)


class SmoothedStochastic:
    def __init__(self, lookback: int, smoothing: int) -> None:
        self.lookback = lookback
        self.smoothing = smoothing
        self.highs: Deque[float] = deque(maxlen=lookback)
        self.lows: Deque[float] = deque(maxlen=lookback)
        self.raw_values: Deque[float] = deque(maxlen=smoothing)

    def update(self, bar: IntradayBar) -> float | None:
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        if len(self.highs) < self.lookback:
            return None
        high = max(self.highs)
        low = min(self.lows)
        if high == low:
            self.raw_values.clear()
            return None
        raw = 100.0 * (bar.close - low) / (high - low)
        self.raw_values.append(raw)
        if len(self.raw_values) < self.smoothing:
            return None
        return sum(self.raw_values) / self.smoothing


@dataclass
class _QuadStochasticState:
    arm_index: int
    stage: str
    first_low: float
    first_d9: float
    first_d14: float
    second_low: float = math.inf


class QuadStochasticPatternDetector:
    """Pattern state machine fed with already-computed stochastic D values."""

    def __init__(self, max_bars_after_arm: int = QUAD_STOCH_MAX_BARS_FROM_ARM) -> None:
        self.max_bars_after_arm = max_bars_after_arm
        self.session: date | None = None
        self.index = -1
        self.state: _QuadStochasticState | None = None
        self.previous: dict[str, float] | None = None

    def update(self, bar: IntradayBar, values: dict[str, float]) -> QuadStochasticTrigger | None:
        if self.session != bar.session:
            self.session = bar.session
            self.index = -1
            self.state = None
            self.previous = None
        self.index += 1

        required = ("d9", "d14", "d40", "d60")
        if any(key not in values or not math.isfinite(values[key]) for key in required):
            self.previous = None
            return None
        if bar.exchange_start.time() < time(9, 35) or bar.exchange_start.time() > time(15, 50):
            self.previous = dict(values)
            return None

        armed_now = all(values[key] < QUAD_STOCH_OVERSOLD for key in required)
        if self.state is not None and self.index - self.state.arm_index > self.max_bars_after_arm:
            # The frozen state machine reprocesses the expiry bar without
            # advancing its index, so this bar may arm a fresh pattern.
            self.state = None
        if self.state is None:
            if armed_now:
                self.state = _QuadStochasticState(
                    arm_index=self.index,
                    stage="armed",
                    first_low=min(bar.open, bar.close),
                    first_d9=values["d9"],
                    first_d14=values["d14"],
                )
            self.previous = dict(values)
            return None

        if self.state.stage == "armed":
            body_low = min(bar.open, bar.close)
            if body_low < self.state.first_low:
                self.state.first_low = body_low
                self.state.first_d9 = values["d9"]
                self.state.first_d14 = values["d14"]
            if values["d9"] > QUAD_STOCH_BOUNCE:
                self.state.stage = "bounced"
            self.previous = dict(values)
            return None

        self.state.second_low = min(self.state.second_low, bar.low)
        price_test = self.state.second_low <= self.state.first_low
        divergence = (
            values["d9"] >= self.state.first_d9 + QUAD_STOCH_MIN_DIVERGENCE
            or values["d14"] >= self.state.first_d14 + QUAD_STOCH_MIN_DIVERGENCE
        )
        all_turning = self.previous is not None and all(values[key] > self.previous[key] for key in required)
        trigger = price_test and divergence and all_turning and values["d9"] < 80
        result = None
        if trigger:
            result = QuadStochasticTrigger(
                ticker=bar.ticker,
                detected_at=bar.end,
                execute_at=bar.end,
                second_test_low=self.state.second_low,
                d9=values["d9"],
                d14=values["d14"],
                d40=values["d40"],
                d60=values["d60"],
            )
            self.state = None
        self.previous = dict(values)
        return result


class QuadStochasticDetector:
    """Four stochastic D lines: (9,3), (14,3), (40,4), and (60,10)."""

    def __init__(self, calendar: TradingSessionCalendar) -> None:
        self.calendar = calendar
        self._reset_indicator_state()
        self.security_id: str | None = None
        self.price_basis_id: str | None = None
        self.last_start = None
        self.last_session: date | None = None

    def _reset_indicator_state(self) -> None:
        names = ("d9", "d14", "d40", "d60")
        self.oscillators = {
            name: SmoothedStochastic(lookback, smoothing)
            for name, (lookback, smoothing) in zip(names, QUAD_STOCH_LINES, strict=True)
        }
        self.pattern = QuadStochasticPatternDetector()

    def update(self, bar: IntradayBar) -> QuadStochasticTrigger | None:
        schedule = self.calendar.get(bar.session)
        if bar.start < schedule.market_open or bar.end > schedule.market_close:
            raise ValueError("QuadStochastic bar is outside the exact exchange session")
        if self.security_id is None:
            self.security_id = bar.security_id
        elif bar.security_id != self.security_id:
            raise ValueError("QuadStochastic detector is bound to one stable security ID")
        if self.last_start is None:
            if bar.start != schedule.market_open:
                raise ValueError("QuadStochastic history must begin at an exchange-session open")
        elif bar.session == self.last_session:
            if bar.start != self.last_start + timedelta(minutes=5):
                raise ValueError("QuadStochastic history has a missing or duplicate five-minute bar")
        else:
            assert self.last_session is not None
            prior_schedule = self.calendar.get(self.last_session)
            if self.last_start + timedelta(minutes=5) != prior_schedule.market_close:
                raise ValueError("QuadStochastic history ended before the prior exchange session close")
            if not self.calendar.is_next_session(self.last_session, bar.session):
                raise ValueError("QuadStochastic history skipped an exchange session")
            if bar.start != schedule.market_open:
                raise ValueError("QuadStochastic history did not resume at the next session open")
        self.last_start = bar.start
        self.last_session = bar.session
        if self.price_basis_id is None:
            self.price_basis_id = bar.price_basis_id
        elif bar.price_basis_id != self.price_basis_id:
            self._reset_indicator_state()
            self.price_basis_id = bar.price_basis_id
        values = {name: oscillator.update(bar) for name, oscillator in self.oscillators.items()}
        if any(value is None for value in values.values()):
            # Still pass an empty set so a new session resets the pattern state.
            self.pattern.update(bar, {})
            return None
        return self.pattern.update(bar, {name: float(value) for name, value in values.items()})


def warm_quad_stochastic(detector: QuadStochasticDetector, bars: Iterable[IntradayBar]) -> None:
    for bar in bars:
        detector.update(bar)
