"""Run one fictional split serially, without retries or global configuration changes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts/project_memory_consolidation_experiment.py"
    protocol_path = Path(__file__).with_name("protocol.json")
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("experiment", script)
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    protocol = experiment.read(protocol_path)
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != protocol["driver_sha256"]:
        raise RuntimeError("experiment_driver_changed_after_protocol_pin")
    run = args.run.resolve()
    if not str(run).startswith("/private/tmp/"):
        raise ValueError("Run directory must resolve under /private/tmp")
    if os.environ.get("CODEX_MEM_DISABLED") == "1":
        raise RuntimeError("unexpected_disabled_environment")
    # The helper checks frozen inputs before this driver reads any history.
    subprocess.run([
        sys.executable, str(script), "validate", "--run", str(run),
        "--protocol", str(protocol_path), "--fixture", str(args.fixture),
        "--gold", str(args.gold),
    ], cwd=repo, check=True)
    fixture = experiment.fixture(args.fixture)
    split = fixture["split"]
    journal = run / "baseline" / f"{split}-phase-execution.jsonl"
    live = {"generate", "consolidate", "read", "judge"}

    def execute(phase, history=None, variant=None, replicate=None):
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        elapsed = time.time() - experiment.read(run / "run.json")["started_at"]
        limits = protocol["limits"]
        if phase in live and (
            counters["attempted_calls"] >= limits["max_calls"]
            or counters["tokens"]["total_tokens"] + limits["per_call_token_reservation"]
            > limits["max_observed_tokens"]
            or elapsed + limits["call_seconds"] >= limits["wall_seconds"]
        ):
            raise RuntimeError("budget_exhausted_stop")
        runtime = args.baseline if phase == "generate" or variant == "baseline" else repo
        command = [
            sys.executable, str(script), phase, "--run", str(run),
            "--runtime-root", str(runtime), "--protocol", str(protocol_path),
            "--fixture", str(args.fixture),
        ]
        for flag, value in (("--history", history), ("--variant", variant),
                            ("--replicate", replicate)):
            if value:
                command += [flag, value]
        if phase in {"judge", "report"}:
            command += ["--gold", str(args.gold)]
        if phase in live:
            command += ["--live"]
        identity = dict(phase=phase, history_id=history, variant=variant, replicate=replicate)
        experiment.append(journal, dict(identity, status="started", at=time.time(), command=command))
        result = subprocess.run(command, cwd=repo)
        experiment.append(journal, dict(identity, status="finished", at=time.time(),
                                        returncode=result.returncode))
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        if result.returncode:
            raise RuntimeError("phase_failed_no_automatic_retry:" + phase)
        home = run / "replicates" / replicate if replicate else run
        if phase in {"generate", "consolidate"}:
            arm = "baseline" if phase == "generate" else "candidate"
            name = "generation.json" if phase == "generate" else "consolidation.json"
            metadata = experiment.read(home / arm / history / name)
            if metadata["status"] != "complete":
                if any(job.get("status") == "disabled" for job in metadata.get("jobs", [])):
                    raise RuntimeError("processing_disabled_stop")
                experiment.append(journal, dict(
                    identity, status="pipeline_failure_no_retry", at=time.time(),
                    jobs=[{key: job.get(key) for key in ("status", "code")}
                          for job in metadata.get("jobs", [])],
                ))
                # Failed extraction has no valid paired memory. Failed
                # consolidation keeps its unchanged memory for paired readers.
                if phase == "generate":
                    return False
        print(json.dumps(dict(identity, status="phase_complete",
                              calls=counters["attempted_calls"],
                              tokens=counters["tokens"]["total_tokens"])), flush=True)
        return True

    try:
        histories = [history["id"] for history in fixture["histories"]]
        eligible = [history for history in histories if execute("generate", history)]
        for history in eligible:
            execute("clone", history)
            execute("consolidate", history)
        for phase in ("read", "judge"):
            for variant in ("baseline", "candidate"):
                for history in eligible:
                    execute(phase, history, variant)
        for history in histories:
            if history not in protocol["regeneration_repeat_history_ids"]:
                continue
            repeat = "regeneration-1"
            if execute("generate", history, replicate=repeat):
                execute("clone", history, replicate=repeat)
                execute("consolidate", history, replicate=repeat)
                for phase in ("read", "judge"):
                    for variant in ("baseline", "candidate"):
                        execute(phase, history, variant, repeat)
        execute("report")
    except Exception as exc:
        experiment.append(journal, dict(status="driver_stopped", at=time.time(),
                                        error_type=type(exc).__name__, error=str(exc),
                                        automatic_retry=False))
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
