"""Cross-Skill routing and complete project installation contracts."""
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = {"scenario-router-research": "scripts/research.py", "equity-research-toolkit": "scripts/equity.py", "market-data-toolkit": "scripts/tools.py"}


class ToolkitTests(unittest.TestCase):
    def call(self, script, *arguments, cwd=None):
        result = subprocess.run([sys.executable, str(ROOT / script), *arguments], cwd=cwd,
                                text=True, capture_output=True, timeout=45)
        return result.returncode, json.loads(result.stdout)

    def test_catalog_and_describe_work_without_external_services(self):
        with tempfile.TemporaryDirectory(prefix="toolkit 项目 ") as temporary:
            code, payload = self.call("scripts/toolkit.py", "list", cwd=temporary)
            self.assertEqual(code, 0, payload)
            self.assertEqual({x["name"] for x in payload["data"]["toolkits"]}, set(SKILLS))
            for name in SKILLS:
                code, payload = self.call("scripts/toolkit.py", "describe", name, cwd=temporary)
                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["ok"])

    def test_child_error_propagates_without_success_wrapper(self):
        code, payload = self.call("scripts/toolkit.py", "run", "scenario-router-research", "--", "backtest")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIsNotNone(payload["error"])

    def test_unknown_toolkit_and_empty_run_are_json_errors(self):
        for args in (("describe", "not-a-toolkit"), ("run", "scenario-router-research")):
            code, payload = self.call("scripts/toolkit.py", *args)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])

    def test_dispatch_rejects_catalog_escape_and_normalizes_failed_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/toolkit.py", root / "scripts/toolkit.py")
            entry = root / "skills/test-toolkit/entry.py"
            entry.parent.mkdir(parents=True)
            entry.write_text('print(\'{"ok":false,"error":{"code":"FIXTURE_FAILURE"}}\')\n')
            for entry_name, expected_error in ((str(entry), "TOOLKIT_ERROR"), ("entry.py", "FIXTURE_FAILURE")):
                (root / "catalog.json").write_text(json.dumps({"toolkits": [{"name": "test-toolkit", "entry": entry_name}]}))
                result = subprocess.run([sys.executable, str(root / "scripts/toolkit.py"), "describe", "test-toolkit"], capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], expected_error)

    def test_install_all_is_portable_and_conflicts_are_preflighted(self):
        with tempfile.TemporaryDirectory(prefix="all skills 项目 ") as temporary:
            project = Path(temporary)
            code, payload = self.call("scripts/share.py", "install", "--project", str(project), "--skill", "all")
            self.assertEqual(code, 0, payload)
            self.assertEqual(len(payload["data"]["installations"]), 3)
            for name, entry in SKILLS.items():
                path = project / ".agents/skills" / name / entry
                result = subprocess.run([sys.executable, str(path), "describe"], cwd=temporary, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(json.loads(result.stdout)["ok"])
        with tempfile.TemporaryDirectory(prefix="conflicting skills ") as temporary:
            project = Path(temporary)
            old = project / ".agents/skills/market-data-toolkit"
            old.mkdir(parents=True)
            (old / "user.txt").write_text("keep me")
            code, payload = self.call("scripts/share.py", "install", "--project", str(project), "--skill", "all")
            self.assertEqual(code, 2, payload)
            self.assertEqual((old / "user.txt").read_text(), "keep me")
            self.assertFalse((old.parent / "scenario-router-research").exists())
            self.assertFalse((old.parent / "equity-research-toolkit").exists())


if __name__ == "__main__":
    unittest.main()
