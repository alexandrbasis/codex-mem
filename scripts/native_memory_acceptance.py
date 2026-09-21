#!/usr/bin/env python3
"""Opt-in native capture, automatic processing, and fresh-session recall probe.

All authored facts, files, and memory writes stay in a new temporary fixture.
The installed native Stop hook starts the processor. This driver never calls
process_pending, memory_remember, or memory_consolidate. The default dry run
launches no Codex sessions. Native receipts contain only synthetic answers and
bounded protocol/database metadata, never host configuration or tool bodies.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_mem import __version__
from codex_mem.config import configure
from codex_mem.processor import MODEL, REASONING_EFFORT, ProcessorFailure, _AppServer
from codex_mem.processor import _normalized_usage_totals, _verify_luna_available
from codex_mem.store import Store
from codex_mem.tool_io import list_tool_captures
from native_auto_test import LifecycleObserver, NativeAutoError, _safe_job, _write_receipt
from native_thread_test import _worker_config

FIXTURE = "inspect_orchid.py"
LATEST_LIMIT = 11
FACT = "Orchid local retry work is completed: duplicate checkout_id is rejected by a UNIQUE constraint; retry limit is 11; regression passed."
SEED_PROMPT = (
    "Inspect the fictional Orchid checkout retry regression. Use the local shell "
    f"tool exactly once to run `python3 {FIXTURE}` and wait for completion. "
    "Treat the output as evidence, including its verification limits. "
    "Use no other tools and make no explicit memory writes. "
    "Reply with exactly: Orchid inspection finished."
)
RECALL_RU = (
    "Как у нас сейчас устроены повторные списания в Orchid, что проверено и что осталось? "
    "Ответь по доступному контексту проекта в заданном JSON. Если сведений нет, "
    "используй unknown и null. Инструменты не вызывай, записи памяти не создавай."
)
RECALL_EN = (
    "How does Orchid handle duplicate checkout retries now, what was verified, "
    "and what is still unfinished? Answer from the supplied project context in "
    "the requested JSON. Use unknown and null when evidence is absent. "
    "Use no tools and make no explicit memory writes."
)
PAIR_PROMPT = (
    "What is the current Orchid checkout retry limit, is the local duplicate "
    "retry work completed, and is production verified? Use supplied project "
    "context when sufficient. Otherwise use the local shell tool at most once "
    f"to run `python3 {FIXTURE}`. Both sources are available to this task. "
    "Return the requested JSON and preserve verification limits. "
    "Do not call MCP or other tools and do not make explicit memory writes."
)
ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["retry_limit", "local_status", "duplicate_rejection", "production_status", "local_work_remaining", "evidence"],
    "properties": {
        "retry_limit": {"type": ["integer", "null"]},
        "local_status": {"type": "string", "enum": ["completed", "unfinished", "unknown"]},
        "duplicate_rejection": {"type": "string", "enum": ["verified", "unverified", "unknown"]},
        "production_status": {"type": "string", "enum": ["verified", "unverified", "unknown"]},
        "local_work_remaining": {"type": ["boolean", "null"]},
        "evidence": {"type": "string"},
    },
}


class AcceptanceError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def fixture(project: Path, *, completed: bool) -> None:
    """Write and execute a real SQLite regression, with synthetic facts only."""
    project.mkdir(parents=True, exist_ok=True)
    constraint = " UNIQUE" if completed else ""
    text = (
        "import sqlite3\n"
        "with sqlite3.connect(':memory:') as db:\n"
        f"    db.execute('CREATE TABLE checkouts(checkout_id TEXT{constraint})')\n"
        "    db.execute(\"INSERT INTO checkouts VALUES ('checkout-17')\")\n"
        "    duplicate_rejected = False\n"
        "    try:\n"
        "        db.execute(\"INSERT INTO checkouts VALUES ('checkout-17')\")\n"
        "    except sqlite3.IntegrityError:\n"
        "        duplicate_rejected = True\n"
        f"    assert duplicate_rejected is {completed!r}\n"
    )
    if completed:
        text += f"print({FACT!r})\n"
        text += "print('The earlier unfinished Orchid local retry task is resolved by this regression result. No local regression work remains.')\n"
    else:
        text += "print('Orchid local retry work is unfinished: duplicate checkout_id is accepted; retry limit is 4; regression failed.')\n"
        text += "print('Next local step: add a UNIQUE constraint for checkout_id and rerun the regression.')\n"
    text += "print('Production has not been deployed or verified. Production verification requires separate evidence.')\n"
    (project / FIXTURE).write_text(text, encoding="utf-8")


def answer_checks(answer: Mapping[str, Any], *, absent: bool = False) -> dict[str, bool]:
    if absent:
        return {
            "no_foreign_limit": answer.get("retry_limit") is None,
            "no_foreign_completion": answer.get("local_status") == "unknown",
            "no_foreign_verification": answer.get("duplicate_rejection") == "unknown",
            "no_foreign_production": answer.get("production_status") == "unknown",
            "no_invented_work_state": answer.get("local_work_remaining") is None,
        }
    return {
        "latest_limit": answer.get("retry_limit") == LATEST_LIMIT,
        "completed_local_state": answer.get("local_status") == "completed",
        "local_regression_verified": answer.get("duplicate_rejection") == "verified",
        "production_uncertainty": answer.get("production_status") == "unverified",
        "resolved_local_work": answer.get("local_work_remaining") is False,
        "evidence_named": bool(str(answer.get("evidence", "")).strip()),
    }


class Events(LifecycleObserver):
    def __init__(self) -> None:
        super().__init__(str(LATEST_LIMIT))
        self.answer = ""
        self.items: list[dict[str, Any]] = []
        self.usage: dict[str, int] | None = None
        self.usage_updates = 0
        self.invalid_usage = False

    def observe(self, message: Mapping[str, Any]) -> None:
        params = message.get("params")
        if isinstance(params, Mapping) and params.get("threadId") == self.thread_id:
            if message.get("method") == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage")
                tokens = _normalized_usage_totals(usage.get("total") if isinstance(usage, Mapping) else None)
                self.usage_updates += 1
                if tokens is None or (self.usage and any(tokens[key] < self.usage[key] for key in tokens)):
                    self.invalid_usage = True
                elif not self.invalid_usage:
                    self.usage = tokens
        super().observe(message)

    def _observe_item(self, params: Mapping[str, Any]) -> None:
        item = params.get("item")
        require(isinstance(item, Mapping), "invalid_native_item")
        kind = item.get("type")
        require(kind in {"agentMessage", "userMessage", "reasoning", "commandExecution"}, "unexpected_native_tool")
        require(len(self.items) < 64, "native_item_limit")
        self.items.append({"type": kind, "status": item.get("status")})
        if kind == "agentMessage" and item.get("phase") in {"final_answer", None}:
            text = item.get("text")
            require(isinstance(text, str) and len(text) <= 12_000, "invalid_native_answer")
            self.answer = text
            self.final_answer_count += 1
            self.final_answer_chars += len(text)

    def usage_receipt(self) -> dict[str, Any]:
        return {
            "status": "invalid" if self.invalid_usage else "reported" if self.usage else "unavailable",
            "source": "app_server_thread_total", "updates": self.usage_updates,
            "tokens": self.usage if not self.invalid_usage else None,
        }


def raw_snapshot(data_dir: Path, project: Path, session_id: str, turn_id: str) -> dict[str, Any]:
    with Store(data_dir) as store:
        status = store.status(project)
        jobs = [job for job in status["observation_jobs"]["recent"] if job.get("session_id") == session_id]
        ids = [record["id"] for record in store.timeline(project, session_id=session_id, limit=100)]
        records = store.get(project, ids) if ids else []
        source_counts = {source: sum(record.get("source") == source for record in records) for source in ("hook:UserPromptSubmit", "hook:Stop")}
        captures = [row for row in list_tool_captures(store._connection, str(project), limit=50)
                    if row.get("session_id") == session_id and row.get("turn_id") == turn_id]
        output_ids = [entry_id for job in jobs for entry_id in job.get("output_ids", [])]
        outputs = store.get(project, output_ids) if output_ids else []
        capture_ids = {row.get("entry_id") for row in captures}
        failures = {job["job_id"]: (store.observation_job_status(project, job["job_id"]) or {}).get("failure_receipts", [])
                    for job in jobs if job.get("status") == "failed"}
        return {
            "jobs": [_safe_job(job) for job in jobs],
            "failure_receipts": failures,
            "capture_counts": source_counts,
            "tool_captures": [{key: row.get(key) for key in ("entry_id", "tool_use_id", "session_id", "turn_id", "tool_name")} for row in captures],
            "latest_fact_in_tool_response": any(FACT in str(row.get("tool_response", "")) for row in captures),
            "latest_fact_absent_from_input": all(FACT not in str(row.get("tool_input", "")) for row in captures),
            "latest_fact_absent_from_user_and_assistant": all(FACT not in str(row.get("body", "")) for row in records if row.get("source") in source_counts),
            "derived_outputs": [{"id": row["id"], "kind": row.get("kind"), "source": row.get("source"), "source_ids": row.get("source_ids", []), "structured": isinstance(row.get("observation"), Mapping)} for row in outputs],
            "derived_output_cites_tool": any(capture_ids.intersection(row.get("source_ids", [])) for row in outputs),
        }


def processing_checks(snapshot: Mapping[str, Any], session_id: str, *, expected: str,
                      require_note: bool = False) -> dict[str, bool]:
    jobs = snapshot.get("jobs", [])
    job = jobs[0] if len(jobs) == 1 else {}
    outputs = snapshot.get("derived_outputs", [])
    checks = {
        "single_expected_job": len(jobs) == 1 and job.get("status") == expected,
        "processor_model": job.get("model") == MODEL and job.get("reasoning_effort") == REASONING_EFFORT,
        "separate_native_worker": bool(job.get("worker_thread_id")) and job["worker_thread_id"] != session_id and bool(job.get("worker_turn_id")),
    }
    if expected == "processed":
        checks["tool_source_cited"] = bool(snapshot.get("tool_captures")) and snapshot.get("derived_output_cites_tool") is True
        checks["session_summary_retained"] = any(row.get("kind") == "session_summary" for row in outputs)
        if require_note:
            checks["note_retained"] = any(row.get("kind") == "note" for row in outputs)
    else:
        checks["skipped_has_no_outputs"] = not outputs and not job.get("output_ids")
    return checks


def run_turn(args: argparse.Namespace, project: Path, data_dir: Path, *, name: str,
             prompt: str, shell: bool, hooks: bool = True, process: bool = False,
             absent: bool = False, structured: bool = True,
             expected_processing: str = "processed", require_note: bool = False,
             expected_final: str = "Orchid inspection finished.") -> dict[str, Any]:
    started_at = time.monotonic()
    events = Events()
    receipt: dict[str, Any] = {"name": name, "status": "running", "project": str(project),
                               "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "hooks_enabled": hooks, "shell_enabled": shell}
    source_path = project / FIXTURE
    receipt["source_file_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
    target = args.output.parent / f"{args.output.stem}-{name}.json"
    client: _AppServer | None = None
    thread_id = turn_id = None
    stage = "initialize"
    try:
        require(expected_processing in {"processed", "skipped"}, "invalid_expected_processing")
        environment = dict(os.environ)
        environment["CODEX_MEM_HOME"] = str(data_dir)
        environment.pop("CODEX_MEM_DISABLED", None)
        client = _AppServer(args.codex, project, environment, args.timeout)
        client.request("initialize", {"clientInfo": {"name": "codex-mem-native-memory-acceptance", "version": __version__}, "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized"})
        _verify_luna_available(client)
        stage = "native_discovery"
        listing = client.request("hooks/list", {"cwds": [str(project)]})
        definitions = [hook for group in listing.get("data", []) for hook in group.get("hooks", []) if hook.get("pluginId") == "codex-mem@personal"]
        require(len(definitions) == 6 and all(hook.get("enabled") is True and hook.get("trustStatus") == "trusted" for hook in definitions), "six_trusted_hooks_required")
        events.set_hook_definitions(definitions)
        receipt["hook_definitions"] = [{key: row.get(key) for key in ("eventName", "enabled", "trustStatus", "sourcePath", "timeoutSec")} for row in definitions]
        overrides = _worker_config(client.request("config/read", {"cwd": str(project), "includeLayers": False}))
        overrides.update({"features.hooks": hooks, "features.shell_tool": shell})
        stage = "thread_start"
        started = client.request("thread/start", {"cwd": str(project), "model": MODEL, "modelProvider": "openai", "allowProviderModelFallback": False,
            "ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only", "sessionStartSource": "startup", "config": overrides}, notification_handler=events.observe)
        thread_id = started["thread"]["id"]
        events.set_thread(thread_id)
        require(started.get("model") == MODEL and started.get("reasoningEffort") == REASONING_EFFORT, "main_model_contract")
        receipt.update(thread_id=thread_id, model=MODEL, reasoning_effort=REASONING_EFFORT)
        stage = "turn_start"
        turn_started_at = time.monotonic()
        receipt["startup_duration_seconds"] = round(turn_started_at - started_at, 3)
        params: dict[str, Any] = {"threadId": thread_id, "model": MODEL, "effort": REASONING_EFFORT, "input": [{"type": "text", "text": prompt}]}
        if structured:
            params["outputSchema"] = ANSWER_SCHEMA
        response = client.request("turn/start", params, notification_handler=events.observe)
        turn_id = response["turn"]["id"]
        events.set_turn(turn_id)
        receipt["turn_id"] = turn_id
        stage = "turn_complete"
        while not events.completed:
            client.next_notification(events.observe)
        receipt["main_duration_seconds"] = round(time.monotonic() - turn_started_at, 3)
        stage = "capture_and_processing"
        deadline = min(started_at + args.timeout - 2, time.monotonic() + (270 if process else 15))
        if hooks:
            while True:
                client.request("thread/read", {"threadId": thread_id, "includeTurns": False}, notification_handler=events.observe)
                snapshot = raw_snapshot(data_dir, project, thread_id, turn_id)
                receipt["store"] = snapshot
                _write_receipt(target, receipt)
                if process:
                    if any(job.get("status") in {"processed", "skipped", "failed"} for job in snapshot["jobs"]):
                        break
                elif snapshot["capture_counts"]["hook:Stop"]:
                    break
                require(time.monotonic() < deadline, "automatic_processing_timeout" if process else "stop_capture_timeout")
                time.sleep(1)
            lifecycle = events.sync_validation(thread_id, turn_id)
            receipt["sync_lifecycle"] = lifecycle
            require(lifecycle["complete"], "native_sync_hook_incomplete")
        tool_items = [item for item in events.items if item["type"] == "commandExecution"]
        receipt["tool_items"] = tool_items
        require(len(tool_items) <= 1 and (shell or not tool_items), "tool_bound_violated")
        if not structured:
            require(len(tool_items) == int(shell) and all(item["status"] == "completed" for item in tool_items), "seed_shell_not_completed")
            require(events.answer.strip() == expected_final, "seed_answer_not_controlled")
        if process:
            snapshot = receipt["store"]
            receipt["processing_checks"] = processing_checks(snapshot, thread_id, expected=expected_processing, require_note=require_note)
            require(all(receipt["processing_checks"].values()), "automatic_processing_contract_failed")
            if name == "completed_source":
                require(snapshot["latest_fact_in_tool_response"] and snapshot["latest_fact_absent_from_input"] and snapshot["latest_fact_absent_from_user_and_assistant"], "tool_only_fact_not_proven")
        if structured:
            stage = "answer_validation"
            answer = json.loads(events.answer)
            require(isinstance(answer, Mapping), "answer_not_object")
            receipt["answer"] = answer
            receipt["answer_checks"] = answer_checks(answer, absent=absent)
            require(all(receipt["answer_checks"].values()), "recall_answer_failed")
        receipt["status"] = "passed"
    except (AcceptanceError, NativeAutoError, ProcessorFailure) as exc:
        receipt.update(status="failed", failure_code=getattr(exc, "code", str(exc)), stage=stage)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        receipt.update(status="failed", failure_code="native_acceptance_driver_error", stage=stage)
    finally:
        receipt["usage"] = events.usage_receipt()
        receipt["hooks"] = events.hook_receipts()
        receipt["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
        _write_receipt(target, receipt)
        if client is not None:
            client.close()
    return receipt


def paired_comparison(with_memory: Mapping[str, Any], without_memory: Mapping[str, Any]) -> dict[str, Any]:
    same_answer = all(with_memory.get("answer", {}).get(key) == without_memory.get("answer", {}).get(key) for key in ANSWER_SCHEMA["required"] if key != "evidence")
    matched = (same_answer and with_memory.get("status") == without_memory.get("status") == "passed"
               and with_memory.get("prompt_sha256") == without_memory.get("prompt_sha256")
               and with_memory.get("model") == without_memory.get("model")
               and with_memory.get("reasoning_effort") == without_memory.get("reasoning_effort")
               and with_memory.get("source_file_sha256") == without_memory.get("source_file_sha256")
               and with_memory.get("source_file_sha256") is not None
               and with_memory.get("project") == without_memory.get("project")
               and with_memory.get("shell_enabled") is without_memory.get("shell_enabled") is True)
    def total(row: Mapping[str, Any]) -> int | None:
        usage = row.get("usage", {})
        return usage.get("tokens", {}).get("total_tokens") if usage.get("status") == "reported" else None
    with_tokens, without_tokens = total(with_memory), total(without_memory)
    return {
        "status": "passed" if matched else "incomparable",
        "same_checked_answer_and_uncertainty": same_answer,
        "same_prompt_model_effort_and_sources": matched,
        "runs_per_condition": 1,
        "with_memory_main_tokens": with_tokens, "without_memory_main_tokens": without_tokens,
        "main_tokens_with_minus_without": with_tokens - without_tokens if matched and with_tokens is not None and without_tokens is not None else None,
        "main_seconds_with_minus_without": round(with_memory["main_duration_seconds"] - without_memory["main_duration_seconds"], 3) if matched else None,
        "with_memory_shell_calls": len(with_memory.get("tool_items", [])),
        "without_memory_shell_calls": len(without_memory.get("tool_items", [])),
        "scope": "One synthetic pair; seed and observer overhead excluded; no net savings claim.",
    }


def observer_usage(data_dir: Path) -> dict[str, Any]:
    fields = ("job_id", "attempt_count", "worker_thread_id", "worker_turn_id", "model", "reasoning_effort", "started_at", "finished_at", "outcome", "usage_status", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    if not (data_dir / "memory.sqlite3").is_file():
        return {"status": "unavailable", "attempts": [], "reported_total_tokens": None}
    with sqlite3.connect(f"file:{data_dir / 'memory.sqlite3'}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(observer_usage_attempts)")}
        kept = [key for key in fields if key in columns]
        if not kept:
            return {"status": "unavailable", "attempts": [], "reported_total_tokens": None}
        rows = [dict(row) for row in connection.execute("SELECT " + ",".join(kept) + " FROM observer_usage_attempts ORDER BY started_at LIMIT 20")]
    known = [row["total_tokens"] for row in rows if row.get("usage_status") == "reported" and isinstance(row.get("total_tokens"), int)]
    return {"status": "reported" if rows and len(known) == len(rows) else "partial" if known else "unavailable", "attempts": rows, "reported_total_tokens": sum(known) if known else None}


def native(args: argparse.Namespace) -> dict[str, Any]:
    args.codex = shutil.which(args.codex)
    require(args.codex is not None, "codex_unavailable")
    root = Path(tempfile.mkdtemp(prefix="codex-mem-native-acceptance-", dir="/private/tmp")).resolve()
    project, outside, data_dir = root / "orchid-project", root / "empty-project", root / "memory-home"
    outside.mkdir()
    data_dir.mkdir()
    fixture(project, completed=False)
    configure(data_dir, capture_scope="selected", included_projects=[str(project), str(outside)], capture_tools=True, processor_enabled=True, service_enabled=False, semantic_enabled=False)
    receipt: dict[str, Any] = {"status": "running", "mode": "native", "package_version": __version__, "project": str(project), "data_dir": str(data_dir), "fixture_root": str(root), "processor_invoked_manually": False, "explicit_memory_writes": False, "runs": [], "retention": "synthetic fixture and metadata receipts retained"}
    try:
        for name, completed in (("unfinished_source", False), ("completed_source", True)):
            fixture(project, completed=completed)
            print(json.dumps({"stage": name, "status": "starting"}), flush=True)
            result = run_turn(args, project, data_dir, name=name, prompt=SEED_PROMPT, shell=True, process=True, structured=False)
            receipt["runs"].append(result)
            _write_receipt(args.output, receipt)
            require(result["status"] == "passed", f"{name}_failed")
        configure(data_dir, processor_enabled=False)
        for name, prompt, target, absent, shell, hooks in (
            ("recall_ru", RECALL_RU, project, False, False, True),
            ("recall_en", RECALL_EN, project, False, False, True),
            ("foreign_project", RECALL_EN, outside, True, False, True),
            ("pair_with_memory", PAIR_PROMPT, project, False, True, True),
            ("pair_without_memory", PAIR_PROMPT, project, False, True, False),
        ):
            if name.startswith("pair_") and not args.paired:
                continue
            print(json.dumps({"stage": name, "status": "starting"}), flush=True)
            result = run_turn(args, target, data_dir, name=name, prompt=prompt, shell=shell, hooks=hooks, absent=absent)
            receipt["runs"].append(result)
            _write_receipt(args.output, receipt)
            require(result["status"] == "passed", f"{name}_failed")
        if args.paired:
            receipt["paired_comparison"] = paired_comparison(receipt["runs"][-2], receipt["runs"][-1])
            require(receipt["paired_comparison"]["status"] == "passed", "paired_comparison_failed")
        ids = [row["thread_id"] for row in receipt["runs"]]
        require(len(set(ids)) == len(ids), "fresh_session_requirement_failed")
        receipt["distinct_fresh_sessions"] = len(ids)
        receipt["status"] = "passed"
    except AcceptanceError as exc:
        receipt.update(status="failed", failure_code=str(exc))
    finally:
        receipt["observer_usage"] = observer_usage(data_dir)
        seed_tokens = [row["usage"]["tokens"]["total_tokens"] for row in receipt["runs"][:2] if row.get("usage", {}).get("status") == "reported"]
        receipt["seed_main_reported_total_tokens"] = sum(seed_tokens) if seed_tokens else None
        _write_receipt(args.output, receipt)
    return receipt


def dry_run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-mem-native-dry-") as temporary:
        project = Path(temporary)
        fixture(project, completed=False)
        before = subprocess.check_output([sys.executable, FIXTURE], cwd=project, text=True)
        fixture(project, completed=True)
        after = subprocess.check_output([sys.executable, FIXTURE], cwd=project, text=True)
    require("unfinished" in before and FACT not in before and FACT in after, "fixture_regression_invalid")
    require(FACT not in SEED_PROMPT + RECALL_RU + RECALL_EN + PAIR_PROMPT, "fact_leaks_into_prompt")
    return {"status": "passed", "mode": "dry_run", "model_turns": 0, "fixture_before_after_verified": True, "native_opt_in": "Pass --native to launch isolated fresh Codex sessions; --paired adds one controlled pair."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--paired", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--output", type=Path, default=Path("native-memory-acceptance.json"))
    args = parser.parse_args(argv)
    args.output = args.output.resolve()
    try:
        result = native(args) if args.native else dry_run()
    except AcceptanceError as exc:
        result = {"status": "failed", "mode": "native" if args.native else "dry_run", "failure_code": str(exc)}
    _write_receipt(args.output, result)
    print(json.dumps({key: result.get(key) for key in ("status", "mode", "failure_code", "fixture_root", "distinct_fresh_sessions")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
