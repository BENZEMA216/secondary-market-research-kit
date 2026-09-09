"""Mechanical exits frozen for the two research branches."""

from __future__ import annotations

from .configuration import (
    EVENT_DAY5_REDUCTION_FRACTION,
    EVENT_EMA_EXIT_START_SESSION,
    EVENT_MAX_HOLDING_SESSIONS,
    REVERSAL_MAX_HOLDING_SESSIONS,
    REVERSAL_TARGET_R,
)
from .models import ExitDecision, require_finite_number, require_nonempty_string, require_ohlc


def _require_holding_session(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("holding_session must be a positive exchange-session count")


class ReversalExitPolicy:
    def __init__(self, entry: float, initial_stop: float, price_basis_id: str) -> None:
        require_finite_number(entry, "entry", positive=True)
        require_finite_number(initial_stop, "initial_stop", positive=True)
        if entry <= initial_stop:
            raise ValueError("long entry must be above stop")
        require_nonempty_string(price_basis_id, "price_basis_id")
        self.entry = entry
        self.stop = initial_stop
        self.target = entry + REVERSAL_TARGET_R * (entry - initial_stop)
        self.price_basis_id = price_basis_id
        self.closed = False

    def rebase_price_basis(self, new_price_basis_id: str, price_factor: float) -> None:
        """Apply a split/corporate-action price factor before any post-action bar."""

        require_nonempty_string(new_price_basis_id, "new_price_basis_id")
        require_finite_number(price_factor, "price_factor", positive=True)
        if self.closed:
            raise ValueError("cannot rebase a closed exit policy")
        self.entry *= price_factor
        self.stop *= price_factor
        self.target *= price_factor
        self.price_basis_id = new_price_basis_id


    def on_bar(
        self, open_: float, high: float, low: float, close: float, price_basis_id: str,
        *, holding_session: int, session_close: bool,
    ) -> ExitDecision | None:
        require_ohlc(open_, high, low, close)
        if price_basis_id != self.price_basis_id:
            raise ValueError("exit bar uses a different corporate-action price basis")
        _require_holding_session(holding_session)
        if self.closed:
            return None
        stop_hit = open_ <= self.stop or low <= self.stop
        target_hit = open_ >= self.target or high >= self.target
        if stop_hit:
            self.closed = True
            return ExitDecision("exit", "same_bar_stop_first" if target_hit else "stop", open_ if open_ <= self.stop else self.stop, 1.0)
        if target_hit:
            self.closed = True
            return ExitDecision("exit", "two_r_target", open_ if open_ >= self.target else self.target, 1.0)
        if session_close and holding_session >= REVERSAL_MAX_HOLDING_SESSIONS:
            self.closed = True
            return ExitDecision("exit", "time_30_sessions", close, 1.0)
        return None


class EventExitPolicy:
    def __init__(self, entry: float, opening_range_low: float, price_basis_id: str) -> None:
        require_finite_number(entry, "entry", positive=True)
        require_finite_number(opening_range_low, "opening_range_low", positive=True)
        if entry <= opening_range_low:
            raise ValueError("long entry must be above opening-range low")
        require_nonempty_string(price_basis_id, "price_basis_id")
        self.entry = entry
        self.stop = opening_range_low
        self.price_basis_id = price_basis_id
        self.remaining_fraction = 1.0
        self.pending_ema_exit = False
        self.closed = False

    def rebase_price_basis(self, new_price_basis_id: str, price_factor: float) -> None:
        """Rebase price levels; the adapter must inversely rebase share quantity."""

        require_nonempty_string(new_price_basis_id, "new_price_basis_id")
        require_finite_number(price_factor, "price_factor", positive=True)
        if self.closed:
            raise ValueError("cannot rebase a closed exit policy")
        self.entry *= price_factor
        self.stop *= price_factor
        self.price_basis_id = new_price_basis_id

    def on_session_open(self, open_: float, price_basis_id: str) -> ExitDecision | None:
        require_finite_number(open_, "open", positive=True)
        if price_basis_id != self.price_basis_id:
            raise ValueError("session open uses a different corporate-action price basis")
        if self.closed:
            return None
        if open_ <= self.stop:
            self.closed = True
            fraction = self.remaining_fraction
            self.remaining_fraction = 0.0
            return ExitDecision("exit", "gap_through_stop", open_, fraction)
        if self.pending_ema_exit:
            self.closed = True
            fraction = self.remaining_fraction
            self.remaining_fraction = 0.0
            return ExitDecision("exit", "ema10_next_open", open_, fraction)
        return None

    def on_intraday_bar(
        self, open_: float, high: float, low: float, price_basis_id: str
    ) -> ExitDecision | None:
        require_ohlc(open_, high, low, open_)
        if price_basis_id != self.price_basis_id:
            raise ValueError("intraday exit bar uses a different corporate-action price basis")
        if self.closed:
            return None
        if open_ <= self.stop or low <= self.stop:
            self.closed = True
            fraction = self.remaining_fraction
            self.remaining_fraction = 0.0
            return ExitDecision("exit", "stop", open_ if open_ <= self.stop else self.stop, fraction)
        return None

    def on_session_close(
        self, close: float, ema10: float, price_basis_id: str, *, holding_session: int
    ) -> tuple[ExitDecision, ...]:
        require_finite_number(close, "close", positive=True)
        require_finite_number(ema10, "ema10", positive=True)
        if price_basis_id != self.price_basis_id:
            raise ValueError("session close uses a different corporate-action price basis")
        _require_holding_session(holding_session)
        if self.closed:
            return ()
        decisions: list[ExitDecision] = []
        if holding_session >= EVENT_MAX_HOLDING_SESSIONS:
            self.closed = True
            fraction = self.remaining_fraction
            self.remaining_fraction = 0.0
            return (ExitDecision("exit", "time_60_sessions", close, fraction),)
        if holding_session == EVENT_EMA_EXIT_START_SESSION and self.remaining_fraction == 1.0:
            self.remaining_fraction = 1.0 - EVENT_DAY5_REDUCTION_FRACTION
            self.stop = self.entry
            decisions.append(ExitDecision("reduce", "day5_half", close, EVENT_DAY5_REDUCTION_FRACTION))
        if holding_session >= EVENT_EMA_EXIT_START_SESSION and close < ema10 and self.remaining_fraction > 0:
            self.pending_ema_exit = True
        return tuple(decisions)
