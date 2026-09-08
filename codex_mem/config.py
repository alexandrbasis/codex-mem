"""Configuration and small local state for Codex Mem.

The memory store owns durable records.  This module owns only user settings and
the tiny hook state used to avoid injecting the same context on every turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

try:  # Hooks run on macOS/Linux today; keep imports portable for library users.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide flock
    fcntl = None  # type: ignore[assignment]


CONFIG_FILENAME = "config.json"
HOOK_STATE_FILENAME = "hook-state.json"
MAX_CONTEXT_CHARS = 6_000
DEFAULT_CONFIG: dict[str, Any] = {
    "capture_enabled": True,
    "capture_tools": True,
    "processor_enabled": True,
    "service_enabled": True,
    "semantic_enabled": True,
    "capture_scope": "selected",
    "context_chars": MAX_CONTEXT_CHARS,
    "excluded_projects": [],
    "included_projects": [],
    # Raw tool I/O is useful for explicit source readback, but noisy tools and
    # the memory server itself should never grow that side index.  Keep the
    # list user-configurable while retaining the existing all-projects scope
    # default unchanged.
    "tool_skip_list": [],
    "private_prompt_gate": True,
}
_CAPTURE_SCOPES = {"selected", "all", "manual"}
_CONFIG_ALIASES = {
    "skip_tools": "tool_skip_list",
}


def _fresh_default_config() -> dict[str, Any]:
    """Return defaults without sharing mutable project-path lists."""

    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_CONFIG.items()
    }


class LoadedConfig(dict[str, Any]):
    """A normal mapping with non-serialized validation state.

    Keeping the state as an attribute preserves the small documented JSON shape
    for callers while allowing hooks to fail closed when an existing config was
    corrupted or unreadable.
    """

    valid: bool

    def __init__(self, values: Mapping[str, Any], *, valid: bool) -> None:
        super().__init__(values)
        self.valid = valid


def data_dir_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the data directory without creating it."""

    raw_path: str | os.PathLike[str]
    if data_dir is not None:
        raw_path = data_dir
    else:
        configured = os.environ.get("CODEX_MEM_HOME", "").strip()
        raw_path = configured or (Path.home() / ".local" / "share" / "codex-mem")
    return Path(raw_path).expanduser().resolve(strict=False)


def config_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the configuration file location."""

    return data_dir_path(data_dir) / CONFIG_FILENAME


def hooks_disabled() -> bool:
    """Whether the global emergency opt-out is enabled."""

    return os.environ.get("CODEX_MEM_DISABLED") == "1"


def load_config(data_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load a valid, complete configuration.

    Missing configuration uses privacy-preserving defaults.  A malformed or
    invalid existing file returns a disabled mapping so hooks fail closed while
    Codex itself continues normally.
    """

    raw: Any
    try:
        with config_path(data_dir).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return LoadedConfig(_fresh_default_config(), valid=True)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return _disabled_config()
    return _normalise_config(raw, strict=False)


def configure(
    data_dir: str | os.PathLike[str] | None = None, **updates: Any
) -> dict[str, Any]:
    """Validate and persist configuration changes for the CLI.

    ``data_dir`` is intentionally an argument rather than a setting, so tests
    and the command-line client can use an isolated memory home.
    """

    # This also makes ``configure(**{"data_dir": path, ...})`` harmless for
    # callers that build options dynamically.
    if data_dir is None and "data_dir" in updates:
        data_dir = updates.pop("data_dir")
    elif "data_dir" in updates:
        raise ValueError("data_dir must be passed as an argument")

    # Accept the names used by the upstream settings and by older local
    # previews, but persist one canonical field so later reads stay stable.
    aliased: dict[str, Any] = {}
    for key, value in updates.items():
        canonical = _CONFIG_ALIASES.get(key, key)
        if canonical in aliased:
            raise ValueError("configuration field specified more than once")
        aliased[canonical] = value
    updates = aliased

    unknown = set(updates).difference(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("unknown configuration field")

    current = load_config(data_dir)
    if not updates:
        return current

    candidate = dict(current)
    candidate.update(updates)
    validated = _normalise_config(candidate, strict=True)
    destination = config_path(data_dir)
    _write_json(destination, validated)
    return validated


def is_excluded_project(
    project: str | os.PathLike[str] | None, config: Mapping[str, Any] | None = None
) -> bool:
    """Return whether ``project`` is an exact or descendant excluded path."""

    settings = config if config is not None else load_config()
    return _matches_project_paths(project, settings.get("excluded_projects", []))


def is_included_project(
    project: str | os.PathLike[str] | None, config: Mapping[str, Any] | None = None
) -> bool:
    """Return whether ``project`` is an exact or descendant included path."""

    settings = config if config is not None else load_config()
    return _matches_project_paths(project, settings.get("included_projects", []))


def automatic_capture_enabled(
    project: str | os.PathLike[str] | None, config: Mapping[str, Any] | None = None
) -> bool:
    """Whether hooks may automatically capture or inject for this project."""

    settings = config if config is not None else load_config()
    if not getattr(settings, "valid", True) or not settings.get("capture_enabled"):
        return False
    if is_excluded_project(project, settings):
        return False
    scope = settings.get("capture_scope")
    if scope == "all":
        return True
    if scope == "selected":
        return is_included_project(project, settings)
    return False


def configured_tool_skip_list(config: Mapping[str, Any] | None = None) -> list[str]:
    """Return the exact per-tool skip list used by raw capture filters."""

    settings = config if config is not None else load_config()
    value = settings.get("tool_skip_list", settings.get("skip_tools", []))
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def private_prompt_gate_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Whether private prompts suppress later tools in the same session/turn."""

    settings = config if config is not None else load_config()
    value = settings.get("private_prompt_gate", True)
    return value is True


def _matches_project_paths(project: str | os.PathLike[str] | None, candidates: Any) -> bool:
    if not project or not isinstance(candidates, list):
        return False

    try:
        project_path = Path(project).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    for entry in candidates:
        if not isinstance(entry, str) or not entry:
            continue
        try:
            root = Path(entry).expanduser().resolve(strict=False)
            project_path.relative_to(root)
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return False


def context_was_injected(
    session_key: str, *, source: str | None = None, data_dir: str | os.PathLike[str] | None = None
) -> bool:
    """Read hook state without creating files."""

    if not session_key:
        return False
    state = _load_hook_state(data_dir)
    record = state.get("context_injections", {}).get(session_key)
    if not isinstance(record, dict):
        return False
    sources = record.get("sources", [])
    if not isinstance(sources, list):
        return False
    if source is None:
        return bool(sources)
    return source in sources


def mark_context_injected(
    session_key: str,
    *,
    source: str,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Persist a successful automatic context injection.

    Hooks are fail-open, so state persistence failures are deliberately ignored
    by callers.  Entries are capped to keep this separate state file small.
    """

    if not session_key or not source:
        return
    base = data_dir_path(data_dir)
    with _hook_state_lock(base):
        state = _load_hook_state(base)
        injections = state.setdefault("context_injections", {})
        if not isinstance(injections, dict):
            injections = {}
            state["context_injections"] = injections
        record = injections.get(session_key)
        if not isinstance(record, dict):
            record = {"sources": []}
            injections[session_key] = record
        sources = record.get("sources")
        if not isinstance(sources, list):
            sources = []
            record["sources"] = sources
        if source not in sources:
            sources.append(source)
        while len(sources) > 128:
            del sources[0]

        # Dict insertion order is deterministic on supported Python versions.
        while len(injections) > 256:
            oldest = next(iter(injections), None)
            if oldest is None:
                break
            del injections[oldest]
        _write_json(base / HOOK_STATE_FILENAME, state)


def mark_private_prompt_gate(
    session_key: str,
    *,
    turn_id: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Remember that a private prompt opened a same-session tool gate."""

    if not session_key:
        return
    base = data_dir_path(data_dir)
    with _hook_state_lock(base):
        state = _load_hook_state(base)
        gates = state.setdefault("private_prompt_gates", {})
        if not isinstance(gates, dict):
            gates = {}
            state["private_prompt_gates"] = gates
        gates[session_key] = {"turn_id": turn_id or None}
        while len(gates) > 256:
            oldest = next(iter(gates), None)
            if oldest is None:
                break
            del gates[oldest]
        _write_json(base / HOOK_STATE_FILENAME, state)


def private_prompt_gate_active(
    session_key: str,
    *,
    turn_id: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Check a private prompt gate without creating or mutating state."""

    if not session_key:
        return False
    state = _load_hook_state(data_dir)
    gates = state.get("private_prompt_gates", {})
    if not isinstance(gates, dict):
        return False
    record = gates.get(session_key)
    if not isinstance(record, dict):
        return False
    opened_turn = record.get("turn_id")
    if opened_turn is None or turn_id is None:
        return True
    return opened_turn == turn_id


def clear_private_prompt_gate(
    session_key: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Clear a prior private prompt gate when a new public prompt arrives."""

    if not session_key:
        return
    base = data_dir_path(data_dir)
    with _hook_state_lock(base):
        state = _load_hook_state(base)
        gates = state.get("private_prompt_gates")
        if not isinstance(gates, dict) or session_key not in gates:
            return
        del gates[session_key]
        _write_json(base / HOOK_STATE_FILENAME, state)


def _normalise_config(raw: Any, *, strict: bool) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        if strict:
            raise ValueError("configuration must be an object")
        return _disabled_config()

    if not strict and not _has_valid_present_fields(raw):
        return _disabled_config()

    result = {
        "capture_enabled": _validate_bool(
            raw.get("capture_enabled", DEFAULT_CONFIG["capture_enabled"]),
            "capture_enabled",
            strict,
        ),
        "capture_tools": _validate_bool(
            raw.get("capture_tools", DEFAULT_CONFIG["capture_tools"]),
            "capture_tools",
            strict,
        ),
        "processor_enabled": _validate_bool(
            raw.get("processor_enabled", DEFAULT_CONFIG["processor_enabled"]),
            "processor_enabled",
            strict,
        ),
        "service_enabled": _validate_bool(raw.get("service_enabled", True), "service_enabled", strict),
        "semantic_enabled": _validate_bool(raw.get("semantic_enabled", True), "semantic_enabled", strict),
        "capture_scope": _validate_capture_scope(
            raw.get("capture_scope", DEFAULT_CONFIG["capture_scope"]), strict
        ),
        "context_chars": _validate_context_chars(
            raw.get("context_chars", DEFAULT_CONFIG["context_chars"]), strict
        ),
        "excluded_projects": _validate_excluded_projects(
            raw.get("excluded_projects", DEFAULT_CONFIG["excluded_projects"]), strict
        ),
        "included_projects": _validate_excluded_projects(
            raw.get("included_projects", DEFAULT_CONFIG["included_projects"]), strict
        ),
        "tool_skip_list": _validate_tool_skip_list(
            raw.get("tool_skip_list", DEFAULT_CONFIG["tool_skip_list"]), strict
        ),
        "private_prompt_gate": _validate_bool(
            raw.get("private_prompt_gate", DEFAULT_CONFIG["private_prompt_gate"]),
            "private_prompt_gate",
            strict,
        ),
    }
    return LoadedConfig(result, valid=True)


def _disabled_config() -> LoadedConfig:
    """Fail closed for hooks when a config file cannot be trusted."""

    value = _fresh_default_config()
    value["capture_enabled"] = False
    value["capture_tools"] = False
    value["processor_enabled"] = False
    value["service_enabled"] = False
    value["semantic_enabled"] = False
    return LoadedConfig(value, valid=False)


def _has_valid_present_fields(raw: Mapping[str, Any]) -> bool:
    """Validate every present field so invalid exclusions never disappear."""

    try:
        if "capture_enabled" in raw:
            _validate_bool(raw["capture_enabled"], "capture_enabled", True)
        if "capture_tools" in raw:
            _validate_bool(raw["capture_tools"], "capture_tools", True)
        if "processor_enabled" in raw:
            _validate_bool(raw["processor_enabled"], "processor_enabled", True)
        for name in ("service_enabled", "semantic_enabled"):
            if name in raw:
                _validate_bool(raw[name], name, True)
        if "capture_scope" in raw:
            _validate_capture_scope(raw["capture_scope"], True)
        if "context_chars" in raw:
            _validate_context_chars(raw["context_chars"], True)
        if "excluded_projects" in raw:
            _validate_excluded_projects(raw["excluded_projects"], True)
        if "included_projects" in raw:
            _validate_excluded_projects(raw["included_projects"], True)
        if "tool_skip_list" in raw:
            _validate_tool_skip_list(raw["tool_skip_list"], True)
        if "private_prompt_gate" in raw:
            _validate_bool(raw["private_prompt_gate"], "private_prompt_gate", True)
    except ValueError:
        return False
    return True


def _validate_bool(value: Any, name: str, strict: bool) -> bool:
    if isinstance(value, bool):
        return value
    if strict:
        raise ValueError(f"{name} must be a boolean")
    return bool(DEFAULT_CONFIG[name])


def _validate_context_chars(value: Any, strict: bool) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_CONTEXT_CHARS:
        return value
    if strict:
        raise ValueError(f"context_chars must be between 1 and {MAX_CONTEXT_CHARS}")
    return MAX_CONTEXT_CHARS


def _validate_capture_scope(value: Any, strict: bool) -> str:
    if isinstance(value, str) and value.lower() in _CAPTURE_SCOPES:
        return value.lower()
    if strict:
        raise ValueError("capture_scope must be selected, all, or manual")
    return str(DEFAULT_CONFIG["capture_scope"])


def _validate_excluded_projects(value: Any, strict: bool) -> list[str]:
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError("excluded_projects must be a list of paths")
        return []

    paths: list[str] = []
    for item in value:
        if not isinstance(item, (str, os.PathLike)):
            if strict:
                raise ValueError("excluded_projects must contain only paths")
            continue
        text = os.fspath(item).strip()
        if not text:
            if strict:
                raise ValueError("excluded_projects cannot contain an empty path")
            continue
        try:
            normalised = str(Path(text).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, TypeError, ValueError):
            if strict:
                raise ValueError("excluded_projects contains an invalid path") from None
            continue
        if normalised not in paths:
            paths.append(normalised)
    return paths


def _validate_tool_skip_list(value: Any, strict: bool) -> list[str]:
    """Validate exact tool names without interpreting shell patterns."""

    if isinstance(value, str):
        candidates: list[Any] = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        if strict:
            raise ValueError("tool_skip_list must be a list of tool names")
        return []
    if len(candidates) > 128:
        if strict:
            raise ValueError("tool_skip_list has too many values")
        candidates = candidates[:128]
    names: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            if strict:
                raise ValueError("tool_skip_list must contain only tool names")
            continue
        cleaned = item.strip()
        if not cleaned:
            if strict:
                raise ValueError("tool_skip_list cannot contain an empty tool name")
            continue
        if len(cleaned) > 256 or "\x00" in cleaned:
            if strict:
                raise ValueError("tool_skip_list contains an invalid tool name")
            continue
        if cleaned.casefold() not in {name.casefold() for name in names}:
            names.append(cleaned)
    return names


def _load_hook_state(data_dir: str | os.PathLike[str] | None) -> dict[str, Any]:
    try:
        with (data_dir_path(data_dir) / HOOK_STATE_FILENAME).open(
            "r", encoding="utf-8"
        ) as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            return raw
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "context_injections": {}, "private_prompt_gates": {}}


class _HookStateLock:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.handle: Any | None = None

    def __enter__(self) -> "_HookStateLock":
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.handle = (self.base / ".hook-state.lock").open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()


def _hook_state_lock(base: Path) -> _HookStateLock:
    return _HookStateLock(base)


def _write_json(destination: Path, value: Mapping[str, Any]) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    except Exception:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise
