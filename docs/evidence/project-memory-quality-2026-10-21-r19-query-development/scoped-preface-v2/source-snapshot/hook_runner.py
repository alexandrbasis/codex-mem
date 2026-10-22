"""Bound the POSIX native hook without reading its stdin in the parent.

This launcher targets the plugin's macOS/Linux hosts; selector pipe handling
has no Windows support guarantee.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time

from .hook_diagnostics import (
    begin_trace, finish_trace, get_trace, trace_error, trace_event, trace_stage,
)
from .hook_process import ForkedWorker, start_worker

HOOK_WORKER_TIMEOUT_SECONDS = 2.0
WORKER_REAP_TIMEOUT_SECONDS = 0.2
MAX_RESPONSE_BYTES = 64 * 1024
MAX_DIAGNOSTIC_BYTES = 4 * 1024
_FALLBACK_REASONS = {
    "timeout": "hook worker exceeded its deadline",
    "invalid-output": "hook worker returned an invalid response",
    "worker-failed": "hook worker exited with an error",
    "launch-failed": "hook worker could not start",
}
_SAFE_DIAGNOSTICS = frozenset(
    f"codex-mem hook: {reason}" for reason in (
        "configuration unavailable",
        "memory unavailable",
        "state unavailable",
        "privacy state unavailable",
        "context unavailable",
        "capture unavailable",
        "queue unavailable",
        "invalid arguments",
        "invalid input",
    )
)


def _safe_stderr(raw: bytes) -> bool:
    """Forward only known, fixed hook messages, never payload-derived text."""
    emitted = False
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if line in _SAFE_DIAGNOSTICS:
            emitted = True
            trace_event("worker_diagnostic", code=line.removeprefix("codex-mem hook: ").replace(" ", "_"))
            print(line, file=sys.stderr)
    return emitted


def _parse_response(raw: bytes) -> dict[str, object] | None:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        trace_event("response_rejected", reason="empty_output" if not raw else "output_limit")
        return None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        trace_error("invalid_response_json", exc)
        return None
    if not isinstance(value, dict) or value.get("continue") is not True:
        trace_event("response_rejected", reason="invalid_response_shape")
        return None
    return value


def _collect_output(child: subprocess.Popen[bytes] | ForkedWorker, deadline: float) -> tuple[bytes, bytes]:
    stdout = bytearray()
    stderr = bytearray()
    with selectors.DefaultSelector() as selector:
        for stream in (child.stdout, child.stderr):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is child.stdout:
                    stdout.extend(chunk)
                    if len(stdout) > MAX_RESPONSE_BYTES:
                        trace_event("output_limit", stdout_bytes=len(stdout), stderr_bytes=len(stderr))
                        raise ValueError("worker response too large")
                elif len(stderr) < MAX_DIAGNOSTIC_BYTES:
                    stderr.extend(chunk[: MAX_DIAGNOSTIC_BYTES - len(stderr)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        child.wait(timeout=remaining)
    return bytes(stdout), bytes(stderr)


def _stop_worker(child: subprocess.Popen[bytes] | ForkedWorker) -> None:
    if child.poll() is None:
        with trace_stage("worker_kill"):
            child.kill()
    try:
        with trace_stage("worker_reap"):
            child.wait(timeout=WORKER_REAP_TIMEOUT_SECONDS)
        trace_event("worker_reaped", worker_returncode=child.returncode)
    except subprocess.TimeoutExpired as exc:
        # A killed process can be slow to schedule. Never spend the remaining
        # native-host deadline waiting indefinitely for its exit notification.
        trace_error("worker_reap_timeout", exc)


def run_hook(launcher: str | Path, *, timeout: float = HOOK_WORKER_TIMEOUT_SECONDS,
             fork_worker: bool = False) -> int:
    """Run one worker and fail open if it misses the deadline or response contract.

    The child inherits stdin. The parent never consumes or copies the hook
    payload, and no child stdout reaches the host before validation.
    """
    trace = get_trace() or begin_trace("supervisor", timeout=timeout)
    deadline = time.monotonic() + timeout
    response: dict[str, object] | None = None
    diagnostics = b""
    failure = "launch-failed"
    child: subprocess.Popen[bytes] | ForkedWorker | None = None
    try:
        environment = os.environ.copy()
        environment["CODEX_MEM_HOOK_DEADLINE"] = repr(deadline)
        environment["CODEX_MEM_HOOK_RUN_ID"] = trace.run_id
        with trace_stage("worker_launch"):
            child = start_worker(launcher, environment, allow_fork=fork_worker)
        trace_event("worker_started", worker_pid=child.pid, timeout_ms=timeout * 1000,
                    code="fork" if isinstance(child, ForkedWorker) else "exec")
        previous_handlers: dict[signal.Signals, object] = {}
        if threading.current_thread() is threading.main_thread():
            def cancel(signum: int, _frame: object) -> None:
                # Kill only this hook worker. Detached service workers own their
                # own lifecycle and must not be signalled with its process group.
                trace_event("cancelled", signal=signum, worker_pid=child.pid)
                try:
                    _stop_worker(child)
                finally:
                    finish_trace("cancelled", signal=signum, worker_returncode=child.returncode)
                raise SystemExit(128 + signum)

            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, cancel)
        try:
            with trace_stage("worker_wait"):
                stdout, diagnostics = _collect_output(child, deadline)
            trace_event("worker_output", stdout_bytes=len(stdout), stderr_bytes=len(diagnostics),
                        worker_returncode=child.returncode)
            if child.returncode == 0:
                with trace_stage("response_validate"):
                    response = _parse_response(stdout)
                failure = "invalid-output"
            else:
                failure = "worker-failed"
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            failure = "timeout"
            trace_error("worker_timeout", exc)
        except ValueError as exc:
            failure = "invalid-output"
            trace_error("worker_invalid_output", exc)
        finally:
            try:
                _stop_worker(child)
                with trace_stage("worker_stream_close"):
                    child.stdout.close()
                    child.stderr.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    except OSError as exc:
        trace_error("worker_os_error", exc)

    fell_back = response is None
    had_diagnostics = False
    if fell_back:
        response = {
            "continue": True,
            "systemMessage": (
                f"codex-mem: {_FALLBACK_REASONS[failure]}; memory capture or context "
                "delivery for this hook invocation is unconfirmed or skipped."
            ),
        }
        print(f"codex-mem hook: worker {failure}", file=sys.stderr)
        trace_event("fallback", reason=failure.replace("-", "_"),
                    worker_returncode=child.returncode if child is not None else None)
    else:
        with trace_stage("diagnostic_write"):
            had_diagnostics = _safe_stderr(diagnostics)
    try:
        with trace_stage("response_write"):
            print(json.dumps(response, ensure_ascii=False), flush=True)
    except Exception as exc:
        trace_error("response_write_failed", exc)
        finish_trace("failed", code="response_write_failed")
        raise
    finish_trace("fallback" if fell_back else "degraded" if had_diagnostics else "ok",
                 code=failure.replace("-", "_") if fell_back else "worker_response")
    return 0
