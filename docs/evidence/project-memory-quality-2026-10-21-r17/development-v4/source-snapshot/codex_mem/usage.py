"""Incremental, metadata-only accounting of local Codex rollout usage.

No prompts, responses, or tool payloads leave this parser. Native response IDs
are the accounting identity; cumulative counters are compatibility input only.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from .config import automatic_capture_enabled, data_dir_path, load_config
from .store import project_key
from .usage_store import UsageStore, period_bounds

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
DISCOVERY_ENTRY_BUDGET = 2048
RECENT_DISCOVERY_ENTRY_BUDGET = 128
RECENT_REFRESH_FILES = 128
COLLECTION_FILE_LIMIT = 128
COLLECTION_BYTE_BUDGET = 8 * 1024 * 1024
FINGERPRINT_PREFIX_BYTES = 1024
FINGERPRINT_SUFFIX_BYTES = 256
SCAN_FINGERPRINT_BYTE_BUDGET = 2 * (FINGERPRINT_PREFIX_BYTES + FINGERPRINT_SUFFIX_BYTES)
PARSER_VERSION = 2
REFRESH_SNAPSHOT_ENTRY_LIMIT = 50000
REFRESH_SNAPSHOT_SECONDS = 0.25
_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 4096 else None


def _counts(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for name in TOKEN_FIELDS:
        item = value.get(name, 0)
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            return None
        result[name] = item
    return result if all(name in value for name in ("input_tokens", "output_tokens", "total_tokens")) and all(v is not None for v in result.values()) and result["total_tokens"] == result["input_tokens"] + result["output_tokens"] and result["cached_input_tokens"] + result["cache_write_input_tokens"] <= result["input_tokens"] and result["reasoning_output_tokens"] <= result["output_tokens"] else None


def coverage_period(data_dir, start_at, end_at, *, project=None, session_id=None, codex_home=None, max_files=2048):
    """Read-only, bounded checkpoint coverage without opening rollout content.

    File completeness is global, even for a scoped ledger report: an unseen
    file's project cannot be known without reading it. Counts are lower bounds
    when inspection is truncated; a cached check cannot discover new sources.
    """
    start, end = period_bounds(start_at, end_at)
    if start is None or end is None:
        raise ValueError("Period coverage requires start_at and end_at")
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    result = {"scope": "global", "known_files": 0, "inspected_files": 0, "unread_files": 0,
              "unread_bytes": 0, "repair_files": 0, "repair_bytes": 0, "errors": 0,
              "missing_source_files": 0, "unsupported_files": 0, "malformed_records": 0,
              "skipped_records": 0, "inspection_truncated": False, "discovery_complete": False,
              "freshness": "unknown", "checked_at": datetime.now(timezone.utc).isoformat(),
              "uncheckpointed_known_files": 0, "note": "Filesystem counts are global lower bounds from known checkpoints; cached checks do not discover new files."}
    database = data_dir_path(data_dir) / "memory.sqlite3"
    if not database.is_file():
        result["status"] = "missing_database"
        return result
    try:
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            conn.row_factory = sqlite3.Row
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_scan_files'").fetchone()
            if not exists:
                result["status"] = "missing_usage_ledger"
                return result
            result["known_files"] = conn.execute("SELECT COUNT(*) FROM usage_scan_files").fetchone()[0]
            rows = conn.execute("SELECT path,offset,parser_state FROM usage_scan_files ORDER BY path DESC LIMIT ?", (max(0, min(int(max_files), 10000)),)).fetchall()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_discovery_queue'").fetchone():
                scope = hashlib.sha256(str(home).encode()).hexdigest()
                result["pending_discovery"] = {row[0]: row[1] for row in conn.execute("SELECT kind,COUNT(*) FROM usage_discovery_queue WHERE scope=? GROUP BY kind", (scope,))}
                result["discovery_overflow_directories"] = result["pending_discovery"].get("overflow", 0)
                result["uncheckpointed_known_files"] = conn.execute("SELECT COUNT(*) FROM usage_discovery_queue q LEFT JOIN usage_scan_files f ON f.path=q.path WHERE q.scope=? AND q.kind='file' AND f.path IS NULL", (scope,)).fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        result.update(status="unreadable", errors=1)
        return result
    result["inspection_truncated"] = len(rows) < result["known_files"]
    earliest = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    for row in rows:
        path = Path(row["path"]).absolute()
        try:
            relative = path.relative_to(home)
            if relative.parts[0] not in {"sessions", "archived_sessions"} or any(p.is_symlink() for p in (path, *list(path.parents)[:len(relative.parts)])):
                result["unsupported_files"] += 1
                continue
            stat = path.stat()
            result["inspected_files"] += 1
            # Sources modified after the upper bound may still contain events
            # within the period. They must remain in the coverage denominator.
            if stat.st_mtime < earliest:
                continue
            state = json.loads(row["parser_state"])
            if state.get("parser_version") != PARSER_VERSION:
                result["repair_files"] += 1
                result["repair_bytes"] += stat.st_size
                unread = stat.st_size
            else:
                unread = stat.st_size if row["offset"] > stat.st_size else max(0, stat.st_size - row["offset"])
            result["unread_bytes"] += unread
            result["unread_files"] += bool(unread)
            result["malformed_records"] += state.get("malformed_records", 0)
            result["skipped_records"] += state.get("skipped_records", 0)
        except FileNotFoundError:
            result["missing_source_files"] += 1
        except (OSError, ValueError, TypeError, IndexError):
            result["errors"] += 1
    result["status"] = "ok"
    if any(result[key] for key in ("unread_files", "repair_files", "errors", "missing_source_files", "unsupported_files", "malformed_records", "skipped_records", "uncheckpointed_known_files")) or result.get("pending_discovery"):
        result["freshness"] = "partial"
    return result


class UsageCollector:
    """Collect bounded local slices; safe to call repeatedly or concurrently."""

    def __init__(self, data_dir=None, codex_home=None):
        self.store = UsageStore(data_dir)
        self.data_dir = data_dir
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
        self._next_file = 0
        self._paths = set()
        self._unsupported = {}
        self._discovery = None
        self._recent_discovery = None
        self._signatures = {}
        self._caught_up = {}
        self._recent = {}
        self._excluded = {}
        self._scope_key = None
        self._single_recent_turn = True
        self._period_discovery = None
        self._period_key = None
        self._period_complete = False
        self._discovery_complete = False
        self._discovery_errors = 0
        self._last_discovery_at = None
        self._checkpointed_paths = set()
        self._durable_discovery = False
        self._durable_incomplete = False
        self._discovery_scope = hashlib.sha256(str(self.codex_home).encode()).hexdigest()

    def close(self):
        for discovery in (self._discovery, self._recent_discovery, self._period_discovery):
            if discovery is not None:
                discovery.close()
        self.store.close()

    def _discover_recent(self):
        """Find native current-day rollouts without waiting for archive traversal."""
        today = datetime.now(timezone.utc).date()
        for delta in (0, -1, 1):
            # Adjacent dates cover native hosts whose local day differs from UTC.
            day = today + timedelta(days=delta)
            directory = self.codex_home / 'sessions' / day.strftime('%Y/%m/%d')
            if directory.is_symlink():
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        candidate = None
                        if not entry.is_symlink() and entry.is_file(follow_symlinks=False) and entry.name.endswith('.jsonl'):
                            candidate = Path(entry.path)
                        yield candidate
            except FileNotFoundError:
                continue
            except OSError:
                self._discovery_errors += 1

    def _discover_period(self, start_at, end_at):
        """Prioritize native date directories, then let the global sweep find
        older sessions that stayed active in the requested time window.
        """
        first = datetime.fromisoformat(start_at.replace("Z", "+00:00")).date() - timedelta(days=1)
        last = datetime.fromisoformat(end_at.replace("Z", "+00:00")).date() + timedelta(days=1)
        day = last
        while day >= first:
            directory = self.codex_home / "sessions" / day.strftime("%Y/%m/%d")
            # A missing directory still spends one discovery slot.
            yield None
            if not directory.is_symlink():
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            yield Path(entry.path) if not entry.is_symlink() and entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl") else None
                except FileNotFoundError:
                    pass
                except OSError:
                    self._discovery_errors += 1
            day -= timedelta(days=1)

    def _discover(self):
        """Yield once per directory entry so discovery itself stays bounded."""
        pending = [self.codex_home / name for name in ("sessions", "archived_sessions")]
        while pending:
            directory = pending.pop()
            if directory.is_symlink():
                continue

            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        candidate = None
                        if not entry.is_symlink():
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                                candidate = Path(entry.path)
                        yield candidate
            except FileNotFoundError:
                continue
            except OSError:
                self._discovery_errors += 1
                continue

    def _discover_persistent(self):
        """Spool directory-entry metadata so short CLI calls make progress.

        Stdlib directory streams have no serializable cursor. One explicit
        refresh therefore snapshots at most 50,000 entries or 250ms before its
        ordinary 2,048-entry discovery pass. Overflow is retained as incomplete,
        never silently called a complete inventory. No rollout text is read.
        """
        roots = [self.codex_home / name for name in ("sessions", "archived_sessions")]
        self.store.discovery_next(self._discovery_scope, roots)
        deadline = time.monotonic() + REFRESH_SNAPSHOT_SECONDS
        remaining = REFRESH_SNAPSHOT_ENTRY_LIMIT
        while remaining > 0 and time.monotonic() < deadline:
            item = self.store.discovery_next(self._discovery_scope, kind="directory")
            if item is None:
                break
            directory = Path(item["path"])
            children, overflow = [], False
            try:
                if self._allowed(directory, directory=True):
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if remaining <= 0 or time.monotonic() >= deadline:
                                overflow = True
                                break
                            remaining -= 1
                            if not entry.is_symlink():
                                if entry.is_dir(follow_symlinks=False):
                                    children.append((Path(entry.path), "directory"))
                                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                                    children.append((Path(entry.path), "file"))
            except FileNotFoundError:
                pass
            except OSError:
                overflow = True
                self._discovery_errors += 1
            self.store.discovery_finish(self._discovery_scope, directory, children, overflow=overflow)
            yield None
        after = None
        while True:
            item = self.store.discovery_next(self._discovery_scope, kind="file", after_path=after)
            if item is None:
                break
            after = item["path"]
            # Queue entries remain durable until scan_file reaches EOF, or
            # explicitly establishes that the source is excluded/unsupported.
            yield Path(after)
        pending = self.store.discovery_pending(self._discovery_scope)
        self._durable_incomplete = bool(pending.get("directory") or pending.get("overflow"))

    def _allowed(self, path: Path, *, directory=False) -> bool:
        path = path.absolute()
        for name in ("sessions", "archived_sessions"):
            root = self.codex_home / name
            try:
                path.relative_to(root)
            except ValueError:
                continue
            # Do not follow symlinked roots, directories, or rollout files.
            return not any(p.is_symlink() for p in (path, *list(path.parents)[:len(path.relative_to(self.codex_home).parts)])) and (directory or path.suffix == ".jsonl")
        return False

    def _observe(self, path: Path):
        """Track metadata only; the content budget belongs to scan_file."""
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if path not in self._signatures:
            checkpoint = self.store.get_checkpoint(path)
            if checkpoint:
                self._checkpointed_paths.add(path)
                state = checkpoint.get("parser_state") or {}
                if (state.get("parser_version") == PARSER_VERSION and state.get("source_mtime_ns") == stat.st_mtime_ns
                        and checkpoint.get("file_identity") == f"{stat.st_dev}:{stat.st_ino}" and checkpoint["offset"] == stat.st_size):
                    self._caught_up[path] = signature
        self._signatures[path] = signature
        if self._caught_up.get(path) == signature or self._unsupported.get(path) == signature or self._excluded.get(path) == signature:
            self._recent.pop(path, None)
        else:
            self._recent[path] = stat.st_mtime_ns
        return signature

    def _forget(self, path: Path):
        self._paths.discard(path)
        for mapping in (self._signatures, self._caught_up, self._recent, self._unsupported, self._excluded):
            mapping.pop(path, None)
        if self._durable_discovery:
            # A vanished or newly disallowed queued source must not prevent the
            # next inventory cycle. Its response identities remain in the ledger.
            self.store.discovery_finish(self._discovery_scope, path)

    def _advance_discovery(self, attribute, factory, budget):
        discovery = getattr(self, attribute)
        if discovery is None:
            discovery = factory()
            setattr(self, attribute, discovery)
        spent = 0
        for _ in range(budget):
            try:
                candidate = next(discovery)
            except StopIteration:
                setattr(self, attribute, None)
                if attribute == "_discovery":
                    self._discovery_complete = not (self._durable_discovery and self._durable_incomplete)
                    self._last_discovery_at = datetime.now(timezone.utc).isoformat()
                elif attribute == "_period_discovery":
                    self._period_complete = True
                break
            spent += 1
            if candidate is not None and self._allowed(candidate):
                try:
                    self._observe(candidate)
                    self._paths.add(candidate)
                except FileNotFoundError:
                    self._forget(candidate)
                except OSError:
                    self._discovery_errors += 1
                    self._forget(candidate)
            elif candidate is not None:
                self._forget(candidate)
        return spent

    def collect(self, config=None, max_files=32, *, max_bytes=COLLECTION_BYTE_BUDGET, period=None, project=None, session_id=None) -> dict:
        settings = config if config is not None else load_config(self.data_dir)
        if settings.get("usage_enabled", True) is not True or not settings.get("capture_enabled"):
            return {"files": 0, "events": 0, "status": "disabled"}
        scope_key = (settings.get("capture_scope"), tuple(settings.get("included_projects") or ()), tuple(settings.get("excluded_projects") or ()), project, session_id)
        if scope_key != self._scope_key:
            self._excluded.clear()
            self._scope_key = scope_key
        max_bytes = max(0, min(int(max_bytes), COLLECTION_BYTE_BUDGET))
        spent = 0
        if period is not None:
            key = period_bounds(*period)
            if None in key:
                raise ValueError("Period refresh requires start_at and end_at")
            if key != self._period_key:
                if self._period_discovery is not None:
                    self._period_discovery.close()
                self._period_discovery = None
                self._period_complete = False
                self._period_key = key
            if not self._period_complete:
                spent += self._advance_discovery("_period_discovery", lambda: self._discover_period(*key), RECENT_DISCOVERY_ENTRY_BUDGET * 4)
        spent += self._advance_discovery("_recent_discovery", self._discover_recent, RECENT_DISCOVERY_ENTRY_BUDGET)
        self._durable_discovery = period is not None
        self._advance_discovery("_discovery", self._discover_persistent if period is not None else self._discover, DISCOVERY_ENTRY_BUDGET - spent)
        # Recheck the newest known sources every cycle, even when directory
        # discovery is still walking historical files. Completed sources enter
        # the recent lane again as soon as their metadata changes.
        watched = sorted(self._signatures, key=lambda path: (self._signatures[path][3], str(path)), reverse=True)
        for path in watched[:RECENT_REFRESH_FILES]:
            try:
                if self._allowed(path):
                    self._observe(path)
                else:
                    self._forget(path)
            except OSError:
                self._forget(path)
        paths = sorted(self._paths)
        if not paths:
            return {"files": 0, "events": 0}
        count = min(max(0, min(max_files, COLLECTION_FILE_LIMIT)), len(paths), max_bytes // 65536)
        recent_quota = (count + 1) // 2
        if count == 1:
            # One file cannot serve both lanes in the same call. Alternate so
            # even this smallest budget guarantees historical progress.
            recent_quota = int(self._single_recent_turn)
            self._single_recent_turn = not self._single_recent_turn
        # Modification time also admits old session folders whose work overlaps
        # the period. Folder date alone would silently miss long-running tasks.
        earliest = datetime.fromisoformat(self._period_key[0].replace("Z", "+00:00")).timestamp() * 1e9 if period is not None else None
        recent = sorted(self._recent, key=lambda path: (earliest is not None and self._recent[path] >= earliest, self._recent[path], str(path)), reverse=True)[:recent_quota]
        recent_set = set(recent)
        historical = []
        while len(historical) < count - len(recent):
            path = paths[self._next_file % len(paths)]
            self._next_file = (self._next_file + 1) % len(paths)
            if path not in recent_set:
                historical.append(path)
        # Separate file slots also reserve a positive share of the byte budget
        # for history when a large active rollout never reaches EOF.
        # Resume/replacement checks read at most two bounded fingerprints per
        # source. Reserve those bytes too, so parsing plus fingerprint reads
        # together stay within the collection cap.
        file_bytes = max_bytes // max(count, 1) - SCAN_FINGERPRINT_BYTE_BUDGET
        result = {"files": 0, "events": 0, "errors": 0, "recent_files": 0, "historical_files": 0}
        for path, lane in [(path, "recent_files") for path in recent] + [(path, "historical_files") for path in historical]:
            try:
                signature = self._observe(path)
                if self._unsupported.get(path) == signature or (lane == "recent_files" and (self._caught_up.get(path) == signature or self._excluded.get(path) == signature)):
                    continue
                item = self.scan_file(path, config=settings, max_bytes=file_bytes, project=project, session_id=session_id)
                if item.get("status") == "unsupported":
                    self._unsupported[path] = signature
                    self._recent.pop(path, None)
                elif item.get("status") == "excluded":
                    self._excluded[path] = signature
                    self._recent.pop(path, None)
                else:
                    self._unsupported.pop(path, None)
                    self._excluded.pop(path, None)
                    if item.get("status") == "ok" and item.get("offset", 0) >= signature[2]:
                        self._caught_up[path] = signature
                        self._recent.pop(path, None)
                if period is not None and (item.get("status") in {"unsupported", "excluded", "outside_scope"} or item.get("status") == "ok" and item.get("offset", 0) >= signature[2]):
                    self.store.discovery_finish(self._discovery_scope, path)
                result["files"] += 1
                result[lane] += 1
                result["events"] += item.get("events", 0)
            except FileNotFoundError:
                self._forget(path)
            except (OSError, ValueError):
                result["errors"] += 1
        return result

    def refresh_period(self, start_at, end_at, config=None, *, max_files=32, max_bytes=COLLECTION_BYTE_BUDGET, project=None, session_id=None):
        """One bounded catch-up pass; repeated calls retain discovery fairness.

        Response records are retained for the whole source, since parsing only
        a slice would lose attribution and cannot safely advance its cursor.
        Explicit project/task scopes constrain which sources may be committed.
        """
        start, end = period_bounds(start_at, end_at)
        if start is None or end is None:
            raise ValueError("Period refresh requires start_at and end_at")
        result = self.collect(config, max_files, max_bytes=max_bytes, period=(start, end), project=project, session_id=session_id)
        result["coverage"] = self.coverage(start, end, project=project, session_id=session_id)
        result["coverage"]["last_refresh_errors"] = result.get("errors", 0)
        if result.get("errors"):
            result["coverage"]["freshness"] = "partial"
        return result

    def coverage(self, start_at, end_at, *, project=None, session_id=None):
        result = coverage_period(self.data_dir, start_at, end_at, project=project, session_id=session_id, codex_home=self.codex_home)
        result.update(discovery_complete=self._discovery_complete, period_discovery_complete=self._period_complete,
                      last_full_discovery_at=self._last_discovery_at, discovery_errors=self._discovery_errors)
        if self._durable_discovery:
            pending = self.store.discovery_pending(self._discovery_scope)
            result["pending_discovery"] = pending
            result["discovery_overflow_directories"] = pending.get("overflow", 0)
            if pending.get("directory") or pending.get("overflow"):
                result["discovery_complete"] = False
        result["freshness"] = "known_sources_current" if result["discovery_complete"] and not any(result.get(key, 0) for key in ("unread_files", "repair_files", "errors", "discovery_errors", "missing_source_files", "unsupported_files", "uncheckpointed_known_files", "malformed_records", "skipped_records")) and not result.get("inspection_truncated") else "partial"
        result["unsupported_files"] += len(self._unsupported)
        result["uncheckpointed_known_files"] = max(result["uncheckpointed_known_files"], len(self._paths - set(self._unsupported) - set(self._excluded) - self._checkpointed_paths))
        if result["unsupported_files"] or result["uncheckpointed_known_files"]:
            result["freshness"] = "partial"
        return result

    def scan_file(self, path, config=None, max_bytes=2097152, *, project=None, session_id=None) -> dict:
        path = Path(path).absolute()
        settings = config if config is not None else load_config(self.data_dir)
        if not self._allowed(path):
            return {"events": 0, "status": "outside_scope"}
        if settings.get("usage_enabled", True) is not True or not settings.get("capture_enabled"):
            return {"events": 0, "status": "disabled"}
        previous = self.store.get_checkpoint(str(path))
        expected = previous.get("offset") if previous else None
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            fingerprint = self._fingerprint(handle, min(previous["offset"], stat.st_size)) if previous else None
            state = {}
            offset = 0
            if previous and previous.get("parser_state", {}).get("parser_version") == PARSER_VERSION and previous.get("file_identity") == identity and previous.get("fingerprint") == fingerprint and previous["offset"] <= stat.st_size:
                offset = previous["offset"]
                state = previous.get("parser_state") or {}
            state["parser_version"] = PARSER_VERSION
            handle.seek(offset)
            events = []
            consumed = 0
            while consumed < max_bytes:
                start = handle.tell()
                line = handle.readline(max_bytes - consumed)
                if not line:
                    break
                consumed += len(line)
                if not line.endswith(b"\n"):
                    # Leave an unfinished tail for a later scan. Oversize complete
                    # records are skipped in chunks, without retaining their text.
                    if start == offset and consumed == len(line) and handle.tell() < stat.st_size:
                        if not state.get("skip_line"):
                            state["skipped_records"] = state.get("skipped_records", 0) + 1
                        state["skip_line"] = True
                        offset = handle.tell()
                    else:
                        handle.seek(start)
                    break
                offset = handle.tell()
                if state.pop("skip_line", False):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    state["malformed_records"] = state.get("malformed_records", 0) + 1
                    continue
                if not isinstance(record, dict):
                    continue
                self._record(record, state, events, path, offset)
                session = state.get("session")
                if session and not automatic_capture_enabled(session.get("project"), settings):
                    return {"events": 0, "status": "excluded"}
                if session and project is not None and project_key(session.get("project")) != project_key(project):
                    return {"events": 0, "status": "excluded"}
            session = state.get("session")
            if not session or state.get("invalid_owner"):
                return {"events": 0, "status": "unsupported"}
            if session_id is not None and session_id not in (session.get("session_id"), session.get("parent_thread_id")):
                return {"events": 0, "status": "excluded"}
            state["source_mtime_ns"] = stat.st_mtime_ns
            checkpoint = {"offset": offset, "file_identity": identity, "fingerprint": self._fingerprint(handle, offset), "parser_state": state}
            committed = self.store.commit_scan(str(path), expected, checkpoint, session, events)
            if committed:
                self._checkpointed_paths.add(path)
            return {"events": len(events) if committed else 0, "offset": offset, "status": "ok" if committed else "conflict"}

    @staticmethod
    def _fingerprint(handle, offset):
        """Detect in-place replacement without rereading the rollout body."""
        position = handle.tell()
        handle.seek(0)
        prefix = handle.read(min(FINGERPRINT_PREFIX_BYTES, offset))
        handle.seek(max(0, offset - FINGERPRINT_SUFFIX_BYTES))
        suffix = handle.read(min(FINGERPRINT_SUFFIX_BYTES, offset))
        handle.seek(position)
        return hashlib.sha256(prefix + suffix).hexdigest()

    def _record(self, record, state, events, path, offset):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind = record.get("type")
        if kind == "session_meta":
            if "session" in state:
                # Forks can embed a second, parent session header and its history.
                state["inherited"] = True
                return
            owner = _text(payload.get("id"))
            match = _UUID.search(path.name)
            if not owner or (match and owner.lower() != match.group(1).lower()):
                state["invalid_owner"] = True
                return
            source = payload.get("source")
            spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else {}
            if not isinstance(spawn, dict):
                spawn = {}
            state["session"] = {
                "thread_id": owner,
                "session_id": _text(payload.get("session_id")) or owner,
                "parent_thread_id": _text(payload.get("parent_thread_id")) or _text(spawn.get("parent_thread_id")),
                "project": _text(payload.get("cwd")),
                "agent_path": _text(payload.get("agent_path")) or _text(spawn.get("agent_path")),
                "agent_nickname": _text(payload.get("agent_nickname")) or _text(spawn.get("agent_nickname")),
                "agent_role": _text(payload.get("agent_role")) or _text(spawn.get("agent_role")),
                "thread_source": _text(payload.get("thread_source")) or _text(source) or ("subagent:" + source["subagent"] if isinstance(source, dict) and isinstance(source.get("subagent"), str) else "subagent" if spawn else None),
                "model_provider": _text(payload.get("model_provider")),
                "started_at": _text(record.get("timestamp")),
            }
            state["root_explicit"] = bool(_text(payload.get("session_id")))
            state["turn_models"] = {}
            return
        session = state.get("session")
        if not session or state.get("invalid_owner"):
            return
        if kind == "turn_context":
            turn = _text(payload.get("turn_id"))
            if turn:
                state["turn_models"][turn] = {"model": _text(payload.get("model")), "model_provider": _text(payload.get("model_provider")) or session.get("model_provider")}
                state["turn_id"] = turn
                # Keep checkpoint size bounded; unknown model is preferable to guessing.
                while len(state["turn_models"]) > 256:
                    del state["turn_models"][next(iter(state["turn_models"]))]
            return
        event_kind = payload.get("type") if kind == "event_msg" else kind
        if event_kind == "thread_settings_applied":
            if payload.get("thread_id") not in (None, session["thread_id"]):
                return
            nested = payload.get("thread_settings")
            settings = nested if isinstance(nested, dict) and "service_tier" in nested else payload
            if "service_tier" in settings:
                state["requested_service_tier"] = _text(settings.get("service_tier"))
                state["requested_service_tier_source"] = "thread_settings_nested" if settings is nested else "thread_settings"
            return
        if event_kind == "token_usage_record":
            if payload.get("thread_id") != session["thread_id"]:
                return
            root = _text(payload.get("session_id"))
            if root and not state.get("root_explicit"):
                session["session_id"] = root
                state["root_explicit"] = True
                for pending in events:
                    pending["session_id"] = root
            response = _text(payload.get("response_id"))
            usage = _counts(payload.get("usage"))
            if not response or usage is None:
                return
            turn = _text(payload.get("turn_id"))
            event = self._event(session, state, turn, usage, record, "response")
            # This is response-scoped evidence only. Thread settings never fill
            # the provider-confirmed field, including after a replay.
            confirmed = _text(payload.get("service_tier"))
            if confirmed is not None:
                event.update(service_tier=confirmed, service_tier_source="token_usage_record")
            event.update(response_id=response, root_turn_id=_text(payload.get("root_turn_id")),
                         event_key=hashlib.sha256(f"response:{session['thread_id']}:{response}".encode()).hexdigest())
            if any(k not in payload["usage"] for k in TOKEN_FIELDS):
                event["quality"] = "response_partial_counters"
            if root and root != session["session_id"]:
                event["quality"] = "root_metadata_conflict_partial_counters" if event.get("quality") == "response_partial_counters" else "root_metadata_conflict"
            events.append(event)
        elif event_kind == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            counts = _counts(info.get("total_token_usage"))
            if counts is None:
                return
            prior = state.get("legacy_totals")
            # A fork may start with copied counters; never bill inherited work.
            if state.get("inherited") or session.get("parent_thread_id"):
                return
            if prior and any(counts[k] is not None and prior.get(k) is not None and counts[k] < prior[k] for k in TOKEN_FIELDS):
                return
            state["legacy_totals"] = counts
            delta = {k: counts[k] - ((prior or {}).get(k) or 0) if counts[k] is not None else None for k in TOKEN_FIELDS}
            if _counts(delta) is None or not any(delta.get(k) for k in ("input_tokens", "output_tokens")):
                return
            turn = state.get("turn_id")
            event = self._event(session, state, turn, delta, record, "legacy")
            key = json.dumps(counts, sort_keys=True)
            event["event_key"] = hashlib.sha256(f"legacy:{session['thread_id']}:{key}".encode()).hexdigest()
            if any(k not in info["total_token_usage"] for k in TOKEN_FIELDS):
                event["quality"] = "legacy_partial_counters"
            events.append(event)

    @staticmethod
    def _event(session, state, turn, usage, record, source_kind):
        context = state.get("turn_models", {}).get(turn, {})
        return {"thread_id": session["thread_id"], "session_id": session["session_id"],
                "turn_id": turn, "model": context.get("model"),
                "model_provider": context.get("model_provider") or session.get("model_provider"),
                "service_tier": None,
                "requested_service_tier": state.get("requested_service_tier"),
                "requested_service_tier_source": state.get("requested_service_tier_source"),
                "service_tier_source": None,
                "model_source": "turn_context" if context.get("model") else "unknown",
                "quality": "response_exact" if source_kind == "response" else "legacy_cumulative_delta",
                "source_kind": source_kind, "recorded_at": _text(record.get("timestamp")), **usage}
