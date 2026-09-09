"""Frozen paper risk policy using the portfolio's own marked equity.

Drawdown controls new risk only. They never pretend that stop levels guarantee
loss limits, and they never disable protective exits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .models import EntrySignal, require_nonempty_string
from .portfolio import EPSILON, PortfolioLedger
from .risk import EntryQuote


@dataclass(frozen=True)
class RiskPolicy:
    version: str = "paper-risk-0.2.0"
    cluster_version: str = "paper-clusters-2026-09-03-v1"
    risk_per_trade: float = 0.0025
    maximum_positions: int = 4
    maximum_stop_risk: float = 0.01
    maximum_gross: float = 1.0
    maximum_name: float = 0.25
    maximum_sector: float = 0.35
    maximum_cluster_stop_risk: float = 0.005
    reduce_at_drawdown: float = 0.05
    halt_at_drawdown: float = 0.10
    daily_loss_limit: float = 0.01

    def __post_init__(self) -> None:
        frozen = ("paper-risk-0.2.0", "paper-clusters-2026-09-03-v1", .0025, 4, .01,
                  1.0, .25, .35, .005, .05, .10, .01)
        actual = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if actual != frozen:
            raise ValueError("paper risk policy is frozen; a change needs a new implementation version")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: int = 0
    entry_price: float = 0.0
    fee: float = 0.0
    initial_stop_risk: float = 0.0
    state: str = "NORMAL"


class RiskController:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy()

    def refresh(self, ledger: PortfolioLedger, as_of: datetime) -> str:
        with ledger.lock:
            ledger._begin(as_of)
            if ledger.maximum_risk_drawdown + 1e-12 >= self.policy.halt_at_drawdown or ledger.equity <= 0:
                if not ledger.halted:
                    ledger.halted = True
                    ledger.halt_reason = "DRAWDOWN_HALT"
                    ledger.record("RISK_HALTED", as_of, drawdown=ledger.risk_drawdown)
                ledger.cancel_pending(as_of, "DRAWDOWN_HALT")
            if ledger.maximum_daily_loss + 1e-12 >= self.policy.daily_loss_limit:
                if ledger.daily_blocked_session != ledger.current_session:
                    ledger.daily_blocked_session = ledger.current_session
                    ledger.record("DAILY_ENTRY_BLOCK", as_of, daily_loss=ledger.daily_loss)
                ledger.cancel_pending(as_of, "DAILY_LOSS_LIMIT")
            if ledger.halted:
                return "HALTED"
            if ledger.daily_blocked_session == ledger.current_session:
                return "DAILY_BLOCKED"
            if ledger.risk_drawdown + 1e-12 >= self.policy.reduce_at_drawdown:
                return "REDUCED"
            return "NORMAL"

    def reset_halt(
        self, ledger: PortfolioLedger, *, operator: str, reason: str, as_of: datetime
    ) -> None:
        """Explicit local reset, not an LLM action; historical MDD stays intact.

        The new risk reference is current marked equity. This is recorded so a
        replay cannot disguise the reset as a recovery to the historic peak.
        The current session's daily block is deliberately NOT cleared.
        """
        require_nonempty_string(operator, "operator")
        require_nonempty_string(reason, "reason")
        with ledger.lock:
            ledger._begin(as_of)
            if ledger.equity <= 0 or any(p.mark_at != as_of for p in ledger.positions.values()):
                raise ValueError("reset needs positive equity and current marks")
            old_peak = ledger.risk_high_water_mark
            ledger.halted, ledger.halt_reason = False, None
            ledger.risk_high_water_mark = ledger.equity
            ledger.maximum_risk_drawdown = 0.0
            ledger.record("MANUAL_RISK_RESET", as_of, operator=operator, reason=reason,
                          previous_risk_peak=old_peak, new_risk_peak=ledger.equity,
                          preserved_maximum_drawdown=ledger.maximum_drawdown)

    def evaluate(
        self, ledger: PortfolioLedger, signal: EntrySignal, quote: EntryQuote,
        sector: str, cluster: str, as_of: datetime, *,
        entry_price: float | None = None,
        fee_for_quantity: Callable[[int], float] | None = None,
        exclude_order_id: str | None = None,
    ) -> RiskDecision:
        with ledger.lock:
            state = self.refresh(ledger, as_of)
            def reject(reason: str) -> RiskDecision:
                return RiskDecision(False, reason, state=state)
            if state in {"HALTED", "DAILY_BLOCKED"}:
                return reject(state)
            if not sector or not cluster or any(
                value.strip().casefold() in {"unknown", "unclassified", "none", "n/a", ""}
                for value in (sector, cluster)
            ):
                return reject("UNKNOWN_SECTOR_OR_CLUSTER")
            previous_classification = ledger.classifications.get(signal.security_id)
            expected_classification = {"sector": sector, "cluster": cluster,
                                       "version": self.policy.cluster_version}
            if previous_classification is not None and previous_classification != expected_classification:
                return reject("CLASSIFICATION_VERSION_OR_MAPPING_CHANGED")
            if quote.timestamp != as_of or signal.execute_at != as_of:
                return reject("STALE_OR_WRONG_TIME_QUOTE")
            if quote.security_id != signal.security_id or quote.ticker != signal.ticker:
                return reject("QUOTE_IDENTITY_MISMATCH")
            if quote.price_basis_id != signal.metadata.get("price_basis_id"):
                return reject("QUOTE_PRICE_BASIS_MISMATCH")
            if quote.security_classification != "primary_common_stock" or quote.prior_close < 5:
                return reject("UNIVERSE_REJECTED")
            if quote.median_dollar_volume_20 < 20_000_000 or quote.spread_bps > 30:
                return reject("LIQUIDITY_OR_SPREAD_REJECTED")
            if any(p.mark_at != as_of for p in ledger.positions.values()):
                return reject("STALE_PORTFOLIO_MARKS")
            entry = quote.ask_price if entry_price is None else entry_price
            if not math.isfinite(entry) or entry <= signal.stop_price or quote.next_bar_open <= signal.stop_price:
                return reject("GAP_THROUGH_STOP")
            positions = tuple(ledger.positions.values())
            pending = tuple(o for o in ledger.pending_orders if o.order_id != exclude_order_id)
            occupied = {p.security_id for p in positions} | {o.signal["security_id"] for o in pending}
            if signal.security_id in occupied:
                return reject("SECURITY_ALREADY_OCCUPIED")
            if len(positions) + len(pending) >= self.policy.maximum_positions:
                return reject("POSITION_LIMIT")
            equity = ledger.equity
            stop_risk_used = sum(p.initial_stop_risk for p in positions) + sum(o.initial_stop_risk for o in pending)
            cluster_risk = sum(p.initial_stop_risk for p in positions if p.cluster == cluster) + sum(
                o.initial_stop_risk for o in pending if o.cluster == cluster)
            gross_used = sum(p.gross_dollars for p in positions) + sum(o.gross_dollars for o in pending)
            sector_used = sum(p.gross_dollars for p in positions if p.sector == sector) + sum(
                o.gross_dollars for o in pending if o.sector == sector)
            multiplier = .5 if state == "REDUCED" else 1.0
            risk_room = min(self.policy.risk_per_trade * multiplier * equity,
                            self.policy.maximum_stop_risk * equity - stop_risk_used,
                            self.policy.maximum_cluster_stop_risk * equity - cluster_risk)
            gross_room = min(self.policy.maximum_gross * equity - gross_used,
                             self.policy.maximum_name * equity,
                             self.policy.maximum_sector * equity - sector_used)
            cash_room = ledger.cash - sum(o.reserved_cash for o in pending)
            per_share_risk = entry - signal.stop_price
            if min(risk_room, gross_room, cash_room) <= EPSILON:
                return reject("RISK_EXPOSURE_OR_CASH_LIMIT")
            quantity = min(math.floor((risk_room + EPSILON) / per_share_risk),
                           math.floor((gross_room + EPSILON) / entry),
                           math.floor((cash_room + EPSILON) / entry))
            fee_fn = fee_for_quantity or (lambda _: 0.0)
            # Binary search avoids a shares-sized loop when minimum fees bind.
            low, high = 0, quantity
            while low < high:
                middle = (low + high + 1) // 2
                if middle * entry + fee_fn(middle) <= cash_room + EPSILON:
                    low = middle
                else:
                    high = middle - 1
            quantity = low
            if quantity <= 0:
                return reject("NO_WHOLE_SHARE_CAPACITY")
            return RiskDecision(True, "ACCEPTED", quantity, entry, fee_fn(quantity),
                                quantity * per_share_risk, state)
