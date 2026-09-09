"""Point-in-time article and structured-event ledgers.

All records are append-only.  The strategy uses effective availability, never
the eventual publication metadata, to avoid backfilling future knowledge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .calendar import TradingSessionCalendar
from .configuration import ARTICLE_CATEGORY_MAP
from .models import deep_freeze, require_aware, require_nonempty_string


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STRICT_PROVENANCE = {"vendor_structured", "deterministic_vendor_map"}
SECURITY_ID_SCHEMES = {"FIGI", "QC_SID"}
TICKER_RE = re.compile(r"^[A-Z0-9.\-]+$")
FISCAL_PERIOD_RE = re.compile(r"^(?:FY[0-9]{4}|[0-9]{4}Q[1-4])$")


def _require_nonempty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_security(raw: Any) -> tuple[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != {"scheme", "value"}:
        raise ValueError("security_id must contain only scheme and value")
    scheme = _require_nonempty(raw.get("scheme"), "security_id.scheme")
    value = _require_nonempty(raw.get("value"), "security_id.value")
    if scheme not in SECURITY_ID_SCHEMES:
        raise ValueError("security_id.scheme must be FIGI or QC_SID")
    return scheme, value


def canonical_security_id(scheme: str, value: str) -> str:
    return f"{scheme}:{value}"


def _validate_ticker(value: Any, field_name: str = "ticker_at_event") -> str:
    ticker = _require_nonempty(value, field_name)
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError(f"{field_name} must be an uppercase point-in-time ticker")
    return ticker


def _validate_fiscal_period(value: Any, field_name: str = "fiscal_period") -> str:
    period = _require_nonempty(value, field_name)
    if not FISCAL_PERIOD_RE.fullmatch(period):
        raise ValueError(f"{field_name} must use canonical YYYYQn or FYYYYY form")
    return period


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_pointers(value: Any, prefix: str = "/facts") -> set[str]:
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, child in value.items():
            result |= _leaf_pointers(child, f"{prefix}/{_json_pointer_escape(str(key))}")
        return result
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for index, child in enumerate(value):
            result |= _leaf_pointers(child, f"{prefix}/{index}")
        return result
    return {prefix}


def parse_utc(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp ending in Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    require_aware(parsed, field_name)
    return parsed


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: record must be an object")
        records.append(value)
    return records


def _require_exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    extra = set(value) - allowed
    missing = required - set(value)
    if extra or missing:
        raise ValueError(f"{label} shape mismatch; extra={sorted(extra)}, missing={sorted(missing)}")


def _validate_measurement(value: Any, *, consensus: bool, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    base = {"value", "currency", "unit", "basis", "period", "period_type"}
    required = set(base)
    if consensus:
        required |= {"snapshot_at_utc", "source_record_id"}
    _require_exact_keys(value, required, required, label)
    _decimal(value["value"])
    for field_name in ("currency", "unit", "basis"):
        _require_nonempty(value.get(field_name), f"{label}.{field_name}")
    _validate_fiscal_period(value.get("period"), f"{label}.period")
    if value.get("period_type") not in {"fiscal_year", "fiscal_quarter"}:
        raise ValueError(f"{label}.period_type must be fiscal_year or fiscal_quarter")
    if consensus:
        parse_utc(str(value["snapshot_at_utc"]), f"{label}.snapshot_at_utc")
        _require_nonempty(value.get("source_record_id"), f"{label}.source_record_id")


def _validate_facts_shape(value: Mapping[str, Any]) -> None:
    allowed = {
        "eps", "revenue", "guidance_disposition", "guidance",
        "guidance_capture_status", "guidance_metrics_issued",
        "prior_guidance_capture_status", "prior_guidance_metrics_issued",
    }
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"facts contain unsupported fields: {sorted(extra)}")
    for name in ("eps", "revenue"):
        if name not in value:
            continue
        block = value[name]
        if not isinstance(block, Mapping):
            raise ValueError(f"{name} must be an object")
        _require_exact_keys(block, {"actual", "consensus"}, {"actual", "consensus"}, name)
        _validate_measurement(block["actual"], consensus=False, label=f"{name}.actual")
        _validate_measurement(block["consensus"], consensus=True, label=f"{name}.consensus")
    if value.get("guidance_disposition") not in {
        "raised", "maintained", "verified_not_issued", "cut", "mixed", "unknown", None,
    }:
        raise ValueError("invalid guidance_disposition")
    guidance = value.get("guidance", [])
    if not isinstance(guidance, list):
        raise ValueError("guidance must be an array")
    if value.get("guidance_capture_status") not in {"complete", "partial", "not_applicable", None}:
        raise ValueError("invalid guidance_capture_status")
    metrics_issued = value.get("guidance_metrics_issued", [])
    if not isinstance(metrics_issued, list) or any(item not in {"revenue", "eps"} for item in metrics_issued):
        raise ValueError("invalid guidance_metrics_issued")
    if len(metrics_issued) != len(set(metrics_issued)):
        raise ValueError("duplicate guidance_metrics_issued")
    if value.get("prior_guidance_capture_status") not in {"complete", "partial", "not_applicable", None}:
        raise ValueError("invalid prior_guidance_capture_status")
    prior_metrics = value.get("prior_guidance_metrics_issued", [])
    if not isinstance(prior_metrics, list) or any(item not in {"revenue", "eps"} for item in prior_metrics):
        raise ValueError("invalid prior_guidance_metrics_issued")
    if len(prior_metrics) != len(set(prior_metrics)):
        raise ValueError("duplicate prior_guidance_metrics_issued")
    current_keys = {
        "low", "high", "period", "period_type", "currency", "unit", "basis", "source_record_id"
    }
    prior_keys = current_keys | {"available_at_utc"}
    for index, item in enumerate(guidance):
        if not isinstance(item, Mapping):
            raise ValueError(f"guidance[{index}] must be an object")
        _require_exact_keys(item, {"metric", "current", "prior"}, {"metric", "current", "prior"}, f"guidance[{index}]")
        if item["metric"] not in {"revenue", "eps"}:
            raise ValueError("unsupported guidance metric")
        current, prior = item["current"], item["prior"]
        if not isinstance(current, Mapping) or not isinstance(prior, Mapping):
            raise ValueError("guidance current/prior must be objects")
        _require_exact_keys(current, current_keys, current_keys, f"guidance[{index}].current")
        _require_exact_keys(prior, prior_keys, prior_keys, f"guidance[{index}].prior")
        current_low, current_high = _decimal(current["low"]), _decimal(current["high"])
        prior_low, prior_high = _decimal(prior["low"]), _decimal(prior["high"])
        if current_high < current_low or prior_high < prior_low:
            raise ValueError("guidance high cannot be below low")
        for point_name, point in (("current", current), ("prior", prior)):
            for field_name in ("currency", "unit", "basis", "source_record_id"):
                _require_nonempty(point.get(field_name), f"guidance[{index}].{point_name}.{field_name}")
            _validate_fiscal_period(
                point.get("period"), f"guidance[{index}].{point_name}.period"
            )
            if point.get("period_type") not in {"fiscal_year", "fiscal_quarter", "other"}:
                raise ValueError(f"guidance[{index}].{point_name}.period_type is invalid")
        parse_utc(str(prior["available_at_utc"]), "prior.available_at_utc")
    guidance_metric_set = {str(item["metric"]) for item in guidance}
    capture_status = value.get("guidance_capture_status")
    prior_capture_status = value.get("prior_guidance_capture_status")
    if capture_status == "not_applicable":
        if guidance or metrics_issued:
            raise ValueError("not_applicable guidance must have no ranges or issued metrics")
    elif capture_status == "complete" and set(metrics_issued) != guidance_metric_set:
        raise ValueError("complete guidance metrics do not match captured ranges")
    if prior_capture_status == "not_applicable" and prior_metrics:
        raise ValueError("not_applicable prior guidance must have no issued metrics")
    if prior_capture_status == "complete" and set(prior_metrics) != guidance_metric_set:
        raise ValueError("complete prior-guidance metrics do not match captured ranges")


@dataclass(frozen=True)
class ArticleRecord:
    record_id: str
    provider: str
    provider_item_id: str
    security_id_scheme: str
    security_id: str
    ticker_at_event: str
    first_published_at: datetime
    article_available_at: datetime
    ticker_link_available_at: datetime
    normalized_category: str
    category_provenance: str
    link_method: str
    provider_categories: tuple[str, ...]
    source_locator: str
    raw_payload_sha256: str
    revision: int
    supersedes_record_id: str | None
    status: str

    @property
    def effective_available_at(self) -> datetime:
        return max(self.article_available_at, self.ticker_link_available_at)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ArticleRecord":
        allowed = {
            "schema_version", "record_type", "record_id", "provider", "provider_item_id",
            "security_id", "ticker_at_event", "first_published_at_utc",
            "article_available_at_utc", "ticker_link_available_at_utc", "link_method",
            "provider_categories", "normalized_category", "category_provenance",
            "source_locator", "raw_payload_sha256", "revision", "supersedes_record_id", "status",
        }
        _require_exact_keys(raw, allowed, allowed, "article record")
        if raw.get("schema_version") != "1.0" or raw.get("record_type") != "article_presence":
            raise ValueError("unsupported article schema")
        security_scheme, security_value = _validate_security(raw.get("security_id"))
        for field_name in ("record_id", "provider", "provider_item_id", "source_locator"):
            _require_nonempty(raw.get(field_name), field_name)
        ticker = _validate_ticker(raw.get("ticker_at_event"))
        provider_categories = raw.get("provider_categories")
        if (
            not isinstance(provider_categories, list)
            or len(provider_categories) != len(set(provider_categories))
            or any(not isinstance(item, str) or not item.strip() for item in provider_categories)
        ):
            raise ValueError("provider_categories must be a unique array of non-empty strings")
        published = parse_utc(str(raw["first_published_at_utc"]), "first_published_at_utc")
        article_available = parse_utc(str(raw["article_available_at_utc"]), "article_available_at_utc")
        link_available = parse_utc(str(raw["ticker_link_available_at_utc"]), "ticker_link_available_at_utc")
        if article_available < published or link_available < published:
            raise ValueError("availability cannot precede publication")
        _require_hash(str(raw["raw_payload_sha256"]), "raw_payload_sha256")
        if raw.get("normalized_category") not in {"earnings", "guidance", "other", "unclassified"}:
            raise ValueError("invalid normalized_category")
        if raw.get("category_provenance") not in {"deterministic_vendor_map", "agent", "human"}:
            raise ValueError("invalid category_provenance")
        if raw.get("link_method") not in {"provider_ticker_link", "deterministic_security_map", "agent_inferred"}:
            raise ValueError("invalid link_method")
        if not isinstance(raw.get("revision"), int) or raw["revision"] < 1:
            raise ValueError("revision must be a positive integer")
        if raw.get("status") not in {"active", "retracted"}:
            raise ValueError("article status must be active or retracted")
        supersedes = raw.get("supersedes_record_id")
        if supersedes is not None:
            _require_nonempty(supersedes, "supersedes_record_id")
        return cls(
            record_id=str(raw["record_id"]),
            provider=str(raw["provider"]),
            provider_item_id=str(raw["provider_item_id"]),
            security_id_scheme=security_scheme,
            security_id=canonical_security_id(security_scheme, security_value),
            ticker_at_event=ticker,
            first_published_at=published,
            article_available_at=article_available,
            ticker_link_available_at=link_available,
            normalized_category=str(raw["normalized_category"]),
            category_provenance=str(raw["category_provenance"]),
            link_method=str(raw["link_method"]),
            provider_categories=tuple(provider_categories),
            source_locator=str(raw["source_locator"]),
            raw_payload_sha256=str(raw["raw_payload_sha256"]),
            revision=int(raw["revision"]),
            supersedes_record_id=None if supersedes is None else str(supersedes),
            status=str(raw["status"]),
        )


@dataclass(frozen=True)
class CoverageInterval:
    security_id: str
    start: datetime
    end: datetime
    status: str


class FeedCoverage:
    def __init__(
        self,
        provider: str,
        intervals: Iterable[CoverageInterval],
        captured_at: datetime,
        raw_manifest_sha256: str,
        pagination_complete: bool,
    ) -> None:
        require_nonempty_string(provider, "provider")
        require_aware(captured_at, "captured_at")
        _require_hash(raw_manifest_sha256, "raw_manifest_sha256")
        if pagination_complete is not True:
            raise ValueError("feed pagination is not complete")
        self.provider = provider
        self.intervals = tuple(intervals)
        self.captured_at = captured_at
        self.raw_manifest_sha256 = raw_manifest_sha256
        self.pagination_complete = pagination_complete
        for item in self.intervals:
            require_nonempty_string(item.security_id, "coverage.security_id")
            require_aware(item.start, "coverage.start")
            require_aware(item.end, "coverage.end")
            if item.status not in {"complete", "missing"}:
                raise ValueError("coverage status must be complete or missing")
            if item.end <= item.start:
                raise ValueError("coverage end must be after start")
            if item.end > captured_at:
                raise ValueError("coverage cannot extend beyond manifest capture time")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FeedCoverage":
        allowed = {
            "schema_version", "record_type", "provider", "captured_at_utc",
            "pagination_complete", "raw_manifest_sha256", "coverage",
        }
        _require_exact_keys(raw, allowed, allowed, "feed manifest")
        if raw.get("schema_version") != "1.0" or raw.get("record_type") != "feed_manifest":
            raise ValueError("unsupported feed manifest schema")
        provider = _require_nonempty(raw.get("provider"), "provider")
        captured_at = parse_utc(str(raw["captured_at_utc"]), "captured_at_utc")
        _require_hash(str(raw["raw_manifest_sha256"]), "raw_manifest_sha256")
        if raw.get("pagination_complete") is not True:
            raise ValueError("feed pagination is not complete")
        intervals: list[CoverageInterval] = []
        coverage = raw.get("coverage")
        if not isinstance(coverage, list):
            raise ValueError("coverage must be an array")
        for item in coverage:
            if not isinstance(item, Mapping):
                raise ValueError("coverage entries must be objects")
            if set(item) != {"security_id", "ticker_at_event", "start_at_utc", "end_at_utc", "status"}:
                raise ValueError("coverage entries have an unexpected shape")
            security_scheme, security_value = _validate_security(item.get("security_id"))
            security_id = canonical_security_id(security_scheme, security_value)
            _validate_ticker(item.get("ticker_at_event"), "coverage.ticker_at_event")
            if item.get("status") not in {"complete", "missing"}:
                raise ValueError("coverage status must be complete or missing")
            start = parse_utc(str(item["start_at_utc"]), "start_at_utc")
            end = parse_utc(str(item["end_at_utc"]), "end_at_utc")
            if end <= start:
                raise ValueError("coverage end must be after start")
            if end > captured_at:
                raise ValueError("coverage cannot extend beyond manifest capture time")
            intervals.append(CoverageInterval(security_id, start, end, str(item["status"])))
        return cls(
            provider,
            intervals,
            captured_at,
            str(raw["raw_manifest_sha256"]),
            bool(raw["pagination_complete"]),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "FeedCoverage":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def covers(self, security_id: str, start: datetime, end: datetime) -> bool:
        require_aware(start, "start")
        require_aware(end, "end")
        if end < start:
            raise ValueError("coverage query end precedes start")
        relevant = [item for item in self.intervals if item.security_id == security_id]
        if any(item.status == "missing" and item.start <= end and item.end >= start for item in relevant):
            return False
        complete = sorted(
            (item for item in relevant if item.status == "complete"),
            key=lambda item: item.start,
        )
        cursor = start
        for item in complete:
            if item.end < cursor:
                continue
            if item.start > cursor:
                return False
            cursor = max(cursor, item.end)
            if cursor >= end:
                return True
        return False


@dataclass(frozen=True)
class ArticleQuery:
    status: str
    records: tuple[ArticleRecord, ...]


class ArticleLedger:
    def __init__(self, records: Iterable[ArticleRecord], coverage: FeedCoverage) -> None:
        by_id: dict[str, ArticleRecord] = {}
        for record in records:
            existing = by_id.get(record.record_id)
            if existing is not None and existing != record:
                raise ValueError(f"conflicting duplicate article record_id: {record.record_id}")
            by_id[record.record_id] = record
        for record in by_id.values():
            if record.provider != coverage.provider:
                raise ValueError(
                    f"article provider {record.provider!r} does not match manifest provider {coverage.provider!r}"
                )
            if record.category_provenance == "deterministic_vendor_map":
                provider_map = ARTICLE_CATEGORY_MAP.get(record.provider)
                if provider_map is None:
                    raise ValueError(f"no frozen category map exists for provider {record.provider!r}")
                mapped = {
                    provider_map.get(category, "unclassified")
                    for category in record.provider_categories
                }
                expected_category = next(iter(mapped)) if len(mapped) == 1 else "unclassified"
                if record.normalized_category != expected_category:
                    raise ValueError(
                        "normalized_category does not match the frozen provider-category map"
                    )
            if record.supersedes_record_id is None:
                if record.revision != 1:
                    raise ValueError("an article root record must have revision 1")
                continue
            prior = by_id.get(record.supersedes_record_id)
            if prior is None:
                raise ValueError(f"article revision references a missing record: {record.supersedes_record_id}")
            if (
                prior.provider != record.provider
                or prior.provider_item_id != record.provider_item_id
                or prior.security_id != record.security_id
                or prior.security_id_scheme != record.security_id_scheme
                or prior.first_published_at != record.first_published_at
            ):
                raise ValueError("article revision changes immutable publication identity")
            if record.revision <= prior.revision:
                raise ValueError("article revision must increase")
            if record.effective_available_at < prior.effective_available_at:
                raise ValueError("article revision cannot become effective before its predecessor")
        self.records = tuple(sorted(by_id.values(), key=lambda item: (item.effective_available_at, item.record_id)))
        self.coverage = coverage

    @classmethod
    def from_jsonl(cls, path: str | Path, coverage: FeedCoverage) -> "ArticleLedger":
        return cls((ArticleRecord.from_mapping(raw) for raw in _load_jsonl(Path(path))), coverage)

    def query(self, security_id: str, start: datetime, as_of: datetime) -> ArticleQuery:
        require_aware(start, "start")
        require_aware(as_of, "as_of")
        if not self.coverage.covers(security_id, start, as_of):
            return ArticleQuery("COVERAGE_UNKNOWN", ())
        matched = tuple(
            record for record in self.records
            if record.security_id == security_id and start <= record.effective_available_at <= as_of
        )
        return ArticleQuery("ARTICLE_PRESENT" if matched else "ZERO_ARTICLE", matched)

    def active_as_of(self, security_id: str, as_of: datetime) -> tuple[ArticleRecord, ...]:
        """Return latest visible, non-retracted revisions at a point in time."""

        require_aware(as_of, "as_of")
        visible = [
            record for record in self.records
            if record.security_id == security_id and record.effective_available_at <= as_of
        ]
        superseded = {record.supersedes_record_id for record in visible if record.supersedes_record_id}
        return tuple(
            record for record in visible
            if record.record_id not in superseded and record.status == "active"
        )


@dataclass(frozen=True)
class StructuredEventRecord:
    record_id: str
    event_key: str
    security_id_scheme: str
    security_id: str
    ticker_at_event: str
    event_kind: str
    fiscal_period: str
    first_published_at: datetime
    effective_available_at: datetime
    input_article_record_ids: tuple[str, ...]
    provenance_mode: str
    verification_status: str
    facts: Mapping[str, Any]
    field_evidence: Mapping[str, Any]
    agent_run: Mapping[str, Any] | None
    raw_inputs_sha256: str
    revision: int
    supersedes_record_id: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StructuredEventRecord":
        allowed = {
            "schema_version", "record_type", "record_id", "event_key", "security_id",
            "ticker_at_event", "event_kind", "fiscal_period", "first_published_at_utc",
            "effective_available_at_utc", "input_article_record_ids", "provenance_mode",
            "verification_status", "facts", "field_evidence", "agent_run", "raw_inputs_sha256",
            "revision", "supersedes_record_id",
        }
        _require_exact_keys(raw, allowed, allowed, "structured event")
        if raw.get("schema_version") != "1.0" or raw.get("record_type") != "structured_event":
            raise ValueError("unsupported structured-event schema")
        security_scheme, security_value = _validate_security(raw.get("security_id"))
        for field_name in ("record_id", "event_key"):
            _require_nonempty(raw.get(field_name), field_name)
        fiscal_period = _validate_fiscal_period(raw.get("fiscal_period"))
        ticker = _validate_ticker(raw.get("ticker_at_event"))
        published = parse_utc(str(raw["first_published_at_utc"]), "first_published_at_utc")
        available = parse_utc(str(raw["effective_available_at_utc"]), "effective_available_at_utc")
        if available < published:
            raise ValueError("event availability cannot precede publication")
        _require_hash(str(raw["raw_inputs_sha256"]), "raw_inputs_sha256")
        if raw.get("event_kind") not in {"earnings", "guidance"}:
            raise ValueError("event_kind must be earnings or guidance")
        if not isinstance(raw.get("facts"), Mapping):
            raise ValueError("facts must be an object")
        _validate_facts_shape(raw["facts"])
        if raw.get("provenance_mode") not in STRICT_PROVENANCE | {"agent_extracted"}:
            raise ValueError("invalid provenance_mode")
        if raw.get("verification_status") not in {"verified", "pending", "conflict", "rejected"}:
            raise ValueError("invalid verification_status")
        input_ids = raw.get("input_article_record_ids")
        if (
            not isinstance(input_ids, list)
            or not input_ids
            or len(input_ids) != len(set(input_ids))
            or any(not isinstance(item, str) or not item.strip() for item in input_ids)
        ):
            raise ValueError("input_article_record_ids must be a non-empty unique array")
        for guidance_item in raw["facts"].get("guidance", []):
            current_source = guidance_item["current"]["source_record_id"]
            if current_source not in input_ids:
                raise ValueError("current guidance source_record_id must be a declared input article")
        if not isinstance(raw.get("revision"), int) or raw["revision"] < 1:
            raise ValueError("revision must be a positive integer")
        evidence = raw.get("field_evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("field_evidence must be an object")
        fact_pointers = _leaf_pointers(raw["facts"])
        for pointer, item in evidence.items():
            if not isinstance(pointer, str) or not isinstance(item, Mapping):
                raise ValueError("invalid field_evidence entry")
            if pointer not in fact_pointers:
                raise ValueError(f"field_evidence pointer does not exist in facts: {pointer}")
            _require_exact_keys(
                item, {"input_record_id", "source_field"}, {"input_record_id", "source_field"}, "field_evidence"
            )
            if item["input_record_id"] not in input_ids:
                raise ValueError("field_evidence references an undeclared input record")
            _require_nonempty(item.get("source_field"), "field_evidence.source_field")
        agent_run = raw.get("agent_run")
        if raw.get("provenance_mode") == "agent_extracted":
            required_agent = {
                "run_id", "model", "prompt_sha256", "input_manifest_sha256", "generated_at_utc",
                "verifier_run_id", "verifier_model", "verifier_prompt_sha256", "verified_at_utc",
            }
            if not isinstance(agent_run, Mapping) or set(agent_run) != required_agent:
                raise ValueError("agent-extracted events require a complete agent_run")
            for field_name in ("run_id", "model"):
                _require_nonempty(agent_run.get(field_name), f"agent_run.{field_name}")
            _require_hash(str(agent_run["prompt_sha256"]), "prompt_sha256")
            _require_hash(str(agent_run["input_manifest_sha256"]), "input_manifest_sha256")
            generated_at = parse_utc(str(agent_run["generated_at_utc"]), "generated_at_utc")
            if available < generated_at:
                raise ValueError("agent event effective time must include extraction completion")
            verifier_fields = (
                "verifier_run_id", "verifier_model", "verifier_prompt_sha256", "verified_at_utc"
            )
            if raw.get("verification_status") == "pending":
                if any(agent_run.get(field_name) is not None for field_name in verifier_fields):
                    raise ValueError("pending agent candidates must leave verifier fields null")
            else:
                for field_name in ("verifier_run_id", "verifier_model"):
                    _require_nonempty(agent_run.get(field_name), f"agent_run.{field_name}")
                _require_hash(str(agent_run["verifier_prompt_sha256"]), "verifier_prompt_sha256")
                verified_at = parse_utc(str(agent_run["verified_at_utc"]), "verified_at_utc")
                if verified_at < generated_at:
                    raise ValueError("agent verification cannot precede extraction")
                if available < verified_at:
                    raise ValueError("agent event effective time must include verifier completion")
                if agent_run["verifier_run_id"] == agent_run["run_id"]:
                    raise ValueError("extractor and verifier must be independent runs")
                if agent_run["verifier_prompt_sha256"] == agent_run["prompt_sha256"]:
                    raise ValueError("extractor and verifier must use distinct role prompts")
            missing_evidence = fact_pointers - set(evidence)
            if missing_evidence:
                raise ValueError(
                    "agent-extracted events require evidence for every facts leaf: "
                    + ", ".join(sorted(missing_evidence))
                )
        elif agent_run is not None:
            raise ValueError("non-agent events must set agent_run to null")
        return cls(
            record_id=str(raw["record_id"]),
            event_key=str(raw["event_key"]),
            security_id_scheme=security_scheme,
            security_id=canonical_security_id(security_scheme, security_value),
            ticker_at_event=ticker,
            event_kind=str(raw["event_kind"]),
            fiscal_period=fiscal_period,
            first_published_at=published,
            effective_available_at=available,
            input_article_record_ids=tuple(str(item) for item in raw.get("input_article_record_ids", [])),
            provenance_mode=str(raw["provenance_mode"]),
            verification_status=str(raw["verification_status"]),
            facts=deep_freeze(raw["facts"]),
            field_evidence=deep_freeze(evidence),
            agent_run=None if agent_run is None else deep_freeze(agent_run),
            raw_inputs_sha256=str(raw["raw_inputs_sha256"]),
            revision=int(raw["revision"]),
            supersedes_record_id=None if raw.get("supersedes_record_id") is None else str(raw["supersedes_record_id"]),
        )


class EventLedger:
    def __init__(self, records: Iterable[StructuredEventRecord]) -> None:
        by_id: dict[str, StructuredEventRecord] = {}
        for record in records:
            existing = by_id.get(record.record_id)
            if existing is not None and existing != record:
                raise ValueError(f"conflicting duplicate event record_id: {record.record_id}")
            by_id[record.record_id] = record
        for record in by_id.values():
            if record.supersedes_record_id is None:
                if record.revision != 1:
                    raise ValueError("an event root record must have revision 1")
                continue
            prior = by_id.get(record.supersedes_record_id)
            if prior is None:
                raise ValueError(f"event revision references a missing record: {record.supersedes_record_id}")
            if (
                prior.event_key != record.event_key
                or prior.security_id != record.security_id
                or prior.security_id_scheme != record.security_id_scheme
                or prior.first_published_at != record.first_published_at
                or prior.event_kind != record.event_kind
                or prior.fiscal_period != record.fiscal_period
                or prior.provenance_mode != record.provenance_mode
            ):
                raise ValueError("event revision changes immutable event identity")
            if record.revision <= prior.revision:
                raise ValueError("event revision must increase")
            if record.effective_available_at < prior.effective_available_at:
                raise ValueError("event revision cannot become effective before its predecessor")
        self.records = tuple(sorted(by_id.values(), key=lambda item: (item.effective_available_at, item.record_id)))

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "EventLedger":
        return cls(StructuredEventRecord.from_mapping(raw) for raw in _load_jsonl(Path(path)))

    def active_as_of(self, security_id: str, as_of: datetime) -> tuple[StructuredEventRecord, ...]:
        visible = [
            record for record in self.records
            if record.security_id == security_id and record.effective_available_at <= as_of
        ]
        superseded = {record.supersedes_record_id for record in visible if record.supersedes_record_id}
        return tuple(record for record in visible if record.record_id not in superseded)


@dataclass(frozen=True)
class ReferenceSnapshotRecord:
    record_id: str
    provider: str
    security_id_scheme: str
    security_id: str
    ticker_at_snapshot: str
    snapshot_kind: str
    metric: str
    fiscal_period: str
    period_type: str
    available_at: datetime
    value: str | None
    low: str | None
    high: str | None
    currency: str
    unit: str
    basis: str
    raw_payload_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReferenceSnapshotRecord":
        keys = {
            "schema_version", "record_type", "record_id", "provider", "security_id",
            "ticker_at_snapshot", "snapshot_kind", "metric", "fiscal_period", "period_type",
            "available_at_utc", "value", "low", "high", "currency", "unit", "basis",
            "raw_payload_sha256",
        }
        _require_exact_keys(raw, keys, keys, "reference snapshot")
        if raw.get("schema_version") != "1.0" or raw.get("record_type") != "reference_snapshot":
            raise ValueError("unsupported reference-snapshot schema")
        scheme, value = _validate_security(raw.get("security_id"))
        for field_name in ("record_id", "provider", "currency", "unit", "basis"):
            _require_nonempty(raw.get(field_name), field_name)
        fiscal_period = _validate_fiscal_period(raw.get("fiscal_period"))
        ticker = _validate_ticker(raw.get("ticker_at_snapshot"), "ticker_at_snapshot")
        if raw.get("snapshot_kind") not in {"consensus", "prior_guidance"}:
            raise ValueError("snapshot_kind must be consensus or prior_guidance")
        if raw.get("metric") not in {"eps", "revenue"}:
            raise ValueError("snapshot metric must be eps or revenue")
        if raw.get("period_type") not in {"fiscal_year", "fiscal_quarter", "other"}:
            raise ValueError("invalid snapshot period_type")
        available_at = parse_utc(str(raw["available_at_utc"]), "available_at_utc")
        _require_hash(str(raw["raw_payload_sha256"]), "raw_payload_sha256")
        if raw["snapshot_kind"] == "consensus":
            if raw.get("value") is None or raw.get("low") is not None or raw.get("high") is not None:
                raise ValueError("consensus snapshots require value and forbid low/high")
            _decimal(raw["value"])
        else:
            if raw.get("value") is not None or raw.get("low") is None or raw.get("high") is None:
                raise ValueError("prior-guidance snapshots require low/high and forbid value")
            low, high = _decimal(raw["low"]), _decimal(raw["high"])
            if high < low:
                raise ValueError("snapshot high cannot be below low")
        return cls(
            record_id=str(raw["record_id"]),
            provider=str(raw["provider"]),
            security_id_scheme=scheme,
            security_id=canonical_security_id(scheme, value),
            ticker_at_snapshot=ticker,
            snapshot_kind=str(raw["snapshot_kind"]),
            metric=str(raw["metric"]),
            fiscal_period=fiscal_period,
            period_type=str(raw["period_type"]),
            available_at=available_at,
            value=None if raw.get("value") is None else str(raw["value"]),
            low=None if raw.get("low") is None else str(raw["low"]),
            high=None if raw.get("high") is None else str(raw["high"]),
            currency=str(raw["currency"]),
            unit=str(raw["unit"]),
            basis=str(raw["basis"]),
            raw_payload_sha256=str(raw["raw_payload_sha256"]),
        )


class ReferenceSnapshotLedger:
    def __init__(self, records: Iterable[ReferenceSnapshotRecord]) -> None:
        by_id: dict[str, ReferenceSnapshotRecord] = {}
        for record in records:
            existing = by_id.get(record.record_id)
            if existing is not None and existing != record:
                raise ValueError(f"conflicting duplicate reference snapshot: {record.record_id}")
            by_id[record.record_id] = record
        records_tuple = tuple(sorted(by_id.values(), key=lambda item: (item.available_at, item.record_id)))
        by_time_and_key: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
        for record in records_tuple:
            key = (*self._comparison_key(record), record.available_at)
            economic_value = (record.value, record.low, record.high, record.period_type)
            by_time_and_key.setdefault(key, set()).add(economic_value)
        if any(len(values) > 1 for values in by_time_and_key.values()):
            raise ValueError("reference ledger has conflicting values at the same logical timestamp")
        self.records = records_tuple
        self.by_id = MappingProxyType(by_id)

    @staticmethod
    def _comparison_key(record: ReferenceSnapshotRecord) -> tuple[str, ...]:
        """Identity of the economic reference whose latest visible value is mandatory."""

        return (
            record.snapshot_kind,
            record.security_id,
            record.metric,
            record.fiscal_period,
            record.currency,
            record.unit,
            record.basis,
        )

    def _require_latest_before(
        self, source: ReferenceSnapshotRecord, event_time: datetime
    ) -> None:
        candidates = tuple(
            record
            for record in self.records
            if self._comparison_key(record) == self._comparison_key(source)
            and record.available_at < event_time
        )
        if not candidates:
            raise ValueError(f"no point-in-time predecessor exists for {source.record_id}")
        latest_at = max(record.available_at for record in candidates)
        if source.available_at != latest_at:
            raise ValueError(
                f"{source.record_id} is not the latest visible point-in-time reference"
            )

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "ReferenceSnapshotLedger":
        return cls(ReferenceSnapshotRecord.from_mapping(raw) for raw in _load_jsonl(Path(path)))

    def get(self, record_id: str) -> ReferenceSnapshotRecord:
        try:
            return self.by_id[record_id]
        except KeyError as exc:
            raise ValueError(f"missing point-in-time reference snapshot: {record_id}") from exc

    def validate_event(self, event: StructuredEventRecord) -> None:
        for metric in ("eps", "revenue"):
            block = event.facts.get(metric)
            if not isinstance(block, Mapping):
                continue
            consensus = block.get("consensus")
            if not isinstance(consensus, Mapping):
                continue
            source = self.get(str(consensus.get("source_record_id")))
            expected = (
                "consensus", metric, event.security_id, str(consensus.get("period")),
                str(consensus.get("period_type")),
                str(consensus.get("currency")), str(consensus.get("unit")), str(consensus.get("basis")),
                str(consensus.get("value")), parse_utc(str(consensus.get("snapshot_at_utc")), "snapshot_at_utc"),
            )
            actual = (
                source.snapshot_kind, source.metric, source.security_id, source.fiscal_period,
                source.period_type,
                source.currency, source.unit, source.basis, source.value, source.available_at,
            )
            if actual != expected or source.available_at >= event.first_published_at:
                raise ValueError(f"{event.record_id}: consensus snapshot does not match {source.record_id}")
            self._require_latest_before(source, event.first_published_at)
        guidance = event.facts.get("guidance", ())
        if isinstance(guidance, Sequence) and not isinstance(guidance, (str, bytes)):
            for item in guidance:
                if not isinstance(item, Mapping) or not isinstance(item.get("prior"), Mapping):
                    continue
                prior = item["prior"]
                source = self.get(str(prior.get("source_record_id")))
                expected = (
                    "prior_guidance", str(item.get("metric")), event.security_id,
                    str(prior.get("period")), str(prior.get("period_type")),
                    str(prior.get("currency")), str(prior.get("unit")), str(prior.get("basis")),
                    str(prior.get("low")), str(prior.get("high")),
                    parse_utc(str(prior.get("available_at_utc")), "prior.available_at_utc"),
                )
                actual = (
                    source.snapshot_kind, source.metric, source.security_id, source.fiscal_period,
                    source.period_type, source.currency, source.unit, source.basis,
                    source.low, source.high, source.available_at,
                )
                if actual != expected or source.available_at >= event.first_published_at:
                    raise ValueError(f"{event.record_id}: prior-guidance snapshot does not match {source.record_id}")
                self._require_latest_before(source, event.first_published_at)


@dataclass(frozen=True)
class EventQualification:
    security_id: str
    window_start: datetime
    cutoff: datetime
    mode: str
    status: str
    eligible_variants: tuple[str, ...]
    event_record_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty_string(self.security_id, "security_id")
        require_aware(self.window_start, "window_start")
        require_aware(self.cutoff, "cutoff")
        if self.cutoff <= self.window_start:
            raise ValueError("qualification cutoff must follow its event-window start")
        if self.mode not in {"strict_primary", "agent_assisted_secondary"}:
            raise ValueError("invalid qualification mode")
        allowed_statuses = {
            "QUALIFIED_EVENT", "QUALIFIED_EVENT_E1_ONLY",
            "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", "ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE",
            "COVERAGE_UNKNOWN",
        }
        if self.status not in allowed_statuses:
            raise ValueError("invalid event qualification status")
        allowed_variants = {"E1", "E2A", "E2B"}
        if (
            len(self.eligible_variants) != len(set(self.eligible_variants))
            or any(item not in allowed_variants for item in self.eligible_variants)
        ):
            raise ValueError("invalid or duplicate event qualification variant")
        if self.status == "QUALIFIED_EVENT":
            if not self.eligible_variants or self.eligible_variants[0] != "E1" or len(self.eligible_variants) < 2:
                raise ValueError("QUALIFIED_EVENT must contain E1 and at least one E2 variant")
            if (
                not self.event_record_ids
                or len(self.event_record_ids) != len(set(self.event_record_ids))
                or any(not isinstance(item, str) or not item for item in self.event_record_ids)
            ):
                raise ValueError("QUALIFIED_EVENT requires unique, non-empty event evidence IDs")
            expected_reasons = tuple(f"ELIGIBLE_{variant}" for variant in self.eligible_variants)
            if self.reason_codes != expected_reasons:
                raise ValueError("QUALIFIED_EVENT reasons must exactly match its eligible variants")
        elif self.status == "QUALIFIED_EVENT_E1_ONLY":
            if self.eligible_variants != ("E1",):
                raise ValueError("QUALIFIED_EVENT_E1_ONLY must contain only E1")
        elif self.eligible_variants:
            raise ValueError(f"{self.status} cannot contain eligible variants")
        elif self.event_record_ids:
            raise ValueError(f"{self.status} cannot carry structured-event evidence IDs")
        if (
            len(self.event_record_ids) != len(set(self.event_record_ids))
            or any(not isinstance(item, str) or not item for item in self.event_record_ids)
        ):
            raise ValueError("event_record_ids must be unique non-empty strings")
        if not self.reason_codes or any(not isinstance(item, str) or not item for item in self.reason_codes):
            raise ValueError("qualification requires non-empty reason codes")


@dataclass(frozen=True)
class InformationRoute:
    security_id: str
    route: str
    reason: str
    computed_at: datetime
    window_start: datetime
    mode: str

    def __post_init__(self) -> None:
        require_nonempty_string(self.security_id, "security_id")
        require_nonempty_string(self.reason, "reason")
        require_aware(self.computed_at, "computed_at")
        require_aware(self.window_start, "window_start")
        if self.computed_at <= self.window_start:
            raise ValueError("information route cutoff must follow its window start")
        if self.mode not in {"strict_primary", "agent_assisted_secondary"}:
            raise ValueError("invalid information route mode")
        if self.route not in {"event", "reversal", "abstain"}:
            raise ValueError("invalid information route")


def choose_information_route(qualification: EventQualification, event_variant: str) -> InformationRoute:
    """Choose the final mutually exclusive branch for one event experiment."""

    if event_variant not in {"E2A", "E2B"}:
        raise ValueError("final router requires a separately selected E2A or E2B arm")
    details = {
        "security_id": qualification.security_id,
        "computed_at": qualification.cutoff,
        "window_start": qualification.window_start,
        "mode": qualification.mode,
    }
    if event_variant in qualification.eligible_variants:
        return InformationRoute(route="event", reason=f"ELIGIBLE_{event_variant}", **details)
    if qualification.status == "ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE":
        return InformationRoute(route="reversal", reason="ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE", **details)
    if qualification.status == "COVERAGE_UNKNOWN":
        return InformationRoute(route="abstain", reason="ABSTAIN_MISSING_COVERAGE", **details)
    return InformationRoute(
        route="abstain",
        reason=qualification.reason_codes[0] if qualification.reason_codes else "ABSTAIN",
        **details,
    )


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("financial values must be decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value}") from exc
    if not parsed.is_finite():
        raise ValueError(f"financial value must be finite: {value}")
    return parsed


def _comparison_pair(record: StructuredEventRecord, name: str) -> tuple[Decimal, Decimal]:
    block = record.facts.get(name)
    if not isinstance(block, Mapping):
        raise ValueError(f"missing {name}")
    actual = block.get("actual")
    consensus = block.get("consensus")
    if not isinstance(actual, Mapping) or not isinstance(consensus, Mapping):
        raise ValueError(f"missing {name} actual/consensus")
    comparable = ("currency", "unit", "basis", "period", "period_type")
    if any(actual.get(key) != consensus.get(key) for key in comparable):
        raise ValueError(f"{name} basis mismatch")
    if actual.get("period") != record.fiscal_period:
        raise ValueError(f"{name} period does not match the event fiscal period")
    expected_period_type = "fiscal_year" if record.fiscal_period.startswith("FY") else "fiscal_quarter"
    if actual.get("period_type") != expected_period_type:
        raise ValueError(f"{name} period type does not match the canonical event fiscal period")
    snapshot = parse_utc(str(consensus["snapshot_at_utc"]), f"{name}.consensus.snapshot_at_utc")
    if snapshot >= record.first_published_at:
        raise ValueError(f"{name} consensus is not point-in-time")
    return _decimal(actual.get("value")), _decimal(consensus.get("value"))


def qualifies_e2a(record: StructuredEventRecord) -> bool:
    double_beat = _earnings_double_beat(record)
    if double_beat is not True:
        return False
    disposition = record.facts.get("guidance_disposition")
    guidance = record.facts.get("guidance", ())
    if disposition == "verified_not_issued":
        guidance_ok = not guidance and record.facts.get("guidance_capture_status") == "not_applicable"
    elif guidance:
        directions = _guidance_directions(record)
        expected_disposition = None
        if directions is not None and all(value >= 0 for value in directions):
            expected_disposition = "raised" if any(value > 0 for value in directions) else "maintained"
        guidance_ok = expected_disposition is not None and disposition in {None, expected_disposition}
    else:
        guidance_ok = False
    return guidance_ok


def _earnings_double_beat(record: StructuredEventRecord) -> bool | None:
    if record.event_kind != "earnings":
        return None
    try:
        eps_actual, eps_consensus = _comparison_pair(record, "eps")
        revenue_actual, revenue_consensus = _comparison_pair(record, "revenue")
    except (KeyError, TypeError, ValueError):
        return False
    return eps_actual > eps_consensus and revenue_actual > revenue_consensus


def _guidance_directions(
    record: StructuredEventRecord, *, require_fiscal_year: bool = False
) -> list[int] | None:
    guidance = record.facts.get("guidance")
    if not isinstance(guidance, Sequence) or isinstance(guidance, (str, bytes)) or not guidance:
        return None
    if record.facts.get("guidance_capture_status") != "complete":
        return None
    directions: list[int] = []
    seen: set[str] = set()
    for item in guidance:
        if not isinstance(item, Mapping) or item.get("metric") not in {"revenue", "eps"}:
            return None
        metric = str(item["metric"])
        if metric in seen:
            return None
        seen.add(metric)
        current = item.get("current")
        prior = item.get("prior")
        if not isinstance(current, Mapping) or not isinstance(prior, Mapping):
            return None
        comparable = ("period", "period_type", "currency", "unit", "basis")
        if any(current.get(key) != prior.get(key) for key in comparable):
            return None
        if require_fiscal_year and current.get("period_type") != "fiscal_year":
            return None
        if (
            require_fiscal_year
            and record.event_kind == "guidance"
            and current.get("period") != record.fiscal_period
        ):
            return None
        try:
            current_mid = (_decimal(current.get("low")) + _decimal(current.get("high"))) / 2
            prior_mid = (_decimal(prior.get("low")) + _decimal(prior.get("high"))) / 2
            prior_available = parse_utc(str(prior["available_at_utc"]), "prior.available_at_utc")
        except (KeyError, TypeError, ValueError):
            return None
        if prior_available >= record.first_published_at:
            return None
        directions.append(1 if current_mid > prior_mid else (-1 if current_mid < prior_mid else 0))
    if set(record.facts.get("guidance_metrics_issued", [])) != seen:
        return None
    if record.facts.get("prior_guidance_capture_status") != "complete":
        return None
    if set(record.facts.get("prior_guidance_metrics_issued", [])) != seen:
        return None
    return directions


def qualifies_e2b(record: StructuredEventRecord) -> bool:
    directions = _guidance_directions(record, require_fiscal_year=True)
    if directions is None:
        return False
    return any(direction > 0 for direction in directions) and all(direction >= 0 for direction in directions)


def _has_unsafe_or_inconsistent_guidance(record: StructuredEventRecord) -> bool:
    disposition = record.facts.get("guidance_disposition")
    if disposition in {"cut", "mixed", "unknown"}:
        return True
    guidance = record.facts.get("guidance", ())
    if disposition == "verified_not_issued":
        return bool(guidance) or record.facts.get("guidance_capture_status") != "not_applicable"
    if disposition in {"raised", "maintained"} and not guidance:
        return True
    if guidance:
        directions = _guidance_directions(record)
        if directions is None:
            return True
        if any(value < 0 for value in directions):
            return True
        expected_disposition = "raised" if any(value > 0 for value in directions) else "maintained"
        return disposition not in {None, expected_disposition}
    return True


def qualify_premarket_event(
    article_ledger: ArticleLedger,
    event_ledger: EventLedger,
    reference_ledger: ReferenceSnapshotLedger,
    security_id: str,
    target_session: date,
    calendar: TradingSessionCalendar,
    mode: str = "strict_primary",
) -> EventQualification:
    """Return E1/E2 eligibility using only records visible before the open."""

    if mode not in {"strict_primary", "agent_assisted_secondary"}:
        raise ValueError("unknown event mode")
    market_open = calendar.get(target_session).market_open
    previous_regular_close = calendar.previous(target_session).market_close
    cutoff = market_open - timedelta(microseconds=1)

    def make(
        status: str,
        variants: tuple[str, ...],
        record_ids: tuple[str, ...],
        reasons: tuple[str, ...],
    ) -> EventQualification:
        return EventQualification(
            security_id=security_id,
            window_start=previous_regular_close,
            cutoff=cutoff,
            mode=mode,
            status=status,
            eligible_variants=variants,
            event_record_ids=record_ids,
            reason_codes=reasons,
        )

    article_query = article_ledger.query(security_id, previous_regular_close, cutoff)
    if article_query.status == "COVERAGE_UNKNOWN":
        return make("COVERAGE_UNKNOWN", (), (), ("ABSTAIN_MISSING_COVERAGE",))
    if article_query.status == "ZERO_ARTICLE":
        return make("ZERO_ARTICLE_WITH_CONFIRMED_COVERAGE", (), (), ("NO_PREMARKET_EVENT",))

    # Arrival time determines what was observed. A delayed or revised old
    # publication is not an eligible new catalyst and cannot be silently
    # discarded just because another article qualifies as a fresh release.
    if any(
        not previous_regular_close < record.first_published_at < market_open
        for record in article_query.records
    ):
        return make(
            "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", (), (),
            ("ABSTAIN_OBSERVED_ARTICLE_OUTSIDE_EVENT_WINDOW",),
        )

    window_articles = tuple(
        record for record in article_ledger.active_as_of(security_id, cutoff)
        if previous_regular_close < record.first_published_at < market_open
        and record.effective_available_at < market_open
    )
    if not window_articles:
        return make(
            "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", (), (), ("ABSTAIN_RETRACTED_OR_OUTSIDE_EVENT_WINDOW",)
        )
    if any(record.normalized_category not in {"earnings", "guidance"} for record in window_articles):
        return make(
            "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", (), (), ("ABSTAIN_UNCLASSIFIED_OR_OTHER_ARTICLE",)
        )
    if any(
        record.link_method == "agent_inferred"
        or record.category_provenance != "deterministic_vendor_map"
        for record in window_articles
    ):
        return make(
            "ARTICLE_PRESENT_BUT_NOT_QUALIFIED", (), (),
            ("ABSTAIN_UNAUDITED_ARTICLE_LINK_OR_CATEGORY",),
        )
    # E1 needs only a point-in-time, deterministically categorized article.
    # Structured facts gate E2A/E2B and cannot retroactively invalidate E1.
    article_ids = {record.record_id for record in window_articles}
    article_by_id = {record.record_id: record for record in window_articles}
    active_all = [
        record for record in event_ledger.active_as_of(security_id, cutoff)
        if previous_regular_close < record.first_published_at < market_open
        and record.effective_available_at < market_open
        and set(record.input_article_record_ids).issubset(article_ids)
    ]
    if mode == "strict_primary":
        active = [record for record in active_all if record.provenance_mode in STRICT_PROVENANCE]
    else:
        active = [record for record in active_all if record.provenance_mode == "agent_extracted"]
    if not active:
        reason = "E1_ONLY_NO_STRUCTURED_EVENT"
        if mode == "strict_primary" and any(
            record.provenance_mode == "agent_extracted" for record in active_all
        ):
            reason = "E1_ONLY_AGENT_FACTS_EXCLUDED_FROM_STRICT"
        elif mode == "agent_assisted_secondary":
            reason = "E1_ONLY_NO_VERIFIED_AGENT_EVENT"
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), (), (reason,)
        )
    covered_article_ids = {
        input_id for record in active for input_id in record.input_article_record_ids
    }
    if covered_article_ids != article_ids:
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
            ("E1_ONLY_INCOMPLETE_STRUCTURED_ARTICLE_COVERAGE",),
        )
    for record in active:
        inputs = [article_by_id[input_id] for input_id in record.input_article_record_ids]
        invalid_binding = (
            record.first_published_at != min(item.first_published_at for item in inputs)
            or record.effective_available_at < max(item.effective_available_at for item in inputs)
        )
        if record.agent_run is not None:
            generated = parse_utc(
                str(record.agent_run["generated_at_utc"]), "agent_run.generated_at_utc"
            )
            invalid_binding = invalid_binding or generated < max(
                item.effective_available_at for item in inputs
            )
            verified_raw = record.agent_run.get("verified_at_utc")
            if verified_raw is not None:
                verified = parse_utc(str(verified_raw), "agent_run.verified_at_utc")
                invalid_binding = invalid_binding or verified < generated
        if invalid_binding:
            return make(
                "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
                ("E1_ONLY_EVENT_INPUT_BINDING_INVALID",),
            )
    by_key: dict[str, list[StructuredEventRecord]] = {}
    for record in active:
        by_key.setdefault(record.event_key, []).append(record)
    if any(len(records) != 1 for records in by_key.values()):
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
            ("E1_ONLY_CONFLICT",),
        )
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            same_natural_identity = (
                left.security_id == right.security_id
                and left.event_kind == right.event_kind
                and left.fiscal_period == right.fiscal_period
            )
            if same_natural_identity:
                return make(
                    "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
                    ("E1_ONLY_NATURAL_EVENT_CONFLICT",),
                )

    no_guidance_claim = any(
        record.facts.get("guidance_disposition") == "verified_not_issued"
        for record in active
    )
    issued_guidance_claim = any(bool(record.facts.get("guidance")) for record in active)
    if no_guidance_claim and issued_guidance_claim:
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
            ("E1_ONLY_CROSS_RECORD_GUIDANCE_CONFLICT",),
        )

    if any(record.verification_status != "verified" for record in active):
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in active),
            ("E1_ONLY_UNVERIFIED_OR_CONFLICTED_FACTS",),
        )

    accepted: list[StructuredEventRecord] = []
    for records in by_key.values():
        record = records[0]
        accepted.append(record)

    unsafe_guidance = any(_has_unsafe_or_inconsistent_guidance(record) for record in accepted)
    if unsafe_guidance:
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in accepted),
            ("E1_ONLY_GUIDANCE_NOT_CLEAR",),
        )

    try:
        for record in accepted:
            reference_ledger.validate_event(record)
    except ValueError:
        return make(
            "QUALIFIED_EVENT_E1_ONLY", ("E1",), tuple(item.record_id for item in accepted),
            ("E1_ONLY_UNVERIFIED_REFERENCE_SNAPSHOT",),
        )

    earnings_records = [record for record in accepted if record.event_kind == "earnings"]
    all_earnings_double_beat = bool(earnings_records) and all(
        _earnings_double_beat(record) is True for record in earnings_records
    )
    variants = ["E1"]
    if all_earnings_double_beat:
        variants.append("E2A")
    fiscal_year_guidance_records = [
        record for record in accepted
        if any(
            isinstance(item, Mapping)
            and isinstance(item.get("current"), Mapping)
            and item["current"].get("period_type") == "fiscal_year"
            for item in record.facts.get("guidance", ())
        )
    ]
    fiscal_year_directions = [
        _guidance_directions(record, require_fiscal_year=True)
        for record in fiscal_year_guidance_records
    ]
    if (
        fiscal_year_directions
        and all(item is not None for item in fiscal_year_directions)
    ):
        merged_directions = [
            direction
            for item in fiscal_year_directions
            if item is not None
            for direction in item
        ]
    else:
        merged_directions = []
    if merged_directions and any(value > 0 for value in merged_directions) and all(
        value >= 0 for value in merged_directions
    ):
        variants.append("E2B")
    if len(variants) == 1:
        return make(
            "QUALIFIED_EVENT_E1_ONLY",
            ("E1",),
            tuple(record.record_id for record in accepted),
            ("E1_ONLY_NOT_POSITIVE",),
        )
    return make(
        "QUALIFIED_EVENT",
        tuple(variants),
        tuple(record.record_id for record in accepted),
        tuple(f"ELIGIBLE_{variant}" for variant in variants),
    )
