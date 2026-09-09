#!/usr/bin/env python3
"""Validate externally produced event ledgers before a backtest consumes them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenario_router.events import (
    ArticleLedger,
    EventLedger,
    FeedCoverage,
    ReferenceSnapshotLedger,
    parse_utc,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, required=True)
    args = parser.parse_args()

    coverage = FeedCoverage.from_json(args.manifest)
    articles = ArticleLedger.from_jsonl(args.articles, coverage)
    events = EventLedger.from_jsonl(args.events)
    snapshots = ReferenceSnapshotLedger.from_jsonl(args.snapshots)
    article_by_id = {record.record_id: record for record in articles.records}
    article_ids = set(article_by_id)
    missing_inputs = sorted({
        input_id
        for event in events.records
        for input_id in event.input_article_record_ids
        if input_id not in article_ids
    })
    if missing_inputs:
        raise ValueError(f"structured events reference missing article records: {missing_inputs}")
    for event in events.records:
        inputs = [article_by_id[input_id] for input_id in event.input_article_record_ids]
        if any(article.security_id != event.security_id for article in inputs):
            raise ValueError(f"{event.record_id}: input article security identity does not match event")
        if event.first_published_at != min(article.first_published_at for article in inputs):
            raise ValueError(f"{event.record_id}: event publication is not bound to its earliest declared input")
        if event.effective_available_at < max(article.effective_available_at for article in inputs):
            raise ValueError(f"{event.record_id}: event became effective before a declared input was available")
        active_input_ids = {
            article.record_id for article in articles.active_as_of(event.security_id, event.effective_available_at)
        }
        if not set(event.input_article_record_ids).issubset(active_input_ids):
            raise ValueError(f"{event.record_id}: event references a superseded or retracted article revision")
        if event.agent_run is not None:
            generated_at = event.agent_run["generated_at_utc"]
            # Runtime parsing has already guaranteed canonical UTC strings.
            generated = parse_utc(str(generated_at), "agent_run.generated_at_utc")
            if generated < max(article.effective_available_at for article in inputs):
                raise ValueError(f"{event.record_id}: agent extraction predates an available input")
            verified_raw = event.agent_run.get("verified_at_utc")
            if verified_raw is not None:
                verified = parse_utc(str(verified_raw), "agent_run.verified_at_utc")
                if verified < generated:
                    raise ValueError(f"{event.record_id}: agent review predates extraction")
        snapshots.validate_event(event)
    print(json.dumps({
        "status": "PASS_CONTRACT_VALIDATION",
        "article_records": len(articles.records),
        "structured_event_records": len(events.records),
        "reference_snapshot_records": len(snapshots.records),
        "coverage_intervals": len(coverage.intervals),
        "note": "Hash shape and references passed; raw payload bytes must still be checked against the declared hashes upstream.",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
