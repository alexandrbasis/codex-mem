#!/usr/bin/env python3
"""Exercise native hook commands under concurrency with fictional local data."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem.config import configure
from codex_mem.store import Store


def run(plugin_root: Path, concurrency: int, rounds: int, events: list[str],
        python: Path | None = None, host_timeout: float | None = None) -> dict:
    manifest = json.loads((plugin_root / "hooks/hooks.json").read_text())
    commands = {
        event: next(hook for group in manifest["hooks"][event]
                    for hook in group["hooks"] if not hook.get("async"))
        for event in events
    }
    with tempfile.TemporaryDirectory(prefix="codex-mem-hook-latency-") as temporary:
        base = Path(temporary)
        fixtures = []
        for index in range(concurrency * rounds):
            fixture = base / str(index)
            project = fixture / "project"
            project.mkdir(parents=True)
            data = fixture / "memory"
            configure(data, capture_scope="all", service_enabled=False,
                      processor_enabled=False, semantic_enabled=False,
                      jev_retrieval_enabled=False)
            event = events[index % len(events)]
            payload = {"hook_event_name": event, "cwd": str(project),
                       "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4())}
            if event == "PostToolUse":
                payload.update(tool_name="Bash", tool_use_id=f"fixture-{index}",
                               tool_input={"command": "fixture_verification"},
                               tool_response={"output": "Fictional verification completed."})
            elif event == "Stop":
                payload["last_assistant_message"] = "Fictional verification completed."
            fixtures.append((index, project, data, payload))

        def trial(fixture: tuple) -> dict:
            index, project, data, payload = fixture
            event = payload["hook_event_name"]
            native = commands[event]
            command = native["command"]
            if python is not None:
                if not command.startswith("python3 "):
                    raise ValueError("Direct-Python probe requires an unchanged python3 command")
                command = shlex.quote(str(python)) + command[len("python3"):]
            started = time.monotonic()
            wall_started = datetime.now(timezone.utc)
            deadline = host_timeout if host_timeout is not None else float(native["timeout"])
            process = subprocess.Popen(
                ["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                env=dict(os.environ, PLUGIN_ROOT=str(plugin_root),
                         CODEX_MEM_HOME=str(data), CODEX_MEM_DISABLED="0",
                         CODEX_MEM_HOOK_LOG="1"),
            )
            spawn_ms = (time.monotonic() - started) * 1000
            timed_out = False
            try:
                stdout, _ = process.communicate(json.dumps(payload).encode(),
                    timeout=max(0.001, deadline - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, _ = process.communicate(timeout=5)
            elapsed = time.monotonic() - started
            timed_out |= elapsed >= deadline
            try:
                response = json.loads(stdout)
            except (ValueError, UnicodeError):
                response = None
            rows = []
            log = data / "logs/hooks.jsonl"
            if log.exists():
                for line in log.read_text().splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
            initial = {row["component"]: row for row in rows
                       if row.get("event") == "started"}
            ends = {row["component"]: row for row in rows
                    if row.get("event") == "completed"}
            captured = 0
            if not timed_out:
                with Store(data) as store:
                    captured = len(store.timeline(project))
            timings = {}
            if "supervisor" in initial:
                supervisor = datetime.fromisoformat(initial["supervisor"]["timestamp"])
                timings["host_to_supervisor_ms"] = round((supervisor - wall_started).total_seconds() * 1000, 3)
                if "worker" in initial:
                    worker = datetime.fromisoformat(initial["worker"]["timestamp"])
                    timings["supervisor_to_worker_ms"] = round((worker - supervisor).total_seconds() * 1000, 3)
            for component, row in ends.items():
                timings[component + "_ms"] = row["elapsed_ms"]
            return {"index": index, "event": event,
                    "passed": not timed_out and process.returncode == 0
                              and response == {"continue": True} and captured == 1,
                    "host_timeout": timed_out, "wall_ms": round(elapsed * 1000, 3),
                    "spawn_ms": round(spawn_ms, 3),
                    "captured": captured, "supervisor_status": ends.get("supervisor", {}).get("status"),
                    "timings": timings}

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(trial, fixtures))
    passed = sum(row["passed"] for row in results)
    return {"status": "passed" if passed == len(results) else "failed",
            "plugin_root": str(plugin_root), "concurrency": concurrency,
            "python_override": str(python) if python is not None else None,
            "host_timeout_override": host_timeout,
            "runs": len(results), "passed": passed,
            "host_timeouts": sum(row["host_timeout"] for row in results),
            "capture_failures": sum(row["captured"] != 1 for row in results),
            "maximum_wall_ms": max(row["wall_ms"] for row in results),
            "temporary_data_removed": True, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=ROOT)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--event", choices=["Stop", "PostToolUse"], action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", type=Path, help="Probe one direct interpreter instead of PATH python3")
    parser.add_argument("--host-timeout", type=float, help="Measure startup outside the configured native timeout")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16 or not 1 <= args.rounds <= 10:
        parser.error("concurrency must be 1..16 and rounds must be 1..10")
    if args.python is not None and not args.python.is_file():
        parser.error("--python must identify an existing interpreter")
    if args.host_timeout is not None and not 2 <= args.host_timeout <= 30:
        parser.error("--host-timeout must be 2..30 seconds")
    result = run(args.plugin_root.resolve(), args.concurrency, args.rounds,
                 args.event or ["Stop", "PostToolUse"],
                 args.python.resolve() if args.python is not None else None, args.host_timeout)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "results"}))
    raise SystemExit(result["status"] != "passed")
