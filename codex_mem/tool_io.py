"""Durable, privacy-filtered raw tool I/O helpers.

The normal memory entry is intentionally a short, reader-facing excerpt.  A
tool call can still contain useful evidence outside that excerpt, so hooks make
one best-effort side-index write for the original *redacted* JSON payload.  The
side index is an audit/readback surface only: its replay helpers produce an
inert plan and never invoke a command or tool.

This module does not own a :class:`~codex_mem.store.Store` instance.  The store
creates the schema and calls :func:`insert_capture` while its entry transaction
is open.  Keeping the connection-level API here lets a partially upgraded
plugin continue to capture normal entries if this optional side index is not
available yet.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any

from .privacy import REDACTED, redact_text


# The upstream tool-use side index uses a 64 KiB soft cap.  Keep the limit in
# bytes rather than Python characters so multi-byte payloads cannot quietly
# grow the durable row beyond the promised bound.
MAX_TOOL_PAYLOAD_BYTES = 64 * 1024
MAX_TOOL_EXCERPT_CHARS = 2_000
MAX_TOOL_NAME_CHARS = 256
MAX_SESSION_CHARS = 256
MAX_PROJECT_CHARS = 4_096
MAX_REPLAY_ITEMS = 100
_TOOL_USE_COLUMNS = (
    "id",
    "entry_id",
    "project",
    "session_id",
    "turn_id",
    "tool_use_id",
    "tool_name",
    "tool_input",
    "tool_response",
    "input_excerpt",
    "response_excerpt",
    "input_metadata_json",
    "response_metadata_json",
    "content_hash",
    "created_at",
    "updated_at",
    "cwd",
)

_PRIVATE_MARKER_RE = re.compile(
    r"<\s*(?:private|secret|sensitive)\b|"
    r"\[\s*(?:private|secret|sensitive)\s*\]",
    re.IGNORECASE,
)
_SENSITIVE_PATH_RE = re.compile(
    r"(?i)^(?:\.env(?:[._-].*)?|credentials?(?:[._-].*)?|"
    r"passwords?(?:[._-].*)?|secrets?(?:[._-].*)?|"
    r"(?:id_(?:rsa|dsa|ecdsa|ed25519)|authorized_keys|known_hosts)(?:[._-].*)?|"
    r".*private[_-]?key.*|.*\.(?:pem|key|p12|pfx))$"
)
_PATH_KEYS = {
    "path",
    "paths",
    "file",
    "files",
    "filepath",
    "file_path",
    "filename",
    "file_name",
    "notebookpath",
    "notebook_path",
    "affected_paths",
    "affectedpaths",
    "changed_files",
    "changedfiles",
    "cwd",
    "working_directory",
    "workdir",
}
_MEDIA_TYPES = {
    "audio",
    "image",
    "video",
    "blob",
    "binary",
    "file",
    "filecontent",
    "file_content",
}
_BASE64_DATA_RE = re.compile(r"^[A-Za-z0-9+/=_-]{256,}$")
_MEDIA_DATA_URI_RE = re.compile(
    r"(?i)^data:(?:image|audio|video)/[^;,]+(?:;[^,]*)?;base64,"
    r"[A-Za-z0-9+/=_-]*$|^data:application/(?:octet-stream|pdf|zip)"
    r"(?:;[^,]*)?;base64,[A-Za-z0-9+/=_-]*$"
)
_MEMORY_TOOL_NAMES = {
    "search",
    "timeline",
    "get_observations",
    "get_tool_uses",
    "session_start_context",
    "observation_search",
    "memory_search",
    "memory_timeline",
    "memory_get",
    "memory_remember",
    "memory_consolidate",
}
_SKIP_CONFIG_KEYS = (
    "tool_skip_list",
    "skip_tools",
)
_OPAQUE = object()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\x00" in value:
        return None
    return redact_text(value)[:maximum].strip() or None


def _identifier(value: Any, maximum: int) -> str | None:
    return _text(value, maximum)


def _cwd_identifier(value: Any) -> str | None:
    text = _identifier(value, MAX_PROJECT_CHARS)
    return _redact_sensitive_path_tokens(text) if text else None


def _is_sensitive_path(value: str) -> bool:
    stripped = value.strip().strip("'\"`,;:()[]{}")
    if not stripped or any(char.isspace() for char in stripped):
        return False
    pieces = re.split(r"[/\\]", stripped)
    # A bare ordinary word is not a path.  This keeps a tool's normal text
    # response from being mistaken for a private filename.
    if len(pieces) == 1 and not (
        _SENSITIVE_PATH_RE.fullmatch(stripped)
        or stripped.startswith(".")
        or "." in stripped
        or stripped in {"secret", "secrets"}
    ):
        return False
    return any(
        _SENSITIVE_PATH_RE.fullmatch(piece)
        or piece.casefold() in {".ssh", ".aws", ".gnupg", ".kube"}
        for piece in pieces
    )


def _private_marker(value: str) -> bool:
    return bool(_PRIVATE_MARKER_RE.search(value))


def is_private_prompt(value: object) -> bool:
    """Return whether a prompt explicitly opens a private block.

    The check intentionally happens on the original prompt before
    :func:`redact_text` replaces its body.  It recognizes an unclosed opener,
    matching the privacy module's fail-closed treatment of interrupted input.
    """

    return isinstance(value, str) and _private_marker(value)


def _is_memory_tool(tool_name: str) -> bool:
    lowered = tool_name.strip().lower()
    if not lowered:
        return False
    if "codex_mem" in lowered or "codex-mem" in lowered:
        return True
    if lowered.startswith("memory_") or "__memory_" in lowered:
        return True
    if lowered.startswith("mcp__"):
        parts = lowered.split("__")
        if len(parts) >= 3:
            server = parts[1]
            tool = "__".join(parts[2:])
            if any(
                marker in server
                for marker in (
                    "claude-mem",
                    "claude_mem",
                    "mcp-search",
                    "cmem",
                    "codex-mem",
                    "codex_mem",
                    "memory",
                )
            ):
                return tool in _MEMORY_TOOL_NAMES or tool.startswith("memory_")
            # A local Codex memory server is also commonly exposed as
            # ``mcp__codex__memory_*``.  Keep this exact tool-family exclusion
            # narrow so unrelated MCP tools remain capturable.
            if tool in _MEMORY_TOOL_NAMES or tool.startswith("memory_"):
                return server in {"codex", "claude", "memory"}
    return False


def _skip_tool(tool_name: str, config: Mapping[str, Any] | None) -> bool:
    if _is_memory_tool(tool_name):
        return True
    if not config:
        return False
    configured: list[str] = []
    for key in _SKIP_CONFIG_KEYS:
        value = config.get(key)
        if isinstance(value, str):
            configured.extend(part.strip() for part in value.split(","))
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            configured.extend(item.strip() for item in value if isinstance(item, str))
    lowered = tool_name.casefold()
    return any(item and item.casefold() == lowered for item in configured)


def _contains_session_memory(value: Any, *, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, str):
        lowered = value.casefold()
        return "session-memory" in lowered or "session_memory" in lowered
    if isinstance(value, Mapping):
        return any(
            _contains_session_memory(child, depth=depth + 1)
            for child in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_session_memory(child, depth=depth + 1) for child in value)
    return False


def _looks_like_media(value: Mapping[str, Any]) -> bool:
    raw_type = value.get("type")
    if isinstance(raw_type, str) and raw_type.casefold() in _MEDIA_TYPES:
        return True
    for key in ("mimeType", "mime_type", "contentType", "content_type"):
        raw_mime = value.get(key)
        if isinstance(raw_mime, str) and (
            raw_mime.casefold().startswith(("image/", "audio/", "video/"))
            or raw_mime.casefold() in {"application/octet-stream", "application/pdf"}
        ):
            return True
    # Explicit binary/data wrappers are opaque unless the object says it is
    # text.  A normal `{data: "status text"}` result remains capturable.
    if any(key in value for key in ("bytes", "base64", "base64_data", "blob")):
        return True
    raw_data = value.get("data")
    if isinstance(raw_data, str) and _BASE64_DATA_RE.fullmatch(raw_data.strip()):
        return True
    return False


def _looks_like_media_text(value: str) -> bool:
    stripped = value.strip()
    if _MEDIA_DATA_URI_RE.fullmatch(stripped):
        return True
    # A long run of one printable character is commonly ordinary tool text
    # (for example, a padded command output), even though it belongs to the
    # base64 alphabet.  Requiring a little alphabetic variety avoids turning
    # bounded-text truncation into opaque-media exclusion while retaining the
    # realistic bare base64 payloads this guard is meant to discard.
    if not _BASE64_DATA_RE.fullmatch(stripped):
        return False
    counts = Counter(stripped)
    if len(counts) < 3:
        return False
    # A highly skewed alphabet is usually padded/plain output rather than a
    # binary encoding (the ordinary truncation test is a long run of ``x``).
    # Keep this conservative heuristic after the explicit data-URI check.
    return max(counts.values()) * 20 < len(stripped) * 19


def _redact_sensitive_path_tokens(value: str) -> str:
    """Remove private filenames embedded in shell commands/free text."""

    # Keep shell punctuation as boundaries too.  This is a lexical split only;
    # it never invokes a shell or attempts to interpret the command.
    parts = re.split(r"(\s+|(?=[;&|<>()=:])|(?<=[;&|<>()=:]))", value)
    for index, part in enumerate(parts):
        if not part or part.isspace():
            continue
        stripped = part.strip("'\"`,;:()[]{}")
        if not stripped or (
            "/" not in stripped
            and "\\" not in stripped
            and not _SENSITIVE_PATH_RE.fullmatch(stripped)
        ):
            continue
        if _is_sensitive_path(stripped):
            # Preserve a common shell punctuation suffix without retaining the
            # filename itself.  The resulting command remains explanatory but
            # cannot reveal the sensitive path.
            prefix = part[: len(part) - len(part.lstrip("'\"`"))]
            suffix = part[len(part.rstrip("'\"`,;:()[]{}")) :]
            parts[index] = prefix + REDACTED + suffix
    return "".join(parts)


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return JSON-safe, redacted content or ``_OPAQUE``.

    Structured text is retained recursively, while bytes, unsupported Python
    objects, and explicit media blocks disappear before JSON serialization.
    Sensitive path values remain in the shape as ``[REDACTED]`` so a caller
    can still understand which argument was present without seeing the path.
    """

    if depth > 12:
        return _OPAQUE
    if isinstance(value, str):
        if "\x00" in value:
            return _OPAQUE
        if _looks_like_media_text(value):
            return _OPAQUE
        redacted = _redact_sensitive_path_tokens(redact_text(value))
        if key.casefold() in _PATH_KEYS and _is_sensitive_path(redacted):
            return REDACTED
        # Commands and free text can contain private filenames too.  Replace
        # only a path-shaped token, keeping ordinary prose useful.
        if _is_sensitive_path(redacted) and ("/" in redacted or "\\" in redacted):
            return REDACTED
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                return _OPAQUE
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _OPAQUE
    if isinstance(value, Mapping):
        if _looks_like_media(value):
            # A text block with a media-looking key is still opaque as a unit;
            # storing a partial image response is misleading and may leak a
            # data URI through an unfamiliar field.
            return _OPAQUE
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or "\x00" in raw_key:
                continue
            safe_key = redact_text(raw_key)
            clean = _sanitize(child, key=safe_key, depth=depth + 1)
            if clean is not _OPAQUE:
                result[safe_key] = clean
        return result
    if isinstance(value, Sequence):
        result_list: list[Any] = []
        for child in value:
            clean = _sanitize(child, key=key, depth=depth + 1)
            if clean is not _OPAQUE:
                result_list.append(clean)
        return result_list
    # Sets, file handles, custom classes, and other opaque values are not
    # coerced to strings.  Stringifying one could disclose a path or secret.
    return _OPAQUE


def _canonical_json(value: Any) -> tuple[str | None, dict[str, Any], str | None]:
    if value is None:
        return None, {
            "present": False,
            "excluded": False,
            "shape": "null",
            "encoding": "json",
            "original_bytes": 0,
            "stored_bytes": 0,
            "truncated": False,
            "redacted": False,
        }, None
    clean = _sanitize(value)
    if clean is _OPAQUE:
        return None, {
            "present": value is not None,
            "excluded": True,
            "reason": "binary_or_opaque",
            "truncated": False,
            "original_bytes": 0,
            "stored_bytes": 0,
        }, None
    try:
        text = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return None, {
            "present": value is not None,
            "excluded": True,
            "reason": "binary_or_opaque",
            "truncated": False,
            "original_bytes": 0,
            "stored_bytes": 0,
        }, None
    raw_bytes = text.encode("utf-8")
    original_bytes = len(raw_bytes)
    if original_bytes > MAX_TOOL_PAYLOAD_BYTES:
        stored, _ = _truncated_json_wrapper(text)
    else:
        stored = text
    stored_bytes = len(stored.encode("utf-8"))
    unredacted = _json_without_redaction(value)
    metadata: dict[str, Any] = {
        "present": value is not None,
        "excluded": False,
        "shape": _shape(clean),
        "encoding": "json",
        "original_bytes": original_bytes,
        "stored_bytes": stored_bytes,
        "truncated": stored != text,
        # Compare before applying the size cap.  Truncation is represented by
        # its own explicit flag and must not be reported as redaction.
        "redacted": unredacted is not None and text != unredacted,
    }
    # A small useful-text excerpt is independent from the bounded JSON row.
    excerpt = _extract_text(clean)
    return stored, metadata, excerpt


def _canonical_stored_json(
    value: str | None,
) -> tuple[str | None, dict[str, Any], str | None] | None:
    """Validate a previously canonicalized JSON payload before DB writes.

    ``Store.remember`` is also a public API, so a caller can bypass hooks and
    supply a plain mapping.  Re-parsing that text here prevents raw secrets,
    invalid JSON, and oversized rows from entering the side index.  Truncated
    wrappers retain their original byte metadata while their head/tail strings
    are redacted again.
    """

    if value is None:
        return _canonical_json(None)
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(decoded, Mapping) and decoded.get("__codex_mem_truncated__") is True:
        clean = _sanitize(decoded)
        if clean is _OPAQUE:
            return None
        try:
            canonical = json.dumps(
                clean,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        original = decoded.get("original_bytes")
        if not isinstance(original, int) or isinstance(original, bool) or original < len(canonical.encode("utf-8")):
            original = len(canonical.encode("utf-8"))
        if len(canonical.encode("utf-8")) > MAX_TOOL_PAYLOAD_BYTES:
            canonical, _ = _truncated_json_wrapper(canonical)
        metadata: dict[str, Any] = {
            "present": True,
            "excluded": False,
            "shape": "object",
            "encoding": "json",
            "original_bytes": original,
            "stored_bytes": len(canonical.encode("utf-8")),
            "truncated": True,
            "redacted": canonical != (_json_without_redaction(decoded) or ""),
        }
        return canonical, metadata, _extract_text(clean)[:MAX_TOOL_EXCERPT_CHARS]
    return _canonical_json(decoded)


def _json_without_redaction(value: Any) -> str | None:
    """Best-effort comparison string without exposing it to callers."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return None


def _shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _extract_text(value: Any, *, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        parts: list[str] = []
        for child in value.values():
            text = _extract_text(child, depth=depth + 1)
            if text and text not in parts:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = []
        for child in value:
            text = _extract_text(child, depth=depth + 1)
            if text and text not in parts:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    marker = f"…[truncated: {len(encoded)} bytes]"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= maximum:
        return marker_bytes[:maximum].decode("utf-8", errors="ignore")
    end = maximum - len(marker_bytes)
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8", errors="ignore") + marker


def _utf8_prefix(value: str, maximum: int) -> str:
    """Return at most ``maximum`` UTF-8 bytes from the beginning."""

    if maximum <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _utf8_suffix(value: str, maximum: int) -> str:
    """Return at most ``maximum`` UTF-8 bytes from the end."""

    if maximum <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[-maximum:].decode("utf-8", errors="ignore")


def _truncated_json_wrapper(value: str) -> tuple[str, int]:
    """Store a bounded, valid JSON document while preserving the final tail.

    Slicing a JSON string directly would leave invalid JSON in the durable
    side index.  A small wrapper keeps the readback contract parseable and
    carries both the beginning and end of the already-redacted canonical
    representation.  The cap is measured after JSON escaping, so quoted or
    non-ASCII payloads cannot exceed the durable byte bound.
    """

    original_bytes = len(value.encode("utf-8"))

    def build(budget: int) -> str:
        # Preserve more of the tail because command output commonly reports
        # the actionable error at the end of a long response.
        head_budget = max(0, int(budget * 0.4))
        tail_budget = max(0, budget - head_budget)
        wrapper = {
            "__codex_mem_truncated__": True,
            "head": _utf8_prefix(value, head_budget),
            "original_bytes": original_bytes,
            "tail": _utf8_suffix(value, tail_budget),
        }
        return json.dumps(
            wrapper,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    # The wrapper's JSON escaping can add a material amount of overhead (for
    # example when a payload contains many quotes).  Binary search the largest
    # fragment budget that still fits the byte cap.
    low = 0
    high = min(original_bytes, MAX_TOOL_PAYLOAD_BYTES)
    while low < high:
        candidate = (low + high + 1) // 2
        if len(build(candidate).encode("utf-8")) <= MAX_TOOL_PAYLOAD_BYTES:
            low = candidate
        else:
            high = candidate - 1
    stored = build(low)
    # The fixed marker-only document always fits, but keep this final guard in
    # case the schema cap is changed independently in a future release.
    if len(stored.encode("utf-8")) > MAX_TOOL_PAYLOAD_BYTES:
        stored = build(0)
    return stored, original_bytes


def _metadata_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ToolCapture(Mapping[str, Any]):
    """A sanitized side-index row ready for :func:`insert_capture`.

    Mapping access is intentional: the Store integration can use either
    attribute access or ``capture["tool_input"]`` while this API evolves.
    """

    project: str
    session_id: str
    tool_use_id: str | None
    tool_name: str
    turn_id: str | None
    tool_input: str | None
    tool_response: str | None
    input_excerpt: str | None
    response_excerpt: str | None
    input_metadata: Mapping[str, Any]
    response_metadata: Mapping[str, Any]
    content_hash: str
    created_at: str
    cwd: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "session_id": self.session_id,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "tool_input": self.tool_input,
            "tool_response": self.tool_response,
            "input_excerpt": self.input_excerpt,
            "response_excerpt": self.response_excerpt,
            "input_metadata": dict(self.input_metadata),
            "response_metadata": dict(self.response_metadata),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "cwd": self.cwd,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def normalize_capture(
    payload: Mapping[str, Any] | Any,
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    private: bool = False,
) -> ToolCapture | None:
    """Build one sanitized raw capture, or ``None`` when policy skips it."""

    if not isinstance(payload, Mapping):
        return None
    tool_name = _identifier(payload.get("tool_name", payload.get("toolName")), MAX_TOOL_NAME_CHARS)
    if not tool_name or _skip_tool(tool_name, config):
        return None
    if private or payload.get("private") is True or payload.get("private_prompt") is True:
        return None
    tool_input = payload.get("tool_input", payload.get("toolInput"))
    if _contains_session_memory(tool_input):
        return None
    tool_response = payload.get("tool_response", payload.get("toolResponse"))
    session_id = _identifier(payload.get("session_id", payload.get("sessionId")), MAX_SESSION_CHARS) or "anonymous"
    turn_id = _identifier(payload.get("turn_id", payload.get("turnId")), MAX_SESSION_CHARS)
    tool_use_id = _identifier(payload.get("tool_use_id", payload.get("toolUseId")), MAX_SESSION_CHARS)
    cwd = _cwd_identifier(payload.get("cwd"))
    workspace = _identifier(project if project is not None else payload.get("project", payload.get("cwd")), MAX_PROJECT_CHARS) or ""
    input_json, input_meta, input_excerpt = _canonical_json(tool_input)
    response_json, response_meta, response_excerpt = _canonical_json(tool_response)
    if input_excerpt:
        input_excerpt = input_excerpt[:MAX_TOOL_EXCERPT_CHARS]
    if response_excerpt:
        response_excerpt = response_excerpt[:MAX_TOOL_EXCERPT_CHARS]
    if input_json is None and response_json is None and not tool_name:
        return None
    digest_source = "\x00".join((tool_name, input_json or "", response_json or ""))
    content_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    return ToolCapture(
        project=workspace,
        session_id=session_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        turn_id=turn_id,
        tool_input=input_json,
        tool_response=response_json,
        input_excerpt=input_excerpt,
        response_excerpt=response_excerpt,
        input_metadata=input_meta,
        response_metadata=response_meta,
        content_hash=content_hash,
        created_at=_utc_now(),
        cwd=cwd,
    )


def install_schema(connection: sqlite3.Connection) -> None:
    """Create the raw side index on an existing Store connection."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            project TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id TEXT,
            tool_use_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_input TEXT,
            tool_response TEXT,
            input_excerpt TEXT,
            response_excerpt TEXT,
            input_metadata_json TEXT NOT NULL,
            response_metadata_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            cwd TEXT,
            UNIQUE(project, session_id, tool_use_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS tool_uses_project_created_idx "
        "ON tool_uses(project, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS tool_uses_project_session_idx "
        "ON tool_uses(project, session_id, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS tool_uses_tool_name_idx "
        "ON tool_uses(project, tool_name)"
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(tool_uses)").fetchall()
        if len(row) > 1
    }
    if "cwd" not in columns:
        connection.execute("ALTER TABLE tool_uses ADD COLUMN cwd TEXT")


def _capture_value(capture: ToolCapture | Mapping[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(capture, ToolCapture):
        return getattr(capture, key, default)
    return capture.get(key, default)


def insert_capture(
    connection: sqlite3.Connection,
    entry_id: str,
    project: str,
    capture: ToolCapture | Mapping[str, Any],
) -> int | None:
    """Insert one capture inside the caller's entry transaction.

    The identity tuple is idempotent.  A duplicate tool-use id is deliberately
    left immutable: a later hook must not silently replace the evidence that a
    processor may already have claimed.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    checked_entry = _identifier(entry_id, 128)
    checked_project = _identifier(project, MAX_PROJECT_CHARS)
    tool_use_id = _identifier(_capture_value(capture, "tool_use_id"), MAX_SESSION_CHARS)
    session_id = _identifier(_capture_value(capture, "session_id"), MAX_SESSION_CHARS)
    tool_name = _identifier(_capture_value(capture, "tool_name"), MAX_TOOL_NAME_CHARS)
    cwd = _cwd_identifier(_capture_value(capture, "cwd"))
    if not checked_entry or not checked_project or not tool_use_id or not session_id or not tool_name:
        return None
    if _is_memory_tool(tool_name):
        return None
    raw_input = _capture_value(capture, "tool_input")
    raw_response = _capture_value(capture, "tool_response")
    input_result: tuple[str | None, dict[str, Any], str | None] | None = None
    response_result: tuple[str | None, dict[str, Any], str | None] | None = None

    if isinstance(capture, ToolCapture):
        # ToolCapture came from normalize_capture, but re-parse it anyway: a
        # manually constructed dataclass must not bypass the DB boundary.
        input_result = _canonical_stored_json(raw_input)
        response_result = _canonical_stored_json(raw_response)
        if input_result is None or response_result is None:
            return None
    else:
        # Public callers may provide either already-canonical JSON strings or
        # the original structured objects.  Prefer canonical readback; when a
        # string is ordinary tool text, normalize the original mapping safely.
        if isinstance(raw_input, (str, type(None))):
            input_result = _canonical_stored_json(raw_input)
        if isinstance(raw_response, (str, type(None))):
            response_result = _canonical_stored_json(raw_response)
        if input_result is None or response_result is None:
            normalized = normalize_capture(
                {
                    "project": checked_project,
                    "session_id": session_id,
                    "turn_id": _capture_value(capture, "turn_id"),
                    "cwd": cwd,
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "tool_input": raw_input,
                    "tool_response": raw_response,
                },
                project=checked_project,
            )
            if normalized is None:
                return None
            input_result = (
                normalized.tool_input,
                dict(normalized.input_metadata),
                normalized.input_excerpt,
            )
            response_result = (
                normalized.tool_response,
                dict(normalized.response_metadata),
                normalized.response_excerpt,
            )
            # normalize_capture also canonicalizes the identifiers.  Re-use
            # those values for the immutable uniqueness key below.
            tool_use_id = normalized.tool_use_id
            session_id = normalized.session_id
            tool_name = normalized.tool_name
            cwd = normalized.cwd
            if not tool_use_id or not session_id or not tool_name:
                return None

    assert input_result is not None and response_result is not None
    tool_input, input_meta, input_excerpt = input_result
    tool_response, response_meta, response_excerpt = response_result
    if _contains_session_memory(tool_input) or _contains_session_memory(tool_response):
        return None
    input_excerpt = input_excerpt[:MAX_TOOL_EXCERPT_CHARS] if input_excerpt else None
    response_excerpt = response_excerpt[:MAX_TOOL_EXCERPT_CHARS] if response_excerpt else None
    now = _capture_value(capture, "created_at", None)
    created_at = _identifier(now, 64) or _utc_now()
    turn_id = _identifier(_capture_value(capture, "turn_id"), MAX_SESSION_CHARS)
    digest = hashlib.sha256(
        "\x00".join((tool_name, tool_input or "", tool_response or "")).encode("utf-8")
    ).hexdigest()
    install_schema(connection)
    row = connection.execute(
        """
        INSERT INTO tool_uses(
            entry_id, project, session_id, turn_id, tool_use_id, tool_name,
            tool_input, tool_response, input_excerpt, response_excerpt,
            input_metadata_json, response_metadata_json, content_hash,
            created_at, updated_at, cwd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, session_id, tool_use_id) DO NOTHING
        RETURNING id
        """,
        (
            checked_entry,
            checked_project,
            session_id,
            turn_id,
            tool_use_id,
            tool_name,
            tool_input,
            tool_response,
            input_excerpt,
            response_excerpt,
            _metadata_json(input_meta),
            _metadata_json(response_meta),
            digest,
            created_at,
            _utc_now(),
            cwd,
        ),
    ).fetchone()
    if row is not None:
        return int(row[0])
    existing = connection.execute(
        "SELECT id FROM tool_uses WHERE project = ? AND session_id = ? AND tool_use_id = ? LIMIT 1",
        (checked_project, session_id, tool_use_id),
    ).fetchone()
    return int(existing[0]) if existing is not None else None


def _row_to_capture(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    def get(key: str, default: Any = None) -> Any:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            # A caller using a plain sqlite3 connection may not have selected
            # sqlite3.Row as its row factory.  The schema order is stable and
            # this fallback keeps the connection-level read API ergonomic.
            try:
                index = _TOOL_USE_COLUMNS.index(key)
                return row[index]  # type: ignore[index]
            except (ValueError, IndexError, TypeError):
                return default

    result: dict[str, Any] = {
        "id": int(get("id")),
        "entry_id": get("entry_id"),
        "project": get("project"),
        "session_id": get("session_id"),
        "turn_id": get("turn_id"),
        "tool_use_id": get("tool_use_id"),
        "tool_name": get("tool_name"),
        "tool_input": get("tool_input"),
        "tool_response": get("tool_response"),
        "input_excerpt": get("input_excerpt"),
        "response_excerpt": get("response_excerpt"),
        "content_hash": get("content_hash"),
        "created_at": get("created_at"),
        "updated_at": get("updated_at"),
        "cwd": get("cwd"),
    }
    for field, source in (("input_metadata", "input_metadata_json"), ("response_metadata", "response_metadata_json")):
        raw = get(source, "{}")
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        result[field] = value if isinstance(value, Mapping) else {}
    return result


def get_tool_capture(
    connection: sqlite3.Connection,
    project: str,
    tool_use_id: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one redacted source row by project/session/tool-use identity."""

    checked_project = _identifier(project, MAX_PROJECT_CHARS)
    checked_tool_id = _identifier(tool_use_id, MAX_SESSION_CHARS)
    if not checked_project or not checked_tool_id:
        return None
    if session_id is None:
        row = connection.execute(
            "SELECT * FROM tool_uses WHERE project = ? AND tool_use_id = ? ORDER BY id DESC LIMIT 1",
            (checked_project, checked_tool_id),
        ).fetchone()
    else:
        checked_session = _identifier(session_id, MAX_SESSION_CHARS)
        if not checked_session:
            return None
        row = connection.execute(
            "SELECT * FROM tool_uses WHERE project = ? AND session_id = ? AND tool_use_id = ? LIMIT 1",
            (checked_project, checked_session, checked_tool_id),
        ).fetchone()
    return _row_to_capture(row) if row is not None else None


def get_tool_capture_for_entry(
    connection: sqlite3.Connection,
    entry_id: str,
    project: str | None = None,
) -> dict[str, Any] | None:
    """Read the raw side-index row linked to one source entry."""

    checked_entry = _identifier(entry_id, 128)
    if not checked_entry:
        return None
    if project is None:
        row = connection.execute(
            "SELECT * FROM tool_uses WHERE entry_id = ? ORDER BY id DESC LIMIT 1",
            (checked_entry,),
        ).fetchone()
    else:
        checked_project = _identifier(project, MAX_PROJECT_CHARS)
        if not checked_project:
            return None
        row = connection.execute(
            "SELECT * FROM tool_uses WHERE entry_id = ? AND project = ? ORDER BY id DESC LIMIT 1",
            (checked_entry, checked_project),
        ).fetchone()
    return _row_to_capture(row) if row is not None else None


def hydrate_source_tool_io(
    connection: sqlite3.Connection,
    source: Mapping[str, Any],
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """Attach retained raw I/O to a source mapping before observer budgeting.

    The returned value is a new mapping.  The source entry itself remains the
    reader-facing excerpt, while ``tool_io`` carries the independently bounded
    redacted original input/response.  This is data for an observer prompt;
    this function never parses it as a command and never executes it.
    """

    if not isinstance(source, Mapping):
        return {}
    result = dict(source)
    entry_id = source.get("id", source.get("entry_id"))
    if not isinstance(entry_id, str):
        return result
    capture = get_tool_capture_for_entry(connection, entry_id, project=project)
    if capture is None:
        return result
    result["tool_io"] = {
        "tool_use_id": capture.get("tool_use_id"),
        "tool_name": capture.get("tool_name"),
        "cwd": capture.get("cwd"),
        "tool_input": capture.get("tool_input"),
        "tool_response": capture.get("tool_response"),
        "input_excerpt": capture.get("input_excerpt"),
        "response_excerpt": capture.get("response_excerpt"),
        "input_metadata": capture.get("input_metadata", {}),
        "response_metadata": capture.get("response_metadata", {}),
        "content_hash": capture.get("content_hash"),
    }
    return result


def list_tool_captures(
    connection: sqlite3.Connection,
    project: str,
    *,
    session_id: str | None = None,
    tool_name: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List source rows without invoking or interpreting their contents."""

    checked_project = _identifier(project, MAX_PROJECT_CHARS)
    if not checked_project:
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REPLAY_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_REPLAY_ITEMS}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be non-negative")
    clauses = ["project = ?"]
    parameters: list[Any] = [checked_project]
    if session_id is not None:
        checked_session = _identifier(session_id, MAX_SESSION_CHARS)
        if not checked_session:
            return []
        clauses.append("session_id = ?")
        parameters.append(checked_session)
    if tool_name is not None:
        checked_name = _identifier(tool_name, MAX_TOOL_NAME_CHARS)
        if not checked_name:
            return []
        clauses.append("tool_name = ?")
        parameters.append(checked_name)
    rows = connection.execute(
        "SELECT * FROM tool_uses WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
        (*parameters, limit, offset),
    ).fetchall()
    return [_row_to_capture(row) for row in rows]


def replay_plan(
    connection: sqlite3.Connection,
    project: str,
    tool_use_id: str | None = None,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build inert source-entry replay plans; this function never executes them."""

    if tool_use_id is None:
        rows = list_tool_captures(connection, project, session_id=session_id, limit=limit)
    else:
        row = get_tool_capture(connection, project, tool_use_id, session_id=session_id)
        rows = [row] if row is not None else []
    return [
        {
            "operation": "replay",
            "execute": False,
            "entry_id": row.get("entry_id"),
            "project": row.get("project"),
            "session_id": row.get("session_id"),
            "turn_id": row.get("turn_id"),
            "tool_use_id": row.get("tool_use_id"),
            "tool_name": row.get("tool_name"),
            "cwd": row.get("cwd"),
            "tool_input": row.get("tool_input"),
            "tool_response": row.get("tool_response"),
            "input_metadata": row.get("input_metadata", {}),
            "response_metadata": row.get("response_metadata", {}),
        }
        for row in rows
    ]


__all__ = [
    "MAX_TOOL_PAYLOAD_BYTES",
    "ToolCapture",
    "normalize_capture",
    "is_private_prompt",
    "install_schema",
    "insert_capture",
    "get_tool_capture",
    "get_tool_capture_for_entry",
    "hydrate_source_tool_io",
    "list_tool_captures",
    "replay_plan",
]
