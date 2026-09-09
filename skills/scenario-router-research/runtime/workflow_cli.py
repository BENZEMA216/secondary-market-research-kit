#!/usr/bin/env python3
"""User-facing local Agent dispatcher. No broker or account connection."""
import argparse
import json
from scenario_router.ai_workflow import AgentWorkflow, stamp
from scenario_router.model_provider import CodexExecProvider, DisabledProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["analyze", "correct", "status"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--bundle")
    parser.add_argument("--question", default="请核对最新材料，解释对事件延续策略的影响。")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--provider", choices=["codex", "off"], default="off")
    parser.add_argument("--output", help="Optional full raw evidence JSON; stdout remains readable")
    args = parser.parse_args()
    provider = CodexExecProvider() if args.provider == "codex" else DisabledProvider()
    flow = AgentWorkflow(args.database, provider)
    if args.command == "status":
        result = flow.inspect(args.conversation)
        result = {**result, "query_receipt": {"question": args.question, "at": stamp(),
                  "kind": "LATEST_SAVED_ANALYSIS", "new_model_analysis": False},
                  "message": "这是最新保存的分析状态，本次查询未重新调用模型。" + result["message"]}
    else:
        if not args.bundle or (args.command == "correct" and args.expected_revision is None):
            parser.error("analyze/correct requires --bundle; correct also requires --expected-revision")
        result = flow.analyze(args.conversation, args.bundle, question=args.question, expected_revision=args.expected_revision)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({k: result[k] for k in ("run_id", "revision", "state", "message", "e2b_screen", "query_receipt") if k in result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
