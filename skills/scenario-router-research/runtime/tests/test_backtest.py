from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scenario_router.backtest import (
    BacktestConfig,
    BacktestDataError,
    BacktestDataset,
    HistoricalBacktester,
    QuoteSnapshot,
    SecuritySessionRecord,
    UnsupportedBacktestFeature,
    simultaneous_entry_sort_key,
    verify_backtest_output,
)
from scenario_router.calendar import ExchangeSession, TradingSessionCalendar
from scenario_router.events import (
    ArticleLedger,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
)
from scenario_router.models import DailyBar, EntrySignal, IntradayBar, MacdSetup
from scenario_router.paper import CostModel
from scenario_router.risk import EntryQuote
import backtest_cli


ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
SECURITY_ID = "FIGI:BBG000B9XRY4"
BASIS = "split-adjusted-v1"
TARGET = date(2026, 8, 5)
END = date(2026, 8, 12)


def weekdays(start: date, end: date) -> list[date]:
    result = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def price_for(day: date) -> float:
    return {
        date(2026, 8, 5): 114.0,
        date(2026, 8, 6): 116.0,
        date(2026, 8, 7): 118.0,
        date(2026, 8, 10): 120.0,
        date(2026, 8, 11): 120.0,
        date(2026, 8, 12): 110.0,
    }.get(day, 100.0)


def make_intraday(day: date) -> list[IntradayBar]:
    result = []
    cursor = datetime.combine(day, time(9, 30), tzinfo=NY)
    close_time = datetime.combine(day, time(16, 0), tzinfo=NY)
    while cursor < close_time:
        if day == TARGET and cursor.time() == time(9, 30):
            values = (110.0, 111.0, 109.0, 110.0, 125_000.0)
        elif day == TARGET and cursor.time() == time(9, 35):
            values = (110.0, 112.0, 109.5, 111.0, 125_000.0)
        elif day == TARGET and cursor.time() == time(9, 40):
            values = (111.0, 111.5, 109.0, 110.0, 125_000.0)
        elif day == TARGET and cursor.time() == time(9, 45):
            values = (110.0, 111.0, 108.5, 110.0, 125_000.0)
        elif day == TARGET and cursor.time() == time(9, 50):
            values = (111.0, 112.1, 110.5, 112.0, 10_000.0)
        elif day == TARGET and cursor.time() == time(9, 55):
            values = (112.1, 114.0, 111.8, 113.5, 5_000.0)
        elif day == TARGET:
            values = (114.0, 115.0, 113.0, 114.0, 1_000.0)
        else:
            price = price_for(day)
            values = (price, price + 1.0, price - 1.0, price, 5_000.0)
        result.append(
            IntradayBar(
                "AAPL", SECURITY_ID, BASIS, cursor,
                values[0], values[1], values[2], values[3], values[4],
            )
        )
        cursor += timedelta(minutes=5)
    return result


def fixture_dataset(*, future_hash: str = "base", mutate_after_end: bool = False) -> BacktestDataset:
    days = weekdays(date(2026, 7, 1), date(2026, 8, 13))
    calendar = TradingSessionCalendar([
        ExchangeSession(
            day,
            datetime.combine(day, time(9, 30), tzinfo=NY),
            datetime.combine(day, time(16, 0), tzinfo=NY),
        )
        for day in days
    ])
    universe = tuple(
        SecuritySessionRecord(
            day, "AAPL", SECURITY_ID, BASIS, "primary_common_stock", "Technology", "mega-cap"
        )
        for day in days
    )
    daily = []
    intraday = []
    for day in days:
        if mutate_after_end and day > END:
            day_bars = []
            cursor = datetime.combine(day, time(9, 30), tzinfo=NY)
            while cursor < datetime.combine(day, time(16, 0), tzinfo=NY):
                day_bars.append(
                    IntradayBar(
                        "AAPL", SECURITY_ID, BASIS, cursor,
                        777.0, 778.0, 776.0, 777.0, 5_000.0,
                    )
                )
                cursor += timedelta(minutes=5)
        else:
            day_bars = make_intraday(day)
        intraday.extend(day_bars)
        daily.append(
            DailyBar(
                "AAPL", SECURITY_ID, BASIS, day,
                day_bars[0].open,
                max(item.high for item in day_bars),
                min(item.low for item in day_bars),
                day_bars[-1].close,
                sum(item.volume for item in day_bars),
            )
        )
    quotes = (
        QuoteSnapshot(
            datetime(2026, 8, 5, 9, 55, tzinfo=NY),
            "AAPL", SECURITY_ID, BASIS, 112.05, 112.15,
        ),
        QuoteSnapshot(
            datetime(2026, 8, 11, 16, 0, tzinfo=NY),
            "AAPL", SECURITY_ID, BASIS, 119.95, 120.05,
        ),
        QuoteSnapshot(
            datetime(2026, 8, 12, 9, 30, tzinfo=NY),
            "AAPL", SECURITY_ID, BASIS, 109.95, 110.05,
        ),
    )
    coverage = FeedCoverage.from_json(ROOT / "examples" / "feed_manifest.sample.json")
    return BacktestDataset(
        calendar=calendar,
        universe=universe,
        daily_bars=tuple(daily),
        intraday_bars=tuple(intraday),
        quotes=quotes,
        article_ledger=ArticleLedger.from_jsonl(
            ROOT / "examples" / "article_presence.sample.jsonl", coverage
        ),
        event_ledger=EventLedger.from_jsonl(
            ROOT / "examples" / "structured_events.sample.jsonl"
        ),
        reference_ledger=ReferenceSnapshotLedger.from_jsonl(
            ROOT / "examples" / "reference_snapshots.sample.jsonl"
        ),
        input_hashes={"synthetic_fixture": future_hash},
        source_directory=None,
    )


def write_fixture_directory(root: Path) -> None:
    dataset = fixture_dataset()

    def write_csv(name: str, header: tuple[str, ...], rows) -> None:
        with (root / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

    write_csv(
        "calendar.csv",
        ("session", "market_open", "market_close"),
        (
            (item.session.isoformat(), item.market_open.isoformat(), item.market_close.isoformat())
            for item in dataset.calendar.sessions
        ),
    )
    write_csv(
        "universe.csv",
        (
            "session", "ticker", "security_id", "price_basis_id",
            "security_classification", "sector", "cluster",
        ),
        (
            (
                item.session.isoformat(), item.ticker, item.security_id, item.price_basis_id,
                item.security_classification, item.sector, item.cluster,
            )
            for item in dataset.universe
        ),
    )
    write_csv(
        "daily_bars.csv",
        (
            "session", "ticker", "security_id", "price_basis_id",
            "open", "high", "low", "close", "volume",
        ),
        (
            (
                item.session.isoformat(), item.ticker, item.security_id, item.price_basis_id,
                item.open, item.high, item.low, item.close, item.volume,
            )
            for item in dataset.daily_bars
        ),
    )
    write_csv(
        "intraday_bars.csv",
        (
            "start", "ticker", "security_id", "price_basis_id",
            "open", "high", "low", "close", "volume",
        ),
        (
            (
                item.start.isoformat(), item.ticker, item.security_id, item.price_basis_id,
                item.open, item.high, item.low, item.close, item.volume,
            )
            for item in dataset.intraday_bars
        ),
    )
    write_csv(
        "quotes.csv",
        ("timestamp", "ticker", "security_id", "price_basis_id", "bid_price", "ask_price"),
        (
            (
                item.timestamp.isoformat(), item.ticker, item.security_id,
                item.price_basis_id, item.bid_price, item.ask_price,
            )
            for item in dataset.quotes
        ),
    )
    for source, destination in (
        ("feed_manifest.sample.json", "feed_manifest.json"),
        ("article_presence.sample.jsonl", "articles.jsonl"),
        ("structured_events.sample.jsonl", "events.jsonl"),
        ("reference_snapshots.sample.jsonl", "reference_snapshots.jsonl"),
    ):
        shutil.copyfile(ROOT / "examples" / source, root / destination)


def config(**overrides) -> BacktestConfig:
    values = {
        "start_session": TARGET,
        "end_session": END,
        "event_variant": "E2A",
        "reversal_variant": "M4",
        "event_mode": "strict_primary",
        "coverage_mode": "retrospective_audit",
        "data_mode": "synthetic_fixture",
        "code_revision": "test-revision",
    }
    values.update(overrides)
    return BacktestConfig(**values)


class HistoricalBacktestTests(unittest.TestCase):
    def test_full_event_route_replays_entry_reduction_and_gap_exit(self) -> None:
        result = HistoricalBacktester(fixture_dataset(), config()).run()
        self.assertEqual("SYNTHETIC_REPLAY_NOT_MARKET_EVIDENCE", result.metrics["research_status"])
        self.assertEqual((1, 1), (result.metrics["trades"], result.metrics["closed_trades"]))
        self.assertEqual(["buy", "sell", "sell"], [item["side"] for item in result.fills])
        self.assertEqual([68, 34, 34], [item["quantity"] for item in result.fills])
        self.assertEqual(
            ["ENTRY", "day5_half", "gap_through_stop"],
            [item["reason"] for item in result.fills],
        )
        target = next(item for item in result.candidates if item["session"] == TARGET.isoformat())
        self.assertEqual(("event", "FILLED", "ALL_FILLED"), (
            target["route"], target["terminal_status"], target["terminal_reason"],
        ))
        self.assertEqual(6, len(result.daily_nav))
        self.assertEqual(
            [0, 0, 0, 0, 1, 1],
            [item["exit_fill_count"] for item in result.daily_nav],
        )
        self.assertTrue(
            all(abs(item["pnl_reconciliation_residual"]) < 1e-8
                for item in result.daily_nav)
        )
        self.assertFalse(result.portfolio_snapshot["positions"])

    def test_costs_reduce_final_equity_without_double_deducting_slippage(self) -> None:
        zero = HistoricalBacktester(fixture_dataset(), config(), CostModel()).run()
        costed = HistoricalBacktester(
            fixture_dataset(), config(), CostModel(25.0, 0.01, 1.0)
        ).run()
        self.assertLess(costed.metrics["final_equity"], zero.metrics["final_equity"])
        self.assertGreater(costed.metrics["commission_cash_expense"], 0)
        self.assertGreater(
            costed.metrics["extra_slippage_embedded_in_fill_prices"], 0
        )
        trade = costed.trades[0]
        self.assertAlmostEqual(
            trade["net_pnl_after_commission"],
            trade["realized_fill_pnl_before_commission"]
            - trade["commission_cash_expense"],
        )
        self.assertAlmostEqual(
            costed.metrics["pnl_reconciliation"]["portfolio_net_pnl"],
            costed.metrics["pnl_reconciliation"]["trade_bookkeeping_net_pnl"],
        )
        self.assertAlmostEqual(
            costed.metrics["pnl_reconciliation"]["portfolio_net_pnl"],
            costed.metrics["pnl_reconciliation"]["daily_bookkeeping_net_pnl"],
        )
        self.assertNotIn("fee", costed.fills[0])
        self.assertNotIn("extra_slippage", costed.fills[0])

    def test_partial_exit_on_open_trade_is_not_labeled_final_exit(self) -> None:
        result = HistoricalBacktester(
            fixture_dataset(), config(end_session=date(2026, 8, 11))
        ).run()
        trade = result.trades[0]
        self.assertEqual("OPEN", trade["status"])
        self.assertEqual("day5_half", trade["last_exit_reason"])
        self.assertIsNone(trade["final_exit_reason"])
        self.assertIsNone(trade["final_exit_at"])
        self.assertIsNotNone(trade["mark_at"])
        self.assertIsNotNone(trade["mark_price"])

    def test_partial_exit_marks_only_sold_shares_at_slipped_fill(self) -> None:
        result = HistoricalBacktester(
            fixture_dataset(),
            config(end_session=date(2026, 8, 11)),
            CostModel(slippage_bps=100.0),
        ).run()
        close_at = datetime(2026, 8, 11, 16, 0, tzinfo=NY).isoformat()
        curve = [
            item for item in result.portfolio_snapshot["equity_curve"]
            if item["at"] == close_at
        ]
        mark = next(item for item in curve if item["reason"] == "MARK")
        exit_fill = next(item for item in curve if item["reason"] == "EXIT_FILL")
        self.assertAlmostEqual(mark["equity"], exit_fill["equity"])
        self.assertEqual(
            "boundary_nbbo_bid_after_partial_exit",
            result.trades[0]["mark_valuation_basis"],
        )
        self.assertEqual(
            "mixed_completed_bar_close_and_boundary_bid_after_partial_exit",
            result.daily_nav[-1]["valuation_basis"],
        )

    def test_unverified_event_reference_degrades_per_core_instead_of_aborting(self) -> None:
        base = fixture_dataset()
        pending = replace(
            base.event_ledger.records[0], verification_status="pending"
        )
        dataset = replace(
            base,
            event_ledger=EventLedger([pending]),
            reference_ledger=ReferenceSnapshotLedger([]),
            input_hashes={"synthetic_fixture": "pending-missing-reference"},
        )
        result = HistoricalBacktester(
            dataset, config(end_session=TARGET, reversal_variant="M3")
        ).run()
        self.assertEqual(0, result.metrics["trades"])
        self.assertEqual("QUALIFIED_EVENT_E1_ONLY", result.candidates[0]["qualification_status"])
        self.assertEqual(
            ["E1_ONLY_UNVERIFIED_OR_CONFLICTED_FACTS"],
            result.candidates[0]["qualification_reason_codes"],
        )
        self.assertEqual("ABSTAIN", result.candidates[0]["terminal_status"])

    def test_m2_reversal_route_executes_at_open_and_honors_gap_stop(self) -> None:
        base = fixture_dataset()
        empty_articles = ArticleLedger([], base.article_ledger.coverage)
        daily = tuple(
            replace(item, open=94.0, low=93.0)
            if item.session == date(2026, 8, 7) else item
            for item in base.daily_bars
        )
        intraday = tuple(
            replace(item, open=94.0, high=95.0, low=93.0, close=94.0)
            if item.session == date(2026, 8, 7) and item.exchange_start.time() == time(9, 30)
            else item
            for item in base.intraday_bars
        )
        quotes = (
            QuoteSnapshot(
                datetime(2026, 8, 6, 9, 30, tzinfo=NY),
                "AAPL", SECURITY_ID, BASIS, 116.05, 116.15,
            ),
            QuoteSnapshot(
                datetime(2026, 8, 7, 9, 30, tzinfo=NY),
                "AAPL", SECURITY_ID, BASIS, 93.95, 94.05,
            ),
        )
        dataset = replace(
            base,
            daily_bars=daily,
            intraday_bars=intraday,
            quotes=quotes,
            article_ledger=empty_articles,
            event_ledger=EventLedger([]),
            reference_ledger=ReferenceSnapshotLedger([]),
            input_hashes={"synthetic_fixture": "m2"},
        )

        class ForcedSetupBacktester(HistoricalBacktester):
            def _update_daily_detectors(self, session: date) -> None:
                if session == date(2026, 8, 5):
                    self.pending_setups[SECURITY_ID] = MacdSetup(
                        "AAPL", SECURITY_ID, BASIS,
                        session, date(2026, 8, 3), 95.0, -0.5,
                    )

        result = ForcedSetupBacktester(
            dataset,
            config(
                start_session=date(2026, 8, 6),
                end_session=date(2026, 8, 7),
                reversal_variant="M2",
            ),
        ).run()
        self.assertEqual(["buy", "sell"], [item["side"] for item in result.fills])
        self.assertEqual(datetime(2026, 8, 6, 9, 30, tzinfo=NY).isoformat(), result.fills[0]["at"])
        self.assertEqual("stop", result.fills[1]["reason"])
        self.assertEqual(93.95, result.fills[1]["price"])
        candidate = next(
            item for item in result.candidates if item["session"] == "2026-08-06"
        )
        self.assertEqual(("reversal", "FILLED"), (
            candidate["route"], candidate["terminal_status"],
        ))

    def test_stop_bar_close_cannot_create_a_false_drawdown_halt(self) -> None:
        base = fixture_dataset()
        empty_articles = ArticleLedger([], base.article_ledger.coverage)
        stop_day = date(2026, 8, 6)
        daily = tuple(
            replace(item, high=2100.0, low=90.0)
            if item.session == stop_day else item
            for item in base.daily_bars
        )
        intraday = tuple(
            replace(item, open=116.0, high=2100.0, low=90.0, close=2000.0)
            if item.session == stop_day
            and item.exchange_start.time() == time(9, 30)
            else item
            for item in base.intraday_bars
        )
        dataset = replace(
            base,
            daily_bars=daily,
            intraday_bars=intraday,
            quotes=(
                QuoteSnapshot(
                    datetime(2026, 8, 6, 9, 30, tzinfo=NY),
                    "AAPL", SECURITY_ID, BASIS, 116.05, 116.15,
                ),
                QuoteSnapshot(
                    datetime(2026, 8, 6, 9, 35, tzinfo=NY),
                    "AAPL", SECURITY_ID, BASIS, 115.95, 116.05,
                ),
            ),
            article_ledger=empty_articles,
            event_ledger=EventLedger([]),
            reference_ledger=ReferenceSnapshotLedger([]),
            input_hashes={"synthetic_fixture": "stop-bar-path"},
        )

        class ForcedSetupBacktester(HistoricalBacktester):
            def _update_daily_detectors(self, session: date) -> None:
                if session == date(2026, 8, 5):
                    self.pending_setups[SECURITY_ID] = MacdSetup(
                        "AAPL", SECURITY_ID, BASIS,
                        session, date(2026, 8, 3), 95.0, -0.5,
                    )

        result = ForcedSetupBacktester(
            dataset,
            config(
                start_session=stop_day,
                end_session=stop_day,
                reversal_variant="M2",
            ),
        ).run()
        self.assertEqual(["buy", "sell"], [item["side"] for item in result.fills])
        self.assertEqual("same_bar_stop_first", result.fills[1]["reason"])
        self.assertEqual(95.0, result.fills[1]["price"])
        self.assertFalse(result.portfolio_snapshot["halted"])
        self.assertLess(result.portfolio_snapshot["maximum_risk_drawdown"], 0.01)
        boundary_marks = [
            item for item in result.portfolio_snapshot["equity_curve"]
            if item["at"] == datetime(2026, 8, 6, 9, 35, tzinfo=NY).isoformat()
            and item["reason"] == "MARK"
        ]
        self.assertEqual(1, len(boundary_marks))
        self.assertAlmostEqual(
            result.metrics["final_equity"], boundary_marks[0]["equity"]
        )

    def test_future_captured_manifest_abstains_in_point_in_time_mode(self) -> None:
        result = HistoricalBacktester(
            fixture_dataset(),
            config(
                coverage_mode="point_in_time",
                end_session=TARGET,
                reversal_variant="M3",
            ),
        ).run()
        self.assertEqual(0, result.metrics["trades"])
        target = next(item for item in result.candidates if item["session"] == TARGET.isoformat())
        self.assertEqual("ABSTAIN", target["terminal_status"])
        self.assertEqual("ABSTAIN_MISSING_COVERAGE", target["terminal_reason"])
        self.assertIn(
            "ABSTAIN_FUTURE_CAPTURED_COVERAGE_MANIFEST",
            target["qualification_reason_codes"],
        )

    def test_unsupported_point_in_time_coverage_shapes_fail_closed(self) -> None:
        with self.assertRaisesRegex(BacktestDataError, "exactly one session"):
            HistoricalBacktester(
                fixture_dataset(), config(coverage_mode="point_in_time")
            )
        with self.assertRaisesRegex(BacktestDataError, "every intraday decision"):
            HistoricalBacktester(
                fixture_dataset(),
                config(
                    coverage_mode="point_in_time",
                    end_session=TARGET,
                    reversal_variant="M4",
                ),
            )

    def test_historical_label_requires_immutable_revision_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "immutable code revision"):
            config(data_mode="historical_point_in_time", code_revision="UNSPECIFIED")

    def test_macd_setup_price_basis_change_invalidates_only_the_candidate(self) -> None:
        base = fixture_dataset()
        new_basis = "post-action-v2"
        empty_articles = ArticleLedger([], base.article_ledger.coverage)
        dataset = replace(
            base,
            universe=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == TARGET else item
                for item in base.universe
            ),
            daily_bars=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == TARGET else item
                for item in base.daily_bars
            ),
            intraday_bars=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == TARGET else item
                for item in base.intraday_bars
            ),
            quotes=tuple(
                replace(item, price_basis_id=new_basis)
                if item.timestamp.date() == TARGET else item
                for item in base.quotes
            ),
            article_ledger=empty_articles,
            event_ledger=EventLedger([]),
            reference_ledger=ReferenceSnapshotLedger([]),
            input_hashes={"synthetic_fixture": "setup-basis-change"},
        )

        class ForcedSetupBacktester(HistoricalBacktester):
            def _update_daily_detectors(self, session: date) -> None:
                if session == date(2026, 8, 4):
                    self.pending_setups[SECURITY_ID] = MacdSetup(
                        "AAPL", SECURITY_ID, BASIS,
                        session, date(2026, 8, 3), 95.0, -0.5,
                    )

        result = ForcedSetupBacktester(
            dataset,
            config(
                start_session=TARGET,
                end_session=TARGET,
                reversal_variant="M2",
            ),
        ).run()
        self.assertEqual(0, result.metrics["trades"])
        self.assertEqual(
            "INVALIDATED_SETUP_PRICE_BASIS_CHANGE",
            result.candidates[0]["terminal_reason"],
        )

    def test_future_data_mutation_does_not_change_past_decisions_or_order_ids(self) -> None:
        base = HistoricalBacktester(
            fixture_dataset(future_hash="base"), config()
        ).run()
        changed = HistoricalBacktester(
            fixture_dataset(future_hash="changed", mutate_after_end=True), config()
        ).run()
        self.assertNotEqual(base.metrics["run_id"], changed.metrics["run_id"])
        self.assertEqual(base.candidates, changed.candidates)
        self.assertEqual(base.orders, changed.orders)
        self.assertEqual(base.fills, changed.fills)
        self.assertEqual(base.daily_nav, changed.daily_nav)
        self.assertEqual(
            base.run_manifest["decision_result_sha256"],
            changed.run_manifest["decision_result_sha256"],
        )

    def test_input_row_order_does_not_change_replay(self) -> None:
        dataset = fixture_dataset()
        reordered = replace(
            dataset,
            universe=tuple(reversed(dataset.universe)),
            daily_bars=tuple(reversed(dataset.daily_bars)),
            intraday_bars=tuple(reversed(dataset.intraday_bars)),
            quotes=tuple(reversed(dataset.quotes)),
        )
        base = HistoricalBacktester(dataset, config()).run()
        changed = HistoricalBacktester(reordered, config()).run()
        self.assertEqual(
            base.run_manifest["decision_result_sha256"],
            changed.run_manifest["decision_result_sha256"],
        )
        self.assertEqual(base.portfolio_snapshot, changed.portfolio_snapshot)

    def test_next_bar_high_low_close_cannot_change_boundary_entry(self) -> None:
        dataset = fixture_dataset()
        changed_intraday = tuple(
            replace(item, high=150.0, close=120.0)
            if item.session == TARGET and item.exchange_start.time() == time(9, 55)
            else item
            for item in dataset.intraday_bars
        )
        changed_daily = tuple(
            replace(item, high=150.0) if item.session == TARGET else item
            for item in dataset.daily_bars
        )
        changed = replace(
            dataset,
            intraday_bars=changed_intraday,
            daily_bars=changed_daily,
            input_hashes={"synthetic_fixture": "next-bar-mutated"},
        )
        base_result = HistoricalBacktester(dataset, config()).run()
        changed_result = HistoricalBacktester(changed, config()).run()
        self.assertEqual(base_result.fills[0], changed_result.fills[0])
        self.assertEqual(base_result.orders[0], changed_result.orders[0])

    def test_mixed_daily_and_intraday_rth_aggregates_fail_closed(self) -> None:
        dataset = fixture_dataset()
        changed = replace(
            dataset,
            daily_bars=tuple(
                replace(item, volume=item.volume + 2.0)
                if item.session == TARGET else item
                for item in dataset.daily_bars
            ),
        )
        with self.assertRaisesRegex(
            BacktestDataError, "regular-hours OHLCV aggregate mismatch"
        ):
            HistoricalBacktester(changed, config())

    def test_missing_entry_nbbo_fails_instead_of_selectively_dropping_trade(self) -> None:
        dataset = fixture_dataset()
        dataset = replace(dataset, quotes=dataset.quotes[1:])
        with self.assertRaisesRegex(BacktestDataError, "missing boundary NBBO"):
            HistoricalBacktester(dataset, config()).run()

    def test_open_position_corporate_action_fails_instead_of_silent_rebase(self) -> None:
        dataset = fixture_dataset()
        action_day = date(2026, 8, 6)
        new_basis = "post-split-v2"
        changed = replace(
            dataset,
            universe=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == action_day else item
                for item in dataset.universe
            ),
            daily_bars=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == action_day else item
                for item in dataset.daily_bars
            ),
            intraday_bars=tuple(
                replace(item, price_basis_id=new_basis)
                if item.session == action_day else item
                for item in dataset.intraday_bars
            ),
            input_hashes={"synthetic_fixture": "corporate-action"},
        )
        with self.assertRaisesRegex(
            UnsupportedBacktestFeature, "open-position corporate action"
        ):
            HistoricalBacktester(changed, config()).run()

    def test_written_outputs_are_hashed_and_snapshot_reloads(self) -> None:
        result = HistoricalBacktester(fixture_dataset(), config()).run()
        with tempfile.TemporaryDirectory() as directory:
            manifest = result.write(directory)
            root = Path(directory)
            self.assertEqual(
                set(manifest["output_sha256"]),
                {
                    "metrics.json", "candidate_ledger.jsonl", "orders.jsonl",
                    "fills.jsonl", "trades.jsonl", "execution_audit.jsonl",
                    "daily_nav.csv", "portfolio_snapshot.json",
                },
            )
            restored = json.loads((root / "portfolio_snapshot.json").read_text())
            self.assertEqual(result.portfolio_snapshot, restored)
            self.assertTrue((root / "run_manifest.json").is_file())
            for name, expected in manifest["output_sha256"].items():
                self.assertEqual(
                    expected, hashlib.sha256((root / name).read_bytes()).hexdigest()
                )
            verified = verify_backtest_output(root)
            self.assertEqual("PASS_BACKTEST_OUTPUT_INTEGRITY", verified["status"])
            self.assertAlmostEqual(result.metrics["final_equity"], verified["restored_final_equity"])
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                result.write(directory)
            (root / "metrics.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BacktestDataError, "hash mismatch"):
                verify_backtest_output(root)

    def test_directory_loader_and_cli_run_the_same_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            output = root / "out"
            data.mkdir()
            write_fixture_directory(data)
            loaded = BacktestDataset.from_directory(data)
            direct = HistoricalBacktester(loaded, config()).run()
            with contextlib.redirect_stdout(io.StringIO()):
                status = backtest_cli.main([
                    "--data", str(data),
                    "--output", str(output),
                    "--start", TARGET.isoformat(),
                    "--end", END.isoformat(),
                    "--event-variant", "E2A",
                    "--reversal-variant", "M4",
                    "--slippage-bps", "0",
                    "--data-mode", "synthetic_fixture",
                    "--code-revision", "test-revision",
                ])
            self.assertEqual(0, status)
            cli_metrics = json.loads(
                (output / "extra_slippage_0_bps_per_side" / "metrics.json").read_text()
            )
            self.assertEqual(direct.metrics, cli_metrics)
            self.assertTrue((output / "cost_comparison.json").is_file())

    def test_simultaneous_sort_is_independent_of_input_order(self) -> None:
        stamp = datetime(2026, 8, 5, 10, 0, tzinfo=NY)
        pairs = []
        for index, ticker in enumerate(("A", "B", "C", "D", "E")):
            signal = EntrySignal(
                f"signal-{ticker}", ticker, f"SID:{ticker}",
                "event" if ticker != "A" else "reversal",
                "E2A" if ticker != "A" else "M4",
                stamp, stamp, 95.0, f"context-{ticker}",
                {"price_basis_id": BASIS, "experiment_id": "sort-test"},
            )
            quote = EntryQuote(
                ticker, f"SID:{ticker}", BASIS, stamp, 100.0, 99.9, 100.1,
                100.0, 50_000_000.0 + index, "primary_common_stock",
            )
            pairs.append((signal, quote))
        forward = [
            signal.ticker for signal, quote in sorted(
                pairs, key=lambda pair: simultaneous_entry_sort_key(*pair)
            )
        ]
        reverse = [
            signal.ticker for signal, quote in sorted(
                reversed(pairs), key=lambda pair: simultaneous_entry_sort_key(*pair)
            )
        ]
        self.assertEqual(forward, reverse)
        self.assertEqual(["E", "D", "C", "B", "A"], forward)


if __name__ == "__main__":
    unittest.main()
