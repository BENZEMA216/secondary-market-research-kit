from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scenario_router.models import EntrySignal
from scenario_router.paper import CostModel, PaperBroker
from scenario_router.portfolio import MarketMark, PortfolioLedger
from scenario_router.portfolio_risk import RiskController, RiskPolicy
from scenario_router.risk import EntryQuote


NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 5, 9, 55, tzinfo=NY)
BASIS = "split-adjusted-test-v1"


def signal(name="TEST", at=NOW, stop=95.0, context=None):
    return EntrySignal(
        f"signal-{name}-{at.isoformat()}", name, f"SID:{name}", "event", "E2A",
        at, at, stop, context or f"context-{name}-{at.date()}",
        {"price_basis_id": BASIS, "experiment_id": "paper-test:E2A+M4"},
    )


def quote(name="TEST", at=NOW, price=100.0, *, ask=None, next_open=None):
    return EntryQuote(name, f"SID:{name}", BASIS, at,
                      price if next_open is None else next_open,
                      price, price if ask is None else ask,
                      100.0, 50_000_000.0, "primary_common_stock")


def mark_all(ledger, at, prices=None):
    prices = prices or {}
    ledger.mark_to_market([
        MarketMark(p.security_id, prices.get(p.ticker, p.mark_price), at, p.price_basis_id)
        for p in ledger.positions.values()
    ], at)


class PaperPortfolioTests(unittest.TestCase):
    def open(self, broker, name="TEST", at=NOW, stop=95, price=100, sector="Technology", cluster="mega"):
        return broker.submit_and_fill(signal(name, at, stop), quote(name, at, price),
                                      sector=sector, cluster=cluster, as_of=at)

    def test_full_synthetic_oracle_through_real_ledger_and_exit_callbacks(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = self.open(broker, stop=108.5, price=112.15)
        self.assertEqual(("FILLED", 68), (order.status, order.quantity))
        policy = broker.get_exit_policy(order.order_id)
        day5 = NOW + timedelta(days=6)
        mark_all(ledger, day5, {"TEST": 120})
        reduction = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_close", 120, 121, BASIS,
            holding_session=5, quote=quote(at=day5, price=120), as_of=day5,
        )
        self.assertEqual(34, reduction.fills[0]["quantity"])
        self.assertEqual(34, ledger.positions[order.order_id].quantity)
        next_open = day5 + timedelta(days=1)
        final = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_open", 119, BASIS,
            quote=quote(at=next_open, price=119), as_of=next_open,
        )
        self.assertEqual("FILLED", final.status)
        self.assertTrue(policy.closed)
        self.assertFalse(ledger.positions)
        self.assertAlmostEqual(499.80, ledger.realized_gross_pnl)
        self.assertAlmostEqual(100_499.80, ledger.cash)
        self.assertAlmostEqual(ledger.cash, ledger.equity)

    def test_fee_and_extra_slippage_debit_cash_exactly_once(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger, cost_model=CostModel(10, .01, 1))
        order = self.open(broker)
        entry = ledger.fills[0]
        self.assertAlmostEqual(100.10, entry["price"])
        self.assertAlmostEqual(100_000 - entry["quantity"] * 100.1 - entry["fee"], ledger.cash)
        when = NOW + timedelta(minutes=5)
        result = broker.exit_position(order.order_id, quote(at=when, price=101), as_of=when)
        self.assertEqual("FILLED", result.status)
        self.assertAlmostEqual(100.899, result.fills[0]["price"])
        self.assertAlmostEqual(sum(f["fee"] for f in ledger.fills), ledger.fees_paid)
        self.assertAlmostEqual(ledger.realized_gross_pnl - ledger.fees_paid, ledger.net_pnl)
        self.assertGreater(ledger.slippage_paid, 0)

    def test_pending_reservations_count_toward_all_position_slots(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        orders = [broker.submit(signal(f"N{i}"), quote(f"N{i}"), sector=f"S{i}",
                                cluster=f"C{i}", as_of=NOW) for i in range(5)]
        self.assertEqual(["PENDING"] * 4 + ["REJECTED"], [o.status for o in orders])
        self.assertEqual("POSITION_LIMIT", orders[-1].reason)
        self.assertEqual(20_000, ledger.reserved_cash)
        self.assertEqual(80_000, ledger.available_cash)

    def test_cluster_initial_stop_risk_includes_pending(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        orders = [broker.submit(signal(f"N{i}"), quote(f"N{i}"), sector=f"S{i}",
                                cluster="same-cluster", as_of=NOW) for i in range(3)]
        self.assertEqual(["PENDING", "PENDING", "REJECTED"], [o.status for o in orders])
        self.assertEqual(500, sum(o.initial_stop_risk for o in ledger.pending_orders))

    def test_sector_limit_and_unknown_classification(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        first = broker.submit(signal("A", stop=99), quote("A"), sector="Tech", cluster="A", as_of=NOW)
        second = broker.submit(signal("B", stop=99), quote("B"), sector="Tech", cluster="B", as_of=NOW)
        self.assertEqual((250, 100), (first.quantity, second.quantity))
        unknown = broker.submit(signal("C"), quote("C"), sector="unknown", cluster="C", as_of=NOW)
        self.assertEqual("UNKNOWN_SECTOR_OR_CLUSTER", unknown.reason)

    def test_cash_includes_fees_and_pending_fee_reservations(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger, cost_model=CostModel(minimum_commission=60_000))
        first = broker.submit(signal("A"), quote("A"), sector="A", cluster="A", as_of=NOW)
        second = broker.submit(signal("B"), quote("B"), sector="B", cluster="B", as_of=NOW)
        self.assertEqual("PENDING", first.status)
        self.assertEqual(65_000, first.reserved_cash)
        self.assertEqual("REJECTED", second.status)
        self.assertGreaterEqual(ledger.available_cash, 0)

    def test_duplicate_context_is_one_order_under_concurrent_calls(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.open(broker), range(20)))
        self.assertEqual(1, len({o.order_id for o in results}))
        self.assertEqual(1, len(ledger.fills))
        self.assertEqual(1, len(ledger.positions))

    def test_snapshot_pending_fill_and_restart_duplicate_are_identical(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger, cost_model=CostModel(2, .01, 1))
        order = broker.submit(signal(), quote(), sector="Tech", cluster="mega", as_of=NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            ledger.save(path)
            recovered = PortfolioLedger.load(path)
        resumed = PaperBroker(recovered)
        resumed.fill(order.order_id, quote(), as_of=NOW)
        broker.fill(order.order_id, quote(), as_of=NOW)
        self.assertEqual(ledger.snapshot(), recovered.snapshot())
        restored = PortfolioLedger.from_snapshot(json.loads(json.dumps(recovered.snapshot())))
        duplicate = PaperBroker(restored).submit_and_fill(
            signal(), quote(), sector="Tech", cluster="mega", as_of=NOW)
        self.assertEqual("FILLED", duplicate.status)
        self.assertEqual(1, len(restored.fills))

    def test_stale_quote_and_gap_through_stop_release_reservation(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = broker.submit(signal(), quote(), sector="Tech", cluster="mega", as_of=NOW)
        result = broker.fill(order.order_id, quote(price=90), as_of=NOW)
        self.assertEqual("GAP_THROUGH_STOP", result.reason)
        self.assertEqual(0, ledger.reserved_cash)
        late = NOW + timedelta(minutes=5)
        second = broker.submit(signal("B", late), quote("B", NOW), sector="Tech", cluster="B", as_of=late)
        self.assertEqual("STALE_OR_WRONG_TIME_QUOTE", second.reason)
        self.assertFalse(ledger.fills)

    def test_unknown_or_stale_valuation_cannot_fund_new_risk(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        self.open(broker)
        later = NOW + timedelta(minutes=5)
        rejected = broker.submit(signal("B", later), quote("B", later),
                                 sector="Other", cluster="Other", as_of=later)
        self.assertEqual("STALE_PORTFOLIO_MARKS", rejected.reason)

    def test_daily_loss_blocks_for_the_session_and_next_session_resets(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        self.open(broker, stop=99)
        later = NOW + timedelta(minutes=5)
        mark_all(ledger, later, {"TEST": 96})
        self.assertEqual("DAILY_BLOCKED", broker.risk.refresh(ledger, later))
        recovery = later + timedelta(minutes=5)
        mark_all(ledger, recovery, {"TEST": 100})
        self.assertEqual("DAILY_BLOCKED", broker.risk.refresh(ledger, recovery))
        tomorrow = recovery + timedelta(days=1)
        mark_all(ledger, tomorrow)
        self.assertEqual("NORMAL", broker.risk.refresh(ledger, tomorrow))

    def test_five_percent_drawdown_halves_only_new_trade_risk(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        first = self.open(broker, stop=99)
        later = NOW + timedelta(minutes=5)
        mark_all(ledger, later, {"TEST": 80})
        tomorrow = later + timedelta(days=1)
        mark_all(ledger, tomorrow)
        second = self.open(broker, "B", tomorrow, sector="Other", cluster="Other")
        self.assertEqual("FILLED", second.status)
        self.assertEqual(250, ledger.positions[first.order_id].quantity)
        self.assertLessEqual(second.initial_stop_risk, .00125 * 95_000)
        self.assertEqual(23, second.quantity)

    def test_ten_percent_halt_cancels_pending_is_latched_and_does_not_block_exit(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        first = self.open(broker, stop=99)
        pending = broker.submit(signal("B"), quote("B"), sector="Other", cluster="Other", as_of=NOW)
        later = NOW + timedelta(minutes=5)
        mark_all(ledger, later, {"TEST": 60})
        self.assertEqual("HALTED", broker.risk.refresh(ledger, later))
        self.assertEqual("CANCELLED", pending.status)
        self.assertEqual(0, ledger.reserved_cash)
        saved = PortfolioLedger.from_snapshot(ledger.snapshot())
        resumed = PaperBroker(saved)
        self.assertTrue(saved.halted)
        result = resumed.exit_position(first.order_id, quote(at=later, price=60), as_of=later)
        self.assertEqual("FILLED", result.status)
        self.assertTrue(saved.halted)
        original_mdd = saved.maximum_drawdown
        resumed.risk.reset_halt(saved, operator="local-user", reason="new research episode", as_of=later)
        self.assertFalse(saved.halted)
        self.assertEqual(original_mdd, saved.maximum_drawdown)
        self.assertEqual("DAILY_BLOCKED", resumed.risk.refresh(saved, later))
        self.assertEqual("MANUAL_RISK_RESET", saved.events[-1]["kind"])

    def test_unfilled_reduction_does_not_mutate_policy(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        policy = broker.get_exit_policy(order.order_id)
        before = copy.deepcopy(policy.__dict__)
        later = NOW + timedelta(days=6)
        failed = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_close", 120, 121, BASIS,
            holding_session=5, quote=None, as_of=later,
        )
        self.assertEqual("REJECTED", failed.status)
        self.assertEqual(before, policy.__dict__)
        self.assertEqual(before, ledger.exit_policy_states[order.order_id]["state"])
        self.assertEqual(50, ledger.positions[order.order_id].quantity)

    def test_odd_share_reduction_leaves_no_fractional_or_orphan_shares(self):
        ledger = PortfolioLedger(6_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        self.assertEqual(3, order.quantity)
        policy = broker.get_exit_policy(order.order_id)
        later = NOW + timedelta(days=6)
        result = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_close", 120, 121, BASIS,
            holding_session=5, quote=quote(at=later, price=120), as_of=later,
        )
        self.assertEqual(1, result.fills[0]["quantity"])
        self.assertEqual(2, ledger.positions[order.order_id].quantity)
        self.assertAlmostEqual(2 / 3, policy.remaining_fraction)
        tomorrow = later + timedelta(days=1)
        final = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_open", 119, BASIS,
            quote=quote(at=tomorrow, price=119), as_of=tomorrow,
        )
        self.assertEqual(2, final.fills[0]["quantity"])
        self.assertFalse(ledger.positions)

    def test_exit_id_and_context_remain_consumed_after_close_and_restore(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        broker.exit_position(order.order_id, quote(), as_of=NOW, exit_id="exit-1")
        recovered = PortfolioLedger.from_snapshot(ledger.snapshot())
        resumed = PaperBroker(recovered)
        resumed.exit_position(order.order_id, quote(), as_of=NOW, exit_id="exit-1")
        self.open(resumed)
        self.assertEqual(2, len(recovered.fills))
        self.assertFalse(recovered.positions)

    def test_policy_and_cost_version_cannot_silently_change(self):
        with self.assertRaises(ValueError):
            RiskPolicy(maximum_positions=10)
        ledger = PortfolioLedger(100_000)
        PaperBroker(ledger, cost_model=CostModel(5))
        with self.assertRaises(ValueError):
            PaperBroker(ledger, cost_model=CostModel(10))

    def test_risk_limit_touch_cannot_be_erased_by_a_later_recovery_mark(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        self.open(broker, stop=99)
        low = NOW + timedelta(minutes=5)
        mark_all(ledger, low, {"TEST": 60})
        recovered = low + timedelta(minutes=5)
        mark_all(ledger, recovered, {"TEST": 105})
        # Even an adapter that refreshes only at its next order cannot miss the
        # earlier observed 10% breach. Preferred broker.mark_to_market latches now.
        self.assertEqual("HALTED", broker.risk.refresh(ledger, recovered))
        self.assertAlmostEqual(.10, ledger.maximum_drawdown)

    def test_broker_mark_entrypoint_immediately_cancels_pending_on_halt(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        self.open(broker, stop=99)
        pending = broker.submit(signal("B"), quote("B"), sector="B", cluster="B", as_of=NOW)
        later = NOW + timedelta(minutes=5)
        state = broker.on_market([MarketMark("SID:TEST", 60, later, BASIS)], later)
        self.assertEqual("HALTED", state)
        self.assertEqual("CANCELLED", pending.status)

    def test_entry_fill_rechecks_changed_price_without_upsizing(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = broker.submit(signal(), quote(), sector="Tech", cluster="mega", as_of=NOW)
        rejected = broker.fill(order.order_id, quote(price=110), as_of=NOW)
        self.assertEqual("FILL_EXCEEDS_RESERVED_RISK", rejected.reason)
        self.assertEqual(0, ledger.reserved_cash)
        second = broker.submit(signal("B"), quote("B"), sector="B", cluster="B", as_of=NOW)
        filled = broker.fill(second.order_id, quote("B", price=99), as_of=NOW)
        self.assertEqual(("FILLED", 50), (filled.status, filled.quantity))
        self.assertEqual(200, filled.initial_stop_risk)

    def test_pending_orders_survive_snapshot_without_releasing_limits(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        for i in range(4):
            broker.submit(signal(str(i)), quote(str(i)), sector=str(i), cluster=str(i), as_of=NOW)
        recovered = PortfolioLedger.from_snapshot(ledger.snapshot())
        blocked = PaperBroker(recovered).submit(signal("FIFTH"), quote("FIFTH"),
                                                sector="Fifth", cluster="Fifth", as_of=NOW)
        self.assertEqual("POSITION_LIMIT", blocked.reason)
        self.assertEqual(20_000, recovered.reserved_cash)

    def test_one_share_is_closed_at_day5_and_reversal_stop_is_stop_first(self):
        ledger = PortfolioLedger(2_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        policy = broker.get_exit_policy(order.order_id)
        later = NOW + timedelta(days=6)
        outcome = broker.propose_and_execute_exit(
            order.order_id, policy, "on_session_close", 120, 110, BASIS,
            holding_session=5, quote=quote(at=later, price=120), as_of=later)
        self.assertEqual(1, outcome.fills[0]["quantity"])
        self.assertFalse(ledger.positions)
        self.assertTrue(policy.closed)
        # Reversal execution consumes the same frozen exit primitive.
        reversal = signal("REV", later)
        from dataclasses import replace
        reversal = replace(reversal, branch="reversal", variant="M4")
        second = broker.submit_and_fill(reversal, quote("REV", later), sector="R", cluster="R", as_of=later)
        rev_policy = broker.get_exit_policy(second.order_id)
        stopped = broker.propose_and_execute_exit(
            second.order_id, rev_policy, "on_bar", 100, 111, 94, 100, BASIS,
            holding_session=1, session_close=False,
            quote=quote("REV", later, price=100), as_of=later)
        self.assertEqual("same_bar_stop_first", stopped.reason)
        self.assertEqual(95, stopped.fills[0]["price"])

    def test_fixed_security_classification_survives_closure_and_restart(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        broker.exit_position(order.order_id, quote(), as_of=NOW)
        later = NOW + timedelta(days=1)
        restored = PortfolioLedger.from_snapshot(ledger.snapshot())
        changed = self.open(PaperBroker(restored), at=later, sector="Different", cluster="Different")
        self.assertEqual("CLASSIFICATION_VERSION_OR_MAPPING_CHANGED", changed.reason)

    def test_stale_or_cross_position_policy_cannot_overwrite_ledger_after_restart(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        first = self.open(broker)
        stale_policy = broker.get_exit_policy(first.order_id)
        second = self.open(broker, "B", sector="Other", cluster="Other")
        wrong_policy = broker.get_exit_policy(second.order_id)
        with self.assertRaisesRegex(ValueError, "stale"):
            broker.propose_and_execute_exit(first.order_id, wrong_policy, "on_session_close",
                120, 121, BASIS, holding_session=5, quote=quote(price=120), as_of=NOW)
        broker.exit_position(first.order_id, quote(), quantity=10, as_of=NOW, exit_id="partial")
        restored = PortfolioLedger.from_snapshot(ledger.snapshot())
        resumed = PaperBroker(restored)
        with self.assertRaisesRegex(ValueError, "stale"):
            resumed.propose_and_execute_exit(first.order_id, stale_policy, "on_session_close",
                120, 121, BASIS, holding_session=5, quote=quote(price=120), as_of=NOW)
        self.assertEqual(40, restored.positions[first.order_id].quantity)

    def test_restore_rejects_shape_nonfinite_negative_naive_and_accounting_changes(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        order = self.open(broker)
        source = ledger.snapshot()
        mutations = [
            lambda raw: raw.update(unknown_field=1),
            lambda raw: raw.pop("cash"),
            lambda raw: raw.pop("initial_cash"),
            lambda raw: raw.update(fees_paid=float("nan")),
            lambda raw: raw.update(last_event_at="2026-08-05T09:55:00"),
            lambda raw: raw["positions"][order.order_id].update(quantity=-1),
            lambda raw: raw["positions"][order.order_id].update(mark_at="2026-08-05T09:55:00"),
            lambda raw: raw["positions"][order.order_id].update(original_stop=99),
            lambda raw: raw["positions"][order.order_id].update(initial_risk_per_share=.1),
            lambda raw: raw.update(high_water_mark=200_000),
            lambda raw: raw["exit_policy_states"][order.order_id]["state"].update(stop=99),
            lambda raw: raw["fills"][0].update(quantity=1),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                raw = copy.deepcopy(source)
                mutate(raw)
                with self.assertRaises(ValueError):
                    PortfolioLedger.from_snapshot(raw)
        self.assertEqual(source, PortfolioLedger.from_snapshot(source).snapshot())

    def test_mechanical_stress_has_no_stop_credit_or_probability_claim(self):
        ledger = PortfolioLedger(100_000)
        broker = PaperBroker(ledger)
        self.open(broker, stop=99)
        report = ledger.stress_report()
        self.assertEqual("MECHANICAL_NOT_PROBABILISTIC", report["kind"])
        self.assertFalse(report["stop_fill_credit"])
        self.assertEqual(2500, report["scenarios"]["all_positions_down_10pct"]["loss_dollars"])
        self.assertEqual(5000, report["scenarios"]["all_positions_down_20pct"]["loss_dollars"])
        self.assertEqual(7500, report["scenarios"]["largest_position_down_30pct"]["loss_dollars"])


if __name__ == "__main__":
    unittest.main()
