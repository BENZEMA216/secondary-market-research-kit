"""Causal session engines for the reversal and event branches."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping

from .calendar import TradingSessionCalendar
from .configuration import (
    MINIMUM_FIRST_20M_VOLUME_MULTIPLE,
    MINIMUM_OPENING_GAP,
    OPENING_RANGE_STARTS,
    ORB_SIGNAL_END,
    ORB_SIGNAL_START,
)
from .events import ArticleLedger, EventQualification, InformationRoute, choose_information_route
from .indicators import QuadStochasticDetector
from .models import (
    EXCHANGE_TIMEZONE,
    EntrySignal,
    IntradayBar,
    MacdSetup,
    require_finite_number,
    require_nonempty_string,
)


def _signal_id(branch: str, variant: str, ticker: str, context_id: str, execute_at: datetime) -> str:
    return f"{branch}:{variant}:{ticker}:{context_id}:{execute_at.isoformat()}"


def _require_next_session(calendar: TradingSessionCalendar, setup: MacdSetup, valid_session: date) -> None:
    if not calendar.is_next_session(setup.signal_session, valid_session):
        raise ValueError("reversal candidate is not the next exchange session after the MACD signal")


def macd_open_signal(
    setup: MacdSetup,
    valid_session: date,
    calendar: TradingSessionCalendar,
    opening_price: float,
    entry_ticker: str,
    entry_price_basis_id: str,
    variant: str,
    article_ledger: ArticleLedger | None = None,
) -> EntrySignal | None:
    """Build M0 or M2 at the next normal session open."""

    if variant not in {"M0", "M2"}:
        raise ValueError("open signal variant must be M0 or M2")
    market_open = calendar.get(valid_session).market_open
    signal_known_at = calendar.get(setup.signal_session).market_close
    news_window_start = calendar.news_window_start_for(setup.third_wave_start_session)
    require_finite_number(opening_price, "opening_price", positive=True)
    require_nonempty_string(entry_ticker, "entry_ticker")
    require_nonempty_string(entry_price_basis_id, "entry_price_basis_id")
    _require_next_session(calendar, setup, valid_session)
    if entry_price_basis_id != setup.price_basis_id:
        raise ValueError("MACD setup and entry open use different corporate-action price bases")
    if opening_price <= setup.third_wave_low:
        return None
    if variant == "M2":
        if article_ledger is None:
            raise ValueError("M2 requires a complete point-in-time article ledger")
        query = article_ledger.query(
            setup.security_id, news_window_start, market_open - timedelta(microseconds=1)
        )
        if query.status != "ZERO_ARTICLE":
            return None
        signal_known_at = market_open - timedelta(microseconds=1)
    context_id = f"macd3:{setup.security_id}:{setup.signal_session.isoformat()}"
    return EntrySignal(
        signal_id=_signal_id("reversal", variant, entry_ticker, context_id, market_open),
        ticker=entry_ticker,
        security_id=setup.security_id,
        branch="reversal",
        variant=variant,
        detected_at=signal_known_at,
        execute_at=market_open,
        stop_price=setup.third_wave_low,
        context_id=context_id,
        metadata={
            "experiment_id": f"reversal:{variant}",
            "third_wave_start_session": setup.third_wave_start_session.isoformat(),
            "price_basis_id": entry_price_basis_id,
        },
    )


class ReversalSession:
    """M1/M3/M4: one next-session QuadStochastic opportunity for a MACD setup."""

    def __init__(
        self,
        setup: MacdSetup,
        valid_session: date,
        calendar: TradingSessionCalendar,
        session_price_basis_id: str,
        quad_stochastic: QuadStochasticDetector,
        article_ledger: ArticleLedger | None,
        variant: str = "M4",
    ) -> None:
        if variant not in {"M1", "M3", "M4"}:
            raise ValueError("QuadStochastic reversal variant must be M1, M3, or M4")
        require_nonempty_string(session_price_basis_id, "session_price_basis_id")
        _require_next_session(calendar, setup, valid_session)
        if session_price_basis_id != setup.price_basis_id:
            raise ValueError("MACD setup and intraday session use different corporate-action price bases")
        if isinstance(quad_stochastic, QuadStochasticDetector) and quad_stochastic.calendar is not calendar:
            raise ValueError("QuadStochastic detector and reversal session must share the exact exchange calendar")
        market_open = calendar.get(valid_session).market_open
        news_window_start = calendar.news_window_start_for(setup.third_wave_start_session)
        binding = getattr(quad_stochastic, "_scenario_router_arm_binding", None)
        requested_binding = (setup.security_id, variant)
        if binding is not None and binding != requested_binding:
            raise ValueError(
                "a QuadStochastic detector cannot be shared across securities or experiment arms; "
                "use one detector per security and arm"
            )
        setattr(quad_stochastic, "_scenario_router_arm_binding", requested_binding)
        self.setup = setup
        self.security_id = setup.security_id
        self.session_price_basis_id = session_price_basis_id
        self.valid_session = valid_session
        self.market_open = market_open
        self.news_window_start = news_window_start
        self.quad_stochastic = quad_stochastic
        self.article_ledger = article_ledger
        self.variant = variant
        self.session_schedule = calendar.get(valid_session)
        self.last_bar_start: datetime | None = None
        self.session_ticker: str | None = None
        self.active = True
        self.reason = "ACTIVE"
        if variant in {"M3", "M4"}:
            if article_ledger is None:
                raise ValueError(f"{variant} requires an article ledger")
            snapshot = article_ledger.query(
                self.security_id, news_window_start, market_open - timedelta(microseconds=1)
            )
            if snapshot.status != "ZERO_ARTICLE":
                self.active = False
                self.reason = (
                    "ABSTAIN_MISSING_COVERAGE" if snapshot.status == "COVERAGE_UNKNOWN" else "VETO_ARTICLE_OBSERVED"
                )

    def on_bar(self, bar: IntradayBar) -> EntrySignal | None:
        if bar.security_id != self.security_id or bar.session != self.valid_session:
            return None
        if self.session_ticker is None:
            self.session_ticker = bar.ticker
        elif bar.ticker != self.session_ticker:
            self.active = False
            self.reason = "ABSTAIN_TICKER_CHANGED_WITHIN_SESSION"
            return None
        if bar.price_basis_id != self.session_price_basis_id:
            self.active = False
            self.reason = "ABSTAIN_PRICE_BASIS_MISMATCH"
            return None
        expected_start = (
            self.session_schedule.market_open
            if self.last_bar_start is None
            else self.last_bar_start + timedelta(minutes=5)
        )
        if bar.start != expected_start or bar.end > self.session_schedule.market_close:
            self.active = False
            self.reason = "ABSTAIN_INTRADAY_DATA_GAP_OR_OUTSIDE_SESSION"
            return None
        self.last_bar_start = bar.start
        if not self.active:
            # Warm the indicator even when this particular candidate is inactive.
            self.quad_stochastic.update(bar)
            return None
        if self.variant == "M4":
            assert self.article_ledger is not None
            query = self.article_ledger.query(self.security_id, self.news_window_start, bar.end)
            if query.status != "ZERO_ARTICLE":
                self.active = False
                self.reason = (
                    "ABSTAIN_MISSING_COVERAGE" if query.status == "COVERAGE_UNKNOWN" else "VETO_ARTICLE_OBSERVED"
                )
                self.quad_stochastic.update(bar)
                return None
        if bar.low <= self.setup.third_wave_low:
            self.active = False
            self.reason = "INVALIDATED_MACD_LOW"
            self.quad_stochastic.update(bar)
            return None
        trigger = self.quad_stochastic.update(bar)
        if trigger is None:
            return None
        if (
            trigger.ticker != bar.ticker
            or trigger.detected_at != bar.end
            or trigger.execute_at != bar.end
        ):
            self.active = False
            self.reason = "ABSTAIN_INVALID_QUAD_STOCHASTIC_TRIGGER_TIME_OR_IDENTITY"
            return None
        if trigger.execute_at >= self.session_schedule.market_close:
            self.active = False
            self.reason = "ABSTAIN_NO_NEXT_REGULAR_BAR"
            return None
        self.active = False
        self.reason = "TRIGGERED"
        context_id = f"macd3:{self.setup.security_id}:{self.setup.signal_session.isoformat()}"
        return EntrySignal(
            signal_id=_signal_id("reversal", self.variant, bar.ticker, context_id, trigger.execute_at),
            ticker=bar.ticker,
            security_id=bar.security_id,
            branch="reversal",
            variant=self.variant,
            detected_at=trigger.detected_at,
            execute_at=trigger.execute_at,
            stop_price=self.setup.third_wave_low,
            context_id=context_id,
            metadata={
                "experiment_id": f"reversal:{self.variant}",
                "quad_stochastic_second_test_low": trigger.second_test_low,
                "third_wave_start_session": self.setup.third_wave_start_session.isoformat(),
                "price_basis_id": self.session_price_basis_id,
            },
        )


@dataclass
class _OpeningRange:
    high: float
    low: float
    volume: float


class EventSession:
    """U0/E0/E1/E2 session state; emits separate, non-pooled test arms."""

    OPENING_STARTS = tuple(time.fromisoformat(value) for value in OPENING_RANGE_STARTS)
    ORB_START = time.fromisoformat(ORB_SIGNAL_START)
    ORB_END = time.fromisoformat(ORB_SIGNAL_END)

    def __init__(
        self,
        ticker: str,
        security_id: str,
        session: date,
        calendar: TradingSessionCalendar,
        prior_close: float,
        average_full_day_volume_20: float,
        prior_close_price_basis_id: str,
        session_price_basis_id: str,
        event_qualification: EventQualification,
    ) -> None:
        require_finite_number(prior_close, "prior_close", positive=True)
        require_finite_number(average_full_day_volume_20, "average_full_day_volume_20", positive=True)
        require_nonempty_string(prior_close_price_basis_id, "prior_close_price_basis_id")
        require_nonempty_string(session_price_basis_id, "session_price_basis_id")
        market_open = calendar.get(session).market_open
        previous_close = calendar.previous(session).market_close
        if event_qualification.security_id != security_id:
            raise ValueError("event qualification is bound to another security")
        if event_qualification.window_start != previous_close:
            raise ValueError("event qualification is bound to another prior-close boundary")
        if event_qualification.cutoff != market_open - timedelta(microseconds=1):
            raise ValueError("event qualification is bound to another market-open cutoff")
        self.ticker = ticker
        self.security_id = security_id
        self.session = session
        self.prior_close = prior_close
        self.adv20 = average_full_day_volume_20
        self.prior_close_price_basis_id = prior_close_price_basis_id
        self.session_price_basis_id = session_price_basis_id
        self.event_qualification = event_qualification
        self.session_schedule = calendar.get(session)
        self.opening_bars: dict[time, IntradayBar] = {}
        self.opening_range: _OpeningRange | None = None
        self.u0 = False
        self.triggered = False
        self.failed = False
        self.reason = "COLLECTING_OPENING_RANGE"
        self.last_bar_start: datetime | None = None

    def _finalize_opening_range(self) -> None:
        if tuple(sorted(self.opening_bars)) != self.OPENING_STARTS:
            self.reason = "ABSTAIN_INCOMPLETE_OPENING_RANGE"
            return
        bars = [self.opening_bars[value] for value in self.OPENING_STARTS]
        self.opening_range = _OpeningRange(
            high=max(bar.high for bar in bars),
            low=min(bar.low for bar in bars),
            volume=sum(bar.volume for bar in bars),
        )
        gap = bars[0].open / self.prior_close - 1.0
        if self.prior_close_price_basis_id != self.session_price_basis_id:
            self.u0 = False
            self.reason = "REJECTED_CORPORATE_ACTION_BASIS"
            return
        self.u0 = (
            gap >= MINIMUM_OPENING_GAP
            and self.opening_range.volume >= MINIMUM_FIRST_20M_VOLUME_MULTIPLE * self.adv20
        )
        self.reason = "U0_ELIGIBLE" if self.u0 else "REJECTED_U0"

    def on_bar(self, bar: IntradayBar) -> tuple[EntrySignal, ...]:
        if bar.security_id != self.security_id or bar.session != self.session or self.triggered or self.failed:
            return ()
        if bar.ticker != self.ticker:
            self.failed = True
            self.reason = "ABSTAIN_TICKER_IDENTITY_MISMATCH"
            return ()
        if bar.price_basis_id != self.session_price_basis_id:
            self.failed = True
            self.reason = "ABSTAIN_PRICE_BASIS_MISMATCH"
            return ()
        expected_start = (
            self.session_schedule.market_open
            if self.last_bar_start is None
            else self.last_bar_start + timedelta(minutes=5)
        )
        if bar.start != expected_start or bar.end > self.session_schedule.market_close:
            self.failed = True
            self.reason = "ABSTAIN_INTRADAY_DATA_GAP_OR_OUTSIDE_SESSION"
            return ()
        self.last_bar_start = bar.start
        start_time = bar.exchange_start.time()
        if start_time in self.OPENING_STARTS:
            if start_time in self.opening_bars:
                raise ValueError(f"duplicate opening-range bar: {start_time}")
            self.opening_bars[start_time] = bar
            return ()
        if start_time < self.ORB_START:
            return ()
        if self.opening_range is None:
            self._finalize_opening_range()
        if not self.u0 or self.opening_range is None:
            return ()
        if start_time > self.ORB_END:
            self.reason = "EXPIRED_NO_ORB"
            return ()
        if bar.high <= self.opening_range.high:
            return ()

        self.triggered = True
        self.reason = "ORB_TRIGGERED"
        context_id = (
            f"u0:{self.security_id}:{self.session.isoformat()}:"
            f"{self.event_qualification.mode}"
        )
        variants = ["E0"]
        variants.extend(self.event_qualification.eligible_variants)
        signals = []
        for variant in variants:
            signals.append(EntrySignal(
                signal_id=_signal_id("event", variant, self.ticker, context_id, bar.end),
                ticker=self.ticker,
                security_id=self.security_id,
                branch="event",
                variant=variant,
                detected_at=bar.end,
                execute_at=bar.end,
                stop_price=self.opening_range.low,
                context_id=context_id,
                metadata={
                    "experiment_id": f"event:{self.event_qualification.mode}:{variant}",
                    "opening_range_high": self.opening_range.high,
                    "opening_range_volume": self.opening_range.volume,
                    "event_status": self.event_qualification.status,
                    "event_record_ids": self.event_qualification.event_record_ids,
                    "event_mode": self.event_qualification.mode,
                    "price_basis_id": self.session_price_basis_id,
                },
            ))
        return tuple(signals)


class SignalRouter:
    """Apply a route frozen from pre-entry information, then resolve duplicates.

    The route is required explicitly so a later event breakout can never be
    used with hindsight to cancel an earlier reversal signal.
    """

    @staticmethod
    def select(
        signals: Iterable[EntrySignal],
        *,
        event_variant: str,
        reversal_variant: str,
        qualifications_by_security_id: Mapping[str, EventQualification],
        event_mode: str,
        calendar: TradingSessionCalendar,
    ) -> tuple[EntrySignal, ...]:
        if event_variant not in {"E2A", "E2B"}:
            raise ValueError("the final portfolio router accepts only a qualified E2A or E2B event arm")
        if reversal_variant not in {"M2", "M3", "M4"}:
            raise ValueError("the final portfolio router accepts only a news-gated M2, M3, or M4 reversal arm")
        if event_mode not in {"strict_primary", "agent_assisted_secondary"}:
            raise ValueError("choose one event-data mode")
        unique: dict[str, EntrySignal] = {}
        routes_by_security_id: dict[str, InformationRoute] = {}
        for signal in signals:
            qualification = qualifications_by_security_id.get(signal.security_id)
            if qualification is None or qualification.security_id != signal.security_id:
                raise ValueError(f"missing or invalid event qualification for {signal.security_id}")
            if qualification.mode != event_mode:
                raise ValueError("event qualification mode does not match the selected experiment mode")
            route = choose_information_route(qualification, event_variant)
            routes_by_security_id[signal.security_id] = route
            exchange_execution = signal.execute_at.astimezone(EXCHANGE_TIMEZONE)
            signal_session = exchange_execution.date()
            schedule = calendar.get(signal_session)
            expected_cutoff = schedule.market_open - timedelta(microseconds=1)
            expected_window_start = calendar.previous(signal_session).market_close
            if route.computed_at != expected_cutoff or route.window_start != expected_window_start:
                raise ValueError("information route is stale or bound to another exchange session")
            if route.computed_at >= signal.execute_at:
                raise ValueError("information route was not frozen before candidate execution")
            if route.route == "abstain" or signal.branch != route.route:
                continue
            if signal.branch == "event" and signal.variant != event_variant:
                continue
            if signal.branch == "reversal" and signal.variant != reversal_variant:
                continue
            execution_time = exchange_execution.time()
            on_grid = (
                execution_time.minute % 5 == 0
                and execution_time.second == 0
                and execution_time.microsecond == 0
            )
            if not schedule.market_open <= signal.execute_at < schedule.market_close or not on_grid:
                raise ValueError("candidate execution is outside the exchange session or five-minute grid")
            if signal.branch == "event":
                if not time(9, 55) <= execution_time <= time(11, 0) or signal.detected_at != signal.execute_at:
                    raise ValueError("event candidate is not the immediate next open in the frozen ORB window")
                if (
                    signal.metadata.get("event_mode") != event_mode
                    or signal.metadata.get("event_status") != qualification.status
                    or tuple(signal.metadata.get("event_record_ids", ())) != qualification.event_record_ids
                ):
                    raise ValueError("event signal evidence does not match its frozen qualification")
            elif signal.variant == "M2":
                if signal.execute_at != schedule.market_open:
                    raise ValueError("M2 must execute at the next session open")
            elif signal.execute_at == schedule.market_open or signal.detected_at != signal.execute_at:
                raise ValueError("QuadStochastic candidate must execute immediately after a completed intraday bar")
            existing = unique.get(signal.signal_id)
            if existing is not None and existing != signal:
                raise ValueError(f"conflicting signal_id: {signal.signal_id}")
            unique[signal.signal_id] = signal
        by_ticker: dict[str, list[EntrySignal]] = {}
        for signal in unique.values():
            by_ticker.setdefault(signal.security_id, []).append(signal)
        selected: list[EntrySignal] = []
        for ticker_signals in by_ticker.values():
            selected.append(min(ticker_signals, key=lambda item: (item.execute_at, item.signal_id)))
        experiment_id = f"portfolio:{event_mode}:{event_variant}+{reversal_variant}"
        routed: list[EntrySignal] = []
        for item in selected:
            route = routes_by_security_id[item.security_id]
            qualification = qualifications_by_security_id[item.security_id]
            routed.append(replace(
                item,
                signal_id=f"{item.signal_id}|{experiment_id}",
                context_id=f"{item.context_id}|{experiment_id}",
                metadata={
                    **dict(item.metadata),
                    "experiment_id": experiment_id,
                    "information_route_reason": route.reason,
                    "qualification_status": qualification.status,
                    "qualification_event_record_ids": qualification.event_record_ids,
                },
            ))
        return tuple(sorted(routed, key=lambda item: (item.execute_at, item.ticker, item.signal_id)))
