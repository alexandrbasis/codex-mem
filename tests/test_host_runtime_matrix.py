"""One public host/queue/context contract for all four runtime combinations.

Only external model executables are deterministic. Each worker runs in a new
process with a real temporary Store. Observations drain the service queue;
project memory drains its durable queue through the public explicit processor.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.project_memory import ProjectMemory
from codex_mem.service import enqueue, service_status
from codex_mem.store import Store

ROOT = Path(__file__).resolve().parents[1]
# Test infrastructure allowance for real synthetic executable startup and reviews.
REAL_FIXTURE_ATTEMPT_BUDGET = 30
READINESS_WATCHDOG = REAL_FIXTURE_ATTEMPT_BUDGET + 10
OUTER_WATCHDOG = 3 * REAL_FIXTURE_ATTEMPT_BUDGET
PAIRS = (("codex", "codex"), ("codex", "pi"), ("pi", "codex"), ("pi", "pi"))


class HostRuntimeMatrixTests(unittest.TestCase):
    def prepare(self, host, runtime, *, project_memory=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.data = self.root / "memory"
        self.host, self.runtime = host, runtime
        self.project_memory = project_memory
        self.executable = self.root / runtime
        fixture = (ROOT / 'tests/fixtures' / ('execution_' + runtime + '.py')).read_text()
        if runtime == 'codex':
            gate_deadline = 'deadline = time.monotonic() + 10'
            self.assertEqual(1, fixture.count(gate_deadline))
            fixture = fixture.replace(gate_deadline,
                'deadline = time.monotonic() + ' + str(REAL_FIXTURE_ATTEMPT_BUDGET), 1)
        self.executable.write_text('#!' + sys.executable + '\n' + fixture)
        self.executable.chmod(0o700)
        self.profile = dict(runtime=runtime, provider="openai" if runtime == "codex" else "fixture",
            model="gpt-6-luna" if runtime == "codex" else "fixture",
            reasoning_effort="medium" if runtime == "codex" else "low", timeout=REAL_FIXTURE_ATTEMPT_BUDGET)
        # An empty native home and executable-only PATH prevent native history,
        # real providers, or a Codex installation from entering the Pi/Pi case.
        self.environment = dict(PATH=str(self.root), HOME=str(self.root / "home"),
            CODEX_HOME=str(self.root / "codex-home"), PI_CODING_AGENT_DIR=str(self.root / "pi-home"),
            CODEX_MEM_HOME=str(self.data), PYTHONPATH=str(ROOT))
        configure(self.data, capture_scope="all", service_enabled=False, usage_enabled=False,
            semantic_enabled=False, processor_workers=1, processing_profile=self.profile,
            processor_enabled=True, project_memory_mode="pilot" if project_memory else "off",
            project_memory_projects=[str(self.project)] if project_memory else [])
        self.scenario()

    def scenario(self, mode="valid", **changes):
        note = dict(title="Local import decision", body="The user requests local import.",
            tags=["import"], source_ids=["s1"], observation=dict(type="decision", subtitle="",
            narrative="The user requests local import.", facts=[], concepts=[], files_read=[], files_modified=[]))
        output = {"result": {"notes": [note], "disposition": "processed", "session_summary": None}}
        values = dict(mode="project_memory" if self.project_memory and mode == "valid" else mode)
        if not self.project_memory:
            values.update(observation=True, output=output)
        if mode == "schema":
            values["output"] = {"unexpected": True}
        values.update(changes)
        self.executable.with_suffix(".json").write_text(json.dumps(values))

    def pi(self, **values):
        payload = dict(version=1, pi_version="1.4.0", project=str(self.project),
            session_id="session", settled=True)
        payload.update(values)
        result = subprocess.run([sys.executable, "-m", "codex_mem.pi_host"],
            input=json.dumps(payload), text=True, capture_output=True, cwd=ROOT,
            env={**os.environ, **self.environment}, timeout=OUTER_WATCHDOG)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def capture(self, text="Use local import.", identity="user-1", host=None, role="user"):
        host = host or self.host
        if host == "pi":
            event = dict(message_id=identity, role=role, text=text)
            if role == "assistant":
                event["stop_reason"] = "stop"
            elif role == "toolResult":
                event.update(tool_id=identity, tool_name="bash")
            return self.pi(operation="capture", events=[event])
        event = dict(cwd=str(self.project), session_id="session", turn_id=identity, message_id=identity)
        if role == "user":
            event.update(hook_event_name="UserPromptSubmit", prompt=text)
        elif role == "assistant":
            event.update(hook_event_name="Stop", last_assistant_message=text)
        else:
            event.update(hook_event_name="PostToolUse", tool_name="Bash", tool_use_id=identity,
                tool_input={"command": "pytest tests/test_import.py"}, tool_response={"output": text, "exit_code": 0})
        with patch.dict(os.environ, self.environment), Store(self.data) as store:
            return handle_hook(event, store)

    def context(self, host=None, project=None):
        host = host or self.host
        project = project or self.project
        if host == "pi":
            return self.pi(operation="context", phase="start", query="local import",
                session_id="reader", project=str(project))["text"]
        with patch.dict(os.environ, self.environment), Store(self.data) as store:
            return json.dumps(handle_hook(dict(hook_event_name="SessionStart", source="startup",
                cwd=str(project), session_id="reader"), store))

    def drain(self, retry=False):
        configure(self.data, service_enabled=True)
        try:
            self.assertEqual("queued", enqueue(self.project, self.data, retry_failed=retry)["status"])
            self.assertEqual(1, service_status(self.data)["queued_projects"])
            code = "from codex_mem.service import run_service; import json; print(json.dumps(run_service(max_cycles=1, poll_interval=0, processor_timeout=" + str(REAL_FIXTURE_ATTEMPT_BUDGET) + ")))"
            if self.project_memory:
                code = ("from codex_mem.episode_processor import process_project_memory; import json,sys; "
                        "print(json.dumps(process_project_memory(sys.argv[1])))")
            result = subprocess.run([sys.executable, "-c", code, str(self.project)], cwd=ROOT, text=True,
                capture_output=True, env={**os.environ, **self.environment}, timeout=OUTER_WATCHDOG)
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)
        finally:
            configure(self.data, service_enabled=False)

    def diagnostics(self):
        with Store(self.data) as store:
            if self.project_memory:
                memory = ProjectMemory(store)
                return dict(attempts=memory.attempts(self.project), coverage=memory.snapshot(self.project)['coverage'])
            jobs = store.status(self.project)['observation_jobs']['recent']
            return dict(service=service_status(self.data), jobs=[dict(
                job=store.observation_job_status(self.project, row['job_id']),
                attempts=store.observation_attempts(self.project, row['job_id'])) for row in jobs])

    def committed_note(self, source_id, receipt):
        diagnostics = self.diagnostics()
        self.assertEqual(1, len(diagnostics['jobs']), dict(receipt=receipt, diagnostics=diagnostics))
        job = diagnostics['jobs'][0]['job']
        self.assertEqual('processed', job['status'], dict(receipt=receipt, diagnostics=diagnostics))
        self.assertEqual(1, len(job['output_ids']), diagnostics)
        with Store(self.data) as store:
            note = store.get(self.project, job['output_ids'])[0]
            raw = store.get(self.project, [source_id])[0]
            self.assertEqual([source_id], note['source_ids'], diagnostics)
            self.assertEqual(str(self.project), note['project'])
            self.assertEqual(job['session_id'], note['session_id'])
            self.assertIsNone(note['superseded_by'])
            self.assertEqual(note['id'], raw['superseded_by'])
            local = store.context(self.project, query='local import', exclude_session='reader', allow_remote=False)
        self.assertIn('local import', local.lower(), dict(receipt=receipt, diagnostics=diagnostics, local_context=local))
        return diagnostics

    def test_observation_queue_delivers_next_request_for_every_pair(self):
        for host, runtime in PAIRS:
            with self.subTest(host=host, runtime=runtime):
                self.prepare(host, runtime)
                configure(self.data, processing_profile={**self.profile, "model": "pending-only"})
                self.capture()
                self.capture()
                with Store(self.data) as store:
                    sources = store.timeline(self.project)
                    self.assertEqual(1, len(sources))
                    source_id = sources[0]["id"]
                    self.assertEqual("user_intent", store.get(self.project, [source_id])[0]["provenance"]["record_role"])
                configure(self.data, processing_profile=self.profile)
                drained = self.drain()
                self.assertEqual(1, drained['jobs'], drained)
                diagnostics = self.committed_note(source_id, drained)
                context = self.context()
                self.assertIn('local import', context.lower(), dict(context=context, diagnostics=diagnostics))
                with Store(self.data) as store:
                    notes = store.search(self.project, "local import")
                    generated = [row for row in notes if row["id"] != source_id]
                    self.assertEqual(1, len(generated), notes)
                    self.assertEqual([source_id], store.get(self.project, [generated[0]["id"]])[0]["source_ids"])
                    self.assertEqual([], store.timeline(self.root / "other"))
                self.assertNotIn("The user requests local import.", self.context(project=self.root / "other"))
                # Context retrieval and a restarted worker create no new capture.
                trace = self.executable.with_suffix(".trace")
                before = trace.read_text().count('"turn/start"' if runtime == "codex" else '"type": "prompt"')
                self.drain()
                self.assertEqual(before, trace.read_text().count('"turn/start"' if runtime == "codex" else '"type": "prompt"'))

    def test_project_memory_queue_preserves_role_reference_and_selected_runtime(self):
        for host, runtime in PAIRS:
            with self.subTest(host=host, runtime=runtime):
                self.prepare(host, runtime, project_memory=True)
                self.capture()
                self.capture("The import test passed.", "tool-1", role="toolResult")
                self.capture("The assistant reports success.", "assistant-1", role="assistant")
                with Store(self.data) as store:
                    sources = store.timeline(self.project)
                    source_ids = {row["id"] for row in sources}
                    roles = {row["provenance"]["record_role"] for row in store.get(self.project, list(source_ids))}
                    self.assertEqual({"user_intent", "tool_record", "assistant_report"}, roles)
                processed = self.drain()
                self.assertEqual('processed', processed['status'], dict(receipt=processed, diagnostics=self.diagnostics()))
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    state = memory.snapshot(self.project)
                    self.assertEqual(1, len(state["episodes"]), state)
                    evidence = state["episodes"][0]["evidence"]["summary"]
                    self.assertTrue(evidence)
                    self.assertTrue(all(row["source_id"] in source_ids for row in evidence))
                    attempts = memory.attempts(self.project)
                    self.assertEqual(1, len(attempts), attempts)
                    self.assertEqual(runtime, attempts[0]["runtime"])
                    self.assertEqual(self.profile["model"], attempts[0]["actual_model"])
                    self.assertEqual("processed", attempts[0]["outcome"])
                    self.assertEqual(12, attempts[0]["total_tokens"])
                self.assertIn("Use local import.", self.context())
                self.assertIn("Use local import.", self.context(host="pi" if host == "codex" else "codex"))
                self.drain()
                with Store(self.data) as store:
                    self.assertEqual(1, len(ProjectMemory(store).attempts(self.project)))
                    self.assertEqual(3, ProjectMemory(store).snapshot(self.project)["coverage"]["captured_events"])


    def test_privacy_and_injected_context_never_become_processing_sources(self):
        for host, runtime in PAIRS:
            with self.subTest(host=host, runtime=runtime):
                self.prepare(host, runtime, project_memory=True)
                self.capture("<private>HIDDEN_REQUEST</private>", "private")
                self.capture("HIDDEN_TOOL", "private-tool", role="toolResult")
                self.capture("HIDDEN_REPORT", "private-report", role="assistant")
                self.capture("<codex_mem_untrusted_context>INJECTED</codex_mem_untrusted_context>", "injection")
                self.capture("HIDDEN_CONTINUATION", "hidden-report", role="assistant")
                self.capture()
                processed = self.drain()
                self.assertEqual('processed', processed['status'], dict(receipt=processed, diagnostics=self.diagnostics()))
                with Store(self.data) as store:
                    sources = store.timeline(self.project)
                    self.assertEqual(1, len(sources), sources)
                    state = ProjectMemory(store).snapshot(self.project)
                    self.assertEqual(1, state["coverage"]["captured_events"])
                    self.assertNotIn("HIDDEN", json.dumps(state))
                trace = self.executable.with_suffix(".trace").read_text()
                self.assertNotIn("HIDDEN", trace)
                self.assertNotIn("INJECTED", trace)
                self.assertIn("Use local import.", self.context())

    def test_failed_project_memory_is_atomic_and_retry_survives_restart(self):
        for host, runtime in PAIRS:
            for mode in ("invalid_json", "schema", "provider_error", "cancelled", "disconnect", "timeout"):
                with self.subTest(host=host, runtime=runtime, mode=mode):
                    self.prepare(host, runtime, project_memory=True)
                    self.capture()
                    self.scenario(mode)
                    failed = self.drain()
                    self.assertEqual("failed", failed["status"], failed)
                    with Store(self.data) as store:
                        memory = ProjectMemory(store)
                        state = memory.snapshot(self.project)
                        self.assertEqual([], state["episodes"])
                        self.assertEqual([], state["claims"])
                        self.assertEqual(1, state["coverage"]["pending_events"])
                        attempt = memory.attempts(self.project)[0]
                        self.assertEqual("failed", attempt["outcome"])
                        self.assertEqual(12, attempt['total_tokens'], dict(failed_receipt=failed, attempt=attempt))
                        if runtime == 'pi':
                            evidence = json.loads(attempt['execution_receipt'])
                            self.assertEqual(1, evidence['generation'], dict(receipt=failed, attempt=attempt))
                            self.assertGreater(attempt['usage_updates'], 0, dict(receipt=failed, attempt=attempt))
                            if mode == 'cancelled':
                                self.assertEqual('pi_cancelled', failed['reason_code'], dict(receipt=failed, attempt=attempt))
                        job_id = attempt["job_id"]
                        memory.retry_failed(self.project, include_quarantined=True)
                    # The source remains readable and both adapters stay available.
                    self.context()
                    self.scenario()
                    recovery = self.drain()
                    self.assertEqual('processed', recovery['status'], dict(receipt=recovery, diagnostics=self.diagnostics()))
                    with Store(self.data) as store:
                        memory = ProjectMemory(store)
                        attempts = memory.attempts(self.project)
                        self.assertEqual({job_id}, {row["job_id"] for row in attempts})
                        self.assertEqual(["failed", "processed"], sorted(row["outcome"] for row in attempts))
                        self.assertEqual(1, len(memory.snapshot(self.project)["episodes"]))
                    self.assertIn("Use local import.", self.context())

    def test_source_deletion_during_generation_prevents_every_pair_from_committing(self):
        for host, runtime in PAIRS:
            with self.subTest(host=host, runtime=runtime):
                self.prepare(host, runtime, project_memory=True)
                self.capture()
                with Store(self.data) as store:
                    source_id = store.timeline(self.project)[0]["id"]
                gate = self.root / "release"
                self.scenario(gate=str(gate))
                ready = False
                phase = None
                with ThreadPoolExecutor(max_workers=1) as pool:
                    worker = pool.submit(self.drain)
                    try:
                        deadline = time.monotonic() + READINESS_WATCHDOG
                        while time.monotonic() < deadline and not worker.done():
                            if gate.with_suffix('.ready').exists():
                                phase = self.diagnostics()
                                with Store(self.data) as store:
                                    ready = bool(store.get(self.project, [source_id]))
                                    if ready:
                                        store.forget(self.project, ids=[source_id])
                                break
                            time.sleep(.01)
                    finally:
                        gate.touch()
                        result = worker.result(timeout=OUTER_WATCHDOG)
                        final = self.diagnostics()
                self.assertTrue(ready, dict(phase=phase, receipt=result, diagnostics=final))
                self.assertEqual("deferred", result["status"], result)
                self.assertEqual("source_changed", result["code"])
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    self.assertEqual([], memory.snapshot(self.project)["episodes"])
                    self.assertEqual([], store.timeline(self.project))
                    self.assertEqual(12, memory.attempts(self.project)[0]["total_tokens"])
                self.assertNotIn("Use local import.", self.context())

    def test_mixed_hosts_share_worker_limit_and_active_profile_after_restart(self):
        for runtime in ("codex", "pi"):
            with self.subTest(runtime=runtime):
                self.prepare("codex", runtime)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    captures = [pool.submit(self.capture, host=host) for host in ("codex", "pi")]
                    for capture in captures:
                        capture.result(timeout=OUTER_WATCHDOG)
                with Store(self.data) as store:
                    sources = store.timeline(self.project)
                    self.assertEqual(2, len(sources))
                    self.assertEqual(2, len({row["session_id"] for row in sources}))
                gate = self.root / "release"
                self.scenario(gate=str(gate))
                configure(self.data, processor_workers=2)
                trace = self.executable.with_suffix(".trace")
                marker = '"turn/start"' if runtime == "codex" else '"type": "prompt"'
                ready = False
                phase = None
                with ThreadPoolExecutor(max_workers=1) as pool:
                    worker = pool.submit(self.drain)
                    try:
                        deadline = time.monotonic() + READINESS_WATCHDOG
                        while time.monotonic() < deadline and not worker.done():
                            phase = self.diagnostics()
                            jobs = phase['jobs']
                            calls = [json.loads(row['attempts'][-1]['execution_calls'])
                                     if row['attempts'][-1]['execution_calls'] is not None else {}
                                     for row in jobs if row['attempts']]
                            prompts = trace.read_text().count(marker) if trace.exists() else 0
                            active = phase['service'].get('workers', {}).get('active')
                            if (prompts == 2 and len(jobs) == len(calls) == 2 and active == 2
                                    and all(row['job']['status'] == 'running' for row in jobs)
                                    and len({row['job']['session_id'] for row in jobs}) == 2
                                    and all(row.get('generation', {}).get('outcome') == 'running' for row in calls)):
                                ready = True
                                # Both distinct leased sessions reached generation before release.
                                configure(self.data, processing_profile={**self.profile, 'model': 'unavailable-next'})
                                break
                            time.sleep(.01)
                    finally:
                        gate.touch()
                        result = worker.result(timeout=OUTER_WATCHDOG)
                        final = self.diagnostics()
                self.assertTrue(ready, dict(phase=phase, receipt=result, diagnostics=final))
                self.assertEqual(2, result['jobs'], dict(receipt=result, diagnostics=final))
                self.assertTrue(all(row['job']['status'] == 'processed' and len(row['job']['output_ids']) == 1
                                    for row in final['jobs']), dict(receipt=result, diagnostics=final))
                from codex_mem.usage_api import usage_report
                report = usage_report(self.data, project=self.project, from_date="2020-01-01", to_date="2030-01-01",
                    group_by=["runtime", "model"])
                rows = report["groups"]["observer"]["model"]["rows"]
                self.assertEqual([self.profile["model"]], [row["value"] for row in rows])
                self.assertEqual({"processed": 2}, rows[0]["outcome_counts"])
                with Store(self.data) as store:
                    jobs = store.status(self.project)['observation_jobs']['recent']
                    self.assertEqual(2, len(jobs))
                    totals = []
                    for job in jobs:
                        attempt = store.observation_attempts(self.project, job['job_id'])[0]
                        calls = json.loads(attempt['execution_calls'])
                        self.assertEqual({'generation', 'summary_support_1'}, set(calls))
                        # Each external fixture call reports twelve synthetic tokens.
                        self.assertEqual([12, 12], [calls[key]['metrics']['total_tokens']
                            for key in ('generation', 'summary_support_1')])
                        total = sum(call['metrics']['total_tokens'] for call in calls.values())
                        self.assertEqual(total, attempt['total_tokens'])
                        totals.append(total)
                self.assertEqual(sum(totals), rows[0]['token_totals']['total_tokens']['total'])
                self.assertIn("local import", self.context(host="codex").lower())
                self.assertIn("local import", self.context(host="pi").lower())
                self.capture(identity="next-user")
                self.scenario()
                self.drain()
                report = usage_report(self.data, project=self.project, from_date="2020-01-01", to_date="2030-01-01",
                    group_by=["model"])
                rows = {row["value"]: row for row in report["groups"]["observer"]["model"]["rows"]}
                self.assertEqual({"failed": 1}, rows["unavailable-next"]["outcome_counts"])
                self.assertEqual({"processed": 2}, rows[self.profile["model"]]["outcome_counts"])

if __name__ == "__main__":
    unittest.main()
