"""Bounded, content-free hook traces, independent of config and SQLite.

Every append is one O_APPEND write. Rotation takes a nonblocking advisory lock;
busy rotation or unavailable logging never delays a hook waiting for a lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterator, Mapping
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - native hooks target POSIX
    fcntl = None  # type: ignore[assignment]


# Keep recent incidents through busy sessions and diagnostic test runs.
MAX_LOG_BYTES = 16 * 1024 * 1024
LOG_BACKUPS = 3
MAX_RECORD_BYTES = 8192
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_TEXT_FIELDS = frozenset({"code", "reason", "status", "stage", "diagnostic_code"})
HOOK_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact"})
_PACKAGE = Path(__file__).resolve().parent
_ACTIVE: ContextVar[HookTrace | None] = ContextVar("codex_mem_hook_trace", default=None)


def log_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Use the memory-home convention without importing config or opening it."""
    raw = data_dir if data_dir is not None else (
        os.environ.get("CODEX_MEM_HOME", "").strip()
        or Path.home() / ".local" / "share" / "codex-mem"
    )
    return Path(raw).expanduser() / "logs" / "hooks.jsonl"


def _regular_fd(path: Path, flags: int) -> int:
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "not a private regular log file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rotate(path: Path, incoming: int) -> None:
    if fcntl is None:
        return
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "not a private regular log file")
        if info.st_size + incoming <= MAX_LOG_BYTES:
            return
    except FileNotFoundError:
        return
    fd = _regular_fd(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if not path.exists() or path.stat().st_size + incoming <= MAX_LOG_BYTES:
            return
        for index in range(LOG_BACKUPS, 0, -1):
            source = path if index == 1 else Path(f"{path}.{index - 1}")
            target = Path(f"{path}.{index}")
            try:
                os.replace(source, target)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def _append(path: Path, record: Mapping[str, Any]) -> bool:
    try:
        raw = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(raw) > MAX_RECORD_BYTES:
            return False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _rotate(path, len(raw))
        fd = _regular_fd(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            return os.write(fd, raw) == len(raw)
        finally:
            os.close(fd)
    except Exception:
        return False


def _fields(values: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _TOKEN.fullmatch(key):
            continue
        if isinstance(value, bool) or (isinstance(value, int) and abs(value) <= 2**63):
            safe[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            safe[key] = round(value, 3)
        elif key in _TEXT_FIELDS and isinstance(value, str) and _TOKEN.fullmatch(value):
            safe[key] = value
    return safe


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _hash(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value[:8192].encode("utf-8", errors="replace")).hexdigest()[:20]


def _frames(tb: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while tb is not None:
        code = tb.tb_frame.f_code
        filename = Path(code.co_filename)
        # Do not include source text, locals, exception messages or user paths.
        try:
            filename.relative_to(_PACKAGE)
            location = "codex_mem/" + filename.name
        except ValueError:
            location = "external"
        function = code.co_name if _TOKEN.fullmatch(code.co_name) else "unknown"
        frames.append({"file": location, "function": function, "line": tb.tb_lineno})
        tb = tb.tb_next
    return frames[-12:]


class HookTrace:
    def __init__(self, component: str, *, data_dir: str | os.PathLike[str] | None = None,
                 run_id: str | None = None, timeout: float | None = None) -> None:
        self.component = component if _TOKEN.fullmatch(component) else "unknown"
        self.run_id = _identifier(run_id) or str(uuid.uuid4())
        self.started = time.monotonic()
        self.stage = "bootstrap"
        self.sequence = 0
        self.error_count = 0
        self.dropped_records = 0
        self.context: dict[str, Any] = {}
        # Optional launch metadata is available before stdin. Keep it distinct
        # from the event actually observed in the payload.
        declared_event = os.environ.get("CODEX_MEM_HOOK_EVENT")
        if declared_event in HOOK_EVENTS:
            self.context["declared_hook_event"] = declared_event
        self.enabled = (os.environ.get("CODEX_MEM_HOOK_LOG") != "0"
                        and os.environ.get("CODEX_MEM_DISABLED") != "1")
        try:
            self.path = log_path(data_dir)
        except Exception:
            self.enabled = False
            self.path = None
        self.last_interrupted_stage: str | None = None
        self.finished = False
        self.emit("started", timeout_ms=timeout * 1000 if timeout is not None else None)

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled or self.finished or not _TOKEN.fullmatch(event):
            return
        try:
            if event == "error":
                self.error_count += 1
            self.sequence += 1
            record = {
                **_fields(fields), **self.context,
                "schema_version": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "run_id": self.run_id, "component": self.component,
                "pid": os.getpid(), "ppid": os.getppid(), "sequence": self.sequence,
                "event": event, "stage": self.stage,
                "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                "dropped_records": self.dropped_records,
            }
            if not _append(self.path, record):
                self.dropped_records += 1
        except Exception:
            self.dropped_records += 1

    def error(self, code: str, exc: BaseException) -> None:
        try:
            details: dict[str, Any] = {"exception_type": type(exc).__name__,
                                      "frames": _frames(exc.__traceback__),
                                      "failed_stage": self.last_interrupted_stage or self.stage}
            number = getattr(exc, "errno", None)
            if isinstance(number, int):
                details.update(errno=number, errno_name=errno.errorcode.get(number, "unknown"))
            sql_code = getattr(exc, "sqlite_errorcode", None)
            if isinstance(sql_code, int):
                details["sqlite_errorcode"] = sql_code
                name = getattr(exc, "sqlite_errorname", None)
                if isinstance(name, str) and re.fullmatch(r"SQLITE_[A-Z_]+", name):
                    details["sqlite_errorname"] = name
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                details["cause_type"] = type(cause).__name__
            # Exception metadata has its own narrow serializer. Never str(exc).
            self.context["error"] = details
            self.emit("error", code=code)
        except Exception:
            self.dropped_records += 1
        finally:
            self.context.pop("error", None)


def get_trace() -> HookTrace | None:
    return _ACTIVE.get()


def begin_trace(component: str, *, data_dir: str | os.PathLike[str] | None = None,
                run_id: str | None = None, timeout: float | None = None) -> HookTrace:
    trace = HookTrace(component, data_dir=data_dir, run_id=run_id, timeout=timeout)
    _ACTIVE.set(trace)
    return trace


def trace_event(event: str, **fields: Any) -> None:
    trace = get_trace()
    if trace is not None:
        trace.emit(event, **fields)


def trace_error(code: str, exc: BaseException) -> None:
    trace = get_trace()
    if trace is not None:
        trace.error(code, exc)


def trace_payload(payload: Mapping[str, Any]) -> None:
    trace = get_trace()
    if trace is None:
        return
    event = payload.get("hook_event_name")
    trace.context["hook_event"] = event if isinstance(event, str) and event in HOOK_EVENTS else "unknown"
    for key in ("session_id", "turn_id"):
        identifier = _identifier(payload.get(key))
        hashed = _hash(payload.get(key))
        if identifier:
            trace.context[key] = identifier
        elif hashed:
            trace.context[key + "_hash"] = hashed
    for source, target in (("cwd", "project_hash"), ("tool_name", "tool_hash"),
                           ("tool_use_id", "tool_use_id_hash")):
        hashed = _hash(payload.get(source))
        if hashed:
            trace.context[target] = hashed
    tool = payload.get("tool_name")
    if isinstance(tool, str):
        trace.context["tool_family"] = (
            "shell" if tool in {"Bash", "Shell", "functions.exec", "functions.exec_command"}
            else "mcp" if tool.startswith("mcp__") else "other"
        )
    trace.emit("payload_received")


@contextmanager
def trace_stage(stage: str) -> Iterator[None]:
    trace = get_trace()
    if trace is None or not trace.enabled:
        yield
        return
    previous = trace.stage
    trace.stage = stage if _TOKEN.fullmatch(stage) else "unknown"
    started = time.monotonic()
    trace.emit("stage_started")
    try:
        yield
    except BaseException:
        trace.last_interrupted_stage = trace.stage
        trace.emit("stage_interrupted", duration_ms=(time.monotonic() - started) * 1000)
        raise
    else:
        trace.emit("stage_completed", duration_ms=(time.monotonic() - started) * 1000)
    finally:
        trace.stage = previous


def finish_trace(status: str = "ok", **fields: Any) -> None:
    trace = get_trace()
    if trace is not None:
        if status == "ok" and trace.error_count:
            status = "degraded"
        trace.emit("completed", status=status, error_count=trace.error_count, **fields)
        trace.finished = True
        _ACTIVE.set(None)
