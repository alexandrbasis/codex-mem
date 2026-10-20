#!/usr/bin/env python3
"""Run from a Codex plugin cache without installing Python packages."""
import os
import sys
import time

# This covers Python-side bootstrap only; shell/interpreter startup precedes it.
_BOOTSTRAP_STARTED = time.monotonic()
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The public hook gets a hard child deadline. Keep its worker on this lean path
# so neither entrypoint imports optional search/CLI code.
if __name__ == "__main__" and sys.argv[1:] == ["hook"]:
    from codex_mem.hook_diagnostics import begin_trace, finish_trace, trace_error, trace_event, trace_stage
    begin_trace("supervisor")
    trace_event("bootstrap_ready", duration_ms=(time.monotonic() - _BOOTSTRAP_STARTED) * 1000)
    try:
        with trace_stage("supervisor_import"):
            from codex_mem.hook_runner import run_hook
    except Exception as exc:
        trace_error("supervisor_import_failed", exc)
        finish_trace("failed")
        raise
    raise SystemExit(run_hook(Path(__file__).resolve(), fork_worker=True))

if __name__ == "__main__" and sys.argv[1:] == ["--hook-worker"]:
    from codex_mem.hook_diagnostics import begin_trace, finish_trace, trace_error, trace_event, trace_stage
    begin_trace("worker", run_id=os.environ.get("CODEX_MEM_HOOK_RUN_ID"))
    trace_event("bootstrap_ready", duration_ms=(time.monotonic() - _BOOTSTRAP_STARTED) * 1000)
    try:
        with trace_stage("worker_import"):
            from codex_mem.hooks import main as hook_main
    except Exception as exc:
        trace_error("worker_import_failed", exc)
        finish_trace("failed")
        raise
    try:
        result = hook_main()
    except Exception as exc:
        trace_error("worker_entrypoint_failed", exc)
        finish_trace("failed")
        raise
    raise SystemExit(result)

if __name__ == "__main__" and sys.argv[1:] == ["process-hook"]:
    from codex_mem.hook_diagnostics import begin_trace, trace_event
    trace = begin_trace("processor", run_id=os.environ.get("CODEX_MEM_HOOK_RUN_ID"))
    os.environ["CODEX_MEM_HOOK_RUN_ID"] = trace.run_id
    trace_event("bootstrap_ready", duration_ms=(time.monotonic() - _BOOTSTRAP_STARTED) * 1000)
    trace_event("processor_bootstrap")

runtime = Path.home() / ".local/share/codex-mem/runtime"
python = runtime / "bin/python3"
if (runtime / ".codex-mem-semantic-runtime.json").is_file() and python.is_file():
    if Path(sys.prefix).resolve() != runtime.resolve():
        try:
            os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
        except Exception as exc:
            if __name__ == "__main__" and sys.argv[1:] == ["process-hook"]:
                from codex_mem.hook_diagnostics import finish_trace, trace_error
                trace_error("processor_runtime_exec_failed", exc)
                finish_trace("failed")
            raise

try:
    from codex_mem.cli import main
except Exception as exc:
    if __name__ == "__main__" and sys.argv[1:] == ["process-hook"]:
        from codex_mem.hook_diagnostics import finish_trace, trace_error
        trace_error("processor_import_failed", exc)
        finish_trace("failed")
    raise

if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        if sys.argv[1:] == ["process-hook"]:
            from codex_mem.hook_diagnostics import finish_trace, trace_error
            trace_error("processor_entrypoint_failed", exc)
            finish_trace("failed")
        raise
    if sys.argv[1:] == ["process-hook"]:
        from codex_mem.hook_diagnostics import finish_trace
        finish_trace("ok" if result == 0 else "failed")
    raise SystemExit(result)
