#!/usr/bin/env python3
"""Verify, package, or install the reviewed files without external dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "scenario-router-research"
SKILL_PREFIX = f"skills/{SKILL_NAME}/"
VERSION = "0.1.0"


class ShareError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ShareError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_relative(raw):
    value = PurePosixPath(raw)
    if not raw or value.is_absolute() or any(p in ("", ".", "..") for p in raw.split("/")) or "\\" in raw or ":" in raw:
        raise ShareError(f"Unsafe manifest path: {raw!r}")
    return value


def source_file(base, raw):
    relative = safe_relative(raw)
    path = base
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ShareError(f"Symlink is not a release file: {raw}")
    if not path.is_file():
        raise ShareError(f"Missing release file: {raw}")
    return path


def manifest_entries(base):
    manifest = source_file(base, "MANIFEST.sha256")
    entries = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ShareError("Invalid manifest line")
        checksum, name = parts
        safe_relative(name)
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ShareError(f"Invalid SHA256 for {name}")
        if name == "MANIFEST.sha256" or name in entries:
            raise ShareError(f"Repeated or self-referencing manifest entry: {name}")
        source_file(base, name)
        entries[name] = checksum
    if not entries:
        raise ShareError("Empty manifest")
    return entries


def verify_manifest(base):
    entries = manifest_entries(base)
    mismatches = [name for name, expected in entries.items() if digest(source_file(base, name)) != expected]
    if mismatches:
        raise ShareError("Integrity mismatch: " + ", ".join(mismatches))
    return entries


def verify():
    root_entries = verify_manifest(ROOT)
    skill = ROOT / SKILL_PREFIX
    skill_entries = verify_manifest(skill)
    if {SKILL_PREFIX + name for name in skill_entries} | {SKILL_PREFIX + "MANIFEST.sha256"} != {
        name for name in root_entries if name.startswith(SKILL_PREFIX)
    }:
        raise ShareError("Repository and Skill manifests disagree")
    runtime_entries = verify_manifest(skill / "runtime")
    if {"runtime/" + name for name in runtime_entries} | {"runtime/MANIFEST.sha256"} != {
        name for name in skill_entries if name.startswith("runtime/")
    }:
        raise ShareError("Runtime and Skill manifests disagree")
    provenance = json.loads(source_file(ROOT, "SOURCE_PROVENANCE.json").read_text(encoding="utf-8"))
    if digest(skill / "runtime/MANIFEST.sha256") != provenance.get("source_manifest_sha256"):
        raise ShareError("Runtime snapshot differs from the recorded source provenance")
    return root_entries, skill_entries, runtime_entries


def write_manifest(base, names):
    lines = [f"{digest(source_file(base, name))}  {name}\n" for name in sorted(names)]
    destination = base / "MANIFEST.sha256"
    if destination.is_symlink():
        raise ShareError("Refusing to write a manifest through a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=base)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("".join(lines))
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh_manifest():
    """Maintainer action: only staged/tracked files can become release inputs."""
    for base in (ROOT, ROOT / SKILL_PREFIX):
        if (base / "MANIFEST.sha256").is_symlink():
            raise ShareError("Refusing to write a manifest through a symlink")
    try:
        result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT,
                                capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShareError("manifest requires a Git checkout with the intended files staged") from exc
    names = result.stdout.decode("utf-8").split("\0")
    names = {name for name in names if name and name not in ("MANIFEST.sha256", SKILL_PREFIX + "MANIFEST.sha256")}
    if not names or SKILL_PREFIX + "SKILL.md" not in names:
        raise ShareError("Stage the intended source files before updating manifests")
    blocked = {".git", "__pycache__", ".venv", "dist", "results", "optional_validation_data", "node_modules"}
    runtime_entries = verify_manifest(ROOT / SKILL_PREFIX / "runtime")
    provenance = json.loads(source_file(ROOT, "SOURCE_PROVENANCE.json").read_text(encoding="utf-8"))
    if digest(ROOT / SKILL_PREFIX / "runtime/MANIFEST.sha256") != provenance.get("source_manifest_sha256"):
        raise ShareError("Runtime snapshot differs from the recorded source provenance")
    expected_runtime = {"runtime/" + name for name in runtime_entries} | {"runtime/MANIFEST.sha256"}
    skill_names = {name[len(SKILL_PREFIX):] for name in names if name.startswith(SKILL_PREFIX)}
    if {name for name in skill_names if name.startswith("runtime/")} != expected_runtime:
        raise ShareError("Runtime files differ from the reviewed original manifest")
    for name in names:
        path = source_file(ROOT, name)
        if blocked.intersection(PurePosixPath(name).parts) or path.name.startswith(".env") or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}:
            raise ShareError(f"Runtime/private file cannot enter release manifest: {name}")
    write_manifest(ROOT / SKILL_PREFIX, skill_names)
    names.add(SKILL_PREFIX + "MANIFEST.sha256")
    write_manifest(ROOT, names)
    verify()
    return {"repository_files": len(names), "skill_files": len(skill_names), "review_required": True}


def archive_bytes(files, expected, destination, prefix):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, path in sorted(files.items()):
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected[relative]:
                raise ShareError(f"Source changed during archive creation: {relative}")
            info = zipfile.ZipInfo(prefix + "/" + relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    # Validate the embedded manifests against the exact archived bytes too.
    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.endswith("/MANIFEST.sha256"):
                base = name.rsplit("/", 1)[0]
                for line in archive.read(name).decode("utf-8").splitlines():
                    checksum, relative = line.split("  ", 1)
                    safe_relative(relative)
                    if hashlib.sha256(archive.read(base + "/" + relative)).hexdigest() != checksum:
                        raise ShareError(f"Archive manifest mismatch: {name}")


def build(output):
    root_entries, skill_entries, _ = verify()
    output = Path(output).expanduser().resolve()
    if output == ROOT or output == (ROOT / SKILL_PREFIX).resolve():
        raise ShareError("Use a separate output directory, for example dist")
    output.mkdir(parents=True, exist_ok=True)
    names = [f"{SKILL_NAME}-{VERSION}.zip", f"secondary-market-research-kit-{VERSION}.zip", "SHA256SUMS"]
    if any((output / name).exists() or (output / name).is_symlink() for name in names):
        raise ShareError("Release output already exists; select a new output directory")
    # Temporary staging makes a failed build leave no half-written archives.
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as temporary:
        staging = Path(temporary)
        skill = ROOT / SKILL_PREFIX
        skill_files = {name: source_file(skill, name) for name in skill_entries}
        skill_files["MANIFEST.sha256"] = source_file(skill, "MANIFEST.sha256")
        root_files = {name: source_file(ROOT, name) for name in root_entries}
        root_files["MANIFEST.sha256"] = source_file(ROOT, "MANIFEST.sha256")
        archive_bytes(skill_files, {**skill_entries, "MANIFEST.sha256": digest(skill / "MANIFEST.sha256")}, staging / names[0], SKILL_NAME)
        archive_bytes(root_files, {**root_entries, "MANIFEST.sha256": digest(ROOT / "MANIFEST.sha256")}, staging / names[1], f"secondary-market-research-kit-{VERSION}")
        (staging / "SHA256SUMS").write_text("".join(f"{digest(staging / name)}  {name}\n" for name in names[:2]), encoding="utf-8")
        created = []
        try:
            for name in names:
                # Exclusive creation also protects against another simultaneous build.
                with (output / name).open("xb") as target:
                    created.append(output / name)
                    target.write((staging / name).read_bytes())
        except Exception:
            for path in created:
                path.unlink()
            raise
    return {"version": VERSION, "artifacts": [{"path": str(output / name), "sha256": digest(output / name)} for name in names]}


def install(project, client):
    _, skill_entries, _ = verify()
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ShareError("--project must be an existing project directory")
    relative_parent = Path(".agents" if client == "codex" else ".claude") / "skills"
    parent = project
    for part in relative_parent.parts:
        parent = parent / part
        if parent.is_symlink():
            raise ShareError("Installation parent is a symlink; choose a regular project directory")
    destination = parent / SKILL_NAME
    if destination.exists() or destination.is_symlink():
        raise ShareError("Skill already exists; choose a different project or review/remove the previous installation manually")
    parent.mkdir(parents=True, exist_ok=True)
    skill = ROOT / SKILL_PREFIX
    # Reserve the final directory exclusively. On failure remove only files created here.
    destination.mkdir()
    try:
        for name in sorted(set(skill_entries) | {"MANIFEST.sha256"}):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(source_file(skill, name).read_bytes())
        verify_manifest(destination)
    except Exception:
        shutil.rmtree(destination)
        raise
    return {"client": client, "destination": str(destination), "files": len(skill_entries) + 1,
            "next_command": [sys.executable, str(destination / "scripts/research.py"), "doctor"]}


def main(argv=None):
    command = "unknown"
    try:
        parser = Parser(description=__doc__)
        subparsers = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        subparsers.add_parser("verify", help="Verify reviewed file hashes; does not run strategy tests")
        subparsers.add_parser("manifest", help="Maintainer: regenerate manifests from staged/tracked source files")
        build_parser = subparsers.add_parser("build", help="Create Skill and source archives without overwriting")
        build_parser.add_argument("--output", default="dist")
        install_parser = subparsers.add_parser("install", help="Copy the complete Skill into one project")
        install_parser.add_argument("--project", required=True)
        install_parser.add_argument("--client", choices=("codex", "claude"), default="codex")
        args = parser.parse_args(argv)
        command = args.command
        if command == "verify":
            root, skill, runtime = verify()
            data = {"repository_files": len(root), "skill_files": len(skill), "original_runtime_files": len(runtime), "runtime_tests_executed": False}
        elif command == "manifest":
            data = refresh_manifest()
        elif command == "build":
            data = build(args.output)
        else:
            data = install(args.project, args.client)
        print(json.dumps({"schema_version": "1.0", "ok": True, "command": command, "data": data, "error": None}, ensure_ascii=False))
        return 0
    except (ShareError, OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({"schema_version": "1.0", "ok": False, "command": command, "data": None,
                          "error": {"code": "SHARE_ERROR", "message": str(exc), "hint": "Run --help; preserve existing outputs and select a new destination."}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
