#!/usr/bin/env python3
"""Freeze local architecture evidence, then detect byte drift.

Run ``snapshot`` before updating the diagram. Use the returned temporary Git
checkout as Archify's --repo-root; the main checkout is never committed.
Run ``seal`` only after successful Archify finalization and review. ``check``
checks hashes, not whether the diagram correctly explains the source.

The source-snapshot.bundle can restore the evidence checkout elsewhere:
    git clone /absolute/path/source-snapshot.bundle /tmp/architecture-evidence
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


FORMAT_VERSION = 1
FIXED_SOURCES = (
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "hooks/hooks.json",
    "pyproject.toml",
    "scripts/codex-mem.py",
    "docs/PROJECT_MEMORY.md",
    "docs/PI_MCP.md",
    "docs/EXECUTION.md",
    "tests/test_observation_profiles.py",
    "tests/test_project_memory_profiles.py",
    "docs/pi-mcp.example.json",
    "tests/test_pi_mcp.py",
    "tests/test_mcp.py",
)
DOCUMENTS = ("candidate.json", "codex-mem.html")
HERE = Path(__file__).resolve().parent


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path.name}")
    return value


def write_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(root: Path, *arguments: str) -> bytes:
    # Avoid global hooks, includes and identity when creating the isolated commit.
    environment = dict(os.environ)
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for key in tuple(environment):
        if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}:
            environment.pop(key)
    process = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *arguments],
        capture_output=True,
        env=environment,
        check=False,
    )
    if process.returncode:
        # Do not echo arbitrary Git output, which may contain an origin credential.
        raise RuntimeError(f"Git command failed: {arguments[0]} (exit {process.returncode})")
    return process.stdout


def sanitized_origin(root: Path) -> str:
    origin = git(root, "remote", "get-url", "origin").decode().strip()
    if re.match(r"^(?:[^/@:]+@)?[^/:]+:[^/].*", origin):
        host, repository_path = origin.split(":", 1)
        origin = f"ssh://{host.rsplit('@', 1)[-1]}/{repository_path}"
    parsed = urlsplit(origin)
    if parsed.scheme in {"https", "http", "ssh", "git"} and parsed.hostname:
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if parsed.scheme == "file":
        return urlunsplit(("file", "", parsed.path, "", ""))
    if not parsed.scheme and origin:
        return (root / origin).resolve().as_uri()
    raise ValueError("Origin URL is missing or unsupported; no credential-bearing URL was saved")


def source_bytes(root: Path, allow_missing: bool = False) -> dict[str, bytes]:
    paths = set(FIXED_SOURCES)
    paths.update(path.relative_to(root).as_posix() for path in (root / "codex_mem").rglob("*.py"))
    if not allow_missing and not any(path.startswith("codex_mem/") for path in paths):
        raise ValueError("No codex_mem Python source files found")
    result = {}
    for relative in sorted(paths):
        path = root / relative
        if allow_missing and not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise ValueError(f"Missing, linked or outside-repository source: {relative}")
        result[relative] = path.read_bytes()
    return result


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {path: sha256(data) for path, data in files.items()}


def dirty_paths(root: Path) -> list[str]:
    # -z emits unquoted paths. A rename or copy carries a second path.
    entries = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    paths = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii")
        paths.add(entry[3:].decode("utf-8", "surrogateescape"))
        if "R" in status or "C" in status:
            if index < len(entries) and entries[index]:
                paths.add(entries[index].decode("utf-8", "surrogateescape"))
                index += 1
    return sorted(paths)


def write_sources(destination: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def snapshot(root: Path, folder: Path) -> dict:
    captured_at = now()
    source = source_bytes(root)
    source_hashes = hashes(source)
    base = git(root, "rev-parse", "HEAD").decode().strip()
    origin = sanitized_origin(root)
    dirty = dirty_paths(root)
    checkout = Path(tempfile.mkdtemp(prefix="codex-mem-architecture-evidence-"))
    try:
        write_sources(checkout, source)
        git(checkout, "init", "--quiet", "--template=")
        git(checkout, "symbolic-ref", "HEAD", "refs/heads/source-snapshot")
        git(checkout, "add", "--all")
        git(
            checkout,
            "-c", "user.name=Architecture evidence",
            "-c", "user.email=architecture-evidence@localhost",
            "-c", "commit.gpgSign=false",
            "commit", "--quiet", "-m", "Local working-tree evidence snapshot; not an upstream commit",
        )
        revision = git(checkout, "rev-parse", "HEAD").decode().strip()
        git(checkout, "remote", "add", "origin", origin)
        bundle_temporary = checkout / "source-snapshot.bundle"
        git(checkout, "bundle", "create", str(bundle_temporary), "--all")
        # Reject a source edit that happened while the snapshot was being built.
        if hashes(source_bytes(root)) != source_hashes:
            raise RuntimeError("Sources changed during capture; retry snapshot")
        staging = Path(tempfile.mkdtemp(prefix=".source-snapshot-", dir=folder))
        try:
            write_sources(staging, source)
            target = folder / "source-snapshot"
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise ValueError("source-snapshot must be a generated directory")
                shutil.rmtree(target)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        bundle = folder / "source-snapshot.bundle"
        shutil.copyfile(bundle_temporary, bundle)
        manifest = {
            "format_version": FORMAT_VERSION,
            "provenance": "local_working_tree_snapshot_not_upstream_commit",
            "captured_at": captured_at,
            "main_repository": {"url": origin, "base_revision": base, "dirty_paths": dirty},
            "snapshot_repository": {"url": origin, "revision": revision, "link_mode": "local-only"},
            "source_selection": {"glob": "codex_mem/**/*.py", "fixed_paths": list(FIXED_SOURCES)},
            "files": source_hashes,
            "bundle": {"path": bundle.name, "sha256": sha256(bundle.read_bytes())},
            "limits": "Byte provenance only; no semantic freshness or completeness guarantee.",
        }
        write_json(folder / "source-manifest.json", manifest)
        candidate_path = folder / "candidate.json"
        if candidate_path.exists():
            candidate = read_json(candidate_path)
            repository = candidate.setdefault("meta", {}).setdefault("repository", {})
            repository.update(manifest["snapshot_repository"])
            write_json(candidate_path, candidate)
        return {
            "status": "snapshot_created",
            "provenance": manifest["provenance"],
            "snapshot_repo_root": str(checkout),
            "snapshot_revision": revision,
            "main_base_revision": base,
            "source_files": len(source),
            "manifest": str(folder / "source-manifest.json"),
            "next": "Update source references, finalize using snapshot_repo_root, then seal after review.",
        }
    except Exception:
        shutil.rmtree(checkout, ignore_errors=True)
        raise


def differences(actual: dict[str, str], expected: dict[str, str]) -> dict:
    return {
        "added": sorted(actual.keys() - expected.keys()),
        "removed": sorted(expected.keys() - actual.keys()),
        "changed": sorted(path for path in actual.keys() & expected.keys() if actual[path] != expected[path]),
    }


def inspect(root: Path, folder: Path, require_seal: bool = True) -> dict:
    issues = []
    manifest_path = folder / "source-manifest.json"
    if not manifest_path.is_file():
        return {"status": "stale", "issues": ["source_manifest_missing"], "semantic_freshness": "not_checked"}
    manifest = read_json(manifest_path)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("Unsupported source manifest version")
    expected = manifest["files"]
    source_difference = differences(hashes(source_bytes(root, allow_missing=True)), expected)
    if any(source_difference.values()):
        issues.append("source_bytes_drift")
    stored_difference = differences(hashes(source_bytes(folder / "source-snapshot", allow_missing=True)), expected)
    if any(stored_difference.values()):
        issues.append("snapshot_copy_drift")
    bundle = folder / "source-snapshot.bundle"
    if not bundle.is_file() or sha256(bundle.read_bytes()) != manifest["bundle"]["sha256"]:
        issues.append("snapshot_bundle_drift")
    candidate_path = folder / "candidate.json"
    if not candidate_path.is_file():
        issues.append("candidate_missing")
    else:
        repository = read_json(candidate_path).get("meta", {}).get("repository", {})
        if any(repository.get(key) != value for key, value in manifest["snapshot_repository"].items()):
            issues.append("candidate_repository_drift")
    if not (folder / "codex-mem.html").is_file():
        issues.append("html_missing")
    seal_path = folder / "maintenance-seal.json"
    if require_seal:
        if not seal_path.is_file():
            issues.append("document_unsealed")
        else:
            seal = read_json(seal_path)
            for relative in (*DOCUMENTS, "source-manifest.json"):
                path = folder / relative
                if not path.is_file() or sha256(path.read_bytes()) != seal.get("files", {}).get(relative):
                    issues.append(f"sealed_file_drift:{relative}")
    return {
        "status": "hashes_match" if not issues else "stale",
        "issues": issues,
        "source_difference": source_difference,
        "snapshot_difference": stored_difference,
        "semantic_freshness": "not_checked",
        "limits": "Hash equality does not prove that architecture claims or line references are correct.",
    }


def seal(root: Path, folder: Path) -> dict:
    result = inspect(root, folder, require_seal=False)
    if result["issues"]:
        raise ValueError("Cannot seal: " + ", ".join(result["issues"]))
    value = {
        "format_version": FORMAT_VERSION,
        "sealed_at": now(),
        "files": {relative: sha256((folder / relative).read_bytes()) for relative in (*DOCUMENTS, "source-manifest.json")},
        "limits": "Records bytes only. Caller must complete Archify finalization and review before sealing.",
    }
    write_json(folder / "maintenance-seal.json", value)
    return {"status": "sealed", "seal": str(folder / "maintenance-seal.json"), "semantic_freshness": "not_checked"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("snapshot", "check", "seal"))
    parser.add_argument("--repo-root", type=Path, default=HERE.parent.parent)
    parser.add_argument("--document-dir", type=Path, default=HERE)
    arguments = parser.parse_args()
    root = arguments.repo_root.resolve()
    folder = arguments.document_dir.resolve()
    try:
        if not folder.is_dir():
            raise ValueError("Document directory must already exist")
        actions = {"snapshot": snapshot, "check": inspect, "seal": seal}
        result = actions[arguments.command](root, folder)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "stale" else 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
