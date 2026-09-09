#!/usr/bin/env python3
"""Portable JSON interface to the frozen, local Scenario Router research engine."""
from __future__ import annotations

import argparse
import ast
import contextlib
from datetime import date
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "runtime"
SCHEMA_VERSION = "secondary-research-cli-v1"
BACKTEST_FILES = (
    "calendar.csv", "universe.csv", "daily_bars.csv", "intraday_bars.csv",
    "quotes.csv", "feed_manifest.json", "articles.jsonl", "events.jsonl",
    "reference_snapshots.jsonl",
)


class CommandError(Exception):
    def __init__(self, code, message, hint, *, exit_code=2, data=None, level="NONE"):
        super().__init__(message)
        self.code, self.hint, self.exit_code = code, hint, exit_code
        self.data, self.level = data, level


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise CommandError("INVALID_ARGUMENT", message,
                           "Run research.py describe or the command's --help.")


def finite_number(raw):
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("a finite number is required") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("a finite number is required")
    return value


def positive_number(raw):
    value = finite_number(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("a positive number is required")
    return value


def nonnegative_number(raw):
    value = finite_number(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("a nonnegative number is required")
    return value


def slippage_values(raw):
    values = []
    for token in raw.split(","):
        value = nonnegative_number(token)
        if value >= 10000:
            raise argparse.ArgumentTypeError("slippage must be below 10000 bps")
        if value not in values:
            values.append(value)
    return ",".join(str(value) for value in values)


def nonempty(raw):
    if not raw.strip():
        raise argparse.ArgumentTypeError("a nonempty value is required")
    return raw


def nonnegative_integer(raw):
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("a nonnegative integer is required") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("a nonnegative integer is required")
    return value


def build_parser():
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--timeout", type=positive_number, default=180.0,
                        help="total command timeout in seconds; put before the command")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("describe", help="machine-readable command and input contracts")
    commands.add_parser("doctor", help="verify Python, timezone and both package manifests")
    commands.add_parser("validate", help="run original unit and runtime checks in a temporary copy")
    demo = commands.add_parser("demo", help="run a bundled synthetic example offline")
    demo.add_argument("--kind", choices=("signal", "paper"), default="signal")
    backtest = commands.add_parser("backtest", help="replay caller-supplied local canonical data")
    backtest.add_argument("--data", required=True, help="canonical input directory")
    backtest.add_argument("--output", required=True, help="new or empty output directory")
    backtest.add_argument("--start", required=True, type=date.fromisoformat)
    backtest.add_argument("--end", required=True, type=date.fromisoformat)
    backtest.add_argument("--event-variant", choices=("E2A", "E2B"), default="E2A")
    backtest.add_argument("--reversal-variant", choices=("M2", "M3", "M4"), default="M4")
    backtest.add_argument("--event-mode", choices=("strict_primary", "agent_assisted_secondary"), default="strict_primary")
    backtest.add_argument("--coverage-mode", choices=("retrospective_audit", "point_in_time"), default="retrospective_audit")
    backtest.add_argument("--data-mode", choices=("historical_point_in_time", "synthetic_fixture"), required=True)
    backtest.add_argument("--initial-cash", type=positive_number, default=100000.0)
    backtest.add_argument("--slippage-bps", type=slippage_values, default="0,10,25,50")
    backtest.add_argument("--commission-per-share", type=nonnegative_number, default=0.0)
    backtest.add_argument("--minimum-commission", type=nonnegative_number, default=0.0)
    backtest.add_argument("--code-revision", required=True, type=nonempty,
                          help="immutable source revision; historical mode requires 40 or 64 hex characters")
    evidence = commands.add_parser("evidence", help="local saved evidence workflow; provider is off by default")
    actions = evidence.add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ("analyze", "correct", "status"):
        action = actions.add_parser(name)
        action.add_argument("--database", required=True, help="SQLite path; status requires an existing database")
        action.add_argument("--conversation", required=True, type=nonempty)
        action.add_argument("--question", type=nonempty,
                            default="请核对最新材料，解释对事件延续策略的影响。")
        action.add_argument("--output", help="new file for full raw evidence; contains caller-supplied source text")
        if name != "status":
            action.add_argument("--bundle", required=True, help="local evidence bundle JSON")
            action.add_argument("--expected-revision", type=nonnegative_integer, required=name == "correct")
            action.add_argument("--provider", choices=("off", "codex"), default="off",
                                help="codex explicitly enables the original external model adapter")
    return parser


def envelope(command, *, data=None, level="NONE", error=None):
    return {"schema_version": SCHEMA_VERSION, "ok": error is None,
            "command": command, "evidence_level": level, "data": data, "error": error}


def failure(command, exc):
    if isinstance(exc, CommandError):
        error = exc
    elif isinstance(exc, (ValueError, TypeError, KeyError, FileNotFoundError,
                          FileExistsError, NotADirectoryError, IsADirectoryError)):
        error = CommandError("INVALID_INPUT", str(exc),
                             "Check the input contract and choose a new output path.")
    elif isinstance(exc, sqlite3.Error):
        error = CommandError("DATABASE_ERROR", str(exc),
                             "Use a valid Scenario Router SQLite database and retry after any writer finishes.")
    else:
        error = CommandError("RUNTIME_ERROR", f"{type(exc).__name__}: {exc}",
                             "Run doctor and validate; inspect the supplied local inputs.", exit_code=3)
    return {"envelope": envelope(command, data=error.data, level=error.level,
                                error={"code": error.code, "message": str(error), "hint": error.hint}),
            "exit_code": error.exit_code}


def _command_name(args):
    return args["command"] + (" " + args["action"] if args.get("action") else "")


def _describe():
    def description(parser):
        fields, children = [], {}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                children = {name: description(child) for name, child in action.choices.items()}
            elif action.dest != "help":
                fields.append({"name": action.dest, "flags": action.option_strings,
                               "required": action.required,
                               "default": action.default,
                               "type": getattr(action.type, "__name__", "string"),
                               "choices": list(action.choices) if action.choices else None,
                               "help": action.help})
        return {"arguments": fields, "commands": children}
    return {"interface": description(build_parser()), "python_minimum": "3.11",
            "exit_codes": {"0": "command completed; evidence labels still apply",
                           "2": "invalid argument, input, integrity or validation failure",
                           "3": "dependency, provider or runtime failure including timeout"},
            "stdout": "Exactly one JSON envelope, except --help human-readable text.",
            "paths": "CLI paths resolve against caller cwd; source paths inside a bundle resolve against its directory.",
            "writes": {"describe": "none", "doctor": "none", "validate": "temporary copy, removed afterward",
                       "demo": "temporary paper snapshot only", "backtest": "new or empty output directory only",
                       "evidence analyze/correct": "append revision to SQLite; optional new raw JSON file",
                       "evidence status": "read-only SQLite; optional new raw JSON file"},
            "input_contracts": {
                "backtest": {"required_files": list(BACKTEST_FILES), "reference": "runtime/BACKTEST.md",
                             "historical_revision": "40 or 64 hexadecimal characters, declaration not independently verified",
                             "coverage": "point_in_time permits one pre-open session only and rejects M4",
                             "data_certification": "Caller data_mode is a declaration, not independently verified market provenance."},
                "evidence": {"bundle_keys": ["bundle_id", "security_id", "ticker", "fiscal_period", "lane", "sources"],
                             "lanes": ["historical_reconstruction", "forward_capture", "synthetic"],
                             "fiscal_period": "FY followed by four digits",
                             "sources": "Exactly one prior and one current source; original full-byte sha256: hash required.",
                             "source_keys": ["source_id", "role", "path", "sha256", "url", "published_at", "captured_at"],
                             "limits": "Each UTF-8 source is 1..300000 bytes inside bundle directory; timezone-aware capture must follow publication and not be future.",
                             "reference": "references/agent-contract.md"}},
            "evidence_levels": ["INTERFACE_METADATA", "LOCAL_ENVIRONMENT_CHECKS", "LOCAL_SYNTHETIC_VALIDATION",
                                "SYNTHETIC_FIXTURE_ONLY", "USER_SUPPLIED_HISTORICAL_REPLAY", "SAVED_STATE_ONLY",
                                "MODEL_REVIEW_ONLY", "NO_MODEL_EVIDENCE", "NONE"],
            "network": "Only explicit evidence analyze/correct --provider codex may call an external model; no automatic installs or login.",
            "scope": "No broker, live order, market data download, profitability or production certification."}


def verify_manifest(root):
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file():
        return {"ok": False, "status": "MANIFEST_MISSING", "checked_files": 0, "problems": ["MANIFEST.sha256"]}
    problems, seen = [], set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            problems.append("invalid manifest entry")
            continue
        expected, name = match.groups()
        path = root / name
        if (name in seen or path.is_symlink() or Path(name).is_absolute()
                or ".." in Path(name).parts or not path.resolve().is_relative_to(root.resolve())):
            problems.append(f"unsafe or duplicate entry: {name}")
            continue
        seen.add(name)
        if not path.is_file():
            problems.append(f"missing: {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            problems.append(f"hash mismatch: {name}")
    if not seen:
        problems.append("empty manifest")
    return {"ok": not problems, "status": "PASS" if not problems else "MANIFEST_MISMATCH",
            "checked_files": len(seen), "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "problems": problems}


def _doctor():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        timezone_ok = ZoneInfo("America/New_York").key == "America/New_York"
    except ZoneInfoNotFoundError:
        timezone_ok = False
    version = None
    for node in ast.parse((RUNTIME / "scenario_router/__init__.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            version = ast.literal_eval(node.value)
    data = {"python": {"version": sys.version.split()[0], "supported": sys.version_info >= (3, 11)},
            "engine_version": version, "timezone": {"name": "America/New_York", "available": timezone_ok},
            "runtime_manifest": verify_manifest(RUNTIME), "skill_manifest": verify_manifest(SKILL),
            "external_model_called": False}
    if not data["python"]["supported"] or not timezone_ok:
        raise CommandError("DEPENDENCY_UNAVAILABLE", "Python 3.11+ and America/New_York timezone data are required.",
                           f"Use Python 3.11+. If timezone data is missing, install OS tzdata or manually run: {sys.executable} -m pip install tzdata. Nothing is installed automatically.",
                           exit_code=3, data=data, level="LOCAL_ENVIRONMENT_CHECKS")
    if not data["runtime_manifest"]["ok"] or not data["skill_manifest"]["ok"]:
        raise CommandError("INTEGRITY_CHECK_FAILED", "One or more package manifest checks failed.",
                           "Use an intact packaged release, or regenerate the skill manifest after intentional development changes.",
                           data=data, level="LOCAL_ENVIRONMENT_CHECKS")
    return data, "LOCAL_ENVIRONMENT_CHECKS"


def _subprocess(command, *, cwd, timeout):
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise CommandError("TIMEOUT", "Local runtime check exceeded the command timeout.",
                           "Retry with --timeout before the command if more time is appropriate.", exit_code=3) from None
    return process.returncode, stdout, stderr


def _validate(args):
    integrity = verify_manifest(RUNTIME)
    if not integrity["ok"]:
        raise CommandError("INTEGRITY_CHECK_FAILED", "Frozen runtime manifest failed.", "Restore the original runtime.", data=integrity)
    with tempfile.TemporaryDirectory(prefix="scenario-validation-") as temporary:
        root = Path(temporary) / "runtime"
        shutil.copytree(RUNTIME, root, ignore=shutil.ignore_patterns("__pycache__", "results", "*.pyc"))
        reports = {}
        for name, report_name in (("validate.py", "validation_report.json"), ("validate_runtime.py", "validation_runtime_report.json")):
            code, stdout, stderr = _subprocess([sys.executable, "-B", str(root / name)], cwd=root, timeout=args["timeout"])
            report_path = root / "results" / report_name
            if not report_path.is_file():
                raise CommandError("VALIDATION_RUNTIME_FAILED", f"{name} produced no report (exit {code}).",
                                   "Run doctor to check runtime dependencies.", exit_code=3,
                                   data={"reports": reports, "stderr_tail": stderr[-4000:]})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            # Temporary paths are diagnostic provenance, not persistent artifact links.
            reports[name] = {"exit_code": code, "report": report}
        data = {"reports": reports, "temporary_copy_removed": True,
                "market_data_certified": False, "profitability_verified": False,
                "live_orders": False, "real_model_called": False}
        if any(item["exit_code"] != 0 for item in reports.values()):
            raise CommandError("VALIDATION_FAILED", "One or more original checks failed.",
                               "Inspect data.reports for the first failing check.", data=data, level="LOCAL_SYNTHETIC_VALIDATION")
        return data, "LOCAL_SYNTHETIC_VALIDATION"


def _demo(args):
    code, stdout, stderr = _subprocess([sys.executable, "-B", str(RUNTIME / "examples" / f"run_{args['kind']}_example.py")],
                                     cwd=RUNTIME, timeout=args["timeout"])
    if code:
        raise CommandError("DEMO_FAILED", f"Synthetic example exited {code}.", "Run doctor and validate.",
                           exit_code=3, data={"stderr_tail": stderr[-4000:]})
    return json.loads(stdout), "SYNTHETIC_FIXTURE_ONLY"


def _new_file(path):
    if path.exists() or path.is_symlink():
        raise CommandError("OUTPUT_EXISTS", f"Refusing to overwrite: {path}", "Choose a new output file.")


def _empty_directory(path):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise CommandError("OUTPUT_EXISTS", f"Output must be new or empty: {path}", "Choose a new or empty output directory.")


def _backtest(args):
    import backtest_cli
    from scenario_router.backtest import BacktestConfig, BacktestDataset
    from scenario_router.paper import CostModel
    output = Path(args["output"])
    _empty_directory(output)
    BacktestConfig(start_session=date.fromisoformat(args["start"]), end_session=date.fromisoformat(args["end"]),
                   initial_cash=args["initial_cash"], event_variant=args["event_variant"],
                   reversal_variant=args["reversal_variant"], event_mode=args["event_mode"],
                   coverage_mode=args["coverage_mode"], data_mode=args["data_mode"], code_revision=args["code_revision"])
    BacktestDataset.from_directory(args["data"])
    for value in args["slippage_bps"].split(","):
        CostModel(float(value), args["commission_per_share"], args["minimum_commission"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".scenario-backtest-", dir=output.parent) as temporary:
        staged = Path(temporary) / "result"
        flags = []
        for key in ("data", "start", "end", "event_variant", "reversal_variant", "event_mode",
                    "coverage_mode", "data_mode", "initial_cash", "slippage_bps",
                    "commission_per_share", "minimum_commission", "code_revision"):
            flags.extend(["--" + key.replace("_", "-"), str(args[key])])
        flags.extend(["--output", str(staged)])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = backtest_cli.main(flags)
        if code:
            raise CommandError("BACKTEST_FAILED", f"Original engine returned {code}.", "Inspect the input contract.", exit_code=3)
        result = json.loads(stream.getvalue())
        _empty_directory(output)
        # Rename is atomic on the same filesystem and cannot replace a nonempty directory.
        staged.rename(output)
    level = "SYNTHETIC_FIXTURE_ONLY" if args["data_mode"] == "synthetic_fixture" else "USER_SUPPLIED_HISTORICAL_REPLAY"
    return {"output": str(output), "summary": result, "data_provenance_independently_verified": False,
            "profitability_verified": False, "live_orders": False}, level


def _evidence(args):
    from scenario_router.ai_workflow import AgentWorkflow, stamp
    from scenario_router.model_provider import CodexExecProvider, DisabledProvider
    database = Path(args["database"])
    output = Path(args["output"]) if args.get("output") else None
    if output:
        _new_file(output)
    if args["action"] == "status":
        if not database.is_file():
            raise CommandError("DATABASE_NOT_FOUND", f"Database does not exist: {database}",
                               "Analyze a local bundle first, or supply an existing database.")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute("SELECT payload FROM runs WHERE conversation=? ORDER BY revision DESC LIMIT 1",
                                     (args["conversation"],)).fetchone()
        result = json.loads(row[0]) if row else {"state": "EMPTY", "revision": 0, "e2b_screen": False,
                                                "message": "尚未分析材料，没有可沿用的仓位。"}
        result["query_receipt"] = {"question": args["question"], "at": stamp(), "kind": "LATEST_SAVED_ANALYSIS", "new_model_analysis": False}
        result["message"] = "这是最新保存的分析状态，本次查询未重新调用模型。" + result["message"]
        level = "SAVED_STATE_ONLY"
    else:
        provider = CodexExecProvider() if args["provider"] == "codex" else DisabledProvider()
        flow = AgentWorkflow(database, provider)
        result = flow.analyze(args["conversation"], args["bundle"], question=args["question"],
                              expected_revision=args.get("expected_revision"))
        level = "MODEL_REVIEW_ONLY" if result["state"] == "CHECKED_CANDIDATE" else "NO_MODEL_EVIDENCE"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects existing content even after a concurrent write.
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    data = {key: result[key] for key in ("run_id", "revision", "state", "message", "e2b_screen", "query_receipt", "reason", "error_type", "comparisons") if key in result}
    data.update(database=str(database), output=str(output) if output else None,
                provider_call_attempts=len(result.get("calls", [])),
                returned_model_responses=sum("raw" in call for call in result.get("calls", [])))
    if args["action"] != "status" and result["state"] == "BLOCKED":
        reason = result.get("reason", "EVIDENCE_BLOCKED")
        provider_failure = result.get("error_type") == "ProviderError"
        code = reason if re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", reason) else "EVIDENCE_BLOCKED"
        raise CommandError(code, result["message"],
                           "State was saved. Inspect it with evidence status; explicitly select --provider codex only when a model call is intended.",
                           exit_code=3 if provider_failure else 2, data=data, level=level)
    return data, level


def _dispatch(args):
    if args["command"] == "describe":
        return _describe(), "INTERFACE_METADATA"
    if sys.version_info < (3, 11):
        raise CommandError("PYTHON_VERSION_UNSUPPORTED", "Python 3.11 or newer is required.",
                           "Run with a supported Python interpreter.", exit_code=3)
    if args["command"] == "doctor":
        return _doctor()
    sys.path.insert(0, str(RUNTIME))
    return {"validate": _validate, "demo": _demo, "backtest": _backtest, "evidence": _evidence}[args["command"]](args)


def _worker():
    args = json.load(sys.stdin)
    command = _command_name(args)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data, level = _dispatch(args)
        result = {"envelope": envelope(command, data=data, level=level), "exit_code": 0}
    except (Exception, SystemExit) as exc:
        result = failure(command, exc)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = "unknown"
    try:
        args = vars(build_parser().parse_args(argv))
        command = _command_name(args)
        for key in ("data", "output", "database", "bundle"):
            if args.get(key):
                # Preserve lexical output symlinks so overwrite checks can reject them.
                path = Path(args[key]).expanduser()
                args[key] = str(Path(os.path.abspath(path)))
        for key in ("start", "end"):
            if key in args:
                args[key] = args[key].isoformat()
        worker = [sys.executable, "-B", "-c", "import runpy,sys; runpy.run_path(sys.argv[1])['_worker']()", str(Path(__file__).resolve())]
        process = subprocess.Popen(worker, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", start_new_session=os.name == "posix",
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            stdout, stderr = process.communicate(json.dumps(args, ensure_ascii=False), timeout=args["timeout"])
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise CommandError("TIMEOUT", f"Command exceeded {args['timeout']} seconds.",
                               "Inspect any saved evidence revision before retrying; a timed-out evidence workflow may remain RUNNING.", exit_code=3) from None
        if process.returncode:
            raise CommandError("WORKER_FAILED", f"Research worker exited {process.returncode}.",
                               "Run doctor using Python 3.11 or newer.", exit_code=3)
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise CommandError("INVALID_WORKER_RESPONSE", "Research worker did not return valid JSON.",
                               "Use an intact release and run doctor.", exit_code=3) from None
    except Exception as exc:
        result = failure(command, exc)
    print(json.dumps(result["envelope"], ensure_ascii=False, allow_nan=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
