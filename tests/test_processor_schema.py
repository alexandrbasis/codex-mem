"""The native schema and local Stop rules must allow the same outcome branches."""

import copy
import unittest
from unittest import mock

try:
    import jsonschema
except ImportError:
    jsonschema = None

from codex_mem import processor


def note(source="s1"):
    return {"title": "Bounded retries", "body": "The retry test passed locally.", "tags": [],
            "source_ids": [source], "observation": {"type": "discovery", "subtitle": "",
            "narrative": "A local execution established the retry boundary.", "facts": [],
            "concepts": [], "files_read": [], "files_modified": []}}


def summary(source="s1"):
    return {"title": "Retry result", "request": None, "investigated": "Retry handling.",
            "learned": "The boundary is enforced.", "completed": "The local test passed.",
            "next_steps": "", "notes": "", "source_ids": [source]}


def request(source="hook:Stop", required=False):
    return processor._runner_request({"job_id": "a" * 32, "lease_token": "b" * 32,
        "summary_required": required, "sources": [{"id": "c" * 32, "title": "Result",
        "body": "The local test passed.", "source": source}]}, 60)


@unittest.skipIf(jsonschema is None, "jsonschema is optional; local validation tests still run")
class ProcessorSchemaTests(unittest.TestCase):
    def valid(self, result, source="hook:Stop", required=False):
        schema = request(source, required)["output_schema"]
        # Test the declared wire shape, so failure below isolates the missing
        # Stop dependency rather than merely an envelope format change.
        wire = {"result": result} if "result" in schema["properties"] else result
        return jsonschema.Draft202012Validator(schema).is_valid(wire)

    def test_optional_stop_schema_rejects_notes_without_required_summary(self):
        self.assertFalse(self.valid({"notes": [note()], "disposition": "processed", "session_summary": None}))

    def test_stop_processed_and_routine_skip_branches(self):
        for source in ("hook:Stop", "hook:Stop:native"):
            with self.subTest(source=source):
                self.assertTrue(self.valid({"notes": [note()], "disposition": "processed", "session_summary": summary()}, source))
                self.assertTrue(self.valid({"notes": [], "disposition": "processed", "session_summary": summary()}, source))
                self.assertTrue(self.valid({"notes": [], "disposition": "skipped", "session_summary": None}, source))
                self.assertFalse(self.valid({"notes": [note()], "disposition": "skipped", "session_summary": None}, source))
                self.assertFalse(self.valid({"notes": [], "disposition": "processed", "session_summary": None}, source))

    def test_required_summary_disallows_skip(self):
        self.assertTrue(self.valid({"notes": [], "disposition": "processed", "session_summary": summary()}, required=True))
        self.assertFalse(self.valid({"notes": [], "disposition": "skipped", "session_summary": None}, required=True))

    def test_non_stop_schema_allows_notes_or_skip_without_summary(self):
        self.assertTrue(self.valid({"notes": [note()], "disposition": "processed", "session_summary": None}, "hook:PostToolUse"))
        self.assertTrue(self.valid({"notes": [], "disposition": "skipped", "session_summary": None}, "hook:PostToolUse"))
        self.assertFalse(self.valid({"notes": [], "disposition": "processed", "session_summary": None}, "hook:PostToolUse"))
        self.assertFalse(self.valid({"notes": [note()], "disposition": "processed", "session_summary": summary()}, "hook:PostToolUse"))


class NativeEnvelopeTests(unittest.TestCase):
    def test_schema_uses_object_root_nested_union_and_required_object_properties(self):
        schema = request()["output_schema"]
        self.assertEqual("object", schema["type"])
        self.assertEqual(["result"], schema["required"])
        self.assertNotIn("anyOf", schema)
        self.assertEqual(2, len(schema["properties"]["result"]["anyOf"]))
        def check(spec):
            if isinstance(spec, list):
                for value in spec:
                    check(value)
            elif isinstance(spec, dict):
                self.assertFalse({"if", "then", "else", "allOf", "oneOf"}.intersection(spec))
                if spec.get("type") == "object":
                    self.assertIs(False, spec["additionalProperties"])
                    self.assertEqual(set(spec["properties"]), set(spec["required"]))
                for value in spec.values():
                    check(value)
        check(schema)

    def test_unwrap_resolves_ids_before_authoritative_validation(self):
        output = {"result": {"notes": [note()], "disposition": "processed", "session_summary": summary()}}
        original = copy.deepcopy(output)
        normalized = processor._unwrap_model_output(output)
        sources = [{"id": "c" * 32, "source": "hook:Stop"}]
        resolved = processor._resolve_source_handles(normalized, sources)
        notes, disposition, saved_summary = processor._validate_model_output(resolved, sources)
        self.assertEqual("processed", disposition)
        self.assertEqual(["c" * 32], notes[0]["source_ids"])
        self.assertEqual(["c" * 32], saved_summary["source_ids"])
        self.assertEqual(original, output)

    def test_malformed_or_ambiguous_envelopes_fail_closed(self):
        flat = {"notes": [], "disposition": "skipped", "session_summary": None}
        cases = (flat, {"result": None}, {"result": flat, "notes": []},
                 {"result": {"result": flat}}, {"result": {"notes": [], "disposition": "skipped"}},
                 {"result": {**flat, "extra": "private-secret"}},
                 {"result": {**flat, "notes": [{"title": "Incomplete note"}]}})
        for value in cases:
            with self.subTest(value=value), self.assertRaises(processor.ProcessorFailure) as failed:
                processor._unwrap_model_output(value)
            self.assertEqual("invalid_response", failed.exception.code)
            self.assertNotIn("private-secret", str(failed.exception))

    def test_schema_envelope_cannot_bypass_semantic_validation(self):
        sources = [{"id": "c" * 32, "source": "hook:Stop"}]
        for result, reason in (
            ({"notes": [note()], "disposition": "processed", "session_summary": None}, "missing_required_summary"),
            ({"notes": [note("unknown")], "disposition": "processed", "session_summary": summary()}, "unknown_source_handle"),
            ({"notes": [note()], "disposition": "skipped", "session_summary": summary()}, "skipped_with_content"),
        ):
            with self.subTest(reason=reason), self.assertRaises(processor.ProcessorFailure) as failed:
                normalized = processor._unwrap_model_output({"result": result})
                resolved = processor._resolve_source_handles(normalized, sources)
                processor._validate_model_output(resolved, sources)
            self.assertEqual(reason, failed.exception.reason_code)

    def test_native_runner_unwraps_before_returning_public_receipt(self):
        native_request = request()
        flat = {"notes": [], "disposition": "skipped", "session_summary": None}
        client = mock.Mock()
        responses = {
            "initialize": {}, "config/read": {"config": {}},
            "model/list": {"data": [{"id": processor.MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]},
            "thread/start": {"thread": {"id": "worker"}, "model": processor.MODEL,
                             "reasoningEffort": "medium", "modelProvider": "openai"},
            "mcpServerStatus/list": {"data": []}, "turn/start": {"turn": {"id": "turn"}},
        }
        client.request.side_effect = lambda method, *args, **kwargs: responses[method]
        for output in ({"result": flat}, flat):
            with self.subTest(enveloped="result" in output), \
                    mock.patch("codex_mem.processor._AppServer", return_value=client), \
                    mock.patch("codex_mem.processor.shutil.which", return_value="codex"), \
                    mock.patch("codex_mem.processor._wait_for_turn_completion"), \
                    mock.patch("codex_mem.processor._read_valid_final_output", return_value=output):
                if "result" in output:
                    receipt = processor.NativeProcessorRunner().run(native_request)
                    self.assertEqual(flat, receipt["output"])
                    sent_schema = next(call.args[1]["outputSchema"] for call in client.request.call_args_list
                                       if call.args[0] == "turn/start")
                    self.assertEqual(native_request["output_schema"], sent_schema)
                else:
                    with self.assertRaises(processor.ProcessorFailure) as failed:
                        processor.NativeProcessorRunner().run(native_request)
                    self.assertEqual("invalid_output_shape", failed.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
