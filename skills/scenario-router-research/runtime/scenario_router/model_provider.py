"""Buffered, research-only model providers; never a broker or execution gateway.

The Codex adapter narrows the installed CLI's advertised capabilities for one
invocation and rejects every non-message/reasoning item. These flags plus an
after-the-fact event audit are NOT a proof of zero-tool process isolation. Hooks,
managed policy and user/project rules are deliberately retained. A caller that
requires a hard no-tools security boundary must use DisabledProvider until such
a boundary is independently provided. No model, account or credential is chosen
or created here; the existing ChatGPT CLI login and configured model are used.

Only JSON syntax is checked here. Domain and full JSON Schema validation remain
the caller's responsibility. Official interface documentation:
https://learn.chatgpt.com/docs/non-interactive-mode
https://learn.chatgpt.com/docs/config-file/config-reference
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Any


class ProviderError(RuntimeError):
    """Closed failure with a safe evidence ledger, never raw process output.

    Workflow callers should persist ``metadata`` alongside the error code. It
    contains hashes and structural events, but no error text, arguments,
    reasoning text, headers or environment values.
    """

    def __init__(self, code: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class ModelResponse:
    raw: str
    metadata: dict[str, Any]


class DisabledProvider:
    """Explicit provider-off path: no subprocess, network, fallback or retry."""

    def complete(self, prompt: str, schema: dict[str, Any]) -> ModelResponse:
        raise ProviderError("MODEL_PROVIDER_DISABLED")


# Only capability-reducing flags. In particular, do not disable hooks or load a
# replacement user config: a no-tool extraction must not bypass local policy.
_CAPABILITY_FEATURES = frozenset({
    "apps", "enable_mcp_apps", "plugins", "remote_plugin",
    "recommended_plugins", "plugin_sharing", "shell_tool", "unified_exec",
    "code_mode", "code_mode_host", "code_mode_only", "code_mode_prewarm",
    "code_mode_interrupt", "js_repl", "js_repl_tools_only", "multi_agent",
    "multi_agent_v2", "goals", "computer_use", "in_app_browser",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "image_generation", "view_image", "sleep_tool", "in_app_chat",
    "in_app_local_automation", "workspace_dependencies", "tool_suggest",
    "standalone_web_search", "skill_mcp_dependency_install", "skill_search",
    "memories", "chronicle", "default_mode_request_user_input",
    "request_permissions_tool", "artifact", "psp", "realtime_conversation",
    "unbounded_connection_retries", "unified_exec_zsh_fork",
})
_REQUIRED_FEATURES = frozenset({
    "apps", "plugins", "shell_tool", "unified_exec", "code_mode_host",
    "multi_agent", "goals", "computer_use", "in_app_browser",
    "browser_use", "image_generation", "view_image",
})
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_MCP_CONFIG_KEY = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_SAFE_VERSION = re.compile(r"codex-cli [A-Za-z0-9_.+-]{1,100}\Z")
_EVENT_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "item.started",
    "item.updated", "item.completed",
})
_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
# Exact messages observed in a separately logged technical diagnostic probe.
# This CLI serializes these nonfatal capability notices as error items. Do not
# use substring/fuzzy matching, broaden to other versions, or enable tools to
# suppress them. Evidence: results/provider-diagnostics/missing-error-diagnostic-probe.json.
_KNOWN_NOTICE_VERSION = "codex-cli 0.153.0-alpha.5"
_KNOWN_NONFATAL_NOTICES = {
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.": "CODE_MODE_HOST_INTENTIONALLY_DISABLED",
    "Skill descriptions were shortened to fit the skills context budget. Codex can still see every skill, but some descriptions are shorter. Disable unused skills or plugins to leave more room for the rest.": "SKILL_DESCRIPTIONS_SHORTENED",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _json_loads(raw: str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _features(raw: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        if (len(fields) < 3 or not _SAFE_IDENTIFIER.fullmatch(fields[0])
                or fields[-1] not in {"true", "false"}
                or fields[0] in result):
            raise ProviderError("CLI_FEATURE_INVENTORY_INVALID")
        result[fields[0]] = fields[-1] == "true"
    if not _REQUIRED_FEATURES.issubset(result):
        raise ProviderError("CLI_CAPABILITY_FLAGS_UNAVAILABLE")
    return result


def _mcp_inventory(raw: str) -> dict[str, tuple[bool, str]]:
    """Discard commands, headers and environments immediately after parsing."""
    try:
        records = _json_loads(raw)
    except (TypeError, ValueError):
        raise ProviderError("CLI_MCP_INVENTORY_INVALID") from None
    if not isinstance(records, list):
        raise ProviderError("CLI_MCP_INVENTORY_INVALID")
    result: dict[str, tuple[bool, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ProviderError("CLI_MCP_INVENTORY_INVALID")
        name, enabled = record.get("name"), record.get("enabled")
        transport = record.get("transport")
        transport_type = transport.get("type") if isinstance(transport, dict) else None
        if (not isinstance(name, str) or not _MCP_CONFIG_KEY.fullmatch(name)
                or type(enabled) is not bool or name in result
                or transport_type not in {"stdio", "streamable_http"}):
            raise ProviderError("CLI_MCP_INVENTORY_INVALID")
        result[name] = (enabled, transport_type)
    return result


def _safe_id(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else "unavailable"


def _error_categories(message: str) -> list[str]:
    lower = message.lower()
    categories = {
        "hook": ("hook",), "permission": ("permission", "denied", "sandbox"),
        "mcp": ("mcp",), "authentication": ("auth", "credential", "login"),
        "connection": ("network", "connect", "timeout"),
        "schema": ("schema",), "configuration": ("config",),
    }
    return [key for key, needles in categories.items()
            if any(needle in lower for needle in needles)] or ["unclassified"]


def _known_notice(message: Any, cli_version: str | None) -> dict[str, Any] | None:
    if (cli_version == _KNOWN_NOTICE_VERSION and isinstance(message, str)
            and message in _KNOWN_NONFATAL_NOTICES):
        return {"code": _KNOWN_NONFATAL_NOTICES[message], "message": message,
                "message_sha256": _digest(message), "cli_event_timestamp": "unavailable"}
    return None


def _stdout_evidence(stdout: str | bytes | None, *, cli_version: str | None = None) -> dict[str, Any]:
    """Project the entire actual stream even when validation fails early."""
    raw_bytes = stdout if isinstance(stdout, bytes) else (stdout or "").encode("utf-8")
    text = raw_bytes.decode("utf-8", errors="replace")
    ledger: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        safe: dict[str, Any] = {"line_index": index, "line_sha256": _digest(line)}
        try:
            event = _json_loads(line)
        except (TypeError, ValueError):
            safe["type"] = "invalid_json"
            ledger.append(safe)
            continue
        if not isinstance(event, dict):
            safe["type"] = "invalid_event"
            ledger.append(safe)
            continue
        kind = event.get("type")
        safe["type"] = kind if kind in _EVENT_TYPES | {"error", "turn.failed"} else "unsupported_event"
        if kind == "thread.started":
            safe["thread_id"] = _safe_id(event.get("thread_id"))
        item = event.get("item")
        error_messages: list[str] = []
        if isinstance(item, dict):
            item_kind = item.get("type")
            safe_item: dict[str, Any] = {
                "id": _safe_id(item.get("id")), "type": _safe_id(item_kind),
            }
            if isinstance(item.get("text"), str):
                safe_item["text_sha256"] = _digest(item["text"])
            if item_kind == "error" or item.get("error") is not None:
                safe_item["error_present"] = True
                if isinstance(item.get("message"), str):
                    error_messages.append(item["message"])
                notice = _known_notice(item.get("message"), cli_version)
                if notice is not None:
                    safe["known_nonfatal_notice"] = notice
            safe["item"] = safe_item
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error_messages.append(error["message"])
        elif isinstance(error, str):
            error_messages.append(error)
        if kind in {"error", "turn.failed"} and isinstance(event.get("message"), str):
            error_messages.append(event["message"])
        if error_messages:
            safe["error_summaries"] = [{
                "message_sha256": _digest(message),
                "categories": _error_categories(message),
            } for message in error_messages]
        ledger.append(safe)
    return {
        "stdout_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "stdout_events": ledger,
        "stdout_events_format": "safe_projection_payloads_omitted",
        "stdout_present": stdout is not None,
    }


def _parse_events(stdout: str, *, cli_version: str | None = None) -> tuple[str, list[dict[str, Any]], str]:
    evidence = _stdout_evidence(stdout, cli_version=cli_version)
    try:
        return _parse_event_stream(stdout, cli_version=cli_version)
    except ProviderError as error:
        error.metadata.update(evidence)
        raise


def _parse_event_stream(stdout: str, *, cli_version: str | None = None) -> tuple[str, list[dict[str, Any]], str]:
    """Accept a completed single turn with no actual tool/action items.

    The returned event ledger is a projection of actual stdout events, not an
    invented transcript. Free text, arguments, environment/header fields, usage
    details and reasoning payloads are omitted; the original stream is hashed.
    """
    events: list[dict[str, Any]] = []
    messages: list[str] = []
    seen_thread = started = completed = False
    actual_model = "unavailable"
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json_loads(line)
        except (TypeError, ValueError):
            raise ProviderError("CLI_EVENT_JSON_INVALID") from None
        if not isinstance(event, dict):
            raise ProviderError("CLI_EVENT_INVALID")
        kind = event.get("type")
        if (kind not in _EVENT_TYPES or event.get("error") is not None
                or event.get("errors") or completed):
            raise ProviderError("CLI_ERROR_OR_UNSUPPORTED_EVENT")
        safe: dict[str, Any] = {"type": kind}
        if "model" in event:
            actual_model = _safe_id(event["model"])
            safe["model"] = actual_model
        possible_item = event.get("item")
        if isinstance(possible_item, dict) and possible_item.get("type") == "error":
            notice = _known_notice(possible_item.get("message"), cli_version)
            if (notice is None or not seen_thread or kind != "item.completed"
                    or possible_item.get("error") is not None
                    or possible_item.get("status") in {"failed", "error"}):
                raise ProviderError("CLI_ITEM_ERROR")
            safe["item"] = {"id": _safe_id(possible_item.get("id")), "type": "error"}
            safe["known_nonfatal_notice"] = notice
            events.append(safe)
            continue
        if kind == "thread.started":
            if seen_thread or started:
                raise ProviderError("CLI_EVENT_SEQUENCE_INVALID")
            seen_thread = True
            safe["thread_id"] = _safe_id(event.get("thread_id"))
        elif kind == "turn.started":
            if not seen_thread or started:
                raise ProviderError("CLI_EVENT_SEQUENCE_INVALID")
            started = True
        elif kind == "turn.completed":
            if not started or not messages:
                raise ProviderError("CLI_EVENT_SEQUENCE_INVALID")
            completed = True
        else:
            if not started:
                raise ProviderError("CLI_EVENT_SEQUENCE_INVALID")
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in _ITEM_TYPES:
                raise ProviderError("CLI_TOOL_OR_UNSUPPORTED_ITEM")
            if item.get("error") is not None or item.get("status") in {"failed", "error"}:
                raise ProviderError("CLI_ITEM_ERROR")
            safe["item"] = {"id": _safe_id(item.get("id")), "type": item["type"]}
            if kind == "item.completed" and item["type"] == "agent_message":
                message = item.get("text")
                if not isinstance(message, str) or not message.strip():
                    raise ProviderError("CLI_FINAL_MESSAGE_MISSING")
                messages.append(message)
                safe["item"]["text_sha256"] = _digest(message)
        events.append(safe)
    if not completed or not messages:
        raise ProviderError("CLI_TURN_INCOMPLETE")
    final = messages[-1]
    try:
        _json_loads(final)
    except (TypeError, ValueError):
        raise ProviderError("MODEL_OUTPUT_JSON_INVALID") from None
    return final, events, actual_model


class CodexExecProvider:
    """Use saved ChatGPT CLI auth with per-invocation capability reductions.

    Instantiation does not run anything. complete() performs local read-only
    preflight and one buffered model invocation, without automatic retry.
    Unknown flags/events, configured MCP inventory drift, errors and timeouts
    close the provider. This is a local research adapter, not security-certified
    no-tools containment; inherited trusted hooks may still run.
    """

    def __init__(self, executable: str = "codex", *, timeout_seconds: float = 120,
                 preflight_timeout_seconds: float = 20, cwd: str | Path | None = None):
        for value in (timeout_seconds, preflight_timeout_seconds):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value <= 0):
                raise ValueError("provider timeouts must be positive finite seconds")
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValueError("executable must be a non-empty path or command name")
        self.executable = executable
        self.timeout_seconds = float(timeout_seconds)
        self.preflight_timeout_seconds = float(preflight_timeout_seconds)
        self.cwd = str(Path(cwd).resolve()) if cwd is not None else None

    def _run(self, arguments: list[str], *, prompt: str | None = None,
             model_call: bool = False) -> str:
        try:
            completed = subprocess.run(
                [self.executable, *arguments], input=prompt, capture_output=True,
                text=True, encoding="utf-8", errors="strict", cwd=self.cwd,
                timeout=self.timeout_seconds if model_call else self.preflight_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            evidence = _stdout_evidence(error.stdout) if model_call else {}
            evidence["output_partial"] = True
            raise ProviderError("CLI_TIMEOUT", metadata=evidence) from None
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise ProviderError("CLI_UNAVAILABLE") from None
        if completed.returncode != 0:
            # stderr can contain endpoint headers or other local configuration.
            evidence = _stdout_evidence(completed.stdout) if model_call else {}
            evidence["returncode"] = completed.returncode
            raise ProviderError("CLI_NONZERO_EXIT", metadata=evidence)
        return completed.stdout

    def complete(self, prompt: str, schema: dict[str, Any]) -> ModelResponse:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("MODEL_PROMPT_INVALID")
        if not isinstance(schema, dict) or not schema:
            raise ProviderError("MODEL_SCHEMA_INVALID")
        try:
            schema_text = json.dumps(schema, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError):
            raise ProviderError("MODEL_SCHEMA_INVALID") from None

        version_text = self._run(["--version"]).strip()
        version = version_text if _SAFE_VERSION.fullmatch(version_text) else "unavailable"
        known_features = _features(self._run(["features", "list"]))
        servers = _mcp_inventory(self._run(["mcp", "list", "--json"]))
        disabled_features = sorted(_CAPABILITY_FEATURES.intersection(known_features))
        overrides: list[str] = []
        for feature in disabled_features:
            overrides.extend(["--disable", feature])
        for name in sorted(servers):
            # This CLI bootstrap layer replaces a server subtable, so setting
            # only enabled=false loses its transport and fails to load. Give
            # the disabled replacement an inert transport without copying any
            # endpoint, arguments, header or environment secret into argv.
            # Quoted dotted-path keys are not supported by the installed CLI.
            transport = ('command="false"' if servers[name][1] == "stdio"
                         else 'url="https://disabled.invalid"')
            overrides.extend(["-c", f'mcp_servers.{name}={{enabled=false,{transport}}}'])
        overrides.extend([
            "-c", 'web_search="disabled"', "-c", "tools.web_search=false",
            "-c", "tools.view_image=false", "-c", "apps._default.enabled=false",
            "-c", 'forced_login_method="chatgpt"',
        ])
        # Read back the effective feature/MCP configuration under these exact
        # overrides. This does not initialize the configured servers or call LLMs.
        effective = _features(self._run([*overrides, "features", "list"]))
        ineffective = {feature for feature in disabled_features if effective.get(feature) is not False}
        # On the locally inspected 0.153.0-alpha.5 build unified_exec remains
        # true even with both it and the legacy zsh alias explicitly disabled.
        # The shell_tool master flag is false; nevertheless record this as a
        # containment limitation, not as a successful unified_exec disable.
        allowed_exception = (
            {"unified_exec"} if version == "codex-cli 0.153.0-alpha.5"
            and effective.get("shell_tool") is False else set()
        )
        if ineffective - allowed_exception:
            raise ProviderError("CLI_CAPABILITY_REDUCTION_FAILED")
        remaining = _mcp_inventory(self._run([*overrides, "mcp", "list", "--json"]))
        if set(remaining) != set(servers) or any(enabled for enabled, _ in remaining.values()):
            raise ProviderError("CLI_MCP_REDUCTION_FAILED")

        started_at = datetime.now(timezone.utc).isoformat()
        context: dict[str, Any] = {
            "provider": "codex_exec", "cli_version": version,
            "model_selection": "inherited_user_configuration",
            "started_at": started_at,
            "prompt_sha256": _digest(prompt), "schema_sha256": _digest(schema_text),
            "requested_disabled_features": disabled_features,
            "disabled_features": sorted(set(disabled_features) - ineffective),
            "feature_disable_exceptions": sorted(ineffective),
            "shell_tool_master_effective": effective["shell_tool"],
            "disabled_mcp_servers": sorted(servers),
            "mcp_disable_method": "per_invocation_disabled_inert_transport",
            "capability_isolation": "best_effort_cli_flags_not_security_boundary",
            "rules_and_hooks_retained": True,
            "adapter_retry_count": 0, "cli_internal_retry_count": "unavailable",
            "full_schema_validated_by_provider": False,
        }
        try:
            with TemporaryDirectory(prefix="scenario-router-schema-") as temporary:
                schema_path = Path(temporary) / "response.schema.json"
                schema_path.write_text(schema_text, encoding="utf-8")
                stdout = self._run([
                    *overrides, "exec", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--color", "never", "--json",
                    "--output-schema", str(schema_path), "-",
                ], prompt=prompt, model_call=True)
            raw, events, model = _parse_events(stdout, cli_version=version)
        except ProviderError as error:
            error.metadata = {**context, **error.metadata,
                              "completed_at": datetime.now(timezone.utc).isoformat(),
                              "provider_error": error.code}
            error.metadata["known_nonfatal_notices"] = [
                {"event_index": index, **event["known_nonfatal_notice"]}
                for index, event in enumerate(error.metadata.get("stdout_events", []))
                if "known_nonfatal_notice" in event
            ]
            raise
        context.update({
            "actual_model": model, "completed_at": datetime.now(timezone.utc).isoformat(),
            "stdout_sha256": _digest(stdout), "response_sha256": _digest(raw),
            "stdout_events": events, "stdout_events_format": "safe_projection_payloads_omitted",
            "known_nonfatal_notices": [
                {"event_index": index, **event["known_nonfatal_notice"]}
                for index, event in enumerate(events) if "known_nonfatal_notice" in event
            ],
            "tool_events_observed": 0, "json_syntax_valid": True,
        })
        return ModelResponse(raw=raw, metadata=context)
