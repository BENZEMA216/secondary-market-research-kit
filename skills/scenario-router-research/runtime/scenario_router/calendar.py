"""Exchange-generated session boundaries used by every causal time gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Iterable

from .models import EXCHANGE_TIMEZONE, require_aware


@dataclass(frozen=True)
class ExchangeSession:
    session: date
    market_open: datetime
    market_close: datetime

    def __post_init__(self) -> None:
        require_aware(self.market_open, "market_open")
        require_aware(self.market_close, "market_close")
        if self.market_open.astimezone(EXCHANGE_TIMEZONE).date() != self.session:
            raise ValueError("market_open does not belong to the declared exchange session")
        if self.market_close.astimezone(EXCHANGE_TIMEZONE).date() != self.session:
            raise ValueError("market_close does not belong to the declared exchange session")
        if self.market_close <= self.market_open:
            raise ValueError("market_close must follow market_open")


class TradingSessionCalendar:
    """Immutable, exact exchange schedule supplied by the backtest adapter."""

    def __init__(self, sessions: Iterable[ExchangeSession]) -> None:
        by_date: dict[date, ExchangeSession] = {}
        for item in sessions:
            if not isinstance(item, ExchangeSession):
                raise ValueError("calendar entries must be ExchangeSession records")
            if item.session in by_date and by_date[item.session] != item:
                raise ValueError(f"conflicting exchange session: {item.session}")
            by_date[item.session] = item
        if not by_date:
            raise ValueError("trading calendar cannot be empty")
        self.sessions = tuple(by_date[key] for key in sorted(by_date))
        self.index = MappingProxyType({item.session: index for index, item in enumerate(self.sessions)})
        self.by_date = MappingProxyType(by_date)

    def get(self, session: date) -> ExchangeSession:
        try:
            return self.by_date[session]
        except KeyError as exc:
            raise ValueError(f"exchange session is absent from calendar: {session}") from exc

    def previous(self, session: date) -> ExchangeSession:
        index = self.index.get(session)
        if index is None or index == 0:
            raise ValueError(f"calendar has no previous session for {session}")
        return self.sessions[index - 1]

    def next(self, session: date) -> ExchangeSession:
        index = self.index.get(session)
        if index is None or index + 1 >= len(self.sessions):
            raise ValueError(f"calendar has no next session for {session}")
        return self.sessions[index + 1]

    def is_next_session(self, signal_session: date, candidate_session: date) -> bool:
        try:
            return self.next(signal_session).session == candidate_session
        except ValueError:
            return False

    def news_window_start_for(self, third_wave_start_session: date) -> datetime:
        return self.previous(third_wave_start_session).market_close
