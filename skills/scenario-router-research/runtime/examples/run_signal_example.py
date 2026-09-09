#!/usr/bin/env python3
"""Synthetic walkthrough: facts -> ORB -> route -> risk intent -> exits.

No provider, broker or LLM is called. Prices, facts and equity are test fixtures;
the exit callbacks illustrate adapter ordering, not a historical return replay.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scenario_router.calendar import ExchangeSession, TradingSessionCalendar  # noqa: E402
from scenario_router.engine import EventSession, SignalRouter  # noqa: E402
from scenario_router.events import (  # noqa: E402
    ArticleLedger,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
    qualify_premarket_event,
)
from scenario_router.exits import EventExitPolicy  # noqa: E402
from scenario_router.models import IntradayBar, to_jsonable  # noqa: E402
from scenario_router.risk import EntryQuote, RiskManager  # noqa: E402


NY = ZoneInfo("America/New_York")
SECURITY_ID = "FIGI:BBG000B9XRY4"


def build_signal_fixture():
    """Shared synthetic entry path for deterministic checks."""
    coverage = FeedCoverage.from_json(ROOT / "examples" / "feed_manifest.sample.json")
    articles = ArticleLedger.from_jsonl(ROOT / "examples" / "article_presence.sample.jsonl", coverage)
    events = EventLedger.from_jsonl(ROOT / "examples" / "structured_events.sample.jsonl")
    references = ReferenceSnapshotLedger.from_jsonl(ROOT / "examples" / "reference_snapshots.sample.jsonl")
    calendar = TradingSessionCalendar([
        ExchangeSession(
            date(2026, 8, 4),
            datetime.fromisoformat("2026-08-04T13:30:00+00:00"),
            datetime.fromisoformat("2026-08-04T20:00:00+00:00"),
        ),
        ExchangeSession(
            date(2026, 8, 5),
            datetime.fromisoformat("2026-08-05T13:30:00+00:00"),
            datetime.fromisoformat("2026-08-05T20:00:00+00:00"),
        ),
    ])
    qualification = qualify_premarket_event(
        articles,
        events,
        references,
        SECURITY_ID,
        date(2026, 8, 5),
        calendar,
    )
    session = EventSession(
        "AAPL", SECURITY_ID, date(2026, 8, 5), calendar,
        prior_close=100.0, average_full_day_volume_20=400.0,
        prior_close_price_basis_id="split-adjusted-v1",
        session_price_basis_id="split-adjusted-v1",
        event_qualification=qualification,
    )
    bars = [
        (9, 30, 110, 111, 109, 110, 100),
        (9, 35, 110, 112, 109.5, 111, 100),
        (9, 40, 111, 111.5, 109, 110, 100),
        (9, 45, 110, 111, 108.5, 110, 100),
        (9, 50, 111, 112.1, 110.5, 112, 50),
    ]
    signals = ()
    for hour, minute, open_, high, low, close, volume in bars:
        signals = session.on_bar(IntradayBar(
            "AAPL", SECURITY_ID, "split-adjusted-v1",
            datetime(2026, 8, 5, hour, minute, tzinfo=NY),
            open_, high, low, close, volume,
        )) or signals
    routed = SignalRouter.select(
        signals,
        event_variant="E2A", reversal_variant="M4",
        qualifications_by_security_id={SECURITY_ID: qualification},
        event_mode="strict_primary", calendar=calendar,
    )
    assert len(routed) == 1 and routed[0].variant == "E2A"
    selected = routed[0]
    quote = EntryQuote(
        ticker=selected.ticker, security_id=selected.security_id,
        price_basis_id="split-adjusted-v1", timestamp=selected.execute_at,
        next_bar_open=112.10, bid_price=112.05, ask_price=112.15,
        prior_close=100.0, median_dollar_volume_20=50_000_000.0,
        security_classification="primary_common_stock",
    )
    intents = RiskManager().allocate(
        routed, {selected.signal_id: quote}, equity=100_000.0
    )
    assert len(intents) == 1
    intent = intents[0]
    assert intent.quantity == 68 and intent.risk_dollars <= 250.0
    return qualification, signals, selected, quote, intents


def main() -> None:
    qualification, signals, selected, quote, intents = build_signal_fixture()
    intent = intents[0]

    exit_policy = EventExitPolicy(
        intent.reference_entry, intent.stop_price, "split-adjusted-v1"
    )
    exits = []
    # Replay adapters must feed every intervening bar/session and handle actual
    # fills. This fixture deliberately supplies only known, synthetic callbacks.
    assert exit_policy.on_intraday_bar(112.10, 113.0, 111.80, "split-adjusted-v1") is None
    for holding_session, close, ema10 in (
        (1, 114.0, 112.0), (2, 116.0, 113.0), (3, 118.0, 115.0),
        (4, 120.0, 117.0), (5, 120.0, 121.0),
    ):
        if holding_session > 1:
            assert exit_policy.on_session_open(close, "split-adjusted-v1") is None
            assert exit_policy.on_intraday_bar(close, close + 1.0, close - 0.5, "split-adjusted-v1") is None
        exits.extend(exit_policy.on_session_close(
            close, ema10, "split-adjusted-v1", holding_session=holding_session
        ))
    final_exit = exit_policy.on_session_open(119.0, "split-adjusted-v1")
    assert final_exit is not None and final_exit.reason == "ema10_next_open"
    exits.append(final_exit)
    assert [item.reason for item in exits] == ["day5_half", "ema10_next_open"]
    assert sum(item.fraction for item in exits) == 1.0 and exit_policy.closed
    print(json.dumps({
        "data_mode": "SYNTHETIC_TEST_FIXTURE_NOT_MARKET_EVIDENCE",
        "event_status": qualification.status,
        "signals": [
            {
                "variant": item.variant,
                "detected_at": item.detected_at.isoformat(),
                "execute_at": item.execute_at.isoformat(),
                "stop_price": item.stop_price,
            }
            for item in signals
        ],
        "selected_experiment": selected.metadata["experiment_id"],
        "order_intents": [to_jsonable(item) for item in intents],
        "exit_decisions": [to_jsonable(item) for item in exits],
        "profitability_validated": False,
        "external_agent_called": False,
        "orders_submitted": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
