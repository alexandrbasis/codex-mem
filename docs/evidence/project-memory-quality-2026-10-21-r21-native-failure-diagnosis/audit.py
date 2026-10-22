"""Read only retained R21 failure metadata. Never construct Store or retry calls."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

sys.dont_write_bytecode = True
out = Path(__file__).resolve().parent
root = out.parents[2]
sys.path.insert(0, str(root))
run = Path('/private/tmp/codex-mem-quality-20261021-r21-experiment')
sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
source_paths = ['codex_mem/processor.py', 'codex_mem/episode_processor.py', 'codex_mem/project_memory.py',
    'docs/evidence/project-memory-quality-2026-10-21-r21/frozen-helper.py',
    'docs/evidence/project-memory-quality-2026-10-21-r21/run-experiment.py']
inputs = [run / name for name in ['run.json', 'calls.jsonl', 'baseline/holdout-phase-execution.jsonl',
    'baseline/h12/generation.json', 'receipts/call-0055-request.json', 'holdout-report.json']]
inputs += [root / name for name in source_paths]
inputs += [root / 'docs/evidence/project-memory-quality-2026-10-21-r21/holdout/report.json',
           root / 'docs/evidence/project-memory-quality-2026-10-21-r21/protocol.json']
database = run / 'baseline/h12/data/memory.sqlite3'
inputs += sorted(database.parent.glob(database.name + '*'))
pins = {str(path): sha(path) for path in inputs}
read = lambda path: json.loads(Path(path).read_text())
ledger = [json.loads(line) for line in (run / 'calls.jsonl').read_text().splitlines() if line.strip()]
call = [row for row in ledger if row.get('call_id') == 'call-0055']
assert len(call) == 2 and [row['status'] for row in call] == ['started', 'failed']
request = read(run / 'receipts/call-0055-request.json')
assert not (run / 'receipts/call-0055-response.json').exists()
assert not any((run / 'receipts').glob('call-0056*'))
helper_path = root / source_paths[3]
spec = importlib.util.spec_from_file_location('r21_native_failure_read_only_helper', helper_path)
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
assert helper.digest(request) == call[0]['request_sha256']
usage = helper.usage(helper.ledger_calls(run / 'calls.jsonl'))
assert usage['attempted_calls'] == 55 and usage['calls_with_reported_tokens'] == 54
assert usage['calls_with_unknown_or_partial_tokens'] == 1 and not usage['token_accounting_complete']
connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
connection.execute('PRAGMA query_only=ON')
connection.row_factory = sqlite3.Row
try:
    attempt = dict(connection.execute('''SELECT attempt_id, project, session_id, job_id, processor_id,
        model, reasoning_effort, started_at, updated_at, finished_at, outcome, error_code,
        worker_thread_id, worker_turn_id, duration_ms, usage_status, usage_source, usage_updates,
        input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, reasoning_tokens, total_tokens
        FROM pm_usage_attempts WHERE attempt_id=?''', ('pma_44042fe5595d4060880cc3ff29d9d70f',)).fetchone())
    job = dict(connection.execute('''SELECT id,project,session_id,status,lease_until,retry_after,
        attempts,revision,generation,error,receipt_json,length(input_json) AS input_json_chars
        FROM pm_jobs WHERE id=?''', (attempt['job_id'],)).fetchone())
    job['receipt'] = json.loads(job.pop('receipt_json'))
    queue_counts = [dict(row) for row in connection.execute('SELECT status,count(*) AS count FROM pm_jobs GROUP BY status')]
    event_counts = [dict(row) for row in connection.execute('SELECT done,count(*) AS count FROM pm_events GROUP BY done')]
    attempts = [dict(row) for row in connection.execute('''SELECT attempt_id,outcome,error_code,usage_status,
        usage_updates,finished_at FROM pm_usage_attempts ORDER BY started_at''')]
finally:
    connection.close()
assert attempt['outcome'] == 'failed' and attempt['usage_status'] == 'unknown'
assert all(attempt[key] is None for key in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens',
                                          'output_tokens', 'reasoning_tokens', 'total_tokens'))
assert job['attempts'] == 1 and job['status'] == 'failed' and job['input_json_chars'] == 2
journal = [json.loads(line) for line in (run / 'baseline/holdout-phase-execution.jsonl').read_text().splitlines()]
assert journal[-1]['error'] == 'unknown_usage_stop' and journal[-1]['automatic_retry'] is False
started = read(run / 'run.json')['started_at']
assert started == 1792651854.4362519
report = read(run / 'holdout-report.json')
assert (root / 'docs/evidence/project-memory-quality-2026-10-21-r21/holdout/report.json').read_bytes() == (run / 'holdout-report.json').read_bytes()
assert report['decision'] == 'incomplete' and all(row['questions_graded'] == 0 for row in report['variants'].values())
protocol = read(root / 'docs/evidence/project-memory-quality-2026-10-21-r21/protocol.json')
missing_evaluations = 2 * protocol['holdout_primary_questions'] + 2 * 6 * len(protocol['repeat_history_ids'])
assert missing_evaluations == 216


def quoted(path, start_text, end_text=None):
    lines = (root / path).read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if start_text in line)
    end = next((index for index in range(start, len(lines)) if end_text in lines[index]), start) if end_text else start
    return {'path': path, 'start_line': start + 1, 'end_line': end + 1,
            'quote': '\n'.join(lines[start:end + 1])}


source_quotes = [
    quoted('codex_mem/processor.py', 'def _native_failure_reason', 'return "native_turn_failed"'),
    quoted('codex_mem/processor.py', '"internalServerError":', '"serverOverloaded":'),
    quoted('codex_mem/processor.py', 'for variant in ("httpConnectionFailed"', 'return "native_server_error"'),
    quoted('codex_mem/processor.py', 'if method.endswith("turn/completed"):', '# Ephemeral threads'),
    quoted('codex_mem/processor.py', '# Stderr is drained', '# include prompt'),
    quoted('codex_mem/processor.py', 'turn_id = _turn_id_from_response', '_wait_for_turn_completion(client, monitor)'),
    quoted('codex_mem/processor.py', 'failed_thread = exc.worker_thread_id or thread_id', 'metrics=metrics(),'),
    quoted(source_paths[3], 'result = NativeProcessorRunner(timeout=', 'write(self.home'),
    quoted(source_paths[3], 'except Exception as exc:', 'raise'),
    quoted('codex_mem/project_memory.py', 'status = "quarantined" if reason', 'return {"status": status'),
]
receipt = {
 'schema_version': 1, 'status': 'read_only_failure_stage_and_observability_audit_complete',
 'causal_reproduction': {'performed': False, 'possible_from_retained_artifacts': False,
   'reason': 'The failed-turn protocol payload, exact error tag/status and remote diagnostic reference were not retained. Retrying the native call is forbidden for this audit.',
   'mock_limit': 'The synthetic classification probe proves mapping and privacy behavior only. It does not reproduce call0055 or prove its remote root cause.'},
 'failure': {'call_rows': call, 'request_metadata': {key: request.get(key) for key in
    ('processor_id', 'output_schema_version', 'model', 'reasoning_effort', 'timeout')},
    'prompt_characters': len(request['prompt']), 'request_file_sha256': sha(run / 'receipts/call-0055-request.json'),
    'request_canonical_digest_matches_started_ledger': True, 'response_file_present': False,
    'actual_runner_class': 'codex_mem.processor.NativeProcessorRunner',
    'phase': 'generation of the shared common prefix, stored under baseline/h12, using candidate runtime',
    'stage_established_from_recorded_code': 'A matching turn/completed notification was classified as native_server_error after a valid turn/start response supplied a turn ID. A successful final-output read/validation was not reached.',
    'stage_not_established': ['The exact turn status value is absent. The failed-turn branch also accepts other non-completed states, so literal status=failed is not separately proved.',
      'A separate turn/started notification is not proved. The error can be buffered before turn/start returns, then applied after its ID is set.',
      'There is no retained proof that model token generation began or how much provider work ran.',
      'The client classification does not identify an internal Codex app-server fault versus an upstream service fault.'],
    'mapped_routes': {'enum_tags': ['internalServerError', 'serverOverloaded'],
      'singleton_http_variants': ['httpConnectionFailed', 'responseStreamConnectionFailed', 'responseStreamDisconnected', 'responseTooManyFailedAttempts'],
      'integer_http_status_range': [500,599]},
    'different_local_errors': 'JSON-RPC error responses, malformed transport and local deadline expiration use protocol_error/timeout, not this mapped reason. An upstream HTTP504 remains one possible shape within the recorded server-error category, not an observed cause.'},
 'timeline': {'original_started_at': started, 'original_started_at_utc': datetime.fromtimestamp(started,timezone.utc).isoformat(),
    'call_started_at': call[0]['at'], 'driver_stopped_at': journal[-1]['at'],
    'elapsed_to_call_start_seconds': call[0]['at'] - started, 'elapsed_to_stop_seconds': journal[-1]['at'] - started,
    'native_metrics_duration_ms': call[1]['metrics']['duration_ms'], 'capture_wall_ms': call[1]['wall_ms'],
    'episode_processor_duration_ms': attempt['duration_ms'],
    'duration_limit': 'Native metrics include local setup and failure-handling/interrupt work. They are not isolated model-compute or provider latency.'},
 'queue_checkpoint_metadata': {'attempt': attempt, 'job': job, 'queue_status_counts': queue_counts,
    'event_done_counts': event_counts, 'attempt_summaries': attempts,
    'incremental_checkpoint_limit': 'The experiment injects Capture as the runner. Capture creates its inner NativeProcessorRunner without forwarding episode_processor checkpoint. No native usage update or intermediate native progression record is retained for this failed call. The final exception contributes worker IDs and unavailable usage.',
    'reason_retention': 'calls.jsonl retains native_server_error. The episode generation receipt and pm_jobs/pm_usage_attempts retain only runner_failure and do not retain the specific mapped reason.',
    'queue_recovery_state_not_retry_authorization': 'The failed job retains attempts1 and a retry_after value. The experiment stopped before another native call; this metadata does not authorize an R21 retry.'},
 'experiment': {'last_journal_entries': journal[-3:], 'run_clock_unchanged': True, 'automatic_retry': False,
    'shared_prefix_attribution': 'Both future reader arms would share this generation result; this failure is before h12 candidate cloning/reading/judging.',
    'primary_decision': report['decision'], 'pilot_gate': report['pilot_gate'], 'primary_complete': report['primary_complete'],
    'questions_graded_by_arm': {name: row['questions_graded'] for name,row in report['variants'].items()},
    'missing_evaluations_protocol_derived': missing_evaluations,
    'phase_attempts': dict(Counter(row['phase'] for row in ledger if row['status']=='started')),
    'phase_count_limit': 'The8 reader and2 judge calls belong to preflight/calibration, not completed sealed primary answers. Zero graded answers does not mean zero earlier native activity.'},
 'cost_and_usage': usage,
 'cost_limit': '770655 reported tokens are a lower bound over54 calls. The failed call may consume tokens despite updates0. Total tokens, actual cost and complete hypothetical cost remain unknown. Stored API-equivalent amounts are dated estimates over observed usage, not billed dollars.',
 'evidence_gaps': ['Raw native failed-turn error tag, integer status and exact turn status were not retained.',
    'No call0055 response envelope, app-server protocol trace, server request ID or safe remote diagnostic ID is retained.',
    'Stderr is deliberately drained without retention, because it can contain prompt/configuration data.',
    'No usage notification was recorded for call0055. Null token fields cannot prove zero usage.',
    'The wrapper does not persist a native stage timeline, separate turn-started boolean, completed-item count or per-stage durations.',
    'Account/model flags are not part of this audit and cannot establish the cause of this earlier failure.'],
 'future_bounded_telemetry_proposal': {'implementation_performed': False,
    'scope': 'A future authorized development task and new run only; do not retry or rewrite R21.',
    'minimum_failure_tuple': ['allowlisted native stage enum', 'allowlisted codexErrorInfo tag or singleton variant',
      'integer HTTP status if present', 'separate turn-started-observed boolean', 'existing usage status/update count'],
    'retention': 'Attach this bounded content-free tuple to ProcessorFailure and the failed Capture ledger row, together with existing worker IDs for joining to the attempt. Keep raw messages, stderr, prompts, configuration, auth and URLs excluded.',
    'followup_validation': 'Use synthetic enum/status/phase controls to prove sanitization and lossless forwarding. A later newly authorized native failure would be needed for causal diagnosis; the mock cannot supply one.',
    'optional_checkpoint_note': 'If experimental crash-checkpoint recovery is required, separately design forwarding of the bounded usage callback through Capture. That is not necessary merely to preserve the error tag/status and is not a fix for this remote failure.'},
 'source_quotes': source_quotes, 'input_sha256': pins,
 'classification_probe': {'path': str(out/'classification-probe.json'), 'sha256': sha(out/'classification-probe.json')},
 'native_calls': 0, 'model_calls': 0, 'network_calls': 0, 'original_store_constructors': 0,
 'global_logs_or_user_history_or_auth_read': False, 'original_sqlite_access': 'mode=ro plus PRAGMA query_only=ON',
 'source_or_old_receipt_changes': 0,
}
assert all(sha(path) == expected for path,expected in pins.items())
receipt['all_pinned_inputs_unchanged'] = True
(out / 'receipt.json').write_text(json.dumps(receipt,indent=2) + '\n')
print(json.dumps({'status': receipt['status'], 'native_root_cause_proved': False,
    'reported_token_lower_bound': usage['reported_tokens_lower_bound']['total_tokens'],
    'unknown_usage_calls': usage['calls_with_unknown_or_partial_tokens'],
    'queue_attempts': job['attempts'], 'pinned_inputs_unchanged': len(pins), 'receipt_sha256': sha(out/'receipt.json')}))
