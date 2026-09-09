"""Persistent, single-process paper portfolio. No broker/account integration.

Money is represented by finite floats; reconciliations use a small tolerance.
Positions are whole shares. Initial stop risk is retained per remaining share,
even after a stop is tightened; this conservative budget is not a loss limit.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import EntrySignal, EXCHANGE_TIMEZONE, require_aware, require_finite_number, require_nonempty_string


EPSILON = 1e-8


@dataclass(frozen=True)
class MarketMark:
    security_id: str
    price: float
    timestamp: datetime
    price_basis_id: str

    def __post_init__(self) -> None:
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.price_basis_id, "price_basis_id")
        require_finite_number(self.price, "mark price", positive=True)
        require_aware(self.timestamp, "mark timestamp")


@dataclass
class Position:
    position_id: str
    security_id: str
    ticker: str
    context_key: str
    branch: str
    variant: str
    price_basis_id: str
    sector: str
    cluster: str
    quantity: int
    initial_quantity: int
    entry_price: float
    original_stop: float
    current_stop: float
    initial_risk_per_share: float
    opened_at: datetime
    mark_price: float
    mark_at: datetime

    @property
    def gross_dollars(self) -> float:
        return self.quantity * self.mark_price

    @property
    def initial_stop_risk(self) -> float:
        return self.quantity * self.initial_risk_per_share


@dataclass
class PaperOrder:
    order_id: str
    context_key: str
    signal: dict[str, Any]
    sector: str
    cluster: str
    quantity: int
    estimated_entry: float
    estimated_fee: float
    initial_stop_risk: float
    submitted_at: datetime
    status: str = "PENDING"
    reason: str = "RESERVED"
    filled_at: datetime | None = None

    @property
    def reserved_cash(self) -> float:
        return self.quantity * self.estimated_entry + self.estimated_fee

    @property
    def gross_dollars(self) -> float:
        return self.quantity * self.estimated_entry


class PortfolioLedger:
    """Cash, positions, reservations and audit history share one restart state.

    The RLock coordinates callers in this process. A snapshot is an atomic file
    replacement, not a multi-process database; use one writer per portfolio.
    """

    SNAPSHOT_VERSION = "paper-portfolio-0.2.0"

    def __init__(self, initial_cash: float, portfolio_id: str = "paper") -> None:
        require_finite_number(initial_cash, "initial_cash", positive=True)
        require_nonempty_string(portfolio_id, "portfolio_id")
        self.initial_cash = float(initial_cash)
        self.portfolio_id = portfolio_id
        self.cash = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.contexts: dict[str, str] = {}
        self.classifications: dict[str, dict[str, str]] = {}
        self.exit_policy_states: dict[str, dict[str, Any]] = {}
        self.exit_action_ids: dict[str, dict[str, Any]] = {}
        self.execution_config: dict[str, Any] | None = None
        self.risk_policy_version: str | None = None
        self.fills: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.realized_gross_pnl = 0.0
        self.fees_paid = 0.0
        self.slippage_paid = 0.0
        self.high_water_mark = float(initial_cash)
        self.risk_high_water_mark = float(initial_cash)
        self.maximum_drawdown = 0.0
        self.maximum_risk_drawdown = 0.0
        self.halted = False
        self.halt_reason: str | None = None
        self.daily_blocked_session: str | None = None
        self.current_session: str | None = None
        self.session_start_equity = float(initial_cash)
        self.maximum_daily_loss = 0.0
        self.last_event_at: datetime | None = None
        self.lock = threading.RLock()

    @property
    def pending_orders(self) -> tuple[PaperOrder, ...]:
        return tuple(order for order in self.orders.values() if order.status == "PENDING")

    @property
    def reserved_cash(self) -> float:
        return sum(order.reserved_cash for order in self.pending_orders)

    @property
    def available_cash(self) -> float:
        return self.cash - self.reserved_cash

    @property
    def equity(self) -> float:
        return self.cash + sum(position.gross_dollars for position in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.quantity * (p.mark_price - p.entry_price) for p in self.positions.values())

    @property
    def net_pnl(self) -> float:
        return self.equity - self.initial_cash

    @property
    def drawdown(self) -> float:
        return max(0.0, 1.0 - self.equity / self.high_water_mark)

    @property
    def risk_drawdown(self) -> float:
        return max(0.0, 1.0 - self.equity / self.risk_high_water_mark)

    @property
    def daily_loss(self) -> float:
        return max(0.0, 1.0 - self.equity / self.session_start_equity)

    def _begin(self, as_of: datetime) -> None:
        require_aware(as_of, "as_of")
        if self.last_event_at is not None and as_of < self.last_event_at:
            raise ValueError("portfolio events must be chronological")
        session = as_of.astimezone(EXCHANGE_TIMEZONE).date().isoformat()
        if session != self.current_session:
            # Called BEFORE the first new-session mark: overnight losses count.
            self.session_start_equity = self.equity
            self.current_session = session
            self.maximum_daily_loss = 0.0
        self.last_event_at = as_of

    def record(self, kind: str, as_of: datetime, **details: Any) -> None:
        self.events.append({"sequence": len(self.events) + 1, "at": as_of.isoformat(),
                            "kind": kind, **copy.deepcopy(details)})

    def _track_value(self, as_of: datetime, reason: str) -> None:
        equity = self.equity
        self.high_water_mark = max(self.high_water_mark, equity)
        self.risk_high_water_mark = max(self.risk_high_water_mark, equity)
        self.maximum_drawdown = max(self.maximum_drawdown, self.drawdown)
        self.maximum_risk_drawdown = max(self.maximum_risk_drawdown, self.risk_drawdown)
        self.maximum_daily_loss = max(self.maximum_daily_loss, self.daily_loss)
        self.equity_curve.append({
            "at": as_of.isoformat(), "reason": reason, "cash": self.cash,
            "equity": equity, "gross_exposure_dollars": equity - self.cash,
            "fees_paid": self.fees_paid, "drawdown": self.drawdown,
            "maximum_drawdown": self.maximum_drawdown,
        })
        expected = self.initial_cash + self.realized_gross_pnl + self.unrealized_pnl - self.fees_paid
        if abs(equity - expected) > max(EPSILON, abs(equity) * 1e-10):
            raise AssertionError("portfolio cash/P&L reconciliation failed")

    def mark_to_market(
        self, marks: Iterable[MarketMark] | Mapping[str, MarketMark], as_of: datetime
    ) -> None:
        with self.lock:
            items = tuple(marks.values()) if isinstance(marks, Mapping) else tuple(marks)
            by_id = {mark.security_id: mark for mark in items}
            if len(by_id) != len(items):
                raise ValueError("duplicate market mark")
            for position in self.positions.values():
                mark = by_id.get(position.security_id)
                if mark is None or mark.timestamp != as_of:
                    raise ValueError("all open positions need synchronous marks")
                if mark.price_basis_id != position.price_basis_id:
                    raise ValueError("market mark price basis mismatch")
            self._begin(as_of)
            for position in self.positions.values():
                mark = by_id[position.security_id]
                position.mark_price, position.mark_at = mark.price, mark.timestamp
            self._track_value(as_of, "MARK")

    def cancel_pending(self, as_of: datetime, reason: str) -> tuple[str, ...]:
        cancelled = []
        for order in self.pending_orders:
            order.status, order.reason = "CANCELLED", reason
            cancelled.append(order.order_id)
            self.record("ORDER_CANCELLED", as_of, order_id=order.order_id, reason=reason)
        return tuple(cancelled)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = {name: copy.deepcopy(value) for name, value in self.__dict__.items()
                      if name not in {"lock", "positions", "orders"}}
            result["snapshot_version"] = self.SNAPSHOT_VERSION
            result["positions"] = {key: asdict(value) for key, value in self.positions.items()}
            result["orders"] = {key: asdict(value) for key, value in self.orders.items()}
            return json.loads(json.dumps(result, default=lambda value: value.isoformat()))

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "PortfolioLedger":
        raw = copy.deepcopy(dict(snapshot))
        if raw.pop("snapshot_version", None) != cls.SNAPSHOT_VERSION:
            raise ValueError("unsupported portfolio snapshot version")
        if not {"initial_cash", "portfolio_id"}.issubset(raw):
            raise ValueError("snapshot initial identity is missing")
        ledger = cls(raw["initial_cash"], raw["portfolio_id"])
        if set(raw) != set(ledger.__dict__) - {"lock"}:
            raise ValueError("portfolio snapshot has missing or unknown fields")
        def finite_tree(value: Any) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("snapshot contains a non-finite number")
            if isinstance(value, dict):
                for child in value.values():
                    finite_tree(child)
            elif isinstance(value, list):
                for child in value:
                    finite_tree(child)
        finite_tree(raw)
        positions, orders = raw.pop("positions"), raw.pop("orders")
        if raw.get("last_event_at") is not None:
            raw["last_event_at"] = datetime.fromisoformat(raw["last_event_at"])
        ledger.__dict__.update(raw)
        for key, value in positions.items():
            if set(value) != {field.name for field in fields(Position)}:
                raise ValueError("snapshot position has missing or unknown fields")
            for field in ("opened_at", "mark_at"):
                value[field] = datetime.fromisoformat(value[field])
            ledger.positions[key] = Position(**value)
        for key, value in orders.items():
            if set(value) != {field.name for field in fields(PaperOrder)}:
                raise ValueError("snapshot order has missing or unknown fields")
            value["submitted_at"] = datetime.fromisoformat(value["submitted_at"])
            if value["filled_at"] is not None:
                value["filled_at"] = datetime.fromisoformat(value["filled_at"])
            ledger.orders[key] = PaperOrder(**value)
        ledger._validate_restored()
        return ledger

    def _validate_restored(self) -> None:
        """Finite accounting/identity checks, not authentication of external data."""
        def close(left: float, right: float) -> bool:
            return abs(left - right) <= max(EPSILON, max(abs(left), abs(right)) * 1e-10)
        def instant(value: Any) -> datetime:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            require_aware(parsed, "snapshot timestamp")
            if self.last_event_at is not None and parsed > self.last_event_at:
                raise ValueError("snapshot contains a future event")
            return parsed
        def shares(value: Any, allow_zero: bool = False) -> None:
            if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
                raise ValueError("snapshot share quantities must be non-negative whole shares")
        for name in ("cash", "fees_paid", "slippage_paid", "high_water_mark", "risk_high_water_mark",
                     "session_start_equity", "maximum_drawdown", "maximum_risk_drawdown", "maximum_daily_loss"):
            require_finite_number(getattr(self, name), name, non_negative=True)
        require_finite_number(self.realized_gross_pnl, "realized_gross_pnl")
        if self.high_water_mark <= 0 or self.risk_high_water_mark <= 0 or self.session_start_equity <= 0:
            raise ValueError("snapshot equity reference must be positive")
        if type(self.halted) is not bool or (self.halted and not self.halt_reason):
            raise ValueError("snapshot halt state is inconsistent")
        if self.last_event_at is not None:
            instant(self.last_event_at)
            if self.current_session != self.last_event_at.astimezone(EXCHANGE_TIMEZONE).date().isoformat():
                raise ValueError("snapshot session does not match its clock")
        elif self.orders or self.positions or self.fills or self.events or self.equity_curve:
            raise ValueError("active snapshot has no clock")
        if self.cash < -EPSILON or self.available_cash < -EPSILON:
            raise ValueError("snapshot cash or reservations are inconsistent")
        if set(self.contexts.values()) != set(self.orders) or len(self.contexts) != len(self.orders):
            raise ValueError("snapshot context registry does not cover its orders exactly")
        for order_id, order in self.orders.items():
            if order.order_id != order_id or self.contexts.get(order.context_key) != order_id:
                raise ValueError("snapshot order/context identity mismatch")
            expected_id = "paper-" + hashlib.sha256(f"{self.portfolio_id}|{order.context_key}".encode()).hexdigest()[:24]
            if order_id != expected_id:
                raise ValueError("snapshot order ID does not match its context")
            if order.status not in {"PENDING", "FILLED", "REJECTED", "CANCELLED"}:
                raise ValueError("invalid snapshot order state")
            shares(order.quantity, allow_zero=order.status == "REJECTED")
            instant(order.submitted_at)
            if order.filled_at is not None:
                if instant(order.filled_at) < order.submitted_at or order.status != "FILLED":
                    raise ValueError("invalid snapshot fill time")
            elif order.status == "FILLED":
                raise ValueError("filled snapshot order has no fill time")
            raw_signal = copy.deepcopy(order.signal)
            if set(raw_signal) != {field.name for field in fields(EntrySignal)}:
                raise ValueError("snapshot signal has missing or unknown fields")
            for name in ("detected_at", "execute_at"):
                raw_signal[name] = datetime.fromisoformat(raw_signal[name])
                require_aware(raw_signal[name], f"signal {name}")
            restored_signal = EntrySignal(**raw_signal)
            experiment = restored_signal.metadata.get("experiment_id")
            require_nonempty_string(experiment, "experiment_id")
            if order.context_key != f"{experiment}|{restored_signal.context_id}":
                raise ValueError("snapshot signal does not match its context")
            for name in ("estimated_entry", "estimated_fee", "initial_stop_risk"):
                require_finite_number(getattr(order, name), name, non_negative=True)
            if order.status in {"PENDING", "FILLED", "CANCELLED"}:
                if restored_signal.execute_at != order.submitted_at:
                    raise ValueError("snapshot order execution time changed")
                if order.estimated_entry <= restored_signal.stop_price or not close(
                    order.initial_stop_risk, order.quantity * (order.estimated_entry - restored_signal.stop_price)
                ):
                    raise ValueError("snapshot reservation stop risk is inconsistent")
                identity = self.classifications.get(restored_signal.security_id)
                if identity != {"sector": order.sector, "cluster": order.cluster,
                                "version": "paper-clusters-2026-09-03-v1"}:
                    raise ValueError("snapshot classification binding is inconsistent")
        quantities: dict[str, int] = {}
        entry_prices: dict[str, float] = {}
        cash, fees, slippage, realized = self.initial_cash, 0.0, 0.0, 0.0
        fill_ids: set[str] = set()
        previous_time: datetime | None = None
        for fill in self.fills:
            if fill["fill_id"] in fill_ids or fill["side"] not in {"buy", "sell"}:
                raise ValueError("snapshot contains duplicate or invalid fills")
            fill_ids.add(fill["fill_id"])
            when = instant(fill["at"])
            if previous_time is not None and when < previous_time:
                raise ValueError("snapshot fills are not chronological")
            previous_time = when
            shares(fill["quantity"])
            require_finite_number(fill["price"], "fill price", positive=True)
            require_finite_number(fill["fee"], "fill fee", non_negative=True)
            require_finite_number(fill["extra_slippage"], "fill slippage", non_negative=True)
            position_id, quantity, price = fill["position_id"], fill["quantity"], fill["price"]
            order = self.orders.get(position_id)
            if order is None or order.status != "FILLED":
                raise ValueError("snapshot fill has no filled entry order")
            if fill["side"] == "buy":
                if position_id in entry_prices or quantity != order.quantity or not close(price, order.estimated_entry):
                    raise ValueError("snapshot entry fill does not match its order")
                quantities[position_id], entry_prices[position_id] = quantity, price
                cash -= quantity * price
            else:
                if quantities.get(position_id, 0) < quantity:
                    raise ValueError("snapshot sells more shares than it owns")
                quantities[position_id] -= quantity
                cash += quantity * price
                realized += quantity * (price - entry_prices[position_id])
                if self.exit_action_ids.get(fill["fill_id"]) != fill:
                    raise ValueError("snapshot exit idempotency registry is inconsistent")
            cash -= fill["fee"]
            fees += fill["fee"]
            slippage += fill["extra_slippage"]
        if any(not close(a, b) for a, b in ((cash, self.cash), (fees, self.fees_paid),
                                          (slippage, self.slippage_paid), (realized, self.realized_gross_pnl))):
            raise ValueError("snapshot fill-ledger accounting does not reconcile")
        if set(self.positions) != {key for key, quantity in quantities.items() if quantity > 0}:
            raise ValueError("snapshot holdings do not reconcile with fills")
        if set(self.exit_action_ids) != {fill["fill_id"] for fill in self.fills if fill["side"] == "sell"}:
            raise ValueError("snapshot has unknown exit idempotency entries")
        for position_id, position in self.positions.items():
            order = self.orders[position_id]
            shares(position.quantity)
            shares(position.initial_quantity)
            if (position.position_id != position_id or position.quantity != quantities[position_id]
                    or position.initial_quantity != order.quantity or position.context_key != order.context_key
                    or position.security_id != order.signal["security_id"] or position.ticker != order.signal["ticker"]
                    or position.price_basis_id != order.signal["metadata"]["price_basis_id"]
                    or position.sector != order.sector or position.cluster != order.cluster
                    or position.branch != order.signal["branch"] or position.variant != order.signal["variant"]):
                raise ValueError("snapshot position identity or quantities are inconsistent")
            for name in ("entry_price", "original_stop", "current_stop", "initial_risk_per_share", "mark_price"):
                require_finite_number(getattr(position, name), name, positive=True)
            if (not close(position.entry_price, order.estimated_entry)
                    or not close(position.original_stop, order.signal["stop_price"])
                    or not close(position.initial_risk_per_share, position.entry_price - position.original_stop)):
                raise ValueError("snapshot position stop/risk was changed")
            if instant(position.mark_at) < instant(position.opened_at) or position.opened_at != order.filled_at:
                raise ValueError("snapshot position times are inconsistent")
        for position_id, saved in self.exit_policy_states.items():
            order = self.orders.get(position_id)
            if order is None or order.status != "FILLED" or set(saved) != {"class", "state"}:
                raise ValueError("snapshot exit policy is not bound to an entry")
            state = saved["state"]
            event = order.signal["branch"] == "event"
            expected_keys = ({"entry", "stop", "price_basis_id", "remaining_fraction", "pending_ema_exit", "closed", "_paper_position_id"}
                             if event else {"entry", "stop", "target", "price_basis_id", "closed", "_paper_position_id"})
            if saved["class"] != ("EventExitPolicy" if event else "ReversalExitPolicy") or set(state) != expected_keys:
                raise ValueError("snapshot exit policy class/fields changed")
            if (type(state["closed"]) is not bool or state["closed"] != (quantities.get(position_id, 0) == 0)
                    or state["_paper_position_id"] != position_id
                    or state["entry"] != order.estimated_entry
                    or state["price_basis_id"] != order.signal["metadata"]["price_basis_id"]
                    or state["stop"] not in {order.signal["stop_price"], order.estimated_entry}):
                raise ValueError("snapshot exit policy stop/identity/state mismatch")
            if event:
                if type(state["pending_ema_exit"]) is not bool or not close(
                    state["remaining_fraction"], quantities.get(position_id, 0) / order.quantity):
                    raise ValueError("snapshot event exit remaining shares mismatch")
            elif state["stop"] != order.signal["stop_price"] or not close(
                state["target"], state["entry"] + 2 * (state["entry"] - state["stop"])):
                raise ValueError("snapshot reversal target changed")
            if position_id in self.positions and self.positions[position_id].current_stop != state["stop"]:
                raise ValueError("snapshot policy and position stops disagree")
        if set(self.exit_policy_states) != set(entry_prices):
            raise ValueError("snapshot is missing an exit policy")
        peak, maximum_dd = self.initial_cash, 0.0
        for item in self.equity_curve:
            instant(item["at"])
            require_finite_number(item["equity"], "equity_curve equity", non_negative=True)
            peak = max(peak, item["equity"])
            dd = max(0.0, 1 - item["equity"] / peak)
            maximum_dd = max(maximum_dd, dd)
            if not close(item["drawdown"], dd) or not close(item["maximum_drawdown"], maximum_dd):
                raise ValueError("snapshot historical drawdown path was changed")
        if not close(self.high_water_mark, peak) or not close(self.maximum_drawdown, maximum_dd):
            raise ValueError("snapshot HWM/MDD does not match equity history")
        if self.equity_curve and not close(self.equity_curve[-1]["equity"], self.equity):
            raise ValueError("snapshot final marked equity does not match its curve")
        if (self.risk_high_water_mark + EPSILON < self.equity or self.risk_high_water_mark > peak + EPSILON
                or self.maximum_risk_drawdown > self.maximum_drawdown + EPSILON
                or self.maximum_risk_drawdown + EPSILON < self.risk_drawdown
                or self.maximum_daily_loss + EPSILON < self.daily_loss
                or max(self.maximum_daily_loss, self.maximum_drawdown, self.maximum_risk_drawdown) > 1):
            raise ValueError("snapshot risk reference is inconsistent")
        for sequence, event in enumerate(self.events, 1):
            if event.get("sequence") != sequence:
                raise ValueError("snapshot audit sequence is inconsistent")
            instant(event["at"])
            if event["kind"] == "MANUAL_RISK_RESET":
                require_nonempty_string(event.get("operator"), "reset operator")
                require_nonempty_string(event.get("reason"), "reset reason")
        if self.risk_high_water_mark < self.high_water_mark - EPSILON and not any(
            event["kind"] == "MANUAL_RISK_RESET" for event in self.events
        ):
            raise ValueError("snapshot risk high-water reference was reset without an audit event")

    def stress_report(self) -> dict[str, Any]:
        """Mechanical shocks only: no probability, stop-fill or hedge credit."""
        with self.lock:
            gross = sum(p.gross_dollars for p in self.positions.values())
            largest = max((p.gross_dollars for p in self.positions.values()), default=0.0)
            scenarios = {}
            for name, loss in (("all_positions_down_10pct", .10 * gross),
                               ("all_positions_down_20pct", .20 * gross),
                               ("largest_position_down_30pct", .30 * largest)):
                scenarios[name] = {"loss_dollars": loss, "projected_equity": self.equity - loss,
                                   "projected_drawdown_from_historical_peak": max(0.0, 1.0 - (self.equity - loss) / self.high_water_mark),
                                   "loss_fraction_of_equity": loss / self.equity if self.equity > 0 else None}
            return {"kind": "MECHANICAL_NOT_PROBABILISTIC", "stop_fill_credit": False,
                    "as_of": None if self.last_event_at is None else self.last_event_at.isoformat(),
                    "equity": self.equity, "gross_dollars": gross,
                    "pending_notional_not_yet_held": sum(o.gross_dollars for o in self.pending_orders),
                    "scenarios": scenarios}

    def save(self, path: str | Path) -> None:
        """Atomically save a local snapshot; parent directory must already exist."""
        with self.lock:
            destination = Path(path)
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
            descriptor, temporary = tempfile.mkstemp(prefix=".paper-snapshot-", dir=destination.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    @classmethod
    def load(cls, path: str | Path) -> "PortfolioLedger":
        return cls.from_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))
