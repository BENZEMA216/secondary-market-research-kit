"""All-or-reject local paper execution; never submits a real order.

Quotes are externally supplied simulation inputs. The engine does not model
queue position, partial fills or market impact. Slippage is EXTRA to bid/ask.
"""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .exits import EventExitPolicy, ReversalExitPolicy
from .models import EntrySignal, require_finite_number, require_nonempty_string, to_jsonable
from .portfolio import EPSILON, MarketMark, PaperOrder, PortfolioLedger, Position
from .portfolio_risk import RiskController
from .risk import EntryQuote


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 0.0
    commission_per_share: float = 0.0
    minimum_commission: float = 0.0

    def __post_init__(self) -> None:
        for name in ("slippage_bps", "commission_per_share", "minimum_commission"):
            require_finite_number(getattr(self, name), name, non_negative=True)
        if self.slippage_bps >= 10_000:
            raise ValueError("slippage must be below 100%")

    def commission(self, quantity: int) -> float:
        return max(self.minimum_commission, quantity * self.commission_per_share) if quantity else 0.0

    def buy_price(self, quote: EntryQuote) -> float:
        return quote.ask_price * (1.0 + self.slippage_bps / 10_000)

    def sell_price(self, quote: EntryQuote, reference_price: float | None = None) -> float:
        # A supplied stop/target reference can make a fill worse, never better.
        reference = quote.bid_price if reference_price is None else min(quote.bid_price, reference_price)
        return reference * (1.0 - self.slippage_bps / 10_000)


@dataclass(frozen=True)
class ExitExecution:
    status: str
    reason: str
    position_id: str
    fills: tuple[dict[str, Any], ...] = ()


def _restore_signal(raw: dict[str, Any]) -> EntrySignal:
    values = copy.deepcopy(raw)
    for key in ("detected_at", "execute_at"):
        values[key] = datetime.fromisoformat(values[key])
    return EntrySignal(**values)


class PaperBroker:
    def __init__(
        self, ledger: PortfolioLedger, risk_controller: RiskController | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self.ledger = ledger
        self.risk = risk_controller or RiskController()
        self.costs = cost_model or CostModel(**(ledger.execution_config or {}))
        with ledger.lock:
            if ledger.execution_config is not None and ledger.execution_config != asdict(self.costs):
                raise ValueError("cost configuration changed during a portfolio replay")
            if ledger.risk_policy_version not in {None, self.risk.policy.version}:
                raise ValueError("risk version changed during a portfolio replay")
            ledger.execution_config = asdict(self.costs)
            ledger.risk_policy_version = self.risk.policy.version

    def mark_to_market(self, marks, as_of: datetime) -> str:
        """Use this runner entry point to latch/cancel immediately after marks."""
        with self.ledger.lock:
            self.ledger.mark_to_market(marks, as_of)
            return self.risk.refresh(self.ledger, as_of)

    def on_market(self, marks, as_of: datetime) -> str:
        """Runner alias: marks alone can halt and cancel pending entries."""
        return self.mark_to_market(marks, as_of)

    @staticmethod
    def context_key(signal: EntrySignal) -> str:
        experiment = signal.metadata.get("experiment_id")
        require_nonempty_string(experiment, "signal.metadata.experiment_id")
        return f"{experiment}|{signal.context_id}"

    def submit(
        self, signal: EntrySignal, quote: EntryQuote, *, sector: str, cluster: str,
        as_of: datetime,
    ) -> PaperOrder:
        ledger = self.ledger
        with ledger.lock:
            context_key = self.context_key(signal)
            if context_key in ledger.contexts:
                return ledger.orders[ledger.contexts[context_key]]
            decision = self.risk.evaluate(
                ledger, signal, quote, sector, cluster, as_of,
                entry_price=self.costs.buy_price(quote), fee_for_quantity=self.costs.commission,
            )
            order_id = "paper-" + hashlib.sha256(
                f"{ledger.portfolio_id}|{context_key}".encode()
            ).hexdigest()[:24]
            order = PaperOrder(
                order_id, context_key, to_jsonable(signal), sector, cluster,
                decision.quantity, decision.entry_price, decision.fee,
                decision.initial_stop_risk, as_of,
                "PENDING" if decision.allowed else "REJECTED", decision.reason,
            )
            ledger.orders[order_id] = order
            ledger.contexts[context_key] = order_id
            if decision.allowed:
                ledger.classifications[signal.security_id] = {
                    "sector": sector, "cluster": cluster,
                    "version": self.risk.policy.cluster_version,
                }
            ledger.record("ORDER_RESERVED" if decision.allowed else "ORDER_REJECTED", as_of,
                          order_id=order_id, context_key=context_key, reason=decision.reason,
                          quantity=decision.quantity, reserved_cash=order.reserved_cash)
            return order

    def fill(self, order_id: str, quote: EntryQuote, *, as_of: datetime) -> PaperOrder:
        ledger = self.ledger
        with ledger.lock:
            order = ledger.orders[order_id]
            if order.status != "PENDING":
                return order
            signal = _restore_signal(order.signal)
            decision = self.risk.evaluate(
                ledger, signal, quote, order.sector, order.cluster, as_of,
                entry_price=self.costs.buy_price(quote), fee_for_quantity=self.costs.commission,
                exclude_order_id=order_id,
            )
            # refresh may have cancelled this reservation under a latched halt.
            if order.status != "PENDING":
                return order
            if not decision.allowed or decision.quantity < order.quantity:
                order.status = "REJECTED"
                order.reason = decision.reason if not decision.allowed else "FILL_EXCEEDS_RESERVED_RISK"
                ledger.record("ORDER_REJECTED", as_of, order_id=order_id, reason=order.reason)
                return order
            quantity, price = order.quantity, decision.entry_price
            fee = self.costs.commission(quantity)
            other_reserved = ledger.reserved_cash - order.reserved_cash
            if quantity * price + fee > ledger.cash - other_reserved + EPSILON:
                order.status, order.reason = "REJECTED", "INSUFFICIENT_CASH_AT_FILL"
                ledger.record("ORDER_REJECTED", as_of, order_id=order_id, reason=order.reason)
                return order
            ledger.cash -= quantity * price + fee
            ledger.fees_paid += fee
            slippage = quantity * (price - quote.ask_price)
            ledger.slippage_paid += slippage
            position = Position(
                order_id, signal.security_id, signal.ticker, order.context_key,
                signal.branch, signal.variant, quote.price_basis_id, order.sector, order.cluster,
                quantity, quantity, price, signal.stop_price, signal.stop_price,
                price - signal.stop_price, as_of, quote.bid_price, as_of,
            )
            ledger.positions[order_id] = position
            order.estimated_entry, order.estimated_fee = price, fee
            order.initial_stop_risk = quantity * (price - signal.stop_price)
            order.status, order.reason, order.filled_at = "FILLED", "ALL_FILLED", as_of
            fill = {"fill_id": f"{order_id}:entry", "order_id": order_id,
                    "position_id": order_id, "side": "buy", "quantity": quantity,
                    "price": price, "fee": fee, "extra_slippage": slippage,
                    "at": as_of.isoformat(), "reason": "ENTRY"}
            ledger.fills.append(fill)
            ledger.record("FILL", as_of, **fill)
            policy = (EventExitPolicy(price, signal.stop_price, quote.price_basis_id)
                      if signal.branch == "event" else
                      ReversalExitPolicy(price, signal.stop_price, quote.price_basis_id))
            self._store_policy(order_id, policy)
            ledger._track_value(as_of, "ENTRY_FILL")
            self.risk.refresh(ledger, as_of)
            return order

    def submit_and_fill(
        self, signal: EntrySignal, quote: EntryQuote, *, sector: str, cluster: str,
        as_of: datetime,
    ) -> PaperOrder:
        with self.ledger.lock:
            order = self.submit(signal, quote, sector=sector, cluster=cluster, as_of=as_of)
            return self.fill(order.order_id, quote, as_of=as_of)

    def _quote_failure(self, position: Position, quote: EntryQuote | None, as_of: datetime) -> str | None:
        if quote is None or quote.timestamp != as_of:
            return "MISSING_OR_STALE_EXIT_QUOTE"
        if quote.security_id != position.security_id or quote.ticker != position.ticker:
            return "EXIT_QUOTE_IDENTITY_MISMATCH"
        if quote.price_basis_id != position.price_basis_id:
            return "EXIT_PRICE_BASIS_MISMATCH"
        return None

    def exit_position(
        self, position_id: str, quote: EntryQuote | None, *, as_of: datetime,
        quantity: int | None = None, reason: str = "MANUAL_PAPER_EXIT",
        reference_price: float | None = None, exit_id: str | None = None,
    ) -> ExitExecution:
        """Risk-reducing fills remain available while HALTED or daily-blocked."""
        ledger = self.ledger
        with ledger.lock:
            action_id = exit_id or f"{position_id}:{as_of.isoformat()}:{reason}:{quantity}"
            if action_id in ledger.exit_action_ids:
                return ExitExecution("FILLED", "ALREADY_APPLIED", position_id,
                                     (copy.deepcopy(ledger.exit_action_ids[action_id]),))
            position = ledger.positions.get(position_id)
            if position is None:
                return ExitExecution("NO_ACTION", "POSITION_CLOSED_OR_UNKNOWN", position_id)
            ledger._begin(as_of)
            failure = self._quote_failure(position, quote, as_of)
            if failure:
                ledger.record("EXIT_REJECTED", as_of, position_id=position_id, reason=failure)
                return ExitExecution("REJECTED", failure, position_id)
            assert quote is not None
            shares = position.quantity if quantity is None else quantity
            if isinstance(shares, bool) or not isinstance(shares, int) or not 0 < shares <= position.quantity:
                raise ValueError("exit quantity must be whole shares within the remaining position")
            if reference_price is not None:
                require_finite_number(reference_price, "reference_price", positive=True)
            price = self.costs.sell_price(quote, reference_price)
            fee = self.costs.commission(shares)
            other_reserved = ledger.reserved_cash
            if ledger.cash + shares * price - fee < other_reserved - EPSILON:
                # Exits outrank entry reservations; release those reservations.
                ledger.cancel_pending(as_of, "RELEASE_CASH_FOR_PROTECTIVE_EXIT")
            if ledger.cash + shares * price - fee < -EPSILON:
                return ExitExecution("REJECTED", "EXIT_FEES_EXCEED_TOTAL_CASH", position_id)
            ledger.cash += shares * price - fee
            ledger.fees_paid += fee
            ledger.realized_gross_pnl += shares * (price - position.entry_price)
            base = quote.bid_price if reference_price is None else min(quote.bid_price, reference_price)
            slippage = shares * (base - price)
            ledger.slippage_paid += slippage
            position.quantity -= shares
            position.mark_price, position.mark_at = quote.bid_price, as_of
            fill = {"fill_id": action_id, "position_id": position_id, "side": "sell",
                    "quantity": shares, "price": price, "fee": fee,
                    "extra_slippage": slippage, "at": as_of.isoformat(), "reason": reason}
            ledger.fills.append(fill)
            ledger.exit_action_ids[action_id] = copy.deepcopy(fill)
            ledger.record("FILL", as_of, **fill)
            if position.quantity == 0:
                del ledger.positions[position_id]
            saved = ledger.exit_policy_states.get(position_id)
            if saved is not None:
                saved["state"]["closed"] = position.quantity == 0
                if saved["class"] == "EventExitPolicy":
                    saved["state"]["remaining_fraction"] = position.quantity / position.initial_quantity
            ledger._track_value(as_of, "EXIT_FILL")
            self.risk.refresh(ledger, as_of)
            return ExitExecution("FILLED", reason, position_id, (copy.deepcopy(fill),))

    def _store_policy(self, position_id: str, policy: EventExitPolicy | ReversalExitPolicy) -> None:
        if not isinstance(policy, (EventExitPolicy, ReversalExitPolicy)):
            raise TypeError("paper execution accepts the frozen event/reversal exit policies")
        policy._paper_position_id = position_id
        self.ledger.exit_policy_states[position_id] = {
            "class": type(policy).__name__, "state": copy.deepcopy(policy.__dict__),
        }

    def get_exit_policy(self, position_id: str) -> EventExitPolicy | ReversalExitPolicy:
        saved = self.ledger.exit_policy_states[position_id]
        state = copy.deepcopy(saved["state"])
        if saved["class"] == "EventExitPolicy":
            policy = EventExitPolicy(state["entry"], state["entry"] * .5, state["price_basis_id"])
        elif saved["class"] == "ReversalExitPolicy":
            policy = ReversalExitPolicy(state["entry"], state["stop"], state["price_basis_id"])
        else:
            raise ValueError("unknown saved exit policy")
        policy.__dict__.update(state)
        return policy

    def propose_and_execute_exit(
        self, position_id: str, policy: EventExitPolicy | ReversalExitPolicy,
        method: str, *args: Any, quote: EntryQuote | None, as_of: datetime,
        **kwargs: Any,
    ) -> ExitExecution:
        """Run a copy, then commit policy mutation only after successful fills.

        Reductions round DOWN from original shares (minimum one share); an exit
        always closes every remaining share. Thus 3 -> 2 -> 0, and a one-share
        position closes at the first reduction rather than creating fractions.
        """
        allowed = {"on_bar", "on_session_open", "on_intraday_bar", "on_session_close"}
        if method not in allowed:
            raise ValueError("not an exit-policy callback")
        ledger = self.ledger
        with ledger.lock:
            position = ledger.positions.get(position_id)
            if position is None:
                return ExitExecution("NO_ACTION", "POSITION_CLOSED_OR_UNKNOWN", position_id)
            saved = ledger.exit_policy_states.get(position_id)
            if (saved is None or getattr(policy, "_paper_position_id", None) != position_id
                    or type(policy).__name__ != saved["class"]
                    or policy.__dict__ != saved["state"]):
                raise ValueError("exit policy is stale or does not belong to the current position state")
            # Persistent ledger state, not the caller's mutable policy, is authoritative.
            candidate = self.get_exit_policy(position_id)
            outcome = getattr(candidate, method)(*args, **kwargs)
            decisions = outcome if isinstance(outcome, tuple) else (() if outcome is None else (outcome,))
            if not decisions:
                ledger._begin(as_of)
                policy.__dict__.update(candidate.__dict__)
                self._store_policy(position_id, policy)
                return ExitExecution("NO_ACTION", "POLICY_STATE_UPDATED", position_id)
            failure = self._quote_failure(position, quote, as_of)
            if failure:
                ledger._begin(as_of)
                ledger.record("EXIT_REJECTED", as_of, position_id=position_id, reason=failure)
                return ExitExecution("REJECTED", failure, position_id)
            # Frozen policies emit at most one action per callback.
            if len(decisions) != 1:
                raise ValueError("multiple exit actions require an explicit atomic fill model")
            decision = decisions[0]
            shares = (position.quantity if decision.action == "exit" else
                      min(position.quantity, max(1, math.floor(position.initial_quantity * decision.fraction))))
            result = self.exit_position(position_id, quote, as_of=as_of, quantity=shares,
                                        reason=decision.reason, reference_price=decision.price,
                                        exit_id=f"{position_id}:{as_of.isoformat()}:{method}:{decision.reason}")
            if result.status != "FILLED":
                return result
            remaining = ledger.positions.get(position_id)
            if isinstance(candidate, EventExitPolicy):
                candidate.remaining_fraction = 0.0 if remaining is None else remaining.quantity / remaining.initial_quantity
            candidate.closed = remaining is None
            policy.__dict__.update(candidate.__dict__)
            if remaining is not None:
                remaining.current_stop = candidate.stop
            self._store_policy(position_id, policy)
            return result
