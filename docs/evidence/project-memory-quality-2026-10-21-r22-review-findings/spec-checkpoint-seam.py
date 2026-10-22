"""Public synthetic Capture-to-auditor seam. No native calls or Store access."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r22-audit'))
from pipeline_contract import validate_checkpoints

spec = importlib.util.spec_from_file_location('spec_checkpoint_helper', ROOT / 'scripts/project_memory_consolidation_experiment.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

with tempfile.TemporaryDirectory(prefix='r22-spec-checkpoint-') as temporary:
    run = Path(temporary)
    (run / 'receipts').mkdir()
    helper.write(run / 'run.json', {'budget_run': str(run), 'started_at': 0})
    capture = helper.Capture(run, {'model': 'gpt-6-luna', 'reasoning_effort': 'medium'}, 'generation', 'baseline', 'public-toy')
    base = {**capture.identity, 'call_id': 'call-0001'}
    tokens = dict(input_tokens=7, cached_input_tokens=0, cache_write_input_tokens=0,
                  output_tokens=3, reasoning_output_tokens=0, total_tokens=10)
    capture._save_checkpoint(base, {'worker_thread_id': 'public-thread', 'worker_turn_id': None,
        'metrics': {'duration_ms': 10, 'usage': {'status': 'reported',
        'source': 'app_server_thread_total', 'updates': 1, 'tokens': tokens}}})
    path = run / 'receipts/call-0001-checkpoint.json'
    assert path.exists(), 'Producer must actually emit a checkpoint'
    checks = []
    result = validate_checkpoints(run, {'call-0001': base}, lambda value, label: checks.append((label, value)))
    assert result['count'] == 1, 'Consumer must actually inspect the checkpoint'
    positive_failures = [label for label, value in checks if not value]
    data = json.loads(path.read_text())
    data['history_id'] = 'wrong-public-history'
    helper.write(path, data)
    negative_checks = []
    negative_result = validate_checkpoints(run, {'call-0001': base}, lambda value, label: negative_checks.append((label, value)))
    assert negative_result['count'] == 1
    assert ('checkpoint_attempt_binding:call-0001', False) in negative_checks
    print(json.dumps({'saved_file': True, 'consumer_count': result['count'],
        'saved_token_keys': sorted(tokens), 'positive_failures': positive_failures,
        'wrong_attempt_negative_rejected': True}))
    assert not positive_failures, 'Valid Capture checkpoint must pass its audit consumer'
