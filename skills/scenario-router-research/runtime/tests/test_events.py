from __future__ import annotations

import copy
import json
import unittest
from datetime import date, datetime
from pathlib import Path

from scenario_router.calendar import ExchangeSession, TradingSessionCalendar
from scenario_router.events import (
    ArticleLedger,
    ArticleRecord,
    CoverageInterval,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
    ReferenceSnapshotRecord,
    StructuredEventRecord,
    choose_information_route,
    qualify_premarket_event,
    qualifies_e2a,
    qualifies_e2b,
)


ROOT = Path(__file__).resolve().parents[1]
SECURITY_ID = "FIGI:BBG000B9XRY4"
ARTICLE_ID = "news_feed:123456:BBG000B9XRY4"
TARGET_SESSION = date(2026, 8, 5)
CALENDAR = TradingSessionCalendar([
    ExchangeSession(
        date(2026, 8, 4),
        datetime.fromisoformat("2026-08-04T13:30:00+00:00"),
        datetime.fromisoformat("2026-08-04T20:00:00+00:00"),
    ),
    ExchangeSession(
        TARGET_SESSION,
        datetime.fromisoformat("2026-08-05T13:30:00+00:00"),
        datetime.fromisoformat("2026-08-05T20:00:00+00:00"),
    ),
])


def read_line(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8").strip())


def read_lines(name: str) -> list[dict]:
    return [
        json.loads(line) for line in (ROOT / "examples" / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sample_ledgers(article_raw: dict | None = None, event_raw: dict | None = None):
    coverage = FeedCoverage.from_json(ROOT / "examples/feed_manifest.sample.json")
    article = ArticleRecord.from_mapping(article_raw or read_line("article_presence.sample.jsonl"))
    event = StructuredEventRecord.from_mapping(event_raw or read_line("structured_events.sample.jsonl"))
    return ArticleLedger([article], coverage), EventLedger([event])


def sample_references() -> ReferenceSnapshotLedger:
    return ReferenceSnapshotLedger.from_jsonl(ROOT / "examples/reference_snapshots.sample.jsonl")


def fact_leaf_pointers(value, prefix="/facts") -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set()
        for key, child in value.items():
            result |= fact_leaf_pointers(child, f"{prefix}/{key}")
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for index, child in enumerate(value):
            result |= fact_leaf_pointers(child, f"{prefix}/{index}")
        return result
    return {prefix}


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_close = datetime.fromisoformat("2026-08-04T20:00:00+00:00")
        self.market_open = datetime.fromisoformat("2026-08-05T13:30:00+00:00")

    def test_sample_is_strict_e2a(self) -> None:
        articles, events = sample_ledgers()
        decision = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual("QUALIFIED_EVENT", decision.status)
        self.assertEqual(("E1", "E2A"), decision.eligible_variants)
        self.assertEqual("event", choose_information_route(decision, "E2A").route)
        self.assertEqual("abstain", choose_information_route(decision, "E2B").route)

    def test_strict_mode_rejects_agent_record(self) -> None:
        event_raw = read_line("structured_events.sample.jsonl")
        event_raw["provenance_mode"] = "agent_extracted"
        event_raw["agent_run"] = {
            "run_id": "run-1",
            "model": "fixed-model-version",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": "2026-08-04T20:05:14Z",
            "verifier_run_id": "verify-1",
            "verifier_model": "fixed-verifier-version",
            "verifier_prompt_sha256": "sha256:" + "f" * 64,
            "verified_at_utc": "2026-08-04T20:05:14Z",
        }
        input_id = event_raw["input_article_record_ids"][0]
        event_raw["field_evidence"] = {
            pointer: {"input_record_id": input_id, "source_field": pointer}
            for pointer in fact_leaf_pointers(event_raw["facts"])
        }
        articles, events = sample_ledgers(event_raw=event_raw)
        strict = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR, "strict_primary"
        )
        secondary = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR, "agent_assisted_secondary"
        )
        self.assertIn("E1_ONLY_AGENT_FACTS_EXCLUDED_FROM_STRICT", strict.reason_codes)
        self.assertIn("E2A", secondary.eligible_variants)

    def test_agent_records_cannot_change_the_strict_primary_decision(self) -> None:
        vendor = StructuredEventRecord.from_mapping(read_line("structured_events.sample.jsonl"))
        agent_raw = read_line("structured_events.sample.jsonl")
        agent_raw["record_id"] = "event:AAPL:2026Q3:agent-root"
        agent_raw["event_key"] = "AAPL:2026Q3:agent-view"
        agent_raw["provenance_mode"] = "agent_extracted"
        agent_raw["agent_run"] = {
            "run_id": "extract-1", "model": "model-1",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": "2026-08-04T20:05:14Z",
            "verifier_run_id": "verify-1", "verifier_model": "model-2",
            "verifier_prompt_sha256": "sha256:" + "f" * 64,
            "verified_at_utc": "2026-08-04T20:05:14Z",
        }
        agent_raw["field_evidence"] = {
            pointer: {"input_record_id": ARTICLE_ID, "source_field": pointer}
            for pointer in fact_leaf_pointers(agent_raw["facts"])
        }
        agent = StructuredEventRecord.from_mapping(agent_raw)
        coverage = FeedCoverage.from_json(ROOT / "examples/feed_manifest.sample.json")
        article = ArticleRecord.from_mapping(read_line("article_presence.sample.jsonl"))
        articles = ArticleLedger([article], coverage)
        ledger = EventLedger([vendor, agent])
        strict = qualify_premarket_event(
            articles, ledger, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR,
            "strict_primary",
        )
        secondary = qualify_premarket_event(
            articles, ledger, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR,
            "agent_assisted_secondary",
        )
        self.assertEqual(("E1", "E2A"), strict.eligible_variants)
        self.assertEqual(("E1", "E2A"), secondary.eligible_variants)
        self.assertEqual((vendor.record_id,), strict.event_record_ids)
        self.assertEqual((agent.record_id,), secondary.event_record_ids)

    def test_published_before_open_but_available_after_open_does_not_leak(self) -> None:
        article_raw = read_line("article_presence.sample.jsonl")
        article_raw["first_published_at_utc"] = "2026-08-05T13:29:00Z"
        article_raw["article_available_at_utc"] = "2026-08-05T13:31:00Z"
        article_raw["ticker_link_available_at_utc"] = "2026-08-05T13:31:00Z"
        event_raw = read_line("structured_events.sample.jsonl")
        event_raw["first_published_at_utc"] = "2026-08-05T13:29:00Z"
        event_raw["effective_available_at_utc"] = "2026-08-05T13:31:00Z"
        articles, events = sample_ledgers(article_raw, event_raw)
        at_open = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual("ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE", at_open.status)
        later = articles.query(
            SECURITY_ID, self.previous_close, datetime.fromisoformat("2026-08-05T14:00:00+00:00")
        )
        self.assertEqual("ARTICLE_PRESENT", later.status)

    def test_missing_coverage_is_not_zero_news(self) -> None:
        articles, _ = sample_ledgers()
        result = articles.query(
            "UNKNOWN", self.previous_close, self.market_open
        )
        self.assertEqual("COVERAGE_UNKNOWN", result.status)
        decision = qualify_premarket_event(
            articles, EventLedger([]), sample_references(), "UNKNOWN", TARGET_SESSION, CALENDAR
        )
        self.assertEqual("abstain", choose_information_route(decision, "E2A").route)

    def test_e2a_requires_two_strict_beats_and_known_guidance(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"]["eps"]["actual"]["value"] = raw["facts"]["eps"]["consensus"]["value"]
        self.assertFalse(qualifies_e2a(StructuredEventRecord.from_mapping(raw)))
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"]["guidance_disposition"] = "unknown"
        self.assertFalse(qualifies_e2a(StructuredEventRecord.from_mapping(raw)))

    def test_e2b_midpoint_rule_and_comparability(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["record_id"] = "event:AAPL:FY2027-guidance:rev1"
        raw["event_key"] = "AAPL:FY2027:guidance"
        raw["event_kind"] = "guidance"
        raw["fiscal_period"] = "FY2027"
        raw["facts"] = {
            "guidance_disposition": "raised",
            "guidance_capture_status": "complete",
            "guidance_metrics_issued": ["revenue"],
            "prior_guidance_capture_status": "complete",
            "prior_guidance_metrics_issued": ["revenue"],
            "guidance": [{
                "metric": "revenue",
                "current": {"low": "410", "high": "414", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "billions", "basis": "company_reported", "source_record_id": ARTICLE_ID},
                "prior": {"low": "405", "high": "410", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "billions", "basis": "company_reported", "source_record_id": "prior", "available_at_utc": "2026-05-01T20:10:00Z"}
            }]
        }
        raw["field_evidence"] = {}
        record = StructuredEventRecord.from_mapping(raw)
        self.assertTrue(qualifies_e2b(record))
        mismatched = copy.deepcopy(raw)
        mismatched["facts"]["guidance"][0]["prior"]["period"] = "FY2026"
        self.assertFalse(qualifies_e2b(StructuredEventRecord.from_mapping(mismatched)))
        quarterly = copy.deepcopy(raw)
        quarterly["fiscal_period"] = "2026Q4"
        quarterly["facts"]["guidance"][0]["current"]["period"] = "2026Q4"
        quarterly["facts"]["guidance"][0]["prior"]["period"] = "2026Q4"
        quarterly["facts"]["guidance"][0]["current"]["period_type"] = "fiscal_quarter"
        quarterly["facts"]["guidance"][0]["prior"]["period_type"] = "fiscal_quarter"
        self.assertFalse(qualifies_e2b(StructuredEventRecord.from_mapping(quarterly)))
        incomplete_prior = copy.deepcopy(raw)
        incomplete_prior["facts"]["prior_guidance_capture_status"] = "partial"
        self.assertFalse(qualifies_e2b(StructuredEventRecord.from_mapping(incomplete_prior)))

    def test_one_release_can_reach_e2a_and_e2b_without_period_collision(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"].update({
            "guidance_disposition": "raised",
            "guidance_capture_status": "complete",
            "guidance_metrics_issued": ["revenue"],
            "prior_guidance_capture_status": "complete",
            "prior_guidance_metrics_issued": ["revenue"],
            "guidance": [{
                "metric": "revenue",
                "current": {
                    "low": "410", "high": "414", "period": "FY2027",
                    "period_type": "fiscal_year", "currency": "USD", "unit": "billions",
                    "basis": "company_reported", "source_record_id": ARTICLE_ID,
                },
                "prior": {
                    "low": "405", "high": "410", "period": "FY2027",
                    "period_type": "fiscal_year", "currency": "USD", "unit": "billions",
                    "basis": "company_reported", "source_record_id": "prior-guidance:AAPL:FY2027",
                    "available_at_utc": "2026-05-01T20:10:00Z",
                },
            }],
        })
        references = read_lines("reference_snapshots.sample.jsonl")
        references.append({
            "schema_version": "1.0", "record_type": "reference_snapshot",
            "record_id": "prior-guidance:AAPL:FY2027", "provider": "news_feed",
            "security_id": {"scheme": "FIGI", "value": "BBG000B9XRY4"},
            "ticker_at_snapshot": "AAPL", "snapshot_kind": "prior_guidance",
            "metric": "revenue", "fiscal_period": "FY2027", "period_type": "fiscal_year",
            "available_at_utc": "2026-05-01T20:10:00Z", "value": None,
            "low": "405", "high": "410", "currency": "USD", "unit": "billions",
            "basis": "company_reported", "raw_payload_sha256": "sha256:" + "9" * 64,
        })
        articles, events = sample_ledgers(event_raw=raw)
        reference_ledger = ReferenceSnapshotLedger(
            ReferenceSnapshotRecord.from_mapping(item) for item in references
        )
        decision = qualify_premarket_event(
            articles, events, reference_ledger, SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(("E1", "E2A", "E2B"), decision.eligible_variants)

        earnings_miss = copy.deepcopy(raw)
        earnings_miss["facts"]["eps"]["actual"]["value"] = "0.01"
        _, missed_events = sample_ledgers(event_raw=earnings_miss)
        missed = qualify_premarket_event(
            articles, missed_events, reference_ledger, SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        # E2B is a separate guidance experiment, not E2A AND guidance.
        self.assertEqual(("E1", "E2B"), missed.eligible_variants)

    def test_future_revision_cannot_change_past_replay(self) -> None:
        base = StructuredEventRecord.from_mapping(read_line("structured_events.sample.jsonl"))
        future_raw = read_line("structured_events.sample.jsonl")
        future_raw["record_id"] = "event:AAPL:2026Q3:rev2"
        future_raw["revision"] = 2
        future_raw["supersedes_record_id"] = base.record_id
        future_raw["effective_available_at_utc"] = "2026-08-05T15:00:00Z"
        future = StructuredEventRecord.from_mapping(future_raw)
        ledger = EventLedger([future, base])
        at_open = ledger.active_as_of(SECURITY_ID, self.market_open)
        self.assertEqual((base.record_id,), tuple(item.record_id for item in at_open))
        later = ledger.active_as_of(SECURITY_ID, datetime.fromisoformat("2026-08-05T16:00:00+00:00"))
        self.assertEqual((future.record_id,), tuple(item.record_id for item in later))

    def test_prompt_injection_cannot_add_action_field(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"]["buy_now"] = True
        with self.assertRaises(ValueError):
            StructuredEventRecord.from_mapping(raw)

    def test_nonfinite_financial_values_are_rejected(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            raw = read_line("structured_events.sample.jsonl")
            raw["facts"]["eps"]["actual"]["value"] = value
            with self.assertRaises(ValueError):
                StructuredEventRecord.from_mapping(raw)

    def test_structured_facts_are_deeply_immutable(self) -> None:
        record = StructuredEventRecord.from_mapping(read_line("structured_events.sample.jsonl"))
        with self.assertRaises(TypeError):
            record.facts["eps"]["actual"]["value"] = "999"

    def test_article_revision_is_point_in_time_and_retraction_keeps_presence_veto(self) -> None:
        base_raw = read_line("article_presence.sample.jsonl")
        base = ArticleRecord.from_mapping(base_raw)
        revised_raw = copy.deepcopy(base_raw)
        revised_raw["record_id"] = "news_feed:123456:BBG000B9XRY4:rev2"
        revised_raw["revision"] = 2
        revised_raw["supersedes_record_id"] = base.record_id
        revised_raw["status"] = "retracted"
        revised_raw["article_available_at_utc"] = "2026-08-05T14:00:00Z"
        revised_raw["ticker_link_available_at_utc"] = "2026-08-05T14:00:00Z"
        revised = ArticleRecord.from_mapping(revised_raw)
        coverage = FeedCoverage.from_json(ROOT / "examples/feed_manifest.sample.json")
        ledger = ArticleLedger([revised, base], coverage)
        self.assertEqual(
            (base.record_id,),
            tuple(item.record_id for item in ledger.active_as_of(SECURITY_ID, self.market_open)),
        )
        after = datetime.fromisoformat("2026-08-05T15:00:00+00:00")
        self.assertEqual((), ledger.active_as_of(SECURITY_ID, after))
        self.assertEqual("ARTICLE_PRESENT", ledger.query(SECURITY_ID, self.previous_close, after).status)

    def test_manifest_provider_must_match_article_provider(self) -> None:
        coverage_raw = json.loads((ROOT / "examples/feed_manifest.sample.json").read_text(encoding="utf-8"))
        coverage_raw["provider"] = "different-provider"
        with self.assertRaises(ValueError):
            ArticleLedger(
                [ArticleRecord.from_mapping(read_line("article_presence.sample.jsonl"))],
                FeedCoverage.from_mapping(coverage_raw),
            )

    def test_frozen_provider_category_map_cannot_be_overridden(self) -> None:
        raw = read_line("article_presence.sample.jsonl")
        raw["normalized_category"] = "guidance"
        coverage = FeedCoverage.from_json(ROOT / "examples/feed_manifest.sample.json")
        with self.assertRaises(ValueError):
            ArticleLedger([ArticleRecord.from_mapping(raw)], coverage)

    def test_coverage_missing_interval_includes_its_start_endpoint(self) -> None:
        start = datetime.fromisoformat("2026-08-05T13:30:00+00:00")
        cutoff = datetime.fromisoformat("2026-08-05T14:00:00+00:00")
        coverage = FeedCoverage("news_feed", [
            CoverageInterval(SECURITY_ID, start, cutoff, "complete"),
            CoverageInterval(
                SECURITY_ID, cutoff,
                datetime.fromisoformat("2026-08-05T14:05:00+00:00"), "missing",
            ),
        ], captured_at=datetime.fromisoformat("2026-08-05T14:05:00+00:00"),
            raw_manifest_sha256="sha256:" + "a" * 64, pagination_complete=True)
        self.assertEqual(
            "COVERAGE_UNKNOWN", ArticleLedger([], coverage).query(SECURITY_ID, start, cutoff).status
        )

    def test_verified_guidance_cut_blocks_e2a_globally(self) -> None:
        articles, base_events = sample_ledgers()
        cut_raw = read_line("structured_events.sample.jsonl")
        cut_raw.update({
            "record_id": "event:AAPL:FY2027:guidance:rev1",
            "event_key": "AAPL:FY2027:guidance",
            "event_kind": "guidance",
            "fiscal_period": "FY2027",
        })
        cut_raw["facts"] = {
            "guidance_disposition": "cut",
            "guidance_capture_status": "complete",
            "guidance_metrics_issued": ["revenue"],
            "prior_guidance_capture_status": "complete",
            "prior_guidance_metrics_issued": ["revenue"],
            "guidance": [{
                "metric": "revenue",
                "current": {"low": "90", "high": "100", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "millions", "basis": "company_reported", "source_record_id": ARTICLE_ID},
                "prior": {"low": "100", "high": "110", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "millions", "basis": "company_reported", "source_record_id": "prior", "available_at_utc": "2026-05-01T20:10:00Z"},
            }],
        }
        cut_raw["field_evidence"] = {}
        cut = StructuredEventRecord.from_mapping(cut_raw)
        decision = qualify_premarket_event(
            articles, EventLedger([*base_events.records, cut]), sample_references(),
            SECURITY_ID, TARGET_SESSION, CALENDAR,
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertTrue(
            set(decision.reason_codes)
            & {"E1_ONLY_GUIDANCE_NOT_CLEAR", "E1_ONLY_CROSS_RECORD_GUIDANCE_CONFLICT"}
        )

    def test_any_conflicted_structured_record_blocks_all_e2(self) -> None:
        articles, base_events = sample_ledgers()
        conflict_raw = read_line("structured_events.sample.jsonl")
        conflict_raw.update({
            "record_id": "event:AAPL:2026Q3:guidance-conflict:rev1",
            "event_key": "AAPL:2026Q3:guidance-conflict",
            "verification_status": "conflict",
        })
        conflict = StructuredEventRecord.from_mapping(conflict_raw)
        decision = qualify_premarket_event(
            articles, EventLedger([*base_events.records, conflict]), sample_references(),
            SECURITY_ID, TARGET_SESSION, CALENDAR,
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertTrue(
            set(decision.reason_codes)
            & {"E1_ONLY_UNVERIFIED_OR_CONFLICTED_FACTS", "E1_ONLY_NATURAL_EVENT_CONFLICT"}
        )

    def test_guidance_label_cannot_hide_a_numeric_cut(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"]["guidance_disposition"] = "maintained"
        raw["facts"]["guidance_capture_status"] = "complete"
        raw["facts"]["guidance_metrics_issued"] = ["revenue"]
        raw["facts"]["prior_guidance_capture_status"] = "complete"
        raw["facts"]["prior_guidance_metrics_issued"] = ["revenue"]
        raw["facts"]["guidance"] = [{
            "metric": "revenue",
            "current": {"low": "90", "high": "100", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "millions", "basis": "company_reported", "source_record_id": ARTICLE_ID},
            "prior": {"low": "100", "high": "110", "period": "FY2027", "period_type": "fiscal_year", "currency": "USD", "unit": "millions", "basis": "company_reported", "source_record_id": "prior", "available_at_utc": "2026-05-01T20:10:00Z"},
        }]
        self.assertFalse(qualifies_e2a(StructuredEventRecord.from_mapping(raw)))

    def test_agent_effective_time_cannot_precede_verification(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["provenance_mode"] = "agent_extracted"
        raw["field_evidence"] = {
            pointer: {"input_record_id": raw["input_article_record_ids"][0], "source_field": pointer}
            for pointer in fact_leaf_pointers(raw["facts"])
        }
        raw["agent_run"] = {
            "run_id": "run-1", "model": "model-1",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": "2026-08-05T14:00:00Z",
            "verifier_run_id": "verify-1", "verifier_model": "model-2",
            "verifier_prompt_sha256": "sha256:" + "f" * 64,
            "verified_at_utc": "2026-08-05T14:01:00Z",
        }
        with self.assertRaises(ValueError):
            StructuredEventRecord.from_mapping(raw)

    def test_agent_pending_candidate_can_exist_before_independent_review(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["provenance_mode"] = "agent_extracted"
        raw["verification_status"] = "pending"
        raw["field_evidence"] = {
            pointer: {"input_record_id": ARTICLE_ID, "source_field": pointer}
            for pointer in fact_leaf_pointers(raw["facts"])
        }
        raw["agent_run"] = {
            "run_id": "extract-1", "model": "model-1",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": raw["effective_available_at_utc"],
            "verifier_run_id": None, "verifier_model": None,
            "verifier_prompt_sha256": None, "verified_at_utc": None,
        }
        candidate = StructuredEventRecord.from_mapping(raw)
        self.assertEqual("pending", candidate.verification_status)

    def test_agent_chronology_is_enforced_inside_qualification(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["provenance_mode"] = "agent_extracted"
        raw["field_evidence"] = {
            pointer: {"input_record_id": ARTICLE_ID, "source_field": pointer}
            for pointer in fact_leaf_pointers(raw["facts"])
        }
        raw["agent_run"] = {
            "run_id": "extract-too-early", "model": "model-1",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": "2026-08-04T20:00:00Z",
            "verifier_run_id": "verify-too-early", "verifier_model": "model-2",
            "verifier_prompt_sha256": "sha256:" + "f" * 64,
            "verified_at_utc": "2026-08-04T20:01:00Z",
        }
        articles, events = sample_ledgers(event_raw=raw)
        decision = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR,
            "agent_assisted_secondary",
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertIn("E1_ONLY_EVENT_INPUT_BINDING_INVALID", decision.reason_codes)

    def test_every_recognized_article_requires_structured_coverage_for_e2(self) -> None:
        base_raw = read_line("article_presence.sample.jsonl")
        second_raw = copy.deepcopy(base_raw)
        second_raw.update({
            "record_id": "news_feed:second-guidance:BBG000B9XRY4",
            "provider_item_id": "second-guidance",
            "first_published_at_utc": "2026-08-04T21:00:00Z",
            "article_available_at_utc": "2026-08-04T21:00:01Z",
            "ticker_link_available_at_utc": "2026-08-04T21:00:01Z",
            "normalized_category": "guidance",
            "provider_categories": ["guidance"],
        })
        coverage = FeedCoverage.from_json(ROOT / "examples/feed_manifest.sample.json")
        articles = ArticleLedger(
            [ArticleRecord.from_mapping(base_raw), ArticleRecord.from_mapping(second_raw)], coverage
        )
        events = EventLedger([StructuredEventRecord.from_mapping(read_line("structured_events.sample.jsonl"))])
        decision = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertIn("E1_ONLY_INCOMPLETE_STRUCTURED_ARTICLE_COVERAGE", decision.reason_codes)

    def test_reference_snapshot_must_match_declared_consensus(self) -> None:
        raws = read_lines("reference_snapshots.sample.jsonl")
        raws[0]["value"] = "999.00"
        references = ReferenceSnapshotLedger(ReferenceSnapshotRecord.from_mapping(raw) for raw in raws)
        articles, events = sample_ledgers()
        decision = qualify_premarket_event(
            articles, events, references, SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertIn("E1_ONLY_UNVERIFIED_REFERENCE_SNAPSHOT", decision.reason_codes)

    def test_consensus_period_type_must_match_the_reported_period(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["facts"]["eps"]["actual"]["period_type"] = "fiscal_year"
        raw["facts"]["eps"]["consensus"]["period_type"] = "fiscal_year"
        self.assertFalse(qualifies_e2a(StructuredEventRecord.from_mapping(raw)))

    def test_fiscal_period_aliases_are_rejected(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        raw["fiscal_period"] = "2026-Q3"
        with self.assertRaises(ValueError):
            StructuredEventRecord.from_mapping(raw)

    def test_delayed_old_article_cannot_disappear_beside_a_fresh_catalyst(self) -> None:
        articles, events = sample_ledgers()
        delayed = read_line("article_presence.sample.jsonl")
        delayed.update({
            "record_id": "news_feed:old-lawsuit:BBG000B9XRY4",
            "provider_item_id": "old-lawsuit",
            "first_published_at_utc": "2026-08-04T19:55:00Z",
            "article_available_at_utc": "2026-08-04T20:10:00Z",
            "ticker_link_available_at_utc": "2026-08-04T20:10:00Z",
            "provider_categories": ["lawsuit"], "normalized_category": "unclassified",
        })
        combined = ArticleLedger(
            [*articles.records, ArticleRecord.from_mapping(delayed)], articles.coverage
        )
        decision = qualify_premarket_event(
            combined, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual("abstain", choose_information_route(decision, "E2A").route)
        self.assertIn("ABSTAIN_OBSERVED_ARTICLE_OUTSIDE_EVENT_WINDOW", decision.reason_codes)

    def test_event_revision_cannot_cross_strict_and_agent_provenance(self) -> None:
        raw = read_line("structured_events.sample.jsonl")
        vendor = StructuredEventRecord.from_mapping(raw)
        raw.update({
            "record_id": "event:AAPL:2026Q3:agent-revision",
            "revision": 2, "supersedes_record_id": vendor.record_id,
            "provenance_mode": "agent_extracted",
        })
        raw["agent_run"] = {
            "run_id": "extract-1", "model": "model-1",
            "prompt_sha256": "sha256:" + "d" * 64,
            "input_manifest_sha256": "sha256:" + "e" * 64,
            "generated_at_utc": raw["effective_available_at_utc"],
            "verifier_run_id": "verify-1", "verifier_model": "model-2",
            "verifier_prompt_sha256": "sha256:" + "f" * 64,
            "verified_at_utc": raw["effective_available_at_utc"],
        }
        raw["field_evidence"] = {
            pointer: {"input_record_id": ARTICLE_ID, "source_field": pointer}
            for pointer in fact_leaf_pointers(raw["facts"])
        }
        agent_revision = StructuredEventRecord.from_mapping(raw)
        with self.assertRaises(ValueError):
            EventLedger([vendor, agent_revision])

    def test_a_stale_consensus_is_rejected_when_a_newer_snapshot_is_visible(self) -> None:
        references = read_lines("reference_snapshots.sample.jsonl")
        newer = copy.deepcopy(references[0])
        newer.update({
            "record_id": "consensus:newer-before-release",
            "available_at_utc": "2026-08-04T20:01:00Z", "value": "999.0",
        })
        ledger = ReferenceSnapshotLedger(
            ReferenceSnapshotRecord.from_mapping(raw) for raw in [*references, newer]
        )
        articles, events = sample_ledgers()
        decision = qualify_premarket_event(
            articles, events, ledger, SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertIn("E1_ONLY_UNVERIFIED_REFERENCE_SNAPSHOT", decision.reason_codes)

    def test_future_consensus_cannot_change_a_past_qualification(self) -> None:
        references = read_lines("reference_snapshots.sample.jsonl")
        future = copy.deepcopy(references[0])
        future.update({
            "record_id": "consensus:future-after-release",
            "available_at_utc": "2026-08-05T15:00:00Z", "value": "999.0",
        })
        ledger = ReferenceSnapshotLedger(
            ReferenceSnapshotRecord.from_mapping(raw) for raw in [*references, future]
        )
        articles, events = sample_ledgers()
        prior = qualify_premarket_event(
            articles, events, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        after = qualify_premarket_event(
            articles, events, ledger, SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(prior, after)

    def test_competing_natural_event_cannot_avoid_conflict_with_a_new_event_key(self) -> None:
        articles, events = sample_ledgers()
        competing = read_line("structured_events.sample.jsonl")
        competing.update({"record_id": "event:competing", "event_key": "other-label"})
        competing["facts"]["eps"]["actual"]["value"] = "0.01"
        ledger = EventLedger([*events.records, StructuredEventRecord.from_mapping(competing)])
        decision = qualify_premarket_event(
            articles, ledger, sample_references(), SECURITY_ID, TARGET_SESSION, CALENDAR
        )
        self.assertEqual(("E1",), decision.eligible_variants)
        self.assertIn("E1_ONLY_NATURAL_EVENT_CONFLICT", decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
