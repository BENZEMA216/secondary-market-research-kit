"""Offline interface tests; fake providers are not external-service evidence."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "market-data-toolkit"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("market_data_tools_cli", SCRIPTS / "tools.py")
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)
import tool_adapters as adapters
from tool_contracts import ToolError, redact_text, verify_manifest


class MarketToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def call(self, *args, env=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "tools.py"), *args],
                                capture_output=True, text=True,
                                env={**os.environ, **(env or {})}, timeout=15)
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {"schema_version", "ok", "command", "evidence_level", "data", "error"})
        self.assertEqual(value["schema_version"], "market-data-cli-v1")
        self.assertEqual(result.stderr, "")
        return result.returncode, value

    def args_file(self, value):
        path = self.root / "args.json"
        path.write_text(json.dumps(value))
        return str(path)

    def test_inventory_covers_financial_families_and_marks_unsafe_factories(self):
        code, result = self.call("list")
        self.assertEqual(code, 0)
        all_tools = result["data"]["tools"]
        ids = {tool["id"] for tool in all_tools}
        for family in ("yfinance", "fmp", "finnhub", "sec", "reddit", "finnlp", "analysis", "chart", "report", "backtrader", "rag", "documents"):
            self.assertTrue(any(name.startswith(family + ".") for name in ids), family)
        self.assertNotIn("coding.exec_python", ids)
        self.assertGreaterEqual(result["data"]["counts"]["wrapped"], 49)
        blocked = [tool for tool in all_tools if tool["status"] == "blocked"]
        self.assertEqual(len(blocked), 4)
        self.assertTrue(all(tool["blocking_reason"] for tool in blocked))

    def test_describe_is_strict_and_help_is_json(self):
        code, result = self.call("describe", "backtrader.back_test")
        self.assertEqual(code, 0)
        schema = result["data"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["strategy"]["enum"], ["SMA_CrossOver"])
        self.assertNotIn("indicator", schema["properties"])
        self.assertEqual(self.call("--help")[1]["command"], "help")

    def test_doctor_checks_presence_without_exposing_credentials_or_importing_optional_packages(self):
        secret = "fixture-secret-NEVER-PRINT-12345"
        code, result = self.call("doctor", "fmp.get_target_price", env={"FMP_API_KEY": secret})
        self.assertEqual(code, 0)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn({"name": "FMP_API_KEY", "present": True}, result["data"]["environment"])
        self.assertFalse(result["data"]["network_called"])

    def test_default_network_gate_runs_before_dependency_import(self):
        args = self.args_file({"symbol": "AAPL"})
        code, result = self.call("run", "yfinance.get_stock_info", "--args-file", args, "--output", str(self.root / "run"))
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "NETWORK_PERMISSION_REQUIRED")
        self.assertFalse((self.root / "run").exists())

    def test_unknown_arguments_and_invalid_dates_fail_closed(self):
        for arguments in ({"symbol": "AAPL", "module": "os:system"},
                          {"symbol": "AAPL", "start_date": "2026-02-30", "end_date": "2026-03-01"}):
            name = "yfinance.get_stock_info" if "module" in arguments else "yfinance.get_stock_data"
            with self.assertRaises(ToolError):
                tools.prepare_request(name, arguments, allow_network=True, check_dependencies=False)
        with self.assertRaises(ToolError):
            tools.prepare_request("backtrader.back_test", {"ticker_symbol": "AAPL", "start_date": "2025-01-01", "end_date": "2025-12-31", "strategy": "os:system"}, allow_network=True, check_dependencies=False)
        with self.assertRaises(ToolError):
            tools.prepare_request("backtrader.back_test", {"ticker_symbol": "AAPL", "start_date": "2025-01-01", "end_date": "2025-12-31", "strategy": "SMA_CrossOver", "strategy_params": {"fast": True}}, allow_network=True, check_dependencies=False)

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        path = self.root / "bad.json"
        for text in ('{"text":"a","text":"b"}', '{"text":NaN}'):
            path.write_text(text)
            code, answer = self.call("run", "text.check_text_length", "--args-file", str(path), "--output", str(self.root / "out"))
            self.assertEqual(code, 2)
            self.assertFalse(answer["ok"])
        self.assertFalse((self.root / "out").exists())

    def test_output_collision_preserves_existing_content(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep")
        code, answer = self.call("run", "text.check_text_length", "--args-file", self.args_file({"text": "hello world"}), "--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(answer["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual(sentinel.read_text(), "keep")

    def test_real_local_dispatch_has_hashes_and_no_temporary_paths(self):
        output = self.root / "local-run"
        code, answer = self.call("run", "text.check_text_length", "--args-file", self.args_file({"text": "hello world", "max_length": 3}), "--output", str(output))
        self.assertEqual(code, 0, answer)
        self.assertIn("Text length 2", answer["data"]["result"])
        self.assertEqual(answer["evidence_level"], "LOCAL_DOCUMENT_PROCESSING")
        for artifact in answer["data"]["artifacts"]:
            path = Path(artifact["path"])
            self.assertTrue(path.is_relative_to(output))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])
        self.assertFalse(any(p.name.startswith(".market-tool-") for p in self.root.iterdir()))

    def test_fake_provider_dispatch_and_logs_are_redacted(self):
        secret = "fixture-secret-abcd-1234"
        spec = tools.get_tool("yfinance.get_stock_info")
        seen = []
        def fake(specification, arguments, workdir):
            seen.append((specification["id"], arguments))
            print("https://provider.invalid/?apikey=" + secret)
            (workdir / "raw.txt").write_text("Authorization: Bearer " + secret)
            return {"symbol": arguments["symbol"], "api_key": secret, "echo": secret, "price": 123.4}
        with patch.dict(os.environ, {"FMP_API_KEY": secret}):
            data = tools.execute_in_stage(spec, {"symbol": "TEST"}, self.root, dispatcher=fake)
        self.assertEqual(seen, [("yfinance.get_stock_info", {"symbol": "TEST"})])
        self.assertEqual(data["result"]["price"], 123.4)
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn(secret, (self.root / "raw.txt").read_text())
        self.assertNotIn(secret, (self.root / "result.json").read_text())

    def test_fake_provider_exception_is_redacted_in_envelope(self):
        secret = "fixture-secret-server-error-9876"
        with patch.dict(os.environ, {"FINNHUB_API_KEY": secret}):
            answer, code = tools.error_envelope("run finnhub.get_company_profile", RuntimeError("token=" + secret))
            encoded = json.dumps(tools.redact(answer))
        self.assertEqual(code, 3)
        self.assertNotIn(secret, encoded)

    def test_namespace_shim_does_not_import_unrelated_providers(self):
        spec = tools.get_tool("yfinance.get_stock_info")
        fake_yf = types.ModuleType("yfinance")
        fake_source = types.ModuleType("finrobot.data_source.yfinance_utils")
        fake_source.YFinanceUtils = types.SimpleNamespace(get_stock_info=lambda symbol: {"symbol": symbol})
        requested = []
        def importer(name):
            requested.append(name)
            if name == "yfinance":
                return fake_yf
            if name == "finrobot.data_source.yfinance_utils":
                return fake_source
            self.fail("Unexpected eager import: " + name)
        previous = {name: sys.modules.get(name) for name in ("finrobot", "finrobot.data_source", "finrobot.functional")}
        try:
            with patch.dict(os.environ, {}, clear=False), patch.object(adapters.importlib, "import_module", side_effect=importer):
                result = adapters.legacy(spec, {"symbol": "TEST"}, self.root)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self.assertEqual(result, {"symbol": "TEST"})
        self.assertEqual(requested, ["yfinance", "finrobot.data_source.yfinance_utils"])

    def test_local_retrieval_preserves_document_hash_and_span(self):
        source = self.root / "filing.txt"
        text = "Revenue rose 20 percent. Cash flow remained positive."
        source.write_text(text)
        output = self.root / "retrieval"
        code, answer = self.call("run", "rag.retrieve_local", "--args-file", self.args_file({"paths": [str(source)], "query": "revenue cash"}), "--output", str(output))
        self.assertEqual(code, 0, answer)
        match = answer["data"]["result"]["matches"][0]
        self.assertEqual(match["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(match["text"], text[match["char_start"]:match["char_end"]])
        self.assertFalse(answer["data"]["result"]["embedding_model_called"])

    def test_fake_earnings_provider_uses_environment_auth_without_date_rewrite(self):
        payload = json.dumps([{"content": "CEO: Revenue grew.", "date": "2024-02-01", "year": 2024}]).encode()
        with patch.dict(os.environ, {"DCF_USERNAME": "fixtureuser", "DCF_PASSWORD": "fixturepassword"}), patch.object(adapters, "fetch", return_value=(payload, "application/json")) as fetch:
            answer = adapters.earnings({"ticker": "TEST", "quarter": "Q1", "year": 2025}, self.root)
            self.assertIn("Authorization", fetch.call_args.args[1])
        docs = json.loads(Path(answer["documents_path"]).read_text())
        self.assertEqual(docs[0]["metadata"]["provider_date"], "2024-02-01")
        self.assertEqual(docs[0]["metadata"]["year_requested"], 2025)

    def test_sec_download_requires_user_identity_and_extracts_bounded_html(self):
        payload = b"<html><script>ignore()</script><p>Annual revenue grew.</p></html>"
        with patch.dict(os.environ, {"SEC_USER_AGENT": "My Research contact@example.invalid"}), patch.object(adapters, "fetch", return_value=(payload, "text/html")) as fetch:
            answer = adapters.sec_archive({"url": "https://www.sec.gov/Archives/edgar/data/1/filing.htm"}, self.root)
            self.assertEqual(fetch.call_args.args[1]["User-Agent"], "My Research contact@example.invalid")
        docs = json.loads(Path(answer["documents_path"]).read_text())
        self.assertNotIn("ignore()", docs[0]["page_content"])
        self.assertIn("Annual revenue", docs[0]["page_content"])
        with self.assertRaises(ToolError):
            tools.prepare_request("documents.sec_archive", {"url": "http://127.0.0.1/secret"}, allow_network=True, check_dependencies=False)

    def test_runtime_snapshot_integrity_and_legal_files_preserved(self):
        self.assertTrue(verify_manifest(SKILL / "runtime")["ok"])
        for name in ("LICENSE", "NOTICE", "TRADEMARK_POLICY.md"):
            self.assertTrue((SKILL / "runtime" / name).is_file())

    def test_empty_manifest_and_provenance_drift_fail_closed(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "MANIFEST.sha256").write_text("")
        self.assertFalse(verify_manifest(runtime)["ok"])
        (runtime / "file.txt").write_text("file")
        digest = hashlib.sha256(b"file").hexdigest()
        (runtime / "MANIFEST.sha256").write_text(f"{digest}  file.txt\n")
        (self.root / "SOURCE_PROVENANCE.json").write_text(json.dumps({"source_manifest_sha256": "0" * 64}))
        with patch.object(tools, "ROOT", self.root):
            self.assertFalse(tools.source_integrity()["ok"])

    def test_full_log_redaction_precedes_truncation(self):
        secret = "PREFIX-credential-NEVER-TAIL"
        def fake(spec, args, stage):
            print("a" * 1000 + secret + "z" * 5980, end="")
            return {"ok": True}
        with patch.dict(os.environ, {"FMP_API_KEY": secret}):
            data = tools.execute_in_stage(tools.get_tool("yfinance.get_stock_info"), {}, self.root, dispatcher=fake)
        self.assertNotIn("NEVER-TAIL", data["upstream_log_tail"])

    def test_no_data_sentinel_is_not_a_successful_quote(self):
        spec = tools.get_tool("fmp.get_historical_bvps")
        module = types.SimpleNamespace(FMPUtils=types.SimpleNamespace(get_historical_bvps=lambda **kwargs: "No BVPS data available"))
        with patch.object(adapters, "namespace_shim"), patch.object(adapters, "_cache_hooks"), patch.object(adapters.importlib, "import_module", return_value=module):
            with self.assertRaises(ToolError) as caught:
                adapters.legacy(spec, {"ticker_symbol": "TEST", "target_date": "2025-01-01"}, self.root)
        self.assertEqual(caught.exception.code, "UPSTREAM_NO_DATA")

    def test_relocation_preserves_json_with_quote_and_backslash_paths(self):
        source = {"file": "/tmp/stage/artifact.txt", "nested": ["/tmp/stage/value"]}
        output = '/tmp/quoted" and \\ path'
        moved = tools.relocate(source, "/tmp/stage", output)
        self.assertEqual(json.loads(json.dumps(moved))["file"], output + "/artifact.txt")


if __name__ == "__main__":
    unittest.main()
