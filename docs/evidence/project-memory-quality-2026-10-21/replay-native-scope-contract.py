"""Replay five disclosed development outputs through temporary Store instances.

The explicit migration removes old basename worktree fields, adds absent empty
consolidation fields, and remaps evidence refs by exact source-span text. This tests the v11 contract, not model quality.
Original receipts and grades remain untouched. No model or service calls occur.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from codex_mem import episode_processor as processor
from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


def replay(request, response, *, migrated):
    disclosed = json.loads(request["prompt"].split("\n", 1)[1])
    assert not disclosed["current_state"]["claims"]
    assert not disclosed["current_state"]["episodes"]
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / Path(disclosed["project"]).name
        project.mkdir()
        data = Path(temporary) / "memory"
        configure(data, capture_scope="all", project_memory_mode="shadow",
                  project_memory_projects=[str(project)], jev_quality_enabled=False)
        with Store(data) as store:
            for event in disclosed["events"]:
                assert not event["truncated"] and event["body_offset"] == 0
                body = "".join(span["text"] for span in event["spans"])
                assert len(body) == event["body_length"]
                store.remember(project, event["title"], body, source=event["source"],
                               session_id=event["session_id"])

        def run(new_request):
            new_disclosed = json.loads(new_request["prompt"].split("\n", 1)[1])
            assert len(disclosed["events"]) == len(new_disclosed["events"])
            ref_map = {}
            for old, new in zip(disclosed["events"], new_disclosed["events"]):
                assert [span["text"] for span in old["spans"]] == [span["text"] for span in new["spans"]]
                ref_map.update({a["ref"]: b["ref"] for a, b in zip(old["spans"], new["spans"])})
            output = deepcopy(response["output"])
            for kind in ("episodes", "claims", "relations"):
                for record in output[kind]:
                    groups = [record["evidence"]] if kind == "relations" else record["evidence"].values()
                    for group in groups:
                        group[:] = [ref_map[ref] for ref in group]
            for claim in output["claims"]:
                # Only these disclosed old basename defects are migrated.
                # Foreign or arbitrary paths must never be silently repaired.
                assert claim["scope"]["worktree"] == Path(disclosed["project"]).name
                if migrated:
                    del claim["scope"]["worktree"]
            if migrated:
                for episode in output["episodes"]:
                    episode.setdefault("consolidation_of", [])
                    episode["evidence"].setdefault("consolidation_of", [])
                import jsonschema
                jsonschema.validate(output, new_request["output_schema"])
            returned = deepcopy(response)
            returned["output"] = output
            return returned

        result = processor.process_project_memory(project, data, runner=run)
        with Store(data) as store:
            state = ProjectMemory(store).snapshot(project)
        expected = "processed" if migrated else "failed"
        assert result["status"] == expected, result
        if not migrated:
            assert result["code"] == "invalid_response", result
            assert not state["claims"] and not state["episodes"]
        else:
            assert len(state["claims"]) == len(response["output"]["claims"])
            assert all(claim["scope"]["worktree"] == str(project.resolve()) for claim in state["claims"])
        return {"status": result["status"], "code": result.get("code"),
                "stored_claims": len(state["claims"]), "stored_episodes": len(state["episodes"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=Path("/tmp/codex-mem-consolidation-retention-experiment"))
    args = parser.parse_args()
    attempts = []
    # Fixed development-only allowlist; no holdout discovery or model calls.
    for index in range(1, 6):
        request_path = args.run / f"receipts/call-{index:04d}-request.json"
        response_path = args.run / f"receipts/call-{index:04d}-response.json"
        request, response = json.loads(request_path.read_text()), json.loads(response_path.read_text())
        attempts.append({"attempt_alias": f"development-attempt-{index}",
                         "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
                         "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(),
                         "unchanged_old_output": replay(request, response, migrated=False),
                         "explicit_v11_wire_migration": replay(request, response, migrated=True)})
    print(json.dumps({"schema_version": processor.OUTPUT_SCHEMA_VERSION,
                      "scope": "five disclosed development outputs only",
                      "migration": "omit old basename worktree; add empty absent consolidation fields; remap refs by exact span text",
                      "limits": ["No fresh model outputs", "No answer-quality regrading", "No service change",
                                 "No continuity or consolidation changes", "Original artifacts remain unchanged"],
                      "attempts": attempts}, indent=2))


if __name__ == "__main__":
    main()
