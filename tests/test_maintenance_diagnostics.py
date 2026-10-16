"""Content-free diagnostics for malformed processor summaries."""

import unittest

from codex_mem.processor import ProcessorFailure, _validated_summary


class MaintenanceDiagnosticsTests(unittest.TestCase):
    def summary(self):
        return {"title": "Outcome", "request": None, "investigated": "",
                "learned": "", "completed": "", "next_steps": "", "notes": "",
                "source_ids": ["stop"]}

    def test_empty_summary_has_specific_content_free_reason(self):
        with self.assertRaises(ProcessorFailure) as caught:
            _validated_summary(self.summary(), [{"id": "stop", "source": "hook:Stop"}])
        self.assertEqual("empty_session_summary", caught.exception.reason_code)

    def test_control_character_has_text_reason_without_recording_payload(self):
        value = self.summary()
        value["learned"] = "private-payload\x00"
        with self.assertRaises(ProcessorFailure) as caught:
            _validated_summary(value, [{"id": "stop", "source": "hook:Stop"}])
        self.assertEqual("invalid_summary_text", caught.exception.reason_code)
        self.assertNotIn("private-payload", str(caught.exception))

    def test_unknown_request_with_substantive_summary_remains_valid(self):
        value = self.summary()
        value["learned"] = "The source reports a local check."
        result = _validated_summary(value, [{"id": "stop", "source": "hook:Stop"}])
        self.assertIsNone(result["request"])
        self.assertEqual(value["learned"], result["learned"])
