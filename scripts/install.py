#!/usr/bin/env python3
"""Install this plugin in the personal Codex marketplace; preserve other entries."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

NAME = "codex-mem"
ROOT = Path(__file__).resolve().parents[1]
FILES = (".codex-plugin", ".mcp.json", "codex_mem", "hooks", "skills", "scripts",
         "tests", "docs", "assets", "README.md", "README.ru.md", "UPSTREAM.md", "LICENSE", "pyproject.toml")
MARKER = ".codex-mem-managed"
_CACHE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
MAX_CACHE_PACKAGES = 32
MAX_CACHE_BACKUP_BYTES = 128 * 1024 * 1024


def manifest(root: Path) -> dict:
    result = json.loads((root / ".codex-plugin/plugin.json").read_text())
    if result.get("name") != NAME or not result.get("version"):
        raise ValueError("Source does not contain a versioned codex-mem plugin")
    for name in (".mcp.json", "hooks/hooks.json", "scripts/codex-mem.py"):
        if not (root / name).is_file():
            raise ValueError("Source plugin is incomplete")
    return result


def registry(path: Path) -> dict:
    if not path.exists():
        return {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", result.get("name", "")):
        raise ValueError("Existing personal marketplace has an invalid name")
    if not isinstance(result.get("plugins"), list) or any(not isinstance(p, dict) for p in result["plugins"]):
        raise ValueError("Existing personal marketplace has an invalid plugins list")
    return result


def write_json(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".codex-mem-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _assert_tree_has_no_symlinks(root: Path) -> None:
    if root.is_symlink() or any(item.is_symlink() for item in root.rglob("*")):
        raise ValueError("Managed source cannot contain symlinks")


def _cache_root(user_home: Path, marketplace_name: str) -> Path:
    """Return the only cache namespace this installer may retain."""

    cache_root = user_home / ".codex" / "plugins" / "cache" / marketplace_name / NAME
    for path in (
        user_home / ".codex",
        user_home / ".codex" / "plugins",
        user_home / ".codex" / "plugins" / "cache",
        user_home / ".codex" / "plugins" / "cache" / marketplace_name,
        cache_root,
    ):
        if path.is_symlink():
            raise ValueError("Codex Mem cache path cannot be a symlink")
    return cache_root


def _assert_cache_ancestors(cache_root: Path) -> None:
    """Recheck the cache path after Codex has had a chance to modify it."""

    path = cache_root
    while path != path.parent:
        if path.is_symlink():
            raise ValueError("Codex Mem cache path cannot be a symlink")
        path = path.parent


def _previous_cache_destination(previous: Path, user_home: Path, marketplace_name: str) -> tuple[Path, str]:
    """Locate the cache slot for an exact prior managed plugin source."""

    version, _ = _verify_managed_package(previous, require_versioned_path=False)
    cache_root = _cache_root(user_home, marketplace_name)
    return cache_root / version, version


def _package_bytes(root: Path) -> int:
    """Validate a package tree while bounding a temporary compatibility backup."""

    total = 0
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError("Managed source cannot contain symlinks")
        if item.is_file():
            total += item.stat().st_size
            if total > MAX_CACHE_BACKUP_BYTES:
                raise ValueError("Codex Mem cache backup exceeds its size limit")
        elif not item.is_dir():
            raise ValueError("Codex Mem cache package contains an unsupported file type")
    return total


def _verify_managed_package(
    package: Path, *, expected_version: str | None = None, require_versioned_path: bool = True
) -> tuple[str, int]:
    """Verify a managed source or a versioned cache package."""

    if package.is_symlink() or not package.is_dir():
        raise ValueError("Codex Mem cache package has an unsafe path")
    if require_versioned_path and not _CACHE_VERSION.fullmatch(package.name):
        raise ValueError("Codex Mem cache package has an unsafe path")
    _assert_tree_has_no_symlinks(package)
    if not (package / MARKER).is_file():
        raise ValueError("Codex Mem cache package is missing its installer marker")
    info = manifest(package)
    version = info.get("version")
    if not isinstance(version, str) or not _CACHE_VERSION.fullmatch(version):
        raise ValueError("Codex Mem cache package has an unsafe version")
    if expected_version is not None and version != expected_version:
        raise ValueError("Codex Mem cache package version does not match its expected version")
    if require_versioned_path and version != package.name:
        raise ValueError("Codex Mem cache package version does not match its path")
    return version, _package_bytes(package)


def _copy_cache_backup(
    source: Path, backup: Path, version: str, total: int, *, source_is_cache: bool
) -> int:
    """Make a verified, bounded exact copy in the installer's temporary workspace."""

    _, source_bytes = _verify_managed_package(
        source, expected_version=version, require_versioned_path=source_is_cache
    )
    if total + source_bytes > MAX_CACHE_BACKUP_BYTES:
        raise ValueError("Codex Mem cache backup exceeds its size limit")
    shutil.copytree(source, backup)
    try:
        copied_version, copied_bytes = _verify_managed_package(backup, expected_version=version)
        if copied_version != version or total + copied_bytes > MAX_CACHE_BACKUP_BYTES:
            raise ValueError("Codex Mem cache backup verification failed")
    except Exception:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    return copied_bytes


def _snapshot_existing_caches(cache_root: Path, backup_root: Path) -> tuple[list[tuple[Path, Path, str, str]], int]:
    """Copy every valid own-package cache before Codex may prune it."""

    _assert_cache_ancestors(cache_root)
    if not cache_root.exists():
        return [], 0
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ValueError("Codex Mem cache path cannot be a symlink or non-directory")
    packages = sorted(cache_root.iterdir(), key=lambda path: path.name)
    if len(packages) > MAX_CACHE_PACKAGES:
        raise ValueError("Codex Mem cache has too many package entries")
    snapshots: list[tuple[Path, Path, str, str]] = []
    total = 0
    for package in packages:
        # Finder metadata and other non-version artifacts are not managed
        # plugin packages.  A version-shaped entry, however, must verify or we
        # refuse activation rather than silently risk a live launcher.
        if not _CACHE_VERSION.fullmatch(package.name):
            continue
        version, _ = _verify_managed_package(package)
        backup = backup_root / version
        copied_bytes = _copy_cache_backup(package, backup, version, total, source_is_cache=True)
        total += copied_bytes
        snapshots.append((backup, package, version, "cached"))
    return snapshots, total


def _add_previous_source_backup(
    snapshots: list[tuple[Path, Path, str, str]],
    total: int,
    previous: Path,
    destination: Path,
    version: str,
    backup_root: Path,
) -> tuple[list[tuple[Path, Path, str, str]], int]:
    """Add the prior managed source only when its cache version was absent."""

    if any(saved_version == version for _, _, saved_version, _ in snapshots):
        return snapshots, total
    backup = backup_root / version
    copied_bytes = _copy_cache_backup(
        previous, backup, version, total, source_is_cache=False
    )
    # The prior source is only eligible because it has our marker and exactly
    # matches its cache version; _previous_cache_destination checked both.
    snapshots.append((backup, destination, version, "previous_source"))
    return snapshots, total + copied_bytes


def _restore_cache_package(
    source: Path, destination: Path, version: str
) -> dict:
    """Restore a missing prior cache without changing an existing cache slot.

    Codex may remove the old version while adding an upgrade.  The old managed
    source remains available until activation finishes. ``mkdir`` claims the
    exact destination first, preventing an overwrite if another process
    restored it meanwhile.
    """

    _assert_cache_ancestors(destination.parent)
    if destination.is_symlink():
        raise ValueError("Codex Mem cache package cannot be a symlink")
    if destination.exists():
        try:
            existing_version, _ = _verify_managed_package(destination)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": version, "path": str(destination), "status": "already_present_unverified"}
        if existing_version == version:
            return {"version": version, "path": str(destination), "status": "already_present"}
        return {"version": version, "path": str(destination), "status": "already_present_unverified"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_cache_ancestors(destination.parent)
    try:
        destination.mkdir()
    except FileExistsError:
        if destination.is_symlink():
            raise ValueError("Codex Mem cache package cannot be a symlink")
        try:
            existing_version, _ = _verify_managed_package(destination)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": version, "path": str(destination), "status": "already_present_unverified"}
        if existing_version == version:
            return {"version": version, "path": str(destination), "status": "already_present"}
        return {"version": version, "path": str(destination), "status": "already_present_unverified"}
    # destination was claimed while absent, so this cannot overwrite a package
    # created by Codex or another installer.  On a copy error keep the partial
    # directory for inspection instead of deleting another writer's files.
    shutil.copytree(source, destination, dirs_exist_ok=True)
    _assert_tree_has_no_symlinks(destination)
    copied_version, _ = _verify_managed_package(destination)
    if copied_version != version:
        raise ValueError("Codex Mem cache restoration verification failed")
    return {"version": version, "path": str(destination), "status": "restored"}


def install(source: Path, user_home: Path, *, apply: bool, register_only: bool = False,
            codex: str = "codex") -> dict:
    source = source.resolve()
    user_home = user_home.expanduser().resolve()
    info = manifest(source)
    target = user_home / "plugins" / NAME
    marketplace = user_home / ".agents/plugins/marketplace.json"
    current = registry(marketplace)
    matches = [p for p in current["plugins"] if p.get("name") == NAME]
    expected = {"source": "local", "path": "./plugins/codex-mem"}
    if len(matches) > 1 or (matches and matches[0].get("source") != expected):
        raise ValueError("A different codex-mem marketplace entry already exists")
    if target.is_symlink() or (target.exists() and not (target / MARKER).is_file()):
        raise ValueError("Destination belongs to a different installation; choose another location manually")
    result = {"plugin": NAME, "version": info["version"], "source": str(source),
              "destination": str(target), "marketplace": str(marketplace),
              "selector": NAME + "@" + current["name"], "applied": False}
    if not apply:
        return result
    if user_home != Path.home().resolve() and not register_only:
        raise ValueError("Custom --home supports --register-only; it does not redirect Codex configuration")
    if not register_only and shutil.which(codex) is None:
        raise ValueError("Codex CLI is not on PATH")
    target.parent.mkdir(parents=True, exist_ok=True)
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    # Serialize registry updates, including the read/modify/write step.
    import fcntl
    with (marketplace.parent / ".codex-mem-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = registry(marketplace)
        matches = [p for p in current["plugins"] if p.get("name") == NAME]
        if len(matches) > 1 or (matches and matches[0].get("source") != expected):
            raise ValueError("Marketplace entry changed while preparing installation")
        old_registry = marketplace.read_bytes() if marketplace.exists() else None
        previous = target.parent / (".codex-mem-previous-" + str(time.time_ns()))
        previous_cache: tuple[Path, str] | None = None
        cache_backups: list[tuple[Path, Path, str, str]] = []
        cache_backup_root: Path | None = None
        cache_total = 0
        retain_cache_backup = False
        stage = Path(tempfile.mkdtemp(prefix=".codex-mem-stage-", dir=target.parent))
        swapped = False
        try:
            # Snapshot all live cache versions before touching the installed
            # source or marketplace registration.  A failed snapshot therefore
            # leaves the previous installation selected and usable.
            if not register_only:
                cache_root = _cache_root(user_home, current["name"])
                cache_backup_root = Path(tempfile.mkdtemp(
                    prefix=".codex-mem-cache-backup-", dir=target.parent
                ))
                cache_backups, cache_total = _snapshot_existing_caches(
                    cache_root, cache_backup_root
                )
            for name in FILES:
                item = source / name
                if not item.exists():
                    continue
                if item.is_symlink():
                    raise ValueError("Source distribution cannot contain symlinks")
                if item.is_dir():
                    if any(p.is_symlink() for p in item.rglob("*")):
                        raise ValueError("Source distribution cannot contain symlinks")
                    shutil.copytree(item, stage / name,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
                else:
                    shutil.copy2(item, stage / name)
            (stage / MARKER).write_text("codex-mem personal installer v1\n")
            manifest(stage)
            if target.exists():
                target.rename(previous)
                previous_cache = _previous_cache_destination(previous, user_home, current["name"])
                if cache_backup_root is not None:
                    destination, version = previous_cache
                    cache_backups, cache_total = _add_previous_source_backup(
                        cache_backups, cache_total, previous, destination, version, cache_backup_root
                    )
            stage.rename(target)
            swapped = True
            if not matches:
                current["plugins"].append({"name": NAME, "source": expected,
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "Productivity"})
            if old_registry is not None:
                backup = marketplace.with_name("marketplace.before-codex-mem-" + str(time.time_ns()) + ".json")
                backup.write_bytes(old_registry)
                backup.chmod(0o600)
                result["registry_backup"] = str(backup)
            write_json(marketplace, current)
            result["applied"] = True
            result["selector"] = NAME + "@" + current["name"]
            if previous.exists():
                result["previous_source"] = str(previous)
        except Exception:
            if swapped:
                shutil.rmtree(target)
            if previous.exists():
                previous.rename(target)
            if cache_backup_root is not None:
                shutil.rmtree(cache_backup_root, ignore_errors=True)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        # Keep the source stable until Codex has copied this version to its cache.
        try:
            activated = _activate(result, codex=codex, register_only=register_only)
            if cache_backups:
                retained: list[dict] = []
                restoration_failed = False
                for backup, destination, version, retained_from in cache_backups:
                    try:
                        receipt = _restore_cache_package(backup, destination, version)
                    except (OSError, shutil.Error, ValueError, json.JSONDecodeError):
                        receipt = {
                            "version": version,
                            "path": str(destination),
                            "status": "restore_failed",
                        }
                    receipt["retained_from"] = retained_from
                    retained.append(receipt)
                    if receipt["status"] not in {"restored", "already_present"}:
                        restoration_failed = True
                activated["retained_caches"] = retained
                if previous_cache is not None:
                    _, previous_version = previous_cache
                    activated["previous_cache"] = next(
                        receipt for receipt in retained if receipt["version"] == previous_version
                    )
                if restoration_failed:
                    activated["cache_preservation"] = "partial"
                    cache_error = (
                        "One or more prior Codex Mem cache packages could not be restored. "
                        "Use the retained cache recovery backup or previous source before restarting Codex."
                    )
                    if activated.get("error"):
                        activated["cache_error"] = cache_error
                    else:
                        activated["error"] = cache_error
                    if cache_backup_root is not None:
                        activated["cache_recovery_backup"] = str(cache_backup_root)
                        retain_cache_backup = True
                else:
                    activated["cache_preservation"] = "complete"
            return activated
        finally:
            if cache_backup_root is not None and not retain_cache_backup:
                shutil.rmtree(cache_backup_root, ignore_errors=True)


def _activate(result: dict, *, codex: str, register_only: bool) -> dict:
    result["installed"] = False
    if not register_only:
        try:
            completed = subprocess.run([codex, "plugin", "add", result["selector"], "--json"],
                                       capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            result["error"] = "Source registered; Codex activation did not return a result. Verify codex plugin list before retrying."
            result["activation_status"] = "unknown"
            return result
        if completed.returncode:
            result["error"] = "Source registered, but Codex activation failed. Retry this installer after resolving the activation failure."
            return result
        try:
            result["codex"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result["error"] = "Source registered; Codex returned an unreadable activation result. Verify codex plugin list."
            return result
        result["installed"] = True
    result["hook_trust"] = "Review the installed hooks in Codex /hooks before automatic capture."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--home", type=Path, default=Path.home(), help="Test destination; requires --register-only for a custom home")
    parser.add_argument("--apply", action="store_true", help="Copy source, register, and install; otherwise preview")
    parser.add_argument("--register-only", action="store_true", help="Prepare source and marketplace without invoking Codex")
    args = parser.parse_args()
    try:
        result = install(args.source, args.home, apply=args.apply, register_only=args.register_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("error") else 0
    except (ValueError, OSError, shutil.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
