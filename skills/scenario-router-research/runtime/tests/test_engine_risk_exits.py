from __future__ import annotations

import random
import json
import copy
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scenario_router.calendar import ExchangeSession, TradingSessionCalendar
from scenario_router.configuration import assert_config_matches_implementation, load_frozen_config
from scenario_router.engine import (
    EventSession,
    ReversalSession,
    SignalRouter,
    macd_open_signal,
)
from scenario_router.events import ArticleLedger, EventQualification, FeedCoverage
from scenario_router.exits import EventExitPolicy, ReversalExitPolicy
from scenario_router.models import (
    DailyBar,
    EntrySignal,
    IntradayBar,
    QuadStochasticTrigger,
    MacdSetup,
    OpenRisk,
    to_jsonable,
)
from scenario_router.risk import EntryQuote, RiskManager


NY = ZoneInfo("America/New_York")
DAY = date(2025, 7, 1)
PRICE_BASIS = "split-adjusted-v1"


def exchange_session(day: date, close_hour: int = 16) -> ExchangeSession:
    return ExchangeSession(
        day,
        datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
        datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=NY),
    )


CALENDAR = TradingSessionCalendar([
    exchange_session(date(2025, 6, 23)),
    exchange_session(date(2025, 6, 24)),
    exchange_session(date(2025, 6, 30)),
    exchange_session(DAY),
    exchange_session(date(2025, 7, 2)),
])


def bar(
    hour: int,
    minute: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    ticker: str = "TEST",
    security_id: str = "SID-TEST",
    price_basis_id: str = PRICE_BASIS,
) -> IntradayBar:
    return IntradayBar(
        ticker, security_id, price_basis_id, datetime(2025, 7, 1, hour, minute, tzinfo=NY),
        open_, high, low, close, volume,
    )


def signal(branch: str, variant: str, ticker: str = "TEST") -> EntrySignal:
    stamp = datetime(2025, 7, 1, 10, 0, tzinfo=NY)
    return EntrySignal(
        signal_id=f"{branch}:{variant}:{ticker}", ticker=ticker, security_id=f"SID-{ticker}",
        branch=branch, variant=variant, detected_at=stamp, execute_at=stamp,
        stop_price=95.0, context_id=f"ctx-{ticker}",
        metadata={
            "price_basis_id": PRICE_BASIS,
            "experiment_id": f"{branch}:{variant}",
            **({
                "event_mode": "strict_primary", "event_status": "QUALIFIED_EVENT",
                "event_record_ids": ("event-1",),
            } if branch == "event" else {}),
        },
    )


class EventSessionTests(unittest.TestCase):
    def make_session(self, mode: str = "strict_primary") -> EventSession:
        qualification = EventQualification(
            security_id="SID-TEST",
            window_start=CALENDAR.previous(DAY).market_close,
            cutoff=CALENDAR.get(DAY).market_open - timedelta(microseconds=1),
            mode=mode, status="QUALIFIED_EVENT",
            eligible_variants=("E1", "E2A"), event_record_ids=("event-1",),
            reason_codes=("ELIGIBLE_E1", "ELIGIBLE_E2A"),
        )
        return EventSession("TEST", "SID-TEST", DAY, CALENDAR,
                            prior_close=100.0, average_full_day_volume_20=400.0,
                            prior_close_price_basis_id=PRICE_BASIS,
                            session_price_basis_id=PRICE_BASIS,
                            event_qualification=qualification)

    def test_exact_gap_volume_and_strict_breakout(self) -> None:
        session = self.make_session()
        opening = [
            bar(9, 30, open_=110, high=111, low=109, close=110, volume=100),
            bar(9, 35, open_=110, high=112, low=109.5, close=111, volume=100),
            bar(9, 40, open_=111, high=111.5, low=109, close=110, volume=100),
            bar(9, 45, open_=110, high=111, low=108.5, close=110, volume=100),
        ]
        for item in opening:
            self.assertEqual((), session.on_bar(item))
        # Equality to OR high is not a breakout.
        self.assertEqual((), session.on_bar(bar(9, 50, open_=111, high=112, low=110, close=111.5, volume=10)))
        signals = session.on_bar(bar(9, 55, open_=111.5, high=112.01, low=111, close=112, volume=10))
        self.assertEqual(("E0", "E1", "E2A"), tuple(item.variant for item in signals))
        self.assertTrue(all(item.execute_at == datetime(2025, 7, 1, 10, 0, tzinfo=NY) for item in signals))
        self.assertTrue(all(item.stop_price == 108.5 for item in signals))

    def test_missing_opening_bar_fails_closed(self) -> None:
        session = self.make_session()
        for minute in (30, 35, 45):
            session.on_bar(bar(9, minute, open_=110, high=112, low=109, close=111, volume=150))
        self.assertEqual((), session.on_bar(bar(9, 50, open_=111, high=113, low=110, close=112, volume=10)))
        self.assertEqual("ABSTAIN_INTRADAY_DATA_GAP_OR_OUTSIDE_SESSION", session.reason)

    def test_orb_after_window_expires(self) -> None:
        session = self.make_session()
        for minute in (30, 35, 40, 45):
            session.on_bar(bar(9, minute, open_=110, high=112, low=109, close=111, volume=100))
        for hour, minute in (
            [(9, value) for value in (50, 55)]
            + [(10, value) for value in range(0, 60, 5)]
        ):
            self.assertEqual(
                (), session.on_bar(bar(hour, minute, open_=111, high=112, low=110, close=111, volume=10))
            )
        self.assertEqual((), session.on_bar(bar(11, 0, open_=111, high=113, low=110, close=112, volume=10)))
        self.assertEqual("EXPIRED_NO_ORB", session.reason)

    def test_unverified_split_basis_cannot_create_a_gap(self) -> None:
        qualification = EventQualification(
            "SID-TEST", CALENDAR.previous(DAY).market_close,
            CALENDAR.get(DAY).market_open - timedelta(microseconds=1), "strict_primary",
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), (), ("E1_ONLY_TEST",),
        )
        session = EventSession(
            "TEST", "SID-TEST", DAY, CALENDAR,
            prior_close=50.0, average_full_day_volume_20=400.0,
            prior_close_price_basis_id="pre-split",
            session_price_basis_id="post-split", event_qualification=qualification,
        )
        for minute in (30, 35, 40, 45):
            session.on_bar(bar(
                9, minute, open_=100, high=102, low=99, close=101, volume=100,
                price_basis_id="post-split",
            ))
        self.assertEqual((), session.on_bar(bar(
            9, 50, open_=101, high=103, low=100, close=102, volume=10,
            price_basis_id="post-split",
        )))
        self.assertEqual("REJECTED_CORPORATE_ACTION_BASIS", session.reason)

    def test_out_of_order_event_bars_fail_closed(self) -> None:
        session = self.make_session()
        session.on_bar(bar(9, 50, open_=110, high=112, low=109, close=111, volume=100))
        self.assertEqual(
            (), session.on_bar(bar(9, 30, open_=110, high=112, low=109, close=111, volume=100))
        )
        self.assertEqual("ABSTAIN_INTRADAY_DATA_GAP_OR_OUTSIDE_SESSION", session.reason)

    def test_event_session_rejects_a_ticker_identity_change(self) -> None:
        session = self.make_session()
        self.assertEqual(
            (), session.on_bar(bar(
                9, 30, open_=110, high=112, low=109, close=111, volume=100, ticker="OLD",
            ))
        )
        self.assertEqual("ABSTAIN_TICKER_IDENTITY_MISMATCH", session.reason)

    def test_utc_bar_is_interpreted_in_exchange_time(self) -> None:
        session = self.make_session()
        for item in (
            bar(9, 30, open_=110, high=111, low=109, close=110, volume=100),
            bar(9, 35, open_=110, high=112, low=109.5, close=111, volume=100),
            bar(9, 40, open_=111, high=111.5, low=109, close=110, volume=100),
            bar(9, 45, open_=110, high=111, low=108.5, close=110, volume=100),
        ):
            utc_bar = IntradayBar(
                item.ticker, item.security_id, item.price_basis_id,
                item.start.astimezone(ZoneInfo("UTC")),
                item.open, item.high, item.low, item.close, item.volume,
            )
            session.on_bar(utc_bar)
        trigger_source = bar(9, 50, open_=111, high=112.1, low=110, close=112, volume=10)
        signals = session.on_bar(IntradayBar(
            trigger_source.ticker, trigger_source.security_id, trigger_source.price_basis_id,
            trigger_source.start.astimezone(ZoneInfo("UTC")),
            trigger_source.open, trigger_source.high, trigger_source.low,
            trigger_source.close, trigger_source.volume,
        ))
        self.assertEqual(datetime(2025, 7, 1, 9, 55, tzinfo=NY), signals[0].execute_at.astimezone(NY))

    def test_strict_and_agent_event_signals_have_distinct_identity(self) -> None:
        strict = self.make_session("strict_primary")
        secondary = self.make_session("agent_assisted_secondary")
        bars = [
            bar(9, 30, open_=110, high=111, low=109, close=110, volume=100),
            bar(9, 35, open_=110, high=112, low=109.5, close=111, volume=100),
            bar(9, 40, open_=111, high=111.5, low=109, close=110, volume=100),
            bar(9, 45, open_=110, high=111, low=108.5, close=110, volume=100),
            bar(9, 50, open_=111, high=112.1, low=110, close=112, volume=10),
        ]
        strict_signals = ()
        secondary_signals = ()
        for item in bars:
            strict_signals = strict.on_bar(item) or strict_signals
            secondary_signals = secondary.on_bar(item) or secondary_signals
        self.assertNotEqual(strict_signals[2].signal_id, secondary_signals[2].signal_id)
        self.assertNotEqual(
            strict_signals[2].metadata["experiment_id"],
            secondary_signals[2].metadata["experiment_id"],
        )


class ReversalSessionTests(unittest.TestCase):
    @staticmethod
    def empty_article_ledger() -> ArticleLedger:
        from scenario_router.events import CoverageInterval
        start = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
        return ArticleLedger([], FeedCoverage(
            "news_feed", [CoverageInterval("SID-TEST", start, end, "complete")],
            captured_at=end, raw_manifest_sha256="sha256:" + "a" * 64,
            pagination_complete=True,
        ))

    def test_m2_uses_previous_close_information_and_open_fill(self) -> None:
        setup = MacdSetup("TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5)
        result = macd_open_signal(
            setup, DAY, CALENDAR, 100, "TEST", PRICE_BASIS, "M2", self.empty_article_ledger(),
        )
        self.assertIsNotNone(result)
        self.assertEqual(CALENDAR.get(DAY).market_open - timedelta(microseconds=1), result.detected_at)
        self.assertEqual(CALENDAR.get(DAY).market_open, result.execute_at)

    def test_macd_low_touch_invalidates_before_quad_stochastic(self) -> None:
        class NeverQuadStochastic:
            def update(self, _: IntradayBar):
                return None

        setup = MacdSetup("TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5)
        engine = ReversalSession(
            setup, DAY, CALENDAR, PRICE_BASIS, NeverQuadStochastic(), None, "M1",
        )
        result = engine.on_bar(bar(9, 30, open_=100, high=101, low=95, close=99, volume=1000))
        self.assertIsNone(result)
        self.assertEqual("INVALIDATED_MACD_LOW", engine.reason)

    def test_quad_stochastic_only_controls_timing_not_stop(self) -> None:
        class OneQuadStochastic:
            def update(self, item: IntradayBar):
                return QuadStochasticTrigger(item.ticker, item.end, item.end, 98, 18, 13, 12, 11)

        setup = MacdSetup("TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5)
        engine = ReversalSession(
            setup, DAY, CALENDAR, PRICE_BASIS, OneQuadStochastic(), None, "M1",
        )
        result = engine.on_bar(bar(9, 30, open_=100, high=101, low=97, close=100, volume=1000))
        self.assertIsNotNone(result)
        self.assertEqual(95, result.stop_price)
        self.assertEqual(98, result.metadata["quad_stochastic_second_test_low"])

    def test_setup_is_rejected_outside_the_next_exchange_session(self) -> None:
        setup = MacdSetup("TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5)
        with self.assertRaises(ValueError):
            macd_open_signal(
                setup, date(2025, 7, 2), CALENDAR, 100, "TEST", PRICE_BASIS, "M0",
            )

    def test_reversal_rejects_an_overnight_price_basis_change(self) -> None:
        setup = MacdSetup(
            "TEST", "SID-TEST", "pre-split", date(2025, 6, 30), date(2025, 6, 24), 95, -0.5
        )
        with self.assertRaises(ValueError):
            macd_open_signal(setup, DAY, CALENDAR, 50, "TEST", "post-split", "M0")

    def test_half_day_last_bar_cannot_emit_a_next_bar_signal(self) -> None:
        half_day_calendar = TradingSessionCalendar([
            exchange_session(date(2025, 6, 23)),
            exchange_session(date(2025, 6, 24)),
            exchange_session(date(2025, 6, 30)),
            exchange_session(DAY, close_hour=13),
        ])

        class LastBarQuadStochastic:
            def update(self, item: IntradayBar):
                if item.start.time() != datetime.strptime("12:55", "%H:%M").time():
                    return None
                return QuadStochasticTrigger(item.ticker, item.end, item.end, 98, 18, 13, 12, 11)

        setup = MacdSetup(
            "TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5
        )
        engine = ReversalSession(
            setup, DAY, half_day_calendar, PRICE_BASIS, LastBarQuadStochastic(), None, "M1",
        )
        start = datetime(2025, 7, 1, 9, 30, tzinfo=NY)
        result = None
        for index in range(42):
            stamp = start + timedelta(minutes=5 * index)
            result = engine.on_bar(bar(
                stamp.hour, stamp.minute, open_=100, high=101, low=97, close=100, volume=1000,
            ))
        self.assertIsNone(result)
        self.assertEqual("ABSTAIN_NO_NEXT_REGULAR_BAR", engine.reason)

    def test_detector_cannot_be_shared_between_ablation_arms(self) -> None:
        class NeverQuadStochastic:
            def update(self, _: IntradayBar):
                return None

        setup = MacdSetup("TEST", "SID-TEST", PRICE_BASIS, date(2025, 6, 30), date(2025, 6, 24), 95, -0.5)
        detector = NeverQuadStochastic()
        ReversalSession(
            setup, DAY, CALENDAR, PRICE_BASIS, detector, None, "M1",
        )
        with self.assertRaises(ValueError):
            ReversalSession(
                setup, DAY, CALENDAR, PRICE_BASIS, detector, self.empty_article_ledger(), "M3",
            )


class RouterAndRiskTests(unittest.TestCase):
    @staticmethod
    def qualification(value: str, *, cutoff: datetime | None = None, mode: str = "strict_primary") -> EventQualification:
        if value == "event":
            status, variants, reasons = (
                "QUALIFIED_EVENT", ("E1", "E2A"), ("ELIGIBLE_E1", "ELIGIBLE_E2A")
            )
        elif value == "reversal":
            status, variants, reasons = "ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE", (), ("NO_PREMARKET_EVENT",)
        else:
            status, variants, reasons = "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", (), ("ABSTAIN",)
        return EventQualification(
            security_id="SID-TEST",
            window_start=CALENDAR.previous(DAY).market_close,
            cutoff=cutoff or CALENDAR.get(DAY).market_open - timedelta(microseconds=1),
            mode=mode, status=status, eligible_variants=variants,
            event_record_ids=("event-1",) if value == "event" else (), reason_codes=reasons,
        )

    def test_event_priority_is_order_independent(self) -> None:
        values = [signal("reversal", "M4"), signal("event", "E2A")]
        qualifications = {"SID-TEST": self.qualification("event")}
        expected = SignalRouter.select(
            values, event_variant="E2A", reversal_variant="M4",
            qualifications_by_security_id=qualifications, event_mode="strict_primary", calendar=CALENDAR,
        )
        random.Random(7).shuffle(values)
        actual = SignalRouter.select(
            values, event_variant="E2A", reversal_variant="M4",
            qualifications_by_security_id=qualifications, event_mode="strict_primary", calendar=CALENDAR,
        )
        self.assertEqual(expected, actual)
        self.assertEqual("event", actual[0].branch)

    def test_qualified_e2_requires_bound_event_evidence(self) -> None:
        with self.assertRaises(ValueError):
            EventQualification(
                security_id="SID-TEST", window_start=CALENDAR.previous(DAY).market_close,
                cutoff=CALENDAR.get(DAY).market_open - timedelta(microseconds=1),
                mode="strict_primary", status="QUALIFIED_EVENT",
                eligible_variants=("E1", "E2A"), event_record_ids=(),
                reason_codes=("ELIGIBLE_E1", "ELIGIBLE_E2A"),
            )

    def test_event_priority_applies_even_when_signal_times_differ(self) -> None:
        reversal = signal("reversal", "M4")
        event = signal("event", "E2A")
        event = EntrySignal(
            signal_id=event.signal_id, ticker=event.ticker, security_id=event.security_id,
            branch=event.branch, variant=event.variant,
            detected_at=event.detected_at + timedelta(minutes=5),
            execute_at=event.execute_at + timedelta(minutes=5), stop_price=event.stop_price,
            context_id=event.context_id, metadata=event.metadata,
        )
        selected = SignalRouter.select(
            [reversal, event], event_variant="E2A", reversal_variant="M4",
            qualifications_by_security_id={"SID-TEST": self.qualification("event")},
            event_mode="strict_primary", calendar=CALENDAR,
        )
        self.assertEqual("event", selected[0].branch)
        self.assertEqual("portfolio:strict_primary:E2A+M4", selected[0].metadata["experiment_id"])

    def test_router_cannot_use_a_later_signal_as_the_route(self) -> None:
        reversal = signal("reversal", "M4")
        event = signal("event", "E2A")
        selected = SignalRouter.select(
            [reversal, event], event_variant="E2A", reversal_variant="M4",
            qualifications_by_security_id={"SID-TEST": self.qualification("reversal")},
            event_mode="strict_primary", calendar=CALENDAR,
        )
        self.assertEqual("reversal", selected[0].branch)

    def test_future_computed_route_is_rejected(self) -> None:
        event = signal("event", "E2A")
        with self.assertRaises(ValueError):
            SignalRouter.select(
                [event], event_variant="E2A", reversal_variant="M4",
                qualifications_by_security_id={
                    "SID-TEST": self.qualification(
                        "event", cutoff=event.detected_at + timedelta(minutes=1)
                    )
                },
                event_mode="strict_primary", calendar=CALENDAR,
            )

    def test_preopen_route_may_be_frozen_after_a_prior_close_signal(self) -> None:
        market_open = CALENDAR.get(DAY).market_open
        candidate = EntrySignal(
            signal_id="reversal:M2:TEST", ticker="TEST", security_id="SID-TEST",
            branch="reversal", variant="M2",
            detected_at=CALENDAR.previous(DAY).market_close,
            execute_at=market_open, stop_price=95, context_id="ctx",
        )
        selected = SignalRouter.select(
            [candidate], event_variant="E2A", reversal_variant="M2",
            qualifications_by_security_id={"SID-TEST": self.qualification("reversal")},
            event_mode="strict_primary", calendar=CALENDAR,
        )
        self.assertEqual("reversal", selected[0].branch)
        self.assertEqual(
            "ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE",
            selected[0].metadata["information_route_reason"],
        )

    def test_final_router_rejects_ungated_reversal_ablation_arms(self) -> None:
        with self.assertRaises(ValueError):
            SignalRouter.select(
                [signal("reversal", "M0")], event_variant="E2A", reversal_variant="M0",
                qualifications_by_security_id={"SID-TEST": self.qualification("reversal")},
                event_mode="strict_primary", calendar=CALENDAR,
            )

    def test_router_does_not_mix_strict_and_agent_modes(self) -> None:
        with self.assertRaises(ValueError):
            SignalRouter.select(
                [signal("event", "E2A")], event_variant="E2A", reversal_variant="M4",
                qualifications_by_security_id={
                    "SID-TEST": self.qualification("event", mode="agent_assisted_secondary")
                },
                event_mode="strict_primary", calendar=CALENDAR,
            )

    def test_router_rejects_event_execution_outside_frozen_orb_window(self) -> None:
        event = signal("event", "E2A")
        for hour, minute in ((9, 50), (11, 5), (10, 1), (16, 0)):
            stamp = datetime(2025, 7, 1, hour, minute, tzinfo=NY)
            candidate = replace(event, detected_at=stamp, execute_at=stamp)
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                SignalRouter.select(
                    [candidate], event_variant="E2A", reversal_variant="M4",
                    qualifications_by_security_id={"SID-TEST": self.qualification("event")},
                    event_mode="strict_primary", calendar=CALENDAR,
                )

    def test_router_rejects_event_evidence_from_another_qualification(self) -> None:
        event = signal("event", "E2A")
        changed = replace(event, metadata={**dict(event.metadata), "event_record_ids": ("other",)})
        with self.assertRaises(ValueError):
            SignalRouter.select(
                [changed], event_variant="E2A", reversal_variant="M4",
                qualifications_by_security_id={"SID-TEST": self.qualification("event")},
                event_mode="strict_primary", calendar=CALENDAR,
            )

    def test_router_rejects_a_late_m2_or_delayed_quad_stochastic_execution(self) -> None:
        late_m2 = signal("reversal", "M2")
        quad_stochastic = signal("reversal", "M4")
        late_quad_stochastic = replace(quad_stochastic, execute_at=quad_stochastic.execute_at + timedelta(minutes=5))
        for candidate in (late_m2, late_quad_stochastic):
            with self.subTest(variant=candidate.variant), self.assertRaises(ValueError):
                SignalRouter.select(
                    [candidate], event_variant="E2A", reversal_variant=candidate.variant,
                    qualifications_by_security_id={"SID-TEST": self.qualification("reversal")},
                    event_mode="strict_primary", calendar=CALENDAR,
                )

    @staticmethod
    def quote(s: EntrySignal, **overrides) -> EntryQuote:
        values = {
            "ticker": s.ticker,
            "security_id": s.security_id,
            "price_basis_id": PRICE_BASIS,
            "timestamp": s.execute_at,
            "next_bar_open": 100.0,
            "bid_price": 99.95,
            "ask_price": 100.05,
            "prior_close": 100.0,
            "median_dollar_volume_20": 50_000_000.0,
            "security_classification": "primary_common_stock",
        }
        values.update(overrides)
        return EntryQuote(**values)

    def test_risk_sizing_and_point_in_time_nbbo_gate(self) -> None:
        s = signal("event", "E2A")
        quote = self.quote(s)
        manager = RiskManager()
        intents = manager.allocate([s], {s.signal_id: quote}, equity=100_000)
        # The displayed ask, not midpoint/trade open, drives fill and sizing.
        self.assertEqual(49, intents[0].quantity)
        self.assertAlmostEqual(247.45, intents[0].risk_dollars)
        self.assertEqual(100.05, intents[0].reference_entry)
        self.assertEqual("event:E2A", intents[0].metadata["experiment_id"])
        self.assertEqual((), manager.allocate([s], {}, equity=100_000))

    def test_quote_price_basis_must_match_the_signal(self) -> None:
        s = signal("event", "E2A")
        with self.assertRaises(ValueError):
            RiskManager().allocate(
                [s], {s.signal_id: self.quote(s, price_basis_id="different-basis")}, 100_000
            )

    def test_total_heat_and_position_cap(self) -> None:
        manager = RiskManager()
        signals = [signal("event", "E2A", ticker=f"T{i}") for i in range(5)]
        quotes = {
            item.signal_id: self.quote(
                item, bid_price=99.975, ask_price=100.025,
                median_dollar_volume_20=100_000_000 - i,
            )
            for i, item in enumerate(signals)
        }
        intents = manager.allocate(signals, quotes, 100_000)
        self.assertEqual(4, len(intents))
        self.assertLessEqual(sum(item.risk_dollars for item in intents), 1_000)
        self.assertEqual((), manager.allocate(
            [signals[0]], quotes, 100_000,
            open_risks=[OpenRisk(f"P{i}", f"SID-P{i}", 250, 25_000) for i in range(4)],
        ))
        self.assertEqual((), manager.allocate(
            [signals[0]], quotes, 100_000,
            open_risks=[OpenRisk("NEARFULL", "SID-NEARFULL", 0, 99_950)],
        ))

    def test_late_or_gap_through_stop_entry_is_rejected(self) -> None:
        s = signal("reversal", "M4")
        manager = RiskManager()
        late = self.quote(
            s, timestamp=s.execute_at + timedelta(minutes=5), bid_price=99.975, ask_price=100.025,
        )
        stopped = self.quote(s, next_bar_open=94.0, bid_price=95.95, ask_price=96.0)
        self.assertEqual((), manager.allocate([s], {s.signal_id: late}, 100_000))
        self.assertEqual((), manager.allocate([s], {s.signal_id: stopped}, 100_000))

    def test_allocation_rejects_cross_time_batches(self) -> None:
        first = signal("reversal", "M4", "A")
        second = signal("event", "E2A", "B")
        second = EntrySignal(
            signal_id=second.signal_id, ticker=second.ticker, security_id=second.security_id,
            branch=second.branch, variant=second.variant,
            detected_at=second.detected_at + timedelta(minutes=5),
            execute_at=second.execute_at + timedelta(minutes=5), stop_price=second.stop_price,
            context_id=second.context_id, metadata=second.metadata,
        )
        with self.assertRaises(ValueError):
            RiskManager().allocate(
                [first, second],
                {first.signal_id: self.quote(first), second.signal_id: self.quote(second)},
                100_000,
            )

    def test_universe_gate_is_executable(self) -> None:
        s = signal("event", "E2A")
        manager = RiskManager()
        for quote in (
            self.quote(s, prior_close=4.99),
            self.quote(s, median_dollar_volume_20=19_999_999),
            self.quote(s, security_classification="ETF"),
        ):
            self.assertEqual((), manager.allocate([s], {s.signal_id: quote}, 100_000))

    def test_nonfinite_market_and_risk_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            DailyBar("TEST", "SID-TEST", PRICE_BASIS, DAY, 100, float("nan"), 99, 100, 1_000)
        s = signal("event", "E2A")
        with self.assertRaises(ValueError):
            self.quote(s, ask_price=float("inf"))
        with self.assertRaises(ValueError):
            OpenRisk("TEST", "SID-TEST", float("nan"), 1_000)
        with self.assertRaises(ValueError):
            RiskManager().allocate([s], {s.signal_id: self.quote(s)}, float("inf"))

    def test_open_position_identity_uses_stable_security_id(self) -> None:
        s = signal("event", "E2A")
        manager = RiskManager()
        existing = OpenRisk("OLDTICKER", s.security_id, 250, 25_000)
        self.assertEqual(
            (), manager.allocate([s], {s.signal_id: self.quote(s)}, 100_000, open_risks=[existing])
        )

    def test_frozen_signal_has_an_explicit_json_export_path(self) -> None:
        s = EntrySignal(
            signal_id="event:E2A:TEST", ticker="TEST", security_id="SID-TEST",
            branch="event", variant="E2A",
            detected_at=datetime(2025, 7, 1, 9, 55, tzinfo=NY),
            execute_at=datetime(2025, 7, 1, 10, 0, tzinfo=NY),
            stop_price=95, context_id="ctx", metadata={"nested": {"values": [1, 2]}},
        )
        with self.assertRaises(TypeError):
            s.metadata["nested"]["values"] = (9,)
        self.assertIn("event:E2A:TEST", json.dumps(to_jsonable(s)))

    def test_every_frozen_config_field_is_part_of_the_contract(self) -> None:
        path = Path(__file__).resolve().parents[1] / "frozen_config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        assert_config_matches_implementation(config)
        for mutation in (
            ("timezone", "UTC"),
            ("execution.entry", "same_bar_close"),
            ("event.primary_event_mode", "agent_assisted_secondary"),
        ):
            changed = copy.deepcopy(config)
            if "." in mutation[0]:
                parent, key = mutation[0].split(".")
                changed[parent][key] = mutation[1]
            else:
                changed[mutation[0]] = mutation[1]
            with self.assertRaises(ValueError):
                assert_config_matches_implementation(changed)

        boolean_alias = copy.deepcopy(config)
        boolean_alias["reversal"]["macd"]["fast"] = True
        with self.assertRaises(ValueError):
            assert_config_matches_implementation(boolean_alias)

        frozen, _ = load_frozen_config(path)
        with self.assertRaises(TypeError):
            frozen["reversal"]["macd"]["fast"] = 99

    def test_exchange_calendar_cannot_be_mutated_after_construction(self) -> None:
        with self.assertRaises(TypeError):
            CALENDAR.by_date[DAY] = exchange_session(DAY, close_hour=13)


class ExitTests(unittest.TestCase):
    def test_reversal_stop_wins_same_bar(self) -> None:
        policy = ReversalExitPolicy(100, 95, PRICE_BASIS)
        result = policy.on_bar(
            100, 111, 94, 105, PRICE_BASIS, holding_session=1, session_close=False
        )
        self.assertEqual("same_bar_stop_first", result.reason)
        self.assertEqual(95, result.price)

    def test_event_day5_then_ema_next_open(self) -> None:
        policy = EventExitPolicy(100, 95, PRICE_BASIS)
        decisions = policy.on_session_close(105, 106, PRICE_BASIS, holding_session=5)
        self.assertEqual("day5_half", decisions[0].reason)
        self.assertEqual(100, policy.stop)
        policy.on_session_close(99, 100, PRICE_BASIS, holding_session=6)
        result = policy.on_session_open(98, PRICE_BASIS)
        self.assertEqual("gap_through_stop", result.reason)
        self.assertEqual(0.5, result.fraction)

    def test_event_ema_exit_does_not_apply_before_day5(self) -> None:
        policy = EventExitPolicy(100, 95, PRICE_BASIS)
        self.assertEqual((), policy.on_session_close(90, 100, PRICE_BASIS, holding_session=3))
        self.assertIsNone(policy.on_session_open(99, PRICE_BASIS))

    def test_event_time_exit_is_session_count(self) -> None:
        policy = EventExitPolicy(100, 95, PRICE_BASIS)
        result = policy.on_session_close(120, 110, PRICE_BASIS, holding_session=60)
        self.assertEqual("time_60_sessions", result[0].reason)

    def test_open_position_can_be_rebased_across_a_split(self) -> None:
        reversal = ReversalExitPolicy(100, 95, "pre-split")
        reversal.rebase_price_basis("post-split", 0.5)
        self.assertIsNone(reversal.on_bar(
            50, 51, 49, 50, "post-split", holding_session=2, session_close=False
        ))
        self.assertEqual(47.5, reversal.stop)
        self.assertEqual(55.0, reversal.target)

        event = EventExitPolicy(100, 95, "pre-split")
        event.rebase_price_basis("post-split", 0.5)
        self.assertIsNone(event.on_session_open(50, "post-split"))
        self.assertEqual(47.5, event.stop)

    def test_exit_policy_fails_closed_on_an_unrebased_price_basis(self) -> None:
        policy = ReversalExitPolicy(100, 95, "pre-split")
        with self.assertRaises(ValueError):
            policy.on_bar(
                50, 51, 49, 50, "post-split", holding_session=2, session_close=False
            )


if __name__ == "__main__":
    unittest.main()
