#!/usr/bin/env python3
"""JSON dispatcher for the Market Data Toolkit; no provider is called by default."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
from tool_contracts import ToolError, read_json, redact, redact_text, secret_values, sha256, validate, verify_manifest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "market-data-cli-v1"


def registry():
    return json.loads((ROOT / "tool_registry.json").read_text(encoding="utf-8"))


def get_tool(name):
    for tool in registry()["tools"]:
        if tool["id"] == name:
            return tool
    raise ToolError("UNKNOWN_TOOL", "Tool is not in the static registry.", hint="Run list; arbitrary module or code names are not accepted.")


def envelope(command, data=None, error=None, level="INTERFACE_METADATA"):
    return {"schema_version": SCHEMA_VERSION, "ok": error is None, "command": command,
            "evidence_level": level, "data": data, "error": error}


def error_envelope(command, error):
    if not isinstance(error, ToolError):
        error = ToolError(type(error).__name__.upper(), str(error), exit_code=3,
                          hint="Inspect doctor TOOL and the source limitations; no automatic retry or dependency installation occurs.")
    return envelope(command, error={"code": error.code, "message": str(error), "hint": error.hint}, level="NONE"), error.exit_code


def _available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def doctor(name=None):
    tools = [get_tool(name)] if name else registry()["tools"]
    modules = {entry["module"]: entry["distribution"] for tool in tools for entry in tool["requires"]["packages"]}
    keys = sorted({key for tool in tools for key in tool["requires"]["environment"]})
    return {"python": {"version": sys.version.split()[0], "supported": sys.version_info >= (3, 11)},
            "dependencies": [{"module": module, "distribution": distribution, "available": _available(module)} for module, distribution in sorted(modules.items())],
            "environment": [{"name": key, "present": bool(os.environ.get(key))} for key in keys],
            "tool_status": [{"id": tool["id"], "status": tool["status"], "blocking_reason": tool["blocking_reason"]} for tool in tools],
            "runtime_integrity": source_integrity(),
            "skill_integrity": verify_manifest(ROOT), "network_called": False, "model_called": False,
            "interpretation": "Availability and integrity diagnostics only; no optional imports, provider authentication or endpoint checks."}


def source_integrity():
    result = verify_manifest(ROOT / "runtime")
    try:
        provenance = read_json(ROOT / "SOURCE_PROVENANCE.json")
        actual = sha256(ROOT / "runtime" / "MANIFEST.sha256")
        if actual != provenance.get("source_manifest_sha256"):
            result["problems"].append("SOURCE_MANIFEST_DIGEST_MISMATCH")
    except Exception:
        result["problems"].append("SOURCE_PROVENANCE_INVALID")
    result["ok"] = not result["problems"]
    return result


def relocate(value, old, new):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [relocate(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: relocate(item, old, new) for key, item in value.items()}
    return value


def prepare_request(name, args, *, allow_network=False, allow_model=False, check_dependencies=True):
    spec = get_tool(name)
    if spec["status"] != "wrapped":
        raise ToolError("TOOL_BLOCKED", spec["blocking_reason"], hint="Use the replacement tool named in blocking_reason.")
    validate(args, spec["parameters"])
    if spec["permissions"]["network"] and not allow_network:
        raise ToolError("NETWORK_PERMISSION_REQUIRED", "This selected tool accesses an external service.",
                        hint="Add --allow-network only when this task authorizes that provider request.")
    if spec["permissions"]["model"] and not allow_model:
        raise ToolError("MODEL_PERMISSION_REQUIRED", "This selected tool requires model access.",
                        hint="Explicit model authorization is required; no models are enabled by default.")
    args = dict(args)
    for field, schema in spec["parameters"]["properties"].items():
        if field not in args and "default" in schema:
            args[field] = schema["default"]
        if field in args and schema.get("format") in {"local-image", "local-document"}:
            args[field] = str(Path(args[field]).expanduser().resolve())
    if name == "rag.retrieve_local":
        args["paths"] = [str(Path(path).expanduser().resolve()) for path in args["paths"]]
    if "start_date" in args and "end_date" in args and args["start_date"] > args["end_date"]:
        raise ToolError("INVALID_DATE_RANGE", "start_date must not follow end_date.")
    if name == "text.check_text_length" and args.get("min_length", 0) > args.get("max_length", 100000):
        raise ToolError("INVALID_ARGUMENT", "min_length must not exceed max_length.")
    if name == "backtrader.back_test":
        params = args.get("strategy_params", {})
        if params.get("fast", 10) >= params.get("slow", 30):
            raise ToolError("INVALID_ARGUMENT", "SMA_CrossOver fast must be less than slow.")
    if check_dependencies:
        missing = [dep["distribution"] for dep in spec["requires"]["packages"] if not _available(dep["module"])]
        if missing:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "Missing optional distributions: " + ", ".join(missing), exit_code=3,
                            hint="Select the requirements group documented for this tool in references/tool-contract.md; installs are never automatic.")
        required = list(spec["requires"]["environment"])
        if name == "sec.get_10k_section" and not args.get("report_address"):
            required.append("FMP_API_KEY")
            for module in ("numpy",):
                if not _available(module):
                    raise ToolError("DEPENDENCY_UNAVAILABLE", "FMP fallback also requires numpy.", exit_code=3)
        missing_keys = [name for name in required if not os.environ.get(name)]
        if missing_keys:
            raise ToolError("CREDENTIAL_UNAVAILABLE", "Missing environment variables: " + ", ".join(missing_keys), exit_code=3,
                            hint="Configure credentials outside the repository; values are never accepted as CLI JSON parameters.")
    return spec, args


def new_output(path):
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise ToolError("OUTPUT_EXISTS", "Output directory already exists; refusing to overwrite.", hint="Select a new output directory for this run.")
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise ToolError("OUTPUT_INSIDE_SKILL", "Keep run outputs outside the installed Skill.")
    return path


def _safe_artifacts(workdir):
    # Caches can contain opaque authentication state. They are never published.
    for name in (".cache", "inputs"):
        path = workdir / name
        if path.exists():
            shutil.rmtree(path)
    secrets = [value.encode() for value in secret_values()]
    text_suffixes = {".txt", ".md", ".json", ".jsonl", ".csv", ".html", ".htm", ".log"}
    for path in workdir.rglob("*"):
        if path.is_symlink():
            raise ToolError("UNSAFE_ARTIFACT", "The provider generated a symbolic-link artifact.", exit_code=3)
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if path.suffix.lower() in text_suffixes:
            try:
                cleaned = redact_text(payload.decode("utf-8"))
            except UnicodeError:
                raise ToolError("UNSAFE_ARTIFACT", "A text artifact is not UTF-8.", exit_code=3) from None
            path.write_text(cleaned, encoding="utf-8")
        elif any(secret in payload for secret in secrets):
            raise ToolError("SENSITIVE_ARTIFACT", "A binary artifact contains a configured credential; the run was not published.", exit_code=3)


def execute_in_stage(spec, args, workdir, dispatcher=None):
    """Private test seam: callers can supply a fake dispatcher; the CLI cannot select one."""
    if dispatcher is None:
        from tool_adapters import dispatch
        dispatcher = dispatch
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        result = dispatcher(spec, args, workdir)
    if result is None:
        raise ToolError("UPSTREAM_EMPTY", "No tool result was produced.", exit_code=3)
    result = redact(result)
    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _safe_artifacts(workdir)
    artifacts = [{"path": str(path.relative_to(workdir)), "sha256": sha256(path), "bytes": path.stat().st_size}
                 for path in sorted(workdir.rglob("*")) if path.is_file()]
    return {"result": result, "artifacts": artifacts,
            "upstream_log_tail": redact_text(captured.getvalue())[-6000:],
            "warnings": spec["warnings"], "status_meaning": "Dispatcher completed; provider output is not independently verified market evidence."}


def _worker():
    payload = json.loads(sys.stdin.read())
    command = "run " + payload["tool"]
    try:
        spec, args = prepare_request(payload["tool"], payload["args"], allow_network=payload["allow_network"], allow_model=payload["allow_model"])
        workdir = Path(payload["stage"])
        os.chdir(workdir)
        data = execute_in_stage(spec, args, workdir)
        level = "EXTERNAL_PROVIDER_OUTPUT_UNVERIFIED" if spec["permissions"]["network"] else "LOCAL_DOCUMENT_PROCESSING"
        answer, code = envelope(command, data=data, level=level), 0
    except Exception as exc:
        answer, code = error_envelope(command, exc)
    print(json.dumps(redact(answer), ensure_ascii=False, allow_nan=False))
    return code


def _stop(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.communicate()


def run(name, args_file, output, *, allow_network, allow_model, timeout):
    args = read_json(args_file)
    # Validate all permissions and output collisions before optional modules are imported.
    spec, args = prepare_request(name, args, allow_network=allow_network, allow_model=allow_model)
    output = new_output(output)
    if not source_integrity()["ok"] or not verify_manifest(ROOT)["ok"]:
        raise ToolError("INTEGRITY_CHECK_FAILED", "The Skill or original runtime snapshot is incomplete or modified.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".market-tool-", dir=output.parent) as directory:
        stage = Path(directory) / "run"
        stage.mkdir()
        payload = {"tool": name, "args": args, "allow_network": allow_network,
                   "allow_model": allow_model, "stage": str(stage)}
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--internal-worker"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", start_new_session=os.name == "posix")
        try:
            stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(process)
            raise ToolError("TIMEOUT", "The bounded provider run exceeded the timeout; no result directory was published.", exit_code=3) from None
        try:
            answer = json.loads(stdout)
        except ValueError:
            raise ToolError("INVALID_WORKER_RESPONSE", "The provider worker did not return its JSON envelope.", exit_code=3) from None
        if process.returncode or not answer["ok"]:
            err = answer.get("error") or {}
            raise ToolError(err.get("code", "WORKER_FAILED"), err.get("message", "Worker failed."),
                            exit_code=process.returncode if process.returncode in (2, 3) else 3,
                            hint=err.get("hint", "Inspect doctor TOOL."))
        # Stage paths in results must remain usable after publication.
        old, new = str(stage), str(output)
        answer = relocate(answer, old, new)
        for path in stage.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".txt", ".md", ".html", ".htm", ".csv", ".log"}:
                text = path.read_text(encoding="utf-8")
                if path.suffix == ".json":
                    text = json.dumps(relocate(json.loads(text), old, new), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                else:
                    text = text.replace(old, new)
                path.write_text(text, encoding="utf-8")
        receipt = {"tool": name, "completed_at": datetime.now(timezone.utc).isoformat(),
                   "args_file_sha256": sha256(args_file), "source_manifest_sha256": sha256(ROOT / "runtime" / "MANIFEST.sha256"),
                   "network_authorized": allow_network, "model_authorized": allow_model,
                   "network_required": spec["permissions"]["network"], "model_required": spec["permissions"]["model"],
                   "market_data_certified": False, "live_orders": False}
        (stage / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Reserve the target exclusively. Never replace a directory that appeared during the run.
        try:
            output.mkdir()
        except FileExistsError:
            raise ToolError("OUTPUT_EXISTS", "Output appeared during the run; refusing to overwrite.") from None
        try:
            for child in stage.iterdir():
                child.rename(output / child.name)
        except Exception:
            # Only remove the directory created by this call; never preexisting output.
            shutil.rmtree(output)
            raise
        answer["data"]["artifacts"] = [{"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
                                       for path in sorted(output.rglob("*")) if path.is_file()]
        answer["data"]["output"] = str(output)
        return answer


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("INVALID_ARGUMENT", message)


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument("--timeout", type=float, default=180, help="total run timeout in seconds; put before command (default 180)")
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List every wrapped/blocked tool without optional imports or network")
    for name in ("describe", "doctor"):
        selected = sub.add_parser(name)
        selected.add_argument("tool", nargs="?")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("tool")
    run_parser.add_argument("--args-file", required=True)
    run_parser.add_argument("--output", required=True, help="new directory outside the installed Skill")
    run_parser.add_argument("--allow-network", action="store_true")
    run_parser.add_argument("--allow-model", action="store_true")
    return cli


def main(argv=None):
    if argv is None and sys.argv[1:] == ["--internal-worker"]:
        return _worker()
    command = "parse"
    try:
        args = parser().parse_args(argv)
        command = args.command + (" " + args.tool if getattr(args, "tool", None) else "")
        if sys.version_info < (3, 11):
            raise ToolError("PYTHON_VERSION_UNSUPPORTED", "Python 3.11 or newer is required.", exit_code=3)
        if not 0 < args.timeout <= 3600:
            raise ToolError("INVALID_ARGUMENT", "timeout must be greater than 0 and at most 3600 seconds.")
        if args.command == "list":
            tools = registry()["tools"]
            data = {"tools": [{key: item[key] for key in ("id", "description", "status", "blocking_reason", "requires", "permissions")} for item in tools],
                    "counts": {"total": len(tools), "wrapped": sum(t["status"] == "wrapped" for t in tools), "blocked": sum(t["status"] == "blocked" for t in tools)}}
            answer = envelope(command, data=data)
        elif args.command == "describe":
            data = get_tool(args.tool) if args.tool else {**registry(), "python_minimum": "3.11",
                "json_envelope": SCHEMA_VERSION, "exit_codes": {"0": "dispatcher completed, evidence labels still apply", "2": "input, permissions, output collision or integrity failure", "3": "dependency, credentials, upstream or timeout failure"},
                "commands": ["list", "describe [TOOL]", "doctor [TOOL]", "run TOOL --args-file FILE --output NEWDIR [--allow-network] [--allow-model]"],
                "timeout": "--timeout SECONDS before command; default 180, maximum 3600",
                "stdout": "one JSON envelope, including --help", "scope": "No brokerage, arbitrary code, automatic dependency install or account login."}
            answer = envelope(command, data=data)
        elif args.command == "doctor":
            answer = envelope(command, data=doctor(args.tool), level="LOCAL_ENVIRONMENT_CHECKS")
        else:
            answer = run(args.tool, args.args_file, args.output, allow_network=args.allow_network, allow_model=args.allow_model, timeout=args.timeout)
        code = 0
    except SystemExit as exc:
        # argparse help is captured by the entry point and represented as JSON as well.
        return int(exc.code or 0)
    except Exception as exc:
        answer, code = error_envelope(command, exc)
    print(json.dumps(redact(answer), ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main()
        print(json.dumps(envelope("help", data={"text": buffer.getvalue()}), ensure_ascii=False))
        raise SystemExit(code)
    raise SystemExit(main())
