"""Verify historical known-control evidence without a model call or run clock."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
R8 = HERE.parent / 'project-memory-quality-2026-10-21-r8'
PRIVATE_R8 = Path('/private/tmp/codex-mem-quality-20261021-r8-experiment')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    sys.path.insert(0, str(ROOT))
    helper_path = ROOT / 'scripts/project_memory_consolidation_experiment.py'
    helper = load('historical_calibration_helper', helper_path)
    protocol = helper.read(HERE / 'protocol.json')
    frozen = helper.read(R8 / 'freeze.json')
    if sha(helper_path) != frozen['helper_sha256']:
        raise ValueError('judge helper changed; historical evidence needs reassessment')
    processor = ROOT / 'codex_mem/processor.py'
    if sha(processor) != frozen['candidate_hashes']['codex_mem/processor.py']:
        raise ValueError('native processor changed; historical profile proof is insufficient')
    source_protocol = helper.read(R8 / 'protocol.json')
    if (helper.phase_profile(protocol, 'judge') != helper.phase_profile(source_protocol, 'judge')
            or helper.judge_prompt_version(protocol) != helper.judge_prompt_version(source_protocol)):
        raise ValueError('judge profile or prompt changed')
    library_path = R8 / 'public-calibration-library.py'
    controls_path = R8 / 'judge-contract-controls.json'
    declared = source_protocol['judge_calibration']
    if sha(library_path) != declared['library_sha256'] or sha(controls_path) != declared['controls_sha256']:
        raise ValueError('historical control definitions changed')
    library = load('historical_known_control_definitions', library_path)
    controls = helper.read(controls_path)
    library.validate_oracle(controls)
    _, payload = library.prepare_packets(helper, controls)[-1]
    request = helper.judge_request(payload, protocol)
    summary_path = R8 / 'calibration-summary.json'
    summary = helper.read(summary_path)
    if summary['status'] != 'completed' or len(summary['results']) != 1:
        raise ValueError('historical calibration did not complete')
    result = summary['results'][0]
    if result['status'] != 'matched':
        raise ValueError('historical known-control check did not match')
    ledger = helper.ledger_calls(PRIVATE_R8 / 'calls.jsonl')
    starts = [row for row in ledger if row.get('status') == 'started'
              and all(row.get(key) == value for key, value in result['attempt'].items())]
    if len(starts) != 1:
        raise ValueError('historical control must bind to exactly one original ledger start')
    call_id = starts[0]['call_id']
    request_path = PRIVATE_R8 / 'receipts' / (call_id + '-request.json')
    response_path = PRIVATE_R8 / 'receipts' / (call_id + '-response.json')
    old_request, response = helper.read(request_path), helper.read(response_path)
    if helper.serialized_request(request) != helper.serialized_request(old_request):
        raise ValueError('complete judge request or output schema changed')
    if starts[0]['request_sha256'] != helper.digest(old_request):
        raise ValueError('historical request is not bound to its native ledger start')
    if helper.digest(response) != result['response_sha256']:
        raise ValueError('historical native response changed')
    helper.validate_native_profile(response, helper.phase_profile(protocol, 'judge'))
    rows = [row for row in ledger if row['call_id'] == call_id]
    usage = helper.usage(rows)
    if (usage['attempted_calls'] != 1 or usage['finished_calls'] != 1
            or usage['failed_calls'] or usage['unresolved_calls']
            or usage['tokens']['total_tokens'] != 18851):
        raise ValueError('historical ledger accounting differs')
    proof = {
        'status': 'verified_historical_preflight_only', 'new_native_calls': 0,
        'helper_sha256': sha(helper_path), 'native_processor_sha256': sha(processor),
        'prompt_version': helper.judge_prompt_version(protocol),
        'judge_profile': helper.phase_profile(protocol, 'judge'),
        'request_digest_sha256': helper.digest(request),
        'serialized_request_sha256': hashlib.sha256(helper.serialized_request(request)).hexdigest(),
        'original_request_file_sha256': sha(request_path),
        'original_response_file_sha256': sha(response_path),
        'original_result_file_sha256': sha(summary_path),
        'original_ledger_rows_sha256': helper.digest(rows),
        'controls_sha256': sha(controls_path), 'library_sha256': sha(library_path),
        'original_call_id': call_id, 'original_usage': usage,
        'source': '../project-memory-quality-2026-10-21-r8/calibration-summary.json',
        'accounting': 'Original call stays charged once in R8 cumulative history. No R10 native receipt or cache entry is created.',
        'limits': 'An earlier known-control check, not fresh or independent judge validation. Availability does not prove unchanged model behavior. This is no evidence for the production retrieval repair.',
    }
    helper.write(HERE / 'historical-calibration-proof.json', proof)
    print(json.dumps({key: proof[key] for key in ('status', 'new_native_calls', 'serialized_request_sha256')}))


if __name__ == '__main__':
    main()
