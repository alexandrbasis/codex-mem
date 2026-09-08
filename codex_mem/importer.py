"""Read-only migration of selected Claude-Mem records into Codex Mem.

The importer is deliberately a narrow boundary.  It accepts an explicit legacy
database path and an exact legacy project value, opens SQLite in ``mode=ro``,
and only knows the two upstream tables that contain already-compressed content:
``observations`` and ``session_summaries``.  It never discovers or migrates a
host corpus on its own.

The destination store owns secret redaction and validation.  This module keeps
the source text intact while constructing a small, labelled envelope that says
legacy assistant claims are unverified.  Stable
``origin:legacy-project:table:id:fingerprint`` keys make a second apply
idempotent through :meth:`Store.remember`'s ``dedupe_key`` while distinguishing
copied databases whose same row id contains different content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .privacy import redact_text


__all__ = ["ClaudeMemImportError", "import_claude_mem"]


ORIGIN = "claude-mem"
DEFAULT_LIMIT = 1_000
MAX_LIMIT = 100_000
FETCH_BATCH_SIZE = 100
MAX_IMPORTED_TITLE = 500
MAX_IMPORTED_BODY = 90_000
MAX_DESTINATION_SESSION = 256
MAX_SOURCE_KEY = 512


class ClaudeMemImportError(ValueError):
    """A safe, actionable error for an unsupported or incomplete legacy DB."""


@dataclass(frozen=True)
class _TableSpec:
    name: str
    content_fields: tuple[str, ...]
    aliases: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class _TablePlan:
    spec: _TableSpec
    columns: dict[str, str]
    values: dict[str, str]


@dataclass(frozen=True)
class _ImportRecord:
    table: str
    row_id: str
    kwargs: dict[str, Any]


_SESSION_ALIASES = (
    "content_session_id",
    "claude_session_id",
    "memory_session_id",
    "sdk_session_id",
    "session_id",
)


_COMMON_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("session_id", _SESSION_ALIASES),
    ("prompt_number", ("prompt_number",)),
    ("created_at", ("created_at",)),
    ("created_at_epoch", ("created_at_epoch",)),
)


_TABLE_SPECS = (
    _TableSpec(
        name="observations",
        content_fields=(
            "title",
            "subtitle",
            "narrative",
            "text",
            "compressed_observation",
            "facts",
            "concepts",
        ),
        aliases=(
            ("title", ("title",)),
            ("subtitle", ("subtitle",)),
            ("type", ("type", "obs_type")),
            ("tool_name", ("tool_name",)),
            ("narrative", ("narrative",)),
            ("text", ("text",)),
            ("compressed_observation", ("compressed_observation",)),
            ("facts", ("facts",)),
            ("concepts", ("concepts",)),
            ("files_read", ("files_read", "files_touched")),
            ("files_modified", ("files_modified", "files_edited")),
            *_COMMON_ALIASES,
            ("correlation_id", ("correlation_id",)),
        ),
    ),
    _TableSpec(
        name="session_summaries",
        content_fields=(
            "request",
            "investigated",
            "learned",
            "completed",
            "next_steps",
            "notes",
            "summary_text",
            "facts",
            "concepts",
        ),
        aliases=(
            ("request", ("request",)),
            ("investigated", ("investigated",)),
            ("learned", ("learned",)),
            ("completed", ("completed",)),
            ("next_steps", ("next_steps",)),
            ("notes", ("notes",)),
            ("summary_text", ("summary_text", "summary", "text")),
            ("facts", ("facts",)),
            ("concepts", ("concepts",)),
            ("files_read", ("files_read",)),
            ("files_edited", ("files_edited",)),
            ("files_touched", ("files_touched",)),
            *_COMMON_ALIASES,
        ),
    ),
)


_KIND_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ID_RE = re.compile(r"[\x00\r\n]")


def _identifier(value: str) -> str:
    """Quote a SQLite identifier discovered through ``PRAGMA table_info``."""

    return '"' + value.replace('"', '""') + '"'


def _database_path(database: Path) -> Path:
    if not isinstance(database, Path):
        try:
            database = Path(database)
        except (TypeError, ValueError) as exc:
            raise ClaudeMemImportError("database must be a readable SQLite file path") from exc
    if not database.exists() or not database.is_file():
        raise ClaudeMemImportError(f"legacy database does not exist or is not a file: {database}")
    try:
        return database.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ClaudeMemImportError(
            f"legacy database path could not be resolved: {database}"
        ) from exc


def _open_read_only(database: Path) -> sqlite3.Connection:
    """Open an existing database without allowing SQLite to create or write it."""

    try:
        # ``as_uri`` escapes spaces and punctuation correctly.  ``mode=ro`` is
        # the important part: a typo or missing file must fail instead of
        # creating a new database beside the requested source.
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        # This is a connection-local guard, not a mutation of the source DB.
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise ClaudeMemImportError(
            f"could not open legacy database read-only: {database}"
        ) from exc


def _table_names(connection: sqlite3.Connection) -> set[str]:
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise ClaudeMemImportError("could not inspect legacy SQLite tables") from exc
    return {str(row[0]).lower() for row in rows}


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
    except sqlite3.Error as exc:
        raise ClaudeMemImportError(f"could not inspect the {table} table schema") from exc
    return {str(row[1]).lower(): str(row[1]) for row in rows}


def _build_plan(connection: sqlite3.Connection, spec: _TableSpec) -> _TablePlan:
    columns = _table_columns(connection, spec.name)
    missing: list[str] = []
    if "id" not in columns:
        missing.append("id")
    if "project" not in columns:
        missing.append("project")

    values: dict[str, str] = {}
    for canonical, aliases in spec.aliases:
        for alias in aliases:
            actual = columns.get(alias.lower())
            if actual is not None:
                values[canonical] = actual
                break

    if not any(field in values for field in spec.content_fields):
        supported = ", ".join(spec.content_fields)
        missing.append(f"at least one supported content column ({supported})")

    if missing:
        supported_columns = ", ".join(sorted(columns.values())) or "<none>"
        missing_text = "; ".join(missing)
        raise ClaudeMemImportError(
            f"unsupported {spec.name} schema: missing {missing_text}; "
            f"found columns: {supported_columns}. "
            "Use a supported Claude-Mem SQLite schema containing id, project, "
            "and compressed observation/summary fields."
        )
    return _TablePlan(spec=spec, columns=columns, values=values)


def _text(value: Any) -> str:
    """Convert a legacy SQLite value to text without performing redaction."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    elif not isinstance(value, str):
        value = str(value)
    # NUL cannot be persisted by the destination Store.  Replacing this one
    # transport-invalid character is structural handling, not secret cleanup.
    return value.replace("\x00", "�")


def _row_values(row: sqlite3.Row, plan: _TablePlan) -> dict[str, str]:
    values: dict[str, str] = {}
    for canonical, actual in plan.values.items():
        # Sanitize every source field before title/body construction or any
        # head+tail clipping.  Store.remember sanitizes again defensively; the
        # first pass prevents a clipped tail from exposing a secret that began
        # beyond a title/body budget.
        values[canonical] = redact_text(_text(row[actual]))
    return values


def _row_id(row: sqlite3.Row, plan: _TablePlan) -> str:
    actual = plan.columns["id"]
    row_id = _text(row[actual]).strip()
    if not row_id:
        raise ClaudeMemImportError(
            f"{plan.spec.name} contains a row with an empty id; cannot build a stable "
            "origin/table/id dedupe key"
        )
    if _ID_RE.search(row_id):
        raise ClaudeMemImportError(
            f"{plan.spec.name} row id contains a line break or NUL; cannot build a stable "
            "dedupe key"
        )
    return row_id


def _legacy_scope(legacy_project: str) -> str:
    """Return a bounded, provenance-friendly identity for the source project.

    The exact project value participates in the digest, so two source projects
    cannot collide even when their display forms are truncated.  Short values
    remain readable in diagnostics; redaction protects a project string that
    accidentally contains a credential while the digest preserves identity.
    """

    digest = hashlib.sha256(legacy_project.encode("utf-8")).hexdigest()
    display = redact_text(legacy_project).replace("\\", "\\\\")
    display = display.replace("\r", "\\r").replace("\n", "\\n")
    if len(display) <= 160:
        return f"{display}~{digest[:16]}"
    return f"sha256-{digest}"


def _content_fingerprint(table: str, values: Mapping[str, str]) -> str:
    """Hash mapped, already-redacted content in deterministic field order."""

    payload = {
        "table": table,
        "fields": [[field, values[field]] for field in sorted(values)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_key(
    *,
    table: str,
    row_id: str,
    legacy_project: str,
    values: Mapping[str, str],
) -> str:
    fingerprint = _content_fingerprint(table, values)
    key = f"{ORIGIN}:{_legacy_scope(legacy_project)}:{table}:{row_id}:{fingerprint}"
    if len(key) > MAX_SOURCE_KEY:
        raise ClaudeMemImportError(
            f"{table} row id {row_id[:80]!r} is too long for a stable dedupe key; "
            "shorten the legacy identifier or migrate it manually"
        )
    return key


def _nonempty(values: Mapping[str, str], field: str) -> str:
    return values.get(field, "").strip()


def _kind(values: Mapping[str, str], table: str) -> str:
    if table == "session_summaries":
        return "legacy-summary"
    raw = _nonempty(values, "type") or "legacy-observation"
    value = _KIND_RE.sub("-", raw.strip().lower()).strip("._-")
    if not value or not value[0].isalnum():
        return "legacy-observation"
    return value[:64]


def _title(values: Mapping[str, str], table: str, row_id: str) -> str:
    if table == "observations":
        candidate = _nonempty(values, "title") or _nonempty(values, "tool_name")
        fallback = f"Legacy Claude-Mem observation {row_id}"
    else:
        candidate = _nonempty(values, "request")
        fallback = f"Legacy Claude-Mem session summary {row_id}"
    if candidate:
        # A request can contain several lines; the first line is a useful,
        # bounded title while the complete request remains in the body.
        candidate = candidate.splitlines()[0].strip() or fallback
    else:
        candidate = fallback
    return _bounded(candidate, MAX_IMPORTED_TITLE)


_DISPLAY_LABELS = {
    "tool_name": "Tool",
    "type": "Type",
    "prompt_number": "Prompt number",
    "session_id": "Legacy session ID",
    "created_at": "Created at",
    "created_at_epoch": "Created at epoch",
    "correlation_id": "Correlation ID",
    "summary_text": "Summary",
    "files_read": "Files read",
    "files_modified": "Files modified",
    "files_edited": "Files edited",
    "files_touched": "Files touched",
}


def _label(field: str) -> str:
    return _DISPLAY_LABELS.get(field, field.replace("_", " ").capitalize())


def _bounded(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    marker = "\n[… legacy field truncated …]\n"
    if maximum <= len(marker) + 2:
        return value[:maximum]
    head = (maximum - len(marker)) // 2
    tail = maximum - len(marker) - head
    return value[:head] + marker + value[-tail:]


def _body(
    values: Mapping[str, str],
    *,
    table: str,
    row_id: str,
    legacy_project: str,
    source_key: str,
) -> str:
    lines = [
        "Legacy Claude-Mem record.",
        "Legacy assistant-generated claims are unverified; verify against current "
        "project state before relying on this record.",
        f"Source: {source_key}",
        f"Legacy project: {redact_text(legacy_project)}",
        "",
    ]

    # Keep the source field order stable.  This makes reruns easy to compare and
    # avoids relying on SQLite's physical column order across schema versions.
    for field, value in values.items():
        if not value.strip() or field == "session_id":
            continue
        lines.append(f"{_label(field)}: {value}")
    session_id = _nonempty(values, "session_id")
    if session_id:
        lines.insert(3, f"Legacy session ID: {session_id}")
    return _bounded("\n".join(lines).strip(), MAX_IMPORTED_BODY)


def _record(
    row: sqlite3.Row,
    plan: _TablePlan,
    *,
    legacy_project: str,
    warnings: list[str],
) -> _ImportRecord:
    row_id = _row_id(row, plan)
    values = _row_values(row, plan)
    if not any(_nonempty(values, field) for field in plan.spec.content_fields):
        supported = ", ".join(plan.spec.content_fields)
        raise ClaudeMemImportError(
            f"{plan.spec.name} row {row_id} has no usable compressed content in "
            f"supported fields ({supported}); repair the source or omit that row explicitly"
        )

    session_id = _nonempty(values, "session_id") or None
    if session_id is not None and len(session_id) > MAX_DESTINATION_SESSION:
        warnings.append(
            f"{plan.spec.name} row {row_id}: legacy session id exceeded "
            f"{MAX_DESTINATION_SESSION} characters and was omitted from destination metadata"
        )
        session_id = None

    source_key = _source_key(
        table=plan.spec.name,
        row_id=row_id,
        legacy_project=legacy_project,
        values=values,
    )
    kwargs: dict[str, Any] = {
        "title": _title(values, plan.spec.name, row_id),
        "body": _body(
            values,
            table=plan.spec.name,
            row_id=row_id,
            legacy_project=legacy_project,
            source_key=source_key,
        ),
        "kind": _kind(values, plan.spec.name),
        "session_id": session_id,
        "turn_id": None,
        "source": source_key,
        "tags": ["legacy", ORIGIN, "unverified", plan.spec.name],
        "dedupe_key": source_key,
    }
    return _ImportRecord(table=plan.spec.name, row_id=row_id, kwargs=kwargs)


def _count(connection: sqlite3.Connection, plan: _TablePlan, legacy_project: str) -> int:
    table = _identifier(plan.spec.name)
    project = _identifier(plan.columns["project"])
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {project} = ?",
            (legacy_project,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ClaudeMemImportError(
            f"could not count {plan.spec.name} rows for the exact legacy project"
        ) from exc
    return int(row[0]) if row is not None else 0


def _read_records(
    connection: sqlite3.Connection,
    plan: _TablePlan,
    *,
    legacy_project: str,
    remaining: int,
    warnings: list[str],
) -> tuple[list[_ImportRecord], int]:
    if remaining <= 0:
        return [], 0
    table = _identifier(plan.spec.name)
    project = _identifier(plan.columns["project"])
    identifier = _identifier(plan.columns["id"])
    records: list[_ImportRecord] = []
    offset = 0
    while len(records) < remaining:
        batch_limit = min(FETCH_BATCH_SIZE, remaining - len(records))
        try:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {project} = ? "
                f"ORDER BY {identifier} ASC LIMIT ? OFFSET ?",
                (legacy_project, batch_limit, offset),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ClaudeMemImportError(
                f"could not read {plan.spec.name} rows for the exact legacy project"
            ) from exc
        if not rows:
            break
        for row in rows:
            records.append(
                _record(row, plan, legacy_project=legacy_project, warnings=warnings)
            )
        offset += len(rows)
        if len(rows) < batch_limit:
            break
    return records, len(records)


def _validate_inputs(
    database: Path,
    project: str,
    legacy_project: str,
    dry_run: bool,
    limit: int,
) -> tuple[Path, str, str, bool, int]:
    source = _database_path(database)
    if not isinstance(project, str) or not project.strip() or "\x00" in project:
        raise ClaudeMemImportError("project must be a non-empty destination project string")
    if (
        not isinstance(legacy_project, str)
        or not legacy_project
        or not legacy_project.strip()
        or "\x00" in legacy_project
    ):
        raise ClaudeMemImportError(
            "legacy_project must be the exact non-empty project value stored in the source DB"
        )
    if not isinstance(dry_run, bool):
        raise ClaudeMemImportError("dry_run must be a boolean")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ClaudeMemImportError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return source, project, legacy_project, dry_run, limit


def import_claude_mem(
    store: Any,
    database: Path,
    project: str,
    legacy_project: str,
    dry_run: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Import selected compressed Claude-Mem rows into ``store``.

    ``database`` is always explicit and is opened with SQLite's read-only URI
    mode.  ``legacy_project`` is bound as an equality parameter; no basename,
    prefix, wildcard, or descendant matching is performed.  ``limit`` is a
    global row budget shared by observations and summaries, read in batches of
    at most :data:`FETCH_BATCH_SIZE`.

    A dry run validates the source schema and rows and returns counts without
    calling the destination store.  An apply calls ``store.remember`` once per
    validated row using a stable key scoped by the exact legacy project and a
    deterministic content fingerprint.  The destination store is responsible
    for final secret sanitation after this module sanitizes before clipping.
    """

    source, destination_project, exact_project, preview, row_limit = _validate_inputs(
        database, project, legacy_project, dry_run, limit
    )

    connection = _open_read_only(source)
    warnings: list[str] = []
    records: list[_ImportRecord] = []
    by_table: dict[str, dict[str, int]] = {
        spec.name: {
            "available": 0,
            "scanned": 0,
            "imported": 0,
            "would_import": 0,
            "already_imported": 0,
            "skipped": 0,
        }
        for spec in _TABLE_SPECS
    }
    try:
        names = _table_names(connection)
        plans: list[_TablePlan] = []
        for spec in _TABLE_SPECS:
            if spec.name in names:
                plans.append(_build_plan(connection, spec))
        if not plans:
            known = ", ".join(spec.name for spec in _TABLE_SPECS)
            raise ClaudeMemImportError(
                f"legacy database has no supported Claude-Mem tables; expected {known}. "
                "Pass the actual Claude-Mem SQLite database explicitly."
            )

        remaining = row_limit
        for plan in plans:
            table_result = by_table[plan.spec.name]
            available = _count(connection, plan, exact_project)
            table_result["available"] = available
            if available > remaining:
                warnings.append(
                    f"{plan.spec.name}: limit {row_limit} reached; "
                    f"{available - remaining} exact-project rows were left for a later run"
                )
            batch, scanned = _read_records(
                connection,
                plan,
                legacy_project=exact_project,
                remaining=remaining,
                warnings=warnings,
            )
            records.extend(batch)
            table_result["scanned"] = scanned
            table_result["skipped"] = max(0, available - scanned)
            remaining -= scanned
    finally:
        connection.close()

    imported = 0
    already_imported = 0
    if not preview:
        for record in records:
            try:
                result = store.remember(destination_project, **record.kwargs)
            except Exception as exc:
                raise ClaudeMemImportError(
                    f"could not import {record.table} row {record.row_id}: {exc}"
                ) from exc
            deduplicated = isinstance(result, Mapping) and bool(result.get("deduplicated"))
            if deduplicated:
                already_imported += 1
                by_table[record.table]["already_imported"] += 1
            else:
                imported += 1
                by_table[record.table]["imported"] += 1
    else:
        for record in records:
            by_table[record.table]["would_import"] += 1

    candidates = len(records)
    return {
        "ok": True,
        "dry_run": preview,
        "database": str(source),
        "project": destination_project,
        "legacy_project": exact_project,
        "limit": row_limit,
        "available": sum(item["available"] for item in by_table.values()),
        "scanned": candidates,
        "candidates": candidates,
        "imported": imported,
        "would_import": candidates if preview else 0,
        "already_imported": already_imported,
        "skipped": sum(item["skipped"] for item in by_table.values()),
        "by_table": by_table,
        "warnings": warnings,
    }
