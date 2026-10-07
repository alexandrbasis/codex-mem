from __future__ import annotations

from pathlib import Path
import json
import re
import tempfile
import unittest
from unittest import mock

from codex_mem.config import (
    HOOK_STATE_FILENAME, configure, context_was_injected, mark_context_injected,
    clear_private_prompt_gate, mark_private_prompt_gate,
)
from codex_mem.freshness import freshness_snapshot
from codex_mem.hooks import _context_marker, handle_hook
from codex_mem.store import Store


class HookContextDedupTests(unittest.TestCase):
    def test_full_legacy_state_keeps_low_sorting_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            state_file = data_dir / HOOK_STATE_FILENAME
            state_file.write_text(json.dumps({"version": 1,
                "context_injections": {f"project:ffff:session:{i:03}": {"sources": ["old"]}
                                       for i in range(256)},
                "private_prompt_gates": {"private-session": {"turn_id": "private-turn"}},
            }, sort_keys=True))
            active = "project:0000:session:active"
            mark_context_injected(active, source="context:memory", data_dir=data_dir)
            # Reload through the actual sorted serializer, then add another
            # session as a concurrent chat or subagent lifecycle hook would.
            mark_context_injected("project:ffff:session:new", source="new", data_dir=data_dir)
            self.assertTrue(context_was_injected(active, source="context:memory", data_dir=data_dir))
            state = json.loads(state_file.read_text())
            self.assertEqual(256, len(state["context_injections"]))
            self.assertEqual({"private-session": {"turn_id": "private-turn"}},
                             state["private_prompt_gates"])
            mark_private_prompt_gate(active, turn_id="private", data_dir=data_dir)
            clear_private_prompt_gate(active, data_dir=data_dir)
            self.assertTrue(context_was_injected(active, source="context:memory", data_dir=data_dir))

    def test_reused_session_refreshes_durable_delivery_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            state_file = data_dir / HOOK_STATE_FILENAME
            active = "project:0000:session:active"
            injections = {active: {"sources": ["same"], "delivery_order": 1}}
            injections.update({f"project:ffff:session:{i:03}": {
                "sources": ["old"], "delivery_order": i + 2} for i in range(255)})
            state_file.write_text(json.dumps({"context_injections": injections}, sort_keys=True))
            mark_context_injected(active, source="same", data_dir=data_dir)
            mark_context_injected("project:ffff:session:new", source="new", data_dir=data_dir)
            state = json.loads(state_file.read_text())["context_injections"]
            self.assertIn(active, state)
            self.assertEqual(["same"], state[active]["sources"])
            self.assertNotIn("project:ffff:session:000", state)
            self.assertEqual(256, len(state))

    def test_capture_telemetry_does_not_repeat_delivered_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "memory"
            project = Path(temporary) / "project"
            configure(data_dir, capture_scope="selected", included_projects=[project])
            payload = {"cwd": str(project), "session_id": "active", "turn_id": "one"}
            # Exercise real capture counters, including the freshness renderer
            # used before hooks deferred full checks. No model or worker runs.
            with Store(data_dir) as store, mock.patch(
                "codex_mem.freshness.hook_freshness_snapshot", freshness_snapshot
            ):
                record = store.remember(project, "Accepted retry decision",
                    "Use a bounded retry for failed checkout.", kind="decision",
                    session_id="other")
                startup = handle_hook({**payload, "hook_event_name": "SessionStart",
                    "source": "startup"}, store)
                before = store.context(project, exclude_session="active", hook_mode=True)
                repeated = handle_hook({**payload, "hook_event_name": "UserPromptSubmit",
                    "prompt": "retry", "turn_id": "two"}, store)
                after = store.context(project, exclude_session="active", hook_mode=True)
                self.assertNotEqual(before, after)
                self.assertEqual(re.findall(r"<entry .*?</entry>", before, re.S),
                                 re.findall(r"<entry .*?</entry>", after, re.S))
                self.assertIn("<freshness>", startup["hookSpecificOutput"]["additionalContext"])
                self.assertIn(record["id"], startup["hookSpecificOutput"]["additionalContext"])
                self.assertEqual({"continue": True}, repeated)

                store.remember(project, "New retry limit", "Stop after three attempts.",
                               kind="decision", session_id="third")
                changed = handle_hook({**payload, "hook_event_name": "UserPromptSubmit",
                    "prompt": "retry", "turn_id": "three"}, store)
                self.assertIn("Stop after three attempts.",
                              changed["hookSpecificOutput"]["additionalContext"])

                other_session = handle_hook({**payload, "hook_event_name": "UserPromptSubmit",
                    "session_id": "different", "prompt": "retry"}, store)
                self.assertIn("hookSpecificOutput", other_session)
                for source in ("resume", "compact", "compact"):
                    restored = handle_hook({**payload, "hook_event_name": "SessionStart",
                        "source": source}, store)
                    self.assertIn("hookSpecificOutput", restored)

    def test_delivery_identity_keeps_record_text_and_provenance(self) -> None:
        prefix = '<codex-mem-context untrusted="true">\n'
        record = '<entry id="one" source="verified">body</entry>\n'
        original = prefix + '<freshness>pending=1</freshness>\n' + record + '</codex-mem-context>'
        telemetry_change = original.replace("pending=1", "pending=2")
        self.assertEqual(_context_marker(original), _context_marker(telemetry_change))
        self.assertNotEqual(_context_marker(original),
                            _context_marker(original.replace("body", "new body")))
        self.assertNotEqual(_context_marker(original),
                            _context_marker(original.replace('source="verified"', 'source="reported"')))
        # A freshness-like tag elsewhere is memory content, not hook telemetry.
        embedded = prefix + record.replace("body", "<freshness>fact</freshness>") + '</codex-mem-context>'
        self.assertNotEqual(_context_marker(embedded),
                            _context_marker(embedded.replace("fact", "changed")))


if __name__ == "__main__":
    unittest.main()
