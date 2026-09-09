from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scenario_router.calendar import ExchangeSession, TradingSessionCalendar
from scenario_router.indicators import (
    QuadStochasticPatternDetector,
    QuadStochasticDetector,
    MacdThreeWaveDetector,
    ThreeWaveHistogramDetector,
)
from scenario_router.models import DailyBar, IntradayBar


NY = ZoneInfo("America/New_York")
PRICE_BASIS = "split-adjusted-v1"


def quad_stochastic_calendar() -> TradingSessionCalendar:
    return TradingSessionCalendar([
        ExchangeSession(
            day,
            datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
            datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY),
        )
        for day in (date(2025, 7, 1), date(2025, 7, 2))
    ])


def bar_at(day: date, hour: int, minute: int, *, open_: float = 100, high: float = 101,
           low: float = 99, close: float = 100, volume: float = 1000,
           price_basis_id: str = PRICE_BASIS) -> IntradayBar:
    return IntradayBar(
        ticker="TEST",
        security_id="SID-TEST",
        price_basis_id=price_basis_id,
        start=datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY),
        open=open_, high=high, low=low, close=close, volume=volume,
    )


class ThreeWaveTests(unittest.TestCase):
    def test_three_negative_waves_fire_once_on_first_contraction(self) -> None:
        detector = ThreeWaveHistogramDetector("TEST", "SID-TEST", PRICE_BASIS)
        observations = [
            (-3.0, 101.0), (-4.0, 99.0), (-3.0, 100.0),
            (1.0, 103.0),
            (-2.5, 98.0), (-3.0, 97.0), (-2.6, 98.0),
            (1.0, 102.0),
            (-1.8, 96.0), (-2.2, 95.0), (-1.7, 96.0), (-1.5, 94.0),
        ]
        found = []
        start = date(2025, 1, 2)
        for index, (hist, low) in enumerate(observations):
            session = start + timedelta(days=index)
            result = detector.update(session, high=low + 2, low=low, histogram=hist)
            if result:
                found.append(result)
        self.assertEqual(1, len(found))
        self.assertEqual(95.0, found[0].third_wave_low)
        self.assertEqual(start + timedelta(days=8), found[0].third_wave_start_session)

    def test_failed_first_contraction_is_not_backfilled(self) -> None:
        detector = ThreeWaveHistogramDetector("TEST", "SID-TEST", PRICE_BASIS)
        observations = [
            (-3.0, 101.0), (-4.0, 99.0), (-3.0, 100.0), (1.0, 103.0),
            (-2.5, 98.0), (-3.0, 97.0), (-2.6, 98.0), (1.0, 102.0),
            # First contraction happens before price makes a lower third-wave low.
            (-1.8, 99.0), (-2.2, 98.0), (-1.7, 98.0), (-1.5, 95.0),
        ]
        results = [
            detector.update(date(2025, 2, 1) + timedelta(days=i), low + 2, low, hist)
            for i, (hist, low) in enumerate(observations)
        ]
        self.assertFalse(any(results))

    def test_macd_state_is_bound_to_stable_id_not_ticker(self) -> None:
        detector = MacdThreeWaveDetector("OLD", "SID-STABLE")
        detector.update(DailyBar("OLD", "SID-STABLE", PRICE_BASIS, date(2025, 1, 2), 100, 101, 99, 100, 1000))
        # A point-in-time ticker change for the same security keeps warm-up state.
        detector.update(DailyBar("NEW", "SID-STABLE", PRICE_BASIS, date(2025, 1, 3), 100, 101, 99, 100, 1000))
        self.assertEqual(2, detector.fast.count)

    def test_macd_state_resets_when_the_corporate_action_basis_changes(self) -> None:
        detector = MacdThreeWaveDetector("TEST", "SID-STABLE")
        detector.update(DailyBar(
            "TEST", "SID-STABLE", "pre-split", date(2025, 1, 2), 100, 101, 99, 100, 1000,
        ))
        detector.update(DailyBar(
            "TEST", "SID-STABLE", "post-split", date(2025, 1, 3), 50, 51, 49, 50, 2000,
        ))
        self.assertEqual(1, detector.fast.count)
        self.assertEqual("post-split", detector.price_basis_id)


class QuadStochasticTests(unittest.TestCase):
    def test_frozen_quad_pattern_and_next_bar_boundary(self) -> None:
        day = date(2025, 7, 1)
        detector = QuadStochasticPatternDetector()
        self.assertIsNone(detector.update(bar_at(day, 9, 35, open_=101, close=100, low=99.8),
                                          {"d9": 10, "d14": 10, "d40": 10, "d60": 10}))
        self.assertIsNone(detector.update(bar_at(day, 9, 40, open_=100.5, close=100.2, low=100),
                                          {"d9": 21, "d14": 12, "d40": 11, "d60": 10.5}))
        self.assertIsNone(detector.update(bar_at(day, 9, 45, open_=100, close=99.8, low=99.5),
                                          {"d9": 15, "d14": 10.5, "d40": 10, "d60": 9.5}))
        trigger = detector.update(bar_at(day, 9, 50, open_=100, close=100.2, low=99.4),
                                  {"d9": 18, "d14": 13, "d40": 12, "d60": 11})
        self.assertIsNotNone(trigger)
        assert trigger is not None
        self.assertEqual(datetime(2025, 7, 1, 9, 55, tzinfo=NY), trigger.detected_at)
        self.assertEqual(datetime(2025, 7, 1, 9, 55, tzinfo=NY), trigger.execute_at)
        self.assertEqual(99.4, trigger.second_test_low)

    def test_pattern_state_resets_each_session(self) -> None:
        detector = QuadStochasticPatternDetector()
        first = date(2025, 7, 1)
        detector.update(bar_at(first, 9, 35), {"d9": 10, "d14": 10, "d40": 10, "d60": 10})
        second = date(2025, 7, 2)
        result = detector.update(bar_at(second, 9, 35), {"d9": 30, "d14": 30, "d40": 30, "d60": 30})
        self.assertIsNone(result)
        self.assertIsNone(detector.state)

    def test_indicator_windows_continue_across_sessions(self) -> None:
        detector = QuadStochasticDetector(quad_stochastic_calendar())
        day = date(2025, 7, 1)
        start = datetime(2025, 7, 1, 9, 30, tzinfo=NY)
        for i in range(78):
            timestamp = start + timedelta(minutes=5 * i)
            detector.update(IntradayBar(
                "TEST", "SID-TEST", PRICE_BASIS, timestamp,
                100, 101, 99, 100 + i / 1000, 1000,
            ))
        next_day = bar_at(day + timedelta(days=1), 9, 30)
        detector.update(next_day)
        self.assertEqual(60, len(detector.oscillators["d60"].highs))

    def test_quad_stochastic_windows_reset_when_the_price_basis_changes(self) -> None:
        detector = QuadStochasticDetector(quad_stochastic_calendar())
        day = date(2025, 7, 1)
        start = datetime(2025, 7, 1, 9, 30, tzinfo=NY)
        for index in range(60):
            timestamp = start + timedelta(minutes=5 * index)
            detector.update(IntradayBar(
                "TEST", "SID-TEST", "pre-split", timestamp, 100, 101, 99, 100, 1000,
            ))
        changed = bar_at(
            day, 14, 30, open_=50, high=51, low=49, close=50,
            price_basis_id="post-split",
        )
        self.assertIsNone(detector.update(changed))
        self.assertEqual([51], list(detector.oscillators["d60"].highs))

    def test_expiry_bar_can_arm_a_new_pattern_on_the_same_bar(self) -> None:
        detector = QuadStochasticPatternDetector(max_bars_after_arm=12)
        day = date(2025, 7, 1)
        values = {"d9": 10, "d14": 10, "d40": 10, "d60": 10}
        for index in range(14):
            total_minutes = 35 + 5 * index
            detector.update(bar_at(day, 9 + total_minutes // 60, total_minutes % 60), values)
        self.assertIsNotNone(detector.state)
        self.assertEqual(13, detector.state.arm_index)
        detector.update(bar_at(day, 10, 45), values)
        self.assertIsNotNone(detector.state)
        self.assertEqual(13, detector.state.arm_index)


if __name__ == "__main__":
    unittest.main()
