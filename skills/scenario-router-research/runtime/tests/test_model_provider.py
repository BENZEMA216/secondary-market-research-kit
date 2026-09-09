"""Offline provider tests: subprocesses are mocked; no account/model calls."""

import json
import hashlib
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scenario_router.model_provider import (
    CodexExecProvider, DisabledProvider, ModelResponse, ProviderError,
    _CAPABILITY_FEATURES, _REQUIRED_FEATURES,
    _KNOWN_NONFATAL_NOTICES,
)


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


def events(*, message='{"ok":true}', extra=None):
    rows = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "type": "reasoning", "id": "reason-1", "text": "private reasoning"}},
    ]
    if extra is not None:
        rows.append(extra)
    rows.extend([
        {"type": "item.completed", "item": {
            "type": "agent_message", "id": "message-1", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": 5}},
    ])
    return "\n".join(json.dumps(row) for row in rows)


class FakeCli:
    def __init__(self, stdout=None):
        self.stdout = events() if stdout is None else stdout
        self.calls = []
        self.schema_path = None
        self.features_fail = False
        self.mcp_fail = False
        self.unified_exec_stays_enabled = False
        self.version = "codex-cli 0.153.0-alpha.5"
        self.inventory = [{"name": "public-news", "enabled": True,
                           "transport": {"type": "stdio"},
                           "env": {"TOKEN": "never-record-this"},
                           "headers": {"Authorization": "never-record-this"}}]

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[-1] == "--version":
            stdout = self.version + "\n"
        elif command[-2:] == ["features", "list"]:
            reduced = "--disable" in command and not self.features_fail
            names = sorted(_REQUIRED_FEATURES | {"sleep_tool", "hooks", "memories"})
            stdout = "\n".join(
                f"{name} stable {'false' if reduced and name in _CAPABILITY_FEATURES and not (name == 'unified_exec' and self.unified_exec_stays_enabled) else 'true'}"
                for name in names)
        elif command[-3:] == ["mcp", "list", "--json"]:
            records = [dict(record) for record in self.inventory]
            if "--disable" in command and not self.mcp_fail:
                for record in records:
                    record["enabled"] = False
            stdout = json.dumps(records)
        else:
            assert "exec" in command
            self.schema_path = Path(command[command.index("--output-schema") + 1])
            assert json.loads(self.schema_path.read_text()) == SCHEMA
            stdout = self.stdout
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="never-record-this")


class ModelProviderTests(unittest.TestCase):
    def test_disabled_does_not_launch_any_process(self):
        with patch("scenario_router.model_provider.subprocess.run") as run:
            with self.assertRaisesRegex(ProviderError, "MODEL_PROVIDER_DISABLED"):
                DisabledProvider().complete("extract", SCHEMA)
            run.assert_not_called()

    def test_buffered_success_uses_stdin_and_narrowed_capabilities(self):
        fake = FakeCli()
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            response = CodexExecProvider().complete("extract only supplied facts", SCHEMA)
        self.assertIsInstance(response, ModelResponse)
        self.assertEqual(json.loads(response.raw), {"ok": True})
        command, kwargs = fake.calls[-1]
        self.assertEqual(kwargs["input"], "extract only supplied facts")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("extract only supplied facts", command)
        for flag in ("--ephemeral", "--skip-git-repo-check", "--json", "--output-schema"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn('mcp_servers.public-news={enabled=false,command="false"}', command)
        for forbidden in ("--ignore-rules", "--ignore-user-config", "--model", "-m",
                          "--dangerously-bypass-approvals-and-sandbox", "hooks"):
            self.assertNotIn(forbidden, command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertEqual(kwargs["timeout"], 120)
        self.assertNotIn("env", kwargs)
        self.assertNotIn("shell", kwargs)
        self.assertFalse(fake.schema_path.exists())
        self.assertEqual(response.metadata["cli_version"], "codex-cli 0.153.0-alpha.5")
        self.assertEqual(response.metadata["actual_model"], "unavailable")
        self.assertEqual(response.metadata["tool_events_observed"], 0)
        metadata = json.dumps(response.metadata)
        self.assertNotIn("never-record-this", metadata)
        self.assertNotIn("private reasoning", metadata)
        self.assertNotIn("input_tokens", metadata)
        self.assertEqual(len(response.metadata["stdout_sha256"]), 64)

    def test_actual_event_model_is_recorded_without_guessing_config(self):
        fake = FakeCli(events().replace('"thread_id": "thread-1"',
                                       '"thread_id": "thread-1", "model": "observed-model-v1"'))
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            result = CodexExecProvider().complete("extract", SCHEMA)
        self.assertEqual(result.metadata["actual_model"], "observed-model-v1")

    def test_nonzero_does_not_leak_stderr(self):
        result = subprocess.CompletedProcess([], 1, stdout="TOKEN=secret", stderr="secret")
        with patch("scenario_router.model_provider.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(ProviderError, "^CLI_NONZERO_EXIT$"):
                CodexExecProvider().complete("extract", SCHEMA)
            self.assertEqual(run.call_count, 1)

    def test_timeout_does_not_retry_or_leak_output(self):
        timeout = subprocess.TimeoutExpired(["codex"], 120, output="secret", stderr="secret")
        with patch("scenario_router.model_provider.subprocess.run", side_effect=timeout) as run:
            with self.assertRaisesRegex(ProviderError, "^CLI_TIMEOUT$"):
                CodexExecProvider().complete("extract", SCHEMA)
            self.assertEqual(run.call_count, 1)

    def test_model_timeout_cleans_temporary_schema_and_has_no_retry(self):
        fake = FakeCli()
        model_paths = []

        def timeout_on_model(command, **kwargs):
            if "exec" in command:
                path = Path(command[command.index("--output-schema") + 1])
                self.assertTrue(path.exists())
                model_paths.append(path)
                raise subprocess.TimeoutExpired(command, 120, output="secret")
            return fake(command, **kwargs)

        with patch("scenario_router.model_provider.subprocess.run", side_effect=timeout_on_model):
            with self.assertRaisesRegex(ProviderError, "^CLI_TIMEOUT$"):
                CodexExecProvider().complete("extract", SCHEMA)
        self.assertEqual(len(model_paths), 1)
        self.assertFalse(model_paths[0].exists())

    def test_missing_cli_fails_closed(self):
        with patch("scenario_router.model_provider.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ProviderError, "CLI_UNAVAILABLE"):
                CodexExecProvider().complete("extract", SCHEMA)

    def test_missing_capability_flag_prevents_model_call(self):
        with patch("scenario_router.model_provider.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "codex-cli 0.1", ""),
            subprocess.CompletedProcess([], 0, "shell_tool stable true", ""),
        ]) as run:
            with self.assertRaisesRegex(ProviderError, "CLI_CAPABILITY_FLAGS_UNAVAILABLE"):
                CodexExecProvider().complete("extract", SCHEMA)
            self.assertEqual(run.call_count, 2)

    def test_effective_feature_reduction_is_verified(self):
        fake = FakeCli()
        fake.features_fail = True
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(ProviderError, "CLI_CAPABILITY_REDUCTION_FAILED"):
                CodexExecProvider().complete("extract", SCHEMA)
        self.assertFalse(any("exec" in command for command, _ in fake.calls))

    def test_effective_mcp_reduction_is_verified(self):
        fake = FakeCli()
        fake.mcp_fail = True
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(ProviderError, "CLI_MCP_REDUCTION_FAILED"):
                CodexExecProvider().complete("extract", SCHEMA)
        self.assertFalse(any("exec" in command for command, _ in fake.calls))

    def test_http_mcp_disabled_without_copying_endpoint_or_headers(self):
        fake = FakeCli()
        fake.inventory[0]["transport"] = {
            "type": "streamable_http", "url": "https://private.example/secret",
            "http_headers": {"Authorization": "never-record-this"}}
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            response = CodexExecProvider().complete("extract", SCHEMA)
        command = fake.calls[-1][0]
        self.assertIn('mcp_servers.public-news={enabled=false,url="https://disabled.invalid"}', command)
        self.assertNotIn("private.example", " ".join(command))
        self.assertNotIn("never-record-this", json.dumps(response.metadata))

    def test_known_build_unified_exec_exception_is_disclosed_not_hidden(self):
        fake = FakeCli()
        fake.unified_exec_stays_enabled = True
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            response = CodexExecProvider().complete("extract", SCHEMA)
        self.assertEqual(response.metadata["feature_disable_exceptions"], ["unified_exec"])
        self.assertNotIn("unified_exec", response.metadata["disabled_features"])
        self.assertIs(response.metadata["shell_tool_master_effective"], False)

    def test_other_build_cannot_silently_reuse_known_feature_exception(self):
        fake = FakeCli()
        fake.version = "codex-cli 0.999.0"
        fake.unified_exec_stays_enabled = True
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(ProviderError, "CLI_CAPABILITY_REDUCTION_FAILED"):
                CodexExecProvider().complete("extract", SCHEMA)

    def test_mcp_untrusted_name_is_not_interpreted_as_config(self):
        fake = FakeCli()
        fake.inventory[0]["name"] = 'bad".enabled=true\n[features]'
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(ProviderError, "CLI_MCP_INVENTORY_INVALID"):
                CodexExecProvider().complete("extract", SCHEMA)
        self.assertFalse(any("exec" in command for command, _ in fake.calls))

    def test_tool_items_rejected_even_if_final_answer_valid(self):
        for item_type in ("command_execution", "mcp_tool_call", "web_search",
                          "file_change", "todo_list", "unknown_future_tool"):
            with self.subTest(item_type=item_type):
                fake = FakeCli(events(extra={"type": "item.started", "item": {
                    "id": "tool-1", "type": item_type, "arguments": {"secret": "x"}}}))
                with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
                    with self.assertRaisesRegex(ProviderError, "CLI_TOOL_OR_UNSUPPORTED_ITEM"):
                        CodexExecProvider().complete("extract", SCHEMA)

    def test_errors_unknown_events_and_incomplete_turn_rejected(self):
        cases = [
            events(extra={"type": "error", "message": "secret"}),
            events(extra={"type": "turn.failed", "error": {"message": "secret"}}),
            events(extra={"type": "future_event"}),
            "\n".join(events().splitlines()[:-1]),
            events() + '\n{"type":"turn.started"}',
            "not json",
        ]
        for stdout in cases:
            with self.subTest(stdout=stdout):
                with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(stdout)):
                    with self.assertRaises(ProviderError):
                        CodexExecProvider().complete("extract", SCHEMA)

    def test_real_probe_error_before_turn_is_error_not_allowed_sequence_variant(self):
        rows = [
            {"type": "thread.started", "thread_id": "probe-thread"},
            {"type": "item.completed", "item": {"type": "error", "id": "error-1",
                "message": "Hook failed; Authorization=never-record-this"}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "error", "id": "error-2",
                "message": "Hook failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "id": "answer",
                "text": '{"ok":true}'}},
            {"type": "turn.completed"},
        ]
        stdout = "\n".join(json.dumps(row) for row in rows)
        with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(stdout)):
            with self.assertRaisesRegex(ProviderError, "^CLI_ITEM_ERROR$") as caught:
                CodexExecProvider().complete("technical probe", SCHEMA)
        metadata = caught.exception.metadata
        self.assertEqual(metadata["stdout_sha256"], hashlib.sha256(stdout.encode()).hexdigest())
        self.assertEqual(len(metadata["stdout_events"]), 6)
        self.assertEqual(metadata["stdout_events"][1]["item"]["type"], "error")
        self.assertIn("hook", metadata["stdout_events"][1]["error_summaries"][0]["categories"])
        self.assertEqual(metadata["provider_error"], "CLI_ITEM_ERROR")
        self.assertIn("prompt_sha256", metadata)
        self.assertNotIn("never-record-this", json.dumps(metadata))
        self.assertNotIn("Authorization", json.dumps(metadata))

    def test_only_exact_known_version_notices_accepted_before_and_during_turn(self):
        notices = list(_KNOWN_NONFATAL_NOTICES)
        rows = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"id": "notice-1", "type": "error", "message": notices[0]}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "notice-2", "type": "error", "message": notices[1]}},
            {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed"},
        ]
        stdout = "\n".join(json.dumps(row) for row in rows)
        with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(stdout)):
            response = CodexExecProvider().complete("extract", SCHEMA)
        saved = response.metadata["known_nonfatal_notices"]
        self.assertEqual([item["message"] for item in saved], notices)
        self.assertEqual(response.metadata["stdout_sha256"], hashlib.sha256(stdout.encode()).hexdigest())
        self.assertEqual([item["event_index"] for item in saved], [1, 3])
        self.assertTrue(all(item["cli_event_timestamp"] == "unavailable" for item in saved))
        self.assertIn("started_at", response.metadata)
        self.assertIn("completed_at", response.metadata)

    def test_near_match_or_other_version_notice_remains_error(self):
        original = next(iter(_KNOWN_NONFATAL_NOTICES))
        for version, message in (
            ("codex-cli 0.153.0-alpha.5", original + " "),
            ("codex-cli 0.153.0-alpha.5", original.replace("unavailable", "available")),
            ("codex-cli 0.154.0", original),
        ):
            with self.subTest(version=version, message=message):
                fake = FakeCli(events(extra={"type": "item.completed", "item": {
                    "id": "notice", "type": "error", "message": message}}))
                fake.version = version
                with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
                    with self.assertRaisesRegex(ProviderError, "CLI_ITEM_ERROR"):
                        CodexExecProvider().complete("extract", SCHEMA)

    def test_known_notice_cannot_hide_tool_error_or_missing_completion(self):
        notice = {"type": "item.completed", "item": {
            "id": "notice", "type": "error", "message": next(iter(_KNOWN_NONFATAL_NOTICES))}}
        rows = [json.loads(line) for line in events(extra=notice).splitlines()]
        variants = [
            rows[:-1],
            rows[:-1] + [{"type": "turn.failed", "error": {"message": "real provider error"}}],
            rows[:-1] + [{"type": "item.completed", "item": {"type": "command_execution", "id": "tool"}}, rows[-1]],
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                stdout = "\n".join(json.dumps(row) for row in variant)
                with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(stdout)):
                    with self.assertRaises(ProviderError) as caught:
                        CodexExecProvider().complete("extract", SCHEMA)
                self.assertEqual(len(caught.exception.metadata["known_nonfatal_notices"]), 1)

    def test_known_notice_with_explicit_error_status_is_rejected(self):
        fake = FakeCli(events(extra={"type": "item.completed", "item": {
            "id": "notice", "type": "error", "status": "failed",
            "message": next(iter(_KNOWN_NONFATAL_NOTICES))}}))
        with patch("scenario_router.model_provider.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(ProviderError, "CLI_ITEM_ERROR"):
                CodexExecProvider().complete("extract", SCHEMA)

    def test_tool_failure_keeps_full_safe_evidence_but_not_arguments(self):
        stdout = events(extra={"type": "item.completed", "item": {
            "id": "tool-1", "type": "mcp_tool_call", "arguments": {"api_key": "secret"}}})
        with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(stdout)):
            with self.assertRaises(ProviderError) as caught:
                CodexExecProvider().complete("extract", SCHEMA)
        metadata = caught.exception.metadata
        self.assertEqual(metadata["stdout_events"][-1]["type"], "turn.completed")
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertNotIn("arguments", json.dumps(metadata))

    def test_nonzero_model_exit_keeps_sanitized_events(self):
        fake = FakeCli()
        stdout = '{"type":"error","message":"auth failed; TOKEN=secret"}'

        def fail_on_model(command, **kwargs):
            if "exec" in command:
                return subprocess.CompletedProcess(command, 1, stdout, "Authorization=secret")
            return fake(command, **kwargs)

        with patch("scenario_router.model_provider.subprocess.run", side_effect=fail_on_model):
            with self.assertRaisesRegex(ProviderError, "CLI_NONZERO_EXIT") as caught:
                CodexExecProvider().complete("extract", SCHEMA)
        metadata = caught.exception.metadata
        self.assertEqual(metadata["returncode"], 1)
        self.assertEqual(metadata["stdout_events"][0]["type"], "error")
        self.assertNotIn("secret", json.dumps(metadata))

    def test_invalid_final_json_duplicate_keys_and_nan_rejected(self):
        for raw in ('```json\n{"ok":true}\n```', '{"ok":true,"ok":false}',
                    '{"ok":NaN}', 'not json', ''):
            with self.subTest(raw=raw):
                with patch("scenario_router.model_provider.subprocess.run", side_effect=FakeCli(events(message=raw))):
                    with self.assertRaises(ProviderError):
                        CodexExecProvider().complete("extract", SCHEMA)

    def test_invalid_prompt_and_schema_do_not_start_cli(self):
        for prompt, schema in (("", SCHEMA), ("extract", {}), ("extract", {"value": float("nan")})):
            with patch("scenario_router.model_provider.subprocess.run") as run:
                with self.assertRaises(ProviderError):
                    CodexExecProvider().complete(prompt, schema)
                run.assert_not_called()

    def test_timeout_values_are_validated(self):
        for value in (True, 0, -1, float("inf"), float("nan"), "120"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CodexExecProvider(timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
