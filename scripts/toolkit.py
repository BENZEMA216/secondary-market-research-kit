#!/usr/bin/env python3
"""One dependency-free dispatcher for all bundled research Skills."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class InputError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def main(argv=None):
    command = "unknown"
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        commands.add_parser("list", help="List bundled toolkits and how to inspect each tool registry")
        for name in ("describe", "doctor", "run"):
            sub = commands.add_parser(name)
            sub.add_argument("toolkit", help="Toolkit name from list")
            if name == "run":
                sub.add_argument("arguments", nargs=argparse.REMAINDER, help="Arguments to the toolkit entry point after --")
        args = parser.parse_args(argv)
        command = args.command
        catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
        if command == "list":
            result = {"schema_version": "secondary-market-toolkit-v1", "ok": True,
                      "command": command, "evidence_level": "INTERFACE_METADATA", "data": catalog, "error": None}
            result["data"]["next_step"] = "toolkit.py describe TOOLKIT, then toolkit.py run TOOLKIT -- COMMAND [arguments]"
            code = 0
        else:
            choices = {item["name"]: item for item in catalog["toolkits"]}
            if args.toolkit not in choices:
                raise InputError("Unknown toolkit; use toolkit.py list")
            item = choices[args.toolkit]
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item["name"]):
                raise InputError("Invalid toolkit name in catalog")
            relative = item["entry"]
            if not relative or PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative or ":" in relative:
                raise InputError("Toolkit entry must be a safe relative path")
            entry = ROOT
            for part in ("skills", item["name"], *PurePosixPath(relative).parts):
                entry = entry / part
                if entry.is_symlink():
                    raise InputError("Toolkit entry must not use symbolic links")
            if not entry.is_file():
                raise InputError("Toolkit entry is missing; use an intact repository/source archive")
            arguments = args.arguments if command == "run" else [command]
            if arguments and arguments[0] == "--":
                arguments = arguments[1:]
            if not arguments:
                raise InputError("run requires a toolkit command after --")
            # Retain the caller cwd: all user paths belong to the caller's project.
            process = subprocess.Popen([sys.executable, "-B", str(entry), *arguments],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       start_new_session=os.name == "posix",
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                stdout, stderr = process.communicate(timeout=7200)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
                raise InputError("Toolkit exceeded the dispatcher maximum time of 7200 seconds") from None
            if "--help" in arguments or "-h" in arguments:
                print(stdout, end="")
                return process.returncode
            try:
                result = json.loads(stdout)
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    raise ValueError("envelope missing ok boolean")
            except (ValueError, TypeError) as exc:
                # Upstream logs may contain user input or secrets; never echo raw output.
                raise InputError("Toolkit did not return its documented JSON envelope; inspect the installed release") from exc
            code = process.returncode
            if not result["ok"] and code == 0:
                code = 2
            if code != 0 and result["ok"]:
                result = {"schema_version": "secondary-market-toolkit-v1", "ok": False, "command": command,
                          "evidence_level": "NONE", "data": None,
                          "error": {"code": "CHILD_FAILED", "message": "Toolkit exited with a failure status", "hint": "Run its doctor command"}}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except (InputError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"schema_version": "secondary-market-toolkit-v1", "ok": False, "command": command,
                          "evidence_level": "NONE", "data": None,
                          "error": {"code": "TOOLKIT_ERROR", "message": str(exc), "hint": "Run toolkit.py list or --help"}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
