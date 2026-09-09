"""Deterministic reference implementation of the combined scenario router."""

from .calendar import ExchangeSession, TradingSessionCalendar
from .engine import EventSession, ReversalSession, SignalRouter, macd_open_signal
from .events import (
    ArticleLedger,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
    choose_information_route,
    qualify_premarket_event,
)
from .indicators import QuadStochasticPatternDetector, QuadStochasticDetector, MacdThreeWaveDetector
from .configuration import STRATEGY_VERSION, load_frozen_config
from .models import DailyBar, EntrySignal, IntradayBar, MacdSetup, OrderIntent, to_jsonable
from .risk import RiskManager

__version__ = "0.3.0"

__all__ = [
    "ArticleLedger",
    "DailyBar",
    "EntrySignal",
    "ExchangeSession",
    "EventLedger",
    "EventSession",
    "FeedCoverage",
    "IntradayBar",
    "QuadStochasticPatternDetector",
    "QuadStochasticDetector",
    "MacdSetup",
    "MacdThreeWaveDetector",
    "OrderIntent",
    "ReversalSession",
    "ReferenceSnapshotLedger",
    "RiskManager",
    "SignalRouter",
    "TradingSessionCalendar",
    "STRATEGY_VERSION",
    "load_frozen_config",
    "macd_open_signal",
    "to_jsonable",
    "choose_information_route",
    "qualify_premarket_event",
]
