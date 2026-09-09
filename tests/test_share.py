"""Release/installation boundaries tested against the real distributable Skill."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = "scenario-router-research"


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill release 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source repo"
        self.repo.mkdir()
        for line in (ROOT / "MANIFEST.sha256").read_text().splitlines():
            _, name = line.split("  ", 1)
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        shutil.copyfile(ROOT / "MANIFEST.sha256", self.repo / "MANIFEST.sha256")

    def call(self, *arguments):
        completed = subprocess.run([sys.executable, str(self.repo / "scripts/share.py"), *arguments],
                                   cwd=self.base, capture_output=True, text=True, timeout=30)
        payload = json.loads(completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)
        return completed.returncode, payload

    def test_portable_archives_and_project_installation(self):
        # Unreviewed runtime/private files must never become release inputs.
        (self.repo / ".env").write_text("PRIVATE_FIXTURE=do-not-share")
        (self.repo / "results").mkdir()
        (self.repo / "results/private.json").write_text("{}")
        (self.repo / "ignored-link").symlink_to(self.base)
        code, payload = self.call("build", "--output", "release output")
        self.assertEqual(code, 0, payload)
        output = self.base / "release output"
        with zipfile.ZipFile(output / f"{SKILL}-0.2.0.zip") as archive:
            self.assertIn(f"{SKILL}/SKILL.md", archive.namelist())
            self.assertIn(f"{SKILL}/scripts/research.py", archive.namelist())
            archive.extractall(self.base / "extracted")
        with zipfile.ZipFile(output / "secondary-market-research-kit-0.2.0.zip") as archive:
            self.assertFalse(any("/.env" in n or "/results/" in n or "ignored-link" in n or "/.git/" in n for n in archive.namelist()))
        for line in (output / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
        project = self.base / "new project 项目"
        project.mkdir()
        code, payload = self.call("install", "--project", str(project))
        self.assertEqual(code, 0, payload)
        installed = project / ".agents/skills" / SKILL
        self.assertTrue((installed / "runtime/scenario_router/engine.py").is_file())
        for entry in (installed, self.base / "extracted" / SKILL):
            for arguments in (("doctor",), ("demo", "--kind", "signal")):
                run = subprocess.run([sys.executable, str(entry / "scripts/research.py"), *arguments],
                                     cwd=self.base, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertTrue(json.loads(run.stdout)["ok"])
        self.assertFalse((project / ".claude").exists())

    def test_existing_installation_and_release_are_preserved(self):
        project = self.base / "project"
        destination = project / ".agents/skills" / SKILL
        destination.mkdir(parents=True)
        marker = destination / "keep.txt"
        marker.write_text("user content")
        code, _ = self.call("install", "--project", str(project))
        self.assertEqual(code, 2)
        self.assertEqual(marker.read_text(), "user content")
        self.assertEqual(list(destination.iterdir()), [marker])
        output = self.base / "release"
        output.mkdir()
        existing = output / "SHA256SUMS"
        existing.write_text("previous release")
        code, _ = self.call("build", "--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(existing.read_text(), "previous release")
        self.assertEqual(list(output.iterdir()), [existing])

    def test_tampered_source_cannot_be_packaged_or_installed(self):
        target = self.repo / "skills" / SKILL / "runtime/scenario_router/engine.py"
        target.write_text(target.read_text() + "\n# modified\n")
        project = self.base / "project"
        project.mkdir()
        for args in (("verify",), ("build", "--output", "release"), ("install", "--project", str(project))):
            code, payload = self.call(*args)
            self.assertEqual(code, 2)
            self.assertIn("Integrity mismatch", payload["error"]["message"])
        self.assertFalse((project / ".agents").exists())
        self.assertFalse((self.base / "release").exists())

    def test_symlinks_cannot_replace_release_files_or_install_parents(self):
        project = self.base / "project"
        project.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (project / ".agents").symlink_to(outside, target_is_directory=True)
        code, _ = self.call("install", "--project", str(project))
        self.assertEqual(code, 2)
        self.assertEqual(list(outside.iterdir()), [])
        target = self.repo / "README.md"
        original = target.read_bytes()
        target.unlink()
        external = outside / "README.md"
        external.write_bytes(original)
        target.symlink_to(external)
        code, payload = self.call("verify")
        self.assertEqual(code, 2)
        self.assertIn("Symlink", payload["error"]["message"])

    def test_manifest_traversal_and_duplicate_entries_are_rejected(self):
        manifest = self.repo / "MANIFEST.sha256"
        original = manifest.read_text()
        for suffix in ("0" * 64 + "  ../outside\n", "0" * 64 + "  D:/outside.txt\n", original.splitlines()[0] + "\n"):
            manifest.write_text(original + suffix)
            code, payload = self.call("build", "--output", "release")
            self.assertEqual(code, 2, payload)
            self.assertFalse((self.base / "release").exists())

    def test_manifest_refresh_cannot_follow_external_symlinks(self):
        sentinel = self.base / "external-sentinel"
        sentinel.write_text("external content must survive")
        for base in (self.repo, self.repo / "skills" / SKILL):
            manifest = base / "MANIFEST.sha256"
            original = manifest.read_bytes()
            manifest.unlink()
            manifest.symlink_to(sentinel)
            code, payload = self.call("manifest")
            self.assertEqual(code, 2, payload)
            self.assertIn("symlink", payload["error"]["message"])
            self.assertEqual(sentinel.read_text(), "external content must survive")
            manifest.unlink()
            manifest.write_bytes(original)

    def test_source_edit_during_build_does_not_publish_invalid_archives(self):
        spec = importlib.util.spec_from_file_location("share_under_test", self.repo / "scripts/share.py")
        share = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(share)
        original_archive = share.archive_bytes

        def edit_then_archive(files, expected, destination, prefix):
            if "README.md" in files:
                (self.repo / "README.md").write_text("concurrent edit")
            return original_archive(files, expected, destination, prefix)

        output = self.base / "release"
        with mock.patch.object(share, "archive_bytes", side_effect=edit_then_archive):
            with self.assertRaisesRegex(share.ShareError, "Source changed"):
                share.build(output)
        self.assertEqual(list(output.iterdir()), [])

    def test_concurrent_source_and_manifest_edit_cannot_change_install_snapshot(self):
        spec = importlib.util.spec_from_file_location("share_install_test", self.repo / "scripts/share.py")
        share = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(share)
        original_verify = share.verify

        def verify_then_edit():
            result = original_verify()
            skill = self.repo / "skills" / SKILL
            target = skill / "SKILL.md"
            target.write_text(target.read_text() + "\nconcurrent edit\n")
            share.write_manifest(skill, result[1][SKILL])
            return result

        project = self.base / "project"
        project.mkdir()
        with mock.patch.object(share, "verify", side_effect=verify_then_edit):
            with self.assertRaisesRegex(share.ShareError, "Source changed"):
                share.install(project, "codex")
        self.assertFalse((project / ".agents/skills" / SKILL).exists())

    def test_claude_install_is_project_scoped(self):
        project = self.base / "claude project"
        project.mkdir()
        code, payload = self.call("install", "--project", str(project), "--client", "claude")
        self.assertEqual(code, 0, payload)
        self.assertTrue((project / ".claude/skills" / SKILL / "SKILL.md").is_file())
        self.assertFalse((project / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
