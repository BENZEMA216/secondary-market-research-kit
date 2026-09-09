"""Portfolio heat, deterministic allocation, and next-bar entry checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from .configuration import (
    MAXIMUM_ENTRY_NBBO_SPREAD_BPS,
    MAXIMUM_GROSS_EXPOSURE,
    MINIMUM_MEDIAN_DOLLAR_VOLUME_20,
    MAXIMUM_POSITIONS,
    MAXIMUM_SINGLE_NAME_EXPOSURE,
    MAXIMUM_TOTAL_OPEN_RISK,
    MINIMUM_PRIOR_CLOSE,
    RISK_PER_TRADE,
)
from .models import (
    EntrySignal,
    OpenRisk,
    OrderIntent,
    require_aware,
    require_finite_number,
    require_nonempty_string,
)


@dataclass(frozen=True)
class EntryQuote:
    ticker: str
    security_id: str
    price_basis_id: str
    timestamp: datetime
    next_bar_open: float
    bid_price: float
    ask_price: float
    prior_close: float
    median_dollar_volume_20: float
    security_classification: str

    def __post_init__(self) -> None:
        require_nonempty_string(self.ticker, "ticker")
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.price_basis_id, "price_basis_id")
        require_nonempty_string(self.security_classification, "security_classification")
        require_aware(self.timestamp, "timestamp")
        require_finite_number(self.next_bar_open, "next_bar_open", positive=True)
        require_finite_number(self.bid_price, "bid_price", positive=True)
        require_finite_number(self.ask_price, "ask_price", positive=True)
        if self.ask_price < self.bid_price:
            raise ValueError("ask_price cannot be below bid_price")
        require_finite_number(self.prior_close, "prior_close", positive=True)
        require_finite_number(
            self.median_dollar_volume_20, "median_dollar_volume_20", non_negative=True
        )

    @property
    def spread_bps(self) -> float:
        midpoint = (self.bid_price + self.ask_price) / 2.0
        return (self.ask_price - self.bid_price) / midpoint * 10_000.0

    @property
    def executable_long_price(self) -> float:
        """Conservative point-in-time long fill reference: the displayed ask."""

        return self.ask_price


class RiskManager:
    def __init__(
        self,
        risk_per_trade: float = RISK_PER_TRADE,
        max_positions: int = MAXIMUM_POSITIONS,
        max_total_open_risk: float = MAXIMUM_TOTAL_OPEN_RISK,
        max_gross_exposure: float = MAXIMUM_GROSS_EXPOSURE,
        max_single_name_exposure: float = MAXIMUM_SINGLE_NAME_EXPOSURE,
        max_spread_bps: float = MAXIMUM_ENTRY_NBBO_SPREAD_BPS,
    ) -> None:
        for field_name, value in (
            ("risk_per_trade", risk_per_trade),
            ("max_total_open_risk", max_total_open_risk),
            ("max_gross_exposure", max_gross_exposure),
            ("max_single_name_exposure", max_single_name_exposure),
            ("max_spread_bps", max_spread_bps),
        ):
            require_finite_number(value, field_name, non_negative=True)
        if not isinstance(max_positions, int) or isinstance(max_positions, bool):
            raise ValueError("max_positions must be an integer")
        if not 0 < risk_per_trade <= max_total_open_risk:
            raise ValueError("invalid risk budget")
        self.risk_per_trade = risk_per_trade
        self.max_positions = max_positions
        self.max_total_open_risk = max_total_open_risk
        self.max_gross_exposure = max_gross_exposure
        self.max_single_name_exposure = max_single_name_exposure
        self.max_spread_bps = max_spread_bps
        if (
            max_positions <= 0
            or max_total_open_risk <= 0
            or max_gross_exposure <= 0
            or max_single_name_exposure <= 0
            or max_spread_bps < 0
        ):
            raise ValueError("risk and exposure limits must be positive")

    def allocate(
        self,
        signals: Iterable[EntrySignal],
        quotes: Mapping[str, EntryQuote],
        equity: float,
        open_risks: Iterable[OpenRisk] = (),
    ) -> tuple[OrderIntent, ...]:
        require_finite_number(equity, "equity", positive=True)
        signals = tuple(signals)
        if len({signal.execute_at for signal in signals}) > 1:
            raise ValueError("allocation batches may contain only one execution timestamp")
        open_risks = tuple(open_risks)
        if len({item.security_id for item in open_risks}) != len(open_risks):
            raise ValueError("open_risks contains duplicate stable security IDs")
        occupied_security_ids = {item.security_id for item in open_risks}
        slots = max(0, self.max_positions - len(open_risks))
        risk_room = max(0.0, self.max_total_open_risk * equity - sum(item.risk_dollars for item in open_risks))
        gross_room = max(0.0, self.max_gross_exposure * equity - sum(item.gross_dollars for item in open_risks))

        candidates: list[tuple[EntrySignal, EntryQuote]] = []
        seen_signal_ids: set[str] = set()
        for signal in signals:
            if signal.signal_id in seen_signal_ids:
                continue
            seen_signal_ids.add(signal.signal_id)
            if signal.security_id in occupied_security_ids:
                continue
            quote = quotes.get(signal.signal_id)
            if quote is None:
                continue
            require_aware(quote.timestamp, "quote.timestamp")
            if quote.ticker != signal.ticker or quote.security_id != signal.security_id:
                raise ValueError("quote identity does not match signal")
            if quote.price_basis_id != signal.metadata.get("price_basis_id"):
                raise ValueError("quote and signal use different corporate-action price bases")
            if quote.timestamp != signal.execute_at:
                # This rejects same-bar or late fills in the reference layer.
                continue
            if (
                quote.security_classification != "primary_common_stock"
                or quote.prior_close < MINIMUM_PRIOR_CLOSE
                or quote.median_dollar_volume_20 < MINIMUM_MEDIAN_DOLLAR_VOLUME_20
            ):
                continue
            if quote.spread_bps > self.max_spread_bps:
                continue
            if quote.next_bar_open <= signal.stop_price or quote.executable_long_price <= signal.stop_price:
                continue
            candidates.append((signal, quote))

        candidates.sort(key=lambda pair: (
            0 if pair[0].branch == "event" else 1,
            pair[0].execute_at,
            -pair[1].median_dollar_volume_20,
            pair[0].ticker,
            pair[0].variant,
        ))
        intents: list[OrderIntent] = []
        accepted_security_ids = set(occupied_security_ids)
        for signal, quote in candidates:
            if slots <= 0 or risk_room <= 0 or gross_room <= 0:
                break
            if signal.security_id in accepted_security_ids:
                continue
            entry_price = quote.executable_long_price
            per_share_risk = entry_price - signal.stop_price
            target_risk = min(self.risk_per_trade * equity, risk_room)
            risk_shares = math.floor(target_risk / per_share_risk)
            single_name_shares = math.floor(self.max_single_name_exposure * equity / entry_price)
            gross_shares = math.floor(gross_room / entry_price)
            quantity = min(risk_shares, single_name_shares, gross_shares)
            if quantity <= 0:
                continue
            risk_dollars = quantity * per_share_risk
            gross_dollars = quantity * entry_price
            intents.append(OrderIntent(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                security_id=signal.security_id,
                branch=signal.branch,
                variant=signal.variant,
                submitted_at=quote.timestamp,
                reference_entry=entry_price,
                stop_price=signal.stop_price,
                quantity=quantity,
                risk_dollars=risk_dollars,
                context_id=signal.context_id,
                metadata=signal.metadata,
            ))
            slots -= 1
            risk_room -= risk_dollars
            gross_room -= gross_dollars
            accepted_security_ids.add(signal.security_id)
        return tuple(intents)
