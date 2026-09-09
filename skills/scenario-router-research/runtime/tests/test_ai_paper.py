from dataclasses import replace
from datetime import timedelta
import unittest

from scenario_router.ai_paper import AIReviewPaperBridge
from scenario_router.paper import PaperBroker
from scenario_router.portfolio import MarketMark, PortfolioLedger
from test_portfolio import signal, quote, NOW, BASIS


class WorkflowStub:
    """Integration plumbing fixture only, never actual model evidence."""
    def __init__(self):
        self.state = {"state": "CHECKED_CANDIDATE", "revision": 1,
                      "run_id": "test-1", "e2b_screen": True}

    def inspect(self, conversation, *, as_of=None):
        return dict(self.state)

    def gate_signal(self, conversation, selected, *, revision, at, events):
        if revision != self.state["revision"] or not self.state["e2b_screen"]:
            raise ValueError("stale fixture")
        return replace(selected, metadata={**dict(selected.metadata),
             "ai_conversation": conversation, "ai_revision": revision,
             "ai_workflow_run_id": self.state["run_id"]})


class AIPaperBridgeTests(unittest.TestCase):
    def setUp(self):
        self.flow = WorkflowStub()
        self.broker = PaperBroker(PortfolioLedger(100000))
        self.bridge = AIReviewPaperBridge(self.flow, "c", self.broker, events=None)

    def reserve(self):
        return self.bridge.submit(replace(signal(), variant="E2B"), quote(),
             revision=1, sector="Tech", cluster="mega", as_of=NOW)

    def test_review_then_real_paper_reservation_and_fill(self):
        order = self.reserve()
        self.assertEqual(order.status, "PENDING")
        self.assertEqual(self.bridge.fill(order.order_id, quote(), as_of=NOW).status, "FILLED")
        self.assertEqual(len(self.broker.ledger.fills), 1)

    def test_correction_cancels_only_unfilled_review_orders(self):
        order = self.reserve()
        self.flow.state.update(state="RUNNING", revision=2, run_id="test-2", e2b_screen=False)
        self.assertEqual(self.bridge.fill(order.order_id, quote(), as_of=NOW).status, "CANCELLED")
        self.assertEqual(self.broker.ledger.reserved_cash, 0)
        self.assertEqual(self.broker.ledger.fills, [])

    def test_on_market_without_signals_cancels_stale_reviews(self):
        order = self.reserve()
        self.flow.state.update(state="BLOCKED", e2b_screen=False)
        ids = self.bridge.on_market([], as_of=NOW)
        self.assertEqual(ids, (order.order_id,))

    def test_correction_does_not_erase_fills_or_disable_protective_exit(self):
        order = self.reserve()
        self.bridge.fill(order.order_id, quote(), as_of=NOW)
        self.flow.state.update(state="BLOCKED", e2b_screen=False)
        later = NOW + timedelta(minutes=5)
        self.bridge.on_market([MarketMark("SID:TEST", 94, later, BASIS)], as_of=later)
        result = self.broker.exit_position(order.order_id, quote(at=later, price=94), as_of=later)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(len(self.broker.ledger.fills), 2)
        self.assertFalse(self.broker.ledger.positions)


if __name__ == "__main__":
    unittest.main()
