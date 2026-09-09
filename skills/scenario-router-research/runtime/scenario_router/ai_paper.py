"""One-writer bridge from the AI review overlay to local paper orders.

Use on_market at EVERY replay timestamp, even with no new signal. Corrections
cancel only unfilled AI orders; past fills stay immutable and protective exits
continue in PaperBroker. Never connects an actual broker or relaxes core gates.
"""
from datetime import datetime

from .ai_workflow import AgentWorkflow
from .events import EventLedger
from .models import EntrySignal
from .paper import PaperBroker, _restore_signal
from .portfolio import MarketMark
from .risk import EntryQuote


class AIReviewPaperBridge:
    def __init__(self, workflow: AgentWorkflow, conversation: str,
                 broker: PaperBroker, events: EventLedger):
        self.workflow, self.conversation = workflow, conversation
        self.broker, self.events = broker, events

    def cancel_invalid_pending(self, *, as_of: datetime) -> tuple[str, ...]:
        state = self.workflow.inspect(self.conversation, as_of=as_of.isoformat())
        cancelled = []
        with self.broker.ledger.lock:
            self.broker.ledger._begin(as_of)
            for order in self.broker.ledger.pending_orders:
                metadata = order.signal["metadata"]
                if metadata.get("ai_conversation") != self.conversation:
                    continue
                if (state["state"] != "CHECKED_CANDIDATE" or not state["e2b_screen"]
                        or state["revision"] != metadata.get("ai_revision")
                        or state.get("run_id") != metadata.get("ai_workflow_run_id")):
                    order.status, order.reason = "CANCELLED", "AI_REVIEW_CHANGED_OR_UNAVAILABLE"
                    self.broker.ledger.record("ORDER_CANCELLED", as_of, order_id=order.order_id, reason=order.reason)
                    cancelled.append(order.order_id)
        return tuple(cancelled)

    def on_market(self, marks: list[MarketMark], *, as_of: datetime):
        self.broker.on_market(marks, as_of)
        return self.cancel_invalid_pending(as_of=as_of)

    def submit(self, signal: EntrySignal, quote: EntryQuote, *, revision: int,
               sector: str, cluster: str, as_of: datetime):
        self.cancel_invalid_pending(as_of=as_of)
        reviewed = self.workflow.gate_signal(self.conversation, signal, revision=revision,
                                            at=as_of, events=self.events)
        return self.broker.submit(reviewed, quote, sector=sector, cluster=cluster, as_of=as_of)

    def fill(self, order_id: str, quote: EntryQuote, *, as_of: datetime):
        self.cancel_invalid_pending(as_of=as_of)
        order = self.broker.ledger.orders[order_id]
        if order.status != "PENDING":
            return order
        metadata = order.signal["metadata"]
        if metadata.get("ai_conversation") != self.conversation:
            raise ValueError("order does not belong to this AI review bridge")
        try:
            self.workflow.gate_signal(self.conversation, _restore_signal(order.signal),
                                      revision=metadata["ai_revision"], at=as_of, events=self.events)
        except ValueError:
            with self.broker.ledger.lock:
                order.status, order.reason = "CANCELLED", "AI_REVIEW_RECHECK_FAILED"
                self.broker.ledger.record("ORDER_CANCELLED", as_of, order_id=order.order_id, reason=order.reason)
            return order
        return self.broker.fill(order_id, quote, as_of=as_of)
