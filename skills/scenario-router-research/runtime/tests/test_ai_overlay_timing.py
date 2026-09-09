"""Real workflow/ledger integration with synthetic text and model fixtures.

These tests prove execution-time plumbing, not real-model accuracy, historical
point-in-time capture, full price-router coverage, or investment returns. The
EntrySignal is a fixture representing an already qualified v1 E2B candidate.
"""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scenario_router.ai_paper import AIReviewPaperBridge
from scenario_router.ai_workflow import AgentWorkflow, digest
from scenario_router.events import EventLedger, StructuredEventRecord, qualifies_e2b
from scenario_router.models import EntrySignal
from scenario_router.paper import PaperBroker
from scenario_router.portfolio import PortfolioLedger
from scenario_router.risk import EntryQuote


def instant(text):
    return datetime.fromisoformat(text)


CREATED = "2026-08-05T13:50:00+00:00"
COMPLETED = "2026-08-05T13:51:00+00:00"
AT = instant("2026-08-05T13:55:00+00:00")
CORRECTED = "2026-08-05T14:00:00+00:00"
CORRECTION_DONE = "2026-08-05T14:01:00+00:00"
SID = "FIGI:BBG000B9XRY4"
BASIS = "synthetic-split-adjusted-v1"


class FixtureProvider:
    """Static fixture; never impersonates a real model invocation."""

    def __init__(self, candidate):
        self.candidate = candidate

    def complete(self, _prompt, _schema):
        return SimpleNamespace(
            raw=json.dumps(self.candidate),
            metadata={"provider": "synthetic_timing_fixture", "real_model": False},
        )


class AIOverlayTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle_path = self.root / "bundle.json"
        sources = []
        candidate = {"abstain": False, "reason": "", "facts": []}
        for role, low, high, published in (
            ("prior", "100", "110", "2026-05-01T20:10:00Z"),
            ("current", "110", "120", "2026-08-04T20:10:00Z"),
        ):
            text = f"FY2027 Total company GAAP revenue guidance: ${low} million to ${high} million."
            filename = role + ".txt"
            (self.root / filename).write_text(text, encoding="utf-8")
            sources.append({
                "source_id": role, "role": role, "path": filename,
                "sha256": digest(text.encode()), "url": "https://example.invalid/synthetic/" + role,
                "published_at": published, "captured_at": "2026-08-04T20:15:00Z",
            })
            candidate["facts"].append({
                "source_id": role, "metric": "revenue", "basis": "gaap", "period": "FY2027",
                "unit": "million_USD", "low": low, "high": high, "quote": text,
            })
        # forward_capture is deliberately a test input here, not a claim that
        # these fabricated materials were actually captured from a live feed.
        self.bundle_path.write_text(json.dumps({
            "bundle_id": "synthetic-gate-timing-only", "security_id": SID, "ticker": "AAPL",
            "fiscal_period": "FY2027", "lane": "forward_capture", "sources": sources,
        }), encoding="utf-8")
        self.flow = AgentWorkflow(self.root / "workflow.sqlite", FixtureProvider(candidate))
        raw = json.loads((Path(__file__).resolve().parents[1] / "examples" / "structured_events.sample.jsonl").read_text())
        raw.update(event_kind="guidance", fiscal_period="FY2027",
                   input_article_record_ids=["current"], field_evidence={})
        raw["facts"] = {
            "guidance_disposition": "raised", "guidance_capture_status": "complete",
            "guidance_metrics_issued": ["revenue"], "prior_guidance_capture_status": "complete",
            "prior_guidance_metrics_issued": ["revenue"],
            "guidance": [{
                "metric": "revenue",
                "current": {
                    "low": "110", "high": "120", "period": "FY2027", "period_type": "fiscal_year",
                    "currency": "USD", "unit": "millions", "basis": "gaap", "source_record_id": "current",
                },
                "prior": {
                    "low": "100", "high": "110", "period": "FY2027", "period_type": "fiscal_year",
                    "currency": "USD", "unit": "millions", "basis": "gaap", "source_record_id": "prior",
                    "available_at_utc": "2026-05-01T20:10:00Z",
                },
            }],
        }
        self.event_raw = raw
        self.event = StructuredEventRecord.from_mapping(raw)
        self.events = EventLedger([self.event])
        self.assertTrue(qualifies_e2b(self.event))
        self.signal = EntrySignal(
            "synthetic-timing-e2b", "AAPL", SID, "event", "E2B", AT, AT, 95, "synthetic-e2b-context",
            {"price_basis_id": BASIS, "experiment_id": "E2B",
             "qualification_event_record_ids": [self.event.record_id]},
        )
        self.first = self.analyze(CREATED, COMPLETED)
        self.assertEqual("CHECKED_CANDIDATE", self.first["state"])

    def analyze(self, created, completed, expected_revision=None):
        # created, first start/end, second start/end, workflow completion.
        with patch("scenario_router.ai_workflow.stamp", side_effect=[created] * 5 + [completed]):
            return self.flow.analyze("timing", self.bundle_path, question="Synthetic timing test only",
                                     expected_revision=expected_revision)

    def correct_later(self):
        self.flow.provider = FixtureProvider({"abstain": True, "reason": "synthetic correction", "facts": []})
        return self.analyze(CORRECTED, CORRECTION_DONE, expected_revision=1)

    def quote(self, at=AT):
        return EntryQuote("AAPL", SID, BASIS, at, 100, 100, 100, 100,
                          50_000_000, "primary_common_stock")

    def gate(self, signal=None, at=AT, revision=1):
        return self.flow.gate_signal("timing", signal or self.signal,
                                     revision=revision, at=at, events=self.events)

    def test_real_workflow_and_event_ledger_allow_positive_overlay(self):
        selected = self.gate()
        self.assertEqual("E2B:AI_REVIEW_SECONDARY", selected.metadata["experiment_id"])
        self.assertEqual(self.first["run_id"], selected.metadata["ai_workflow_run_id"])
        self.assertEqual(1, selected.metadata["ai_revision"])
        self.assertEqual("E2B", self.signal.metadata["experiment_id"])
        self.assertFalse(self.first["calls"][0]["metadata"]["real_model"])
        self.assertEqual("E2B:AI_REVIEW_SECONDARY", self.gate(selected).metadata["experiment_id"])

    def test_model_must_complete_before_execution(self):
        at = instant(CREATED)
        early = replace(self.signal, detected_at=at, execute_at=at)
        with self.assertRaises(ValueError):
            self.gate(early, at=at)

    def test_identity_source_and_signal_time_must_match(self):
        for altered in (
            replace(self.signal, security_id="FIGI:OTHER"),
            replace(self.signal, variant="E2A"),
            replace(self.signal, execute_at=instant(CORRECTED)),
            replace(self.signal, metadata={**self.signal.metadata, "qualification_event_record_ids": ["wrong-event"]}),
        ):
            with self.subTest(signal=altered), self.assertRaises(ValueError):
                self.gate(altered)

    def test_same_article_but_different_guidance_year_is_rejected(self):
        raw = json.loads(json.dumps(self.event_raw))
        raw["fiscal_period"] = "FY2028"
        raw["facts"]["guidance"][0]["current"]["period"] = "FY2028"
        raw["facts"]["guidance"][0]["prior"]["period"] = "FY2028"
        other_year = StructuredEventRecord.from_mapping(raw)
        self.assertTrue(qualifies_e2b(other_year))
        with self.assertRaises(ValueError):
            self.flow.gate_signal("timing", self.signal, revision=1, at=AT,
                                  events=EventLedger([other_year]))

    def test_earnings_quarter_can_include_the_matching_annual_guidance(self):
        raw = json.loads(json.dumps(self.event_raw))
        raw.update(event_kind="earnings", fiscal_period="2026Q3")
        quarter_event = StructuredEventRecord.from_mapping(raw)
        self.assertTrue(qualifies_e2b(quarter_event))
        selected = self.flow.gate_signal("timing", self.signal, revision=1, at=AT,
                                         events=EventLedger([quarter_event]))
        self.assertEqual("E2B:AI_REVIEW_SECONDARY", selected.metadata["experiment_id"])

    def test_future_correction_does_not_change_past_gate_but_blocks_new_time(self):
        self.correct_later()
        self.assertEqual("ABSTAIN", self.flow.inspect("timing")["state"])
        self.assertEqual(self.first["run_id"], self.gate().metadata["ai_workflow_run_id"])
        for when, revision in ((CORRECTED, 1), (CORRECTION_DONE, 1), (CORRECTION_DONE, 2)):
            at = instant(when)
            selected = replace(self.signal, detected_at=at, execute_at=at)
            with self.subTest(when=when, revision=revision), self.assertRaises(ValueError):
                self.gate(selected, at=at, revision=revision)

    def test_bridge_reads_as_of_and_cancels_when_correction_actually_begins(self):
        self.correct_later()  # The replay database already contains future rows.
        ledger = PortfolioLedger(100_000)
        bridge = AIReviewPaperBridge(self.flow, "timing", PaperBroker(ledger), self.events)
        order = bridge.submit(self.signal, self.quote(), revision=1, sector="Tech", cluster="mega", as_of=AT)
        self.assertEqual("PENDING", order.status)
        self.assertEqual((), bridge.on_market([], as_of=AT))
        self.assertGreater(ledger.reserved_cash, 0)
        self.assertEqual((order.order_id,), bridge.on_market([], as_of=instant(CORRECTED)))
        self.assertEqual("CANCELLED", order.status)
        self.assertEqual(0, ledger.reserved_cash)
        self.assertEqual([], ledger.fills)

    def test_positive_bridge_fill_uses_real_gate_even_with_future_rows(self):
        self.correct_later()
        ledger = PortfolioLedger(100_000)
        bridge = AIReviewPaperBridge(self.flow, "timing", PaperBroker(ledger), self.events)
        order = bridge.submit(self.signal, self.quote(), revision=1, sector="Tech", cluster="mega", as_of=AT)
        filled = bridge.fill(order.order_id, self.quote(), as_of=AT)
        self.assertEqual("FILLED", filled.status)
        self.assertEqual(1, len(ledger.fills))
        self.assertEqual(1, len(ledger.positions))


if __name__ == "__main__":
    unittest.main()
