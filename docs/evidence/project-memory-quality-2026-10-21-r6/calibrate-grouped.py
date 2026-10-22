"""Run one declared known-control regression before the R6 holdout freeze."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    runner = REPO / 'docs/evidence/project-memory-quality-2026-10-21-r3/native-judge-calibration.py'
    spec = importlib.util.spec_from_file_location('known_control_regression', runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = module.load_helper(module.DEFAULT_HELPER)
    protocol = helper.read(args.protocol)
    declaration = protocol['judge_calibration']
    controls_path = REPO / declaration['controls']
    controls = helper.read(controls_path)
    module.validate_oracle(controls)
    if (declaration['runner_sha256'] != module.file_sha256(Path(__file__))
            or declaration['controls_sha256'] != module.file_sha256(controls_path)
            or declaration['packet_ids'] != ['grouped-six']
            or declaration['max_calls'] != 1
            or declaration['max_observed_tokens'] != 200000
            or (protocol['model'], protocol['reasoning_effort']) != ('gpt-6-luna', 'medium')
            or helper.judge_prompt_version(protocol) != 'answer-source-span-refs-v3'
            or controls.get('fictional') is not True or len(controls['controls']) != 6):
        raise ValueError('known-control regression requires exact declared files, packet, profile and limits')
    prepared = [module.prepare_packets(helper, controls)[-1]]
    plan = {'status': 'prepared_no_native_calls', 'control_count': 6, 'packet_count': 1,
            'planned_native_calls': 1, 'interpretation': 'Known-control regression, not independent judge validation.',
            'controls_sha256': module.file_sha256(controls_path),
            'request_sha256': [helper.digest(helper.judge_request(prepared[0][1], protocol))]}
    if not args.live:
        print(json.dumps(plan))
        return
    run = args.run.resolve()
    if not str(run).startswith('/private/tmp/'):
        raise ValueError('regression requires an isolated original budget root')
    metadata_path = run / 'run.json'
    metadata_bytes = metadata_path.read_bytes()
    metadata = json.loads(metadata_bytes)
    if (metadata['budget_run'] != str(run) or metadata['protocol_sha256'] != helper.digest(protocol)
            or (run / 'freeze.json').exists() or helper.ledger_calls(run / 'calls.jsonl')):
        raise ValueError('regression requires the matching unused original clock before freeze')
    with (run / 'judge-calibration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = module.dispatch_controls(helper, run, protocol, declaration, prepared, plan,
                                          metadata_path, metadata_bytes)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
