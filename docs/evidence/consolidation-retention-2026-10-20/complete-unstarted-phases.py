"""Complete only untouched planned phases after the frozen driver stopped.

This operator continuation is a disclosed procedural deviation. It never
retries a started phase or repairs a rejected answer, grade, or pipeline output.
The frozen product, helper, protocol, driver, fixture, and gold stay unchanged.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    evidence = Path(__file__).resolve().parent
    repo = evidence.parents[2]
    run = Path("/tmp/codex-mem-consolidation-retention-experiment").resolve()
    baseline = Path("/tmp/codex-mem-retention-baseline").resolve()
    script = repo / "scripts/project_memory_consolidation_experiment.py"
    protocol_path = evidence / "protocol.json"
    fixture_path = evidence / "inputs/holdout.json"
    gold_path = evidence / "inputs/holdout.gold.json"
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("experiment", script)
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    protocol = experiment.read(protocol_path)
    journal = run / "baseline/holdout-manual-continuation.jsonl"
    histories = ["fiction-07", "fiction-08", "fiction-09", "fiction-10",
                 "fiction-13", "fiction-18", "fiction-20"]
    pins = {
        script: "b44f489342588c28c2014356391f5b51720f57f5d1db0139147f4773d93d2475",
        evidence / "run-experiment.py": protocol["driver_sha256"],
        fixture_path: "f67209139b4e990e1e91d8dcb0f1c6f06dcc77ff4879beb5050e4de9dc7c39d6",
        gold_path: "4b532bf1d9f713b849e7719b606e5133325d09ca74bac897e76933ed05db522f",
    }
    for path, expected in pins.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"frozen_input_changed:{path.name}")
    subprocess.run([
        sys.executable, str(script), "validate", "--run", str(run),
        "--protocol", str(protocol_path), "--fixture", str(fixture_path),
        "--gold", str(gold_path),
    ], cwd=repo, check=True)
    counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
    if counters["attempted_calls"] != 59 or not counters["token_accounting_complete"]:
        raise RuntimeError("unexpected_initial_ledger_no_continuation")
    if journal.exists():
        raise RuntimeError("continuation_already_started_no_retry")
    experiment.append(journal, {
        "status": "operator_continuation_started", "at": time.time(),
        "reason": "Frozen driver stopped after baseline fiction-20 judge fact coverage rejection.",
        "procedural_deviation": True, "automatic_retry": False,
        "operator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "initial_usage": counters,
        "planned_candidate_judges": histories,
        "planned_regeneration": "fiction-05/regeneration-1",
    })
    live = {"generate", "consolidate", "read", "judge"}

    def execute(phase, history=None, variant=None, replicate=None):
        identity = dict(phase=phase, history_id=history, variant=variant,
                        replicate=replicate)
        entries = [json.loads(line) for line in (run / "calls.jsonl").read_text().splitlines()]
        ledger_phase = {"generate": "generation", "read": "reader"}.get(phase, phase)
        ledger_arm = "baseline" if phase == "generate" else "candidate" if phase == "consolidate" else variant
        run_id = f"replicates/{replicate}" if replicate else "primary"
        if phase in live and any(
            item.get("status") == "started" and item.get("phase") == ledger_phase
            and item.get("variant") == ledger_arm and item.get("history_id") == history
            and item.get("run_id") == run_id for item in entries
        ):
            raise RuntimeError("phase_already_started_no_retry:" + repr(identity))
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        limits = protocol["limits"]
        elapsed = time.time() - experiment.read(run / "run.json")["started_at"]
        if phase in live and (
            counters["attempted_calls"] >= limits["max_calls"]
            or counters["tokens"]["total_tokens"] + limits["per_call_token_reservation"]
            > limits["max_observed_tokens"]
            or elapsed + limits["call_seconds"] >= limits["wall_seconds"]
        ):
            raise RuntimeError("budget_exhausted_stop")
        runtime = baseline if phase == "generate" or variant == "baseline" else repo
        command = [
            sys.executable, str(script), phase, "--run", str(run),
            "--runtime-root", str(runtime), "--protocol", str(protocol_path),
            "--fixture", str(fixture_path),
        ]
        for flag, value in (("--history", history), ("--variant", variant),
                            ("--replicate", replicate)):
            if value:
                command += [flag, value]
        if phase in {"judge", "report"}:
            command += ["--gold", str(gold_path)]
        if phase in live:
            command += ["--live"]
        experiment.append(journal, dict(identity, status="started", at=time.time(),
                                        command=command))
        result = subprocess.run(command, cwd=repo)
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        experiment.append(journal, dict(identity, status="finished", at=time.time(),
                                        returncode=result.returncode, usage=counters))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        if result.returncode:
            # The rejected output stays missing. Continue only untouched phases.
            experiment.append(journal, dict(identity, status="rejected_no_retry",
                                            at=time.time(), automatic_retry=False))
            return False
        if phase in {"generate", "consolidate"}:
            home = run / "replicates" / replicate if replicate else run
            arm = "baseline" if phase == "generate" else "candidate"
            name = "generation.json" if phase == "generate" else "consolidation.json"
            metadata = experiment.read(home / arm / history / name)
            if any(job.get("status") == "disabled" for job in metadata.get("jobs", [])):
                raise RuntimeError("processing_disabled_stop")
            if metadata["status"] != "complete":
                experiment.append(journal, dict(identity, status="pipeline_failure_no_retry",
                                                at=time.time(), automatic_retry=False))
                return phase != "generate"
        print(json.dumps(dict(identity, status="completed",
                              calls=counters["attempted_calls"],
                              tokens=counters["tokens"]["total_tokens"])), flush=True)
        return True

    try:
        for history in histories:
            execute("judge", history, "candidate")
        repeat = "regeneration-1"
        if execute("generate", "fiction-05", replicate=repeat):
            execute("clone", "fiction-05", replicate=repeat)
            execute("consolidate", "fiction-05", replicate=repeat)
            for phase in ("read", "judge"):
                for variant in ("baseline", "candidate"):
                    execute(phase, "fiction-05", variant, repeat)
        execute("report")
        experiment.append(journal, {"status": "operator_continuation_finished",
                                    "at": time.time(), "procedural_deviation": True})
    except Exception as exc:
        experiment.append(journal, {"status": "operator_continuation_stopped",
                                    "at": time.time(), "error": str(exc),
                                    "automatic_retry": False})
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
