"""Standard-library-only input and output boundaries for the toolkit CLI."""
from __future__ import annotations

import base64
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import quote, quote_plus, urlsplit


class ToolError(Exception):
    def __init__(self, code, message, *, exit_code=2, hint="Inspect describe TOOL and supply only the declared arguments."):
        super().__init__(message)
        self.code, self.exit_code, self.hint = code, exit_code, hint


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolError("INVALID_JSON", "Duplicate JSON object keys are not accepted.")
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ToolError("INVALID_INPUT_FILE", "Expected an existing JSON file no larger than 2 MB.")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_JSON", "Input is not strict UTF-8 JSON.") from exc


def validate(value, schema, where="args"):
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(value, option, where)
                return
            except ToolError:
                pass
        raise ToolError("INVALID_ARGUMENT", f"{where} does not match an allowed type.")
    typ = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int,
               "number": type(value) in (int, float) and math.isfinite(value)}
    if typ and not matches.get(typ, False):
        raise ToolError("INVALID_ARGUMENT", f"{where} must be {typ}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("INVALID_ARGUMENT", f"{where} is not one of the declared choices.")
    if typ == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ToolError("UNKNOWN_ARGUMENT", f"{where} contains undeclared fields.")
        if set(schema.get("required", [])) - set(value):
            missing = sorted(set(schema["required"]) - set(value))
            raise ToolError("MISSING_ARGUMENT", f"Missing fields: {', '.join(missing)}")
        for name, item in value.items():
            if name in props:
                validate(item, props[name], f"{where}.{name}")
    elif typ == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise ToolError("INVALID_ARGUMENT", f"{where} has an unsupported item count.")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{index}]")
    elif typ == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000000):
            raise ToolError("INVALID_ARGUMENT", f"{where} has an unsupported length.")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ToolError("INVALID_ARGUMENT", f"{where} has an invalid format.")
        form = schema.get("format")
        if form == "date":
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError:
                raise ToolError("INVALID_ARGUMENT", f"{where} must be a real YYYY-MM-DD date.") from None
        elif form == "sec-url":
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not parsed.path.startswith("/Archives/") or parsed.query or parsed.fragment):
                raise ToolError("INVALID_ARGUMENT", f"{where} must be an HTTPS SEC Archives URL without credentials or query parameters.")
        elif form in {"local-image", "local-document"}:
            candidate = Path(value).expanduser()
            suffixes = {".png", ".jpg", ".jpeg"} if form == "local-image" else {".txt", ".md", ".json", ".jsonl"}
            if not candidate.is_file() or candidate.suffix.lower() not in suffixes or candidate.stat().st_size > 5_000_000:
                raise ToolError("INVALID_ARGUMENT", f"{where} must be an existing supported local file no larger than 5 MB.")
            if form == "local-image":
                head = candidate.read_bytes()[:12]
                if not (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")):
                    raise ToolError("INVALID_ARGUMENT", f"{where} must contain PNG or JPEG image bytes.")
    elif typ in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError("INVALID_ARGUMENT", f"{where} is below the minimum.")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError("INVALID_ARGUMENT", f"{where} exceeds the maximum.")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ToolError("INVALID_ARGUMENT", f"{where} must be greater than {schema['exclusiveMinimum']}.")


SECRET_NAME = re.compile(r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential|DCF_USERNAME)", re.I)


def secret_values():
    values = set()
    for name, value in os.environ.items():
        if value and SECRET_NAME.search(name):
            values.update((value, quote(value, safe=""), quote_plus(value)))
    if os.environ.get("DCF_USERNAME") and os.environ.get("DCF_PASSWORD"):
        joined = os.environ["DCF_USERNAME"] + ":" + os.environ["DCF_PASSWORD"]
        values.add(base64.b64encode(joined.encode()).decode())
    return sorted((v for v in values if v), key=len, reverse=True)


def redact_text(text):
    for value in secret_values():
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)((?:api[_-]?key|token|secret|password|authorization|cookie)\s*[=:]\s*)([^\s&\"'<>]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer|Basic)\s+[A-Za-z0-9+/_=.:-]+", r"\1 [REDACTED]", text)
    return text


def redact(value):
    if isinstance(value, dict):
        return {redact_text(str(key)): ("[REDACTED]" if SECRET_NAME.search(str(key)) else redact(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_manifest(root):
    root = Path(root)
    path = root / "MANIFEST.sha256"
    problems = []
    if not path.is_file() or path.is_symlink():
        return {"ok": False, "checked": 0, "problems": ["MANIFEST_MISSING"]}
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            expected, rel = line.split("  ", 1)
            target = root / rel
            if rel in seen or not re.fullmatch(r"[a-f0-9]{64}", expected) or not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
                raise ValueError()
            seen.add(rel)
            if not target.is_file() or sha256(target) != expected:
                problems.append(rel)
        except (ValueError, OSError):
            problems.append("INVALID_MANIFEST_ENTRY")
    if not seen:
        problems.append("MANIFEST_EMPTY")
    return {"ok": not problems, "checked": len(seen), "problems": problems}
