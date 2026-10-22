"""Offline verification of immutable R13 evidence; no model execution or grading."""
from pathlib import Path
import collections
import hashlib
import importlib.util
import json
import re
import tarfile
import urllib.parse

BASE = Path(__file__).resolve().parent
HOLDOUT = BASE / 'holdout'
RUN = Path('/private/tmp/codex-mem-quality-20261021-r13-experiment')
SEALED = Path('/private/tmp/codex-mem-quality-20261021-r13-sealed')
def sha(data):
    return hashlib.sha256(data).hexdigest()
def read(path):
    return json.loads(path.read_bytes())
def archive(path):
    with tarfile.open(path) as handle:
        return {item.name: handle.extractfile(item).read() for item in handle.getmembers() if item.isfile()}

spec = importlib.util.spec_from_file_location('r13_frozen_audit', BASE / 'frozen-helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper_hash = sha((BASE / 'frozen-helper.py').read_bytes())
assert helper_hash == 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388'
manifest = read(HOLDOUT / 'export-manifest.json')
sanitization = read(HOLDOUT / 'sanitization.json')
report = read(HOLDOUT / 'report.json')
protocol = read(HOLDOUT / 'protocol.json')
fixture = read(HOLDOUT / 'fixture.json')
gold = read(HOLDOUT / 'gold.json')
questions = read(HOLDOUT / 'per-question.json')['questions']
members = archive(HOLDOUT / 'fictional-run-evidence.tar.gz')
def member(name):
    return json.loads(members[name])
assert len(members) == manifest['archived_files']
for name, expected in manifest['files'].items():
    assert sha((HOLDOUT / name).read_bytes()) == expected
for name, hashes in sanitization['changed_members'].items():
    assert sha(members[name]) == hashes['public_export_sha256']
assert (HOLDOUT / 'experiment.py').read_bytes() == (BASE / 'frozen-helper.py').read_bytes()
assert (HOLDOUT / 'protocol.json').read_bytes() == (BASE / 'protocol.json').read_bytes()
pins = read(BASE / 'sealed-input-pins.json')['files']
for name, expected in pins.items():
    assert sha((SEALED / name).read_bytes()) == expected
assert (HOLDOUT / 'fixture.json').read_bytes() == (SEALED / 'holdout-fixture.json').read_bytes()
assert (HOLDOUT / 'gold.json').read_bytes() == (SEALED / 'holdout-gold.json').read_bytes()
product_manifest = read(BASE / 'frozen-product-manifest.json')
product = archive(BASE / 'frozen-product.tar.gz')
freeze = read(BASE / 'freeze.json')
assert len(product) == product_manifest['file_count'] == 47
assert sha((BASE / 'frozen-product.tar.gz').read_bytes()) == product_manifest['archive_sha256']
assert product_manifest['files'] == freeze['candidate_hashes']
for name, expected in product_manifest['files'].items():
    assert sha(product[name]) == expected
    assert sha((BASE.parents[2] / name).read_bytes()) == expected
assert sha((BASE.parents[2] / 'scripts/project_memory_consolidation_experiment.py').read_bytes()) == helper_hash
support_count = fact_count = 0
for history in fixture['histories']:
    events = {event['id']: event for session in history['sessions'] for event in session['events']}
    for question in history['questions']:
        for fact in gold['questions'][history['id'] + '/' + question['id']]['required_facts']:
            fact_count += 1
            for support in fact['support']:
                event = events[support['event_id']]
                field = support.get('source_field', 'body')
                text = event.get('text', '')
                if field != 'body':
                    container, key = field.split('.', 1)
                    text = event['tool_capture'][container][key]
                assert text[support['start']:support['end']] == support['quote']
                support_count += 1
assert fact_count == 128
assert members['calls.jsonl'] == (RUN / 'calls.jsonl').read_bytes()
ledger = [json.loads(line) for line in members['calls.jsonl'].splitlines()]
starts = {row['call_id']: row for row in ledger if row['status'] == 'started'}
ends = {row['call_id']: row for row in ledger if row['status'] == 'completed'}
assert len(starts) == len(ends) == 319 and len(ledger) == 638
raw = {}
for call_id, attempt in starts.items():
    for kind in ['request', 'response']:
        name = 'receipts/' + call_id + '-' + kind + '.json'
        raw[name] = (RUN / name).read_bytes()
    request = json.loads(raw['receipts/' + call_id + '-request.json'])
    response = json.loads(raw['receipts/' + call_id + '-response.json'])
    assert helper.digest(request) == attempt['request_sha256']
    assert response['metrics'] == ends[call_id]['metrics']
    expected_profile = helper.phase_profile(protocol, attempt['phase'])
    assert request['model'] == attempt['model'] == expected_profile['model']
    assert request['reasoning_effort'] == attempt['reasoning_effort'] == expected_profile['reasoning_effort']
    assert attempt['helper_sha256'] == helper_hash
    assert attempt['protocol_sha256'] == helper.digest(protocol)
cleaned, _ = helper.sanitize_export_members(raw)
assert all(members[name] == data for name, data in cleaned.items())
totals = {key: sum(row['metrics']['usage']['tokens'][key] for row in ends.values())
          for key in ['input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens']}
assert totals == {'input_tokens': 4938641, 'output_tokens': 144742, 'cached_input_tokens': 176896, 'total_tokens': 5083383}
assert len([name for name in members if name.startswith('receipts/') and name.endswith('-request.json')]) == 319
assert len([name for name in members if name.startswith('receipts/') and name.endswith('-response.json')]) == 319
coverage, scores, critical = collections.Counter(), collections.Counter(), collections.Counter()
identities = set()
for record in questions:
    for attempt in record['attempts']:
        if attempt['superseded']:
            continue
        key = (attempt['run_id'], attempt['variant'], attempt['repeat'])
        identity = (record['history_id'], record['question']['id'], *key)
        assert identity not in identities
        identities.add(identity)
        assert attempt['status'] == 'graded' and attempt['grade'] is not None
        coverage[key] += 1
        scores[key] += attempt['grade']['full_supported_answer']
        critical[key] += len(attempt['grade']['critical_errors'])
        reader = member(attempt['reader_artifact'])
        assert next(answer for answer in reader['output']['answers'] if answer['id'] == record['question']['id']) == attempt['answer']
        judge_name = attempt['reader_artifact'].replace('reader-', 'judge-')
        assert next(grade for grade in member(judge_name)['grades'] if grade['id'] == record['question']['id']) == attempt['grade']
expected = {('primary', arm, 0): 96 for arm in ['baseline', 'candidate']}
expected.update({('primary', arm, 1): 6 for arm in ['baseline', 'candidate']})
expected.update({('regeneration-1', arm, 0): 6 for arm in ['baseline', 'candidate']})
assert len(questions) == 96 and dict(coverage) == expected
assert scores['primary', 'baseline', 0] == 64 and scores['primary', 'candidate', 0] == 68
assert critical['primary', 'baseline', 0] == 1 and critical['primary', 'candidate', 0] == critical['primary', 'candidate', 1] == 0
allocations = report['paired_native_request_reuse']['allocations']
assert len(allocations) == 3
for allocation in allocations:
    scope = allocation['scope']
    call_id = allocation['source_call_id']
    source_request = json.loads(raw['receipts/' + call_id + '-request.json'])
    assert sha(helper.serialized_request(source_request)) == allocation['request_bytes_sha256']
    assert sha(raw['receipts/' + call_id + '-request.json']) == allocation['request_file_sha256']
    assert sha(raw['receipts/' + call_id + '-response.json']) == allocation['response_file_sha256']
    assert allocation['metrics']['usage'] == json.loads(raw['receipts/' + call_id + '-response.json'])['metrics']['usage']
    assert allocation['source_variant'] != allocation['recipient']['variant']
    assert scope['question_id'] == allocation['recipient']['question_id']
    history = next(item for item in fixture['histories'] if item['id'] == scope['history_id'])
    prefix = ('replicates/' + scope['run_id'] + '/' if scope['run_id'] != 'primary' else '') + allocation['recipient']['variant'] + '/' + history['id']
    context = next(item for item in member(prefix + '/contexts.json') if item['id'] == scope['question_id'])
    originals = helper.original_source_fields(history, member(prefix + '/generation.json')['source_map'])
    request = helper.typed_reader_request([context], helper.reader_citation_catalog([context], originals), protocol)
    assert helper.serialized_request(request) == helper.serialized_request(source_request)
history_ids = {history['id'] for history in fixture['histories']}
arm_tokens = {}
for arm in ['baseline', 'candidate']:
    actual = sum(row['metrics']['usage']['tokens']['total_tokens'] for row in ends.values()
                 if row.get('variant') == arm and row.get('phase') not in ['generation', 'judge']
                 and row.get('run_id', 'primary') == 'primary' and row.get('repeat', 0) == 0 and row.get('history_id') in history_ids)
    allocated = sum(item['metrics']['usage']['tokens']['total_tokens'] for item in allocations
                    if item['recipient']['variant'] == arm and item['scope']['phase'] == 'reader'
                    and item['scope']['run_id'] == 'primary' and item['scope']['repeat'] == 0 and item['scope']['history_id'] in history_ids)
    arm_tokens[arm] = actual + allocated
assert arm_tokens == report['incremental_processing_and_reader_tokens']
shared_generation = sum(row['metrics']['usage']['tokens']['total_tokens'] for row in ends.values()
                        if row.get('phase') == 'generation' and row.get('run_id', 'primary') == 'primary' and row.get('history_id') in history_ids)
assert {arm: value + shared_generation for arm, value in arm_tokens.items()} == report['equivalent_complete_arm_tokens']
public_calls = [row for row in starts.values() if row['variant'] in ['calibration', 'public-reader-probe']]
assert len(public_calls) == 8 and sum(ends[row['call_id']]['metrics']['usage']['tokens']['total_tokens'] for row in public_calls) == 136309
assert report['primary_complete'] and report['repeats_complete'] and not report['pipeline_failures'] and report['decision'] == 'fail'
assert len(report['paired_fact_losses']) == 4 and report['paired_recall_losses'] == [{'history_id': 'hold_1016', 'question_id': 'q1'}]
patterns = {'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
            'openai_key': r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}', 'aws_key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
            'jwt': r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}',
            'access_url': r'https?://[^\s<>"\\]*(?:access_token=|[?&]token=|[?&]key=|[?&]signature=)'}
privacy = {key: 0 for key in patterns}
hosts = set()
for data in members.values():
    try:
        text = data.decode()
    except UnicodeDecodeError:
        continue
    for key, pattern in patterns.items():
        privacy[key] += len(re.findall(pattern, text))
    for url in re.findall(r'https?://[^\s<>"\\]+', text):
        hosts.add(urllib.parse.urlsplit(url).netloc)
assert not any(privacy.values())
unchanged, second = helper.sanitize_export_members(members)
assert unchanged == members and second['changed_member_count'] == 0
output = {'schema_version': 1, 'status': 'passed_with_limits', 'concrete_gaps': [], 'decision_verified': 'complete_failed',
          'scope': 'Immutable R13 export plus authorized released inputs/private receipts. No model calls, regrading, repair or source changes.',
          'source_sha256': {str(path.relative_to(BASE)): sha(path.read_bytes()) for path in [BASE / 'frozen-helper.py', BASE / 'freeze.json', BASE / 'protocol.json', BASE / 'frozen-product.tar.gz', HOLDOUT / 'export-manifest.json', HOLDOUT / 'report.json', HOLDOUT / 'per-question.json', HOLDOUT / 'fictional-run-evidence.tar.gz', HOLDOUT / 'sanitization.json']},
          'integrity': {'archive_members': len(members), 'manifest_hashes_checked': len(manifest['files']), 'sanitized_member_hashes_checked': len(sanitization['changed_members']), 'native_request_response_pairs_checked': 319, 'all_profiles_efforts_protocol_helper_pins_checked': True, 'logical_reader_and_judge_matches': 216, 'frozen_product_files_checked': 47, 'current_sources_unchanged_at_audit': True, 'sealed_input_pins_checked': len(pins), 'gold_required_facts': fact_count, 'exact_support_offsets_and_provenance_checked': support_count},
          'coverage': {str(key): value for key, value in coverage.items()}, 'fully_supported': {str(key): value for key, value in scores.items()}, 'critical_errors': {str(key): value for key, value in critical.items()},
          'usage': {'actual_calls': 319, 'finished_calls': 319, 'failed_calls': 0, 'unresolved_calls': 0, 'tokens': totals, 'public_calls_included_once': 8, 'public_tokens_included_once': 136309, 'cumulative': report['cumulative_pilot_usage']},
          'reuse': {'allocations': 3, 'all_recipient_requests_reconstructed_byte_identically': True, 'all_source_receipt_fingerprints_and_usage_match': True, 'equivalent_primary_reader_tokens': arm_tokens, 'shared_generation_tokens': shared_generation, 'equivalent_complete_arm_tokens': report['equivalent_complete_arm_tokens'], 'actual_calls_counted_once': True},
          'paired_fact_losses': report['paired_fact_losses'], 'paired_recall_losses': report['paired_recall_losses'],
          'privacy': {'credential_access_url_pattern_counts': privacy, 'approved_raw_diagnostic_identifiers_remaining': 0, 'second_sanitizer_changes': 0, 'url_hosts': sorted(hosts), 'fictional_content_preserved': True},
          'limits': ['Frozen grades are preserved and matched, not independently rescored.', 'Disclosed public controls are not independent quality validation.', 'Pattern scans cannot establish absence of every possible sensitive string.', 'Failure decision remains unchanged; source integrity checks do not establish product quality.']}
path = BASE / 'export-audit.json'
assert not path.exists(), 'Refusing to overwrite an existing audit.'
path.write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps({'audit_sha256': sha(path.read_bytes()), 'archive_sha256': sha((HOLDOUT / 'fictional-run-evidence.tar.gz').read_bytes()), 'fact_supports': support_count, 'scores': output['fully_supported'], 'critical': output['critical_errors']}))
