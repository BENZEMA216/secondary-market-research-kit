#!/usr/bin/env python3
"""Synthetic rules -> portfolio risk -> fills -> restart -> exits.

This exercises actual local modules, not a return backtest. No LLM or real
account is called. The optional AI text workflow has separate raw evidence.
"""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run_signal_example import build_signal_fixture, NY
from scenario_router.paper import CostModel, PaperBroker
from scenario_router.portfolio import MarketMark, PortfolioLedger


def run(costs):
    _, _, selected, quote, _ = build_signal_fixture()
    ledger = PortfolioLedger(100_000, "synthetic-" + str(costs.slippage_bps))
    broker = PaperBroker(ledger, cost_model=costs)
    order = broker.submit_and_fill(selected, quote, sector="synthetic:Technology",
                                   cluster="synthetic:mega-cap", as_of=selected.execute_at)
    assert order.status == "FILLED"
    if costs == CostModel():
        assert order.quantity == 68
    with tempfile.TemporaryDirectory(prefix="scenario-paper-") as temp:
        path = Path(temp) / "snapshot.json"
        ledger.save(path)
        recovered = PortfolioLedger.load(path)
    broker = PaperBroker(recovered)
    broker.submit_and_fill(selected, quote, sector="synthetic:Technology",
                           cluster="synthetic:mega-cap", as_of=selected.execute_at)
    assert len(recovered.fills) == 1, "restart must not create another entry"
    policy = broker.get_exit_policy(order.order_id)
    # Actual 2026 holding sessions in the fixture, not calendar-day increments.
    for holding_day, day, close, ema in ((1, 5, 114, 112), (2, 6, 116, 113),
                                        (3, 7, 118, 115), (4, 10, 120, 117), (5, 11, 120, 121)):
        at = datetime(2026, 8, day, 16, 0, tzinfo=NY)
        broker.on_market([MarketMark(selected.security_id, close, at, quote.price_basis_id)], at)
        exit_quote = replace(quote, timestamp=at, next_bar_open=close, bid_price=close, ask_price=close)
        result = broker.propose_and_execute_exit(order.order_id, policy, "on_session_close",
                    close, ema, quote.price_basis_id, holding_session=holding_day,
                    quote=exit_quote, as_of=at)
    at = datetime(2026, 8, 12, 9, 30, tzinfo=NY)
    broker.on_market([MarketMark(selected.security_id, 119, at, quote.price_basis_id)], at)
    broker.propose_and_execute_exit(order.order_id, policy, "on_session_open", 119, quote.price_basis_id,
        quote=replace(quote, timestamp=at, next_bar_open=119, bid_price=119, ask_price=119), as_of=at)
    assert not recovered.positions and policy.closed
    assert abs(recovered.equity - (100_000 + recovered.realized_gross_pnl - recovered.fees_paid)) < 1e-7
    if costs == CostModel():
        assert abs(recovered.equity - 100_499.80) < 1e-7
    return recovered.snapshot()


def main():
    result = {"data_mode": "SYNTHETIC_FIXTURE_NOT_RETURNS_EVIDENCE",
              "zero_cost": run(CostModel()),
              "illustrative_cost": run(CostModel(slippage_bps=10, commission_per_share=.01, minimum_commission=1)),
              "cost_note": "10 bp extra to spread and $0.01/share min $1 are test assumptions, not a quoted broker tariff",
              "profitability_validated": False, "real_model_called": False, "real_orders_submitted": False}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
