"""Bounded adapters; optional dependencies are imported only for the selected tool."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import base64
import hashlib
from html import escape
from html.parser import HTMLParser
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import types
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tool_contracts import ToolError, redact, sha256


ROOT = Path(__file__).resolve().parents[1]
CLASS_MODULES = {
    "YFinanceUtils": "yfinance_utils", "FMPUtils": "fmp_utils", "SECUtils": "sec_utils",
    "FinnHubUtils": "finnhub_utils", "RedditUtils": "reddit_utils", "FinNLPUtils": "finnlp_utils",
}


class LazyClass:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        module = importlib.import_module("finrobot.data_source." + CLASS_MODULES[self.name])
        return getattr(getattr(module, self.name), name)


def namespace_shim(workdir):
    """Avoid original eager package initializers; preserve each selected module's code."""
    for name in ("finrobot", "finrobot.data_source", "finrobot.functional"):
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT / "runtime" / Path(*name.split(".")))]
        module.__package__ = name
        sys.modules[name] = module
    source = sys.modules["finrobot.data_source"]
    for name in CLASS_MODULES:
        setattr(source, name, LazyClass(name))
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["MPLCONFIGDIR"] = str(workdir / ".cache" / "matplotlib")
    os.environ["XDG_CACHE_HOME"] = str(workdir / ".cache")
    os.environ["HF_HOME"] = str(workdir / ".cache" / "huggingface")


def _cache_hooks(spec, workdir):
    if any(p["module"] == "yfinance" for p in spec["requires"]["packages"]):
        module = importlib.import_module("yfinance")
        if hasattr(module, "set_tz_cache_location"):
            module.set_tz_cache_location(str(workdir / ".cache" / "yfinance"))
    if spec["id"].startswith(("sec.", "analysis.", "report.")):
        # The import only occurs for tools whose dependency group includes sec_api.
        module = importlib.import_module("finrobot.data_source.sec_utils")
        module.CACHE_PATH = str(workdir / ".cache" / "sec")


def jsonable(value):
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    module = type(value).__module__
    if module.startswith("pandas") and hasattr(value, "to_json"):
        orient = "split" if getattr(value, "ndim", 1) == 2 else "index"
        return {"type": type(value).__name__, "data": json.loads(value.to_json(orient=orient, date_format="iso"))}
    if module.startswith("numpy") and hasattr(value, "item"):
        return jsonable(value.item())
    raise ToolError("UNSUPPORTED_RESULT", "The upstream result cannot be represented as bounded JSON.", exit_code=3)


def legacy(spec, args, workdir):
    namespace_shim(workdir)
    _cache_hooks(spec, workdir)
    module = importlib.import_module(spec["module"])
    fn = getattr(getattr(module, spec["class"]), spec["method"])
    kwargs = dict(args)
    for name, relative in spec.get("inject_outputs", {}).items():
        path = workdir / "artifacts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "save_folder":
            path.mkdir()
        kwargs[name] = str(path)
    if spec["id"] == "backtrader.back_test":
        # No arbitrary class paths, sizer classes, indicator or eval/import kwargs are accepted.
        kwargs["strategy_params"] = json.dumps(kwargs.get("strategy_params", {}))
    if spec["id"] == "report.build_annual_report":
        for name in ("operating_results", "market_position", "business_overview", "risk_assessment", "competitors_analysis"):
            kwargs[name] = escape(kwargs[name])
        for name in ("share_performance_image_path", "pe_eps_performance_image_path"):
            source = Path(kwargs[name])
            target = workdir / "inputs" / (name + source.suffix.lower())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            kwargs[name] = str(target)
    result = fn(**kwargs)
    if result is None:
        raise ToolError("UPSTREAM_EMPTY", "The upstream function returned no result; inspect provider entitlement and inputs.", exit_code=3)
    if isinstance(result, str) and result.strip() in {"No data available", "No BVPS data available", "No close date data found", "N/A", "Not Given"}:
        raise ToolError("UPSTREAM_NO_DATA", "The upstream provider returned a no-data sentinel.", exit_code=3)
    if type(result).__module__.startswith("pandas") and getattr(result, "empty", False):
        raise ToolError("UPSTREAM_NO_DATA", "The upstream provider returned an empty table.", exit_code=3)
    if isinstance(result, str) and re.search(r"^(?:Failed to|Error:|❌)|downloaded failed", result, re.I):
        raise ToolError("UPSTREAM_REPORTED_ERROR", result, exit_code=3)
    # Some upstream methods catch exceptions and return an apparently successful message.
    for name, relative in spec.get("inject_outputs", {}).items():
        path = workdir / "artifacts" / relative
        if name != "save_folder" and not path.is_file():
            raise ToolError("UPSTREAM_ARTIFACT_MISSING", "The expected upstream artifact was not created.", exit_code=3)
        if name == "save_folder" and not any(p.is_file() for p in path.rglob("*")):
            raise ToolError("UPSTREAM_ARTIFACT_MISSING", "No filing artifact was downloaded.", exit_code=3)
    return jsonable(result)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise ToolError("REDIRECT_BLOCKED", "The provider redirected outside its declared HTTPS origin.", exit_code=3)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, headers):
    opener = build_opener(SameOriginRedirect())
    with opener.open(Request(url, headers=headers), timeout=30) as response:
        payload = response.read(20_000_001)
        if len(payload) > 20_000_000:
            raise ToolError("RESPONSE_TOO_LARGE", "Provider response exceeds 20 MB.", exit_code=3)
        return payload, response.headers.get("Content-Type", "")


def write_documents(workdir, docs):
    path = workdir / "artifacts" / "documents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(docs), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {"document_count": len(docs), "documents_path": str(path), "documents_sha256": sha256(path)}


def earnings(args, workdir):
    url = f"https://discountingcashflows.com/api/transcript/{args['ticker']}/{args['quarter']}/{args['year']}/"
    credentials = os.environ["DCF_USERNAME"] + ":" + os.environ["DCF_PASSWORD"]
    payload, _ = fetch(url, {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(), "Accept": "application/json"})
    try:
        records = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ToolError("UPSTREAM_INVALID_JSON", "Transcript provider did not return JSON.", exit_code=3) from None
    if not isinstance(records, list) or not records:
        raise ToolError("UPSTREAM_EMPTY", "Transcript provider returned no records.", exit_code=3)
    docs = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("content"), str) or not record["content"].strip():
            raise ToolError("UPSTREAM_INVALID_RESULT", "Transcript records lack nonempty content.", exit_code=3)
        docs.append({"page_content": record["content"], "metadata": {"source_url": url,
            "source_sha256": hashlib.sha256(payload).hexdigest(), "captured_at": datetime.now(timezone.utc).isoformat(),
            "ticker_requested": args["ticker"], "year_requested": args["year"], "quarter_requested": args["quarter"],
            "provider_date": record.get("date"), "provider_year": record.get("year"),
            "provider_identity_authenticated": False}})
    return write_documents(workdir, docs)


class ArchiveText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.suppressed += 1
        if tag in {"p", "br", "div", "tr", "h1", "h2", "h3", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        if tag in {"p", "div", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def sec_archive(args, workdir):
    identity = os.environ["SEC_USER_AGENT"]
    if "@" not in identity or any(c in identity for c in "\r\n"):
        raise ToolError("INVALID_SEC_IDENTITY", "SEC_USER_AGENT must contain your own organization/contact email and no newline.")
    payload, content_type = fetch(args["url"], {"User-Agent": identity, "Accept": "text/html,text/plain,application/pdf"})
    artifact = workdir / "artifacts"
    artifact.mkdir(parents=True, exist_ok=True)
    if payload.startswith(b"%PDF-"):
        raw = artifact / "filing.pdf"
        raw.write_bytes(payload)
        return {"raw_path": str(raw), "sha256": sha256(raw), "source_url": args["url"],
                "text_extracted": False, "reason": "PDF is archived only; provide an explicitly converted local text file for retrieval."}
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError:
        raise ToolError("UNSUPPORTED_ENCODING", "SEC archive is not UTF-8 text or a PDF.", exit_code=3) from None
    raw = artifact / "filing.html" if "html" in content_type.lower() or "<html" in text.lower() else artifact / "filing.txt"
    raw.write_bytes(payload)
    if raw.suffix == ".html":
        parser = ArchiveText()
        parser.feed(text)
        text = re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()
    if not text.strip():
        raise ToolError("UPSTREAM_EMPTY", "SEC archive yielded no readable text.", exit_code=3)
    result = write_documents(workdir, [{"page_content": text, "metadata": {"source_url": args["url"],
        "source_sha256": hashlib.sha256(payload).hexdigest(), "captured_at": datetime.now(timezone.utc).isoformat(),
        "text_transform": "html-text-parser-v1" if raw.suffix == ".html" else "utf8-v1", "point_in_time_certified": False}}])
    return {**result, "raw_path": str(raw), "text_extracted": True}


def _documents(path):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        if path.suffix == ".json":
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get("documents"), list):
                value = value["documents"]
            if isinstance(value, dict) and isinstance(value.get("page_content"), str):
                value = [value]
            if not isinstance(value, list):
                raise ValueError()
            docs = value
        elif path.suffix == ".jsonl":
            docs = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            docs = [{"page_content": text, "metadata": {}}]
        if any(not isinstance(doc, dict) or not isinstance(doc.get("page_content"), str) for doc in docs):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ToolError("INVALID_DOCUMENT", "Documents must be UTF-8 text or JSON records with page_content strings.") from None
    return docs, hashlib.sha256(raw).hexdigest()


def local_retrieval(args, workdir):
    # Latin words and individual CJK characters permit transparent multilingual term matching.
    tokenize = lambda text: re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.lower())
    terms = set(tokenize(args["query"]))
    if not terms:
        raise ToolError("EMPTY_QUERY", "The query must contain searchable letters, digits or CJK characters.")
    size, matches, count = args.get("chunk_chars", 1200), [], 0
    for file in args["paths"]:
        path = Path(file)
        docs, digest = _documents(path)
        for number, doc in enumerate(docs):
            text = doc["page_content"]
            count += 1
            for start in range(0, len(text), size):
                part = text[start:start + size]
                frequencies = Counter(tokenize(part))
                matched = sorted(terms & frequencies.keys())
                if matched:
                    score = sum(1 + math.log(frequencies[term]) for term in matched) / math.sqrt(max(1, len(frequencies)))
                    matches.append({"score": score, "matched_terms": matched, "text": part,
                        "source_path": str(path), "source_sha256": digest, "document_index": number,
                        "char_start": start, "char_end": start + len(part), "metadata": doc.get("metadata", {})})
    matches.sort(key=lambda item: (-item["score"], item["source_path"], item["document_index"], item["char_start"]))
    return {"method": "deterministic-term-match-v1", "query": args["query"], "documents_scanned": count,
            "matches": matches[:args.get("max_results", 5)], "embedding_model_called": False,
            "source_truth_certified": False}


def dispatch(spec, args, workdir):
    functions = {"legacy": legacy, "earnings": earnings, "sec_archive": sec_archive, "local_retrieval": local_retrieval}
    if spec["adapter"] == "legacy":
        return legacy(spec, args, workdir)
    return functions[spec["adapter"]](args, workdir)
