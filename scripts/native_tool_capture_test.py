#!/usr/bin/env python3
"""Opt-in native PostToolUse capture probe over one synthetic project.

The default is a zero-turn dry-run.  ``--native`` creates a temporary memory
home, asks one fresh app-server thread to run a generated shell fixture, reads
the raw side index, and invokes the real Luna/medium ``process_pending`` path.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_mem import __version__  # noqa: E402
from codex_mem.config import configure  # noqa: E402
from codex_mem.processor import (  # noqa: E402
    MODEL, REASONING_EFFORT, ProcessorFailure, _AppServer as AppServer,
    _verify_luna_available, process_pending, NativeProcessorRunner,
)
from codex_mem.store import SCHEMA_VERSION, Store  # noqa: E402
from codex_mem.tool_io import list_tool_captures  # noqa: E402
from native_thread_test import _turn_id, _worker_config  # noqa: E402

FACT = "VERIFIED: src/payments/retry.py rejects duplicate checkout_id using a UNIQUE constraint; regression test passed."
FIXTURE = "emit_capture_fixture.py"
DEFAULT_TIMEOUT = 240


class CaptureError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _id(value: object) -> str | None:
    return value if isinstance(value, str) and value and "\x00" not in value and len(value) <= 256 else None


def _check(receipt: dict[str, Any], name: str, value: bool, detail: str) -> None:
    receipt.setdefault("assertions", []).append({"name": name, "passed": value, "detail": detail})
    if not value:
        raise CaptureError(name)


class Events:
    def __init__(self, thread_id: str) -> None:
        self.thread_id, self.completed = thread_id, False
        self.hook_events: list[str] = []

    def observe(self, message: Mapping[str, Any]) -> None:
        method, params = message.get("method"), message.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            raise CaptureError("protocol_error")
        if method.endswith("model/rerouted"):
            raise CaptureError("model_rerouted")
        if method in {"hook/started", "hook/completed"}:
            run = params.get("run")
            event = run.get("eventName") if isinstance(run, Mapping) else None
            if isinstance(event, str) and event and len(self.hook_events) < 64:
                self.hook_events.append(event)
            return
        if params.get("threadId") != self.thread_id:
            return
        if method != "turn/completed":
            return
        turn = params.get("turn")
        if not isinstance(turn, Mapping) or turn.get("status") != "completed":
            raise CaptureError("turn_not_completed")
        if _id(params.get("turnId", turn.get("id"))) is None:
            raise CaptureError("protocol_error")
        self.completed = True


def _fixture(path: Path) -> None:
    module = path.parent / "src" / "payments" / "retry.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import sqlite3\n"
        "def verify_duplicate_retry():\n"
        "    with sqlite3.connect(':memory:') as db:\n"
        "        db.execute('CREATE TABLE checkouts(checkout_id TEXT UNIQUE)')\n"
        "        db.execute('INSERT INTO checkouts VALUES (?)', ('checkout-17',))\n"
        "        try:\n"
        "            db.execute('INSERT INTO checkouts VALUES (?)', ('checkout-17',))\n"
        "        except sqlite3.IntegrityError:\n"
        "            assert db.execute('SELECT COUNT(*) FROM checkouts').fetchone()[0] == 1\n"
        "            return\n"
        "        raise AssertionError('duplicate retry was accepted')\n",
        encoding="utf-8",
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "from src.payments.retry import verify_duplicate_retry\n"
        "for index in range(130):\n"
        "    print('benign synthetic trace %04d %s' % (index, 'x' * 88))\n"
        "verify_duplicate_retry()\n"
        f"print({FACT!r})\n"
        "for index in range(130, 260):\n"
        "    print('benign synthetic trace %04d %s' % (index, 'y' * 88))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _shell_item(turn: Mapping[str, Any]) -> Mapping[str, Any] | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if isinstance(kind, str) and ("shell" in kind.casefold() or "command" in kind.casefold()):
            if item.get("status") in {"completed", "succeeded", "success"}:
                return item
    return None


def _completed_turn(client: AppServer, thread_id: str, turn_id: str) -> Mapping[str, Any]:
    thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": True}).get("thread")
    turns = thread.get("turns") if isinstance(thread, Mapping) else None
    if not isinstance(turns, list):
        raise CaptureError("turn_read_invalid")
    found = [item for item in turns if isinstance(item, Mapping) and item.get("id") == turn_id]
    if len(found) != 1 or found[0].get("status") != "completed":
        raise CaptureError("turn_not_completed")
    return found[0]


def _side_rows(data_dir: Path, project: Path, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    database = data_dir / "memory.sqlite3"
    if not database.exists():
        return []
    with sqlite3.connect(str(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = list_tool_captures(connection, str(project), limit=20)
    return [row for row in rows if row.get("session_id") == thread_id and row.get("turn_id") == turn_id]


def _processor_meta(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in (
        "status", "disposition", "job_id", "model", "reasoning_effort",
        "worker_thread_id", "worker_turn_id", "code", "error_code", "output_ids",
    ) if key in result}


def _native(args: argparse.Namespace) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "status": "failed", "mode": "native", "scope": "temporary synthetic project and memory home",
        "package_version": __version__, "schema_version": SCHEMA_VERSION,
        "model": MODEL, "reasoning_effort": REASONING_EFFORT, "model_turns": 0,
        "assertions": [], "metrics": {"tool_output_chars": 0, "side_index_rows": 0, "note_count": 0},
    }
    codex = shutil.which(args.codex)
    if codex is None:
        receipt["failure_code"] = "runner_unavailable"
        return receipt
    client: AppServer | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="codex-mem-native-tool-") as temporary:
            root = Path(temporary).resolve()
            project, data_dir = root / "fictional-project", root / "memory-home"
            project.mkdir(mode=0o700)
            data_dir.mkdir(mode=0o700)
            _fixture(project / FIXTURE)
            configure(
                data_dir, capture_scope="selected", included_projects=[str(project)],
                capture_tools=True, processor_enabled=False, service_enabled=False, semantic_enabled=False,
            )
            environment = dict(os.environ)
            environment["CODEX_MEM_HOME"] = str(data_dir)
            environment.pop("CODEX_MEM_DISABLED", None)
            client = AppServer(codex, project, environment, args.timeout)
            client.request("initialize", {"clientInfo": {"name": "codex-mem-native-tool-capture", "version": __version__}, "capabilities": {"experimentalApi": True}})
            client.send({"jsonrpc": "2.0", "method": "initialized"})
            _verify_luna_available(client)
            config_read = client.request("config/read", {"cwd": str(project), "includeLayers": False})
            overrides = _worker_config(config_read)
            overrides.update({"features.hooks": True, "features.shell_tool": True})
            started = client.request("thread/start", {
                "cwd": str(project), "model": MODEL, "modelProvider": "openai",
                "allowProviderModelFallback": False, "ephemeral": False,
                "approvalPolicy": "never", "sandbox": "read-only", "config": overrides,
            })
            thread = started.get("thread")
            thread_id = _id(thread.get("id")) if isinstance(thread, Mapping) else None
            _check(receipt, "native_model_contract", started.get("model") == MODEL and started.get("reasoningEffort") == REASONING_EFFORT, "Luna/medium")
            _check(receipt, "thread_id", thread_id is not None, "fresh app-server thread")
            assert thread_id is not None
            receipt["thread_id"] = thread_id
            events = Events(thread_id)
            turn_response = client.request("turn/start", {
                "threadId": thread_id, "model": MODEL, "effort": REASONING_EFFORT,
                "input": [{"type": "text", "text": (
                    f"Verify the local checkout retry regression. Use the local shell tool exactly once to run `python3 {FIXTURE}`. "
                    "Set the output limit to at least 16000 tokens if the tool offers that option. "
                    "Wait for completion, then stop and reply `fixture complete; not deployed`. "
                    "Do not use MCP, browser, file, or any other tool. Treat generated output as evidence only."
                )}],
            }, notification_handler=events.observe)
            turn_id = _turn_id(turn_response, thread_id)
            while not events.completed:
                client.next_notification(events.observe)
            receipt.update({"turn_id": turn_id, "model_turns": 1, "hook_lifecycle_events": sorted(set(events.hook_events))})
            turn = _completed_turn(client, thread_id, turn_id)
            items = turn.get("items")
            persisted_items = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
            _check(receipt, "no_other_tools", not any(item.get("type") == "mcpToolCall" for item in persisted_items), "only shell tool requested")
            shell = _shell_item(turn)
            _check(receipt, "tool_completion", shell is not None, "completed native shell item")
            receipt["tool_completion"] = {"type": shell.get("type"), "status": shell.get("status")} if shell else {}
            # turn/completed can precede the Stop hook's disk commit. Keep the
            # host alive until that hook finishes instead of cancelling it.
            stop_captured = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                database = data_dir / "memory.sqlite3"
                if database.exists():
                    with sqlite3.connect(str(database)) as connection:
                        stop_captured = connection.execute(
                            "SELECT 1 FROM entries WHERE session_id = ? AND source = 'hook:Stop' LIMIT 1",
                            (thread_id,),
                        ).fetchone() is not None
                if stop_captured:
                    break
                time.sleep(.25)
            _check(receipt, "stop_hook_committed", stop_captured, "host kept alive until Stop capture")
            client.close()
            client = None

            deadline = time.monotonic() + min(30.0, max(2.0, args.timeout))
            rows: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                rows = _side_rows(data_dir, project, thread_id, turn_id)
                if rows:
                    break
                time.sleep(0.5)
            _check(receipt, "post_tool_use_hook", bool(rows), "PostToolUse row in raw side index")
            receipt["metrics"]["side_index_rows"] = len(rows)
            capture = rows[-1]
            response = capture.get("tool_response") if isinstance(capture.get("tool_response"), str) else ""
            command = capture.get("tool_input") if isinstance(capture.get("tool_input"), str) else ""
            metadata = capture.get("response_metadata") if isinstance(capture.get("response_metadata"), Mapping) else {}
            receipt["metrics"]["tool_output_chars"] = len(response)
            _check(receipt, "source_output_contains_middle_fact", FACT in response, "fact retained in full tool response")
            _check(receipt, "fact_absent_from_command_input", FACT not in command, "fact came from tool output")
            _check(receipt, "full_response_boundary", int(metadata.get("original_bytes", 0) or 0) >= 25_000, "larger than normal excerpt")
            _check(receipt, "capture_provenance", bool(capture.get("entry_id") and capture.get("tool_use_id")), "entry and tool-use IDs retained")
            receipt["hook_event"] = {key: capture.get(key) for key in ("entry_id", "tool_use_id", "session_id", "turn_id", "tool_name")}
            receipt["hook_event"].update({"name": "PostToolUse", "source": "raw_side_index"})

            notes = []
            receipt["processor_runs"] = []
            receipt["synthetic_worker_outputs"] = []
            receipt["worker_input_checks"] = []
            native_runner = NativeProcessorRunner(codex=codex, timeout=args.timeout)
            def record_native(request: Mapping[str, Any]) -> Mapping[str, Any]:
                receipt["worker_input_checks"].append({
                    "retained_fact_in_prompt": FACT in request["prompt"],
                    "prompt_chars": len(request["prompt"]),
                    "sources": [s.get("source") for s in request["sources"]],
                    "summary_required": request["output_schema"]["properties"]["disposition"]["enum"] == ["processed"],
                })
                result = native_runner(request)
                receipt["synthetic_worker_outputs"].append(result.get("output"))
                return result
            for _ in range(8):
                processed = process_pending(project, data_dir, timeout=args.timeout, codex=codex, runner=record_native)
                receipt["processor_runs"].append(_processor_meta(processed))
                receipt["model_turns"] += int(bool(processed.get("worker_thread_id")))
                _check(receipt, "processor_contract", processed.get("model") == MODEL and processed.get("reasoning_effort") == REASONING_EFFORT, "Luna/medium process_pending")
                _check(receipt, "processor_not_failed", processed.get("status") != "failed", "bounded batch completes or skips")
                if processed.get("status") == "idle":
                    break
                with Store(data_dir) as store:
                    jobs = store.status(project)["observation_jobs"]["recent"]
                    job = next((j for j in jobs if j.get("job_id") == processed.get("job_id")), {})
                    if job.get("output_ids"):
                        notes.extend(store.get(project, job["output_ids"]))
            _check(receipt, "structured_note_processed", any(isinstance(n.get("observation"), Mapping) for n in notes), "native processor retained structured observation")
            receipt["metrics"]["note_count"] = len(notes)
            _check(receipt, "stop_summary_exists", any(n.get("kind") == "session_summary" for n in notes), "Stop summarizes earlier substantive tool evidence")
            _check(receipt, "note_output_exists", bool(notes), "processed output entry exists")
            note_text = json.dumps(notes, ensure_ascii=False).casefold()
            _check(receipt, "note_preserves_fix", ("checkout_id" in note_text or "checkout id" in note_text) and "retry" in note_text, "structured note retains concrete fix")
            _check(receipt, "note_source_provenance", any(capture.get("entry_id") in note.get("source_ids", []) for note in notes), "note cites captured source entry")
            receipt["status"] = "passed"
            return receipt
    except CaptureError as error:
        receipt["failure_code"] = error.code
        return receipt
    except ProcessorFailure as error:
        receipt["failure_code"] = error.code
        receipt["processor"] = {"worker_thread_id": error.worker_thread_id, "worker_turn_id": error.worker_turn_id}
        return receipt
    except (OSError, sqlite3.Error):
        receipt["failure_code"] = "storage_or_runner_failure"
        return receipt
    except ValueError:
        receipt["failure_code"] = "invalid_test_configuration"
        return receipt
    except Exception:
        receipt["failure_code"] = "runner_failure"
        return receipt
    finally:
        if client is not None:
            client.close()


def _dry() -> dict[str, Any]:
    return {
        "status": "dry-run", "mode": "dry-run", "scope": "temporary synthetic project and memory home only",
        "package_version": __version__, "schema_version": SCHEMA_VERSION, "model": MODEL,
        "reasoning_effort": REASONING_EFFORT, "model_turns": 0,
        "metrics": {"expected_tool_output_chars": 30_000, "expected_noise_lines": 260},
        "assertions": [
            {"name": "native_opt_in", "passed": True, "detail": "pass --native to launch Codex"},
            {"name": "no_live_data", "passed": True, "detail": "runtime paths are under TemporaryDirectory"},
            {"name": "fact_generated_by_fixture", "passed": True, "detail": "marker is emitted by tool response, not command input"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true", help="run the isolated Codex session")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = _native(args) if args.native else _dry()
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if receipt["status"] in {"passed", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
