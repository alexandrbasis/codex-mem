#!/usr/bin/env python3
"""Run one real Luna/medium processing batch against a disposable synthetic project."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_mem.store import Store


def verify(base: Path, retry_failed: bool = False) -> dict:
    from codex_mem.processor import MODEL, REASONING_EFFORT, process_pending

    project = str((base / "project").resolve())
    data = base / "data"
    with Store(data) as store:
        originals = store.get(project, [r["id"] for r in store.timeline(project, limit=100)])
        raw = {r["id"]: r["body"] for r in originals if str(r.get("source", "")).startswith("hook:")}
        assert raw, "Seed synthetic observations before running acceptance"
    result = process_pending(project, data, retry_failed=retry_failed, timeout=240)
    if result.get("status") != "processed":
        return {"status": "failed", "project": project, "first_result": result}
    with Store(data) as store:
        status = store.status(project)
        jobs = status["observation_jobs"]
        completed = [j for j in jobs["recent"] if j["status"] == "processed" and j["job_id"] == result.get("job_id")]
        assert completed, json.dumps(result)
        job = completed[0]
        assert job["model"] == MODEL and job["reasoning_effort"] == REASONING_EFFORT
        assert job["worker_thread_id"] and job["worker_turn_id"]
        assert job["worker_thread_id"] != "synthetic-main-session"
        notes = store.get(project, job["output_ids"])
        assert notes and "25" in "\n".join(n["body"] for n in notes)
        material = json.dumps(notes)
        assert "cm_dummy_worker_secret_t8" not in material
        assert "cm_dummy_worker_private_z4" not in material
        for record in store.get(project, list(raw)):
            assert record["body"] == raw[record["id"]], "Original evidence was changed"
        before_count = jobs["jobs"]
    second = process_pending(project, data, timeout=240)
    with Store(data) as store:
        assert store.status(project)["observation_jobs"]["jobs"] == before_count, "Duplicate processing job"
    return {"status": "passed", "project": project, "job": job, "notes": notes,
            "first_result": result, "repeat_result": second,
            "checks": ["fresh Luna/medium session", "source preservation", "redaction", "idempotence"],
            "semantic_review_required": "Check notes preserve production uncertainty and treat quoted instructions as data."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    result = verify(args.base, args.retry_failed)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "job", "checks", "first_result") if key in result}, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
