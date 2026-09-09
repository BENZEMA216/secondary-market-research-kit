#!/usr/bin/env python3
"""Repeatable local runtime checks. Never calls a real model or market/account API.

Run validate.py for the complete unit suite and contract checks. This entry
exercises the paper dispatcher and, when supplied, an optional evidence bundle.
Live AI evidence is deliberately not promoted from unit-test substitutes.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from scenario_router import __version__
from scenario_router.ai_workflow import AgentWorkflow, digest
from scenario_router.model_provider import DisabledProvider
from scenario_router.portfolio_risk import RiskPolicy
from validate import package_manifest

ROOT = Path(__file__).resolve().parent


def check_paper():
    run = subprocess.run([sys.executable, str(ROOT / "examples/run_paper_example.py")],
                         cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(run.stdout)
    zero, cost = result["zero_cost"], result["illustrative_cost"]
    assert abs(zero["cash"] - 100499.80) < 1e-7
    assert len(zero["fills"]) == 3 and not zero["positions"]
    assert cost["fees_paid"] == 3 and cost["slippage_paid"] > 0
    assert abs(cost["cash"] - (100000 + cost["realized_gross_pnl"] - cost["fees_paid"])) < 1e-7
    return {"status": "PASS_SYNTHETIC_PAPER", "zero_cost_final_equity": zero["cash"],
            "cost_case_final_equity": cost["cash"], "cost_case_fees": cost["fees_paid"],
            "cost_case_extra_slippage": cost["slippage_paid"], "raw": result}


def check_provider_off():
    with tempfile.TemporaryDirectory(prefix="ai-provider-off-") as temp:
        directory = Path(temp)
        sources = []
        for source_id, role, published_at, content in (
            ("prior", "prior", "2025-01-01T00:00:00Z", "FY2026 revenue guidance: 100 to 110 million USD."),
            ("current", "current", "2025-02-01T00:00:00Z", "FY2026 revenue guidance: 110 to 120 million USD."),
        ):
            path = directory / f"{source_id}.txt"
            path.write_text(content, encoding="utf-8")
            sources.append({
                "source_id": source_id,
                "role": role,
                "path": path.name,
                "sha256": digest(content.encode()),
                "url": "https://example.invalid/synthetic",
                "published_at": published_at,
                "captured_at": "2025-02-02T00:00:00Z",
            })
        bundle_path = directory / "bundle.json"
        bundle_path.write_text(json.dumps({
            "bundle_id": "synthetic-provider-off",
            "security_id": "TEST:EXAMPLE",
            "ticker": "TEST",
            "fiscal_period": "FY2026",
            "lane": "synthetic",
            "sources": sources,
        }), encoding="utf-8")
        result = AgentWorkflow(directory / "jobs.sqlite", DisabledProvider()).analyze(
            "provider-off-check", bundle_path, question="核对指引")
        assert result["state"] == "BLOCKED" and result["reason"] == "MODEL_PROVIDER_DISABLED"
        return {"status": "PASS_PROVIDER_OFF", "model_called": False}


def main():
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": __version__,
              "status": "FAIL", "checks": {}, "real_model_called_this_validation": False,
              "profitability_verified": False, "live_orders": False}
    stage = "frozen_risk_policy"
    try:
        frozen = json.loads((ROOT / "frozen_risk_policy.json").read_text())
        assert frozen == asdict(RiskPolicy()), "risk policy drift"
        result["checks"][stage] = "PASS"
        stage = "paper_end_to_end"
        result["checks"][stage] = check_paper()
        stage = "provider_off"
        result["checks"][stage] = check_provider_off()
        result["status"] = "PASS_LOCAL_RUNTIME_CHECKS"
    except Exception as exc:
        result["checks"][stage] = {"status": "FAIL", "error": type(exc).__name__, "message": str(exc)}
    result["package_sha256"] = package_manifest()
    result["scope"] = "Paper accounting/risk and disabled-provider checks only. No live model, return backtest, OOS or production certification in this command."
    path = ROOT / "results/validation_runtime_report.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({"status": result["status"], "report": str(path), "checks": list(result["checks"])}, ensure_ascii=False))
    return 0 if result["status"] == "PASS_LOCAL_RUNTIME_CHECKS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
