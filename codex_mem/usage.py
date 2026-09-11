"""Incremental, metadata-only accounting of local Codex rollout usage.

No prompts, responses, or tool payloads leave this parser. Native response IDs
are the accounting identity; cumulative counters are compatibility input only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .config import automatic_capture_enabled, load_config
from .usage_store import UsageStore

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
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

    def close(self):
        if self._discovery is not None:
            self._discovery.close()
        self.store.close()

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

    def collect(self, config=None, max_files=32) -> dict:
        settings = config if config is not None else load_config(self.data_dir)
        if settings.get("usage_enabled", True) is not True or not settings.get("capture_enabled"):
            return {"files": 0, "events": 0, "status": "disabled"}
        if self._discovery is None:
            self._discovery = self._discover()
        for _ in range(2048):
            try:
                candidate = next(self._discovery)
            except StopIteration:
                self._discovery = None
                break
            if candidate is not None and self._allowed(candidate):
                self._paths.add(candidate)
        paths = sorted(self._paths)
        if not paths:
            return {"files": 0, "events": 0}
        count = min(max(0, min(max_files, 128)), len(paths))
        chosen = [paths[(self._next_file + i) % len(paths)] for i in range(count)]
        self._next_file = (self._next_file + count) % len(paths)
        result = {"files": 0, "events": 0, "errors": 0}
        for path in chosen:
            try:
                stat = path.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if self._unsupported.get(path) == signature:
                    continue
                item = self.scan_file(path, config=settings, max_bytes=max(65536, 8388608 // max(count, 1)))
                if item.get("status") == "unsupported":
                    self._unsupported[path] = signature
                else:
                    self._unsupported.pop(path, None)
                result["files"] += 1
                result["events"] += item.get("events", 0)
            except FileNotFoundError:
                self._paths.discard(path)
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
        prefix = handle.read(min(1024, offset))
        handle.seek(max(0, offset - 256))
        suffix = handle.read(min(256, offset))
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
