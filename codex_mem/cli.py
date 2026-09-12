"""Command-line interface for the local codex-mem store.

All ordinary commands emit a single JSON value on stdout.  ``serve`` is the
exception: it reserves stdout for its newline-delimited MCP protocol stream.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .store import MAX_TOOL_USE_ID_CHARS, Store, StoreError
from .semantic import SemanticError
from .service import ServiceError
from .mcp import bound_tool_uses

try:  # The package initializer is created alongside the rest of the package.
    from . import __version__ as _VERSION
except ImportError:  # pragma: no cover - useful while developing individual files
    _VERSION = "1.1.0"


class CLIError(ValueError):
    """A user-correctable argument error that can be rendered as JSON."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message)


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _error(code: str, message: str) -> int:
    _emit({"ok": False, "error": {"code": code, "message": message}})
    return 2


def _absolute_project(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CLIError("project must be a non-empty absolute path")
    if not Path(value).is_absolute():
        raise CLIError("project must be an absolute path")
    return value


def _positive(value: str, *, field: str, maximum: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CLIError(f"{field} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise CLIError(f"{field} must be between {minimum} and {maximum}")
    return number


def _collect_ids(namespace: argparse.Namespace) -> list[str]:
    values = list(getattr(namespace, "ids", []) or []) + list(
        getattr(namespace, "id_values", []) or []
    )
    if not values:
        raise CLIError("at least one record ID is required")
    if len(values) > 100:
        raise CLIError("at most 100 record IDs are allowed")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CLIError("record IDs must not be empty")
    if len(set(values)) != len(values):
        raise CLIError("record IDs must not contain duplicates")
    return values


def _collect_optional_ids(namespace: argparse.Namespace) -> list[str] | None:
    """Collect raw-evidence IDs without requiring an ID selector."""

    values = list(getattr(namespace, "ids", []) or []) + list(
        getattr(namespace, "id_values", []) or []
    )
    for encoded in getattr(namespace, "id_arrays", []) or []:
        values.extend(
            _parse_string_array(
                encoded,
                field="ids",
                maximum=100,
                maximum_chars=MAX_TOOL_USE_ID_CHARS,
            )
        )
    if not values:
        return None
    if len(values) > 100:
        raise CLIError("at most 100 record IDs are allowed")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CLIError("tool-use IDs must not be empty")
    if any(len(value) > MAX_TOOL_USE_ID_CHARS for value in values):
        raise CLIError("tool-use IDs are too long")
    if len(set(values)) != len(values):
        raise CLIError("tool-use IDs must not contain duplicates")
    return values


def _parse_bool(value: str, *, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CLIError(f"{field} must be true or false")


def _parse_excluded_projects(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CLIError("excluded_projects must be a JSON string array or comma-separated paths") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, str) or not item for item in parsed):
            raise CLIError("excluded_projects must be a string array")
        return parsed
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_string_array(value: str, *, field: str, maximum: int = 100, maximum_chars: int = 1_000) -> list[str]:
    """Parse a bounded string array used by config and structured filters."""

    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CLIError(f"{field} must be a JSON string array or comma-separated values") from exc
        if not isinstance(parsed, list):
            raise CLIError(f"{field} must be a string array")
        candidates = parsed
    else:
        candidates = [item.strip() for item in value.split(",")]
    if len(candidates) > maximum:
        raise CLIError(f"{field} has too many values")
    values: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip() or "\x00" in candidate:
            raise CLIError(f"{field} must contain non-empty strings")
        item = candidate.strip()
        if len(item) > maximum_chars:
            raise CLIError(f"{field} contains a value that is too long")
        if item not in seen:
            values.append(item)
            seen.add(item)
    return values


def _filter_values(values: Sequence[str] | None, *, field: str) -> list[str] | None:
    """Normalize repeatable retrieval flags, accepting one JSON/comma page too."""

    if values is None:
        return None
    flattened: list[str] = []
    for value in values:
        flattened.extend(_parse_string_array(value, field=field))
    return list(dict.fromkeys(flattened))


def _config_updates(namespace: argparse.Namespace) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for item in namespace.set_values or []:
        if "=" not in item:
            raise CLIError("--set must use key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise CLIError("--set key must not be empty")
        if key in {"capture_enabled", "capture_tools", "processor_enabled", "service_enabled", "semantic_enabled", "usage_enabled"}:
            updates[key] = _parse_bool(value, field=key)
        elif key == "context_chars":
            updates[key] = _positive(value, field=key, maximum=6_000)
        elif key in {"excluded_projects", "included_projects"}:
            updates[key] = _parse_excluded_projects(value)
        elif key in {"skip_tools", "tool_skip_list"}:
            updates[key] = _parse_string_array(value, field=key)
        elif key == "capture_scope":
            capture_scope = value.strip()
            if capture_scope not in {"selected", "all", "manual"}:
                raise CLIError("capture_scope must be selected, all, or manual")
            updates[key] = capture_scope
        else:
            # Let configure own the canonical supported-key validation.
            updates[key] = value

    if namespace.capture_enabled is not None:
        updates["capture_enabled"] = namespace.capture_enabled
    if namespace.capture_tools is not None:
        updates["capture_tools"] = namespace.capture_tools
    if namespace.processor_enabled is not None:
        updates["processor_enabled"] = namespace.processor_enabled
    for name in ("service_enabled", "semantic_enabled", "usage_enabled"):
        if getattr(namespace, name, None) is not None:
            updates[name] = getattr(namespace, name)
    if namespace.context_chars is not None:
        updates["context_chars"] = namespace.context_chars
    if namespace.excluded_projects is not None:
        updates["excluded_projects"] = namespace.excluded_projects
    if namespace.included_projects is not None:
        updates["included_projects"] = namespace.included_projects
    if getattr(namespace, "skip_tools", None) is not None:
        values: list[str] = []
        for item in namespace.skip_tools:
            values.extend(_parse_string_array(item, field="skip_tools"))
        updates["skip_tools"] = list(dict.fromkeys(values))
    if namespace.capture_scope is not None:
        updates["capture_scope"] = namespace.capture_scope
    return updates


def _observation_metadata(namespace: argparse.Namespace) -> dict[str, Any] | None:
    """Build optional structured observation metadata for ``remember``."""

    fields = {
        "type": getattr(namespace, "observation_type", None),
        "subtitle": getattr(namespace, "observation_subtitle", None),
        "facts": getattr(namespace, "facts", None),
        "concepts": getattr(namespace, "concepts", None),
        "files_read": getattr(namespace, "files_read", None),
        "files_modified": getattr(namespace, "files_modified", None),
    }
    if not any(value is not None for value in fields.values()):
        return None
    if not isinstance(fields["type"], str) or not fields["type"].strip():
        raise CLIError("--observation-type is required when structured observation fields are supplied")
    return {key: value for key, value in fields.items() if value is not None}


def _doctor() -> dict[str, Any]:
    """Return local, non-mutating diagnostics without claiming host integration."""
    runtime_ok = sys.version_info >= (3, 10)
    sqlite_fts = False
    sqlite_error: str | None = None
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE codex_mem_fts_probe USING fts5(content)")
            sqlite_fts = True
        finally:
            connection.close()
    except sqlite3.Error:
        sqlite_error = "FTS5 unavailable"

    repository_root = Path(__file__).resolve().parent.parent
    plugin_manifest = repository_root / ".codex-plugin" / "plugin.json"
    mcp_config = repository_root / ".mcp.json"
    manifest_valid = False
    if plugin_manifest.is_file():
        try:
            manifest_valid = isinstance(json.loads(plugin_manifest.read_text(encoding="utf-8")), dict)
        except (OSError, json.JSONDecodeError):
            manifest_valid = False
    plugin_ok = manifest_valid and mcp_config.is_file()
    return {
        "ready": runtime_ok and sqlite_fts and plugin_ok,
        "version": _VERSION,
        "runtime": {
            "ok": runtime_ok,
            "python": sys.version.split()[0],
            "minimum": "3.10",
        },
        "sqlite": {
            "ok": sqlite_fts,
            "version": sqlite3.sqlite_version,
            "fts5": sqlite_fts,
            "detail": sqlite_error,
        },
        "plugin": {
            "ok": plugin_ok,
            "manifest": {"path": str(plugin_manifest), "exists": plugin_manifest.is_file(), "valid_json": manifest_valid},
            "mcp_config": {"path": str(mcp_config), "exists": mcp_config.is_file()},
        },
        "hooks": {
            "status": "UNKNOWN",
            "detail": "Host hook registration was not checked by this local command.",
        },
    }


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="codex-mem", description="Local project memory for Codex")
    parser.add_argument("--data-dir", help="Directory containing the local codex-mem database and config")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="Run the newline-delimited MCP stdio server")
    commands.add_parser("hook", help="Run the local hook entrypoint")
    commands.add_parser("process-hook", help="Process captured observations from a native asynchronous hook")
    process = commands.add_parser("process", help="Process one observation batch in a fresh Luna/medium session")
    process.add_argument("--project", required=True)
    process.add_argument("--retry-failed", action="store_true", help="Explicitly retry a failed observation batch")
    process.add_argument("--timeout", type=lambda value: _positive(value, field="timeout", minimum=10, maximum=240), default=240)

    remember = commands.add_parser("remember", help="Store a project memory")
    remember.add_argument("--project", required=True)
    remember.add_argument("--title", required=True)
    remember.add_argument("--body", required=True)
    remember.add_argument("--kind", default="note")
    remember.add_argument("--session-id")
    remember.add_argument("--turn-id")
    remember.add_argument("--source")
    remember.add_argument("--tag", dest="tags", action="append", default=[])
    remember.add_argument("--dedupe-key")
    remember.add_argument("--source-id", dest="source_ids", action="append")
    remember.add_argument("--observation-type", "--type", dest="observation_type")
    remember.add_argument("--observation-subtitle", dest="observation_subtitle")
    remember.add_argument("--fact", "--facts", dest="facts", action="append")
    remember.add_argument("--concept", "--concepts", "--observation-concept", dest="concepts", action="append")
    remember.add_argument("--file-read", "--files-read", dest="files_read", action="append")
    remember.add_argument("--file-modified", "--files-modified", dest="files_modified", action="append")

    search = commands.add_parser("search", help="Search project memory previews")
    search.add_argument("--project", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=lambda value: _positive(value, field="limit", maximum=100), default=10)
    search.add_argument("--kind", dest="kinds", action="append")
    search.add_argument("--type", "--types", "--observation-type", dest="types", action="append")
    search.add_argument("--concept", "--concepts", dest="concepts", action="append")
    search.add_argument("--file", "--files", dest="files", action="append")
    search.add_argument("--intent", choices=("lookup", "resume"),
                        help="Prioritize relevant summaries and decisions for resume; returns retrieval metadata")
    search.add_argument("--mode", choices=("auto", "lexical", "semantic", "hybrid"),
                        help="Return results plus retrieval metadata; default automatically uses an available semantic index")
    search.add_argument("--detail", choices=("compact", "full"), default="compact",
                        help="Compact previews by default; full retains all preview metadata")

    semantic = commands.add_parser("semantic", help="Prepare and inspect the optional local semantic index")
    semantic_commands = semantic.add_subparsers(dest="semantic_command", required=True)
    semantic_commands.add_parser("setup", help="Explicitly download the pinned model; install optional runtime first")
    semantic_status = semantic_commands.add_parser("status", help="Show model readiness and index counts")
    semantic_status.add_argument("--project")
    semantic_index = semantic_commands.add_parser("index", help="Index one bounded batch of project records locally")
    semantic_index.add_argument("--project", required=True)
    semantic_index.add_argument("--retry-failed", action="store_true")

    service = commands.add_parser("service", help="Manage the local background queue worker")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for action in ("start", "run", "status"):
        service_commands.add_parser(action)
    stop = service_commands.add_parser("stop")
    stop.add_argument("--expected-owner", help="Stop only the verified owner from service status")
    for action in ("enqueue", "retry"):
        item = service_commands.add_parser(action)
        item.add_argument("--project", required=True)
    resume = service_commands.add_parser(
        "resume-pending", help="Resume later work while preserving an inspected rejected batch")
    resume.add_argument("--project", required=True)
    resume.add_argument("--rejected-job-id", required=True)
    recover = service_commands.add_parser(
        "recover-expired", help="Recover an inspected expired job without retrying rejected batches")
    recover.add_argument("--project", required=True)
    recover.add_argument("--job-id", required=True)

    usage = commands.add_parser("usage", help="Collect or inspect recorded session token usage; no cost estimates")
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_scan = usage_commands.add_parser("scan", help="Import a bounded batch of local usage metadata")
    usage_scan.add_argument("--codex-home", help="Codex state directory containing sessions")
    usage_scan.add_argument("--file", help="Scan one rollout inside the Codex sessions directories")
    usage_scan.add_argument("--max-files", type=lambda value: _positive(value, field="max_files", maximum=1000), default=32)
    usage_status = usage_commands.add_parser("status", help="Read token totals by task, agent and model")
    usage_status.add_argument("--project")
    usage_status.add_argument("--session-id", help="Root task/session ID")

    get = commands.add_parser("get", help="Read full project memory records")
    get.add_argument("--project", required=True)
    get.add_argument("--id", dest="id_values", action="append")
    get.add_argument("ids", nargs="*")

    tool_uses = commands.add_parser(
        "tool-uses",
        aliases=["get-tool-uses"],
        help="Read captured raw tool input/output evidence",
    )
    tool_uses.add_argument("--project", required=True)
    tool_uses.add_argument("--id", dest="id_values", action="append")
    tool_uses.add_argument("--ids", dest="id_arrays", action="append")
    tool_uses.add_argument("ids", nargs="*")
    tool_uses.add_argument("--session-id")
    tool_uses.add_argument(
        "--limit",
        type=lambda value: _positive(value, field="limit", maximum=100),
        default=10,
    )

    timeline = commands.add_parser("timeline", help="List recent project memory")
    timeline.add_argument("--project", required=True)
    timeline.add_argument("--session-id")
    timeline.add_argument("--limit", type=lambda value: _positive(value, field="limit", maximum=100), default=20)
    timeline.add_argument("--anchor-id", help="Exact memory ID; returns chronological neighbors")
    timeline.add_argument("--before", type=int, default=5, help="Earlier neighbors, requires anchor; total window at most 100")
    timeline.add_argument("--after", type=int, default=5, help="Later neighbors, requires anchor; recent --limit does not apply")
    timeline.add_argument("--detail", choices=("compact", "full"), default="compact",
                          help="Compact previews by default; full retains all preview metadata")

    context = commands.add_parser("context", help="Build a bounded memory context")
    context.add_argument("--project", required=True)
    context.add_argument("--query", default="")
    context.add_argument(
        "--budget",
        type=lambda value: _positive(value, field="budget", minimum=128, maximum=6_000),
        default=6_000,
    )
    context.add_argument("--exclude-session")
    context.add_argument("--kind", dest="kinds", action="append")
    context.add_argument("--type", "--types", "--observation-type", dest="types", action="append")
    context.add_argument("--concept", "--concepts", dest="concepts", action="append")
    context.add_argument("--file", "--files", dest="files", action="append")

    status = commands.add_parser("status", help="Inspect local memory-store status")
    status.add_argument("--project")

    forget = commands.add_parser("forget", help="Permanently delete memory records")
    forget.add_argument("--project", required=True)
    forget.add_argument("--id", dest="id_values", action="append")
    forget.add_argument("ids", nargs="*")

    backup = commands.add_parser("backup", help="Create a local database backup")
    backup.add_argument("path", nargs="?")
    backup.add_argument("--path", dest="backup_path")

    prune = commands.add_parser("prune", help="Delete records older than a retention period")
    prune.add_argument(
        "--days",
        type=lambda value: _positive(value, field="days", minimum=0, maximum=3_650),
        default=90,
    )

    config = commands.add_parser("config", help="Show or update local capture configuration")
    config.add_argument("--set", dest="set_values", action="append", default=[])
    config.add_argument("--capture-enabled", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--capture-tools", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--processor-enabled", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--service-enabled", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--semantic-enabled", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--usage-enabled", action=argparse.BooleanOptionalAction, default=None)
    config.add_argument("--context-chars", type=lambda value: _positive(value, field="context_chars", maximum=6_000))
    config.add_argument("--exclude-project", dest="excluded_projects", action="append")
    config.add_argument("--include-project", dest="included_projects", action="append")
    config.add_argument("--skip-tool", "--skip-tools", dest="skip_tools", action="append")
    config.add_argument(
        "--scope",
        "--capture-scope",
        dest="capture_scope",
        choices=("selected", "all", "manual"),
        help="Automatic capture scope: selected, all, or manual",
    )

    importer = commands.add_parser("import-claude", help="Preview or import legacy Claude memory records")
    importer.add_argument("--database", required=True, help="Path to the legacy Claude memory SQLite database")
    importer.add_argument("--project", required=True, help="Absolute destination project path")
    importer.add_argument("--legacy-project", required=True, help="Legacy project identifier to import")
    importer.add_argument("--apply", action="store_true", help="Write the import; without this flag the command is a dry run")
    importer.add_argument(
        "--limit",
        type=lambda value: _positive(value, field="limit", maximum=100_000),
        default=1_000,
    )

    commands.add_parser("doctor", help="Check local runtime, SQLite, and plugin files")
    return parser


def _normalize_global_data_dir(arguments: Sequence[str]) -> list[str]:
    """Accept --data-dir before or after a subcommand without duplicate parsers."""
    remaining: list[str] = []
    data_dir: str | None = None
    index = 0
    values = list(arguments)
    while index < len(values):
        value = values[index]
        if value == "--data-dir":
            if index + 1 >= len(values):
                raise CLIError("--data-dir requires a value")
            data_dir = values[index + 1]
            index += 2
            continue
        if value.startswith("--data-dir="):
            data_dir = value.split("=", 1)[1]
            index += 1
            continue
        remaining.append(value)
        index += 1
    return (["--data-dir", data_dir] if data_dir is not None else []) + remaining


def _run_store_command(namespace: argparse.Namespace) -> Any:
    command = namespace.command
    if command == "import-claude":
        from .importer import import_claude_mem

        project = _absolute_project(namespace.project)
        import_args = {
            "database": Path(namespace.database),
            "project": project,
            "legacy_project": namespace.legacy_project,
            "dry_run": not namespace.apply,
            "limit": namespace.limit,
        }
        # A preview reads the legacy database only. Constructing Store would
        # initialize a destination SQLite file, which would make "dry run"
        # misleading even though importer itself does not call remember.
        if not namespace.apply:
            return import_claude_mem(None, **import_args)
        with Store(namespace.data_dir) as store:
            return import_claude_mem(store, **import_args)

    # Reject malformed project selectors before opening the local DB for a
    # command that otherwise has no reason to create or touch it.
    if command in {"remember", "search", "get", "get-tool-uses", "tool-uses", "timeline", "context", "forget"}:
        _absolute_project(namespace.project)
    elif command == "status" and namespace.project is not None:
        _absolute_project(namespace.project)
    if command in {"get", "forget"}:
        _collect_ids(namespace)

    with Store(namespace.data_dir) as store:
        if command == "remember":
            observation = _observation_metadata(namespace)
            remember_kwargs: dict[str, Any] = {
                "kind": namespace.kind,
                "session_id": namespace.session_id,
                "turn_id": namespace.turn_id,
                "source": namespace.source,
                "tags": namespace.tags or None,
                "dedupe_key": namespace.dedupe_key,
                "source_ids": namespace.source_ids,
            }
            if observation is not None:
                remember_kwargs["observation"] = observation
            result = store.remember(
                _absolute_project(namespace.project),
                namespace.title,
                namespace.body,
                **remember_kwargs,
            )
            from .integration import after_write
            result["background"] = after_write(namespace.project, namespace.data_dir)
            return result
        if command == "search":
            from .integration import search_memory
            result = search_memory(
                store,
                _absolute_project(namespace.project),
                namespace.query,
                mode=namespace.mode or "auto",
                intent=namespace.intent or "lookup",
                limit=namespace.limit,
                kinds=namespace.kinds,
                types=_filter_values(namespace.types, field="types"),
                concepts=_filter_values(namespace.concepts, field="concepts"),
                files=_filter_values(namespace.files, field="files"),
            )
            from .retrieval import preview_records
            result["results"] = preview_records(result["results"], detail=namespace.detail)
            return result if namespace.mode or namespace.intent else result["results"]
        if command in {"get-tool-uses", "tool-uses"}:
            return bound_tool_uses(
                store.get_tool_uses(
                    _absolute_project(namespace.project),
                    ids=_collect_optional_ids(namespace),
                    session_id=namespace.session_id,
                    limit=namespace.limit,
                )
            )
        if command == "get":
            return store.get(_absolute_project(namespace.project), _collect_ids(namespace))
        if command == "timeline":
            from .retrieval import preview_records
            records = store.timeline(
                _absolute_project(namespace.project),
                session_id=namespace.session_id,
                limit=namespace.limit,
                anchor_id=namespace.anchor_id,
                before=namespace.before,
                after=namespace.after,
            )
            return preview_records(records, detail=namespace.detail)
        if command == "context":
            context_kwargs: dict[str, Any] = {
                "query": namespace.query,
                "budget": namespace.budget,
                "exclude_session": namespace.exclude_session,
            }
            for name in ("kinds", "types", "concepts", "files"):
                values = getattr(namespace, name, None)
                if values is not None:
                    context_kwargs[name] = _filter_values(values, field=name)
            return {
                "context": store.context(
                    _absolute_project(namespace.project),
                    **context_kwargs,
                )
            }
        if command == "status":
            return store.status(
                project=_absolute_project(namespace.project) if namespace.project is not None else None
            )
        if command == "forget":
            return store.forget(_absolute_project(namespace.project), _collect_ids(namespace))
        if command == "backup":
            path = namespace.backup_path or namespace.path
            if not path:
                raise CLIError("backup path is required")
            return store.backup(path)
        if command == "prune":
            return store.prune(days=namespace.days)
    raise AssertionError(f"not a store command: {command}")


def main(args: Sequence[str] | None = None) -> int:
    """Run the CLI and return a conventional process exit code."""
    arguments = list(sys.argv[1:] if args is None else args)
    try:
        namespace = _build_parser().parse_args(_normalize_global_data_dir(arguments))
        if namespace.command == "serve":
            from .mcp import serve

            return serve(namespace.data_dir)
        if namespace.command == "hook":
            from . import hooks

            return hooks.main(data_dir=namespace.data_dir)
        if namespace.command == "process-hook":
            from . import hooks

            return hooks.process_hook_main(data_dir=namespace.data_dir)
        if namespace.command == "process":
            from .processor import process_pending

            value = process_pending(_absolute_project(namespace.project), namespace.data_dir,
                                    retry_failed=namespace.retry_failed, timeout=namespace.timeout)
            _emit(value)
            return 2 if value.get("status") == "failed" else 0
        if namespace.command == "service":
            from .service import recover_expired, resume_pending, run_service, service_status, start_service, stop_service
            from .integration import enqueue_project, index_project
            action = namespace.service_command
            if action == "run":
                from .usage import UsageCollector
                collector = UsageCollector(namespace.data_dir)
                value = run_service(namespace.data_dir, indexer=index_project,
                                    usage_collector=collector.collect)
            elif action == "start":
                value = start_service(namespace.data_dir)
            elif action == "stop":
                value = stop_service(namespace.data_dir, expected_owner=namespace.expected_owner)
            elif action == "status":
                value = service_status(namespace.data_dir)
            elif action == "resume-pending":
                value = resume_pending(_absolute_project(namespace.project), namespace.data_dir,
                                       rejected_job_id=namespace.rejected_job_id)
            elif action == "recover-expired":
                value = recover_expired(_absolute_project(namespace.project), namespace.data_dir,
                                        job_id=namespace.job_id)
            else:
                value = enqueue_project(_absolute_project(namespace.project), namespace.data_dir,
                                        retry_failed=action == "retry")
            _emit(value)
            return 2 if value.get("status") in {"failed", "error", "blocked", "halted", "unknown", "unavailable"} else 0
        if namespace.command == "usage":
            if namespace.usage_command == "scan":
                from .usage import UsageCollector
                collector = UsageCollector(namespace.data_dir, codex_home=namespace.codex_home)
                value = (collector.scan_file(namespace.file) if namespace.file
                         else collector.collect(max_files=namespace.max_files))
            else:
                from .usage_store import UsageStore
                with UsageStore(namespace.data_dir) as usage_store:
                    totals = usage_store.usage_totals(
                        project=_absolute_project(namespace.project) if namespace.project else None,
                        session_id=namespace.session_id,
                    )
                    value = {"status": "ok", "records": totals,
                             "note": "Recorded token usage only; cached input and reasoning output are subsets."}
            _emit(value)
            return 2 if value.get("status") in {"failed", "error", "unavailable"} else 0
        if namespace.command == "semantic":
            from .semantic import prepare_model, semantic_status, index_pending
            if namespace.semantic_command == "setup":
                value = prepare_model()
            elif namespace.semantic_command == "index":
                value = index_pending(_absolute_project(namespace.project), namespace.data_dir,
                                      retry_failed=namespace.retry_failed)
            else:
                if namespace.project is not None:
                    _absolute_project(namespace.project)
                with Store(namespace.data_dir) as store:
                    value = {"model": semantic_status(), "index": store.embedding_status(namespace.project)}
            _emit(value)
            return 2 if value.get("status") in {"failed", "error", "unavailable"} else 0
        if namespace.command == "config":
            from .config import configure, load_config

            updates = _config_updates(namespace)
            value = configure(namespace.data_dir, **updates) if updates else load_config(namespace.data_dir)
        elif namespace.command == "doctor":
            value = _doctor()
        else:
            value = _run_store_command(namespace)
    except CLIError as exc:
        return _error("invalid_arguments", str(exc))
    except SemanticError as exc:
        return _error("semantic_unavailable", exc.code)
    except ServiceError:
        return _error("service_unavailable", "Background service operation failed")
    except (StoreError, OSError):
        return _error("store_error", "Memory operation failed")
    except ValueError:
        # Config and store validation may have processed private command content.
        return _error("invalid_arguments", "Invalid arguments")
    _emit(value)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    raise SystemExit(main())
