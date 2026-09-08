#!/usr/bin/env python3
"""Bounded, provider-aware comparison of the two observation contracts.
The run is offline by default: it imports the pinned Claude Mem prompt/parser
at runtime and exercises Codex Mem capture/recovery in temporary stores. The
optional Codex run is provider-level evidence only; no quality or whole-pipeline
parity claim is made.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CORPUS = ROOT / "fixtures" / "comparison" / "corpus.json"
UPSTREAM = Path("/private/tmp/claude-mem-upstream-20260908")
UPSTREAM_SHA = "fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0"
FIXED_EPOCH_MS = 1_760_000_000_000
class ComparisonError(RuntimeError):
    pass
def j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def clip(value: str, limit: int = 2_000) -> str:
    return value if len(value) <= limit else value[:limit] + "..."
def command(args: Sequence[str], *, timeout: float = 20, env: Mapping[str, str] | None = None) -> tuple[int, str, str, bool]:
    try:
        result = subprocess.run(
            list(args), cwd=ROOT, env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout", True
    except OSError:
        return 127, "", "unavailable", False
    return result.returncode, clip(result.stdout), clip(result.stderr), False
def git_sha(path: Path) -> str | None:
    code, out, _, _ = command(["git", "-C", str(path), "rev-parse", "HEAD"], timeout=15)
    value = next((line.strip() for line in out.splitlines() if line.strip()), "")
    return value if code == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None
def binary(binary: str | None) -> dict[str, Any]:
    if not binary:
        return {"available": False}
    code, out, _, _ = command([binary, "--version"], timeout=15)
    version = next((line.strip() for line in out.splitlines() if line.strip()), None)
    return {"available": code == 0, "version": clip(version, 160) if version else None}
def availability(upstream: Path) -> dict[str, Any]:
    claude, bun, codex = shutil.which("claude"), shutil.which("bun"), shutil.which("codex")
    auth: dict[str, Any] = {"status": "unavailable"}
    if claude:
        _, out, _, _ = command([claude, "auth", "status"])
        try:
            raw = json.loads(out)
            auth = {
                "status": "read", "logged_in": bool(raw.get("loggedIn")),
                "auth_method": raw.get("authMethod") if isinstance(raw.get("authMethod"), str) else None,
                "api_provider": raw.get("apiProvider") if isinstance(raw.get("apiProvider"), str) else None,
            }
        except (json.JSONDecodeError, AttributeError, TypeError):
            auth = {"status": "unreadable", "logged_in": False}
    provider_vars = (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS",
        "VERTEXAI_PROJECT", "CLAUDE_MEM_ANTHROPIC_API_KEY", "CLAUDE_MEM_OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY",
    )
    cache = Path.home() / ".claude" / "plugins" / "cache" / "thedotmack" / "claude-mem" / "13.24.1"
    return {
        "executables": {"claude": binary(claude), "bun": binary(bun), "codex": binary(codex)},
        "claude_auth": auth,
        "provider_environment": {"present_names": [x for x in provider_vars if os.environ.get(x)], "values_emitted": False},
        "claude_mem_cache": {
            "version_13_24_1": cache.is_dir(),
            "worker_bundle": (cache / "scripts" / "worker-service.cjs").is_file(),
            "observer_runtime_started": False,
        },
        "upstream": {"exists": upstream.is_dir(), "sha": git_sha(upstream), "sha_matches": git_sha(upstream) == UPSTREAM_SHA},
    }
def load_corpus(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError("corpus_unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != "codex-mem-observer-comparison-corpus.v1":
        raise ComparisonError("corpus_schema")
    events, samples = value.get("events"), value.get("parser_samples")
    if not isinstance(events, list) or not events or not isinstance(samples, list):
        raise ComparisonError("corpus_shape")
    ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or event["id"] in ids:
            raise ComparisonError("corpus_event_ids")
        ids.add(event["id"])
        if not isinstance(event.get("tool_name"), str) or not isinstance(event.get("parameters"), dict):
            raise ComparisonError("corpus_event_fields")
        if "outcome" not in event or not isinstance(event.get("expectation"), dict):
            raise ComparisonError("corpus_event_expectation")
    return value
def expand(event: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(event)
    outcome = event.get("outcome")
    if isinstance(outcome, Mapping) and isinstance(outcome.get("generated_chars"), int):
        target, prefix, suffix, fill = outcome["generated_chars"], str(outcome.get("prefix", "")), str(outcome.get("suffix", "")), str(outcome.get("fill", ""))
        if not fill or target < len(prefix) + len(suffix):
            raise ComparisonError("long_output_fixture")
        size = target - len(prefix) - len(suffix)
        result["outcome"] = prefix + (fill * ((size // len(fill)) + 1))[:size] + suffix
    return result
def configure_capture(data: Path, project: Path) -> None:
    from codex_mem.config import configure
    configure(data, capture_scope="selected", included_projects=[str(project)], capture_enabled=True, capture_tools=True, processor_enabled=False, service_enabled=False, semantic_enabled=False)
def hook_payload(event: Mapping[str, Any], project: Path, *, tool_use_id: str | None = None, outcome: Any | None = None) -> dict[str, Any]:
    expanded = expand(event)
    tool_response = expanded["outcome"]
    if isinstance(tool_response, Mapping) and not any(key in tool_response for key in ("content", "data", "detail", "error", "message", "output", "resource", "result", "response", "stderr", "stdout", "structuredContent", "text")):
        tool_response = {**tool_response, "output": j(tool_response)}
    return {"hook_event_name": "PostToolUse", "cwd": str(project), "session_id": "comparison-session", "turn_id": f"turn-{expanded['id']}", "tool_name": expanded["tool_name"], "tool_use_id": tool_use_id or f"comparison-{expanded['id']}", "tool_input": expanded["parameters"], "tool_response": tool_response if outcome is None else outcome}
def bun_helper(upstream: Path) -> str:
    return f'''import {{ ModeManager }} from {json.dumps(str(upstream / "src/services/domain/ModeManager.ts"))};
import {{ buildInitPrompt, buildObservationPrompt }} from {json.dumps(str(upstream / "src/sdk/prompts.ts"))};
import {{ parseAgentXml }} from {json.dumps(str(upstream / "src/sdk/parser.ts"))};
const mode = ModeManager.getInstance().loadMode("code");
const input = await new Response(Bun.stdin.stream()).json();
const events = Array.isArray(input.events) ? input.events : [];
const prompts = events.map((event) => {{
  const observation = buildObservationPrompt({{ id: event.id, tool_name: event.tool_name, tool_input: JSON.stringify(event.parameters), tool_output: typeof event.outcome === "string" ? event.outcome : JSON.stringify(event.outcome), created_at_epoch: event.created_at_epoch, cwd: event.cwd }});
  const init = buildInitPrompt("comparison-corpus", String(event.session_id || "comparison-session"), "Observe one bounded synthetic tool event for a provider comparison.", mode, String(event.prior_context || ""));
  return {{ id: event.id, observation_prompt: observation, prompt: init + "\\n\\n" + observation }};
}});
for (const item of prompts) {{ const bytes = new TextEncoder().encode(item.prompt); item.prompt_sha256 = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(x => x.toString(16).padStart(2, "0")).join(""); item.prompt_chars = item.prompt.length; item.has_elision_marker = /<elided chars="[0-9]+/.test(item.prompt); delete item.prompt; delete item.observation_prompt; }}
const parser = (Array.isArray(input.parser_samples) ? input.parser_samples : []).map((sample) => ({{ id: sample.id, parsed: parseAgentXml(sample.raw) }}));
console.log(JSON.stringify({{ mode: mode.name, observation_types: mode.observation_types.map(x => x.id), prompts, parser }}));'''
def upstream_runtime(upstream: Path, events: Sequence[Mapping[str, Any]], samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bun = shutil.which("bun")
    if not bun:
        return {"status": "unavailable", "reason": "bun_unavailable"}
    if git_sha(upstream) != UPSTREAM_SHA:
        return {"status": "blocked", "reason": "upstream_sha_mismatch"}
    payload_events = []
    for index, source in enumerate(events):
        event = expand(source)
        payload_events.append({"id": event["id"], "tool_name": event["tool_name"], "parameters": event["parameters"], "outcome": event["outcome"], "cwd": event.get("cwd"), "prior_context": event.get("prior_context", ""), "session_id": event.get("session_id", "comparison-session"), "created_at_epoch": FIXED_EPOCH_MS + index})
    with tempfile.TemporaryDirectory(prefix="codex-mem-compare-runtime-") as temporary:
        env = dict(os.environ)
        env.update({"CLAUDE_MEM_DATA_DIR": str(Path(temporary) / "data"), "CLAUDE_MEM_MODES_DIR": str(upstream / "plugin" / "modes"), "CLAUDE_MEM_MODE": "code", "CLAUDE_MEM_LOG_LEVEL": "SILENT"})
        try:
            result = subprocess.run([bun, "-e", bun_helper(upstream)], cwd=ROOT, env=env, input=j({"events": payload_events, "parser_samples": list(samples)}), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "failed", "reason": "upstream_runtime_timeout"}
        except OSError:
            return {"status": "failed", "reason": "upstream_runtime_failed"}
    if result.returncode != 0:
        return {"status": "failed", "reason": "upstream_runtime_failed"}
    try:
        decoded = json.loads(next(line for line in reversed(result.stdout.splitlines()) if line.strip()))
    except (StopIteration, json.JSONDecodeError, TypeError):
        return {"status": "failed", "reason": "upstream_runtime_protocol"}
    if not isinstance(decoded, dict):
        return {"status": "failed", "reason": "upstream_runtime_protocol"}
    decoded["status"] = "ok"
    return decoded
def parser_checks(corpus: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    expected = {str(x["id"]): x for x in corpus["parser_samples"]}
    checks = []
    for item in results:
        sample, parsed = expected.get(str(item.get("id"))), item.get("parsed")
        if not sample or not isinstance(parsed, Mapping):
            checks.append({"id": item.get("id"), "status": "failed"})
            continue
        observations = parsed.get("observations") if isinstance(parsed.get("observations"), list) else []
        fields = {key for key, value in observations[0].items() if value not in (None, [], "")} if observations and isinstance(observations[0], Mapping) else set()
        passed = parsed.get("valid") is bool(sample.get("valid")) and len(observations) == int(sample.get("observation_count", 0))
        if sample.get("skipped"):
            passed = passed and isinstance(parsed.get("summary"), Mapping) and parsed["summary"].get("skipped") is True
        passed = passed and all(field in fields for field in sample.get("field_presence", []))
        checks.append({"id": item["id"], "status": "passed" if passed else "failed", "observed_fields": sorted(fields)})
    return checks
def prompt_checks(corpus: Mapping[str, Any], runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    prompts = {str(x["id"]): x for x in runtime.get("prompts", []) if isinstance(x, Mapping)}
    checks = []
    for event in corpus["events"]:
        item, marker = prompts.get(str(event["id"]), {}), event["expectation"].get("required_prompt_marker")
        okay = bool(item) and (marker is None or bool(item.get("has_elision_marker")))
        checks.append({"id": event["id"], "status": "passed" if okay else "failed", "prompt_chars": item.get("prompt_chars"), "prompt_sha256": item.get("prompt_sha256"), "has_elision_marker": bool(item.get("has_elision_marker"))})
    return checks
def text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).lower()
def output_checks(event: Mapping[str, Any], count: int, output: Any, fields: set[str] | None = None) -> list[dict[str, Any]]:
    expectation, value = event["expectation"], text(output)
    checks = []
    if expectation.get("minimum_observations") is not None:
        checks.append({"name": "minimum_observations", "passed": count >= int(expectation["minimum_observations"]), "observations": count})
    if expectation.get("maximum_observations") is not None:
        checks.append({"name": "maximum_observations", "passed": count <= int(expectation["maximum_observations"]), "observations": count})
    checks.extend({"name": "required_token", "token": token, "passed": str(token).lower() in value} for token in expectation.get("required_tokens", []))
    checks.extend({"name": "forbidden_token", "token": token, "passed": str(token).lower() not in value} for token in expectation.get("forbidden_completion_tokens", []))
    if fields is not None:
        for field in expectation.get("required_fields", []):
            checks.append({"name": "required_field", "field": field, "passed": ("facts" in fields or "narrative" in fields) if field == "facts_or_narrative" else field in fields})
    return checks
def codex_case(event: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    from codex_mem import hooks
    from codex_mem.processor import process_pending
    from codex_mem.store import Store
    with tempfile.TemporaryDirectory(prefix="codex-mem-compare-codex-") as temporary:
        project, data = Path(temporary) / "project", Path(temporary) / "memory"
        project.mkdir()
        configure_capture(data, project)
        with Store(data) as store:
            hooks.handle_hook(hook_payload(event, project), store)
            captures = store.get_tool_uses(project, session_id="comparison-session", limit=5)
            source = captures[0] if captures else {}
            source_rows = store.get(project, [str(source["entry_id"])]) if source.get("entry_id") else []
            source_row = source_rows[0] if source_rows else {}
        result = dict(process_pending(project, data, timeout=timeout))
        with Store(data) as store:
            recent = store.status(project).get("observation_jobs", {}).get("recent", [])
            ids = recent[0].get("output_ids", []) if recent and isinstance(recent[0], Mapping) else []
            notes = store.get(project, ids) if isinstance(ids, list) and ids else []
            source_row = store.get(project, [str(source["entry_id"])])[0] if source.get("entry_id") else {}
        clean = [{"title": clip(str(x.get("title", "")), 500), "body": clip(str(x.get("body", "")), 2_000), "tags": x.get("tags", []) if isinstance(x.get("tags"), list) else []} for x in notes if isinstance(x, Mapping)]
        return {"id": event["id"], "status": result.get("status"), "code": result.get("code"), "timeout": timeout, "capture_pipeline": "hooks.handle_hook->tool_uses->process_pending_hydration", "capture_rows": len(captures), "source_body_chars": len(str(source_row.get("body", ""))), "worker_thread_id": result.get("worker_thread_id"), "worker_turn_id": result.get("worker_turn_id"), "note_count": len(clean), "source_superseded": source_row.get("superseded_by") is not None, "checks": output_checks(event, len(clean), clean), "notes": clean, "model": result.get("model"), "reasoning_effort": result.get("reasoning_effort")}
def codex_live(corpus: Mapping[str, Any], available: Mapping[str, Any], timeout: float, limit: int) -> dict[str, Any]:
    if not available.get("executables", {}).get("codex", {}).get("available"):
        return {"status": "skipped", "reason": "codex_unavailable", "cases": []}
    cases = []
    for event in list(corpus["events"])[:limit]:
        try:
            cases.append(codex_case(event, timeout))
        except Exception:
            cases.append({"id": event["id"], "status": "failed", "reason": "codex_pipeline_failed"})
    okay = cases and all(x.get("status") in {"processed", "skipped"} and all(check.get("passed") for check in x.get("checks", [])) for x in cases)
    return {"status": "ok" if okay else "partial", "provider": "codex-mem", "model": "gpt-5.6-luna", "reasoning_effort": "medium", "scope": "provider-level", "cases": cases}
def capture_contract(corpus: Mapping[str, Any]) -> dict[str, Any]:
    from codex_mem import hooks
    from codex_mem.store import Store
    with tempfile.TemporaryDirectory(prefix="codex-mem-compare-capture-") as temporary:
        project, data = Path(temporary) / "project", Path(temporary) / "memory"
        project.mkdir()
        configure_capture(data, project)
        with Store(data) as store:
            for event in corpus["events"]:
                hooks.handle_hook(hook_payload(event, project), store)
            hooks.handle_hook(hook_payload(corpus["events"][0], project, tool_use_id="comparison-redaction", outcome={"output": "public api_key=synthetic-secret <private>hidden</private>"}), store)
            rows = store.get_tool_uses(project, session_id="comparison-session", limit=32)
            source_rows = store.get(project, [str(row["entry_id"]) for row in rows if row.get("entry_id")])
    expected_ids = {f"comparison-{event['id']}" for event in corpus["events"]}
    main_rows = [row for row in rows if row.get("tool_use_id") in expected_ids]
    source_by_id = {str(row["id"]): row for row in source_rows}
    by_tool_id = {str(row["tool_use_id"]): row for row in main_rows}
    long_selected = "comparison-long-output" in expected_ids
    long_response = str(by_tool_id.get("comparison-long-output", {}).get("tool_response", ""))
    redaction_row = next((row for row in rows if row.get("tool_use_id") == "comparison-redaction"), {})
    redacted_response = str(redaction_row.get("tool_response", ""))
    checks = [
        {"name": "all_events_captured", "passed": {str(row.get("tool_use_id")) for row in main_rows} == expected_ids},
        {"name": "source_ids_unique", "passed": len({str(row.get("entry_id")) for row in main_rows}) == len(main_rows)},
        {"name": "raw_input_and_response_present", "passed": all(isinstance(row.get("tool_input"), str) and isinstance(row.get("tool_response"), str) for row in main_rows)},
        {"name": "hook_provenance_preserved", "passed": all(str(source_by_id.get(str(row.get("entry_id")), {}).get("source", "")).startswith("hook:PostToolUse") for row in main_rows)},
        {"name": "long_output_retained_before_processing", "passed": not long_selected or len(long_response) >= 24_000},
        {"name": "long_middle_and_tail_retained", "passed": not long_selected or ("middle output that is intentionally elided" in long_response and "END-SIGNAL: the final line says retry budget exhausted." in long_response)},
        {"name": "side_index_redacts_sensitive_text", "passed": bool(redaction_row) and "synthetic-secret" not in redacted_response and "hidden" not in redacted_response},
    ]
    return {"status": "passed" if all(x["passed"] for x in checks) else "failed", "checks": checks, "captured": len(main_rows), "raw_side_index": True}
def recovery_contract() -> dict[str, Any]:
    from codex_mem.processor import MODEL, REASONING_EFFORT, process_pending
    from codex_mem.store import Store
    def response(output: Mapping[str, Any]) -> dict[str, Any]:
        return {"output": dict(output), "evidence": {"thread_start": {"thread_id": "comparison-thread", "model": MODEL, "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"}, "turn_started": {"thread_id": "comparison-thread", "turn_id": "comparison-turn"}, "turn_completed": True, "no_tools": True, "rerouted": False}}
    with tempfile.TemporaryDirectory(prefix="codex-mem-compare-recovery-") as temporary:
        project, data = Path(temporary) / "project", Path(temporary) / "memory"
        project.mkdir()
        with Store(data) as store:
            source = store.remember(project, "Recovery source", "A raw observation must survive an invalid response.", source="hook:Stop", session_id="recovery")
        first = process_pending(project, data, runner=lambda _: response({"notes": [{"title": "missing attribution", "body": "invalid", "tags": []}], "disposition": "processed"}))
        idle = process_pending(project, data, runner=lambda _: response({"notes": [], "disposition": "skipped"}))
        def retry(request: Mapping[str, Any]) -> dict[str, Any]:
            ids = [item["id"] for item in request["sources"]]
            return response({"notes": [{"title": "Recovered raw evidence", "body": "The invalid response did not delete the raw source.", "tags": ["recovery"], "source_ids": ids}], "disposition": "processed"})
        second = process_pending(project, data, retry_failed=True, runner=retry)
        with Store(data) as store:
            row = store.get(project, [str(source["id"])])[0]
        checks = [{"name": "invalid_response_fails", "passed": first.get("status") == "failed"}, {"name": "failed_job_requires_explicit_retry", "passed": idle.get("status") == "idle"}, {"name": "retry_processes_source", "passed": second.get("status") == "processed"}, {"name": "raw_source_survives_until_retry", "passed": row.get("superseded_by") is not None}]
    return {"status": "passed" if all(x["passed"] for x in checks) else "failed", "checks": checks}
def receipt(args: argparse.Namespace) -> dict[str, Any]:
    corpus_path, upstream = Path(args.corpus).expanduser().resolve(), Path(args.upstream).expanduser().resolve()
    corpus = load_corpus(corpus_path)
    selected = corpus["events"]
    if args.case:
        wanted = set(args.case)
        selected = [x for x in selected if x["id"] in wanted]
        if len(selected) != len(wanted):
            raise ComparisonError("unknown_case")
    selected_corpus = dict(corpus)
    selected_corpus["events"] = selected
    available = availability(upstream)
    runtime = upstream_runtime(upstream, selected, corpus["parser_samples"])
    if runtime.get("status") != "ok":
        raise ComparisonError(str(runtime.get("reason", "upstream_runtime_failed")))
    offline = {"capture": capture_contract(selected_corpus), "recovery": recovery_contract()}
    codex = codex_live(selected_corpus, available, args.timeout, args.max_live_cases) if args.codex_live else {"status": "not_requested"}
    if codex.get("status") == "partial":
        scope = "codex-provider-partial"
    elif codex.get("status") == "ok":
        scope = "codex-provider-only"
    else:
        scope = "contract-only"
    return {
        "schema": "codex-mem-observer-comparison-receipt.v1", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "scope": scope,
        "claims": {"whole_pipeline_comparison": False, "claude_provider_ab": False, "synthetic_corpus": True, "upstream_prompt_parser_runtime": True},
        "provenance": {"upstream_sha": git_sha(upstream), "expected_upstream_sha": UPSTREAM_SHA, "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest()},
        "availability": available, "corpus": {"path": str(corpus_path), "event_count": len(selected), "event_ids": [x["id"] for x in selected]},
        "contract": {"upstream_mode": runtime.get("mode"), "observation_types": runtime.get("observation_types", []), "parser_checks": parser_checks(corpus, runtime.get("parser", [])), "prompt_checks": prompt_checks(selected_corpus, runtime)},
        "offline": offline, "codex": codex, "live_statuses": [str(codex.get("status"))],
    }
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument("--upstream", default=os.environ.get("CLAUDE_MEM_UPSTREAM", str(UPSTREAM)))
    parser.add_argument("--case", action="append")
    parser.add_argument("--codex-live", action="store_true")
    parser.add_argument("--max-live-cases", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_live_cases <= 8 or not 10 <= args.timeout <= 600:
            raise ComparisonError("live_bounds")
        print(json.dumps(receipt(args), ensure_ascii=False, indent=2))
    except ComparisonError as exc:
        print(json.dumps({"schema": "codex-mem-observer-comparison-receipt.v1", "status": "blocked", "reason": str(exc)}, indent=2), file=sys.stderr)
        return 2
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
