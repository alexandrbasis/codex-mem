#!/usr/bin/env python3
"""Test a real detached local queue worker using only new fictional notes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem.config import configure
from codex_mem.semantic import DIMENSIONS, MODEL, MODEL_REVISION
from codex_mem.service import enqueue, service_status, start_service, stop_service
from codex_mem.store import Store


def wait_until(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for local service acceptance condition")


def run():
    receipt = {"status": "failed", "data": "new fictional-only temporary notes", "model_turns": 0}
    with tempfile.TemporaryDirectory(prefix="codex-mem-service-acceptance-") as temporary:
        base = Path(temporary)
        projects = [base / "alpha", base / "beta"]
        for project in projects:
            project.mkdir()
        data = base / "memory"
        configure(data, included_projects=projects, capture_scope="selected")
        try:
            with Store(data) as store:
                for i, project in enumerate(projects):
                    store.remember(project, f"Fixture {i}", "This fictional queue test preserves tasks after restart.")
            for project in projects:
                assert enqueue(project, data)["status"] == "queued"
            assert service_status(data)["queued_projects"] == 2
            with ThreadPoolExecutor(max_workers=4) as pool:
                starts = list(pool.map(lambda _: start_service(data), range(4)))
            receipt["concurrent_starts"] = starts
            assert all(start["status"] in {"started", "starting", "already_starting", "running"}
                       for start in starts), starts
            reported_pids = {start["pid"] for start in starts if isinstance(start.get("pid"), int)}
            assert len(reported_pids) <= 1, starts
            live = wait_until(lambda: (s if (s := service_status(data))["running"] else None))
            receipt["first_pid"] = live["pid"]
            assert not reported_pids or reported_pids == {live["pid"]}, starts
            wait_until(lambda: service_status(data)["queued_projects"] == 0)
            with Store(data) as store:
                assert all(
                    store.embedding_status(p, model=MODEL, revision=MODEL_REVISION, dimensions=DIMENSIONS)["indexed"] == 1
                    for p in projects
                )
                assert all(store.status(p)["observation_jobs"]["jobs"] == 0 for p in projects)
            first_stop = stop_service(data)
            assert first_stop["status"] == "stopping", first_stop
            wait_until(lambda: service_status(data)["status"] == "stopped", seconds=10)
            with Store(data) as store:
                store.remember(projects[1], "Second fixture", "A newly queued local note survives a stopped worker.")
            assert enqueue(projects[1], data)["status"] == "queued"
            assert service_status(data)["queued_projects"] == 1
            restarted = start_service(data)
            receipt["restart"] = restarted
            assert restarted["status"] in {"started", "starting", "already_starting", "running"}, restarted
            restarted_live = wait_until(lambda: (s if (s := service_status(data))["running"] else None))
            receipt["restart_pid"] = restarted_live["pid"]
            wait_until(lambda: service_status(data)["queued_projects"] == 0)
            with Store(data) as store:
                assert store.embedding_status(
                    projects[1], model=MODEL, revision=MODEL_REVISION, dimensions=DIMENSIONS
                )["indexed"] == 2
            final_stop = stop_service(data)
            assert final_stop["status"] == "stopping", final_stop
            wait_until(lambda: service_status(data)["status"] == "stopped", seconds=10)
            receipt.update(status="passed", final_service=service_status(data),
                           workers={"concurrent_reported_pids": sorted(reported_pids),
                                    "first_pid": receipt["first_pid"],
                                    "restart_pid": receipt["restart_pid"]},
                           checks=["concurrent starts yield one active worker", "queue drains two projects",
                                   "real local embeddings", "no AI observation calls for explicit fixtures",
                                   "durable enqueue while stopped", "restart resumes pending work", "cooperative stop"])
        finally:
            stop_service(data)
            wait_until(lambda: service_status(data)["status"] == "stopped", seconds=15)
    receipt["temporary_data_removed"] = True
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run()
    except Exception as exc:
        result = {"status": "failed", "error": type(exc).__name__, "detail": str(exc)[:1500]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
