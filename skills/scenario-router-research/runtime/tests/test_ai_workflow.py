import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scenario_router.ai_workflow import AgentWorkflow, compare_candidates, digest, load_bundle, validate_candidate


class FakeProvider:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete(self, prompt, schema):
        self.prompts.append(prompt)
        raw = self.result if isinstance(self.result, str) else json.dumps(self.result)
        return SimpleNamespace(raw=raw, metadata={"provider": "unit_test_fixture", "real_model": False})


class AIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.texts = {
            "prior": "FY2026 Total revenue guidance: $100 million to $110 million. GAAP EPS: $1.00 to $1.20.",
            "current": "FY2026 Total revenue guidance: $110 million to $120 million. GAAP EPS: $1.10 to $1.30.",
        }
        sources = []
        for source_id, text in self.texts.items():
            (self.root / (source_id + ".txt")).write_text(text)
            sources.append({"source_id": source_id, "role": source_id, "path": source_id + ".txt", "sha256": digest(text.encode()), "url": "https://example.invalid/test", "published_at": "2025-01-01T00:00:00Z" if source_id == "prior" else "2025-02-01T00:00:00Z", "captured_at": "2025-02-02T00:00:00Z"})
        self.manifest = {"bundle_id": "synthetic-only", "security_id": "TEST:EXAMPLE", "ticker": "TEST", "fiscal_period": "FY2026", "lane": "synthetic", "sources": sources}
        self.path = self.root / "bundle.json"
        self.path.write_text(json.dumps(self.manifest))
        self.candidate = {"abstain": False, "reason": "", "facts": []}
        for source, revenue, eps in (("prior", ("100", "110"), ("1.00", "1.20")), ("current", ("110", "120"), ("1.10", "1.30"))):
            for metric, unit, numbers in (("revenue", "million_USD", revenue), ("eps", "USD_per_share", eps)):
                self.candidate["facts"].append({"source_id": source, "metric": metric, "basis": "gaap", "period": "FY2026", "unit": unit, "low": numbers[0], "high": numbers[1], "quote": self.texts[source]})

    def flow(self, provider=None):
        return AgentWorkflow(self.root / "jobs.sqlite", provider or FakeProvider(self.candidate))

    def test_two_real_paths_in_runner_but_fixture_is_not_model_evidence(self):
        provider = FakeProvider(self.candidate)
        flow = self.flow(provider)
        result = flow.analyze("c", self.path, question="请看这份公告")
        self.assertEqual(result["state"], "CHECKED_CANDIDATE")
        self.assertTrue(result["e2b_screen"])
        self.assertEqual(len(result["calls"]), 2)
        self.assertNotEqual(result["calls"][0]["prompt_sha256"], result["calls"][1]["prompt_sha256"])
        self.assertFalse(result["calls"][0]["metadata"]["real_model"])
        self.assertEqual(flow.inspect("c")["revision"], 1)
        self.assertIn("不是买入许可", result["message"])

    def test_hash_mismatch_fails_before_model(self):
        (self.root / "current.txt").write_text("changed")
        provider = FakeProvider(self.candidate)
        result = self.flow(provider).analyze("c", self.path, question="看")
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(provider.prompts, [])

    def test_correction_provider_off_does_not_restore_previous_candidate(self):
        flow = self.flow()
        before = flow.analyze("c", self.path, question="看")
        class Off:
            def complete(self, *_):
                raise RuntimeError("provider unavailable")
        flow.provider = Off()
        after = flow.analyze("c", self.path, question="资料更正", expected_revision=1)
        self.assertEqual(after["state"], "BLOCKED")
        self.assertFalse(flow.inspect("c")["e2b_screen"])
        self.assertEqual(flow.inspect("c", as_of=before["completed_at"])["revision"], 1)
        self.assertEqual(flow.inspect("c", as_of=before["created_at"])["state"], "RUNNING")

    def test_restart_keeps_current_revision(self):
        flow = self.flow()
        flow.analyze("c", self.path, question="看")
        self.assertEqual(self.flow().inspect("c")["revision"], 1)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.flow().analyze("c", self.path, question="错版", expected_revision=0)

    def test_old_model_completion_cannot_replace_new_correction(self):
        flow = self.flow()
        original = FakeProvider(self.candidate)
        class SlowOld:
            done = False
            def complete(inner, prompt, schema):
                if not inner.done:
                    inner.done = True
                    flow.provider = FakeProvider({"abstain": True, "reason": "new evidence", "facts": []})
                    flow.analyze("c", self.path, question="最新更正", expected_revision=1)
                return original.complete(prompt, schema)
        flow.provider = SlowOld()
        late = flow.analyze("c", self.path, question="旧请求")
        self.assertEqual(late["state"], "SUPERSEDED")
        self.assertEqual(flow.inspect("c")["revision"], 2)
        self.assertFalse(flow.inspect("c")["e2b_screen"])

    def test_exact_quote_and_numeric_check(self):
        bundle = load_bundle(self.path)
        for field, bad in (("quote", "fabricated evidence"), ("low", "999"), ("period", "FY2027"), ("unit", "billion_USD"), ("source_id", "made-up")):
            candidate = copy.deepcopy(self.candidate)
            candidate["facts"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_candidate(json.dumps(candidate), bundle)

    def test_invalid_json_and_extra_actions_are_blocked(self):
        for candidate in ("not json", json.dumps({**self.candidate, "buy": True})):
            with self.subTest(candidate=candidate[:30]):
                result = self.flow(FakeProvider(candidate)).analyze("c", self.path, question="看")
                self.assertEqual(result["state"], "BLOCKED")

    def test_disagreement_is_not_majority_voted(self):
        changed = copy.deepcopy(self.candidate)
        changed["facts"][0]["high"] = "100"
        result = compare_candidates(self.candidate, changed, load_bundle(self.path))
        self.assertEqual(result["status"], "CONFLICT")
        self.assertFalse(result["e2b_screen"])

    def test_omitted_counterpart_never_qualifies(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["facts"].pop()
        result = compare_candidates(candidate, candidate, load_bundle(self.path))
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_cut_vetoes_revenue_raise(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["facts"][-1].update(low="0.80", high="0.90")
        result = compare_candidates(candidate, candidate, load_bundle(self.path))
        self.assertFalse(result["e2b_screen"])
        self.assertIn("cut", [x["direction"] for x in result["comparisons"]])

    def test_no_lookahead_capture(self):
        self.manifest["sources"][0]["captured_at"] = "2099-01-01T00:00:00Z"
        self.path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "capture"):
            load_bundle(self.path)

    def test_outside_path_is_rejected(self):
        self.manifest["sources"][0]["path"] = "../outside.txt"
        self.path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "escapes"):
            load_bundle(self.path)

    def test_historical_reconstruction_cannot_gate_trade(self):
        flow = self.flow()
        flow.analyze("c", self.path, question="看")
        with self.assertRaisesRegex(ValueError, "historical"):
            flow.gate_signal("c", None, revision=1, at=datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
