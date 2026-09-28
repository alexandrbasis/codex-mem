"""Lifecycle retrieval must deliver memory without running full health scans."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.store import Store


class HookContextBudgetTests(unittest.TestCase):
    def test_lifecycle_delivers_memory_without_database_health_or_vector_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            configure(root / "memory", capture_scope="all", service_enabled=False,
                      processor_enabled=False)
            with Store(root / "memory") as store:
                store.remember(project, "Aurora release", "Aurora requires staged deployment.")
                for event in ("SessionStart", "UserPromptSubmit"):
                    with self.subTest(event=event), patch(
                        "codex_mem.freshness._freshness_snapshot",
                        side_effect=AssertionError("full health scan in lifecycle hook"),
                    ) as health, patch.object(
                        store, "embedding_status",
                        side_effect=AssertionError("vector scan in lifecycle hook"),
                    ) as vectors:
                        response = handle_hook(dict(
                            hook_event_name=event, cwd=str(project),
                            session_id=event, turn_id="turn", source="startup",
                            prompt="Aurora",
                        ), store)
                        context = response["hookSpecificOutput"]["additionalContext"]
                        self.assertIn("Aurora requires staged deployment", context)
                        self.assertIn("freshness unknown", context)
                        health.assert_not_called()
                        vectors.assert_not_called()

    def test_explicit_context_still_checks_current_health(self):
        with tempfile.TemporaryDirectory() as directory, Store(directory) as store:
            configure(directory, capture_scope="all", semantic_enabled=False)
            with patch.object(store, "embedding_status", wraps=store.embedding_status) as scan:
                context = store.context(Path(directory) / "project")
                scan.assert_called_once()
                self.assertIn("Memory empty", context)

    def test_native_budget_reserves_startup_time_without_extending_worker(self):
        from codex_mem.hook_runner import HOOK_WORKER_TIMEOUT_SECONDS
        manifest = json.loads((Path(__file__).resolve().parents[1] / "hooks/hooks.json").read_text())
        for groups in manifest["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if not hook.get("async"):
                        self.assertGreaterEqual(hook["timeout"] - HOOK_WORKER_TIMEOUT_SECONDS, 3)
        self.assertEqual(2.0, HOOK_WORKER_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
