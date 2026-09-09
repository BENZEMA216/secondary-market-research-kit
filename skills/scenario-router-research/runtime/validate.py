#!/usr/bin/env python3
"""Run local release checks for the deterministic strategy package.

This validates reference logic, selected causal paths, runtime contracts, and
parity with an optional locked pilot dataset. JSON Schema documents are parsed,
not independently evaluated by a Draft 2020-12 validator. It deliberately does
not calculate or claim investment returns.
"""

from __future__ import annotations

import compileall
import csv
import hashlib
import io
import json
import subprocess
import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

from scenario_router.calendar import ExchangeSession, TradingSessionCalendar
from scenario_router.configuration import STRATEGY_VERSION, load_frozen_config
from scenario_router.events import (
    ArticleLedger,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
    qualify_premarket_event,
)
from scenario_router.indicators import QuadStochasticDetector, MacdThreeWaveDetector
from scenario_router.models import DailyBar, EXCHANGE_TIMEZONE, IntradayBar


ROOT = Path(__file__).resolve().parent
# Optional reference inputs are intentionally local to this standalone package.
# They are not shipped with the standalone handoff, so the related parity
# checks report SKIPPED unless a recipient adds compatible files explicitly.
LOCKED_DATA = ROOT / "optional_validation_data" / "parsed"
LOCKED_TRADE_LOG = ROOT / "optional_validation_data" / "trade_log.csv"
EXPECTED_MACD3_LONG = {
    "AAPL": 2, "AMD": 4, "AMZN": 4, "APP": 4, "AVGO": 2, "COIN": 0,
    "HOOD": 0, "IWM": 3, "META": 2, "MSFT": 3, "NFLX": 5, "NVDA": 3,
    "PLTR": 2, "QQQ": 1, "SMH": 0, "SPY": 1, "TSLA": 1,
}


def unit_tests() -> dict[str, object]:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "passed": result.wasSuccessful(),
    }


def validate_json_artifacts(input_files: dict[str, str] | None = None) -> dict[str, object]:
    artifact_paths = list((ROOT / "schemas").glob("*.json")) + [
        ROOT / "frozen_config.json",
        ROOT / "examples" / "feed_manifest.sample.json",
        ROOT / "examples" / "article_presence.sample.jsonl",
        ROOT / "examples" / "structured_events.sample.jsonl",
        ROOT / "examples" / "reference_snapshots.sample.jsonl",
    ]
    if input_files is not None:
        for path in artifact_paths:
            input_files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((ROOT / "schemas").glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
    json.loads((ROOT / "frozen_config.json").read_text(encoding="utf-8"))
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
    decision = qualify_premarket_event(
        articles,
        events,
        references,
        "FIGI:BBG000B9XRY4",
        date(2026, 8, 5),
        calendar,
    )
    if decision.eligible_variants != ("E1", "E2A"):
        raise AssertionError(f"sample event decision drifted: {decision}")
    return {
        "schemas_parse": True,
        "draft_2020_12_schema_validation_executed": False,
        "runtime_contract_validation": True,
        "sample_event_variants": list(decision.eligible_variants),
    }


def _read_csv(path: Path, input_files: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Hash exactly the bytes parsed by a local-data check."""
    payload = path.read_bytes()
    if input_files is not None:
        input_files[str(path)] = hashlib.sha256(payload).hexdigest()
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def golden_macd_parity(input_files: dict[str, str] | None = None) -> dict[str, object]:
    if not LOCKED_DATA.exists():
        return {"status": "SKIPPED_LOCKED_DATA_NOT_PRESENT", "path": str(LOCKED_DATA)}
    actual: dict[str, int] = {}
    for ticker in sorted(EXPECTED_MACD3_LONG):
        detector = MacdThreeWaveDetector(ticker, f"PILOT:{ticker}", not_before=date(2011, 1, 1))
        count = 0
        for row in _read_csv(LOCKED_DATA / f"{ticker.lower()}_1d.csv", input_files):
            output = detector.update(DailyBar(
                ticker=ticker,
                security_id=f"PILOT:{ticker}",
                price_basis_id="locked-raw-split-regime",
                session=date.fromisoformat(row["timestamp"][:10]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
            if output is not None:
                count += 1
        actual[ticker] = count
    if actual != EXPECTED_MACD3_LONG:
        raise AssertionError(f"MACD golden parity failed: expected={EXPECTED_MACD3_LONG}, actual={actual}")
    return {"status": "PASS", "total": sum(actual.values()), "by_ticker": actual}


def _observed_session_calendar(bars: list[IntradayBar]) -> TradingSessionCalendar:
    """Pilot-only schedule: observed dates, 09:30 open, last observed bar end.

    This enables a reproducible detector smoke test but cannot establish that a
    whole session or the final bars are missing. Formal replay must use an
    independently sourced exchange calendar, not this helper.
    """
    closes: dict[date, datetime] = {}
    for bar in bars:
        closes[bar.session] = max(closes.get(bar.session, bar.end), bar.end)
    return TradingSessionCalendar([
        ExchangeSession(
            session,
            datetime.combine(session, time(9, 30), tzinfo=EXCHANGE_TIMEZONE),
            close,
        )
        for session, close in sorted(closes.items())
    ])


def quad_stochastic_locked_data_smoke(input_files: dict[str, str] | None = None) -> dict[str, object]:
    if not LOCKED_DATA.exists():
        return {"status": "SKIPPED_LOCKED_DATA_NOT_PRESENT", "path": str(LOCKED_DATA)}
    total_bars = 0
    total_raw_triggers = 0
    by_ticker: dict[str, int] = {}
    observed_sessions: set[date] = set()
    for ticker in sorted(EXPECTED_MACD3_LONG):
        bars = [
            IntradayBar(
                ticker=ticker,
                security_id=f"PILOT:{ticker}",
                price_basis_id="locked-raw-split-regime",
                start=datetime.fromisoformat(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            for row in _read_csv(LOCKED_DATA / f"{ticker.lower()}_5m.csv", input_files)
        ]
        calendar = _observed_session_calendar(bars)
        observed_sessions.update(calendar.by_date)
        detector = QuadStochasticDetector(calendar)
        triggers = 0
        for bar in bars:
            result = detector.update(bar)
            total_bars += 1
            if result is not None:
                triggers += 1
        by_ticker[ticker] = triggers
        total_raw_triggers += triggers
    expected: dict[str, int] = {}
    if LOCKED_TRADE_LOG.exists():
        for row in _read_csv(LOCKED_TRADE_LOG, input_files):
            if (
                row["strategy"] == "quad_stochastic_reversal"
                and row["variant"] == "quad_full"
                and row["exit_style"] == "stoch80"
            ):
                expected[row["ticker"]] = expected.get(row["ticker"], 0) + 1
        expected = {ticker: expected.get(ticker, 0) for ticker in sorted(EXPECTED_MACD3_LONG)}
    return {
        "status": "PASS_NO_CRASH",
        "bars_processed": total_bars,
        "raw_triggers": total_raw_triggers,
        "by_ticker": by_ticker,
        "observed_first_session": min(observed_sessions).isoformat(),
        "observed_last_session": max(observed_sessions).isoformat(),
        "observed_session_count": len(observed_sessions),
        "calendar_source": "Observed CSV dates; fixed 09:30 ET open and last observed bar end per date.",
        "official_exchange_calendar_completeness_verified": False,
        "point_in_time_price_basis_verified": False,
        "locked_trade_log_total": sum(expected.values()) if expected else None,
        "reference_executed_by_ticker": expected or None,
        "note": "The standalone detector and reference executed-trade simulation have different position-blocking semantics, so their counts are diagnostic and are not asserted as golden parity. The observed-session calendar is only a local smoke-test fixture; it cannot detect wholly missing sessions or truncated session endings. The price-basis label does not certify corporate-action adjustment provenance.",
    }


def end_to_end_cli_smoke() -> dict[str, object]:
    commands = {
        "signal_example": [sys.executable, str(ROOT / "examples" / "run_signal_example.py")],
        "ledger_validator": [
            sys.executable,
            str(ROOT / "validate_ledgers.py"),
            "--manifest", str(ROOT / "examples" / "feed_manifest.sample.json"),
            "--articles", str(ROOT / "examples" / "article_presence.sample.jsonl"),
            "--events", str(ROOT / "examples" / "structured_events.sample.jsonl"),
            "--snapshots", str(ROOT / "examples" / "reference_snapshots.sample.jsonl"),
        ],
    }
    outputs: dict[str, object] = {}
    for name, command in commands.items():
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise AssertionError(f"{name} failed:\n{result.stdout}\n{result.stderr}")
        outputs[name] = json.loads(result.stdout)
    example = outputs["signal_example"]
    if (
        example.get("data_mode") != "SYNTHETIC_TEST_FIXTURE_NOT_MARKET_EVIDENCE"
        or example.get("event_status") != "QUALIFIED_EVENT"
        or example.get("selected_experiment") != "portfolio:strict_primary:E2A+M4"
        or {item["variant"] for item in example.get("signals", [])} != {"E0", "E1", "E2A"}
        or any(example.get(key) is not False for key in (
            "profitability_validated", "external_agent_called", "orders_submitted",
        ))
    ):
        raise AssertionError("signal example did not demonstrate the frozen synthetic scenario")
    intents = example.get("order_intents", [])
    if (
        len(intents) != 1
        or intents[0]["variant"] != "E2A"
        or intents[0]["quantity"] != 68
        or not 0 < intents[0]["risk_dollars"] <= 250.0
        or intents[0]["submitted_at"] != "2026-08-05T09:55:00-04:00"
    ):
        raise AssertionError("synthetic route -> risk -> order-intent flow drifted")
    exits = example.get("exit_decisions", [])
    if (
        [item["reason"] for item in exits] != ["day5_half", "ema10_next_open"]
        or [item["fraction"] for item in exits] != [0.5, 0.5]
    ):
        raise AssertionError("synthetic exit-policy flow drifted")
    if outputs["ledger_validator"].get("status") != "PASS_CONTRACT_VALIDATION":
        raise AssertionError("ledger validator did not report runtime-contract success")
    return {"status": "PASS", "outputs": outputs}


def package_manifest() -> dict[str, str]:
    paths = [
        path for path in ROOT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "results" not in path.parts
        and (path.suffix in {".py", ".json", ".jsonl", ".md", ".toml"} or path.name == ".gitignore")
    ]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def main() -> int:
    checks: dict[str, object] = {}
    input_files: dict[str, str] = {}
    passed = False
    stage = "compile"
    try:
        checks[stage] = compileall.compile_dir(str(ROOT / "scenario_router"), quiet=1)
        stage = "frozen_config"
        _, config_hash = load_frozen_config(ROOT / "frozen_config.json")
        input_files[str(ROOT / "frozen_config.json")] = config_hash
        checks[stage] = {
            "status": "PASS",
            "strategy_version": STRATEGY_VERSION,
            "sha256": config_hash,
        }
        for stage, check in (
            ("unit_tests", unit_tests),
            ("contracts", lambda: validate_json_artifacts(input_files)),
            ("end_to_end_cli", end_to_end_cli_smoke),
            ("macd_golden_parity", lambda: golden_macd_parity(input_files)),
            ("quad_stochastic_locked_data_smoke", lambda: quad_stochastic_locked_data_smoke(input_files)),
        ):
            checks[stage] = check()
        passed = bool(
            checks["compile"]
            and checks["unit_tests"]["passed"]
            and checks["end_to_end_cli"]["status"] == "PASS"
            and checks["macd_golden_parity"]["status"] in {"PASS", "SKIPPED_LOCKED_DATA_NOT_PRESENT"}
            and checks["quad_stochastic_locked_data_smoke"]["status"] in {"PASS_NO_CRASH", "SKIPPED_LOCKED_DATA_NOT_PRESENT"}
        )
    except Exception as exc:
        checks[stage] = {"status": "FAIL", "error_type": type(exc).__name__, "message": str(exc)}
    try:
        manifest = package_manifest()
    except Exception as exc:
        passed = False
        manifest = {}
        checks["package_manifest"] = {"status": "FAIL", "error_type": type(exc).__name__, "message": str(exc)}
    reference_data_exercised = bool(
        checks.get("macd_golden_parity", {}).get("status") == "PASS"
        and checks.get("quad_stochastic_locked_data_smoke", {}).get("status") == "PASS_NO_CRASH"
    )
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": ("PASS_REFERENCE_LOGIC" if reference_data_exercised else "PASS_SYNTHETIC_ONLY") if passed else "FAIL",
        "scope": "Selected reference-logic/runtime-contract checks only. Schema documents are JSON-parsed, not Draft 2020-12 evaluated. A PASS is not data-feed certification, an Agent release, profitability evidence, or live-trading readiness.",
        "checks": checks,
        "input_files_sha256": input_files,
        "package_sha256": manifest,
        "verification_boundaries": {
            "locked_market_data_exercised": reference_data_exercised,
            "raw_provider_source_bytes_verified": False,
            "reference_snapshot_source_completeness_verified": False,
            "official_exchange_calendar_completeness_verified": False,
            "point_in_time_universe_and_price_basis_verified": False,
            "real_agent_extraction_executed": False,
            "real_agent_independent_review_executed": False,
            "agent_review_artifact_verified": False,
            "profitability_verified": False,
            "live_execution_executed": False,
        },
        "environment": {
            "python": sys.version,
            "lean_cli_executed": False,
            "quantconnect_cloud_executed": False,
        },
    }
    output = ROOT / "results" / "validation_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
