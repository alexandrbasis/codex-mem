"""Compare displayed source intervals only. Never read or grade answers."""
import hashlib
import json
from pathlib import Path
import re
import tarfile
import xml.etree.ElementTree as ET

out = Path(__file__).resolve().parent
root = out.parents[2]
run = Path('/private/tmp/codex-mem-quality-20261021-r19-experiment')
before = json.loads((out / 'before-disclosed-replay.json').read_text())
after = json.loads((out / 'after-disclosed-replay.json').read_text())
assert before['original_pins'] == after['original_pins']
assert len(before['rows']) == len(after['rows']) == 96
assert all(row['saved_byteexact'] for row in before['rows'])


def covers(old, new):
    same = all(old[key] == new[key] for key in ('source_id', 'source_role', 'source_field'))
    start = int(old['start']) - int(new['start'])
    return same and start >= 0 and new['quote'][start:start + len(old['quote'])] == old['quote']


rows, loss_count = [], 0
for old, new in zip(before['rows'], after['rows']):
    assert (old['case'], old['id']) == (new['case'], new['id'])
    loss = [source for source in old['sources'] if not any(covers(source, item) for item in new['sources'])]
    gain = [source for source in new['sources'] if not any(covers(source, item) for item in old['sources'])]
    loss_count += len(loss)
    if old['context_sha256'] != new['context_sha256']:
        rows.append({'case': old['case'], 'id': old['id'], 'question': old['question'],
            'old_visible_source_intervals': len(old['sources']),
            'new_visible_source_intervals': len(new['sources']),
            'lost_intervals': loss, 'gained_intervals': gain,
            'after_context_characters': new['context_characters']})
assert loss_count == 0
loss_case = next(row for row in rows if (row['case'], row['id']) == ('R19H11', 'q3'))
assert any('I have not exported the public cut yet.' in source['quote'] and
           source['source_role'] == 'assistant_report' for source in loss_case['gained_intervals'])
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
frozen = Path(json.loads((out / 'replay-runtime.json').read_text())['frozen_runtime'])
changed = [str(path.relative_to(root)) for path in (root / 'codex_mem').glob('*.py')
           if sha(path) != sha(frozen / path.relative_to(root))]
assert changed == ['codex_mem/project_memory_query.py']
checks = []
for item in json.loads((out / 'targeted-tests.json').read_text()):
    log = Path(item['log'])
    text = log.read_text()
    count = int(re.search(r'Ran (\d+) tests?', text).group(1))
    assert item['exit_code'] == 0 and text.rstrip().endswith('OK')
    checks.append({**item, 'tests': count, 'log_sha256': sha(log)})
receipt = {
    'status': 'offline_query_repair_regressions_verified_only',
    'quality_gate_pass_claim': False, 'answer_or_grade_changes': 0,
    'r19_status': 'closed FAIL unchanged',
    'runtime_file_changed': changed,
    'query_before_sha256': sha(out / 'query-before.py'),
    'query_after_sha256': sha(root / 'codex_mem/project_memory_query.py'),
    'contract_tests_sha256': sha(root / 'tests/test_project_memory_query_compound_contract.py'),
    'targeted_tests': checks, 'targeted_test_total': sum(item['tests'] for item in checks),
    'vertical_red_green_cycles': 6,
    'replay': {
        'scope': '96 disclosed R19 candidate contexts before and after query repair',
        'before_saved_byteexact': 96, 'after_saved_byteexact': sum(row['saved_byteexact'] for row in after['rows']),
        'changed_contexts': len(rows), 'source_visibility_gain_contexts': sum(bool(row['gained_intervals']) for row in rows),
        'lost_previously_visible_source_intervals': loss_count,
        'maximum_context_characters': max(row['context_characters'] for row in after['rows']),
        'context_budget': 6000,
        'owner_budget_formula': 'max(1000, 6000 // len(selected_scopes) - 100)',
        'original_store_policy': 'SQLite mode=ro backup only; Store opens fresh scratch copies only',
        'original_pinned_files_unchanged': len(after['original_pins']),
        'source_rows': rows,
        'network_calls': 0, 'native_calls': 0, 'model_calls': 0,
        'before_receipt_sha256': sha(out / 'before-disclosed-replay.json'),
        'after_receipt_sha256': sha(out / 'after-disclosed-replay.json'),
        'superseded_attempt': 'incorrect-owner-budget replay files retain initial seven multi-scope mismatches; excluded from final conclusions',
    },
    'architecture_source_delta': {
        'stage': 'local read query planning',
        'change': 'Unquoted alphabetic hyphen modifier plus ordinary alphabetic head is topical unless explicit identity or existing scope guard protects it. Only lexical matching input splits eligible compounds.',
        'unchanged': 'Original query bytes, original source bytes and spans, project read ownership, model/provider boundaries, capture/processing/storage, reader prompt and all saved R19 answers/grades.',
        'identity_protection': 'Quotes including multiword phrases, function/module/file/command/artifact/task/version/environment labels, calls, paths, members, numeric/version/environment constraints, explicit unknown project labels and coordinated scope names.',
    },
    'limits': [
        'This finite English syntax convention does not identify nouns or infer intended identity.',
        'A bare unknown or unselected scope name retains prior hard guards. An unquoted name plus ordinary-looking head is ambiguous and follows the modifier convention.',
        'Unsupported function words or adverbs can resemble heads. Quoting or explicit project/code labels disambiguate literal identity.',
        'Source visibility is mechanical. It does not establish answer quality, entailment, acceptance or a fresh R20 goal pass.',
        'No fresh R20 sealed input was read and no model, native, network or semantic-classifier call was made.',
    ],
}
(out / 'final-query-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({'tests': receipt['targeted_test_total'], 'source_gain_contexts': receipt['replay']['source_visibility_gain_contexts'],
                  'source_losses': loss_count, 'max_context_chars': receipt['replay']['maximum_context_characters'],
                  'receipt_sha256': sha(out / 'final-query-receipt.json')}))
