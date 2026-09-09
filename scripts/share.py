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
SKILL_NAME = "scenario-router-research"  # Default preserves the v0.1 installer contract.
SKILLS = (SKILL_NAME, "equity-research-toolkit", "market-data-toolkit")
ENTRY_POINTS = {SKILL_NAME: "scripts/research.py", "equity-research-toolkit": "scripts/equity.py", "market-data-toolkit": "scripts/tools.py"}
VERSION = "0.2.0"


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
    skills, runtimes = {}, {}
    for name in SKILLS:
        prefix = f"skills/{name}/"
        skill = ROOT / prefix
        entries = verify_manifest(skill)
        if {prefix + item for item in entries} | {prefix + "MANIFEST.sha256"} != {
            item for item in root_entries if item.startswith(prefix)
        }:
            raise ShareError(f"Repository and Skill manifests disagree: {name}")
        runtimes[name] = verify_runtime(name, entries)
        skills[name] = entries
    return root_entries, skills, runtimes


def verify_runtime(name, skill_entries):
    skill = ROOT / "skills" / name
    entries = verify_manifest(skill / "runtime")
    if {"runtime/" + item for item in entries} | {"runtime/MANIFEST.sha256"} != {
        item for item in skill_entries if item.startswith("runtime/")
    }:
        raise ShareError(f"Runtime and Skill manifests disagree: {name}")
    provenance_root = ROOT if name == SKILL_NAME else skill
    provenance = json.loads(source_file(provenance_root, "SOURCE_PROVENANCE.json").read_text(encoding="utf-8"))
    if digest(skill / "runtime/MANIFEST.sha256") != provenance.get("source_manifest_sha256"):
        raise ShareError(f"Runtime snapshot differs from the recorded source provenance: {name}")
    return entries


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
    for base in (ROOT, *(ROOT / "skills" / name for name in SKILLS)):
        if (base / "MANIFEST.sha256").is_symlink():
            raise ShareError("Refusing to write a manifest through a symlink")
    try:
        result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT,
                                capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShareError("manifest requires a Git checkout with the intended files staged") from exc
    names = result.stdout.decode("utf-8").split("\0")
    generated = {"MANIFEST.sha256"} | {f"skills/{name}/MANIFEST.sha256" for name in SKILLS}
    names = {name for name in names if name and name not in generated}
    if not names or not all(f"skills/{name}/SKILL.md" in names for name in SKILLS):
        raise ShareError("Stage the intended source files before updating manifests")
    blocked = {".git", "__pycache__", ".venv", "dist", "results", "output", "test_output", "optional_validation_data", "node_modules"}
    all_skill_names = {}
    for skill_name in SKILLS:
        prefix = f"skills/{skill_name}/"
        skill_names = {name[len(prefix):] for name in names if name.startswith(prefix)}
        verify_runtime(skill_name, skill_names)
        all_skill_names[skill_name] = skill_names
    for name in names:
        path = source_file(ROOT, name)
        if blocked.intersection(PurePosixPath(name).parts) or path.name in {"config.ini", "OAI_CONFIG_LIST"} or path.name.startswith(".env") or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".ipynb"}:
            raise ShareError(f"Runtime/private file cannot enter release manifest: {name}")
    for skill_name, skill_names in all_skill_names.items():
        write_manifest(ROOT / "skills" / skill_name, skill_names)
        names.add(f"skills/{skill_name}/MANIFEST.sha256")
    write_manifest(ROOT, names)
    verify()
    return {"repository_files": len(names), "skill_files": {name: len(items) for name, items in all_skill_names.items()}, "review_required": True}


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
    root_entries, all_skill_entries, _ = verify()
    output = Path(output).expanduser().resolve()
    if output == ROOT or any(output == (ROOT / "skills" / name).resolve() for name in SKILLS):
        raise ShareError("Use a separate output directory, for example dist")
    output.mkdir(parents=True, exist_ok=True)
    names = [f"{name}-{VERSION}.zip" for name in SKILLS] + [f"secondary-market-research-kit-{VERSION}.zip", "SHA256SUMS"]
    if any((output / name).exists() or (output / name).is_symlink() for name in names):
        raise ShareError("Release output already exists; select a new output directory")
    # Temporary staging makes a failed build leave no half-written archives.
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as temporary:
        staging = Path(temporary)
        for skill_name, skill_entries in all_skill_entries.items():
            skill = ROOT / "skills" / skill_name
            skill_files = {name: source_file(skill, name) for name in skill_entries}
            skill_files["MANIFEST.sha256"] = source_file(skill, "MANIFEST.sha256")
            archive_bytes(skill_files, {**skill_entries, "MANIFEST.sha256": root_entries[f"skills/{skill_name}/MANIFEST.sha256"]}, staging / f"{skill_name}-{VERSION}.zip", skill_name)
        root_files = {name: source_file(ROOT, name) for name in root_entries}
        root_files["MANIFEST.sha256"] = source_file(ROOT, "MANIFEST.sha256")
        archive_bytes(root_files, {**root_entries, "MANIFEST.sha256": digest(ROOT / "MANIFEST.sha256")}, staging / names[-2], f"secondary-market-research-kit-{VERSION}")
        (staging / "SHA256SUMS").write_text("".join(f"{digest(staging / name)}  {name}\n" for name in names[:-1]), encoding="utf-8")
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


def install(project, client, selected=SKILL_NAME):
    root_entries, all_skill_entries, _ = verify()
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ShareError("--project must be an existing project directory")
    relative_parent = Path(".agents" if client == "codex" else ".claude") / "skills"
    parent = project
    for part in relative_parent.parts:
        parent = parent / part
        if parent.is_symlink():
            raise ShareError("Installation parent is a symlink; choose a regular project directory")
    selected_names = SKILLS if selected == "all" else (selected,)
    for name in selected_names:
        destination = parent / name
        if destination.exists() or destination.is_symlink():
            raise ShareError(f"Skill already exists: {name}; review/remove the previous installation manually")
    parent.mkdir(parents=True, exist_ok=True)
    created, installations = [], []
    try:
        for skill_name in selected_names:
            destination = parent / skill_name
            destination.mkdir()  # Exclusive reservation; never delete another install.
            created.append(destination)
            skill, entries = ROOT / "skills" / skill_name, all_skill_entries[skill_name]
            expected = {**entries, "MANIFEST.sha256": root_entries[f"skills/{skill_name}/MANIFEST.sha256"]}
            for name in sorted(set(entries) | {"MANIFEST.sha256"}):
                payload = source_file(skill, name).read_bytes()
                if hashlib.sha256(payload).hexdigest() != expected[name]:
                    raise ShareError(f"Source changed during installation: {skill_name}/{name}")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(payload)
            verify_manifest(destination)
            installations.append({"skill": skill_name, "destination": str(destination), "files": len(entries) + 1,
                                  "next_command": [sys.executable, str(destination / ENTRY_POINTS[skill_name]), "doctor"]})
    except Exception:
        for destination in created:
            shutil.rmtree(destination)
        raise
    return {"client": client, "installations": installations, **(installations[0] if len(installations) == 1 else {})}


def main(argv=None):
    command = "unknown"
    try:
        parser = Parser(description=__doc__)
        subparsers = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        subparsers.add_parser("verify", help="Verify reviewed file hashes; does not run strategy tests")
        subparsers.add_parser("manifest", help="Maintainer: regenerate manifests from staged/tracked source files")
        build_parser = subparsers.add_parser("build", help="Create Skill and source archives without overwriting")
        build_parser.add_argument("--output", default=f"dist/{VERSION}")
        install_parser = subparsers.add_parser("install", help="Copy the complete Skill into one project")
        install_parser.add_argument("--project", required=True)
        install_parser.add_argument("--client", choices=("codex", "claude"), default="codex")
        install_parser.add_argument("--skill", choices=(*SKILLS, "all"), default=SKILL_NAME)
        args = parser.parse_args(argv)
        command = args.command
        if command == "verify":
            root, skill, runtime = verify()
            data = {"repository_files": len(root), "skill_files": {name: len(items) for name, items in skill.items()},
                    "runtime_files": {name: len(items) for name, items in runtime.items()}, "runtime_tests_executed": False}
        elif command == "manifest":
            data = refresh_manifest()
        elif command == "build":
            data = build(args.output)
        else:
            data = install(args.project, args.client, args.skill)
        print(json.dumps({"schema_version": "1.0", "ok": True, "command": command, "data": data, "error": None}, ensure_ascii=False))
        return 0
    except (ShareError, OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({"schema_version": "1.0", "ok": False, "command": command, "data": None,
                          "error": {"code": "SHARE_ERROR", "message": str(exc), "hint": "Run --help; preserve existing outputs and select a new destination."}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
