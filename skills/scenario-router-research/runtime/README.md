# Scenario Router 0.3.0

This is a standalone Python reference implementation of a US-equity research strategy. It contains deterministic signal rules, event qualification, portfolio risk controls, local paper execution, a chronological historical backtest adapter, and an optional AI evidence-review workflow.

Start with [STRATEGY_THREE_LAYER_RESEARCH.md](STRATEGY_THREE_LAYER_RESEARCH.md). It explains the three layers, their authority boundaries, the current rules, and the research still required.

For the historical data contract, replay order, cost scenarios, outputs, and explicit simulation limitations, see [BACKTEST.md](BACKTEST.md).

## What this package is

- A causal reference core: completed bars in, typed candidates and auditable decisions out.
- A local research and paper-simulation package; it has no brokerage connector and cannot place live orders.
- A standalone handoff whose public identifiers use descriptive financial and software terms.
- A fail-closed design: missing coverage, inconsistent timing, identity drift, incompatible price bases, disabled AI, or incomplete evidence produces abstention or rejection.

Operational evidence fields such as `source_id`, `source_sha256`, `source_locator`, `category_provenance`, and `provenance_mode` preserve the lineage of market and company evidence so point-in-time decisions can be audited.

## Scope

The core supports two mutually exclusive long-only branches:

1. A no-observed-article reversal branch: daily three-wave MACD divergence followed by an optional five-minute four-line stochastic reversal trigger.
2. A positive-event continuation branch: premarket event qualification followed by gap, volume, and opening-range-breakout confirmation.

An optional AI workflow reads archived issuer documents twice, validates exact quotations and numeric fields, stores revisions, and can only veto an already-qualified guidance-raise candidate. It cannot create an event fact, size a position, change risk limits, reset a halt, or submit an order.

The package API and strategy contract are version `0.3.0`. The embedded paper-risk and portfolio-snapshot contracts remain `0.2.0` because their behavior and serialized shape did not change.

## Requirements

- Python 3.11 or newer.
- The strategy core uses only the Python standard library.
- A compatible local model CLI is optional and disabled by default.

## Verify locally

Run from this directory:

~~~bash
shasum -a 256 -c MANIFEST.sha256
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/run_signal_example.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/run_paper_example.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 validate_ledgers.py \
  --manifest examples/feed_manifest.sample.json \
  --articles examples/article_presence.sample.jsonl \
  --events examples/structured_events.sample.jsonl \
  --snapshots examples/reference_snapshots.sample.jsonl
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 validate.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 validate_runtime.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 workflow_cli.py --help
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 backtest_cli.py --help
~~~

`validate.py` runs the complete unit and contract suite. Optional historical parity data is not included, so those checks correctly report `SKIPPED` and the overall result is `PASS_SYNTHETIC_ONLY`. `validate_runtime.py` checks paper accounting and the disabled-provider path; it does not call a real model.

## Package map

- `scenario_router/`: strategy, event, risk, paper, and AI workflow modules.
- `scenario_router/backtest.py`, `backtest_cli.py`: fail-closed historical data adapter, replay engine, audit ledgers, metrics, and cost-scenario CLI.
- `BACKTEST.md`: exact historical input and output contracts plus execution assumptions.
- `frozen_config.json`: executable signal and routing contract.
- `frozen_risk_policy.json`: portfolio risk contract.
- `schemas/`: four point-in-time event-data contracts.
- `examples/`: synthetic examples only; they are not market evidence.
- `tests/`: deterministic unit and cross-layer tests.
- `MANIFEST.sha256`: integrity hashes for every other file in the package.
- `validate.py`, `validate_runtime.py`, `validate_ledgers.py`: local checks.
- `workflow_cli.py`: optional AI evidence-review entry point.

## Important boundary

This package does not include raw research material, model traces, PDFs, screenshots, SQLite audit databases, historical pilot data, or the partial platform-specific M0 diagnostic. The historical adapter still requires externally sourced point-in-time universe membership, an official exchange calendar, corporate-action price bases, complete daily and five-minute bars, boundary NBBO, and event datasets. Before validation, an adapter must normalize its event-feed provider label to the frozen generic value `news_feed`; an unnormalized provider label is rejected by design.

Passing tests or completing a historical replay proves only that the packaged rules and supplied records were processed under the declared assumptions. The historical evidence label and immutable revision are caller declarations, not vendor authentication. The current single-manifest `point_in_time` mode is restricted to one session and does not support M4; multi-session causal news coverage needs a coverage-revision ledger. A replay does not prove profitability, out-of-sample robustness, source completeness, fill realism, or live readiness.
