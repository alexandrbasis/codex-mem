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
from typing import Any

from .config import automatic_capture_enabled, load_config
from .usage_store import UsageStore

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

    def close(self):
        for discovery in (self._discovery, self._recent_discovery):
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
            except OSError:
                continue

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
            except OSError:
                continue

    def _allowed(self, path: Path) -> bool:
        path = path.absolute()
        for name in ("sessions", "archived_sessions"):
            root = self.codex_home / name
            try:
                path.relative_to(root)
            except ValueError:
                continue
            # Do not follow symlinked roots, directories, or rollout files.
            return not any(p.is_symlink() for p in (path, *list(path.parents)[:len(path.relative_to(self.codex_home).parts)])) and path.suffix == ".jsonl"
        return False

    def _observe(self, path: Path):
        """Track metadata only; the content budget belongs to scan_file."""
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
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
                break
            spent += 1
            if candidate is not None and self._allowed(candidate):
                try:
                    self._observe(candidate)
                    self._paths.add(candidate)
                except OSError:
                    self._forget(candidate)
        return spent

    def collect(self, config=None, max_files=32) -> dict:
        settings = config if config is not None else load_config(self.data_dir)
        if settings.get("usage_enabled", True) is not True or not settings.get("capture_enabled"):
            return {"files": 0, "events": 0, "status": "disabled"}
        scope_key = (settings.get("capture_scope"), tuple(settings.get("included_projects") or ()), tuple(settings.get("excluded_projects") or ()))
        if scope_key != self._scope_key:
            self._excluded.clear()
            self._scope_key = scope_key
        spent = self._advance_discovery("_recent_discovery", self._discover_recent, RECENT_DISCOVERY_ENTRY_BUDGET)
        self._advance_discovery("_discovery", self._discover, DISCOVERY_ENTRY_BUDGET - spent)
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
        count = min(max(0, min(max_files, COLLECTION_FILE_LIMIT)), len(paths))
        recent_quota = (count + 1) // 2
        if count == 1:
            # One file cannot serve both lanes in the same call. Alternate so
            # even this smallest budget guarantees historical progress.
            recent_quota = int(self._single_recent_turn)
            self._single_recent_turn = not self._single_recent_turn
        recent = sorted(self._recent, key=lambda path: (self._recent[path], str(path)), reverse=True)[:recent_quota]
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
        file_bytes = COLLECTION_BYTE_BUDGET // max(count, 1) - SCAN_FINGERPRINT_BYTE_BUDGET
        result = {"files": 0, "events": 0, "errors": 0, "recent_files": 0, "historical_files": 0}
        for path, lane in [(path, "recent_files") for path in recent] + [(path, "historical_files") for path in historical]:
            try:
                signature = self._observe(path)
                if self._unsupported.get(path) == signature or (lane == "recent_files" and (self._caught_up.get(path) == signature or self._excluded.get(path) == signature)):
                    continue
                item = self.scan_file(path, config=settings, max_bytes=file_bytes)
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
                result["files"] += 1
                result[lane] += 1
                result["events"] += item.get("events", 0)
            except FileNotFoundError:
                self._forget(path)
            except (OSError, ValueError):
                result["errors"] += 1
        return result

    def scan_file(self, path, config=None, max_bytes=2097152) -> dict:
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
            if previous and previous.get("file_identity") == identity and previous.get("fingerprint") == fingerprint and previous["offset"] <= stat.st_size:
                offset = previous["offset"]
                state = previous.get("parser_state") or {}
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
                    continue
                if not isinstance(record, dict):
                    continue
                self._record(record, state, events, path, offset)
                session = state.get("session")
                if session and not automatic_capture_enabled(session.get("project"), settings):
                    return {"events": 0, "status": "excluded"}
            session = state.get("session")
            if not session or state.get("invalid_owner"):
                return {"events": 0, "status": "unsupported"}
            checkpoint = {"offset": offset, "file_identity": identity, "fingerprint": self._fingerprint(handle, offset), "parser_state": state}
            committed = self.store.commit_scan(str(path), expected, checkpoint, session, events)
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
            if payload.get("thread_id") in (None, session["thread_id"]) and "service_tier" in payload:
                state["service_tier"] = _text(payload.get("service_tier"))
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
            event.update(response_id=response, root_turn_id=_text(payload.get("root_turn_id")),
                         event_key=hashlib.sha256(f"response:{session['thread_id']}:{response}".encode()).hexdigest())
            if any(k not in payload["usage"] for k in TOKEN_FIELDS):
                event["quality"] = "response_partial_counters"
            if root and root != session["session_id"]:
                event["quality"] = "root_metadata_conflict"
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
                "service_tier": state.get("service_tier"),
                "model_source": "turn_context" if context.get("model") else "unknown",
                "quality": "response_exact" if source_kind == "response" else "legacy_cumulative_delta",
                "source_kind": source_kind, "recorded_at": _text(record.get("timestamp")), **usage}
