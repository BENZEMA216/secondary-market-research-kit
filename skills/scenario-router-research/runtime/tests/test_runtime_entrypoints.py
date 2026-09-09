from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import unittest

from scenario_router.portfolio_risk import RiskPolicy
from validate_runtime import check_paper

ROOT = Path(__file__).resolve().parents[1]


class RuntimeEntryPoints(unittest.TestCase):
    def test_frozen_risk_json_matches_runtime(self):
        self.assertEqual(asdict(RiskPolicy()), json.loads((ROOT / "frozen_risk_policy.json").read_text()))

    def test_actual_paper_cli_reconciles(self):
        result = check_paper()
        self.assertEqual(result["status"], "PASS_SYNTHETIC_PAPER")
        self.assertAlmostEqual(result["zero_cost_final_equity"], 100499.80)

    def test_agent_cli_exists_without_login(self):
        run = subprocess.run([sys.executable, "workflow_cli.py", "--help"], cwd=ROOT,
                             capture_output=True, text=True, check=True, timeout=10)
        self.assertIn("--expected-revision", run.stdout)


if __name__ == "__main__":
    unittest.main()
