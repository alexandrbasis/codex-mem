#!/usr/bin/env python3
"""Read-only checks for the engineering skill lock and configured enablement.

Python 3.10+; no third-party packages. Lock schema version 1 has a `skills`
array. Each entry contains name, scope (local/global), path, files (relative
path -> SHA-256), and invocation_policy (implicit/explicit-only). The top-level
config.path chooses the user or project configuration; relative selectors resolve
against that configuration file's parent. Selectors do not add discovery roots. Source,
upstream_files, and patches metadata do not affect installed-file checks.

On Python 3.10 the TOML reader supports only the generated skills.config
subset: ordinary table headers, single-line quoted paths or names, and boolean enabled.
Unsupported syntax in a skills.config entry fails closed. This is not a YAML
or Markdown validator. Only SKILL.md frontmatter name and the YAML policy's
allow_implicit_invocation boolean are read. Runtime skill discovery remains
an independent check through Codex; this script checks files and configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

LOCAL_NAMES = frozenset("ask-matt code-review domain-modeling grill-with-docs implement implement-spec pr research retro setup-matt-pocock-skills to-spec to-tickets triage wayfinder".split())
GLOBAL_NAMES = frozenset("codebase-design diagnosing-bugs improve-codebase-architecture prototype tdd wizard grilling writing-for-agents".split())


def frontmatter_name(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.fullmatch(r"name:\s*([^#]+?)\s*(?:#.*)?", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def config_entries(path: Path) -> list[dict]:
    """Use full TOML when available; otherwise read the documented subset."""
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        entries: list[dict] = []
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                current = {} if line == "[[skills.config]]" else None
                if current is not None:
                    entries.append(current)
                continue
            if current is None:
                continue
            match = re.fullmatch(r"(path|name|enabled)\s*=\s*(.+)", line)
            if not match:
                raise ValueError("unsupported skills.config syntax on Python 3.10")
            key, value = match.groups()
            if key in current:
                raise ValueError("duplicate skills.config key")
            if key == "enabled":
                match_bool = re.fullmatch(r"(true|false)\s*(?:#.*)?", value)
                if not match_bool:
                    raise ValueError("unsupported skills.config boolean")
                current[key] = match_bool.group(1) == "true"
            elif value.startswith('"'):
                # JSON strings cover the generated paths. Refuse TOML-only escapes.
                decoder = json.JSONDecoder()
                decoded, end = decoder.raw_decode(value)
                if not isinstance(decoded, str) or value[end:].strip().split("#", 1)[0].strip():
                    raise ValueError("unsupported skills.config path")
                current[key] = decoded
            else:
                match_string = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
                if not match_string:
                    raise ValueError("unsupported skills.config path")
                current[key] = match_string.group(1)
        return entries
    data = tomllib.loads(text)
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("skills must be a table")
    entries = skills.get("config", [])
    if not isinstance(entries, list) or any(not isinstance(x, dict) for x in entries):
        raise ValueError("skills.config must be an array of tables")
    return entries


def resolved_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def implicit_policy(path: Path) -> bool | None:
    """Read only an unquoted YAML policy block and its boolean setting."""
    if not path.is_file():
        return None
    in_policy = False
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            in_policy = bool(re.fullmatch(r"policy:\s*(?:#.*)?", raw))
            continue
        if in_policy:
            match = re.fullmatch(r"\s+allow_implicit_invocation:\s*(true|false)\s*(?:#.*)?", raw)
            if match:
                found.append(match.group(1) == "true")
    return found[0] if len(found) == 1 else None


def validate(root: Path, user_root: Path, manifest: Path, config_override: Path | None = None) -> dict:
    issues: list[str] = []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "checked_skills": 0, "issues": [f"Cannot read valid lock JSON: {manifest}"]}
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("skills"), list):
        return {"ok": False, "checked_skills": 0, "issues": [f"Invalid lock schema: {manifest}"]}
    entries = data["skills"]
    lock_config = data.get("config", {})
    if not isinstance(lock_config, dict) or not isinstance(lock_config.get("path", ".codex/config.toml"), str):
        return {"ok": False, "checked_skills": 0, "issues": [f"Invalid lock configuration: {manifest}"]}
    config_path = resolved_path(str(config_override or lock_config.get("path", ".codex/config.toml")), root)
    try:
        configs = config_entries(config_path)
    except (OSError, UnicodeError, ValueError):
        issues.append(f"Cannot read supported TOML configuration: {config_path}")
        configs = []
    configured: dict[Path, list] = {}
    name_overrides: dict[str, list] = {}
    for config in configs:
        if isinstance(config.get("name"), str):
            name_overrides.setdefault(config["name"], []).append(config.get("enabled"))
        if not isinstance(config.get("path"), str):
            # User configuration can contain unrelated name-only overrides.
            continue
        path = resolved_path(config["path"], config_path.parent)
        if path.name == "SKILL.md":
            path = path.parent
        configured.setdefault(path, []).append(config.get("enabled"))
    seen_names: set[str] = set()
    scopes: dict[str, set[str]] = {"local": set(), "global": set()}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"Invalid lock entry at index {index}")
            continue
        name, scope = entry.get("name"), entry.get("scope")
        if not isinstance(name, str) or not isinstance(scope, str) or scope not in scopes or not isinstance(entry.get("path"), str):
            issues.append(f"Invalid lock identity at index {index}")
            continue
        if name in seen_names:
            issues.append(f"Duplicate lock skill name: {name}")
        seen_names.add(name)
        scopes[scope].add(name)
        path = resolved_path(entry["path"], root)
        expected_root = root / ".agents/skills" if scope == "local" else user_root
        if path != (expected_root / name).resolve():
            issues.append(f"Skill path does not match its scope: {path}")
        skill_file = path / "SKILL.md"
        try:
            if frontmatter_name(skill_file) != name:
                issues.append(f"SKILL.md name mismatch: {skill_file}")
        except (OSError, UnicodeError):
            issues.append(f"Cannot read SKILL.md: {skill_file}")
        enabled = configured.get(path, [])
        if len(enabled) != 1 or enabled[0] is not True:
            issues.append(f"Expected one enabled=true config selector: {path}")
        if any(value is False for value in name_overrides.get(name, [])):
            issues.append(f"Name override disables selected skill: {name}")
        policy = entry.get("invocation_policy")
        actual_policy = implicit_policy(path / "agents/openai.yaml")
        if not isinstance(policy, str) or policy not in {"implicit", "explicit-only"}:
            issues.append(f"Invalid invocation policy: {path}")
        elif policy == "explicit-only" and actual_policy is not False:
            issues.append(f"Explicit-only YAML policy missing or invalid: {path / 'agents/openai.yaml'}")
        elif policy == "implicit" and actual_policy is False:
            issues.append(f"Implicit invocation conflicts with YAML policy: {path / 'agents/openai.yaml'}")
        files = entry.get("files")
        if not isinstance(files, dict) or "SKILL.md" not in files:
            issues.append(f"Lock must include files and SKILL.md: {path}")
            continue
        for relative, expected_hash in files.items():
            if not isinstance(relative, str):
                issues.append(f"Invalid file key: {path}")
                continue
            file_path = path / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or not file_path.resolve().is_relative_to(path):
                issues.append(f"File path escapes skill directory: {file_path}")
                continue
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                issues.append(f"Invalid SHA-256 in lock: {file_path}")
                continue
            try:
                actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            except OSError:
                issues.append(f"Missing or unreadable locked file: {file_path}")
                continue
            if actual_hash != expected_hash:
                issues.append(f"File hash drift: {file_path}")
        if path.is_dir():
            for file_path in path.rglob("*"):
                if file_path.is_file() and file_path.name != ".DS_Store" and "__pycache__" not in file_path.parts:
                    if file_path.relative_to(path).as_posix() not in files:
                        issues.append(f"Installed file absent from lock: {file_path}")
    for scope, expected in (("local", LOCAL_NAMES), ("global", GLOBAL_NAMES)):
        for name in sorted(expected - scopes[scope]):
            issues.append(f"Missing {scope} lock skill: {name}")
        for name in sorted(scopes[scope] - expected):
            issues.append(f"Unexpected {scope} lock skill: {name}")
    # Canonical file paths collapse ~/.codex/skills -> ~/.agents/skills aliases.
    discovered: dict[str, set[Path]] = {}
    scan_roots = {root / ".agents/skills", user_root}
    if user_root == (Path.home() / ".agents/skills").resolve():
        scan_roots.add(Path.home() / ".codex/skills")
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        for directory in scan_root.iterdir():
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                name = frontmatter_name(skill_file)
            except (OSError, UnicodeError):
                continue
            if name in LOCAL_NAMES | GLOBAL_NAMES:
                discovered.setdefault(name, set()).add(skill_file.resolve())
    for name, paths in sorted(discovered.items()):
        if len(paths) > 1:
            issues.append(f"Duplicate installed skill name {name}: " + ", ".join(str(x) for x in sorted(paths)))
    return {"ok": not issues, "checked_skills": len(entries), "issues": issues}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--user-skills-root", type=Path, default=Path.home() / ".agents/skills")
    parser.add_argument("--manifest", type=Path, help="Lock path; relative paths resolve under --root")
    parser.add_argument("--config", type=Path, help="Configuration override; relative paths resolve under --root")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    manifest = args.manifest or Path("docs/agents/skill-lock.json")
    manifest = resolved_path(str(manifest), root)
    try:
        result = validate(root, args.user_skills_root.expanduser().resolve(), manifest, args.config)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        result = {"ok": False, "checked_skills": 0, "issues": ["Cannot complete filesystem validation"]}
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(f"Validated {result['checked_skills']} engineering skills.")
    else:
        print("Engineering skill validation failed:")
        for issue in result["issues"]:
            print(f"- {issue}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
