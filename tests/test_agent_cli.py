"""Integration checks for observable CLI behavior, using local synthetic inputs only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/scenario-router-research"
RUNTIME = SKILL / "runtime"
CLI = SKILL / "scripts/research.py"


class AgentCLIIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent cli 空间 ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_cli(self, *arguments, code=0, script=CLI, cwd=None):
        completed = subprocess.run([sys.executable, "-B", str(script), *arguments],
                                   cwd=cwd or self.directory, capture_output=True, text=True,
                                   encoding="utf-8", timeout=60,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        # Exactly one JSON line, with a stable envelope even on invalid arguments.
        self.assertEqual(len(completed.stdout.splitlines()), 1, completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(set(result), {"schema_version", "ok", "command", "evidence_level", "data", "error"})
        self.assertEqual(result["schema_version"], "secondary-research-cli-v1")
        self.assertIs(result["ok"], code == 0)
        if code:
            self.assertEqual(set(result["error"]), {"code", "message", "hint"})
        return result

    def bundle(self):
        sources = []
        for role, day, text in (("prior", "2025-01-01", "FY2026 revenue guidance: 100 to 110 million USD."),
                                ("current", "2025-02-01", "FY2026 revenue guidance: 110 to 120 million USD.")):
            path = self.directory / (role + " source.txt")
            path.write_text(text, encoding="utf-8")
            sources.append({"source_id": role, "role": role, "path": path.name,
                            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                            "url": "https://example.invalid/synthetic",
                            "published_at": day + "T00:00:00Z", "captured_at": "2025-02-02T00:00:00Z"})
        path = self.directory / "source bundle.json"
        path.write_text(json.dumps({"bundle_id": "synthetic-cli", "security_id": "TEST:EXAMPLE",
                                    "ticker": "TEST", "fiscal_period": "FY2026", "lane": "synthetic",
                                    "sources": sources}), encoding="utf-8")
        return path

    def fixture(self):
        # Reuse the frozen engine's synthetic fixture generator; no market download.
        directory = self.directory / "input data"
        directory.mkdir()
        code = ("import importlib.util,sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[1]); "
                "spec=importlib.util.spec_from_file_location('frozen_fixture',Path(sys.argv[1])/'tests/test_backtest.py'); "
                "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                "m.write_fixture_directory(Path(sys.argv[2]))")
        subprocess.run([sys.executable, "-B", "-c", code, str(RUNTIME), str(directory)],
                       check=True, capture_output=True, text=True, timeout=30)
        return directory

    def backtest_arguments(self, *extra):
        return ("backtest", "--data", "input data", "--output", "output data",
                "--start", "2026-08-05", "--end", "2026-08-12",
                "--data-mode", "synthetic_fixture", "--code-revision", "synthetic-test",
                "--slippage-bps", "0,10", *extra)

    def test_describe_exposes_all_backtest_flags_and_provider_default(self):
        result = self.run_cli("describe")
        commands = result["data"]["interface"]["commands"]
        flags = {flag for item in commands["backtest"]["arguments"] for flag in item["flags"]}
        self.assertEqual(flags, {"--data", "--output", "--start", "--end", "--event-variant",
                                 "--reversal-variant", "--event-mode", "--coverage-mode", "--data-mode",
                                 "--initial-cash", "--slippage-bps", "--commission-per-share",
                                 "--minimum-commission", "--code-revision"})
        arguments = commands["evidence"]["commands"]["analyze"]["arguments"]
        self.assertEqual(next(item["default"] for item in arguments if item["name"] == "provider"), "off")

    def test_all_parse_errors_are_json(self):
        for arguments in ((), ("bogus",), ("backtest",), ("demo", "--kind", "bogus"),
                          ("--timeout", "nan", "demo"), ("--timeout", "0", "demo"),
                          ("evidence", "correct", "--database", "a", "--conversation", "a", "--bundle", "b")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, code=2)
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_help_remains_human_readable(self):
        completed = subprocess.run([sys.executable, "-B", str(CLI), "backtest", "--help"],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--code-revision", completed.stdout)
        self.assertTrue(completed.stdout.startswith("usage:"))

    def test_demos_from_unrelated_cwd_preserve_evidence_boundary(self):
        before = sorted(str(path.relative_to(RUNTIME)) for path in RUNTIME.rglob("*"))
        signal = self.run_cli("demo", "--kind", "signal")
        self.assertEqual(signal["evidence_level"], "SYNTHETIC_FIXTURE_ONLY")
        self.assertEqual(signal["data"]["order_intents"][0]["quantity"], 68)
        self.assertFalse(signal["data"]["profitability_validated"])
        paper = self.run_cli("demo", "--kind", "paper")
        self.assertAlmostEqual(paper["data"]["zero_cost"]["cash"], 100499.8)
        self.assertFalse(paper["data"]["real_model_called"])
        after = sorted(str(path.relative_to(RUNTIME)) for path in RUNTIME.rglob("*"))
        self.assertEqual(before, after, "CLI must not leave pycache/results in the frozen runtime")

    def test_relocated_skill_and_manifest_tamper_detection(self):
        relocated = self.directory / "copied skill"
        shutil.copytree(SKILL, relocated, ignore=shutil.ignore_patterns("__pycache__", "results"))
        manifest = relocated / "MANIFEST.sha256"
        entries = []
        for path in sorted(relocated.rglob("*")):
            if path.is_file() and path != manifest:
                entries.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(relocated).as_posix()}")
        manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
        script = relocated / "scripts/research.py"
        doctor = self.run_cli("doctor", script=script)
        self.assertTrue(doctor["data"]["runtime_manifest"]["ok"])
        self.assertEqual(doctor["data"]["runtime_manifest"]["checked_files"], 49)
        self.run_cli("demo", "--kind", "signal", script=script)
        path = relocated / "runtime/frozen_config.json"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        failed = self.run_cli("doctor", script=script, code=2)
        self.assertEqual(failed["error"]["code"], "INTEGRITY_CHECK_FAILED")
        self.assertFalse(failed["data"]["runtime_manifest"]["ok"])

    def test_timeout_is_runtime_json_failure(self):
        result = self.run_cli("--timeout", "0.00001", "demo", code=3)
        self.assertEqual(result["error"]["code"], "TIMEOUT")

    def test_backtest_bad_data_leaves_no_output(self):
        (self.directory / "input data").mkdir()
        result = self.run_cli(*self.backtest_arguments(), code=2)
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertIn("missing", result["error"]["message"])
        self.assertFalse((self.directory / "output data").exists())

    def test_backtest_replay_and_collision_preserves_original_bytes(self):
        self.fixture()
        result = self.run_cli(*self.backtest_arguments())
        output = self.directory / "output data"
        summary = output / "cost_comparison.json"
        self.assertEqual(Path(result["data"]["output"]).resolve(), output.resolve())
        self.assertEqual(result["evidence_level"], "SYNTHETIC_FIXTURE_ONLY")
        self.assertEqual(len(result["data"]["summary"]["scenarios"]), 2)
        self.assertFalse(result["data"]["data_provenance_independently_verified"])
        payload = summary.read_bytes()
        repeated = self.run_cli(*self.backtest_arguments(), code=2)
        self.assertEqual(repeated["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual(summary.read_bytes(), payload)
        self.assertEqual(sorted(path.name for path in output.iterdir()),
                         ["cost_comparison.json", "extra_slippage_0_bps_per_side", "extra_slippage_10_bps_per_side"])

    def test_bad_historical_revision_is_not_downgraded(self):
        (self.directory / "input data").mkdir()
        result = self.run_cli(*self.backtest_arguments("--data-mode", "historical_point_in_time"), code=2)
        self.assertIn("revision", result["error"]["message"])
        self.assertFalse((self.directory / "output data").exists())

    def test_status_missing_database_never_creates_it_or_parent(self):
        result = self.run_cli("evidence", "status", "--database", "missing parent/jobs.sqlite",
                              "--conversation", "test", code=2)
        self.assertEqual(result["error"]["code"], "DATABASE_NOT_FOUND")
        self.assertFalse((self.directory / "missing parent").exists())

    def test_provider_off_records_blocked_and_status_does_not_reanalyze(self):
        bundle = self.bundle()
        result = self.run_cli("evidence", "analyze", "--database", "jobs.sqlite", "--conversation", "test",
                              "--bundle", bundle.name, "--output", "raw evidence.json", code=3)
        self.assertEqual(result["error"]["code"], "MODEL_PROVIDER_DISABLED")
        self.assertEqual(result["evidence_level"], "NO_MODEL_EVIDENCE")
        self.assertEqual(result["data"]["state"], "BLOCKED")
        raw = json.loads((self.directory / "raw evidence.json").read_text())
        self.assertFalse(raw["e2b_screen"])
        self.assertEqual(len(raw["calls"]), 1)
        self.assertNotIn("raw", raw["calls"][0])
        database = self.directory / "jobs.sqlite"
        payload = database.read_bytes()
        status = self.run_cli("evidence", "status", "--database", database.name, "--conversation", "test")
        self.assertEqual(status["evidence_level"], "SAVED_STATE_ONLY")
        self.assertEqual(status["data"]["revision"], 1)
        self.assertFalse(status["data"]["query_receipt"]["new_model_analysis"])
        self.assertEqual(database.read_bytes(), payload)

    def test_evidence_existing_output_prevents_revision_write(self):
        bundle = self.bundle()
        output = self.directory / "raw.json"
        output.write_bytes(b"existing")
        result = self.run_cli("evidence", "analyze", "--database", "jobs.sqlite", "--conversation", "test",
                              "--bundle", bundle.name, "--output", output.name, code=2)
        self.assertEqual(result["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual(output.read_bytes(), b"existing")
        self.assertFalse((self.directory / "jobs.sqlite").exists())

    def test_correct_stale_revision_does_not_append(self):
        bundle = self.bundle()
        arguments = ("--database", "jobs.sqlite", "--conversation", "test", "--bundle", bundle.name)
        self.run_cli("evidence", "analyze", *arguments, code=3)
        result = self.run_cli("evidence", "correct", *arguments, "--expected-revision", "0", code=2)
        self.assertIn("stale expected revision", result["error"]["message"])
        with sqlite3.connect(self.directory / "jobs.sqlite") as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_invalid_bundle_does_not_succeed_or_resurrect_previous_candidate(self):
        bundle = self.bundle()
        arguments = ("--database", "jobs.sqlite", "--conversation", "test", "--bundle", bundle.name)
        self.run_cli("evidence", "analyze", *arguments, code=3)
        (self.directory / "current source.txt").write_text("tampered", encoding="utf-8")
        result = self.run_cli("evidence", "correct", *arguments, "--expected-revision", "1", code=2)
        self.assertEqual(result["error"]["code"], "EVIDENCE_BLOCKED")
        self.assertEqual(result["data"]["revision"], 2)
        self.assertFalse(result["data"]["e2b_screen"])
        self.assertIn("hash mismatch", result["data"]["reason"])


if __name__ == "__main__":
    unittest.main()
