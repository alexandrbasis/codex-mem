#!/usr/bin/env python3
"""Exercise capture, MCP consolidation and later recall with disposable data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem.config import configure
from codex_mem.store import Store


def run(schema_dir: Path | None = None) -> dict:
    validations = []
    validator = None
    if schema_dir:
        import jsonschema
        validator = jsonschema.validate
    with tempfile.TemporaryDirectory(prefix="codex-mem-acceptance-") as temporary:
        temp = Path(temporary)
        data = temp / "memory"
        project = str(temp / "project")
        other_project = str(temp / "other")
        # This verifies the synchronous hook/MCP contract with synthetic data.
        # Native processing, local model quality and detached service lifecycle
        # have separate acceptance drivers and must not start from this check.
        configure(data, capture_scope="selected", included_projects=[project],
                  processor_enabled=False, service_enabled=False, semantic_enabled=False)
        env = dict(os.environ, CODEX_MEM_HOME=str(data))
        env.pop("CODEX_MEM_DISABLED", None)
        common = {"cwd": project, "session_id": "acceptance-old", "transcript_path": None,
                  "model": "synthetic-test", "permission_mode": "default"}
        timings = []

        def hook(event: str, **fields):
            payload = dict(common, hook_event_name=event, **fields)
            if event == "PreCompact":
                payload.pop("permission_mode", None)
            slug = {"SessionStart": "session-start", "UserPromptSubmit": "user-prompt-submit",
                    "PostToolUse": "post-tool-use", "Stop": "stop", "PreCompact": "pre-compact"}[event]
            if validator:
                validator(payload, json.loads((schema_dir / (slug + ".command.input.schema.json")).read_text()))
            start = time.perf_counter()
            process = subprocess.run([sys.executable, str(ROOT / "scripts/codex-mem.py"), "hook"],
                                     input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10)
            timings.append((time.perf_counter() - start) * 1000)
            assert process.returncode == 0, "Hook process failed"
            assert not process.stderr, "Hook reported an error: " + process.stderr
            output = json.loads(process.stdout)
            assert output.get("continue", True), "Memory hook stopped the task"
            assert output.get("decision") != "block", "Memory hook requested a continuation"
            if validator:
                validator(output, json.loads((schema_dir / (slug + ".command.output.schema.json")).read_text()))
            validations.append(event)
            return output

        hook("SessionStart", source="startup")
        hook("UserPromptSubmit", turn_id="turn-one", prompt="Fix Aurora cache eviction. DATABASE_PASSWORD=hunter2 <private>hidden-test-value</private>")
        tool = dict(turn_id="turn-one", tool_name="Bash", tool_use_id="call-one",
                    tool_input={"command": "python3 -m unittest tests.test_aurora"},
                    tool_response="Aurora regression: 1 test passed. <private>RAW_OUTPUT_MUST_NOT_BE_STORED</private>")
        hook("PostToolUse", **tool)
        with Store(data) as store:
            assert not any(r["kind"] == "tool" for r in store.timeline(project)), "Private turn leaked tool capture"
        hook("UserPromptSubmit", turn_id="turn-two", prompt="Continue public Aurora cache eviction verification.")
        tool["turn_id"] = "turn-two"
        hook("PostToolUse", **tool)
        hook("PostToolUse", **tool)
        hook("Stop", turn_id="turn-two", stop_hook_active=False,
             last_assistant_message="Aurora cache eviction is fixed. The focused test passed; production behavior is unverified.")
        with Store(data) as store:
            records = store.timeline(project, limit=100)
            assert len([r for r in records if r["kind"] == "tool"]) == 1, "Duplicate tool capture"
            full = store.get(project, [r["id"] for r in records])
            material = json.dumps(full)
            for secret in ("hunter2", "hidden-test-value", "RAW_OUTPUT_MUST_NOT_BE_STORED"):
                assert secret not in material, "Raw/private content persisted"
            assert "Aurora regression: 1 test passed." in material, "Tool result evidence missing"
            assert not store.search(project, "Aurora"), "Raw evidence leaked into memory search"
            assert not store.search(other_project, "Aurora"), "Cross-project result leak"
            ids = [r["id"] for r in records]

        messages = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "codex-mem-acceptance", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_consolidate", "arguments": {
                "project": project, "title": "Aurora cache eviction verification", "body": "Synthetic acceptance summary: focused unittest passed for cache eviction. Production is unverified.",
                "source_ids": ids, "session_id": "acceptance-old", "source": "synthetic:test_aurora"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"project": project, "query": "Aurora"}}},
        ]
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/codex-mem.py"), "serve"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        replies = []
        try:
            for message in messages:
                process.stdin.write((json.dumps(message) + "\n").encode())
                process.stdin.flush()
                if "id" in message:
                    assert select.select([process.stdout], [], [], 10)[0], "MCP response timed out"
                    replies.append(json.loads(process.stdout.readline()))
            process.stdin.close()
            process.wait(timeout=10)
            assert process.returncode == 0 and not process.stderr.read(), "MCP subprocess failed"
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
        assert len(replies) == 4 and replies[0]["id"] == 0, "MCP notification/id protocol mismatch"
        for reply in replies:
            assert "error" not in reply and not reply.get("result", {}).get("isError"), "MCP operation failed"
        assert len(replies[1]["result"]["tools"]) == 8, "Unexpected tool catalog"
        with Store(data) as store:
            search = store.search(project, "Aurora")
            assert len(search) == 1, "Consolidated sources remain active"
            summary_id = search[0]["id"]
            summary = store.get(project, [summary_id])[0]
            assert set(summary["source_ids"]) == set(ids), "Consolidation lost provenance"
            assert len(store.get(project, ids)) == len(ids), "Consolidation erased sources"
        common["session_id"] = "acceptance-new"
        for source in ("startup", "compact", "compact"):
            output = hook("SessionStart", source=source)
            context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert "Aurora" in context and "unverified" in context, "Later session lost the summary or uncertainty"
            assert len(context) <= 6000, "Injected context exceeded budget"
        hook("PreCompact", turn_id="turn-two", trigger="auto")
        with Store(data) as store:
            target = temp / "backup.sqlite"
            store.backup(target)
            assert target.exists(), "Backup missing"
            deleted = store.forget(project, [summary_id])
            assert deleted["deleted"] == 1 and not store.get(project, [summary_id]), "Deletion did not persist"
            assert all(r["id"] != summary_id for r in store.search(project, "Aurora")), "Deleted row remains searchable"
            for i in range(500):
                store.remember(project, f"Benchmark {i}", f"Cache synthetic benchmark item {i}", dedupe_key=f"benchmark-{i}")
            query_ms = []
            for _ in range(20):
                start = time.perf_counter()
                assert store.search(project, "cache", limit=10)
                query_ms.append((time.perf_counter() - start) * 1000)
        return {"status": "passed", "data": "disposable synthetic project; no user history read",
                "checks": ["official hook payloads", "capture", "dedupe", "redaction", "private-turn gate", "project isolation", "MCP subprocess",
                           "consolidation provenance", "repeated compaction recall", "bounded injection", "backup", "forget"],
                "official_schema_validation": validator is not None, "hook_invocations": len(validations),
                "hook_process_ms_median": round(statistics.median(timings), 2),
                "search_500_records_ms_median": round(statistics.median(query_ms), 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-dir", type=Path, help="Optional official Codex generated hook schemas; requires jsonschema for verification only")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.schema_dir)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")
