"""Offline integration and failure tests for the derived equity research adapter."""
from __future__ import annotations

import configparser
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/equity-research-toolkit"
SCRIPT = SKILL / "scripts/equity.py"
SOURCE = SKILL / "runtime/core/src"
HAS_DEPS = all(importlib.util.find_spec(name) for name in ("pandas", "numpy", "matplotlib", "openai", "yfinance", "reportlab"))
HAS_SDK = HAS_DEPS and importlib.util.find_spec("agents") is not None


class EquityCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="equity test 空间 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def call(self, *args, code=0, script=SCRIPT):
        result = subprocess.run([sys.executable, "-B", str(script), *args], cwd=self.directory,
                                capture_output=True, text=True, timeout=120,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"schema_version", "ok", "command", "evidence_level", "data", "error"})
        self.assertEqual(payload["schema_version"], "equity-research-cli-v1")
        self.assertEqual(payload["ok"], code == 0)
        return payload

    def args_file(self, value, name="arguments.json"):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def module(self):
        spec = importlib.util.spec_from_file_location("equity_adapter_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_registry_covers_modules_and_three_source_entrypoints(self):
        payload = self.call("describe")
        tools = payload["data"]["tools"]
        self.assertEqual(payload["data"]["tool_count"], 71)
        self.assertTrue({"finance.historical", "finance.forecast", "data.peers", "data.news", "data.technical",
                         "valuation.dcf", "valuation.ev-ebitda", "sensitivity.analyze", "catalyst.analyze",
                         "report.html", "report.pdf", "pipeline.full", "source.financial-analysis",
                         "source.html-report", "source.pdf-report"}.issubset(tools))
        self.assertTrue(tools["text.section"]["network"] and tools["text.section"]["model"])
        self.assertIn("text.agent-section", tools)
        self.assertEqual(len([name for name in tools if name.startswith("text.enhanced.")]), 8)

    def test_all_parse_errors_are_json(self):
        for arguments in ((), ("invalid",), ("demo",), ("--timeout", "nan", "describe"),
                          ("run", "data.profile", "--unexpected")):
            with self.subTest(args=arguments):
                result = self.call(*arguments, code=2)
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_config_template_is_placeholder_only_and_never_overwrites(self):
        result = self.call("config-template", "--output", "private settings/config.ini")
        self.assertFalse(result["data"]["contains_credentials"])
        path = self.directory / "private settings/config.ini"
        before = path.read_bytes()
        self.assertIn(b"YOUR_FMP_API_KEY", before)
        result = self.call("config-template", "--output", "private settings/config.ini", code=2)
        self.assertEqual(result["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual(path.read_bytes(), before)

    def test_network_and_model_are_explicit_gates_before_dependencies_or_config(self):
        args = self.args_file({"ticker": "AAPL"})
        result = self.call("run", "data.profile", "--args-file", args.name, "--output", "network", code=2)
        self.assertEqual(result["error"]["code"], "NETWORK_DISABLED")
        args = self.args_file({"data": {}, "prompt_type": "risks", "company_name": "Example", "company_ticker": "TEST"})
        result = self.call("run", "text.section", "--args-file", args.name, "--output", "model", "--allow-network", code=2)
        self.assertEqual(result["error"]["code"], "MODEL_DISABLED")
        self.assertFalse((self.directory / "network").exists())
        self.assertFalse((self.directory / "model").exists())

    def test_unknown_and_duplicate_input_keys_fail(self):
        path = self.args_file({"ticker": "AAPL", "api_key": "SENSITIVE_TEST_VALUE"})
        result = self.call("run", "data.profile", "--args-file", path.name, "--output", "report", code=2)
        self.assertNotIn("SENSITIVE_TEST_VALUE", json.dumps(result))
        path.write_text('{"ticker":"AAPL","ticker":"MSFT"}')
        result = self.call("run", "data.profile", "--args-file", path.name, "--output", "report", code=2)
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_source_cli_parameters_cannot_inject_model_flags(self):
        args = self.args_file({"company_ticker": "TEST", "company_name": ["Example", "--generate-text-sections"]})
        result = self.call("run", "source.financial-analysis", "--args-file", args.name,
                           "--output", "injected", "--allow-network", code=2)
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        spec = self.module().registry()["source.html-report"]
        values = {item["name"]: "dummy" for item in spec["parameters"] if item["required"]}
        values["company_ticker"] = "TEST"
        values["analyst_names"] = ["Analyst", "--enable-text-regeneration"]
        args = self.args_file(values)
        result = self.call("run", "source.html-report", "--args-file", args.name,
                           "--output", "injected", "--allow-network", code=2)
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertFalse((self.directory / "injected").exists())

    def test_publication_preserves_concurrently_created_empty_directory(self):
        module = self.module()
        stage, output = self.directory / "stage", self.directory / "output"
        stage.mkdir()
        (stage / "result.json").write_text("new")
        output.mkdir()
        inode = output.stat().st_ino
        with self.assertRaises(module.ToolError) as caught:
            module.publish_new_directory(stage, output)
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(output.stat().st_ino, inode)
        self.assertEqual(list(output.iterdir()), [])

    def test_enhanced_local_methods_do_not_initialize_model_client(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        from modules.enhanced_text_generator import EnhancedTextGenerator
        samples = {
            "section-summary": {"section_content": "Revenue growth is an important driver of this supplied scenario. The main risk is uncertain demand."},
            "risk-factors": {"risks": [{"category": "market", "title": "Demand", "description": "Supplied illustrative risk."}]},
            "format-data-reference": {"value": 1000000, "metric": "Revenue", "source": "Synthetic fixture", "date": "2024-12-31"},
        }
        with patch.object(EnhancedTextGenerator, "_init_client", side_effect=AssertionError("must not initialize client")):
            for tool, values in samples.items():
                with self.subTest(tool=tool):
                    result = module.invoke("text.enhanced." + tool, values, self.directory, configparser.ConfigParser(), {})
                    self.assertTrue(result["text"])
                    self.assertFalse(result["model_called_by_this_tool"])

    def test_new_model_paths_require_explicit_capability_flags(self):
        samples = {
            "text.enhanced.executive-summary": {"data": {"ticker": "TEST"}},
            "text.agent-section": {"data": {"company_news": [{"title": "Synthetic"}]}, "text_type": "tagline",
                                   "company_name": "Example", "company_ticker": "TEST"},
        }
        for tool, values in samples.items():
            args = self.args_file(values)
            for flags, expected in (([], "MODEL_DISABLED"), (["--allow-model"], "NETWORK_DISABLED"),
                                    (["--allow-network"], "MODEL_DISABLED")):
                with self.subTest(tool=tool, flags=flags):
                    result = self.call("run", tool, "--args-file", args.name, "--output", "new-model", *flags, code=2)
                    self.assertEqual(result["error"]["code"], expected)
        self.assertFalse((self.directory / "new-model").exists())

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for enhanced model mocks")
    def test_enhanced_five_model_methods_and_fallback_are_strict(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        config = configparser.ConfigParser()
        config["API_KEYS"] = {"openai_api_key": "synthetic-model-token", "openai_model": "explicit-test-model"}
        samples = {
            "executive-summary": {"data": {"ticker": "TEST", "company_name": "Example", "current_price": 10, "target_price": 12}},
            "forecast-methodology": {"forecast_config": {"revenue_growth": "5%", "historical_cagr": "4%"}},
            "catalyst-analysis": {"catalysts": [{"description": "Synthetic event", "sentiment": "positive"}]},
            "valuation-analysis": {"valuation_data": {"current_price": 10, "target_price": 12}},
            "investment-recommendation": {"analysis_data": {"ticker": "TEST", "current_price": 10, "target_price": 12}},
        }
        for tool, values in samples.items():
            client = MagicMock()
            client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Mock model text."))])
            with patch("openai.OpenAI", return_value=client), contextlib.redirect_stderr(io.StringIO()):
                result = module.invoke("text.enhanced." + tool, values, self.directory, config, {"allow_network": True, "allow_model": True})
            self.assertEqual(result["text"], "Mock model text.")
            self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "explicit-test-model")
        for behavior in ("error", "empty", "length", "content_filter", "refusal"):
            client = MagicMock()
            if behavior == "error":
                client.chat.completions.create.side_effect = RuntimeError("synthetic failure")
            else:
                client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason=behavior if behavior in ("length", "content_filter") else "stop",
                    message=SimpleNamespace(content="" if behavior == "empty" else "Partial text",
                                            refusal="refused" if behavior == "refusal" else None))])
            with patch("openai.OpenAI", return_value=client), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(module.ToolError) as caught:
                    module.invoke("text.enhanced.executive-summary", samples["executive-summary"], self.directory,
                                  config, {"allow_network": True, "allow_model": True})
            self.assertEqual(caught.exception.code, "MODEL_GENERATION_FAILED")

    @unittest.skipUnless(HAS_SDK, "run under project .venv for agent SDK mocks")
    def test_agent_manager_all_eight_typed_sections_without_tools_or_tracing(self):
        os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
        import agents
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        config = configparser.ConfigParser()
        config["API_KEYS"] = {"openai_api_key": "synthetic-sdk-token", "openai_model": "explicit-sdk-model"}
        fields = {"tagline": "tagline", "company_overview": "overview", "investment_overview": "investment_update",
                  "valuation_overview": "valuation_analysis", "risks": "risk_analysis", "competitor_analysis": "competitive_analysis",
                  "major_takeaways": "takeaways", "news_summary": "news_summary"}
        for section, field in fields.items():
            async def run(agent, prompt, *, run_config, max_turns):
                self.assertTrue(run_config.tracing_disabled)
                self.assertFalse(run_config.trace_include_sensitive_data)
                self.assertEqual(max_turns, 1)
                self.assertFalse(agent.tools or agent.handoffs or agent.mcp_servers)
                self.assertEqual(agent.model.model, "explicit-sdk-model")
                return SimpleNamespace(final_output=agent.output_type(**{field: "Typed mock section."}))
            values = {"data": {"company_news": [{"title": "Synthetic news", "text": "Supplied test data"}]},
                      "text_type": section, "company_name": "Example", "company_ticker": "TEST"}
            with self.subTest(section=section), patch.object(agents.Runner, "run", side_effect=run) as runner, \
                    patch.dict(os.environ, {"OPENAI_MODEL_NAME": "ambient-must-not-be-used"}), \
                    patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
                result = module.invoke("text.agent-section", values, self.directory, config, {"allow_network": True, "allow_model": True})
                self.assertEqual(os.environ["OPENAI_MODEL_NAME"], "ambient-must-not-be-used")
            runner.assert_called_once()
            self.assertEqual(result["text"], "Typed mock section.")
            self.assertFalse(result["tracing_enabled"])

    @unittest.skipUnless(HAS_SDK, "run under project .venv for agent SDK mocks")
    def test_agent_manager_rejects_string_fallback_and_empty_typed_output(self):
        os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
        import agents
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        from modules.equity_agents.tagline_agent import TaglineResponse
        config = configparser.ConfigParser()
        config["API_KEYS"] = {"openai_api_key": "synthetic-sdk-token", "openai_model": "explicit-sdk-model"}
        values = {"data": {"company_news": [{"title": "Synthetic"}]}, "text_type": "tagline", "company_name": "Example", "company_ticker": "TEST"}
        for output in ("wrong string fallback", None, TaglineResponse(tagline="")):
            with patch.object(agents.Runner, "run", new=AsyncMock(return_value=SimpleNamespace(final_output=output))):
                with self.assertRaises(module.ToolError) as caught:
                    module.invoke("text.agent-section", values, self.directory, config, {"allow_network": True, "allow_model": True})
            self.assertEqual(caught.exception.code, "INVALID_AGENT_OUTPUT")

    @unittest.skipUnless(HAS_SDK, "run under project .venv for explicit credential mocks")
    def test_new_model_paths_never_borrow_ambient_credentials(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        samples = {
            "text.enhanced.executive-summary": {"data": {"ticker": "TEST"}},
            "text.agent-section": {"data": {"company_news": [{"title": "Synthetic"}]}, "text_type": "tagline",
                                   "company_name": "Example", "company_ticker": "TEST"},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-must-not-be-used"}), \
                patch("openai.OpenAI", side_effect=AssertionError("unexpected client")), \
                patch("openai.AsyncOpenAI", side_effect=AssertionError("unexpected client")):
            for tool, values in samples.items():
                with self.assertRaises(module.ToolError) as caught:
                    module.invoke(tool, values, self.directory, configparser.ConfigParser(), {"allow_network": True, "allow_model": True})
                self.assertEqual(caught.exception.code, "CONFIG_REQUIRED")

    @unittest.skipUnless(HAS_SDK, "run under project .venv for agent input checks")
    def test_agent_manager_rejects_unconsumable_data_before_client_creation(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        import pandas as pd
        config = configparser.ConfigParser()
        config["API_KEYS"] = {"openai_api_key": "synthetic-sdk-token", "openai_model": "explicit-sdk-model"}
        for data, section, code in (({"foo": "bar"}, "tagline", "INVALID_INPUT"),
                                    ({"financial_metrics": pd.DataFrame()}, "tagline", "EMPTY_UPSTREAM_DATA"),
                                    ({"financial_metrics": {"revenue": 1}}, "tagline", "INVALID_INPUT"),
                                    ({"financial_metrics": pd.DataFrame([{"Revenue": 1}])}, "news_summary", "EMPTY_UPSTREAM_DATA")):
            values = {"data": data, "text_type": section, "company_name": "Example", "company_ticker": "TEST"}
            with patch("openai.AsyncOpenAI", side_effect=AssertionError("unexpected client")):
                with self.assertRaises(module.ToolError) as caught:
                    module.invoke("text.agent-section", values, self.directory, config, {"allow_network": True, "allow_model": True})
            self.assertEqual(caught.exception.code, code)

    def test_doctor_does_not_import_optional_libraries_or_read_config(self):
        module = self.module()
        with patch.object(module.importlib, "import_module", side_effect=AssertionError("unexpected import")), \
                patch.object(module, "private_config", side_effect=AssertionError("unexpected config")):
            try:
                report = module.doctor()
            except module.ToolError as exc:
                self.assertEqual(exc.code, "DEPENDENCY_UNAVAILABLE")
                report = exc.data
        self.assertFalse(report["third_party_modules_imported"])
        self.assertFalse(report["config_read"])
        self.assertTrue(report["runtime_manifest"]["ok"])
        self.assertEqual(report["runtime_manifest"]["checked_files"], 36)

    def test_frozen_copy_hashes_and_local_modification_notices(self):
        provenance = json.loads((SKILL / "SOURCE_PROVENANCE.json").read_text())
        self.assertEqual(provenance["source_revision"], "d221910096de87579b02f8f0674652bf1a175f51")
        self.assertEqual(len(provenance["local_modified_files"]), 6)
        for entry in provenance["files"]:
            data = (SKILL / "runtime" / entry["runtime_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["distribution_sha256"])
            if entry["local_modified"]:
                self.assertIn(b"Modified before redistribution", data)
            else:
                self.assertEqual(entry["source_sha256"], entry["distribution_sha256"])
        self.assertFalse(list((SKILL / "runtime").rglob("config.ini")))
        self.assertFalse(list((SKILL / "runtime").rglob("*.ipynb")))

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for finance/report checks")
    def test_offline_demo_executes_original_finance_valuation_charts_html_pdf(self):
        before = sorted(str(path.relative_to(SKILL / "runtime")) for path in (SKILL / "runtime").rglob("*"))
        result = self.call("demo", "--output", "demo report", "--pdf")
        self.assertEqual(result["evidence_level"], "SYNTHETIC_FIXTURE_ONLY")
        self.assertFalse(result["data"]["network_enabled"])
        self.assertFalse(result["data"]["model_enabled"])
        values = result["data"]["result"]["valuations"]["results"]
        self.assertAlmostEqual(values[0]["target_price"], 38.88)
        self.assertAlmostEqual(values[1]["target_price"], 39.24654145891674)
        output = self.directory / "demo report"
        self.assertTrue((output / "report.pdf").read_bytes().startswith(b"%PDF-"))
        self.assertGreater((output / "report.pdf").stat().st_size, 20000, "PDF should embed actual generated charts")
        html = (output / "report.html").read_text()
        self.assertIn("SYNTHETIC FIXTURE ONLY", html)
        self.assertNotIn("https://cdn.tailwindcss.com", html)
        self.assertNotIn("https://fonts.googleapis.com", html)
        self.assertIn("126000000", (output / "financial_metrics_and_forecasts.csv").read_text())
        payload = (output / "report.html").read_bytes()
        collision = self.call("demo", "--output", "demo report", code=2)
        self.assertEqual(collision["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual((output / "report.html").read_bytes(), payload)
        after = sorted(str(path.relative_to(SKILL / "runtime")) for path in (SKILL / "runtime").rglob("*"))
        self.assertEqual(before, after)

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for finance/report checks")
    def test_standalone_copy_runs_valuation_from_unrelated_cwd(self):
        copied = self.directory / "copied skill"
        shutil.copytree(SKILL, copied, ignore=shutil.ignore_patterns("__pycache__"))
        arguments = self.args_file(json.loads((copied / "examples/valuation.json").read_text()), "valuation inputs.json")
        result = self.call("run", "valuation.combined", "--args-file", arguments.name, "--output", "valuation out",
                           script=copied / "scripts/equity.py")
        self.assertEqual(result["evidence_level"], "LOCAL_INPUT_CALCULATIONS")
        self.assertEqual(result["data"]["result"]["synthesis"]["methods_used"], 2)

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for finance/report checks")
    def test_pipeline_empty_financial_statement_fails_without_publication(self):
        values = json.loads((SKILL / "examples/offline-pipeline.json").read_text())
        values["financial_data"]["income_statement"] = {"$table": []}
        path = self.args_file(values)
        result = self.call("run", "pipeline.full", "--args-file", path.name, "--output", "empty output", code=2)
        self.assertEqual(result["error"]["code"], "EMPTY_UPSTREAM_DATA")
        self.assertFalse((self.directory / "empty output").exists())

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for finance/report checks")
    def test_future_wacc_and_projection_constraints_fail_closed(self):
        values = json.loads((SKILL / "examples/valuation.json").read_text())
        values["assumptions"]["wacc"] = values["assumptions"]["terminal_growth"]
        path = self.args_file(values)
        result = self.call("run", "valuation.dcf", "--args-file", path.name, "--output", "invalid value", code=2)
        self.assertEqual(result["error"]["code"], "INVALID_VALUATION")
        self.assertFalse((self.directory / "invalid value").exists())

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for finance/report checks")
    def test_past_forecast_years_fail(self):
        values = json.loads((SKILL / "examples/offline-pipeline.json").read_text())
        values["forecast_config"]["revenue_growth_assumptions"] = {"2024E": .05}
        path = self.args_file(values)
        result = self.call("run", "pipeline.full", "--args-file", path.name, "--output", "invalid years", code=2)
        self.assertEqual(result["error"]["code"], "INVALID_FORECAST")

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for network/model mocks")
    def test_empty_mock_network_data_is_not_success(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        import modules.market_data_api as market
        config = configparser.ConfigParser()
        config["API_KEYS"] = {"fmp_api_key": "unit-test-only-fmp-token"}
        with patch.object(market, "get_comprehensive_financial_data", return_value={"income_statement": None}) as fetch:
            with self.assertRaises(module.ToolError) as caught:
                module.invoke("data.financials", {"ticker": "TEST"}, self.directory, config, {"allow_network": True})
        fetch.assert_called_once()
        self.assertEqual(caught.exception.code, "EMPTY_UPSTREAM_DATA")

    @unittest.skipUnless(HAS_DEPS, "run under project .venv for network/model mocks")
    def test_mock_model_failure_is_strict_and_secret_never_returns(self):
        module = self.module()
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(sys.path.remove, str(SOURCE))
        import modules.text_generator_agents as text
        secret = "test-only-private-model-token-58128"
        path = self.directory / "private.ini"
        path.write_text("[API_KEYS]\nopenai_api_key = " + secret)
        config = module.private_config({"config_file": str(path)})
        values = {"data": {}, "prompt_type": "risks", "company_name": "Example", "company_ticker": "TEST"}
        with patch.object(text, "OpenAI", side_effect=RuntimeError(secret)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(text.TextGenerationError) as caught:
                module.invoke("text.section", values, self.directory, config, {"allow_model": True, "allow_network": True})
        failure = module.failed("run text.section", caught.exception)
        self.assertFalse(failure["envelope"]["ok"])
        self.assertEqual(failure["exit_code"], 3)
        self.assertNotIn(secret, json.dumps(failure))


if __name__ == "__main__":
    unittest.main()
