#!/usr/bin/env python3
"""JSON interface for a derivative equity research toolkit built on FinRobot."""
from __future__ import annotations

import argparse
import configparser
import contextlib
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import hashlib
import importlib
import importlib.metadata
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "runtime"
SOURCE = RUNTIME / "core/src"
SCHEMA = "equity-research-cli-v1"
SECRETS = []
SECTIONS = ("tagline", "company_overview", "investment_overview", "valuation_overview",
            "risks", "competitor_analysis", "major_takeaways", "news_summary")
DEPENDENCIES = ("pandas", "numpy", "requests", "matplotlib", "openai", "yfinance",
                "reportlab", "pytz", "tabulate", "socksio", "openai-agents")
CONFIG_TEMPLATE = """# Created template only. Replace placeholders privately; never commit configured keys.
[API_KEYS]
fmp_api_key = YOUR_FMP_API_KEY
openai_api_key = YOUR_OPENAI_API_KEY
openai_model = YOUR_MODEL_NAME
openai_base_url = https://api.openai.com/v1
# Optional retail sentiment provider:
adanos_api_key = YOUR_ADANOS_API_KEY
adanos_base_url = https://api.adanos.org
"""


class ToolError(Exception):
    def __init__(self, code, message, hint="Run describe and check the tool input contract.", *, exit_code=2, data=None):
        super().__init__(message)
        self.code, self.hint, self.exit_code, self.data = code, hint, exit_code, data


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise ToolError("INVALID_ARGUMENT", message)


def positive(raw):
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("a finite positive number is required")
    return value


def parser():
    result = Parser(description=__doc__)
    result.add_argument("--timeout", type=positive, default=300, help="total seconds; place before command")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("describe", "list", "doctor"):
        commands.add_parser(name)
    template = commands.add_parser("config-template")
    template.add_argument("--output", required=True, help="new configuration file, never overwritten")
    demo = commands.add_parser("demo", help="offline synthetic finance, valuation, charts and HTML/PDF")
    demo.add_argument("--output", required=True, help="new output directory")
    demo.add_argument("--pdf", action="store_true")
    run = commands.add_parser("run", help="invoke one explicitly registered tool")
    run.add_argument("tool")
    run.add_argument("--args-file", required=True, help="JSON object; paths inside resolve against caller cwd")
    run.add_argument("--output", required=True, help="new output directory; no overwrite or partial publication")
    run.add_argument("--config-file", help="explicit private config only; no defaults or credential environment discovery")
    run.add_argument("--allow-network", action="store_true", help="explicitly allow required external data/model requests")
    run.add_argument("--allow-model", action="store_true", help="explicitly allow model calls; also requires --allow-network")
    return result


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ToolError("INVALID_INPUT", "Duplicate JSON key in input.")
            result[key] = value
        return result
    def invalid(value):
        raise ToolError("INVALID_INPUT", "Nonfinite JSON numbers are not supported.")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=invalid)


def registry():
    return read_json(SKILL / "references/tool-registry.json")["tools"]


def scrub(value):
    if isinstance(value, str):
        for secret in SECRETS:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(api[_-]?key|authorization|access_token)([=:\s]+)([^\s&\"']+)", r"\1\2[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if re.search(r"(?i)(api.?key|access.?token|authorization|secret|password)", str(k)) else scrub(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def jsonable(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "columns") and hasattr(value, "to_json"):
        # split retains named row indexes needed for peer tables.
        return {"$table": json.loads(value.to_json(orient="split", date_format="iso"))}
    if hasattr(value, "item"):
        return jsonable(value.item())
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    raise ToolError("INVALID_RESULT", f"Unserializable result type: {type(value).__name__}", exit_code=3)


def unpack(value, caller):
    if isinstance(value, list):
        return [unpack(item, caller) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$csv"}:
        import pandas as pd
        return pd.read_csv((Path(caller) / value["$csv"]).resolve())
    if set(value) == {"$json"}:
        return unpack(read_json((Path(caller) / value["$json"]).resolve()), caller)
    if set(value) == {"$table"}:
        import pandas as pd
        data = value["$table"]
        return pd.DataFrame(**data) if isinstance(data, dict) else pd.DataFrame(data)
    return {key: unpack(item, caller) for key, item in value.items()}


def require_object(value):
    if not isinstance(value, dict):
        raise ToolError("INVALID_INPUT", "Arguments must be a JSON object.")


def nonempty(value, where):
    if value is None or (hasattr(value, "empty") and value.empty) or (isinstance(value, (str, dict, list, tuple)) and not value):
        raise ToolError("EMPTY_UPSTREAM_DATA", f"{where} returned no usable data.",
                        "Check source availability and supplied inputs; no report was published.")
    return value


def check_params(spec, values):
    require_object(values)
    allowed = {item["name"] for item in spec["parameters"]}
    missing = [item["name"] for item in spec["parameters"] if item["required"] and item["name"] not in values]
    unknown = set(values) - allowed
    if unknown or missing:
        raise ToolError("INVALID_ARGUMENT", f"Missing parameters: {missing}; unknown parameters: {sorted(unknown)}")
    for parameter in spec["parameters"]:
        name = parameter["name"]
        if name not in values:
            continue
        value, expected = values[name], parameter.get("type")
        if parameter.get("choices") and value not in parameter["choices"]:
            raise ToolError("INVALID_ARGUMENT", f"{name} must be one of {parameter['choices']}.")
        if spec["kind"] in ("enhanced_text", "agent_manager"):
            if expected in ("str", "string") and not isinstance(value, str):
                raise ToolError("INVALID_ARGUMENT", f"{name} requires a string.")
            if expected in ("Dict", "dict") and not isinstance(value, dict):
                raise ToolError("INVALID_ARGUMENT", f"{name} requires a JSON object.")
            if expected.startswith("List[") and not isinstance(value, list):
                raise ToolError("INVALID_ARGUMENT", f"{name} requires a JSON array.")
        if spec["kind"] == "source_main":
            is_list = parameter.get("nargs") is not None
            if is_list and not isinstance(value, list):
                raise ToolError("INVALID_ARGUMENT", f"{name} requires a JSON array.")
            if not is_list and isinstance(value, (list, dict)):
                raise ToolError("INVALID_ARGUMENT", f"{name} requires a scalar value.")
            items = value if is_list else [value]
            for item in items:
                if expected in ("str", "string") and not isinstance(item, str):
                    raise ToolError("INVALID_ARGUMENT", f"{name} requires string values.")
                if expected == "bool" and type(item) is not bool:
                    raise ToolError("INVALID_ARGUMENT", f"{name} requires a JSON boolean.")
                if expected == "int" and type(item) is not int:
                    raise ToolError("INVALID_ARGUMENT", f"{name} requires integer values.")
                if expected == "float" and (type(item) not in (int, float) or not math.isfinite(item)):
                    raise ToolError("INVALID_ARGUMENT", f"{name} requires finite numeric values.")
                if is_list and isinstance(item, str) and item.startswith("-"):
                    raise ToolError("INVALID_ARGUMENT", f"{name} array values cannot be command-line options.")
    for name in ("generate_text", "pdf"):
        if name in values and type(values[name]) is not bool:
            raise ToolError("INVALID_ARGUMENT", f"{name} requires a JSON boolean.")
    for name in ("ticker", "company_ticker", "target_ticker"):
        if name in values and (not isinstance(values[name], str) or not re.fullmatch(r"[A-Za-z0-9^.=-]{1,24}", values[name])):
            raise ToolError("INVALID_INPUT", "Ticker must be a bounded symbol, not a path.")
    for name in ("output_csv_name", "html_report_prefix"):
        if name in values and (not isinstance(values[name], str) or Path(values[name]).name != values[name] or values[name] in (".", "..")):
            raise ToolError("INVALID_INPUT", f"{name} must be a filename without directory components.")


def permissions(tool, spec, values, options):
    model = bool(spec.get("model") or values.get("generate_text") or values.get("generate_text_sections") or values.get("enable_text_regeneration"))
    network = bool(spec.get("network") or model or (tool == "pipeline.full" and "financial_data" not in values))
    if model and not options.get("allow_model"):
        raise ToolError("MODEL_DISABLED", "This invocation requires an explicitly enabled model.", "Add --allow-model and --allow-network only when a real model call is intended.")
    if network and not options.get("allow_network"):
        raise ToolError("NETWORK_DISABLED", "This invocation requires external network access.", "Add --allow-network only when an external request is intended.")
    return network, model


def private_config(options):
    config = configparser.ConfigParser(interpolation=None)
    config.add_section("API_KEYS")
    path = options.get("config_file")
    if path:
        with Path(path).open(encoding="utf-8") as stream:
            config.read_file(stream)
        for section in config.sections():
            for key, value in config.items(section):
                if re.search(r"key|token|secret|password", key, re.I) and value:
                    SECRETS.append(value)
    return config


def credential(config, name):
    value = config.get("API_KEYS", name, fallback="").strip()
    if not value or value.upper().startswith(("YOUR_", "PLACEHOLDER")):
        raise ToolError("CONFIG_REQUIRED", f"A configured {name} is required.",
                        "Use config-template and supply an explicit private --config-file. No credentials are discovered automatically.")
    return value


def manifest_check():
    path = RUNTIME / "MANIFEST.sha256"
    problems, count = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            problems.append("malformed manifest entry")
            continue
        expected, name = match.groups()
        entry = RUNTIME / name
        count += 1
        if entry.is_symlink() or not entry.resolve().is_relative_to(RUNTIME.resolve()) or not entry.is_file():
            problems.append(name)
        elif hashlib.sha256(entry.read_bytes()).hexdigest() != expected:
            problems.append(name)
    expected = read_json(SKILL / "SOURCE_PROVENANCE.json")["source_manifest_sha256"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        problems.append("source manifest digest mismatch")
    return {"ok": not problems and count > 0, "checked_files": count, "problems": problems}


def doctor():
    versions = {}
    for package in DEPENDENCIES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    data = {"python": sys.version.split()[0], "supported": sys.version_info >= (3, 11),
            "dependencies": versions, "missing": [name for name, version in versions.items() if version is None],
            "runtime_manifest": manifest_check(), "third_party_modules_imported": False,
            "network_called": False, "config_read": False,
            "install_commands": [["python3.11", "-m", "venv", ".venv"],
                                 [".venv/bin/python", "-m", "pip", "install", "-r", str(SKILL / "requirements.lock.txt")]]}
    if not data["runtime_manifest"]["ok"]:
        raise ToolError("INTEGRITY_CHECK_FAILED", "Frozen runtime integrity failed.", data=data)
    if not data["supported"] or data["missing"]:
        raise ToolError("DEPENDENCY_UNAVAILABLE", "Python 3.11+ and optional research dependencies are required for execution.",
                        "Follow data.install_commands in a project-local virtual environment; nothing is installed automatically.", exit_code=3, data=data)
    return data


def _numeric(value, name, *, positive_value=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (positive_value and value <= 0):
        raise ToolError("INVALID_INPUT", f"{name} must be a finite {'positive ' if positive_value else ''}number.")
    return value


def validate_forecast(frame, config):
    nonempty(frame, "historical metrics")
    actual = sorted(c for c in frame.columns if isinstance(c, str) and re.fullmatch(r"\d{4}A", c))
    if not actual or config.get("revenue_base_year") != actual[-1]:
        raise ToolError("INVALID_FORECAST", "revenue_base_year must be the latest actual year.")
    assumptions = config.get("revenue_growth_assumptions", {})
    if not assumptions or any(not re.fullmatch(r"\d{4}E", year) for year in assumptions):
        raise ToolError("INVALID_FORECAST", "Supply explicit YYYY E forecast columns, for example 2027E.")
    years = sorted(int(year[:-1]) for year in assumptions)
    if years != list(range(int(actual[-1][:-1]) + 1, int(actual[-1][:-1]) + 1 + len(years))):
        raise ToolError("INVALID_FORECAST", "Forecast years must be consecutive and strictly after all actual years.")
    for year, value in assumptions.items():
        if _numeric(value, year) <= -1:
            raise ToolError("INVALID_FORECAST", "Revenue growth must exceed -100%.")
    for metric in ("Revenue", "SG&A Margin", "EPS", "PE Ratio", "Contribution Margin", "EBITDA Margin"):
        row = frame.loc[frame["metrics"] == metric, actual[-1]]
        if row.empty or row.isna().any():
            raise ToolError("INVALID_FORECAST", f"Latest actual {metric} is missing.")


def _valuation(tool, values):
    from modules.valuation_engine import ValuationEngine
    financial = values["financial_data"]
    require_object(financial)
    _numeric(financial.get("shares_outstanding"), "shares_outstanding", positive_value=True)
    # Explicit scalars prevent silent EBITDA/FCF defaults or accidental table-unit inference.
    _numeric(financial.get("ebitda"), "ebitda", positive_value=True)
    if tool in ("valuation.dcf", "valuation.combined"):
        _numeric(financial.get("free_cash_flow"), "free_cash_flow", positive_value=True)
        assumptions = values.get("assumptions")
        required = {"growth_rate_1_5", "growth_rate_6_10", "terminal_growth", "wacc", "projection_years"}
        if not isinstance(assumptions, dict) or set(assumptions) != required:
            raise ToolError("INVALID_VALUATION", "DCF requires all five explicit assumptions; run describe.")
        for name in required:
            _numeric(assumptions[name], name)
        if assumptions["wacc"] <= max(assumptions["terminal_growth"], 0) or not 1 <= assumptions["projection_years"] <= 50 or type(assumptions["projection_years"]) is not int:
            raise ToolError("INVALID_VALUATION", "Require WACC > max(terminal growth, 0) and 1..50 integer projection years.")
    if tool in ("valuation.ev-ebitda", "valuation.combined"):
        _numeric(values.get("target_multiple"), "target_multiple", positive_value=True)
    # The original model prefers the last table forecast to explicit EBITDA; keep units explicit here.
    if "analysis" in financial:
        raise ToolError("INVALID_VALUATION", "Use explicit same-unit EBITDA/FCF/shares scalars; omit analysis from valuation financial_data.")
    engine = ValuationEngine(financial, values.get("peer_data"))
    outputs = []
    if tool in ("valuation.ev-ebitda", "valuation.combined"):
        outputs.append(engine.calculate_ev_ebitda_valuation(values["target_multiple"]))
    if tool in ("valuation.peer", "valuation.combined") and values.get("peer_data"):
        outputs.append(engine.calculate_peer_comparison_valuation())
    if tool == "valuation.peer" and not values.get("peer_data"):
        raise ToolError("INVALID_VALUATION", "Peer valuation requires peer_data with explicit positive ev_ebitda multiples.")
    if tool in ("valuation.dcf", "valuation.combined"):
        outputs.append(engine.calculate_dcf_valuation(values["assumptions"]))
    if not outputs or any(not math.isfinite(item.target_price) or item.target_price <= 0 for item in outputs):
        raise ToolError("INVALID_VALUATION", "The original valuation model produced no valid positive valuation.")
    return {"results": outputs, "synthesis": engine.synthesize_valuation(),
            "football_field": engine.generate_football_field_data(),
            "assumptions_notice": "Original simplified model assumes net debt equals 10% of enterprise value; confidence weights and ranges are heuristics, not measured probabilities."}


def _sensitivity(values):
    from modules.sensitivity_analyzer import SensitivityAnalyzer
    frame = nonempty(values["forecast"], "forecast")
    if not any(str(column).endswith("E") for column in frame.columns):
        raise ToolError("INVALID_INPUT", "Sensitivity analysis requires forecast columns ending E.")
    steps = values.get("steps", 5)
    if type(steps) is not int or not 2 <= steps <= 100:
        raise ToolError("INVALID_INPUT", "steps must be an integer between 2 and 100.")
    revenue_range, margin_range = values.get("revenue_range", [-0.05, 0.05]), values.get("margin_range", [-0.02, 0.02])
    for pair in (revenue_range, margin_range):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or _numeric(pair[0], "range") >= _numeric(pair[1], "range"):
            raise ToolError("INVALID_INPUT", "Sensitivity ranges must contain two ordered finite numbers.")
    analyzer = SensitivityAnalyzer(frame)
    result = {"revenue": analyzer.analyze_revenue_sensitivity(tuple(revenue_range), steps),
              "margin": analyzer.analyze_margin_sensitivity(tuple(margin_range), steps),
              "combined": analyzer.generate_sensitivity_table(tuple(revenue_range), tuple(margin_range), steps),
              "summary": analyzer.generate_sensitivity_summary(),
              "method_notice": "Deterministic scenario perturbations; no calibrated probability forecast."}
    for name in ("revenue", "margin", "combined"):
        nonempty(result[name], name)
    return result


def _news(tool, values):
    nonempty(values["news"], "news input")
    if tool == "catalyst.analyze":
        from modules.catalyst_analyzer import CatalystAnalyzer
        analyzer = CatalystAnalyzer(values["ticker"], company_name=values.get("company_name"))
        items = analyzer.identify_catalysts(values["news"], values.get("financial_calendar"))
        return {"catalysts": items, "summary": analyzer.generate_catalyst_summary(), "top": analyzer.get_top_catalysts(5),
                "method_notice": "Keyword/rule classifications and heuristic scores, not verified forward events."}
    from modules.news_integrator import NewsIntegrator
    analyzer = NewsIntegrator(values["ticker"], company_name=values.get("company_name"))
    analyzer.set_news_data(values["news"])
    items = analyzer.process_news(values.get("days_window", 5))
    return {"articles": items, "summary": analyzer.generate_news_summary(), "categories": analyzer.get_news_by_category()}


def _enhanced_text(spec, values, config):
    from modules.enhanced_text_generator import EnhancedTextGenerator
    for key in ("data", "forecast_config", "catalysts", "valuation_data", "analysis_data", "risks", "section_content"):
        if key in values:
            nonempty(values[key], key)
    if not spec["model"]:
        # These three public methods are pure text operations. Skip __init__,
        # which otherwise creates an SDK client and probes OPENAI_API_KEY.
        generator = object.__new__(EnhancedTextGenerator)
        if spec["method"] == "format_data_reference":
            for name in ("source", "metric"):
                nonempty(values[name].strip(), name)
        result = getattr(generator, spec["method"])(**values)
        nonempty(result, spec["method"])
        return {"text": result, "source_method": spec["method"], "model_called_by_this_tool": False,
                "source_labels": "Caller-supplied and not independently verified."}

    class StrictEnhancedGenerator(EnhancedTextGenerator):
        def _generate_fallback(self, context):
            raise ToolError("MODEL_GENERATION_FAILED", "Enhanced generator entered its fallback path.",
                            "Check the explicitly configured model service; fallback is not a completed analysis.", exit_code=3)

        def _generate_with_llm(self, system_prompt, user_prompt):
            # Retain the original public methods and prompts, but validate the
            # completion before the source helper discards its finish metadata.
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                    temperature=self.config.temperature, max_tokens=self.config.max_tokens)
                choice = response.choices[0]
                message = choice.message
                if (choice.finish_reason != "stop" or getattr(message, "refusal", None)
                        or getattr(message, "tool_calls", None) or not isinstance(message.content, str)
                        or not message.content.strip()):
                    raise ToolError("MODEL_GENERATION_FAILED", "Enhanced model response was incomplete, refused, or empty.", exit_code=3)
                return message.content
            except ToolError:
                raise
            except Exception:
                raise ToolError("MODEL_GENERATION_FAILED", "Enhanced model request did not return a usable completion.",
                                "Check the explicitly configured model service; no fallback result was accepted.", exit_code=3) from None

    generator = StrictEnhancedGenerator(api_key=credential(config, "openai_api_key"),
                                        base_url=config.get("API_KEYS", "openai_base_url", fallback=None))
    if generator.client is None:
        raise ToolError("MODEL_GENERATION_FAILED", "Enhanced generator could not initialize its model client.", exit_code=3)
    generator.config.model = credential(config, "openai_model")
    result = getattr(generator, spec["method"])(**values)
    if not isinstance(result, str) or not result.strip() or result.startswith("[Content generation pending"):
        raise ToolError("MODEL_GENERATION_FAILED", "Enhanced generator did not return usable model text.", exit_code=3)
    return {"text": result.strip(), "source_method": spec["method"], "model_called_by_this_tool": True,
            "model": generator.config.model, "content_facts_independently_verified": False}


def _agent_section(values, config):
    # Disable tracing before importing the SDK or any module that constructs agents.
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    import asyncio
    import pandas as pd
    data = values["data"]
    allowed = {"financial_metrics", "peer_ebitda", "peer_ev_ebitda", "company_news"}
    if set(data) - allowed:
        raise ToolError("INVALID_INPUT", "Agent data contains unsupported fields; use financial_metrics, peer_ebitda, peer_ev_ebitda or company_news.")
    table_rows = 0
    for name in ("financial_metrics", "peer_ebitda", "peer_ev_ebitda"):
        table = data.get(name)
        if table is not None:
            if not isinstance(table, pd.DataFrame):
                raise ToolError("INVALID_INPUT", f"Agent {name} requires a table, such as $csv or $table.")
            table_rows += len(table) if not table.empty else 0
    news = data.get("company_news", [])
    if news is None:
        news = []
    if not isinstance(news, list) or any(not isinstance(article, dict) or not any(isinstance(article.get(key), str) and article[key].strip() for key in ("title", "text")) for article in news):
        raise ToolError("INVALID_INPUT", "Agent company_news requires article objects with nonempty title or text.")
    if table_rows == 0 and not news:
        raise ToolError("EMPTY_UPSTREAM_DATA", "Agent has no nonempty financial tables or news to consume.")
    if values["text_type"] == "news_summary" and not news:
        raise ToolError("EMPTY_UPSTREAM_DATA", "The news_summary agent requires nonempty company_news.")
    import agents as sdk
    from openai import AsyncOpenAI
    sdk.set_tracing_disabled(True)
    if importlib.metadata.version("openai-agents") != "0.3.3":
        raise ToolError("DEPENDENCY_UNAVAILABLE", "The agent-manager adapter requires the verified openai-agents==0.3.3 interface.",
                        "Install the supplied project-local lock file.", exit_code=3)
    from modules.equity_agents import agent_manager as source_manager
    for key in ("company_name", "company_ticker"):
        nonempty(values[key].strip(), key)
    model_name = credential(config, "openai_model")
    client = AsyncOpenAI(api_key=credential(config, "openai_api_key"),
                         base_url=config.get("API_KEYS", "openai_base_url", fallback=None))
    model = sdk.OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    manager = source_manager.EquityResearchAgentManager()
    fields = {"tagline": "tagline", "company_overview": "overview", "investment_overview": "investment_update",
              "valuation_overview": "valuation_analysis", "risks": "risk_analysis", "competitor_analysis": "competitive_analysis",
              "major_takeaways": "takeaways", "news_summary": "news_summary"}
    section = values["text_type"]
    selected = manager.agents[section]
    if selected.tools or selected.handoffs or selected.mcp_servers:
        raise ToolError("UNEXPECTED_AGENT_CAPABILITIES", "The frozen agent unexpectedly requests tools, handoffs or MCP.", exit_code=3)
    manager.agents[section] = selected.clone(model=model, tools=[], handoffs=[], mcp_servers=[])
    original_runner, original_model_env = source_manager.Runner, os.environ.pop("OPENAI_MODEL_NAME", None)

    class CheckedRunner:
        @staticmethod
        async def run(agent, prompt, **kwargs):
            result = await original_runner.run(agent, prompt,
                                               run_config=sdk.RunConfig(model=model, tracing_disabled=True,
                                                                        trace_include_sensitive_data=False), max_turns=1)
            output = result.final_output
            field = fields[section]
            if not isinstance(output, agent.output_type) or not isinstance(getattr(output, field, None), str) or not getattr(output, field).strip():
                raise ToolError("INVALID_AGENT_OUTPUT", "Typed agent result is missing its nonempty section field.",
                                "The source manager's string fallback is disabled; retry with valid structured model output.", exit_code=3)
            return result

    source_manager.Runner = CheckedRunner
    async def execute():
        try:
            return await manager.generate_text_section(**values)
        finally:
            await client.close()
    try:
        text = asyncio.run(execute())
    finally:
        source_manager.Runner = original_runner
        if original_model_env is not None:
            os.environ["OPENAI_MODEL_NAME"] = original_model_env
    return {"text": text.strip(), "text_type": section, "source_manager": "EquityResearchAgentManager",
            "model": model_name, "model_called_by_this_tool": True, "tracing_enabled": False,
            "tools_enabled": False, "handoffs_enabled": False, "content_facts_independently_verified": False}


def _report(tool, values, stage):
    data = dict(values["data"])
    for field in ("company_name_full", "company_ticker", "tagline", "company_overview", "investment_overview",
                  "valuation_overview", "risks", "competitor_analysis", "major_takeaways"):
        nonempty(data.get(field), f"report field {field}")
    if "analysis_df" not in data:
        raise ToolError("INVALID_REPORT", "report data must include a nonempty analysis_df table.")
    nonempty(data["analysis_df"], "report financial table")
    data.setdefault("research_source", "Secondary Market Research Kit — built on FinRobot")
    data.setdefault("data_source_text", "Caller-supplied inputs; provenance not independently certified")
    if tool == "report.html":
        from modules.html_template_professional import render_professional_html_report
        from modules.html_renderer import format_dataframe_to_html_table
        data.setdefault("financial_summary_table_html", format_dataframe_to_html_table(data["analysis_df"]))
        payload = render_professional_html_report(data)
        if not payload or "<html" not in payload.lower() or len(payload) < 1000:
            raise ToolError("REPORT_GENERATION_FAILED", "HTML renderer produced no valid document.", exit_code=3)
        # The source template includes CDN CSS/font loaders. Embedded template CSS
        # remains usable offline; report viewing must not fetch those resources.
        payload = re.sub(r'<script\b[^>]*\bsrc=["\']https?://[^>]*>.*?</script>', '', payload, flags=re.I | re.S)
        payload = re.sub(r'<link\b[^>]*(?:href)=["\']https?://[^>]*>', '', payload, flags=re.I)
        (stage / "report.html").write_text(payload, encoding="utf-8")
    else:
        from modules import professional_pdf_report
        original_paragraph = professional_pdf_report.Paragraph
        def source_aware_paragraph(text, *args, **kwargs):
            if text == "Source: Company Filings":
                text = "Source: " + data["data_source_text"]
            return original_paragraph(text, *args, **kwargs)
        professional_pdf_report.Paragraph = source_aware_paragraph
        report = professional_pdf_report.ProfessionalEquityReport(str(stage / "report.pdf"), data)
        original_metadata = report._add_pdf_metadata
        def metadata(canvas, doc):
            original_metadata(canvas, doc)
            canvas.setCreator("Secondary Market Research Kit — built on FinRobot")
        report._add_pdf_metadata = metadata
        try:
            report.build()
        finally:
            professional_pdf_report.Paragraph = original_paragraph
        if not (stage / "report.pdf").is_file() or not (stage / "report.pdf").read_bytes().startswith(b"%PDF-"):
            raise ToolError("REPORT_GENERATION_FAILED", "PDF renderer produced no valid PDF.", exit_code=3)
    return {"format": tool.split(".")[1], "model_called_by_this_tool": False,
            "text_provenance": "Caller-supplied; rendering does not validate its claims."}


def _pipeline(values, stage, config, options):
    from modules.financial_data_processor import extract_historical_metrics_from_api_data, calculate_growth_and_forecasts
    from modules.chart_generator import generate_revenue_ebitda_chart, generate_eps_pe_chart
    if values.get("period", "annual") != "annual":
        raise ToolError("INVALID_INPUT", "The annual forecast pipeline requires period=annual.")
    if "financial_data" in values:
        financial = values["financial_data"]
    else:
        from modules.market_data_api import get_comprehensive_financial_data
        financial = get_comprehensive_financial_data(values["ticker"], credential(config, "fmp_api_key"), "annual", values.get("years_limit", 5))
    for name in ("income_statement", "balance_sheet", "cash_flow", "ratios", "key_metrics"):
        nonempty(financial.get(name), f"financial_data.{name}")
    historical = nonempty(extract_historical_metrics_from_api_data(financial), "historical calculation")
    validate_forecast(historical, values["forecast_config"])
    forecast = nonempty(calculate_growth_and_forecasts(historical, values["forecast_config"]), "forecast calculation")
    forecast.to_csv(stage / "financial_metrics_and_forecasts.csv", index=False)
    for name, frame in financial.items():
        if name in ("income_statement", "balance_sheet", "cash_flow", "ratios", "key_metrics"):
            frame.to_csv(stage / f"{name}_raw_data.csv", index=False)
    valuations = _valuation("valuation.combined", values["valuation_inputs"])
    sensitivity = _sensitivity({"forecast": forecast})
    catalysts = _news("catalyst.analyze", {"ticker": values["ticker"], "company_name": values["company_name"], "news": values["news"]}) if values.get("news") else {"status": "NOT_REQUESTED_NO_NEWS"}
    supplied = dict(values.get("text_sections") or {})
    generated = []
    if values.get("generate_text"):
        from modules.text_generator_agents import generate_text_section
        for section in SECTIONS:
            supplied[section] = generate_text_section({"financial_metrics": forecast, "company_news": values.get("news"),
                                                       "sensitivity_analysis": jsonable(sensitivity), "catalyst_analysis": jsonable(catalysts),
                                                       "valuation_analysis": jsonable(valuations)}, section,
                                                      credential(config, "openai_api_key"), values["company_name"], values["ticker"],
                                                      base_url=config.get("API_KEYS", "openai_base_url", fallback=None),
                                                      model=config.get("API_KEYS", "openai_model", fallback=None), strict=True)
            nonempty(supplied[section], f"model section {section}")
            generated.append(section)
    for section in SECTIONS:
        nonempty(supplied.get(section), f"text_sections.{section}")
        (stage / (section + ".txt")).write_text(supplied[section], encoding="utf-8")
    chart1 = generate_revenue_ebitda_chart(forecast, str(stage / "revenue_ebitda.png"), values["ticker"])
    chart2 = generate_eps_pe_chart(forecast, str(stage / "eps_pe.png"), values["ticker"])
    for name in ("revenue_ebitda.png", "eps_pe.png"):
        nonempty((stage / name).read_bytes() if (stage / name).is_file() else None, name)
    # Relative paths keep HTML portable after the staging directory is renamed.
    data = {**supplied, "company_name_full": values["company_name"], "company_ticker": values["ticker"],
            "analysis_df": forecast, "financial_summary_df": forecast,
            "revenue_chart_path": "revenue_ebitda.png", "eps_pe_chart_path": "eps_pe.png",
            "revenue_chart_base64": chart1, "eps_pe_chart_base64": chart2,
            "valuation_analysis": jsonable(valuations["synthesis"]), "sensitivity_analysis": jsonable(sensitivity),
            "catalyst_analysis": jsonable(catalysts), "rating": "Unrated research scenario",
            "share_price": str(values["valuation_inputs"]["financial_data"].get("current_price", "N/A")),
            "target_price": str(valuations["synthesis"]["target_price"]),
            "research_source": "Secondary Market Research Kit — built on FinRobot",
            "data_source_text": "Caller-supplied local inputs" if "financial_data" in values else "FMP API responses; not independently certified",
            "disclaimer_text": valuations["assumptions_notice"]}
    _report("report.html", {"data": data}, stage)
    if values.get("pdf"):
        _report("report.pdf", {"data": {**data, "revenue_chart_path": chart1, "eps_pe_chart_path": chart2}}, stage)
    summary = {"ticker": values["ticker"], "forecast_years": list(values["forecast_config"]["revenue_growth_assumptions"]),
               "valuations": valuations, "sensitivity": sensitivity, "catalysts": catalysts,
               "text_generation": {"enabled": bool(values.get("generate_text")), "successful_sections": generated,
                                   "fallback_sections": [], "supplied_sections": [] if generated else list(SECTIONS)},
               "input_mode": "CALLER_SUPPLIED" if "financial_data" in values else "FMP_API",
               "scope": "Research calculations and rendered artifacts only; no causal investment-return validation."}
    (stage / "analysis_summary.json").write_text(json.dumps(scrub(jsonable(summary)), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return summary


def _source_main(tool, spec, values, stage, config, options):
    from modules import common_utils
    # Avoid the source's implicit local config lookup, even for offline rendering.
    selected_config = config
    if not options.get("allow_network"):
        selected_config = configparser.ConfigParser()
        selected_config.add_section("API_KEYS")
    common_utils.load_config = lambda config_path=None: selected_config
    module = importlib.import_module(spec["module"])
    args = dict(values)
    args["output_dir"] = str(stage)
    model_failures = []
    if values.get("generate_text_sections") or values.get("enable_text_regeneration"):
        credential(config, "openai_api_key")
        original_generator = module.generate_text_section
        def strict_generation(*positional, **named):
            named["strict"] = True
            try:
                return nonempty(original_generator(*positional, **named), "generated section")
            except Exception:
                model_failures.append("failed")
                raise
        module.generate_text_section = strict_generation
    if tool == "source.financial-analysis":
        credential(config, "fmp_api_key")
        if args.get("generate_text_sections"):
            credential(config, "openai_api_key")
        if args.get("period", "annual") != "annual":
            raise ToolError("INVALID_INPUT", "Original annual forecast main does not safely aggregate quarterly inputs.")
        # Reject the original hardcoded projection years before any malformed result can publish.
        original_forecast = module.calculate_growth_and_forecasts
        def checked_forecast(frame, forecast_config):
            validate_forecast(frame, forecast_config)
            return original_forecast(frame, forecast_config)
        module.calculate_growth_and_forecasts = checked_forecast
    elif tool == "source.html-report":
        args["skip_auto_fetch"] = not options.get("allow_network") or args.get("skip_auto_fetch", False)
        args.setdefault("research_source", "Secondary Market Research Kit — built on FinRobot")
        args.setdefault("data_source_text", "Caller-supplied inputs")
        args.setdefault("analyst_names", ["Research author"])
        args.setdefault("logo_image_path", "")
    else:
        args["skip_market_fetch"] = not options.get("allow_network") or args.get("skip_market_fetch", False)
        args.setdefault("research_source", "Secondary Market Research Kit — built on FinRobot")
        args.setdefault("analyst_names", ["Research author"])
        if not args.get("analysis_dir"):
            raise ToolError("INVALID_ARGUMENT", "source.pdf-report requires explicit analysis_dir.")
    for key, value in list(args.items()):
        if key.endswith(("_file", "_csv", "_path", "_dir")) and key != "output_dir" and value:
            args[key] = str((Path(options["caller_cwd"]) / value).resolve())
            if not Path(args[key]).exists():
                raise ToolError("INVALID_INPUT", f"Missing input file for {key}.")
    if tool != "source.financial-analysis":
        import pandas as pd
        financial_csv = Path(args["analysis_csv"]) if tool == "source.html-report" else Path(args["analysis_dir"]) / "financial_metrics_and_forecasts.csv"
        nonempty(pd.read_csv(financial_csv), "source report financial analysis")
        text_paths = [Path(args[section + "_file"]) for section in SECTIONS[:-1]] if tool == "source.html-report" else [Path(args["analysis_dir"]) / (section + ".txt") for section in SECTIONS[:-1]]
        for path in text_paths:
            nonempty(path.read_text(encoding="utf-8").strip(), "source report text section")
    arguments = [str(SOURCE / (spec["module"] + ".py"))]
    for key, value in args.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                arguments.append(flag)
        elif isinstance(value, list):
            arguments.extend([flag, *(str(item) for item in value)])
        elif value is not None:
            arguments.append(flag + "=" + str(value))
    old = sys.argv
    try:
        sys.argv = arguments
        code = module.main()
    finally:
        sys.argv = old
    if code not in (None, 0) or model_failures:
        raise ToolError("SOURCE_PIPELINE_FAILED", f"Original entrypoint returned {code}.",
                        "Upstream data/model generation failed; the new output directory was not published.", exit_code=3)
    if tool == "source.financial-analysis":
        summary = read_json(stage / "analysis_summary.json")
        text = summary.get("text_generation", {})
        if text.get("enabled") and (text.get("fallback_sections") or len(text.get("successful_sections", [])) != len(SECTIONS)):
            raise ToolError("INCOMPLETE_MODEL_OUTPUT", "A source section failed or was skipped; the full model report is incomplete.", exit_code=3)
        import pandas as pd
        nonempty(pd.read_csv(stage / summary["files_generated"]["main_analysis"]), "financial analysis CSV")
        return summary
    suffix = ".html" if tool == "source.html-report" else ".pdf"
    artifacts = list(stage.glob("*" + suffix))
    if not artifacts or any(path.stat().st_size < 500 for path in artifacts):
        raise ToolError("REPORT_GENERATION_FAILED", "Original renderer did not produce a nonempty report.", exit_code=3)
    return {"source_entrypoint": spec["module"], "report_count": len(artifacts), "rendered_only": True}


def invoke(tool, values, stage, config, options):
    spec = registry().get(tool)
    if spec is None:
        raise ToolError("UNKNOWN_TOOL", "Unknown registered tool.", "Run list for supported tool names.")
    check_params(spec, values)
    permissions(tool, spec, values, options)
    if spec["kind"] == "source_main":
        return _source_main(tool, spec, values, stage, config, options)
    if spec["kind"] == "function":
        module = importlib.import_module(spec["module"])
        function = getattr(module, spec["function"])
        call = dict(values)
        if spec.get("credential"):
            call["api_key"] = credential(config, spec["credential"])
        if tool == "text.section":
            call.update(strict=True, base_url=config.get("API_KEYS", "openai_base_url", fallback=None),
                        model=config.get("API_KEYS", "openai_model", fallback=None))
        if tool == "finance.forecast":
            validate_forecast(call["df_historical"], call["forecast_config"])
        if "output_path" in inspect.signature(function).parameters:
            call["output_path"] = str(stage / "chart.png")
        result = function(**call)
        nonempty(result, tool)
        if tool.startswith("chart."):
            if not (stage / "chart.png").is_file():
                raise ToolError("CHART_GENERATION_FAILED", "Chart renderer returned without an image.", exit_code=3)
            return {"chart": "chart.png"}
        if tool == "data.financials":
            for name in ("income_statement", "balance_sheet", "cash_flow", "ratios", "key_metrics"):
                nonempty(result.get(name), name)
        if tool == "data.technical" and not any(isinstance(result.get(name), (float, int)) and math.isfinite(result[name])
                                                for name in ("sma50", "sma200", "rsi14", "macd", "price")):
            raise ToolError("EMPTY_UPSTREAM_DATA", "No technical indicator data was returned.")
        if tool == "data.company-metrics" and not any(isinstance(result.get(name), (float, int)) and math.isfinite(result[name])
                                                      for name in ("share_price", "market_cap", "shares_outstanding")):
            raise ToolError("EMPTY_UPSTREAM_DATA", "No core company market metrics were returned.")
        if tool == "news.enhanced":
            nonempty(result.get("articles"), "enhanced news articles")
        if tool in ("data.peers", "data.ratios"):
            for index, item in enumerate(result):
                nonempty(item, f"{tool} table {index}")
        return result
    if spec["kind"] == "enhanced_chart":
        from modules.enhanced_chart_generator import EnhancedChartGenerator
        result = getattr(EnhancedChartGenerator(), spec["method"])(**values, output_dir=str(stage))
        nonempty(result, tool)
        if not list(stage.glob("*.png")):
            raise ToolError("CHART_GENERATION_FAILED", "Enhanced chart renderer produced no PNG.", exit_code=3)
        return {"charts": [path.name for path in stage.glob("*.png")]}
    if spec["kind"] == "enhanced_text":
        return _enhanced_text(spec, values, config)
    if spec["kind"] == "agent_manager":
        return _agent_section(values, config)
    if tool.startswith("valuation."):
        return _valuation(tool, values)
    if tool == "sensitivity.analyze":
        return _sensitivity(values)
    if tool in ("catalyst.analyze", "news.process"):
        return _news(tool, values)
    if tool.startswith("report."):
        return _report(tool, values, stage)
    if tool == "pipeline.full":
        return _pipeline(values, stage, config, options)
    if tool == "sentiment.snapshot":
        from modules.retail_sentiment_client import RetailSentimentClient
        client = RetailSentimentClient(credential(config, "adanos_api_key"), config.get("API_KEYS", "adanos_base_url", fallback="https://api.adanos.org"))
        result = client.get_snapshot(**values)
        if result.get("coverage_ratio", 0) <= 0:
            raise ToolError("EMPTY_UPSTREAM_DATA", "No retail sentiment source returned data.")
        return result
    raise ToolError("UNKNOWN_TOOL", "No dispatcher for this tool.")


def output_inventory(stage):
    result = []
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise ToolError("INVALID_ARTIFACT", "Generated artifacts must not be symlinks.")
        if path.is_file():
            result.append({"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def publish_new_directory(stage, output):
    """Reserve output exclusively, link complete files without replacing any entry.

    On error, roll back only entries whose inode still matches this invocation.
    A concurrent writer's entries, including a newly populated directory, survive.
    """
    owned = []
    try:
        output.mkdir()
        owned.append((output, output.stat(), True))
        for source in sorted(stage.rglob("*"), key=lambda path: (len(path.parts), path.as_posix())):
            destination = output / source.relative_to(stage)
            if source.is_dir():
                destination.mkdir()
                owned.append((destination, destination.stat(), True))
            elif source.is_file() and not source.is_symlink():
                os.link(source, destination)
                owned.append((destination, destination.stat(), False))
            else:
                raise ToolError("INVALID_ARTIFACT", "Unsupported generated filesystem entry.")
    except Exception as exc:
        for path, original, directory in reversed(owned):
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    path.rmdir() if directory else path.unlink()
            except OSError:
                pass
        if isinstance(exc, FileExistsError):
            raise ToolError("OUTPUT_EXISTS", "Output was concurrently created; existing data was preserved.") from None
        raise


def offline_guard():
    def audit(event, args):
        if event in ("socket.connect", "socket.connect_ex", "socket.getaddrinfo"):
            raise ToolError("NETWORK_DISABLED", "An offline tool attempted a network operation.")
    sys.addaudithook(audit)


def _work(options):
    command = options["command"]
    if command in ("list", "describe"):
        return {"tools": registry(), "tool_count": len(registry()), "python_minimum": "3.11",
                "commands": ["describe", "list", "doctor", "config-template --output NEW_FILE", "demo --output NEW_DIR [--pdf]",
                             "run TOOL --args-file JSON --output NEW_DIR [--config-file PRIVATE_INI] [--allow-network] [--allow-model]"],
                "input_markers": {"$csv": "Local CSV path -> DataFrame; resolves against caller cwd",
                                  "$json": "Local JSON file -> recursive data; resolves against caller cwd",
                                  "$table": "DataFrame records or split object {columns,index,data}"},
                "output_contract": "One JSON envelope except --help. Tables use $table split format; nonfinite outputs become null.",
                "exit_codes": {"0": "completed at the declared evidence level", "2": "input/capability/integrity failure", "3": "dependency/provider/runtime failure"},
                "model_requirements": "Both --allow-network and --allow-model; explicit config only. Strict generation rejects empty/fallback output.",
                "scope": "No trading. Live requests and model content are not independently verified investment evidence."}, "INTERFACE_METADATA"
    if command == "doctor":
        return doctor(), "LOCAL_ENVIRONMENT_CHECKS"
    if command == "config-template":
        path = Path(options["output"])
        if path.exists() or path.is_symlink():
            raise ToolError("OUTPUT_EXISTS", "Configuration output already exists.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(CONFIG_TEMPLATE)
        return {"output": str(path), "contains_credentials": False}, "CONFIG_TEMPLATE_ONLY"
    if sys.version_info < (3, 11):
        raise ToolError("DEPENDENCY_UNAVAILABLE", "Python 3.11 or newer is required.", exit_code=3)
    integrity = manifest_check()
    if not integrity["ok"]:
        raise ToolError("INTEGRITY_CHECK_FAILED", "Frozen runtime integrity failed.", data=integrity)
    output = Path(options["output"])
    if output.exists() or output.is_symlink():
        raise ToolError("OUTPUT_EXISTS", "Output directory already exists; choose a new directory.")
    if command == "demo":
        raw = read_json(SKILL / "examples/offline-pipeline.json")
        raw["pdf"] = options.get("pdf", False)
        tool, level = "pipeline.full", "SYNTHETIC_FIXTURE_ONLY"
    else:
        tool = options["tool"]
        raw = read_json(options["args_file"])
        spec = registry().get(tool)
        if spec is None:
            raise ToolError("UNKNOWN_TOOL", "Unknown registered tool.")
        check_params(spec, raw)
        network, model = permissions(tool, spec, raw, options)
        level = "MODEL_ASSISTED_RESEARCH" if model else "EXTERNAL_DATA_UNVERIFIED" if network else "LOCAL_INPUT_CALCULATIONS"
    if not options.get("allow_network"):
        offline_guard()
    config = private_config(options)
    with tempfile.TemporaryDirectory(prefix="equity-runtime-") as temp:
        os.environ["MPLCONFIGDIR"] = str(Path(temp) / "matplotlib")
        os.environ["XDG_CACHE_HOME"] = str(Path(temp) / "cache")
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(SOURCE))
        os.chdir(temp)
        # yfinance otherwise stores cookies/timezones in a user-level cache.
        # Keep that optional library's writes inside this invocation's temp area.
        if tool.startswith(("data.", "source.")) or tool == "finance.peer-forecast" or (tool == "pipeline.full" and "financial_data" not in raw):
            import yfinance
            yfinance.set_tz_cache_location(str(Path(temp) / "yfinance"))
        values = unpack(raw, options["caller_cwd"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".equity-stage-", dir=output.parent) as staging:
            stage = Path(staging) / "artifacts"
            stage.mkdir()
            result = invoke(tool, values, stage, config, options)
            payload = scrub(jsonable(result))
            (stage / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            inventory = output_inventory(stage)
            if output.exists() or output.is_symlink():
                raise ToolError("OUTPUT_EXISTS", "Output appeared while processing; no existing data was overwritten.")
            publish_new_directory(stage, output)
    return {"tool": tool, "output": str(output), "result": payload, "artifacts": inventory,
            "network_enabled": bool(options.get("allow_network")), "model_enabled": bool(options.get("allow_model")),
            "market_provenance_certified": False}, level


def envelope(command, data=None, level="NONE", error=None):
    return {"schema_version": SCHEMA, "ok": error is None, "command": command, "evidence_level": level,
            "data": data, "error": error}


def failed(command, exc):
    if isinstance(exc, ToolError):
        error = exc
    elif isinstance(exc, (ModuleNotFoundError, ImportError)):
        error = ToolError("DEPENDENCY_UNAVAILABLE", f"Required dependency unavailable: {getattr(exc, 'name', '')}",
                          "Run doctor and install the project-local lock file using its reported commands.", exit_code=3)
    elif isinstance(exc, (ValueError, TypeError, KeyError, FileNotFoundError)):
        error = ToolError("INVALID_INPUT", f"{type(exc).__name__}: {exc}")
    else:
        # Do not echo provider exceptions, request URLs, headers, captured logs or configuration.
        error = ToolError("RUNTIME_FAILED", f"Research tool failed ({type(exc).__name__}). No new output was published.",
                          "Check input completeness and provider availability. Failure text and logs are withheld to protect credentials.", exit_code=3)
    return {"envelope": envelope(command, data=scrub(jsonable(error.data)),
                                 error={"code": error.code, "message": scrub(str(error)), "hint": scrub(error.hint)}),
            "exit_code": error.exit_code}


def _worker():
    options = json.load(sys.stdin)
    command = options["command"] + (" " + options["tool"] if options.get("tool") else "")
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data, level = _work(options)
        result = {"envelope": envelope(command, data=jsonable(data), level=level), "exit_code": 0}
    except (Exception, SystemExit) as exc:
        result = failed(command, exc)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    command = "unknown"
    try:
        options = vars(parser().parse_args(argv))
        command = options["command"] + (" " + options["tool"] if options.get("tool") else "")
        options["caller_cwd"] = os.getcwd()
        for key in ("output", "args_file", "config_file"):
            if options.get(key):
                options[key] = os.path.abspath(Path(options[key]).expanduser())
        # Source adapters must not silently select unrelated saved credentials.
        env = {key: value for key, value in os.environ.items()
               if not re.search(r"(?i)(api_?key|access_?token|secret|password|^OPENAI_|^ADANOS_|^FMP_)", key)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
        process = subprocess.Popen([sys.executable, "-B", "-c", "import runpy,sys; runpy.run_path(sys.argv[1])['_worker']()", str(Path(__file__).resolve())],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", env=env, start_new_session=os.name == "posix")
        try:
            stdout, stderr = process.communicate(json.dumps(options), timeout=options["timeout"])
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise ToolError("TIMEOUT", "Research command exceeded the specified timeout.",
                            "No completed result is certified; inspect any temporary output before retrying.", exit_code=3) from None
        if process.returncode:
            raise ToolError("WORKER_FAILED", f"Research worker exited {process.returncode}.", exit_code=3)
        result = json.loads(stdout)
    except Exception as exc:
        result = failed(command, exc)
    print(json.dumps(result["envelope"], ensure_ascii=False, allow_nan=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
