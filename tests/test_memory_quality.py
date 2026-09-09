"""Continuation context and source provenance, without a native model."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from codex_mem.store import Store


class MemoryQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_continuation_preserves_summary_and_decision_ahead_of_recent_trivia(self):
        summary = self.store.remember(
            self.project, "Roadmap handoff", "Version 0.3 has autosave; publication is unverified.",
            kind="session_summary",
        )
        decision = self.store.remember(
            self.project, "Roadmap persistence decision", "Use autosave to preserve unfinished edits.",
            observation={"type": "decision"},
        )
        for index in range(60):
            self.store.remember(self.project, f"Roadmap CSS {index}", "Theme spacing is 12px.")
        for query in ("", "roadmap"):
            context = self.store.context(self.project, query=query, budget=1400)
            entries = ET.fromstring(context).findall("entry")
            self.assertEqual([summary["id"], decision["id"]],
                             [entry.attrib["id"] for entry in entries[:2]])
            self.assertLessEqual(len(context), 1400)
        # This affects reading order only; old notes are not silently deleted.
        self.assertEqual(62, self.store.status(self.project)["active_entries"])

    def test_generated_summary_is_not_repeated_and_custom_body_is_retained(self):
        fields = {
            "request": "Continue roadmap work", "learned": "Autosave replaced manual Apply in v0.3",
            "completed": "Local tests reportedly passed", "next_steps": "Check marketplace installation",
        }
        body = "\n".join(f'{key.replace("_", " ").capitalize()}: {value}'
                         for key, value in fields.items())
        entry = self.store.remember(self.project, "Roadmap", body,
                                    kind="session_summary", session_summary=fields)
        context = self.store.context(self.project, budget=1200)
        self.assertEqual(1, context.count(fields["next_steps"]))
        self.assertEqual(1, context.count(fields["learned"]))
        self.assertIn(entry["id"], context)
        custom = self.store.remember(self.project, "Custom handoff", "Additional evidence: receipt-42",
                                     kind="session_summary", session_summary=fields)
        context = self.store.context(self.project, budget=5000)
        self.assertIn("Additional evidence: receipt-42", context)
        self.assertIn(custom["id"], context)

    def test_provenance_distinguishes_assistant_claim_from_tool_record(self):
        report = self.store.remember(self.project, "Final answer", "205 tests passed",
                                     source="hook:Stop", kind="session")
        note = self.store.remember(self.project, "Reported test result", "Assistant reported 205 passes",
                                   source="processor:test", source_ids=[report["id"]])
        tool = self.store.remember(self.project, "Read README", "README says 205 tests passed",
                                   source="hook:PostToolUse:read", kind="tool")
        second = self.store.remember(self.project, "Documented test result", "README reports passes",
                                     source="processor:test", source_ids=[tool["id"]])
        found = self.store.get(self.project, [note["id"], second["id"]])
        self.assertEqual(["assistant_report"], found[0]["provenance"]["linked_source_roles"])
        self.assertEqual(["tool_record"], found[1]["provenance"]["linked_source_roles"])
        for record in found:
            self.assertEqual("not_assessed", record["provenance"]["verification"])
        context = self.store.context(self.project, budget=2000)
        self.assertIn('linked_source_roles="assistant_report"', context)
        self.assertIn('linked_source_roles="tool_record"', context)
        foreign = self.project / "other"
        self.assertEqual([], self.store.get(foreign, [note["id"]]))

    def test_oversized_summary_does_not_starve_fitting_decision(self):
        self.store.remember(self.project, "Large summary", "A custom summary.",
                            kind="session_summary", session_summary={"next_steps": "x" * 2000})
        decision = self.store.remember(self.project, "Decision", "Keep unfinished edits.",
                                       kind="decision")
        context = self.store.context(self.project, budget=1024)
        self.assertIn(decision["id"], context)
        self.assertLessEqual(len(context), 1024)
        ET.fromstring(context)

    def test_summary_dedup_preserves_independent_observation_evidence(self):
        self.store.remember(
            self.project, "Handoff", "Next steps: Deploy.",
            kind="session_summary", session_summary={"next_steps": "Deploy."},
            observation={"type": "decision", "facts": ["No test execution evidence is available."]},
        )
        context = self.store.context(self.project, budget=2000)
        self.assertEqual(1, context.count("Deploy."))
        self.assertIn("No test execution evidence is available.", context)

    def test_repeated_stop_summaries_do_not_hide_newer_cross_session_decision(self):
        summaries = [self.store.remember(
            self.project, f"Roadmap handoff {index}", f"Old chat revision {index}",
            session_id="old-chat", kind="session_summary",
        ) for index in range(50)]
        decision = self.store.remember(
            self.project, "Roadmap deployment decision", "Keep publication blocked until live verification.",
            session_id="new-chat", observation={"type": "decision"},
        )
        for query in ("", "roadmap"):
            context = self.store.context(self.project, query=query, budget=6000)
            ids = [entry.attrib["id"] for entry in ET.fromstring(context).findall("entry")]
            self.assertEqual([summaries[-1]["id"], decision["id"]], ids)
        # Reading a compact handoff does not remove historical Stop records.
        self.assertEqual(50, len(self.store.timeline(self.project, session_id="old-chat", limit=100)))
        self.assertIsNone(self.store.get(self.project, [summaries[0]["id"]])[0]["superseded_by"])

    def test_context_balances_recent_sessions_with_decisions_and_observations(self):
        summaries = [self.store.remember(
            self.project, f"Chat {index}", "Progress " + ("detail " * 1000),
            session_id=f"chat-{index}", kind="session_summary",
            session_summary={"next_steps": f"Finish pending task {index}"},
        ) for index in range(4)]
        decision = self.store.remember(
            self.project, "Release decision", "Do not publish without receipt.",
            session_id="decision-chat", observation={"type": "decision"},
        )
        discovery = self.store.remember(
            self.project, "Retry finding", "Retry preserves the immutable request ID.",
            session_id="finding-chat", observation={"type": "discovery"},
        )
        context = self.store.context(self.project, budget=6000)
        ids = [entry.attrib["id"] for entry in ET.fromstring(context).findall("entry")]
        self.assertEqual([summaries[-1]["id"], discovery["id"], summaries[-2]["id"], decision["id"]], ids[:4])
        self.assertLessEqual(len(context), 6000)

    def test_fifty_old_decisions_do_not_hide_a_new_discovery_or_open_failure(self):
        for index in range(50):
            self.store.remember(
                self.project, f"Old roadmap decision {index}", "Historical decision.",
                session_id="old-chat", observation={"type": "decision"},
            )
        recent = self.store.remember(
            self.project, "Roadmap verification failure", "Live installation failed; resolve before release.",
            session_id="new-chat", observation={"type": "discovery"},
        )
        # Ordinary unstructured trivia still does not displace the observation.
        for index in range(50):
            self.store.remember(self.project, f"Roadmap CSS {index}", "Theme spacing is 12px.")
        for query in ("", "roadmap"):
            context = self.store.context(self.project, query=query, budget=5300)
            entries = ET.fromstring(context).findall("entry")
            self.assertEqual(recent["id"], entries[0].attrib["id"])
            self.assertIn("resolve before release", context)

    def test_delayed_earlier_stop_summary_does_not_replace_later_event_handoff(self):
        early = self.store.remember(
            self.project, "Earlier Stop", "Deployment is still pending.",
            source="hook:Stop", session_id="same-chat",
        )
        late = self.store.remember(
            self.project, "Later Stop", "Deployment verified.",
            source="hook:Stop", session_id="same-chat",
        )
        newer = self.store.remember(
            self.project, "Roadmap later result", "Deployment verified; monitor the rollout.",
            source="processor:test", session_id="same-chat", kind="session_summary",
            source_ids=[late["id"]], session_summary={"next_steps": "Monitor rollout."},
        )
        retried = self.store.remember(
            self.project, "Roadmap retried old result", "Earlier pending deployment.",
            source="processor:test", session_id="same-chat", kind="session_summary",
            source_ids=[early["id"]], session_summary={"next_steps": "Deploy old version."},
        )
        for query in ("", "roadmap"):
            context = self.store.context(self.project, query=query, budget=5300)
            ids = [entry.attrib["id"] for entry in ET.fromstring(context).findall("entry")]
            self.assertEqual([newer["id"]], ids)
            self.assertNotIn("Deploy old version", context)
        self.assertNotIn(retried["id"], self.store.context(self.project, query="retried", budget=5300))
        historical = self.store.get(self.project, [newer["id"], retried["id"]])
        self.assertTrue(all(record["superseded_by"] is None for record in historical))

    def test_summary_position_uses_same_session_stop_over_later_unrelated_sources(self):
        early = self.store.remember(self.project, "Early Stop", "Still pending.",
                                    session_id="local-chat", source="hook:Stop")
        late = self.store.remember(self.project, "Late Stop", "Work completed.",
                                   session_id="local-chat", source="hook:Stop")
        later_tool = self.store.remember(self.project, "Later tool", "Unrelated next-turn command.",
                                         session_id="local-chat", source="hook:PostToolUse:read")
        other_chat = self.store.remember(self.project, "Other chat Stop", "Other work.",
                                         session_id="other-chat", source="hook:Stop")
        current = self.store.remember(
            self.project, "Latest local handoff", "Completed local work.",
            session_id="local-chat", kind="session_summary", source="processor:test",
            source_ids=[late["id"]],
        )
        delayed = self.store.remember(
            self.project, "Delayed local handoff", "Pending older work.",
            session_id="local-chat", kind="session_summary", source="processor:test",
            source_ids=[early["id"], later_tool["id"], other_chat["id"]],
        )
        context = self.store.context(self.project, budget=5300)
        self.assertIn(current["id"], context)
        self.assertNotIn(delayed["id"], context)

    def test_long_structured_handoff_keeps_unfinished_work_and_tail_caveat(self):
        fields = {
            "request": "Release roadmap", "investigated": "Inspected " + "file details " * 1400,
            "learned": "Autosave worked locally. " + "implementation details " * 500 + " Production remains unverified.",
            "completed": "Local fix only.", "next_steps": "Verify live install before publication.",
            "notes": "The assistant report is not an execution receipt.",
        }
        body = "\n".join(f'{key.replace("_", " ").capitalize()}: {value}'
                         for key, value in fields.items())
        record = self.store.remember(self.project, "Long handoff", body,
                                     kind="session_summary", session_id="previous-chat", session_summary=fields)
        context = self.store.context(self.project, budget=5300, exclude_session="new-chat")
        self.assertIn(record["id"], context)
        self.assertIn(fields["next_steps"], context)
        self.assertIn(fields["notes"], context)
        self.assertIn("Production remains unverified.", context)
        self.assertIn("[truncated]", context)
        self.assertEqual(1, context.count(fields["next_steps"]))
        self.assertLessEqual(len(context), 5300)
        ET.fromstring(context)

    def test_context_dedup_is_project_and_excluded_session_scoped(self):
        local = self.store.remember(self.project, "Local summary", "Keep local task open.",
                                    session_id="shared-name", kind="session_summary")
        foreign = self.store.remember(self.project / "other", "Foreign summary", "Private foreign history.",
                                      session_id="shared-name", kind="session_summary")
        excluded = self.store.remember(self.project, "Current summary", "Do not inject current chat.",
                                       session_id="current-chat", kind="session_summary")
        for query in ("", "summary"):
            context = self.store.context(self.project, query=query, budget=6000, exclude_session="current-chat")
            self.assertIn(local["id"], context)
            self.assertNotIn(foreign["id"], context)
            self.assertNotIn(excluded["id"], context)

    def test_context_filters_do_not_revive_an_older_matching_session_summary(self):
        old = self.store.remember(
            self.project, "Roadmap obsolete release", "Old selection.", session_id="one-chat",
            kind="session_summary", observation={"type": "decision", "concepts": ["release"]},
        )
        new = self.store.remember(
            self.project, "Current unrelated handoff", "New selection.", session_id="one-chat",
            kind="session_summary", observation={"type": "discovery"},
        )
        matching = self.store.remember(
            self.project, "Roadmap decision", "The release needs verification.",
            observation={"type": "decision", "concepts": ["release"], "files_read": ["release.py"]},
        )
        context = self.store.context(self.project, query="roadmap", types=["decision"],
                                     concepts=["release"], files=["release.py"], budget=2000)
        self.assertIn(matching["id"], context)
        self.assertNotIn(old["id"], context)
        self.assertNotIn(new["id"], context)
        self.assertNotIn(old["id"], self.store.context(self.project, query="obsolete", budget=2000))
        # Exact retrieval still exposes the older matching evidence.
        self.assertEqual(old["id"], self.store.get(self.project, [old["id"]])[0]["id"])

    def test_compacted_mixed_metadata_keeps_independent_evidence_and_custom_body(self):
        record = self.store.remember(
            self.project, "Mixed handoff <unsafe>", "Independent receipt: command exited 1.",
            kind="session_summary", source="processor:test<&\"",
            session_summary={"investigated": "details " * 2000, "next_steps": "Fix failing deployment."},
            observation={"type": "decision", "facts": ["No successful deployment was observed."],
                         "narrative": "details " * 2000 + " This is not production proof."},
        )
        context = self.store.context(self.project, budget=5300)
        self.assertIn(record["id"], context)
        self.assertIn("Independent receipt: command exited 1.", context)
        self.assertIn("No successful deployment was observed.", context)
        self.assertIn("This is not production proof.", context)
        root = ET.fromstring(context)
        self.assertEqual("processor:test<&\"", root.find("entry").attrib["source"])
        self.assertIsNotNone(root.find("entry/metadata/observation"))
        self.assertIsNotNone(root.find("entry/metadata/session_summary"))

    def test_small_context_budgets_are_valid_xml_with_whole_ids_and_explicit_clipping(self):
        record = self.store.remember(
            self.project, "Long entry", "<&>" * 10000,
            session_summary={"next_steps": "Pending action " * 1000},
        )
        rendered = False
        for budget in (128, 180, 256, 380, 500, 700, 1024):
            context = self.store.context(self.project, budget=budget)
            root = ET.fromstring(context)
            self.assertLessEqual(len(context), budget)
            for entry in root.findall("entry"):
                rendered = True
                self.assertEqual(record["id"], entry.attrib["id"])
                self.assertIn("[truncated]", context)
        self.assertTrue(rendered)

    def test_inventory_counts_history_separately_and_preserves_version_conflicts(self):
        raw = self.store.remember(self.project, "Raw", "Historical report", source="hook:Stop")
        old = self.store.remember(self.project, "Roadmap v0.2", "Apply is manual in v0.2")
        latest = self.store.remember(self.project, "Roadmap v0.3", "Autosave was reported for v0.3",
                                     kind="session_summary")
        self.store.remember(self.project, "History", "A command log", kind="tool")
        self.store.remember(self.project / "other", "Foreign", "Must not count")
        inventory = self.store.status(self.project)["inventory"]
        self.assertEqual(1, inventory["raw_capture"]["total"])
        self.assertEqual(1, inventory["durable_note"]["active"])
        self.assertEqual(1, inventory["session_summary"]["active"])
        self.assertEqual(1, inventory["other_history"]["active"])
        records = self.store.get(self.project, [old["id"], latest["id"]])
        self.assertTrue(all(record["superseded_by"] is None for record in records))
        context = self.store.context(self.project, query="roadmap", budget=2000)
        self.assertIn(old["id"], context)
        self.assertIn(latest["id"], context)
        self.assertNotIn(raw["id"], context)


if __name__ == "__main__":
    unittest.main()
