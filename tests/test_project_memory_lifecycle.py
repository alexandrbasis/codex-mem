"""Opt-in scope and durable project-memory delivery, without a live model."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure, load_config, project_memory_enabled
from codex_mem.hooks import _remember_safely
from codex_mem.service import _run_project_memory_cycle, enqueue, run_service
from codex_mem.store import Store


class ProjectMemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "memory"
        self.project = str(self.root / "project")
        self.other = str(self.root / "other")
        self.settings = configure(self.home, capture_scope="all", project_memory_mode="shadow",
                                  project_memory_projects=[self.project, self.other])

    def test_default_off_empty_scope_and_shadow_retrieval(self):
        self.assertEqual("off", load_config(self.root / "fresh")["project_memory_mode"])
        self.assertTrue(project_memory_enabled(self.project, self.settings))
        self.assertFalse(project_memory_enabled(self.project, self.settings, pilot_only=True))
        self.assertTrue(project_memory_enabled(self.project, {**self.settings, "project_memory_mode": "pilot"}, pilot_only=True))
        self.assertFalse(project_memory_enabled(self.project, {**self.settings, "project_memory_projects": []}))
        self.assertFalse(project_memory_enabled(self.project + "/worktree", self.settings))

    def test_privacy_gates_and_invalid_fields_fail_closed(self):
        for update in ({"capture_scope": "manual"}, {"capture_enabled": False},
                       {"excluded_projects": [self.project]}, {"project_memory_mode": "off"},
                       {"project_memory_projects": ["relative"]}, {"excluded_projects": "broken"}):
            with self.subTest(update=update):
                self.assertFalse(project_memory_enabled(self.project, {**self.settings, **update}))
        with mock.patch.dict("os.environ", {"CODEX_MEM_DISABLED": "1"}):
            self.assertFalse(project_memory_enabled(self.project, self.settings))
        for update in ({"project_memory_mode": True}, {"project_memory_projects": ["relative"]}):
            with self.assertRaises(ValueError):
                configure(self.home, **update)
        (self.home / "config.json").write_text(json.dumps({"project_memory_mode": "invalid"}))
        self.assertFalse(load_config(self.home).valid)
        self.assertEqual("off", load_config(self.home)["project_memory_mode"])

    def test_prompt_wakes_only_scoped_enabled_memory_without_model(self):
        store = mock.Mock(data_dir=self.home)
        with mock.patch("codex_mem.hooks._wake_capture_queue") as wake:
            _remember_safely(store, self.project, title="prompt", body="user correction", kind="session",
                             payload={"session_id": "s"}, source="hook:UserPromptSubmit", tags=[], dedupe_prefix="prompt")
            store.remember.assert_called_once()
            wake.assert_called_once_with(self.project, self.home, coalesce_seconds=0)
        configure(self.home, project_memory_mode="off")
        with mock.patch("codex_mem.hooks._wake_capture_queue") as wake:
            _remember_safely(store, self.project, title="prompt", body="user correction", kind="session",
                             payload={}, source="hook:UserPromptSubmit", tags=[], dedupe_prefix="prompt")
            wake.assert_not_called()

    def test_capture_failure_never_wakes(self):
        store = mock.Mock(data_dir=self.home)
        store.remember.side_effect = OSError("unavailable")
        with mock.patch("codex_mem.hooks._wake_capture_queue") as wake:
            _remember_safely(store, self.project, title="prompt", body="correction", kind="session",
                             payload={}, source="hook:UserPromptSubmit", tags=[], dedupe_prefix="prompt")
            wake.assert_not_called()

    def test_durable_discovery_without_queue_notification_and_rotation(self):
        with mock.patch("codex_mem.project_memory.ProjectMemory") as memory:
            memory.return_value.pending_projects.return_value = [self.project, self.other]
            process = mock.Mock(return_value={"status": "processed"})
            previous = _run_project_memory_cycle(self.home, load_config, 100, {}, processor=process)
            _run_project_memory_cycle(self.home, load_config, 101, {}, previous=previous, processor=process)
            self.assertEqual([self.project, self.other], [c.args[0] for c in process.call_args_list])
            memory.return_value.pending_projects.assert_called_with(
                limit=256, projects=[self.project, self.other], ready_only=True)
            self.assertTrue(all(call.kwargs['background'] for call in process.call_args_list))

    def test_failure_has_cooldown_and_config_change_cancels(self):
        retry = {}
        with mock.patch("codex_mem.project_memory.ProjectMemory") as memory:
            memory.return_value.pending_projects.return_value = [self.project]
            process = mock.Mock(side_effect=RuntimeError("runner unavailable"))
            _run_project_memory_cycle(self.home, load_config, 100, retry, processor=process)
            _run_project_memory_cycle(self.home, load_config, 101, retry, processor=process)
            self.assertEqual(1, process.call_count)
            _run_project_memory_cycle(self.home, load_config, 106, retry, processor=process)
            self.assertEqual(2, process.call_count)
            disabled = {**self.settings, "project_memory_mode": "off"}
            _run_project_memory_cycle(self.home, mock.Mock(side_effect=[self.settings, disabled]), 200, retry, processor=process)
            self.assertEqual(2, process.call_count)

    def test_daemon_discovers_idle_pending_and_keeps_observation_work(self):
        enqueue(self.project, self.home)
        process = mock.Mock(return_value={"status": "processed"})
        normal = mock.Mock(return_value={"status": "idle"})
        with mock.patch("codex_mem.project_memory.ProjectMemory") as memory:
            memory.return_value.pending_projects.return_value = [self.project]
            receipt = run_service(self.home, processor=normal, project_memory_processor=process,
                                  indexer=lambda *a, **k: None, max_cycles=2, poll_interval=0)
        self.assertEqual("cycle_limit", receipt["status"])
        self.assertEqual(2, process.call_count)
        normal.assert_called()

    def test_committed_prompt_recovers_without_stop_or_notification(self):
        with Store(self.home) as store:
            store.remember(self.project, title="User prompt", body="Keep deployment open until verified.",
                           kind="session", source="hook:UserPromptSubmit", session_id="prompt-only")
            captured = datetime.fromisoformat(store._connection.execute(
                'SELECT created_at FROM entries LIMIT 1').fetchone()[0].replace('Z', '+00:00')).timestamp()
        process = mock.Mock(return_value={"status": "processed"})
        with mock.patch('codex_mem.project_memory.time.time', return_value=captured + 31):
            run_service(self.home, processor=lambda *a, **k: {"status": "idle"},
                        project_memory_processor=process, max_cycles=1, poll_interval=0)
        process.assert_called_once_with(self.project, data_dir=self.home, background=True)

    def test_disabled_worker_does_not_discover_or_dispatch(self):
        configure(self.home, project_memory_mode="off")
        process = mock.Mock()
        with mock.patch("codex_mem.project_memory.ProjectMemory") as memory:
            _run_project_memory_cycle(self.home, load_config, 100, {}, processor=process)
            memory.assert_not_called()
        process.assert_not_called()

    def test_rapid_prompt_burst_waits_for_quiet_but_signals_are_readable(self):
        from codex_mem.project_memory_retrieval import build_project_brief
        epoch = 1767225600.0
        process = mock.Mock(return_value={"status": "processed"})
        retry = {}
        with Store(self.home) as store:
            for offset in range(5):
                record = store.remember(self.project, 'Prompt', f'Keep requirement {offset} open.',
                                        kind='session', source='hook:UserPromptSubmit', session_id='burst')
                stamp = datetime.fromtimestamp(epoch + offset).astimezone().isoformat()
                store._connection.execute('UPDATE entries SET created_at=? WHERE id=?', (stamp, record['id']))
                with mock.patch('codex_mem.project_memory.time.time', return_value=epoch + offset):
                    _run_project_memory_cycle(self.home, load_config, epoch + offset, retry, processor=process)
                process.assert_not_called()
            brief = build_project_brief(store, self.project, include_shadow=True)
            self.assertTrue(brief['pending'])
            self.assertEqual(5, len(brief['signals']))
        # A new Store in each cycle proves eligibility survives process restart.
        with mock.patch('codex_mem.project_memory.time.time', return_value=epoch + 35):
            _run_project_memory_cycle(self.home, load_config, epoch + 35, retry, processor=process)
        process.assert_called_once_with(self.project, data_dir=self.home, background=True)

    def test_stop_checkpoint_obeys_coalescing_and_explicit_claim_stays_immediate(self):
        from codex_mem.project_memory import ProjectMemory
        epoch = 1767225600.0
        with Store(self.home) as store:
            for source, offset in (('hook:UserPromptSubmit', 0), ('hook:Stop', 1)):
                record = store.remember(self.project, 'Event', 'Deployment is open.', kind='session',
                                        source=source, session_id='checkpoint')
                stamp = datetime.fromtimestamp(epoch + offset).astimezone().isoformat()
                store._connection.execute('UPDATE entries SET created_at=? WHERE id=?', (stamp, record['id']))
        process = mock.Mock(return_value={'status': 'processed'})
        with mock.patch('codex_mem.project_memory.time.time', return_value=epoch + 5):
            _run_project_memory_cycle(self.home, load_config, epoch + 5, {}, processor=process)
        process.assert_not_called()
        with mock.patch('codex_mem.project_memory.time.time', return_value=epoch + 7):
            _run_project_memory_cycle(self.home, load_config, epoch + 7, {}, processor=process)
        process.assert_called_once_with(self.project, data_dir=self.home, background=True)
        with Store(self.home) as store, mock.patch('codex_mem.project_memory.time.time', return_value=epoch + 1):
            self.assertIsNotNone(ProjectMemory(store).claim(self.project))


if __name__ == "__main__":
    unittest.main()
