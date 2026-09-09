from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import validate as validation


CSV_HEADER = "timestamp,open,high,low,close,volume\n"


class ValidationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        defaults = {
            "ROOT": self.root,
            "LOCKED_DATA": self.root / "absent_locked_data",
            "LOCKED_TRADE_LOG": self.root / "absent_trade_log.csv",
        }
        for name, value in defaults.items():
            self.patches.enter_context(patch.object(validation, name, value))
        self.patches.enter_context(patch.object(validation.compileall, "compile_dir", return_value=True))
        self.patches.enter_context(patch.object(validation, "load_frozen_config", return_value=({}, "a" * 64)))
        self.patches.enter_context(patch.object(validation, "unit_tests", return_value={"passed": True}))
        self.patches.enter_context(patch.object(validation, "validate_json_artifacts", return_value={"schemas_parse": True}))
        self.patches.enter_context(patch.object(validation, "end_to_end_cli_smoke", return_value={"status": "PASS"}))
        self.patches.enter_context(patch.object(validation, "package_manifest", return_value={}))

    def run_validation(self) -> tuple[int, dict]:
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = validation.main()
        report = json.loads((self.root / "results" / "validation_report.json").read_text())
        return exit_code, report

    def write_stale_pass(self) -> None:
        result_dir = self.root / "results"
        result_dir.mkdir()
        (result_dir / "validation_report.json").write_text('{"status":"PASS_REFERENCE_LOGIC"}\n')

    def test_missing_optional_data_is_only_synthetic_pass(self) -> None:
        exit_code, report = self.run_validation()
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS_SYNTHETIC_ONLY", report["status"])
        self.assertFalse(report["verification_boundaries"]["locked_market_data_exercised"])
        self.assertEqual("SKIPPED_LOCKED_DATA_NOT_PRESENT", report["checks"]["quad_stochastic_locked_data_smoke"]["status"])

    def test_reference_pass_requires_both_data_checks(self) -> None:
        with patch.object(validation, "golden_macd_parity", return_value={"status": "PASS"}), \
                patch.object(validation, "quad_stochastic_locked_data_smoke", return_value={"status": "PASS_NO_CRASH"}):
            exit_code, report = self.run_validation()
        self.assertEqual((0, "PASS_REFERENCE_LOGIC"), (exit_code, report["status"]))
        boundaries = report["verification_boundaries"]
        self.assertTrue(boundaries.pop("locked_market_data_exercised"))
        self.assertFalse(any(boundaries.values()))
        self.assertFalse(report["environment"]["quantconnect_cloud_executed"])

    def test_runtime_exception_overwrites_a_stale_pass(self) -> None:
        self.write_stale_pass()
        with patch.object(validation, "validate_json_artifacts", side_effect=ValueError("invalid candidate")):
            exit_code, report = self.run_validation()
        self.assertEqual((1, "FAIL"), (exit_code, report["status"]))
        self.assertEqual("ValueError", report["checks"]["contracts"]["error_type"])
        self.assertEqual("invalid candidate", report["checks"]["contracts"]["message"])
        self.assertNotIn("end_to_end_cli", report["checks"])

    def test_failed_tests_cannot_leave_a_pass_report(self) -> None:
        self.write_stale_pass()
        with patch.object(validation, "unit_tests", return_value={"passed": False, "failures": 1}):
            exit_code, report = self.run_validation()
        self.assertEqual((1, "FAIL"), (exit_code, report["status"]))

    def test_manifest_exception_also_produces_current_failure(self) -> None:
        self.write_stale_pass()
        with patch.object(validation, "package_manifest", side_effect=OSError("unreadable artifact")):
            exit_code, report = self.run_validation()
        self.assertEqual((1, "FAIL"), (exit_code, report["status"]))
        self.assertEqual("OSError", report["checks"]["package_manifest"]["error_type"])

    def test_partial_locked_data_is_failure_not_silent_skip(self) -> None:
        data = self.root / "partial_data"
        data.mkdir()
        daily = data / "test_1d.csv"
        payload = CSV_HEADER + "2026-06-09,100,101,99,100,1000\n"
        daily.write_text(payload)
        with patch.object(validation, "LOCKED_DATA", data), \
                patch.object(validation, "EXPECTED_MACD3_LONG", {"TEST": 0}):
            exit_code, report = self.run_validation()
        self.assertEqual((1, "FAIL"), (exit_code, report["status"]))
        self.assertEqual("FileNotFoundError", report["checks"]["quad_stochastic_locked_data_smoke"]["error_type"])
        self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), report["input_files_sha256"][str(daily)])


class ObservedCalendarSmokeTests(unittest.TestCase):
    def test_csv_smoke_uses_calendar_and_hashes_the_bytes_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test_5m.csv"
            payload = CSV_HEADER + (
                "2026-06-09 09:30:00-04:00,100,101,99,100,1000\n"
                "2026-06-09 09:35:00-04:00,100,101,99,100,1000\n"
                "2026-06-10 09:30:00-04:00,100,101,99,100,1000\n"
            )
            path.write_text(payload)
            inputs: dict[str, str] = {}
            with patch.object(validation, "LOCKED_DATA", root), \
                    patch.object(validation, "LOCKED_TRADE_LOG", root / "not_present.csv"), \
                    patch.object(validation, "EXPECTED_MACD3_LONG", {"TEST": 0}):
                result = validation.quad_stochastic_locked_data_smoke(inputs)
            self.assertEqual("PASS_NO_CRASH", result["status"])
            self.assertEqual(3, result["bars_processed"])
            self.assertEqual(2, result["observed_session_count"])
            self.assertEqual("2026-06-09", result["observed_first_session"])
            self.assertEqual("2026-06-10", result["observed_last_session"])
            self.assertFalse(result["official_exchange_calendar_completeness_verified"])
            self.assertFalse(result["point_in_time_price_basis_verified"])
            self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), inputs[str(path)])

    def test_intraday_gap_is_still_rejected_in_observed_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_5m.csv").write_text(CSV_HEADER + (
                "2026-06-09 09:30:00-04:00,100,101,99,100,1000\n"
                "2026-06-09 09:40:00-04:00,100,101,99,100,1000\n"
            ))
            with patch.object(validation, "LOCKED_DATA", root), \
                    patch.object(validation, "EXPECTED_MACD3_LONG", {"TEST": 0}):
                with self.assertRaisesRegex(ValueError, "missing or duplicate"):
                    validation.quad_stochastic_locked_data_smoke()


class CliSmokeTests(unittest.TestCase):
    def test_actual_example_includes_order_intent_and_complete_exit(self) -> None:
        result = validation.end_to_end_cli_smoke()
        self.assertEqual("PASS", result["status"])
        example = result["outputs"]["signal_example"]
        self.assertEqual(68, example["order_intents"][0]["quantity"])
        self.assertEqual(["day5_half", "ema10_next_open"], [item["reason"] for item in example["exit_decisions"]])

    def test_successful_process_without_complete_flow_is_rejected(self) -> None:
        old_output = {"event_status": "QUALIFIED_EVENT", "signals": [{"variant": "E2A"}]}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(old_output), stderr="")
        with patch.object(validation.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(AssertionError, "frozen synthetic scenario"):
                validation.end_to_end_cli_smoke()


if __name__ == "__main__":
    unittest.main()
