"""Typed records shared by the deterministic scenario-router core.

The core deliberately has no broker, network, or LLM dependency.  A backtest
adapter supplies point-in-time bars and event records; the core only returns
signals and order intents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo


EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")


def require_finite_number(value: float, field_name: str, *, positive: bool = False, non_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be positive")
    if non_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def require_nonempty_string(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON-like data used by immutable strategy records."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value


def to_jsonable(value: Any) -> Any:
    """Serialize frozen strategy records without relying on ``asdict``/pickle."""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def require_ohlc(open_: float, high: float, low: float, close: float) -> None:
    for field_name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        require_finite_number(value, field_name, positive=True)
    tolerance = max(open_, high, low, close) * 1e-12
    if high + tolerance < max(open_, close) or low - tolerance > min(open_, close) or high + tolerance < low:
        raise ValueError("inconsistent OHLC values")


@dataclass(frozen=True)
class DailyBar:
    ticker: str
    security_id: str
    price_basis_id: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.price_basis_id, "price_basis_id")
        require_ohlc(self.open, self.high, self.low, self.close)
        require_finite_number(self.volume, "volume", non_negative=True)


@dataclass(frozen=True)
class IntradayBar:
    """A completed five-minute regular-hours bar.

    ``start`` is the opening timestamp of the bar.  Therefore a signal based on
    this bar can be executed no earlier than ``end`` (the next bar open).
    """

    ticker: str
    security_id: str
    price_basis_id: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    duration: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.price_basis_id, "price_basis_id")
        require_aware(self.start, "start")
        require_ohlc(self.open, self.high, self.low, self.close)
        if self.duration != timedelta(minutes=5):
            raise ValueError("canonical strategy only accepts five-minute bars")
        require_finite_number(self.volume, "volume", non_negative=True)
        local_time = self.exchange_start.time()
        if local_time.minute % 5 != 0 or local_time.second != 0 or local_time.microsecond != 0:
            raise ValueError("five-minute bars must start on the exchange five-minute grid")
        if local_time < datetime.strptime("09:30", "%H:%M").time() or local_time > datetime.strptime("15:55", "%H:%M").time():
            raise ValueError("canonical strategy only accepts regular-hours bars")

    @property
    def exchange_start(self) -> datetime:
        return self.start.astimezone(EXCHANGE_TIMEZONE)

    @property
    def end(self) -> datetime:
        return self.start + self.duration

    @property
    def session(self) -> date:
        return self.exchange_start.date()


@dataclass(frozen=True)
class MacdSetup:
    ticker: str
    security_id: str
    price_basis_id: str
    signal_session: date
    third_wave_start_session: date
    third_wave_low: float
    signal_histogram: float

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.price_basis_id, "price_basis_id")
        if self.third_wave_start_session > self.signal_session:
            raise ValueError("third wave cannot begin after its signal session")
        require_finite_number(self.third_wave_low, "third_wave_low", positive=True)
        require_finite_number(self.signal_histogram, "signal_histogram")


@dataclass(frozen=True)
class QuadStochasticTrigger:
    ticker: str
    detected_at: datetime
    execute_at: datetime
    second_test_low: float
    d9: float
    d14: float
    d40: float
    d60: float

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_aware(self.detected_at, "detected_at")
        require_aware(self.execute_at, "execute_at")
        # A completed bar end and the next bar open share the same timestamp.
        if self.execute_at < self.detected_at:
            raise ValueError("QuadStochastic trigger execution cannot precede detection")
        for field_name in ("second_test_low", "d9", "d14", "d40", "d60"):
            require_finite_number(getattr(self, field_name), field_name)


@dataclass(frozen=True)
class EntrySignal:
    signal_id: str
    ticker: str
    security_id: str
    branch: str
    variant: str
    detected_at: datetime
    execute_at: datetime
    stop_price: float
    context_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("signal_id", "ticker", "security_id", "variant", "context_id"):
            require_nonempty_string(getattr(self, field_name), field_name)
        if self.branch not in {"reversal", "event"}:
            raise ValueError("branch must be reversal or event")
        require_aware(self.detected_at, "detected_at")
        require_aware(self.execute_at, "execute_at")
        if self.execute_at < self.detected_at:
            raise ValueError("execution cannot precede detection")
        require_finite_number(self.stop_price, "stop_price", positive=True)
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class OrderIntent:
    signal_id: str
    ticker: str
    security_id: str
    branch: str
    variant: str
    submitted_at: datetime
    reference_entry: float
    stop_price: float
    quantity: int
    risk_dollars: float
    context_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("signal_id", "ticker", "security_id", "branch", "variant", "context_id"):
            require_nonempty_string(getattr(self, field_name), field_name)
        require_aware(self.submitted_at, "submitted_at")
        require_finite_number(self.reference_entry, "reference_entry", positive=True)
        require_finite_number(self.stop_price, "stop_price", positive=True)
        require_finite_number(self.risk_dollars, "risk_dollars", non_negative=True)
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class OpenRisk:
    ticker: str
    security_id: str
    risk_dollars: float
    gross_dollars: float

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_nonempty_string(self.security_id, "security_id")
        require_finite_number(self.risk_dollars, "risk_dollars", non_negative=True)
        require_finite_number(self.gross_dollars, "gross_dollars", non_negative=True)


@dataclass(frozen=True)
class ExitDecision:
    action: str
    reason: str
    price: float
    fraction: float

    def __post_init__(self) -> None:
        if self.action not in {"exit", "reduce"}:
            raise ValueError("action must be exit or reduce")
        if not 0 < self.fraction <= 1:
            raise ValueError("fraction must be in (0, 1]")
        require_finite_number(self.price, "price", positive=True)
        require_finite_number(self.fraction, "fraction", positive=True)
