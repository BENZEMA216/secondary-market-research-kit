# Historical Backtest Adapter

`scenario_router.backtest.HistoricalBacktester` is the chronological adapter
for the frozen Scenario Router core.  It does not reimplement the signal rules:
it feeds completed bars and point-in-time evidence into `EventSession`,
`ReversalSession`, `SignalRouter`, and `PaperBroker`.

The adapter is a research tool.  A successful run means that the supplied data
could be replayed under the declared assumptions; it does not certify the data
vendor, market capacity, profitability, or live readiness.

## Quick start

Python 3.11 or newer is required.

~~~bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 backtest_cli.py \
  --data /absolute/path/to/point_in_time_dataset \
  --output results/e2a_m4 \
  --start 2018-01-02 \
  --end 2025-12-31 \
  --event-variant E2A \
  --reversal-variant M4 \
  --event-mode strict_primary \
  --coverage-mode retrospective_audit \
  --data-mode historical_point_in_time \
  --slippage-bps 0,10,25,50 \
  --commission-per-share 0.005 \
  --minimum-commission 1 \
  --code-revision YOUR_IMMUTABLE_GIT_SHA
~~~

`--slippage-bps` is **per side** and is extra to the supplied bid/ask.  Every
cost scenario receives a fresh detector state, portfolio, and risk budget.
The extra-slippage dollar fields are diagnostics already embedded in fill
prices; do not subtract them from P&L again.  Commission is the only separate
cash deduction.  Historical mode requires an explicitly declared 40- or
64-hex immutable source revision; the adapter records but cannot independently
verify that caller declaration.
Run E2A/E2B and M2/M3/M4 as separate experiments; never pool their capital and
then describe the result as an independent arm.

## Canonical input directory

All nine files are required.  JSONL files may contain no records, but the files
must exist.  CSV headers are exact and timestamps must include an offset.

### `calendar.csv`

~~~text
session,market_open,market_close
2026-08-05,2026-08-05T09:30:00-04:00,2026-08-05T16:00:00-04:00
~~~

Use an independently sourced official exchange schedule, including holidays,
half days, and daylight-saving offsets.  The adapter never infers missing
sessions from observed bars.

### `universe.csv`

~~~text
session,ticker,security_id,price_basis_id,security_classification,sector,cluster
2026-08-05,AAPL,FIGI:BBG000B9XRY4,split-adjusted-v1,primary_common_stock,Technology,mega-cap
~~~

This is one point-in-time row per security and exchange session.  Include
historical ticker changes and securities that later delisted.  `sector` and
`cluster` must be known values because the frozen paper risk controller rejects
unknown classifications.

### `daily_bars.csv`

~~~text
session,ticker,security_id,price_basis_id,open,high,low,close,volume
2026-08-05,AAPL,FIGI:BBG000B9XRY4,split-adjusted-v1,110,115,108.5,114,1000000
~~~

The adapter derives prior close, average full-day volume 20, median dollar
volume 20, and EMA10 itself.  Entry statistics use only the prior 20 complete,
consecutive exchange sessions on the same price basis.  At least 20 warm-up
sessions must precede the first traded date; MACD normally needs more.

### `intraday_bars.csv`

~~~text
start,ticker,security_id,price_basis_id,open,high,low,close,volume
2026-08-05T09:30:00-04:00,AAPL,FIGI:BBG000B9XRY4,split-adjusted-v1,110,111,109,110,125000
~~~

Every listed security/session must contain the complete regular-hours
five-minute grid from the declared open through the final bar ending at the
declared close.  Missing, duplicate, truncated, or out-of-grid sessions fail
the whole run.  Daily OHLC must equal the regular-hours five-minute aggregate
within a relative `1e-10` price tolerance; daily volume must equal summed
five-minute volume within one share or one part per million.  This deliberately
rejects extended-hours or mixed-vendor aggregation drift because daily lows and
volumes directly drive setups and sizing.  M3/M4 stochastic state is warmed
with all supplied prior sessions and is reset after a membership gap or
price-basis change.

### `quotes.csv`

~~~text
timestamp,ticker,security_id,price_basis_id,bid_price,ask_price
2026-08-05T09:55:00-04:00,AAPL,FIGI:BBG000B9XRY4,split-adjusted-v1,112.05,112.15
~~~

Supply NBBO snapshots at every boundary that can become an entry or exit.
Missing required NBBO aborts the run instead of selectively dropping a trade.
Entry uses ask plus extra slippage.  Exit uses the worse of the policy reference
and boundary bid, then applies extra slippage.

### Existing event contracts

- `feed_manifest.json`: the existing feed-manifest schema.
- `articles.jsonl`: the existing article-presence schema.
- `events.jsonl`: the existing structured-event schema.
- `reference_snapshots.jsonl`: the existing reference-snapshot schema.

Stable IDs in these files must exactly match `universe.csv` and the bar files.
The provider label must already be normalized to `news_feed` as required by the
frozen core.

## Coverage modes

`retrospective_audit` treats the feed manifest as an after-the-fact data
completeness certificate.  Its capture timestamp may follow the historical
decision.  This is useful for research but cannot be described as a live-parity
decision.

`point_in_time` is a deliberately narrow single-snapshot check.  Because the
current contract supplies only one `feed_manifest.json`, it supports exactly
one replay session and only a pre-open coverage decision.  A manifest captured
after the cutoff forces `COVERAGE_UNKNOWN` and abstention.  M4 is rejected in
this mode because M4 needs a new coverage assertion at every intraday decision
boundary.  A causal multi-session point-in-time study therefore needs a future
coverage-revision-ledger input contract; the adapter fails closed until that
contract exists.  The selected mode and its meaning are written to every run
manifest.

## Fixed event order

For each exchange timestamp the adapter performs one deterministic sequence:

1. Validate the session identity and reject an unsupported open-position price
   basis or ticker change.
2. Preview immutable exit policies.  Mark every position synchronously at the
   completed-bar close, except a position that must exit is marked at its exact
   projected fill price; this prevents an impossible pre-exit equity peak.
3. Execute those protective and scheduled exits.
4. Feed the newly completed bar into the active strategy state machines.
5. Collect all securities' candidates for that timestamp.
6. Apply the pre-open frozen information route.
7. Sort entries by event-before-reversal, execution time, prior 20-day median
   dollar volume descending, ticker, variant, and signal ID.
8. Submit and fill sequentially through the existing `PaperBroker` and frozen
   portfolio risk controller.
9. At the close, record one daily NAV row and only then update daily MACD state.

This prevents CSV row order from choosing which four simultaneous candidates
receive the available position slots.

## Outputs

Each cost directory contains:

- `run_manifest.json`: source revision, strategy/risk hashes, input hashes,
  configuration, cost semantics, output hashes, and execution limitations.
- `candidate_ledger.jsonl`: one security/session record, including abstentions,
  qualifications, routes, trailing windows, signals, orders, and terminal reason.
- `orders.jsonl`, `fills.jsonl`, `execution_audit.jsonl`: portfolio and quote-bound
  execution evidence.
- `trades.jsonl`: parent trade with partial exits, fill-basis P&L before
  commission, commission cash expense, costs already embedded in fill prices,
  reconciliation residual, and R multiple.
- `daily_nav.csv`: a row for every replayed exchange session, including no-trade
  days, begin/end equity, entry/exit fill counts, exposure, risk state, costs,
  and an independently testable P&L reconciliation.
- `metrics.json`: descriptive return, volatility, Sharpe, Sortino, drawdown,
  turnover, trade statistics, bookkeeping P&L by executed branch, and a
  security-session funnel.  The funnel's first stage is not an upstream signal
  count.
- `portfolio_snapshot.json`: the existing restartable and self-validating paper
  portfolio snapshot.

The root output also contains `cost_comparison.json` for all requested slippage
scenarios.  Output files are atomically created and SHA-256 hashed.  Existing
artifacts are never overwritten; select a new output directory for another
experiment.  `scenario_router.backtest.verify_backtest_output(path)`
independently recomputes every artifact hash and restores the portfolio
snapshot before returning `PASS_BACKTEST_OUTPUT_INTEGRITY`.

## Explicit limitations

- M2 uses the frozen core's atomic opening assumption: the opening price is
  observed and the fill shares the same opening timestamp.
- Five-minute OHLC determines whether an intrabar stop/target was touched; the
  supplied NBBO at the bar boundary supplies the executable quote proxy.
- Positions are normally valued at the last completed five-minute bar close
  (and at the declared session open before the first completed bar), not at a
  liquidation bid.  A position with a deterministic same-boundary exit is
  instead valued at its projected fill so the conservative exit assumption
  cannot manufacture a contradictory pre-exit high-water mark.
- Fills are all-or-reject.  There is no queue position, displayed-size limit,
  partial fill, auction, LULD/halt, or market-impact model.
- Open-position splits and other corporate actions are rejected because the
  existing portfolio has no atomic price/quantity/policy rebase operation.
- Dividends, cash interest, benchmarks, factor attribution, confidence
  intervals, and multiple-testing corrections are not yet modeled.
- Open trades are marked at the final session; they are not force-liquidated.
- This reference implementation loads the canonical files into memory.  Large
  all-US-universe studies should add a date/security partitioned storage layer
  without changing the chronological adapter contract.
- `historical_point_in_time` is a caller-declared evidence label.  The adapter
  validates shapes, identities, timing, and hashes, but it cannot authenticate
  the vendor, dataset vintage, or raw-source ownership.

These limitations are repeated in machine-readable output.  Do not remove them
from a research report merely because a run completed successfully.
