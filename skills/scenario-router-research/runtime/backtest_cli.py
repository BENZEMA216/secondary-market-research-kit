#!/usr/bin/env python3
"""Run one or more costed historical scenario-router replays."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from scenario_router.backtest import (
    BACKTEST_ENGINE_VERSION,
    BacktestConfig,
    BacktestDataset,
    HistoricalBacktester,
)
from scenario_router.models import to_jsonable
from scenario_router.paper import CostModel


def _slippage_values(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid slippage bps: {item}") from exc
        if not math.isfinite(value) or value < 0 or value >= 10_000:
            raise argparse.ArgumentTypeError("slippage bps must be finite and in [0, 10000)")
        if value not in values:
            values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one slippage scenario is required")
    return tuple(values)


def _scenario_name(value: float) -> str:
    token = f"{value:.6f}".rstrip("0").rstrip(".").replace(".", "p")
    return f"extra_slippage_{token}_bps_per_side"


def _atomic_json(path: Path, value: Any) -> None:
    payload = (
        json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one frozen Scenario Router arm using point-in-time CSV/JSONL inputs. "
            "This is a research backtest, never a live-order command."
        )
    )
    parser.add_argument("--data", required=True, help="directory containing the canonical data files")
    parser.add_argument("--output", required=True, help="new or empty output directory")
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="first traded session YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="last replayed session YYYY-MM-DD")
    parser.add_argument("--event-variant", choices=("E2A", "E2B"), default="E2A")
    parser.add_argument("--reversal-variant", choices=("M2", "M3", "M4"), default="M4")
    parser.add_argument(
        "--event-mode",
        choices=("strict_primary", "agent_assisted_secondary"),
        default="strict_primary",
    )
    parser.add_argument(
        "--coverage-mode",
        choices=("retrospective_audit", "point_in_time"),
        default="retrospective_audit",
        help=(
            "retrospective_audit treats the feed manifest as later data QA; "
            "point_in_time supports one pre-open session only and rejects M4"
        ),
    )
    parser.add_argument(
        "--data-mode",
        choices=("historical_point_in_time", "synthetic_fixture"),
        required=True,
        help="machine-readable evidence label written to every result",
    )
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument(
        "--slippage-bps",
        type=_slippage_values,
        default=(0.0, 10.0, 25.0, 50.0),
        help="comma-separated extra slippage per side outside bid/ask (default: 0,10,25,50)",
    )
    parser.add_argument("--commission-per-share", type=float, default=0.0)
    parser.add_argument("--minimum-commission", type=float, default=0.0)
    parser.add_argument(
        "--code-revision",
        required=True,
        help="Git SHA or immutable source revision recorded in the run manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dataset = BacktestDataset.from_directory(args.data)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "cost_comparison.json").exists():
        raise FileExistsError(
            f"refusing to overwrite existing backtest summary: {output / 'cost_comparison.json'}"
        )
    artifact_names = {
        "run_manifest.json", "metrics.json", "candidate_ledger.jsonl",
        "orders.jsonl", "fills.jsonl", "trades.jsonl",
        "execution_audit.jsonl", "daily_nav.csv", "portfolio_snapshot.json",
    }
    collisions: list[str] = []
    for slippage in args.slippage_bps:
        scenario_directory = output / _scenario_name(slippage)
        if scenario_directory.exists() and not scenario_directory.is_dir():
            collisions.append(str(scenario_directory))
        elif scenario_directory.is_dir():
            collisions.extend(
                str(path)
                for path in scenario_directory.iterdir()
                if path.name in artifact_names
            )
    collisions.sort()
    if collisions:
        raise FileExistsError(
            "refusing to overwrite existing backtest artifacts: "
            + ", ".join(collisions)
        )
    scenarios = []
    for slippage in args.slippage_bps:
        try:
            config = BacktestConfig(
                start_session=args.start,
                end_session=args.end,
                initial_cash=args.initial_cash,
                event_variant=args.event_variant,
                reversal_variant=args.reversal_variant,
                event_mode=args.event_mode,
                coverage_mode=args.coverage_mode,
                data_mode=args.data_mode,
                code_revision=args.code_revision,
            )
        except ValueError as exc:
            parser.error(str(exc))
        costs = CostModel(
            slippage_bps=slippage,
            commission_per_share=args.commission_per_share,
            minimum_commission=args.minimum_commission,
        )
        scenario_name = _scenario_name(slippage)
        result = HistoricalBacktester(dataset, config, costs).run()
        manifest = result.write(output / scenario_name)
        scenarios.append({
            "name": scenario_name,
            "run_id": result.metrics["run_id"],
            "dataset_id": result.run_manifest["dataset_id"],
            "cost_model": asdict(costs),
            "metrics": dict(result.metrics),
            "decision_result_sha256": manifest["decision_result_sha256"],
        })
    summary = {
        "engine_version": BACKTEST_ENGINE_VERSION,
        "dataset_id": scenarios[0]["dataset_id"],
        "input_sha256": dict(sorted(dataset.input_hashes.items())),
        "experiment": {
            "event_variant": args.event_variant,
            "reversal_variant": args.reversal_variant,
            "event_mode": args.event_mode,
            "coverage_mode": args.coverage_mode,
            "data_mode": args.data_mode,
            "start_session": args.start.isoformat(),
            "end_session": args.end.isoformat(),
            "initial_cash": args.initial_cash,
            "code_revision": args.code_revision,
        },
        "scenarios": scenarios,
    }
    _atomic_json(output / "cost_comparison.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
