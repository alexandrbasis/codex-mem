#!/usr/bin/env python3
"""Offline R20 complete-or-partial canonical export integrity/recount audit. Never dispatches or grades models.

Run only after canonical export is complete. The mandatory confirmation flag is
an operator gate, not a claim that missing/incomplete evidence passed.
"""
from pathlib import Path
import argparse
import collections
import hashlib
import importlib.util
import json
import re
import tarfile
import urllib.parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--complete-export-confirmed', action='store_true', required=True)
    parser.add_argument('--formal-release-confirmed', action='store_true', required=True)
    parser.add_argument('--binding-json', type=Path, required=True)
    parser.add_argument('--run', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r20-experiment'))
    parser.add_argument('--export', dest='export_dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    audit_base = Path(__file__).resolve().parent
    base = audit_base.parent / 'project-memory-quality-2026-10-21-r20'
    repo = base.parents[2]
    export = (args.export_dir or base / 'holdout').resolve()
    run = args.run.resolve()
    if not str(run).startswith('/private/tmp/'):
        parser.error('Run must remain under /private/tmp/')
    if args.output.exists():
        parser.error('Refusing to overwrite an existing audit.')
    binding = json.loads(args.binding_json.read_bytes())
    import jsonschema
    gaps, checks = [], {}
    sha = lambda data: hashlib.sha256(data).hexdigest()
    read = lambda path: json.loads(path.read_bytes())

    def check(condition, label):
        checks[label] = bool(condition)
        if not condition:
            gaps.append(label)

    def archive(path):
        with tarfile.open(path) as handle:
            return {item.name: handle.extractfile(item).read() for item in handle.getmembers() if item.isfile()}

    spec = importlib.util.spec_from_file_location('r18_frozen_export_audit', base / 'frozen-helper.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper_hash = sha((base / 'frozen-helper.py').read_bytes())
    protocol = read(export / 'protocol.json')
    freeze = read(run / 'freeze.json')
    report = read(export / 'report.json')
    manifest = read(export / 'export-manifest.json')
    sanitization = read(export / 'sanitization.json')
    fixture, gold = read(export / 'fixture.json'), read(export / 'gold.json')
    records = read(export / 'per-question.json')['questions']
    members = archive(export / 'fictional-run-evidence.tar.gz')
    member = lambda name: json.loads(members[name])
    check(helper_hash == binding['helper_sha256'], 'frozen_helper_pin')
    check(sha((base / 'protocol.json').read_bytes()) == binding['protocol_raw_sha256'], 'protocol_raw_pin')
    check(helper.digest(protocol) == binding['protocol_canonical_sha256'], 'protocol_canonical_pin')
    release = read(base / 'formal-release.json')
    check(sha((run / 'freeze.json').read_bytes()) == release['actual_freeze']['sha256'], 'actual_freeze_release_binding')
    check(len(members) == manifest['archived_files'], 'archive_member_count')
    for name, expected in manifest['files'].items():
        check(sha((export / name).read_bytes()) == expected, 'export_file:' + name)
    for name, hashes in sanitization['changed_members'].items():
        check(sha(members[name]) == hashes['public_export_sha256'], 'sanitized_member:' + name)
    check((export / 'experiment.py').read_bytes() == (base / 'frozen-helper.py').read_bytes(), 'export_frozen_helper_identity')
    check((export / 'protocol.json').read_bytes() == (base / 'protocol.json').read_bytes(), 'export_protocol_identity')
    pins = dict(protocol['public_preflight']['file_sha256'])
    pins.update(protocol.get('protocol_preparation', {}).get('input_pins', {}))
    for name, expected in pins.items():
        path = Path(name) if Path(name).is_absolute() else repo / name
        check(sha(path.read_bytes()) == expected, 'source_pin:' + name)
    preparation = protocol.get('protocol_preparation', {})
    for label, gate_binding in preparation.get('final_gate_bindings', {}).items():
        path = Path(gate_binding['path'])
        if not path.is_absolute():
            path = repo / path
        check(sha(path.read_bytes()) == gate_binding['sha256'], 'final_gate_binding:' + label)
    builder_inputs = preparation.get('builder_inputs')
    if builder_inputs:
        path = Path(builder_inputs['path'])
        if not path.is_absolute():
            path = repo / path
        check(sha(path.read_bytes()) == builder_inputs['sha256'], 'protocol_builder_inputs_pin')
    product = archive(base / 'frozen-product.tar.gz')
    source_archive = read(base / 'frozen-source-archive.json')
    check(sha((base / 'frozen-product.tar.gz').read_bytes()) == source_archive['archive_sha256'], 'product_archive_pin')
    check(len(product) == len(freeze['candidate_hashes']) == 48, 'candidate_runtime_file_count')
    for name, expected in freeze['candidate_hashes'].items():
        check(sha(product[name]) == expected, 'candidate_archive:' + name)
        check(sha((repo / name).read_bytes()) == expected, 'candidate_current:' + name)
    baseline = Path(protocol['baseline_runtime_root'])
    check(len(protocol['baseline_runtime_hashes']) == 47, 'baseline_runtime_file_count')
    for name, expected in protocol['baseline_runtime_hashes'].items():
        check(sha((baseline / name).read_bytes()) == expected, 'baseline_source:' + name)
    author_root = Path('/private/tmp/codex-mem-quality-20261021-r20-sealed')
    author_path = base / 'sealed-author-manifest.json'
    author = {}
    for name, expected in release['continuity']['all_private_file_hashes'].items():
        author[name] = (author_root / name).read_bytes()
        check(sha(author[name]) == expected, 'released_author_file:' + name)
    check(author['holdout-fixture.json'] == (export / 'fixture.json').read_bytes(), 'released_fixture_identity')
    check(author['holdout-gold.json'] == (export / 'gold.json').read_bytes(), 'released_gold_identity')
    check(release['protocol_canonical_sha256'] == helper.digest(protocol), 'formal_release_protocol_binding')
    source_review = read(base / 'review-source-manifest.json')
    for name, expected in source_review['owned_file_sha256'].items():
        check(sha((repo / name).read_bytes()) == expected, 'reviewed_source_file:' + name)
    for name, expected in source_review.get('standards_source_sha256', {}).items():
        check(sha((repo / name).read_bytes()) == expected, 'review_standards_file:' + name)
    fact_count = support_count = 0
    for history in fixture['histories']:
        events = {event['id']: event for session in history['sessions'] for event in session['events']}
        for question in history['questions']:
            check(isinstance(question.get('question'), str) and bool(question['question'].strip()), 'question_prompt:' + history['id'] + '/' + question['id'])
            for fact in gold['questions'][history['id'] + '/' + question['id']]['required_facts']:
                fact_count += 1
                for support in fact['support']:
                    event = events[support['event_id']]
                    field = support.get('source_field', 'body')
                    text = event.get('text', '')
                    if field != 'body':
                        container, key = field.split('.', 1)
                        text = event['tool_capture'][container][key]
                    check(text[support['start']:support['end']] == support['quote'], 'support_offset:' + history['id'] + '/' + question['id'] + '/' + str(support_count))
                    support_count += 1
    check(fact_count == release['counts']['required_facts'] and support_count == release['counts']['positive_anchors'], 'released_fact_support_counts')
    check(members['calls.jsonl'] == (run / 'calls.jsonl').read_bytes(), 'ledger_private_identity')
    ledger = [json.loads(line) for line in members['calls.jsonl'].splitlines()]
    starts = {row['call_id']: row for row in ledger if row['status'] == 'started'}
    finishes = {row['call_id']: row for row in ledger if row['status'] in {'completed', 'failed'}}
    check(len(starts) == sum(row['status'] == 'started' for row in ledger), 'unique_native_start_ids')
    check(len(finishes) == sum(row['status'] in {'completed', 'failed'} for row in ledger), 'unique_native_finish_ids')
    check(set(starts) == set(finishes), 'all_native_attempts_finished')
    raw, phase_profiles = {}, collections.Counter()
    token_sums = collections.Counter()
    unknown_usage_calls = []
    actual_profile_unavailable_calls = []
    failed = sum(row['status'] == 'failed' for row in finishes.values())
    native_schema_checks = 0
    memory_sizes = []
    for call_id, attempt in starts.items():
        name = 'receipts/' + call_id + '-request.json'
        raw[name] = (run / name).read_bytes()
        request = json.loads(raw[name])
        check(helper.digest(request) == attempt['request_sha256'], 'request_digest:' + call_id)
        profile = helper.phase_profile(protocol, attempt['phase'])
        check(request['model'] == attempt['model'] == profile['model'] and request['reasoning_effort'] == attempt['reasoning_effort'] == profile['reasoning_effort'], 'request_profile:' + call_id)
        check(attempt['helper_sha256'] == helper_hash and attempt['protocol_sha256'] == helper.digest(protocol), 'attempt_pins:' + call_id)
        phase_profiles[attempt['phase'], attempt['model'], attempt['reasoning_effort']] += 1
        response_name = 'receipts/' + call_id + '-response.json'
        if response_name in members:
            raw[response_name] = (run / response_name).read_bytes()
            response = json.loads(raw[response_name])
            check(response.get('metrics') == finishes[call_id].get('metrics'), 'response_metrics:' + call_id)
            try:
                helper.validate_native_profile(response, profile)
                jsonschema.Draft202012Validator(request['output_schema']).validate(response['output'])
                native_schema_checks += 1
            except (ValueError, jsonschema.ValidationError) as error:
                check(False, 'native_profile_or_schema:' + call_id + ':' + type(error).__name__)
        else:
            check(finishes[call_id]['status'] == 'failed', 'missing_response_only_on_failed_call:' + call_id)
            actual_profile_unavailable_calls.append(call_id)
        metrics = finishes[call_id].get('metrics') or {}
        usage = metrics.get('usage') or {}
        tokens = usage.get('tokens')
        if usage.get('status') == 'reported' and isinstance(tokens, dict) and all(type(tokens.get(key)) is int for key in ['input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens']):
            token_sums.update({key: tokens[key] for key in ['input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens']})
        else:
            unknown_usage_calls.append(call_id)
            check(finishes[call_id]['status'] == 'failed', 'unknown_usage_on_recorded_failure:' + call_id)
        if attempt['phase'] == 'reader':
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            check(len(payload.get('contexts', [])) == 1, 'single_question_view:' + call_id)
            for context in payload.get('contexts', []):
                memory_sizes.append(len(context['memory']))
                check(len(context['memory']) <= protocol['context_chars'], 'annotated_context_cap:' + call_id)
    cleaned, _ = helper.sanitize_export_members(raw)
    for name, data in cleaned.items():
        check(members[name] == data, 'private_public_receipt:' + name)
    check(len(starts) == report['usage']['attempted_calls'] and len(finishes) == report['usage']['finished_calls'], 'reported_call_counts')
    check(dict(token_sums) == {key: report['usage']['reported_tokens_lower_bound'][key] for key in token_sums}, 'known_reported_token_lower_bound')
    if unknown_usage_calls:
        check(not report['usage']['token_accounting_complete'] and all(value is None for value in report['usage']['tokens'].values()), 'unknown_total_tokens_remain_null_not_zero')
        check(report['usage']['calls_with_unknown_or_partial_tokens'] == len(unknown_usage_calls), 'unknown_usage_call_count')
    else:
        check(dict(token_sums) == {key: report['usage']['tokens'][key] for key in token_sums}, 'fully_known_token_totals')
    check(failed == report['usage']['failed_calls'], 'reported_native_failures')
    # Recompute only pure ledger usage/pricing; never call historical_accounting.
    import sys
    from decimal import Decimal
    sys.path.insert(0, str(repo))
    observed_usage = helper.usage(ledger)
    check(observed_usage == report['usage'], 'pure_ledger_usage_and_hypothetical_pricing_recount')
    check(report['usage'].get('cost_usd') is None, 'actual_billing_not_invented')
    historical_pointer = protocol['historical_spend']
    check(historical_pointer.get('sha256') == 'e3681ede54fdb23e4c5342b09a42f597e9494a603526981ad5e2b95d19e7056a', 'canonical_r19_historical_report_pin')
    check(set(historical_pointer) == {'report', 'sha256', 'usage_field'} and historical_pointer['usage_field'] == 'cumulative_pilot_usage', 'historical_pointer_consumer_contract')
    historical_path = repo / historical_pointer['report']
    check(sha(historical_path.read_bytes()) == historical_pointer['sha256'], 'historical_report_pin')
    from audit_contracts import selected_historical_usage
    historical_usage = selected_historical_usage(read(historical_path), historical_pointer)
    for tier in ['standard', 'fast']:
        prior_amount = historical_usage['hypothetical_api_equivalent_usd'][tier]
        current_amount = observed_usage['hypothetical_api_equivalent_usd'][tier]
        if prior_amount is not None and current_amount is not None and report['cumulative_pilot_usage']['hypothetical_api_equivalent_usd'][tier] is not None:
            check(Decimal(report['cumulative_pilot_usage']['hypothetical_api_equivalent_usd'][tier]) == Decimal(prior_amount) + Decimal(current_amount), 'cumulative_hypothetical_scenario:' + tier)
    check(read(run / 'run.json')['started_at'] == release['original_started_at'], 'original_clock_release_binding')
    coverage, grades, scores, critical = collections.Counter(), {}, collections.Counter(), collections.Counter()
    seen = set()
    for record in records:
        for attempt in record['attempts']:
            if attempt.get('superseded'):
                continue
            key = (attempt['run_id'], attempt['variant'], attempt['repeat'])
            identity = (record['history_id'], record['question']['id'], *key)
            check(identity not in seen, 'unique_logical_attempt:' + str(identity))
            seen.add(identity)
            coverage[key] += 1
            if attempt.get('answer') is not None:
                reader = member(attempt['reader_artifact'])
                check(next(answer for answer in reader['output']['answers'] if answer['id'] == record['question']['id']) == attempt['answer'], 'logical_reader:' + str(identity))
            if attempt.get('grade') is not None:
                judge_name = attempt['reader_artifact'].replace('reader-', 'judge-')
                check(next(grade for grade in member(judge_name)['grades'] if grade['id'] == record['question']['id']) == attempt['grade'], 'logical_judge:' + str(identity))
                scores[key] += attempt['grade']['full_supported_answer']
                critical[key] += len(attempt['grade']['critical_errors'])
                grades[identity] = attempt['grade']
    expected = {('primary', arm, 0): sum(len(h['questions']) for h in fixture['histories']) for arm in ['baseline', 'candidate']}
    expected.update({('primary', arm, 1): sum(len(h['questions']) for h in fixture['histories'] if h['id'] in protocol['reader_repeat_history_ids']) for arm in ['baseline', 'candidate']})
    expected.update({('regeneration-1', arm, 0): sum(len(h['questions']) for h in fixture['histories'] if h['id'] in protocol['regeneration_repeat_history_ids']) for arm in ['baseline', 'candidate']})
    check(len(records) == sum(len(h['questions']) for h in fixture['histories']) and dict(coverage) == expected, 'all_216_planned_logical_slots')
    for arm in ['baseline', 'candidate']:
        primary = [grade for identity, grade in grades.items() if identity[2:] == ('primary', arm, 0)]
        check(len(primary) == report['variants'][arm]['questions_graded'], 'primary_grade_count:' + arm)
        check(scores['primary', arm, 0] == report['variants'][arm]['full_supported_count'], 'primary_supported_recount:' + arm)
        check(critical['primary', arm, 0] == report['variants'][arm]['critical_errors'], 'primary_critical_recount:' + arm)
    losses, recall_losses = [], []
    for history in fixture['histories']:
        for question in history['questions']:
            prefix = (history['id'], question['id'], 'primary')
            baseline_grade = grades.get((*prefix, 'baseline', 0))
            candidate_grade = grades.get((*prefix, 'candidate', 0))
            if baseline_grade is None or candidate_grade is None:
                continue
            if baseline_grade['constraints_obligations_reasons_recalled'] and not candidate_grade['constraints_obligations_reasons_recalled']:
                recall_losses.append((history['id'], question['id']))
            before = {item['fact_index']: item['recalled'] for item in baseline_grade['fact_recall']}
            for item in candidate_grade['fact_recall']:
                if before.get(item['fact_index']) and not item['recalled']:
                    losses.append((history['id'], question['id'], item['fact_index']))
    check(set(losses) == {(item['history_id'], item['question_id'], item['fact_index']) for item in report['paired_fact_losses']}, 'paired_fact_loss_recount')
    check(set(recall_losses) == {(item['history_id'], item['question_id']) for item in report['paired_recall_losses']}, 'paired_recall_loss_recount')
    primary_graded = sum(identity[2] == 'primary' and identity[4] == 0 for identity in grades)
    repeat_graded = len(grades) - primary_graded
    check(report['primary_complete'] == (primary_graded == sum(value for key, value in expected.items() if key[0] == 'primary' and key[2] == 0)), 'primary_completeness_recount')
    check(report['repeats_complete'] == (repeat_graded == sum(value for key, value in expected.items() if not (key[0] == 'primary' and key[2] == 0))), 'repeat_completeness_recount')
    check(report['constraints_preserved'] == (not losses and not recall_losses), 'constraints_preserved_recount')
    import math
    gate_blockers = []
    n_primary = sum(len(h['questions']) for h in fixture['histories'])
    threshold = math.ceil(protocol['pass_criteria']['candidate_fully_supported_min'] * n_primary)
    if len(grades) != sum(expected.values()):
        gate_blockers.append('missing_graded_slots')
    if scores['primary', 'candidate', 0] < threshold:
        gate_blockers.append('candidate_supported_below_threshold')
    if any(count for key, count in critical.items() if key[1] == 'candidate'):
        gate_blockers.append('candidate_critical_errors_in_primary_or_repeats')
    if losses or recall_losses:
        gate_blockers.append('paired_individual_fact_or_constraint_recall_loss')
    if report.get('pipeline_failures'):
        gate_blockers.append('recorded_pipeline_failure')
    bscore, cscore = scores['primary', 'baseline', 0], scores['primary', 'candidate', 0]
    if bscore / n_primary < .9:
        if 100 * (cscore - bscore) / n_primary < protocol['pass_criteria']['gain_min_percentage_points_if_baseline_below_90']:
            gate_blockers.append('required_quality_gain_not_observed')
    if len(starts) > protocol['limits']['max_calls']:
        gate_blockers.append('current_call_cap_exceeded')
    if observed_usage['token_accounting_complete'] and observed_usage['tokens']['total_tokens'] > protocol['limits']['max_observed_tokens']:
        gate_blockers.append('current_token_cap_exceeded')
    if unknown_usage_calls or len(starts) != len(finishes):
        gate_blockers.append('current_usage_or_call_completion_unknown')
    check(not (report['decision'] == 'pass' and gate_blockers), 'pass_not_claimed_with_failed_gate')
    # Observe only isolated trial stores, opening SQLite read-only. Never use Store.
    import sqlite3
    db_snapshots = 0
    for history in fixture['histories']:
        hid = history['id']
        baseline_gen = run / 'baseline' / hid / 'generation.json'
        candidate_gen = run / 'candidate' / hid / 'generation.json'
        consolidation_path = run / 'candidate' / hid / 'consolidation.json'
        if not all(path.exists() for path in [baseline_gen, candidate_gen, consolidation_path]):
            continue
        before, cloned, consolidation = read(baseline_gen), read(candidate_gen), read(consolidation_path)
        state = before['state_sha256']
        check(cloned['state_sha256'] == cloned['cloned_baseline_state_sha256'] == consolidation['state_sha256'] == state, 'shared_clone_metadata:' + hid)
        check(consolidation['candidate_processing_mode'] == 'read_only_snapshot' and not consolidation['jobs'] and not consolidation['accepted_merge_count'] and consolidation['before'] == consolidation['after'], 'candidate_no_processing:' + hid)
        for arm in ['baseline', 'candidate']:
            database = run / arm / hid / 'data' / 'memory.sqlite3'
            connection = sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                actual_state = helper.digest(list(connection.iterdump()))
            finally:
                connection.close()
            check(actual_state == state, 'shared_db_readonly_identity:' + arm + '/' + hid)
        db_snapshots += 1
    check(not report['primary_complete'] or db_snapshots == len(fixture['histories']), 'complete_primary_has_all_shared_snapshots')
    allocations = report.get('paired_native_request_reuse', {}).get('allocations', [])
    for item in allocations:
        call_id, scope = item['source_call_id'], item['scope']
        request_name, response_name = 'receipts/' + call_id + '-request.json', 'receipts/' + call_id + '-response.json'
        check(sha(raw[request_name]) == item['request_file_sha256'] and sha(raw[response_name]) == item['response_file_sha256'], 'reuse_private_fingerprints:' + call_id)
        check(sha(helper.serialized_request(json.loads(raw[request_name]))) == item['request_bytes_sha256'], 'reuse_exact_request:' + call_id)
        check(item['source_variant'] != item['recipient']['variant'] and all(item['recipient'].get(key) == value for key, value in scope.items() if key in item['recipient']), 'reuse_cross_arm_scope:' + call_id)
        check(item['metrics']['usage'] == json.loads(raw[response_name])['metrics']['usage'], 'reuse_usage:' + call_id)
        if scope['phase'] == 'reader':
            history = next(history for history in fixture['histories'] if history['id'] == scope['history_id'])
            prefix = ((scope['run_id'] if scope['run_id'].startswith('replicates/') else 'replicates/' + scope['run_id']) + '/' if scope['run_id'] != 'primary' else '') + item['recipient']['variant'] + '/' + history['id']
            context = next(context for context in member(prefix + '/contexts.json') if context['id'] == scope['question_id'])
            originals = helper.original_source_fields(history, member(prefix + '/generation.json')['source_map'])
            request = helper.typed_reader_request([context], helper.reader_citation_catalog([context], originals), protocol)
            check(helper.serialized_request(request) == helper.serialized_request(json.loads(raw[request_name])), 'reuse_recipient_reconstruction:' + call_id)
    ownership_parts = ownership_aggregates = 0
    for name in sorted(members):
        if not re.search(r'/reader-[0-9]+(?:\.q-[^/]+)?\.json$', name):
            continue
        private_path = run / name
        directory = private_path.parent
        if not private_path.exists() or not (directory / 'contexts.json').exists():
            check(False, 'ownership_private_artifact_missing:' + name)
            continue
        saved = read(private_path)
        relative = directory.relative_to(run)
        root_run = run / Path(*relative.parts[:2]) if relative.parts[0] == 'replicates' else run
        hid = directory.name
        history = next(h for h in fixture['histories'] if h['id'] == hid)
        originals = helper.original_source_fields(history, read(directory / 'generation.json')['source_map'])
        contexts = {c['id']: c for c in read(directory / 'contexts.json')}
        catalogs = {qid: helper.reader_citation_catalog([c], originals) for qid,c in contexts.items()}
        try:
            if '.q-' in private_path.name:
                qid = saved['question_id']
                native = saved['native_receipt']
                response = read(run / 'receipts' / (native['source_call_id'] + '-response.json'))
                resolved = helper.resolve_reader_output(response['output'], [contexts[qid]], catalogs[qid], evidence_contract=protocol.get('reader_evidence_contract'))
                check(resolved == saved['output'], 'assertion_part_raw_resolution:' + name)
                check(helper.reader_assertion_ownership(response['output']) == saved['assertion_ownership'], 'assertion_part_intervals:' + name)
                ownership_parts += 1
            else:
                helper.validate_isolated_reader(directory, private_path, saved, contexts, catalogs, root_run, protocol)
                ownership_aggregates += 1
        except (ValueError, KeyError, TypeError) as error:
            check(False, 'assertion_ownership_binding:' + name + ':' + type(error).__name__)

    history_ids = {history['id'] for history in fixture['histories']}
    equivalent = {}
    for arm in ['baseline', 'candidate']:
        actual_tokens = sum(((row.get('metrics') or {}).get('usage') or {}).get('tokens', {}).get('total_tokens', 0) for call_id, row in finishes.items() if starts[call_id].get('variant') == arm and starts[call_id].get('phase') not in ['generation', 'judge'] and starts[call_id].get('run_id', 'primary') == 'primary' and starts[call_id].get('repeat', 0) == 0 and starts[call_id].get('history_id') in history_ids)
        reused = sum(item['metrics']['usage']['tokens']['total_tokens'] for item in allocations if item['recipient']['variant'] == arm and item['scope']['phase'] == 'reader' and item['scope']['run_id'] == 'primary' and item['scope']['repeat'] == 0)
        equivalent[arm] = actual_tokens + reused
    check(equivalent == report['incremental_processing_and_reader_tokens'], 'equivalent_arm_tokens_recount')
    public = [call_id for call_id, row in starts.items() if row.get('variant') in {'calibration', 'public-reader-probe'}]
    check(len(public) == 8 and sum(finishes[call_id]['metrics']['usage']['tokens']['total_tokens'] for call_id in public) == binding['public_tokens'], 'public_usage_included_once')
    check(report['cumulative_pilot_usage']['attempted_calls'] == historical_usage['attempted_calls'] + len(starts), 'cumulative_attempts_recount')
    from audit_contracts import cumulative_recount
    cumulative_expected = cumulative_recount(historical_usage, observed_usage)
    for field, value in cumulative_expected.items():
        check(report['cumulative_pilot_usage'].get(field) == value, 'independent_cumulative:' + field)
    known_cumulative_lower_bound = cumulative_expected['reported_tokens_lower_bound']['total_tokens']
    check(not unknown_usage_calls or report['decision'] == 'incomplete', 'current_unknown_usage_cannot_claim_completed_quality')
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
    _, repeat_sanitizer = helper.sanitize_export_members(members)
    check(repeat_sanitizer['changed_member_count'] == 0, 'no_approved_raw_native_diagnostic_ids')
    graded_stage_counts = collections.Counter((identity[2], identity[3], identity[4]) for identity in grades)
    missing_grade_counts = {key: value - graded_stage_counts[key] for key, value in expected.items()}
    failed_receipts = []
    for call_id, row in finishes.items():
        if row['status'] == 'failed':
            usage = (row.get('metrics') or {}).get('usage') or {}
            failed_receipts.append({'call_id': call_id, 'error_type': row.get('error_type'), 'error_code': row.get('error_code'), 'wall_ms': row.get('wall_ms'), 'usage_status': usage.get('status'), 'usage_updates': usage.get('updates'), 'usage_tokens': usage.get('tokens'), 'response_exists': ('receipts/' + call_id + '-response.json') in members})
    output = {'schema_version': 1, 'status': 'canonical_export_integrity_passed_with_limits' if not gaps else 'canonical_export_integrity_gaps_found', 'decision_preserved': report['decision'], 'gaps': gaps, 'check_count': len(checks), 'checks': checks, 'source_sha256': {str(path.relative_to(base)): sha(path.read_bytes()) for path in [base / 'protocol.json', base / 'actual-freeze.json', base / 'frozen-helper.py', base / 'frozen-product.tar.gz', base / 'formal-release.json', author_path, export / 'export-manifest.json', export / 'report.json', export / 'per-question.json', export / 'fictional-run-evidence.tar.gz']}, 'pins': {'combined_dynamic_source_pins': len(pins), 'candidate_files': len(product), 'baseline_files': len(protocol['baseline_runtime_hashes']), 'reviewed_source_files': len(source_review['owned_file_sha256'])}, 'released_counts': {'histories': len(fixture['histories']), 'questions': len(records), 'facts': fact_count, 'supports': support_count}, 'usage': {'actual_attempts': len(starts), 'finished': len(finishes), 'failed': failed, 'unresolved': len(set(starts) - set(finishes)), 'known_reported_token_lower_bound': dict(token_sums), 'current_total_tokens': report['usage']['tokens'], 'unknown_usage_call_ids': unknown_usage_calls, 'actual_profile_unavailable_call_ids': actual_profile_unavailable_calls, 'known_cumulative_token_lower_bound': known_cumulative_lower_bound, 'actual_cost_usd': None, 'reported_usage': report['usage'], 'cumulative_usage': report['cumulative_pilot_usage']}, 'coverage': {str(key): value for key, value in coverage.items()}, 'fully_supported': {str(key): value for key, value in scores.items()}, 'critical_errors': {str(key): value for key, value in critical.items()}, 'graded_logical_attempts': len(grades), 'graded_stage_counts': {str(key): value for key, value in graded_stage_counts.items()}, 'missing_grade_stage_counts': {str(key): value for key, value in missing_grade_counts.items()}, 'observed_critical_errors_only': True, 'assertion_part_bindings_checked': ownership_parts, 'assertion_aggregate_bindings_checked': ownership_aggregates, 'ownership_limit': 'Structural ownership is not semantic entailment; auxiliary refs never receive answer-credit from this audit.', 'native_requests_checked': len(starts), 'native_response_schemas_actual_profiles_checked': native_schema_checks, 'failed_native_receipts': failed_receipts, 'shared_readonly_snapshot_pairs': db_snapshots, 'paired_fact_losses': losses, 'paired_recall_losses': recall_losses, 'reuse_allocations': len(allocations), 'recounted_quality_gate_blockers': gate_blockers, 'pipeline_failures_preserved': report.get('pipeline_failures'), 'declared_native_limits': protocol.get('limits'), 'token_cap_compliance': ('Current native-token cap can be checked from reported usage.' if observed_usage['token_accounting_complete'] else 'Current unknown usage prevents establishing full token compliance.'), 'equivalent_primary_arm_tokens': equivalent, 'actual_reader_memory_max_chars': max(memory_sizes, default=None), 'phase_profiles': {str(key): value for key, value in phase_profiles.items()}, 'privacy': {'pattern_counts': dict(privacy), 'url_hosts': sorted(hosts), 'second_sanitizer_changes': repeat_sanitizer['changed_member_count']}, 'author_exposure_limitation_preserved_in_release': release.get('exposure_attestation'), 'export_operation': {'mode': 'offline_integrity_only', 'native_driver_report_presence': (run / 'holdout-report.json').exists(), 'no_native_resumption_retry_or_regrade': True}, 'limits': ['Any failed call usage or actual native profile absent from receipts remains unknown, not zero.', 'Hypothetical API scenarios cover reported known usage only and exclude the failed call’s unknown usage; they are not complete total costs or actual billing.', 'Partial grades are descriptive observations under full denominators, not a completed quality outcome.', 'Mechanical identity/schema/recount only; no new model judgments, grade repair, reruns, Store mutation, source edits or network.', 'Incomplete slots and failed calls remain explicit; integrity success never asserts quality success.', 'Disclosed controls and post-sealing exposure limitations remain attached; actual billing is unknown.', 'Pattern scans cannot prove absence of every possible sensitive string.']}
    args.output.write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'status': output['status'], 'gaps': len(gaps), 'output': str(args.output), 'sha256': sha(args.output.read_bytes())}))
    return 0 if not gaps else 1


if __name__ == '__main__':
    raise SystemExit(main())
