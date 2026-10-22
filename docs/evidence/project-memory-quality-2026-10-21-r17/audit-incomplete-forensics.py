#!/usr/bin/env python3
"""Independent, offline integrity audit of adapted R17 forensic evidence.

No canonical aggregate/export/historical_accounting, model, Store API, regrade,
retry, network or frozen-source modification is performed. Isolated trial DBs
are opened with SQLite mode=ro only to verify shared snapshot logical hashes.
"""
from pathlib import Path
from decimal import Decimal
import collections
import hashlib
import importlib.util
import json
import re
import sqlite3
import tarfile
import urllib.parse
import jsonschema

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[2]
RUN = Path('/private/tmp/codex-mem-quality-20261021-r17-experiment')
OUT = BASE / 'incomplete-forensic-export'
sha = lambda data: hashlib.sha256(data).hexdigest()
read = lambda path: json.loads(path.read_bytes())
encoded = lambda value: (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def audit():
    result_path = BASE / 'incomplete-forensic-export-audit.json'
    if result_path.exists():
        raise ValueError('Refusing to overwrite an existing forensic audit.')
    checks, gaps = {}, []
    def check(condition, name):
        checks[name] = bool(condition)
        if not condition:
            gaps.append(name)
    def archive(path):
        with tarfile.open(path) as handle:
            return {item.name: handle.extractfile(item).read() for item in handle.getmembers() if item.isfile()}
    def readonly_state_hash(data):
        connection = sqlite3.connect('file:' + str(data / 'memory.sqlite3') + '?mode=ro', uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            return helper.digest(list(connection.iterdump()))
        finally:
            connection.close()
    spec = importlib.util.spec_from_file_location('r17_unchanged_forensic_audit_helper', BASE / 'frozen-helper.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper_hash = sha((BASE / 'frozen-helper.py').read_bytes())
    check(helper_hash == 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388', 'frozen_helper')
    exporter = BASE / 'export-incomplete-forensics.py'
    check(sha(exporter.read_bytes()) == 'a0922bcdd2fc3c2eb754177d3aa7c04e69eff910afefb48e97f54a72ae11924a', 'adapted_exporter_pin')
    manifest = read(OUT / 'export-manifest.json')
    accounting = read(BASE / 'accounting.json')
    summary = read(BASE / 'result-summary.json')
    sanitization = read(OUT / 'sanitization.json')
    provenance = read(OUT / 'exporter-provenance.json')
    check(sha((OUT / 'export-manifest.json').read_bytes()) == '7d6d6278be3a6eec7291b79499b15ae47f2fa47c60aadc4827328b91ccb783be', 'forensic_manifest_pin')
    check(sha((BASE / 'accounting.json').read_bytes()) == '0cf5daff3600acc46e780a0d5f06481da7b8eeac1f3b9519b1a653df677c0b7d', 'accounting_pin')
    check(sha((BASE / 'result-summary.json').read_bytes()) == '26e1ca97752540f3cc63a856d0e0f6dccff9147ea5b548278fed7f480059547c', 'summary_pin')
    members = archive(OUT / 'forensic-evidence.tar.gz')
    check(sha((OUT / 'forensic-evidence.tar.gz').read_bytes()) == manifest['archive_sha256'] == '4a767f7180e6c7bc29669007713a9a95ee0ed6fbfb11e8ff20a3e382d418c4af', 'forensic_archive_pin')
    check(len(members) == manifest['archive_member_count'], 'forensic_archive_count')
    for name, expected in manifest['files'].items():
        check(sha((OUT / name).read_bytes()) == expected, 'forensic_file:' + name)
    for name, value in manifest['members'].items():
        check(sha(members[name]) == value['public_sha256'] and len(members[name]) == value['bytes'], 'forensic_member:' + name)
        check((OUT / 'members' / name).read_bytes() == members[name], 'expanded_member:' + name)
    protocol = read(BASE / 'protocol.json')
    freeze = read(BASE / 'actual-freeze.json')
    check(sha((BASE / 'protocol.json').read_bytes()) == '4db8d98ff0e88b78253a4b991925613c1fdf7196abf2e5c0eb2a46e123d2415b', 'protocol_raw')
    check(helper.digest(protocol) == '961820bf89218870b20b169f77eb8954e7c0708d09beab6dbc1cb69bfc9a724c', 'protocol_semantic')
    check(set(protocol['historical_spend']) == {'path', 'sha256'}, 'frozen_incompatible_pointer_unmodified')
    check(sha((BASE / 'actual-freeze.json').read_bytes()) == '1fbd7c58beecc5bf60a67e4b7ee6785ebc83b8c4afe8e0be158df6937bd67a1c', 'freeze_pin')
    pin_groups = {'public': protocol['public_preflight']['file_sha256'], 'preparation': protocol['protocol_preparation']['input_pins']}
    for group, pins in pin_groups.items():
        for name, expected in pins.items():
            path = Path(name) if Path(name).is_absolute() else REPO / name
            check(sha(path.read_bytes()) == expected, group + '_pin:' + name)
    for name, binding in protocol['protocol_preparation'].get('final_gate_bindings', {}).items():
        path = Path(binding['path'])
        if not path.is_absolute():
            path = REPO / path
        check(sha(path.read_bytes()) == binding['sha256'], 'review_binding:' + name)
    builder_input = protocol['protocol_preparation']['builder_inputs']
    check(sha(Path(builder_input['path']).read_bytes()) == builder_input['sha256'], 'builder_inputs_pin')
    product = archive(BASE / 'frozen-product.tar.gz')
    check(sha((BASE / 'frozen-product.tar.gz').read_bytes()) == '784fd3a517591c8d520f421a86c3ddde322e0698077cf1973f9cb586f7924184', 'product_archive')
    check(len(product) == len(freeze['candidate_hashes']) == 48, 'candidate_module_count')
    for name, expected in freeze['candidate_hashes'].items():
        check(sha(product[name]) == expected == sha((REPO / name).read_bytes()), 'candidate_module:' + name)
    check(len(protocol['baseline_runtime_hashes']) == 47, 'baseline_module_count')
    for name, expected in protocol['baseline_runtime_hashes'].items():
        check(sha((Path(protocol['baseline_runtime_root']) / name).read_bytes()) == expected, 'baseline_module:' + name)
    author_manifest = read(BASE / 'released-author-evidence-manifest.json')
    author_path = BASE / 'released-author-evidence.tar.gz'
    author = archive(author_path)
    check(sha(author_path.read_bytes()) == author_manifest['archive_sha256'], 'author_archive')
    for name, entry in author_manifest['members'].items():
        expected = entry.get('sha256') if isinstance(entry, dict) else entry
        check(sha(author[name]) == expected, 'author_member:' + name)
    release = read(BASE / 'author-formal-release.json')
    check(sha((BASE / 'author-formal-release.json').read_bytes()) == '298c83442f7346a03eb8bb3af05c9cb162803fad509a9813cfa67a36c31bd216', 'formal_release')
    input_paths = read(RUN / 'inputs.json')
    input_by_name = {Path(name).name: Path(name) for name in input_paths}
    for name, expected in input_paths.items():
        check(sha(Path(name).read_bytes()) == expected, 'raw_input_pin:' + Path(name).name)
    fixture = json.loads(members['inputs/holdout-fixture.json'])
    gold = json.loads(members['inputs/holdout-gold.json'])
    for name in ['holdout-fixture.json', 'holdout-gold.json']:
        check(members['inputs/' + name] == author[name] == input_by_name[name].read_bytes(), 'released_input_identity:' + name)
    fact_count = support_count = 0
    for history in fixture['histories']:
        events = {event['id']: event for session in history['sessions'] for event in session['events']}
        for question in history['questions']:
            check(isinstance(question.get('question'), str) and bool(question['question'].strip()), 'prompt_key:' + history['id'] + '/' + question['id'])
            for fact in gold['questions'][history['id'] + '/' + question['id']]['required_facts']:
                fact_count += 1
                for support in fact['support']:
                    event = events[support['event_id']]
                    text = event.get('text', '')
                    field = support.get('source_field', 'body')
                    if field != 'body':
                        container, key = field.split('.', 1)
                        text = event['tool_capture'][container][key]
                    check(text[support['start']:support['end']] == support['quote'], 'anchor:' + str(support_count))
                    support_count += 1
    check(fact_count == 352 and support_count == 368, 'author_fact_anchor_counts')
    raw_direct = {}
    for name, entry in manifest['members'].items():
        if name.startswith('run/'):
            source = RUN / name[4:]
        elif name.startswith('provenance/'):
            source = BASE / name[11:]
        elif name.startswith('inputs/'):
            source = input_by_name[Path(name).name]
        else:
            continue
        raw_direct[name] = source.read_bytes()
        check(sha(raw_direct[name]) == entry['private_original_sha256'], 'raw_direct_source:' + name)
    # Generated records are independently reconstructed from fixture, source map,
    # and actual reader artifacts rather than accepted as forensic score output.
    records = []
    stage_counts = collections.Counter()
    logical_answers = 0
    for history in fixture['histories']:
        hid = history['id']
        for question in history['questions']:
            record = {'history_id': hid, 'question': question, 'gold': gold['questions'][hid + '/' + question['id']], 'attempts': []}
            planned = [('primary', 0)]
            if hid in protocol['reader_repeat_history_ids']:
                planned.append(('primary', 1))
            if hid in protocol['regeneration_repeat_history_ids']:
                planned.append(('regeneration-1', 0))
            for run_id, repeat in planned:
                root = RUN if run_id == 'primary' else RUN / 'replicates/regeneration-1'
                for variant in ['baseline', 'candidate']:
                    directory = root / variant / hid
                    gen = read(directory / 'generation.json') if (directory / 'generation.json').exists() else None
                    mapping = (gen or {}).get('source_map', {})
                    links = [{'event_id': event['id'], 'source_id': mapping.get(event['id']), 'session_id': session['id'], 'project_key': session.get('project_key'), 'source_role': event['type'], 'text': event.get('text', '')} for session in history['sessions'] for event in session['events']]
                    context = None
                    if (directory / 'contexts.json').exists():
                        context = next((item for item in read(directory / 'contexts.json') if item['id'] == question['id']), None)
                    attempt = {'run_id': run_id, 'repeat': repeat, 'variant': variant, 'answer': None, 'grade': None, 'context': context, 'source_links': links, 'status': 'missing_answer_and_grade', 'generation_metadata_exists': gen is not None}
                    matches = []
                    for path in sorted(directory.glob('**/reader-*.json')):
                        payload = read(path)
                        if 'output' not in payload or helper.reader_artifact_repeat(path, payload) != repeat:
                            continue
                        if payload.get('question_id') and payload['question_id'] != question['id']:
                            continue
                        if payload.get('question_id') and (path.parent / f'reader-{repeat}.json').exists():
                            continue
                        answer = next((item for item in payload['output']['answers'] if item['id'] == question['id']), None)
                        if answer is not None:
                            matches.append((path, payload, answer))
                    check(len(matches) <= 1, 'unique_logical_reader:' + str((hid, question['id'], run_id, variant, repeat)))
                    if matches:
                        path, payload, answer = matches[0]
                        check(not path.with_name(path.name.replace('reader-', 'judge-')).exists(), 'no_main_judge:' + str(path.relative_to(RUN)))
                        attempt.update(answer=answer, status='answer_present_ungraded', reader_artifact=str(path.relative_to(RUN)), reader_shared_metrics=payload.get('metrics'), reader_shared_latency_ms=payload.get('latency_ms'))
                        logical_answers += 1
                    stage_counts[variant, run_id, repeat, attempt['status']] += 1
                    record['attempts'].append(attempt)
            records.append(record)
    reconstructed = {'schema_version': 1, 'scope': 'Forensic observations only. Not canonical helper output. Missing/ungraded slots do not constitute a quality score.', 'questions': records}
    check(encoded(reconstructed) == members['per-question.json'], 'all_per_question_records_reproduced_from_private_sources')
    check(len(records) == 96 and sum(len(record['attempts']) for record in records) == 216 and logical_answers == 72, '96_records_216_slots_72_ungraded_answers')
    check(all(attempt['grade'] is None for record in records for attempt in record['attempts']), 'all_main_grades_absent')
    for record in records:
        history = next(item for item in fixture['histories'] if item['id'] == record['history_id'])
        for attempt in record['attempts']:
            if attempt['answer'] is None:
                continue
            directory = RUN / attempt['variant'] / record['history_id']
            generation = read(directory / 'generation.json')
            context = attempt['context']
            originals = helper.original_source_fields(history, generation['source_map'])
            catalog = helper.reader_citation_catalog([context], originals)
            part = read(directory / ('reader-' + str(attempt['repeat']) + '.q-' + record['question']['id'] + '.json'))
            call_id = part['native_receipt']['source_call_id']
            response = read(RUN / 'receipts' / (call_id + '-response.json'))
            request = read(RUN / 'receipts' / (call_id + '-request.json'))
            expected_request = helper.typed_reader_request([context], catalog, protocol)
            check(helper.serialized_request(expected_request) == helper.serialized_request(request), 'logical_exact_native_request:' + str((record['history_id'], record['question']['id'], attempt['repeat'])))
            resolved = helper.resolve_reader_output(response['output'], [context], catalog)
            check(resolved == part['output'] and resolved['answers'] == [attempt['answer']], 'logical_raw_response_resolution:' + str((record['history_id'], record['question']['id'], attempt['repeat'])))
    check(stage_counts['baseline', 'primary', 0, 'answer_present_ungraded'] == 66 and stage_counts['baseline', 'primary', 1, 'answer_present_ungraded'] == 6, 'baseline_66_primary_6_reader_repeat')
    check(not any(key[0] == 'candidate' and key[3] == 'answer_present_ungraded' for key in stage_counts), 'candidate_unstarted_answers')
    raw_direct['per-question.json'] = encoded(reconstructed)
    raw_direct['canonical-report-unavailable.json'] = encoded({'canonical_output_exists': False, 'reason': summary['reason'], 'forensic_export_is_not_canonical_result': True, 'quality_score': None})
    check(len(raw_direct) == len(members), 'every_original_member_reconstructed')
    reproduced, reproduced_sanitization = helper.sanitize_export_members(raw_direct)
    for name, data in reproduced.items():
        check(data == members[name], 'unchanged_sanitizer_reproduction:' + name)
    check(helper.export_archive_bytes(reproduced) == (OUT / 'forensic-evidence.tar.gz').read_bytes(), 'deterministic_archive_reproduction')
    check(sha(helper.export_archive_bytes(raw_direct)) == sanitization['private_original_archive_sha256'], 'private_original_archive_reproduction')
    ledger = [json.loads(line) for line in members['run/calls.jsonl'].splitlines()]
    starts = {row['call_id']: row for row in ledger if row['status'] == 'started'}
    ends = {row['call_id']: row for row in ledger if row['status'] in {'completed', 'failed'}}
    check(len(ledger) == 304 and len(starts) == len(ends) == 152 and all(row['status'] == 'completed' for row in ends.values()), '152_finished_native_attempts')
    token_sums = collections.Counter()
    phases = collections.Counter()
    reader_sizes = []
    schema_checks = 0
    for call_id, attempt in starts.items():
        request = json.loads(raw_direct['run/receipts/' + call_id + '-request.json'])
        response = json.loads(raw_direct['run/receipts/' + call_id + '-response.json'])
        check(helper.digest(request) == attempt['request_sha256'], 'request_digest:' + call_id)
        profile = helper.phase_profile(protocol, attempt['phase'])
        check(request['model'] == attempt['model'] == profile['model'] and request['reasoning_effort'] == attempt['reasoning_effort'] == profile['reasoning_effort'], 'requested_profile:' + call_id)
        check(attempt['protocol_sha256'] == helper.digest(protocol) and attempt['helper_sha256'] == helper_hash, 'attempt_pins:' + call_id)
        check(response['metrics'] == ends[call_id]['metrics'], 'native_metrics:' + call_id)
        try:
            helper.validate_native_profile(response, profile)
            jsonschema.Draft202012Validator(request['output_schema']).validate(response['output'])
            schema_checks += 1
        except (ValueError, jsonschema.ValidationError) as error:
            check(False, 'native_profile_schema:' + call_id + ':' + type(error).__name__)
        check(ends[call_id]['metrics']['usage']['status'] == 'reported', 'known_usage:' + call_id)
        token_sums.update({key: ends[call_id]['metrics']['usage']['tokens'][key] for key in ['input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens']})
        phases[attempt['phase'], attempt['variant'], attempt['model']] += 1
        if attempt['phase'] == 'reader':
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            check(set(payload) == {'contexts'} and len(payload['contexts']) == 1, 'reader_no_hidden_gold_history_or_neighbor_pool:' + call_id)
            context = payload['contexts'][0]
            reader_sizes.append(len(context['memory']))
            check(len(context['memory']) <= protocol['context_chars'], 'annotated_reader_cap:' + call_id)
        elif attempt['phase'] == 'generation':
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            check(not ({'gold', 'questions', 'answers'} & set(payload)), 'producer_no_gold_questions_answers:' + call_id)
    check(dict(token_sums) == {'input_tokens': 2055885, 'output_tokens': 73707, 'cached_input_tokens': 0, 'total_tokens': 2129592}, 'actual_native_token_recount')
    check(len([name for name in members if name.startswith('run/receipts/') and name.endswith('-request.json')]) == 152 and len([name for name in members if name.startswith('run/receipts/') and name.endswith('-response.json')]) == 152, 'all_native_request_response_pairs_exported')
    public = [call_id for call_id, row in starts.items() if row['variant'] in ['calibration', 'public-reader-probe']]
    check(len(public) == 8 and sum(ends[call_id]['metrics']['usage']['tokens']['total_tokens'] for call_id in public) == 141210, 'public_8_calls_141210_tokens_included_once')
    prior_path = REPO / protocol['historical_spend']['path']
    check(sha(prior_path.read_bytes()) == protocol['historical_spend']['sha256'], 'historical_path_sha_integrity_not_canonical_consumption')
    prior = read(prior_path)['usage']
    check(prior['attempted_calls'] == 2671 and prior['tokens']['total_tokens'] == 43403007, 'historical_through_r16')
    check(accounting['usage']['attempted_calls'] == 152 and accounting['usage']['tokens']['total_tokens'] == token_sums['total_tokens'], 'forensic_current_accounting')
    check(accounting['cumulative_usage']['attempted_calls'] == 2823 and accounting['cumulative_usage']['tokens']['total_tokens'] == 45532599, 'cumulative_independent_arithmetic')
    for key in token_sums:
        check(accounting['cumulative_usage']['tokens'][key] == prior['tokens'][key] + token_sums[key], 'cumulative_token:' + key)
    for tier in ['standard', 'fast']:
        check(Decimal(accounting['cumulative_usage']['hypothetical_api_equivalent_usd'][tier]) == Decimal(prior['hypothetical_api_equivalent_usd'][tier]) + Decimal(accounting['usage']['hypothetical_api_equivalent_usd'][tier]), 'hypothetical_scenario_arithmetic:' + tier)
    shared_snapshots = 0
    for history in fixture['histories']:
        hid = history['id']
        baseline = read(RUN / 'baseline' / hid / 'generation.json')
        candidate = read(RUN / 'candidate' / hid / 'generation.json')
        consolidation = read(RUN / 'candidate' / hid / 'consolidation.json')
        clone_audit = read(RUN / 'candidate' / hid / 'consolidation-audit.json')
        state = baseline['state_sha256']
        check(baseline['status'] == candidate['status'] == consolidation['status'] == 'complete', 'shared_generation_complete:' + hid)
        check(baseline['runtime_hashes'] == freeze['candidate_hashes'] and baseline['shared_generation_runtime'] == 'candidate', 'shared_producer_runtime:' + hid)
        check(candidate['cloned_baseline_state_sha256'] == candidate['state_sha256'] == consolidation['cloned_baseline_state_sha256'] == consolidation['state_sha256'] == state, 'shared_clone_recorded_hashes:' + hid)
        check(consolidation['candidate_processing_mode'] == 'read_only_snapshot' and not consolidation['jobs'] and consolidation['accepted_merge_count'] == 0 and consolidation['before'] == consolidation['after'], 'candidate_no_processing:' + hid)
        check(not clone_audit['records'], 'empty_no_merge_audit_records:' + hid)
        check(readonly_state_hash(RUN / 'baseline' / hid / 'data') == state and readonly_state_hash(RUN / 'candidate' / hid / 'data') == state, 'shared_readonly_logical_db_identity:' + hid)
        for path in (RUN / 'baseline' / hid).glob('reader-*.json'):
            if re.fullmatch(r'reader-[0-9]+\.json', path.name):
                check(read(path)['state_sha256'] == state, 'reader_store_state:' + str(path.relative_to(RUN)))
        shared_snapshots += 1
    stop = read(BASE / 'external-stop.json')
    check(stop['original_clock'] == read(RUN / 'run.json')['started_at'] == 1792627001.5524879, 'original_clock_identity')
    check(not stop['active_native_call_terminated'] and stop['unresolved_native_calls'] == 0 and stop['current_phase']['child_exited'] and stop['native_calls'] == 152, 'phase_drained_external_stop_receipt')
    check(stop['current_phase']['parent_finish_journal_missing_due_to_intentional_suspension'], 'missing_finish_journal_explained_not_hidden')
    check(summary['status'] == 'incomplete_frozen_protocol_consumer_mismatch' and summary['quality_score'] is None and summary['quality_rate'] is None and summary['gain'] is None and not summary['canonical_report_and_export_available'] and not summary['scored_quality_failure'], 'no_canonical_or_scored_quality_claim')
    check(not (RUN / 'holdout-report.json').exists() and not (BASE / 'holdout' / 'report.json').exists(), 'canonical_report_absence')
    check(provenance['canonical_functions_never_called'] == ['aggregate', 'export_evidence', 'historical_accounting'], 'exporter_declared_noncanonical_boundary')
    patterns = {'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', 'openai_key': r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}', 'aws_key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', 'jwt': r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}', 'access_url': r'https?://[^\s<>"\\]*(?:access_token=|[?&]token=|[?&]key=|[?&]signature=)'}
    privacy = collections.Counter({key: 0 for key in patterns})
    hosts = set()
    for data in list(members.values()) + list(author.values()):
        try:
            text = data.decode()
        except UnicodeDecodeError:
            continue
        for key, pattern in patterns.items():
            privacy[key] += len(re.findall(pattern, text))
        for url in re.findall(r'https?://[^\s<>"\\]+', text):
            hosts.add(urllib.parse.urlsplit(url).netloc)
    check(not any(privacy.values()), 'credential_access_url_scan')
    _, second_sanitization = helper.sanitize_export_members(members)
    check(second_sanitization['changed_member_count'] == 0, 'approved_native_diagnostic_ids_removed')
    check(sha((BASE / 'audit-final-export.py').read_bytes()) == '88cc066361c6722aa06b37289c927d5f810a5655caf4315538bf280b5048b32e', 'prior_final_audit_script_unchanged')
    audit_result = {'schema_version': 1, 'status': 'forensic_integrity_passed_with_limits' if not gaps else 'forensic_integrity_gaps_found', 'gaps': gaps, 'checks': checks, 'check_count': len(checks), 'scope': 'Adapted forensic evidence integrity only; no canonical quality result, regrade, native/network calls, Store API mutation or frozen edits.', 'source_sha256': {str(path.relative_to(BASE)): sha(path.read_bytes()) for path in [exporter, OUT / 'export-manifest.json', OUT / 'forensic-evidence.tar.gz', OUT / 'sanitization.json', BASE / 'accounting.json', BASE / 'result-summary.json', BASE / 'external-stop.json', BASE / 'historical-pointer-contract-audit.json', BASE / 'author-formal-release.json', author_path]}, 'archive': {'members': len(members), 'every_original_private_member_reconstructed': len(raw_direct), 'deterministic_sanitizer_and_archive_reproduced': not any('reproduction' in gap for gap in gaps)}, 'pins': {'public_count': len(pin_groups['public']), 'preparation_count': len(pin_groups['preparation']), 'candidate_modules': len(product), 'baseline_modules': len(protocol['baseline_runtime_hashes'])}, 'released_counts': {'histories': len(fixture['histories']), 'questions': len(records), 'facts': fact_count, 'anchors': support_count}, 'usage': {'actual_attempts': len(starts), 'finished': len(ends), 'failed': 0, 'unresolved': len(set(starts) - set(ends)), 'tokens': dict(token_sums), 'schema_and_actual_native_profile_checks': schema_checks, 'public_calls_included_once': 8, 'public_tokens_included_once': 141210, 'cumulative_calls': 2823, 'cumulative_tokens': 45532599, 'actual_billed_usd': None, 'hypothetical_api_equivalent_usd': accounting['usage']['hypothetical_api_equivalent_usd'], 'pricing_complete': False}, 'stage_counts': [{'variant': key[0], 'run_id': key[1], 'repeat': key[2], 'status': key[3], 'count': value} for key, value in sorted(stage_counts.items())], 'logical_answers': logical_answers, 'main_grades': 0, 'planned_attempt_slots': 216, 'quality_score': None, 'shared_readonly_snapshots_checked': shared_snapshots, 'max_actual_annotated_reader_memory_chars': max(reader_sizes, default=None), 'privacy': {'patterns': dict(privacy), 'url_hosts': sorted(hosts), 'second_sanitizer_changes': second_sanitization['changed_member_count']}, 'author_exposure_limit': release.get('post_sealing_exposure_limitation'), 'limits': ['Integrity pass is not a canonical report/export or quality pass. Canonical output remains unavailable under unchanged frozen bytes.', 'Main grades never ran. All 216 planned slots and 0 graded counts remain; missing/ungraded evidence is not a scored quality failure.', 'Read-only SQLite hashes verify isolated shared states but do not establish semantic quality.', 'External stop/process-drain observations are preserved receipts, not independently observed process history.', 'The F11 parent finish-journal receipt is explicitly absent due to intentional parent suspension; child completion is supported by six durable answers and completed native ledger.', 'Pattern scans do not prove absence of every possible sensitive string; actual billing is unknown and hypothetical pricing is incomplete.']}
    result_path.write_text(json.dumps(audit_result, indent=2) + '\n')
    print(json.dumps({'status': audit_result['status'], 'gaps': gaps, 'checks': len(checks), 'audit_sha256': sha(result_path.read_bytes()), 'members': len(members), 'answers': logical_answers, 'tokens': dict(token_sums)}))
    return 0 if not gaps else 1


if __name__ == '__main__':
    raise SystemExit(audit())
