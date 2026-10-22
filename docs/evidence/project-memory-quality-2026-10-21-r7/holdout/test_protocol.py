"""Offline checks for experiment accounting and acceptance gates."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import types
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

SCRIPT = Path(__file__).resolve().parents[1] / 'project_memory_consolidation_experiment.py'
SPEC = importlib.util.spec_from_file_location('experiment', SCRIPT)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def tokens(total=100):
    return dict(input_tokens=total-10, cached_input_tokens=0, cache_write_input_tokens=0,
                output_tokens=10, reasoning_output_tokens=0, total_tokens=total)


def receipt(status='reported', total=100):
    return {'usage': {'status':status, 'tokens':tokens(total)}}


def model_call(call_id, phase, variant, total=100, run_id='primary', repeat=0):
    base=dict(call_id=call_id,phase=phase,variant=variant,history_id='fiction-01',run_id=run_id,repeat=repeat,prompt_version=experiment.PROMPT_VERSION)
    return [{**base,'status':'started'},{**base,'status':'completed','metrics':receipt(total=total)}]


class ProtocolTests(unittest.TestCase):
    def cli_inputs(self, root):
        run, runtime = root / 'run', root / 'runtime'
        runtime.mkdir()
        (runtime / '.experiment-baseline-commit').write_text('unit-baseline')
        protocol = {'comparison_mode': 'shared-prefix', 'baseline_commit': 'unit-baseline',
                    'regeneration_repeat_history_ids': ['unit_00'], 'revision': 2}
        protocol_path = root / 'protocol.json'
        experiment.write(protocol_path, protocol)
        histories = [{'id': f'unit_{i:02d}', 'project_keys': ['local'], 'sessions': [],
                      'questions': [{'id': f'q{q}', 'question': 'What remains?'} for q in range(6)]}
                     for i in range(16)]
        development, holdout = root / 'development.json', root / 'unit-holdout.json'
        experiment.write(development, {'schema_version': 1, 'fictional': True, 'split': 'development', 'histories': histories[:4]})
        experiment.write(holdout, {'schema_version': 1, 'fictional': True, 'split': 'holdout', 'histories': histories})
        sealed = root / 'unit-manifest.json'
        experiment.write(sealed, {'files': {holdout.name: experiment.hashlib.sha256(holdout.read_bytes()).hexdigest()}})
        old = {**protocol, 'revision': 1}
        experiment.write(run / 'run.json', {'started_at': 1000, 'protocol_sha256': experiment.digest(old), 'budget_run': str(run)})
        child = run / 'replicates/regeneration-1/run.json'
        experiment.write(child, {'started_at': 1100, 'protocol_sha256': experiment.digest(old), 'budget_run': str(run)})
        return run, runtime, protocol_path, protocol, development, holdout, sealed, child

    def cli(self, phase, run, runtime, protocol_path, *arguments):
        argv = [str(SCRIPT), phase, '--run', str(run), '--runtime-root', str(runtime),
                '--protocol', str(protocol_path), *map(str, arguments)]
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            return experiment.main()

    def test_cli_revision_propagates_to_existing_replicates_before_freeze_and_regeneration(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(Path(temporary))
            old_child = child.read_text()
            old_root = (run / 'run.json').read_text()
            for row in model_call('preserved', 'reader', 'baseline'):
                experiment.append(run / 'calls.jsonl', row)
            score = run / 'baseline/original-score.json'
            experiment.write(score, {'original_score': False})
            ledger_before, score_before = (run / 'calls.jsonl').read_bytes(), score.read_bytes()
            with patch('codex_mem.processor.NativeProcessorRunner') as native:
                self.assertEqual(self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol'), 0)
                self.assertEqual(experiment.read(child)['protocol_sha256'], experiment.digest(protocol))
                self.assertEqual(experiment.read(child)['started_at'], 1100)
                self.assertEqual(experiment.read(run / 'run.json')['started_at'], 1000)
                audit = experiment.calls(run / 'protocol-revisions.jsonl')
                saved = {change['path']: change['previous_run_json'] for row in audit for change in row.get('changes', [])}
                self.assertEqual(saved['run.json'], old_root)
                self.assertEqual(saved['replicates/regeneration-1/run.json'], old_child)
                self.assertEqual(self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed), 0)
                self.assertLessEqual(audit[-1]['at'], experiment.read(run / 'freeze.json')['at'])
                with patch.object(experiment, 'generate') as generate:
                    self.assertEqual(self.cli('generate', run, runtime, path, '--fixture', holdout, '--replicate',
                        'regeneration-1', '--history', 'unit_00', '--variant', 'baseline', '--live'), 0)
                    generate.assert_called_once()
                native.assert_not_called()
            self.assertEqual((run / 'calls.jsonl').read_bytes(), ledger_before)
            self.assertEqual(score.read_bytes(), score_before)

    def test_cli_freeze_refuses_stale_child_until_explicit_prefreeze_revision(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(Path(temporary))
            root_state = experiment.read(run / 'run.json')
            root_state['protocol_sha256'] = experiment.digest(protocol)
            experiment.write(run / 'run.json', root_state)
            child_before = child.read_bytes()
            with self.assertRaisesRegex(ValueError, 'replicate.*--revise-protocol'):
                self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed)
            self.assertFalse((run / 'freeze.json').exists())
            self.assertEqual(child.read_bytes(), child_before)
            extra = run / 'replicates/regeneration-extra/run.json'
            experiment.write(extra, {**experiment.read(child), 'started_at': 1200})
            self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol')
            self.assertEqual(experiment.read(extra)['protocol_sha256'], experiment.digest(protocol))
            self.assertEqual(experiment.read(extra)['started_at'], 1200)
            self.assertEqual(self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed), 0)

    def test_cli_postfreeze_revision_never_changes_root_or_child_metadata(self):
        for stale in ('root', 'child'):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
                run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(Path(temporary))
                self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol')
                self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed)
                if stale == 'root':
                    experiment.write(path, {**protocol, 'revision': 3})
                else:
                    state = experiment.read(child)
                    state['protocol_sha256'] = '0' * 64
                    experiment.write(child, state)
                files = [run / 'run.json', child, run / 'freeze.json', run / 'protocol-revisions.jsonl', run / 'protocol-current.json']
                before = {file: file.read_bytes() for file in files}
                with patch('codex_mem.processor.NativeProcessorRunner') as native:
                    with self.assertRaisesRegex(ValueError, 'only before freeze'):
                        self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol')
                    native.assert_not_called()
                self.assertEqual({file: file.read_bytes() for file in files}, before)
                self.assertFalse((run / 'calls.jsonl').exists())

    def test_cli_metadata_revision_rejects_foreign_budget_and_symlink_paths_without_writes(self):
        for invalid in ('foreign_budget', 'child_symlink', 'replica_directory_symlink', 'atomic_temporary_symlink'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
                root = Path(temporary)
                run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(root)
                outside = root / 'unrelated'
                outside.mkdir()
                external = outside / 'run.json'
                experiment.write(external, {'preserve': 'unrelated metadata'})
                if invalid == 'foreign_budget':
                    state = experiment.read(child)
                    state['budget_run'] = str(outside)
                    experiment.write(child, state)
                elif invalid == 'child_symlink':
                    child.unlink()
                    child.symlink_to(external)
                elif invalid == 'replica_directory_symlink':
                    (run / 'replicates/unrelated').symlink_to(outside, target_is_directory=True)
                else:
                    (run / 'run.json.tmp').symlink_to(external)
                before = (run / 'run.json').read_bytes(), external.read_bytes(), child.read_bytes()
                with patch('codex_mem.processor.NativeProcessorRunner') as native:
                    with self.assertRaisesRegex(ValueError, 'budget root|symlink'):
                        self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol')
                    native.assert_not_called()
                self.assertEqual(((run / 'run.json').read_bytes(), external.read_bytes(), child.read_bytes()), before)
                self.assertFalse((run / 'protocol-revisions.jsonl').exists())
                self.assertFalse((run / 'freeze.json').exists())
                self.assertFalse((run / 'calls.jsonl').exists())

    def test_cli_missing_original_root_clock_cannot_reset_an_established_run(self):
        for artifact in ('freeze', 'ledger', 'audit', 'score', 'lock'):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
                root = Path(temporary)
                run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(root)
                child.unlink()
                child.parent.rmdir()
                child.parent.parent.rmdir()
                (run / 'run.json').unlink()
                if artifact == 'freeze':
                    experiment.write(run / 'freeze.json', {'protocol_sha256': experiment.digest(protocol), 'at': 1200})
                elif artifact == 'ledger':
                    for row in model_call('preserved', 'reader', 'baseline'):
                        experiment.append(run / 'calls.jsonl', row)
                elif artifact == 'audit':
                    experiment.append(run / 'protocol-revisions.jsonl', {'original_started_at': 1000})
                elif artifact == 'score':
                    experiment.write(run / 'baseline/original-score.json', {'full_supported_answer': False})
                else:
                    (run / 'budget.lock').touch()
                before = {str(file.relative_to(run)): file.read_bytes() for file in run.rglob('*') if file.is_file()}
                with patch('codex_mem.processor.NativeProcessorRunner') as native:
                    with self.assertRaisesRegex(ValueError, 'original.*run.json.*missing|missing.*original.*run.json'):
                        self.cli('validate', run, runtime, path, '--fixture', development, '--revise-protocol')
                    native.assert_not_called()
                after = {str(file.relative_to(run)): file.read_bytes() for file in run.rglob('*') if file.is_file()}
                self.assertEqual(after, before)
                self.assertFalse((run / 'run.json').exists())

    def test_cli_supplied_malformed_sealed_pins_fail_before_clock_or_freeze_creation(self):
        for manifest in ({'sha256': {'fixture.json': 'a' * 64}}, {'files': {}},
                         {'files': {'../fixture.json': 'a' * 64}}, {'files': {'fixture.json': 'not-a-hash'}}):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
                run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(Path(temporary))
                child.unlink(); child.parent.rmdir(); child.parent.parent.rmdir(); (run / 'run.json').unlink()
                experiment.write(sealed, manifest)
                with patch('codex_mem.processor.NativeProcessorRunner') as native:
                    with self.assertRaisesRegex(ValueError, 'sealed manifest'):
                        self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed)
                    native.assert_not_called()
                self.assertEqual(list(run.iterdir()), [])

    def test_cli_pristine_run_can_start_and_freeze_without_a_clock_reset(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            root = Path(temporary)
            run, runtime, path, protocol, development, holdout, sealed, child = self.cli_inputs(root)
            child.unlink()
            child.parent.rmdir()
            child.parent.parent.rmdir()
            (run / 'run.json').unlink()
            with patch('codex_mem.processor.NativeProcessorRunner') as native:
                self.assertEqual(self.cli('freeze', run, runtime, path, '--sealed-manifest', sealed), 0)
                first = experiment.read(run / 'run.json')
                self.assertEqual(first['protocol_sha256'], experiment.digest(protocol))
                self.assertEqual(first['budget_run'], str(run))
                self.assertEqual(self.cli('validate', run, runtime, path, '--fixture', development), 0)
                self.assertEqual(experiment.read(run / 'run.json'), first)
                native.assert_not_called()

    def test_exact_request_reuse_is_cross_arm_validated_and_charged_to_both_arms(self):
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(paired_native_request_reuse='exact-request-v1', regeneration_repeat_history_ids=[])
        request = experiment.request('Fictional identical input.', experiment.object_schema({'value': experiment.TEXT}), protocol)
        response = {'output': {'value': 'Exact response.'}, 'metrics': receipt()}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            baseline = experiment.Capture(run, protocol, 'reader', 'baseline', 'fiction-01')
            candidate = experiment.Capture(run, protocol, 'reader', 'candidate', 'fiction-01')
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = response
                self.assertEqual(baseline(request), response)
                baseline.accept_for_reuse()
                self.assertEqual(candidate(request), response)
                candidate.accept_for_reuse()
                runner.assert_called_once()
            self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 1)
            self.assertEqual(len(list((run / 'receipts').glob('*-response.json'))), 1)
            reused = experiment.native_reuse_allocations(run, protocol)
            self.assertEqual(len(reused), 1)
            self.assertEqual(reused[0]['source_call_id'], 'call-0001')
            self.assertEqual(reused[0]['recipient']['variant'], 'candidate')
            self.assertEqual(reused[0]['metrics']['usage'], response['metrics']['usage'])
            self.assertTrue(candidate.dispatch_provenance()['reused'])
            rows = [dict(id=f'q{i}', full_supported_answer=True, current_state_actionable=True,
                constraints_obligations_reasons_recalled=True, citations_supported=True, critical_errors=[],
                empty_or_evasive=False, fact_recall=[dict(fact_index=0, recalled=True)], category='constraint') for i in range(6)]
            for arm in ('baseline', 'candidate'):
                experiment.write(run / arm / 'fiction-01/judge-0.json', {'grades': rows})
                experiment.write(run / arm / 'fiction-01/contexts.json', [{'context_chars': 100}])
            report = experiment.aggregate(types.SimpleNamespace(run=run),
                {'split': 'development', 'histories': [{'id': 'fiction-01'}]}, protocol)
            self.assertEqual(report['equivalent_complete_arm_tokens'], {'baseline': 100, 'candidate': 100})
            self.assertEqual(report['usage']['attempted_calls'], 1)
            self.assertEqual(report['usage']['tokens']['total_tokens'], 100)
            self.assertEqual(report['decision'], 'no_improvement')

    def test_paired_reuse_requires_exact_bytes_and_separate_repeat_history_phase_protocol(self):
        template = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        template['paired_native_request_reuse'] = 'exact-request-v1'
        request = experiment.request('Same fictional input.', experiment.object_schema({'first': experiment.TEXT,
                                                                                      'second': experiment.TEXT}), template)
        for difference in ('not_validated', 'same_arm', 'prompt', 'schema_order', 'repeat', 'history', 'phase', 'protocol', 'run'):
            with self.subTest(difference=difference), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
                base = experiment.Capture(run, template, 'reader', 'baseline', 'fiction-01')
                protocol = dict(template)
                other_run = run
                if difference == 'protocol':
                    protocol['revision'] = 2
                if difference == 'run':
                    other_run = run / 'replicates/regeneration-1'
                    experiment.write(other_run / 'run.json', {'budget_run': str(run)})
                candidate = experiment.Capture(other_run, protocol, 'judge' if difference == 'phase' else 'reader',
                    'baseline' if difference == 'same_arm' else 'candidate',
                    'fiction-02' if difference == 'history' else 'fiction-01')
                if difference == 'repeat':
                    candidate.identity['repeat'] = 1
                changed = json.loads(json.dumps(request))
                if difference == 'prompt':
                    changed['prompt'] += ' Changed.'
                if difference == 'schema_order':
                    changed['output_schema']['properties'] = dict(reversed(list(changed['output_schema']['properties'].items())))
                with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                    runner.return_value.return_value = {'output': {'first': 'A', 'second': 'B'}, 'metrics': receipt()}
                    base(request)
                    if difference != 'not_validated':
                        base.accept_for_reuse()
                    candidate(changed)
                    self.assertEqual(runner.call_count, 2)
                self.assertFalse((run / 'native-request-reuse.jsonl').exists())

    def test_unknown_usage_and_changed_raw_receipts_cannot_seed_or_supply_reuse(self):
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol['paired_native_request_reuse'] = 'exact-request-v1'
        request = experiment.request('Fictional source.', experiment.object_schema({'value': experiment.TEXT}), protocol)
        for bad in ('unknown_usage', 'changed_receipt'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
                base = experiment.Capture(run, protocol, 'reader', 'baseline', 'fiction-01')
                candidate = experiment.Capture(run, protocol, 'reader', 'candidate', 'fiction-01')
                with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                    runner.return_value.return_value = {'output': {'value': 'Literal.'},
                        'metrics': receipt('unavailable') if bad == 'unknown_usage' else receipt()}
                    base(request)
                    base.accept_for_reuse()
                    if bad == 'unknown_usage':
                        self.assertFalse((run / 'native-request-cache.jsonl').exists())
                        with self.assertRaisesRegex(RuntimeError, 'usage_unknown'):
                            candidate(request)
                    else:
                        experiment.write(run / 'receipts/call-0001-response.json', {'output': {'value': 'Changed.'}, 'metrics': receipt()})
                        with self.assertRaisesRegex(ValueError, 'receipt changed'):
                            candidate(request)
                    runner.assert_called_once()
                    self.assertFalse((run / 'native-request-reuse.jsonl').exists())

    def test_quality_or_cost_policy_is_optional_and_does_not_allow_worse_quality(self):
        for baseline_count, candidate_count, baseline_cost, candidate_cost, policy, decision in (
                (23, 24, 100, 200, None, 'no_improvement'),
                (23, 24, 100, 200, 'quality-or-cost-v1', 'pass'),
                (24, 24, 200, 100, 'quality-or-cost-v1', 'pass'),
                (24, 23, 200, 100, 'quality-or-cost-v1', 'fail')):
            with self.subTest(policy=policy, baseline=baseline_count, candidate=candidate_count), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                experiment.write(run / 'run.json', {'budget_run': str(run)})
                histories = [{'id': f'fiction-{i:02d}'} for i in range(1, 5)]
                for arm, count in (('baseline', baseline_count), ('candidate', candidate_count)):
                    for hi, history in enumerate(histories):
                        rows = []
                        for qi in range(6):
                            success = hi * 6 + qi < count
                            rows.append(dict(id=f'q{qi}', full_supported_answer=success, current_state_actionable=True,
                                constraints_obligations_reasons_recalled=success, citations_supported=True,
                                critical_errors=[], empty_or_evasive=False,
                                fact_recall=[dict(fact_index=0, recalled=success)], category='constraint'))
                        directory = run / arm / history['id']
                        experiment.write(directory / 'judge-0.json', {'grades': rows})
                        experiment.write(directory / 'contexts.json', [{'context_chars': 100}])
                for row in model_call('baseline-reader', 'reader', 'baseline', baseline_cost) + model_call('candidate-reader', 'reader', 'candidate', candidate_cost):
                    experiment.append(run / 'calls.jsonl', row)
                protocol = {'regeneration_repeat_history_ids': []}
                if policy:
                    protocol['improvement_policy'] = policy
                report = experiment.aggregate(types.SimpleNamespace(run=run), {'split': 'development', 'histories': histories}, protocol)
                self.assertEqual(report['decision'], decision)
                self.assertEqual(report['variants']['baseline']['questions_planned'], 24)
                self.assertEqual(report['variants']['candidate']['questions_planned'], 24)

    def test_typed_reader_catalog_resolves_only_displayed_exact_source_spans(self):
        contexts = [{'id': 'q1', 'question': 'What remains?', 'memory':
            '<project-memory><source source_id="original-long-id" source_role="user_intent" start="0" end="10">'
            '<quote_excerpt>Keep logs.</quote_excerpt></source></project-memory>'}]
        originals = {'original-long-id': {'source_role': 'user_intent', 'fields': {'body': 'Keep logs. Hidden note.'}}}
        catalog = experiment.reader_citation_catalog(contexts, originals)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['quote'], 'Keep logs.')
        self.assertNotIn('Hidden note.', json.dumps(catalog))
        self.assertEqual((catalog[0]['start'], catalog[0]['end']), (0, 10))
        raw = {'answers': {'q1': {'answer': 'Keep the logs.', 'citation_refs': [catalog[0]['ref']],
            'uncertainties': [], 'evidence_check': {'accepted_results': 'None evidenced.',
                'open_work_and_reasons': 'Logs must remain.', 'source_role_distinctions': 'User instruction.'}}}}
        before = json.dumps(raw, sort_keys=True)
        resolved = experiment.resolve_reader_output(raw, contexts, catalog)
        citation = resolved['answers'][0]['citations'][0]
        self.assertEqual(citation['source_id'], 'original-long-id')
        self.assertEqual(citation['quote'], 'Keep logs.')
        self.assertEqual(citation['source_field'], 'body')
        self.assertTrue(experiment.typed_citations_visible([citation], catalog, originals))
        self.assertEqual(json.dumps(raw, sort_keys=True), before)
        raw['answers']['q1']['citation_refs'] = ['made-up-ref']
        with self.assertRaisesRegex(ValueError, 'citation ref'):
            experiment.resolve_reader_output(raw, contexts, catalog)

    def test_typed_tool_citations_use_original_capture_fields_not_canonical_prose(self):
        from xml.sax.saxutils import escape
        command = 'pytest checks.py --task QA-42'
        output = '7 passed. Hidden output tail.'
        history = {'sessions': [{'events': [{'id': 'e1', 'type': 'tool', 'text': 'Canonical capture marker.',
            'tool_capture': {'tool_name': 'Bash', 'tool_input': {'command': command},
                             'tool_response': {'exit_code': 0, 'output': output, 'stderr': 'Hidden stderr.'}}}]}]}
        originals = experiment.original_source_fields(history, {'e1': 'tool-source'})
        contexts = [{'id': 'q1', 'question': 'What ran?', 'memory': '<project-memory>'
            '<source source_id="tool-source" source_role="tool_record" source_field="tool_input.command" '
            f'start="0" end="{len(command)}" tool_use_id="capture-1"><quote_excerpt>{escape(command)}</quote_excerpt></source>'
            '<source source_id="tool-source" source_role="tool_record" source_field="tool_response.output" '
            f'start="0" end="{len(output)}" quote_truncated="True" tool_use_id="capture-1">'
            '<quote_excerpt>7 passed.</quote_excerpt></source></project-memory>'}]
        catalog = experiment.reader_citation_catalog(contexts, originals)
        self.assertEqual([entry['source_field'] for entry in catalog], ['tool_input.command', 'tool_response.output'])
        self.assertEqual(catalog[1]['end'], len('7 passed.'))
        self.assertNotIn('Hidden', json.dumps(catalog))
        citations = [{'catalog_ref': entry['ref'], **{k: v for k, v in entry.items() if k != 'ref'}} for entry in catalog]
        self.assertTrue(experiment.typed_citations_visible(citations, catalog, originals))
        self.assertFalse(experiment.citations_visible(citations, experiment.visible_source_quotes(contexts),
                                                    {'tool-source': 'Canonical capture marker.'}))
        for changes in ({'source_field': 'tool_response.stderr'}, {'quote': output, 'end': len(output)},
                        {'source_id': 'other-source'}, {'source_field': 'body'}):
            invalid = [{**citations[1], **changes}]
            self.assertFalse(experiment.typed_citations_visible(invalid, catalog, originals))
        # Even literal source text cannot authorize an unshown field.
        bad_contexts = json.loads(json.dumps(contexts))
        bad_contexts[0]['memory'] = bad_contexts[0]['memory'].replace('source_field="tool_input.command"',
                                                                    'source_field="tool_response.output"')
        with self.assertRaisesRegex(ValueError, 'original source field'):
            experiment.reader_citation_catalog(bad_contexts, originals)

    def test_typed_catalog_keeps_unshown_gaps_and_other_sources_uncitable(self):
        contexts = [{'id': 'q1', 'question': 'What is visible?', 'memory': '<project-memory>'
            '<source source_id="a" source_role="assistant_report" start="0" end="5"><quote_excerpt>FIRST</quote_excerpt></source>'
            '<source source_id="a" source_role="assistant_report" start="13" end="17"><quote_excerpt>LAST</quote_excerpt></source>'
            '</project-memory>'}]
        originals = {'a': {'source_role': 'assistant_report', 'fields': {'body': 'FIRST HIDDEN LAST'}},
                     'b': {'source_role': 'assistant_report', 'fields': {'body': 'FIRST HIDDEN LAST'}}}
        catalog = experiment.reader_citation_catalog(contexts, originals)
        self.assertEqual([entry['quote'] for entry in catalog], ['FIRST', 'LAST'])
        self.assertNotIn('HIDDEN', json.dumps(catalog))
        self.assertNotIn('b', [entry['source_id'] for entry in catalog])
        forged = {'catalog_ref': catalog[0]['ref'], **{k: v for k, v in catalog[0].items() if k != 'ref'},
                  'quote': 'FIRST HIDDEN LAST', 'end': 17}
        self.assertFalse(experiment.typed_citations_visible([forged], catalog, originals))

    def test_typed_reader_request_is_opt_in_blind_and_checklist_cannot_earn_judge_credit(self):
        contexts = [{'id': 'q1', 'question': 'What remains?', 'memory': '<project-memory/>'}]
        protocol = {'model': 'gpt-6-luna', 'reasoning_effort': 'medium',
                    'reader_citation_contract': 'visible-span-refs-v1'}
        request = experiment.typed_reader_request(contexts, [], protocol)
        schema = request['output_schema']['properties']['answers']['properties']['q1']
        self.assertEqual(next(iter(schema['properties'])), 'evidence_check')
        self.assertEqual(schema['properties']['citation_refs']['maxItems'], 0)
        payload = json.loads(request['prompt'].split('\n', 1)[1])
        self.assertEqual(set(payload), {'contexts'})
        self.assertEqual(payload['contexts'][0]['memory'], contexts[0]['memory'])
        self.assertEqual(experiment.reader_prompt_version({}), experiment.PROMPT_VERSION)
        self.assertNotEqual(experiment.reader_prompt_version(protocol), experiment.PROMPT_VERSION)
        with self.assertRaisesRegex(ValueError, 'unsupported reader'):
            experiment.reader_prompt_version({'reader_citation_contract': True})
        answer = {'id': 'q1', 'answer': 'Unknown.', 'citations': [], 'uncertainties': [],
                  'evidence_check': {'accepted_results': 'Known fact absent from actual answer.'}}
        self.assertEqual(experiment.reader_answer_for_judge(answer, protocol),
                         {'id': 'q1', 'answer': 'Unknown.', 'citations': [], 'uncertainties': []})
        self.assertEqual(experiment.reader_answer_for_judge(answer, {}), answer)
        row = {'phase': 'reader', 'prompt_version': experiment.reader_prompt_version(protocol)}
        self.assertTrue(experiment.current_prompt_receipt(row, protocol))
        self.assertFalse(experiment.current_prompt_receipt(row, {}))

    def test_witness_public_cli_reader_to_judge(self):
        self.test_typed_public_reader_and_judge_preserve_raw_receipt_and_tool_field_provenance(witness=True)

    def test_witness_public_cli_rejection_keeps_native_receipts_without_salvage(self):
        self.test_typed_public_reader_and_judge_preserve_raw_receipt_and_tool_field_provenance(witness=True, reject=True)

    def test_custom_judge_profile_public_cli_preserves_reader_profile_and_reuses_same_arm_contract(self):
        self.test_typed_public_reader_and_judge_preserve_raw_receipt_and_tool_field_provenance(witness=True, custom_judge=True)

    def test_typed_public_reader_and_judge_preserve_raw_receipt_and_tool_field_provenance(self, witness=False, reject=False, custom_judge=False):
        from codex_mem.store import Store
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(reader_citation_contract='visible-span-refs-v1', reader_repeat_history_ids=[],
                        paired_native_request_reuse='exact-request-v1', candidate_processing_mode='read_only_snapshot')
        protocol.pop('historical_spend', None)
        if witness:
            protocol['judge_evidence_contract'] = 'answer-source-witnesses-v1'
        if custom_judge:
            protocol['judge_profile'] = {'model': 'gpt-6-astra', 'reasoning_effort': 'medium'}
        event = {'id': 'execution', 'type': 'tool', 'text': 'QA-42 captured execution marker.',
                 'tool_capture': {'tool_name': 'Bash', 'tool_input': {'command': 'pytest checks.py --task QA-42'},
                                  'tool_response': {'exit_code': 0, 'output': '7 passed.'}}}
        questions = [{'id': f'q{i}', 'question': 'What execution is recorded for QA-42?', 'project_keys': ['local']}
                     for i in range(6)]
        history = {'id': 'fiction-typed', 'project_keys': ['local'], 'sessions': [{'id': 'first', 'project_key': 'local', 'events': [event]}], 'questions': questions}
        raw_outputs = []
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run = Path(temporary)
            project = run / 'project'
            project.mkdir()
            directory = run / 'baseline/fiction-typed'
            experiment.configure_store(directory / 'data', [project])
            with Store(directory / 'data') as store:
                source = experiment.capture_event(store, project, 'first', event)
            experiment.write(directory / 'generation.json', {'status': 'complete', 'source_map': {'execution': source['id']},
                'projects': {'local': str(project)}, 'runtime_hashes': experiment.code_hash(experiment.ROOT),
                'captured_history_sha256': experiment.digest(history['sessions']),
                'state_sha256': experiment.state_hash(directory / 'data')})
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run),
                'protocol_sha256': experiment.digest(protocol)})
            protocol_path, fixture_path = run / 'protocol.json', run / 'fixture.json'
            experiment.write(protocol_path, protocol)
            experiment.write(fixture_path, {'schema_version': 1, 'fictional': True, 'split': 'development',
                'histories': [history] + [{**history, 'id': 'unused-' + str(i)} for i in range(3)]})
            args = types.SimpleNamespace(run=run, variant='baseline', history=None, replicate=False,
                                         runtime_root=experiment.ROOT, gold=run / 'gold.json')
            def native_reader(request):
                self.assertEqual(request['model'], 'gpt-6-luna')
                payload = json.loads(request['prompt'].split('\n', 1)[1])
                self.assertEqual(set(payload), {'contexts'})
                refs = list(dict.fromkeys(source.get('citation_ref')
                    for context in payload['contexts']
                    for block in experiment.re.findall(r'<project-memory\b.*?</project-memory>', context['memory'], flags=experiment.re.S)
                    for source in experiment.ElementTree.fromstring(block).iter('source')
                    if source.get('source_field') in ('tool_input.command', 'tool_response.output')))
                self.assertEqual(len(refs), 2)
                raw = {'answers': {q['id']: {'evidence_check': {
                    'accepted_results': 'Execution output reports 7 passed.',
                    'open_work_and_reasons': 'Not evidenced.', 'source_role_distinctions': 'Captured execution only.'},
                    'answer': 'The captured execution reports 7 passed.', 'citation_refs': refs,
                    'uncertainties': []} for q in questions}}
                raw_outputs.append(json.loads(json.dumps(raw)))
                return {'output': raw, 'metrics': receipt()}
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.side_effect = native_reader
                if witness:
                    self.assertEqual(self.cli('read', run, experiment.ROOT, protocol_path, '--fixture', fixture_path,
                        '--history', history['id'], '--variant', 'baseline', '--live'), 0)
                else:
                    experiment.readers(args, {'histories': [history]}, protocol)
            persisted = experiment.read(directory / 'reader-0.json')
            raw_receipt = experiment.read(run / 'receipts/call-0001-response.json')
            self.assertEqual(raw_receipt['output'], raw_outputs[0])
            self.assertIsInstance(persisted['output']['answers'], list)
            self.assertNotIn('evidence_check', json.dumps(persisted['output']['answers']))
            self.assertEqual(set(persisted['evidence_checks']), {q['id'] for q in questions})
            private_catalog = experiment.read(directory / 'citation-catalog.json')
            self.assertEqual(private_catalog['source_aliases'], {'S01': source['id']})
            citation = next(c for c in persisted['output']['answers'][0]['citations'] if c['source_field'] == 'tool_input.command')
            self.assertEqual(citation['source_id'], source['id'])
            self.assertEqual(citation['tool_use_id'], 'execution')
            self.assertEqual(citation['source_role'], 'tool_record')
            self.assertEqual(citation['source_field'], 'tool_input.command')
            self.assertEqual(citation['quote'], 'pytest checks.py --task QA-42')
            experiment.write(args.gold, {'questions': {'fiction-typed/' + q['id']: {
                'required_facts': [{'text': 'Execution reports 7 passed.', 'support': []}], 'category': 'evidence'}
                for q in questions}})
            def native_judge(request):
                self.assertEqual(request['model'], 'gpt-6-astra' if custom_judge else 'gpt-6-luna')
                payload = json.loads(request['prompt'].split('\n', 1)[1])
                self.assertTrue(all('evidence_check' not in case['answer'] for case in payload['cases']))
                grade = dict(full_supported_answer=True, current_state_actionable=True,
                    constraints_obligations_reasons_recalled=True, citations_supported=True,
                    empty_or_evasive=False, critical_errors=[], fact_recall={'0': True}, rationale='Synthetic grade.')
                if witness:
                    self.assertIn('original-only', request['prompt'])
                    source = next(s for s in payload['judge_source_catalog'] if s['source_field'] == 'tool_response.output')
                    grade.update(critical_witnesses=[], fact_recall={'0': {
                        'recalled': not reject, 'answer_refs': [] if reject else ['A0001'],
                        'source_refs': [] if reject else [source['ref']],
                        'omission_reason': 'literal_omission' if reject else 'not_omitted',
                        'missing_anchor': '7 passed' if reject else ''}})
                response = {'output': {'grades': {case['id']: grade for case in payload['cases']}}, 'metrics': receipt()}
                if custom_judge:
                    response['evidence'] = {'thread_start': {'model': 'gpt-6-astra', 'reasoning_effort': 'medium',
                        'model_provider': 'openai'}, 'rerouted': False}
                return response
            adapter_factory = experiment.native_phase_adapter
            def judge_adapter(protocol, phase):
                adapter = adapter_factory(protocol, phase)
                if custom_judge and phase == 'judge':
                    adapter.NativeProcessorRunner = runner
                return adapter
            with patch('codex_mem.processor.NativeProcessorRunner') as runner, patch.object(
                    experiment, 'native_phase_adapter', side_effect=judge_adapter):
                runner.return_value.side_effect = native_judge
                if witness:
                    command = ('--fixture', fixture_path, '--gold', args.gold, '--history', history['id'], '--variant', 'baseline', '--live')
                    if reject:
                        with self.assertRaisesRegex(ValueError, 'anchor occurs'):
                            self.cli('judge', run, experiment.ROOT, protocol_path, *command)
                        raw_before = (run / 'receipts/call-0002-response.json').read_bytes()
                        with self.assertRaisesRegex(ValueError, 'no automatic retry'):
                            self.cli('judge', run, experiment.ROOT, protocol_path, *command)
                        self.assertEqual((run / 'receipts/call-0002-response.json').read_bytes(), raw_before)
                        self.assertFalse((directory / 'judge-0.json').exists())
                        rejection = experiment.read(directory / 'judge-0.rejected.json')
                        self.assertEqual(rejection['questions_accepted'], 0)
                        self.assertEqual(len(rejection['planned_question_ids']), 6)
                        runner.assert_called_once()
                        self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 2)
                        report = experiment.aggregate(types.SimpleNamespace(run=run),
                            {'split': 'development', 'histories': [history]},
                            {'regeneration_repeat_history_ids': [], 'judge_evidence_contract': 'answer-source-witnesses-v1',
                             'reader_citation_contract': 'visible-span-refs-v1'})
                        self.assertEqual(report['variants']['baseline']['questions_planned'], 6)
                        self.assertEqual(report['variants']['baseline']['questions_graded'], 0)
                        self.assertEqual(report['decision'], 'incomplete')
                        return
                    self.assertEqual(self.cli('judge', run, experiment.ROOT, protocol_path, *command), 0)
                else:
                    experiment.judges(args, {'histories': [history]}, protocol)
            self.assertTrue(all(g['citations_supported'] for g in experiment.read(directory / 'judge-0.json')['grades']))
            ledger = experiment.ledger_calls(run / 'calls.jsonl')
            self.assertEqual(ledger[0]['prompt_version'], experiment.REF_READER_PROMPT_VERSION)
            if custom_judge:
                self.assertEqual([(r['phase'], r['model']) for r in ledger if r['status'] == 'started'],
                    [('reader', 'gpt-6-luna'), ('judge', 'gpt-6-astra')])
            self.assertEqual(experiment.usage(ledger)['attempted_calls'], 2)

            experiment.clone(args, {'histories': [history]}, protocol)
            experiment.consolidate(args, {'histories': [history]}, protocol)
            args.variant = 'candidate'
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                experiment.readers(args, {'histories': [history]}, protocol)
                experiment.judges(args, {'histories': [history]}, protocol)
                runner.assert_not_called()
            candidate = experiment.read(run / 'candidate/fiction-typed/reader-0.json')
            self.assertEqual(candidate['latency_ms'], persisted['latency_ms'])
            self.assertTrue(candidate['native_dispatch']['reused'])
            self.assertEqual(candidate['output'], persisted['output'])
            self.assertEqual(len(experiment.native_reuse_allocations(run, protocol)), 2)
            self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 2)

            export_args = types.SimpleNamespace(run=run, gold=args.gold, destination=run / 'export')
            experiment.export_evidence(export_args, {'split': 'development', 'histories': [history]}, protocol)
            with tarfile.open(export_args.destination / 'fictional-run-evidence.tar.gz') as archive:
                names = archive.getnames()
                self.assertIn('native-request-cache.jsonl', names)
                self.assertIn('native-request-reuse.jsonl', names)
                self.assertEqual(len([name for name in names if name.startswith('receipts/') and name.endswith('-response.json')]), 2)

    def test_inline_reader_refs_preserve_all_visible_text_within_each_view_budget(self):
        source_id = 'a' * 32
        xml = ('<project-memory><source source_id="' + source_id +
               '" source_role="user_intent" start="0" end="10"><quote_excerpt>Keep logs.</quote_excerpt>'
               '</source></project-memory>')
        memory = xml + ' ' * (6000 - len(xml))
        contexts = [{'id': 'q1', 'question': 'What remains?', 'memory': memory},
                    {'id': 'q2', 'question': 'What is the scope?', 'memory': memory}]
        originals = {source_id: {'source_role': 'user_intent', 'fields': {'body': 'Keep logs. Hidden source tail.'}}}
        catalog = experiment.reader_citation_catalog(contexts, originals)
        request = experiment.typed_reader_request(contexts, catalog, {'model': 'gpt-6-luna',
            'reasoning_effort': 'medium', 'context_chars': 6000})
        payload = json.loads(request['prompt'].split('\n', 1)[1])
        self.assertEqual(set(payload), {'contexts'})
        for view in payload['contexts']:
            self.assertLessEqual(len(view['memory']), 6000)
            self.assertEqual(view['memory'].count('Keep logs.'), 1)
            self.assertNotIn('Hidden source tail.', view['memory'])
            self.assertNotIn(source_id, view['memory'])
            self.assertIn('source_id="S01"', view['memory'])
            self.assertIn('citation_ref="R0001"', view['memory'])
            self.assertIn('source_role="user_intent" start="0" end="10"', view['memory'])
        self.assertEqual(len(catalog), 1)
        # Very short original IDs may not pay for annotations. Reject instead of silently exceeding a view.
        tiny = [{'id': 'q1', 'question': 'What remains?', 'memory': memory.replace(source_id, 'b') + ' ' * 31}]
        small_catalog = experiment.reader_citation_catalog(tiny, {'b': originals[source_id]})
        with self.assertRaisesRegex(ValueError, 'exceeds the frozen context budget'):
            experiment.typed_reader_request(tiny, small_catalog, {'model': 'gpt-6-luna', 'reasoning_effort': 'medium'})

    def test_read_only_candidate_preparation_preserves_the_frozen_shared_prefix(self):
        from codex_mem.store import Store
        from codex_mem.project_memory import ProjectMemory
        protocol = {'comparison_mode': 'shared-prefix', 'candidate_processing_mode': 'read_only_snapshot',
                    'limits': {'jobs_per_consolidation': 1}}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            project = run / 'project'
            project.mkdir()
            baseline = run / 'baseline/fiction-readonly'
            experiment.configure_store(baseline / 'data', [project])
            with Store(baseline / 'data') as store:
                store.remember(project, 'Inert requirement', 'Keep QA-42 approval.',
                               source='hook:UserPromptSubmit', session_id='first')
                memory = ProjectMemory(store)
                memory.commit(memory.claim(project), {'episodes': [], 'claims': [], 'relations': []})
            before = experiment.state_hash(baseline / 'data')
            experiment.write(baseline / 'generation.json', {'status': 'complete', 'projects': {'local': str(project)},
                'state_sha256': before, 'persisted_state': experiment.persisted_state(baseline / 'data')})
            args = types.SimpleNamespace(run=run, history=None, runtime_root=experiment.ROOT)
            fixture = {'histories': [{'id': 'fiction-readonly', 'sessions': []}]}
            experiment.clone(args, fixture, protocol)
            candidate = run / 'candidate/fiction-readonly'
            with patch.object(experiment, 'process') as processor, patch.object(experiment, 'Capture') as capture:
                experiment.consolidate(args, fixture, protocol)
                processor.assert_not_called()
                capture.assert_not_called()
            self.assertEqual(experiment.state_hash(candidate / 'data'), before)
            self.assertEqual(experiment.state_hash(baseline / 'data'), before)
            prepared = experiment.read(candidate / 'consolidation.json')
            self.assertEqual(prepared['candidate_processing_mode'], 'read_only_snapshot')
            self.assertEqual(prepared['before'], prepared['after'])
            self.assertEqual(prepared['jobs'], [])
            self.assertEqual(prepared['accepted_merge_count'], 0)
            self.assertFalse((run / 'calls.jsonl').exists())
            with Store(candidate / 'data') as store:
                store.remember(project, 'Changed clone', 'Unexpected candidate mutation.')
            with self.assertRaisesRegex(ValueError, 'shared prefix'):
                experiment.consolidate(args, fixture, protocol)

    def test_typed_public_reader_rejects_unknown_refs_without_repair_or_retry(self):
        from codex_mem.store import Store
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(reader_citation_contract='visible-span-refs-v1', reader_repeat_history_ids=[],
                        paired_native_request_reuse='exact-request-v1')
        questions = [{'id': f'q{i}', 'question': 'What remains?', 'project_keys': ['local']} for i in range(6)]
        history = {'id': 'fiction-invalid-ref', 'sessions': [], 'questions': questions}
        raw = {'answers': {q['id']: {'evidence_check': {'accepted_results': 'Not evidenced.',
            'open_work_and_reasons': 'Not evidenced.', 'source_role_distinctions': 'Not evidenced.'},
            'answer': 'Unknown.', 'citation_refs': ['R9999'], 'uncertainties': []} for q in questions}}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            project = run / 'project'
            project.mkdir()
            directory = run / 'baseline/fiction-invalid-ref'
            experiment.configure_store(directory / 'data', [project])
            with Store(directory / 'data'):
                pass
            experiment.write(directory / 'generation.json', {'status': 'complete', 'source_map': {},
                'projects': {'local': str(project)}, 'runtime_hashes': experiment.code_hash(experiment.ROOT)})
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            args = types.SimpleNamespace(run=run, variant='baseline', history=None, replicate=False,
                                         runtime_root=experiment.ROOT)
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = {'output': raw, 'metrics': receipt()}
                with self.assertRaisesRegex(ValueError, 'citation ref'):
                    experiment.readers(args, {'histories': [history]}, protocol)
                with self.assertRaisesRegex(ValueError, 'no automatic retry'):
                    experiment.readers(args, {'histories': [history]}, protocol)
                runner.assert_called_once()
            self.assertFalse((directory / 'reader-0.json').exists())
            self.assertFalse((run / 'native-request-cache.jsonl').exists())
            self.assertEqual(experiment.read(directory / 'reader-0.rejected.json')['answers_accepted'], 0)
            self.assertEqual(experiment.read(run / 'receipts/call-0001-response.json')['output'], raw)
            self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 1)

    def test_saved_missing_fact_rejects_whole_batch_without_changing_receipt(self):
        evidence = SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20'
        with tarfile.open(evidence / 'holdout-run/fictional-run-evidence.tar.gz') as archive:
            response = json.load(archive.extractfile('receipts/call-0059-response.json'))
            request = json.load(archive.extractfile('receipts/call-0059-request.json'))
        cases = json.loads(request['prompt'].split('\n', 1)[1])['cases']
        original = json.dumps(response, sort_keys=True)
        with self.assertRaisesRegex(ValueError, 'judge fact coverage does not match frozen gold: answer-04'):
            experiment.validate_judge_grades(response['output']['grades'], cases)
        self.assertEqual(json.dumps(response, sort_keys=True), original)

    def test_witness_judge_answer_refs_resolve_exact_multisentence_spans(self):
        protocol = {'judge_evidence_contract': 'answer-source-witnesses-v1', 'model': 'gpt-6-luna', 'reasoning_effort': 'medium'}
        answer = 'The local test passed. Deployment remains pending.\n\nDo not treat that test as production approval.'
        case = {'id': 'a', 'question': 'What is verified?', 'answer': {'answer': answer},
                'gold': {'required_facts': [{'text': 'Deployment remains pending.'}], 'forbidden_inferences': []}}
        source = {'ref': 'J0001', 'source_id': 's', 'source_field': 'body', 'source_role': 'user_intent',
                  'start': 0, 'end': 27, 'quote': 'Deployment remains pending.', 'reader_visible': True}
        request = experiment.judge_request({'cases': [case], 'original_history': [],
            'shared_reader_visible_memory': [], 'judge_source_catalog': [source]}, protocol)
        payload = json.loads(request['prompt'].split('\n', 1)[1])
        catalog = payload['cases'][0]['answer_span_catalog']
        selected = next(span for span in catalog if span['quote'] == 'Deployment remains pending.')
        self.assertEqual(answer[selected['start']:selected['end']], selected['quote'])
        fact_schema = request['output_schema']['properties']['grades']['properties']['a']['properties']['fact_recall']['properties']['0']['properties']
        self.assertNotIn('answer_quotes', fact_schema)
        self.assertEqual(fact_schema['answer_refs']['items']['enum'], [span['ref'] for span in catalog])
        self.assertNotIn('Production deployment is pending.', [span['quote'] for span in catalog])
        grade = dict(full_supported_answer=True, current_state_actionable=True,
            constraints_obligations_reasons_recalled=True, citations_supported=True, empty_or_evasive=False,
            critical_errors=[], critical_witnesses=[], rationale='Synthetic exact pointer control.',
            fact_recall={'0': {'recalled': True, 'answer_refs': [selected['ref']], 'source_refs': ['J0001'],
                              'omission_reason': 'not_omitted', 'missing_anchor': ''}})
        decoded = experiment.decode_judge_output({'grades': {'a': grade}}, [case], protocol, [source])
        self.assertEqual(decoded[0]['fact_recall'][0]['answer_witnesses'], [selected])
        grade['fact_recall']['0']['answer_refs'] = ['A9999']
        with self.assertRaises(ValueError):
            experiment.decode_judge_output({'grades': {'a': grade}}, [case], protocol, [source])
        self.assertNotIn('answer_span_catalog', case)
        critical = request['output_schema']['properties']['grades']['properties']['a']['properties']['critical_witnesses']['items']['properties']
        self.assertNotIn('answer_quote', critical)
        self.assertEqual(critical['answer_ref']['enum'], [span['ref'] for span in catalog])
        grade['fact_recall']['0']['answer_refs'] = [selected['ref']]
        grade.update(full_supported_answer=False, critical_errors=['false_closure'], critical_witnesses=[{
            'error_type': 'false_closure', 'answer_ref': selected['ref'], 'assertion_mode': 'affirmative_current',
            'relation': 'asserts_forbidden_conclusion', 'prohibition_ref': 'policy:false_closure'}])
        # This boundary validates provenance, not whether a claimed semantic relation is true.
        result = experiment.decode_judge_output({'grades': {'a': grade}}, [case], protocol, [source])[0]
        self.assertEqual(result['critical_witnesses'][0]['answer_witness'], selected)
        grade['critical_witnesses'][0]['answer_ref'] = 'A9999'
        with self.assertRaises(ValueError):
            experiment.decode_judge_output({'grades': {'a': grade}}, [case], protocol, [source])
        forged = {**case, 'answer_span_catalog': [{'ref': 'FORGED', 'quote': 'Production approved.'}]}
        clean = experiment.judge_request({'cases': [forged], 'original_history': [],
            'shared_reader_visible_memory': [], 'judge_source_catalog': [source]}, protocol)
        self.assertEqual(json.loads(clean['prompt'].split('\n', 1)[1])['cases'][0]['answer_span_catalog'], catalog)

    def test_witness_judge_missing_anchor_schema_selects_only_exact_gold_spans(self):
        protocol = {'judge_evidence_contract': 'answer-source-witnesses-v1'}
        cases = [{'id': 'a', 'answer': {'answer': 'The execution result is not supplied.'},
                  'gold': {'required_facts': [{'text': 'External submissions: zero.'}], 'forbidden_inferences': []}}]
        schema = experiment.judge_schema(cases, protocol, [])
        anchor = schema['properties']['grades']['properties']['a']['properties']['fact_recall']['properties']['0']['properties']['missing_anchor']
        self.assertEqual(anchor, {'type': 'string', 'enum': ['', 'External submissions: zero.',
            'External', 'submissions', 'zero', 'External submissions', 'submissions: zero']})
        self.assertNotIn('zero external submissions', anchor['enum'])
        grade = dict(full_supported_answer=False, current_state_actionable=False,
                     constraints_obligations_reasons_recalled=False, citations_supported=False,
                     empty_or_evasive=False, critical_errors=[], critical_witnesses=[], rationale='Public omission control.',
                     fact_recall={'0': {'recalled': False, 'answer_refs': [], 'source_refs': [],
                         'omission_reason': 'literal_omission', 'missing_anchor': 'zero external submissions'}})
        with self.assertRaisesRegex(ValueError, 'missing_anchor'):
            experiment.decode_judge_output({'grades': {'a': grade}}, cases, protocol, [])
        grade['fact_recall']['0']['missing_anchor'] = 'zero'
        decoded = experiment.decode_judge_output({'grades': {'a': grade}}, cases, protocol, [])
        self.assertFalse(decoded[0]['fact_recall'][0]['recalled'])
        cases[0]['answer']['answer'] = 'The execution made ZERO external submissions.'
        with self.assertRaisesRegex(ValueError, 'anchor occurs'):
            experiment.decode_judge_output({'grades': {'a': grade}}, cases, protocol, [])
        grade['fact_recall']['0'].update(omission_reason='semantic_omission', missing_anchor='')
        self.assertFalse(experiment.decode_judge_output({'grades': {'a': grade}}, cases, protocol, [])[0]['fact_recall'][0]['recalled'])
        self.assertEqual(experiment.judge_prompt_version(protocol), 'answer-source-span-refs-v3')
        self.assertEqual(experiment.judge_prompt_version({}), 'exact-coverage-v3')

    def test_witness_judge_resolves_delivered_answer_and_canonical_sources(self):
        protocol = {'judge_evidence_contract': 'answer-source-witnesses-v1'}
        cases = [{'id': 'answer-00', 'answer': {'answer': 'Use participant codes in the review batch.'},
                  'gold': {'required_facts': [{'text': 'Use participant codes in the review batch.'}],
                           'forbidden_inferences': []}}]
        sources = [{'ref': 'J0001', 'source_id': 'user-source', 'source_field': 'body',
                    'start': 12, 'end': 29, 'source_role': 'user_intent',
                    'quote': 'participant codes', 'reader_visible': True}]
        grade = dict(full_supported_answer=True, current_state_actionable=True,
                     constraints_obligations_reasons_recalled=True, citations_supported=True,
                     empty_or_evasive=False, critical_errors=[], critical_witnesses=[], rationale='Public control.',
                     fact_recall={'0': {'recalled': True, 'answer_refs': ['A0001'],
                         'source_refs': ['J0001'], 'omission_reason': 'not_omitted', 'missing_anchor': ''}})
        output = {'grades': {'answer-00': grade}}
        decoded = experiment.decode_judge_output(output, cases, protocol, sources)
        fact = decoded[0]['fact_recall'][0]
        self.assertTrue(fact['recalled'])
        self.assertEqual(fact['answer_witnesses'], [{'ref': 'A0001', 'start': 0, 'end': 42,
            'quote': 'Use participant codes in the review batch.'}])
        self.assertEqual(fact['source_witnesses'], sources)
        self.assertNotEqual(experiment.judge_prompt_version(protocol), experiment.JUDGE_PROMPT_VERSION)
        self.assertEqual(experiment.judge_prompt_version({}), experiment.JUDGE_PROMPT_VERSION)
        inconsistent = json.loads(json.dumps(output))
        inconsistent['grades']['answer-00']['constraints_obligations_reasons_recalled'] = False
        with self.assertRaisesRegex(ValueError, 'full answer'):
            experiment.decode_judge_output(inconsistent, cases, protocol, sources)

    def test_witness_judge_rejects_literal_omission_and_unsupported_or_nonaffirmative_receipts(self):
        protocol = {'judge_evidence_contract': 'answer-source-witnesses-v1'}
        case = {'id': 'a', 'answer': {'answer': 'Use participant codes. Do not transfer dining permission. Swaps were canceled.'},
                'gold': {'required_facts': [{'text': 'Use participant codes.'}],
                         'forbidden_inferences': [{'text': 'Dining permission authorizes this task.', 'error_type': 'project_mixing'}]}}
        source = {'ref': 'J0001', 'source_id': 's', 'source_field': 'body', 'start': 0, 'end': 22,
                  'source_role': 'user_intent', 'quote': 'Use participant codes.', 'reader_visible': True}
        grade = dict(full_supported_answer=False, current_state_actionable=True,
                     constraints_obligations_reasons_recalled=False, citations_supported=True,
                     empty_or_evasive=False, critical_errors=[], critical_witnesses=[], rationale='Control.',
                     fact_recall={'0': {'recalled': False, 'answer_refs': [], 'source_refs': [],
                         'omission_reason': 'literal_omission', 'missing_anchor': 'participant codes'}})
        mutations = [lambda g: None,
            lambda g: g['fact_recall']['0'].update(recalled=True, omission_reason='not_omitted', missing_anchor=''),
            lambda g: g['fact_recall']['0'].update(missing_anchor='invented anchor'),
            lambda g: g['fact_recall']['0'].update(missing_anchor=''),
            lambda g: g['fact_recall']['0'].update(recalled=True, omission_reason='not_omitted', missing_anchor='',
                answer_refs=['A9999'], source_refs=['J0001']),
            lambda g: g['fact_recall']['0'].update(recalled=True, omission_reason='not_omitted', missing_anchor='',
                answer_refs=['A0001'], source_refs=['J9999'])]
        for mutate in mutations:
            packet = json.loads(json.dumps(grade)); mutate(packet)
            with self.assertRaises(ValueError):
                experiment.decode_judge_output({'grades': {'a': packet}}, [case], protocol, [source])
        grade['fact_recall']['0'].update(omission_reason='semantic_omission', missing_anchor='')
        for mode in ('negated', 'historical', 'quoted_report', 'conditional', 'uncertain'):
            packet = {**grade, 'critical_errors': ['project_mixing'], 'critical_witnesses': [{
                'error_type': 'project_mixing', 'answer_ref': 'A0001',
                'assertion_mode': mode, 'relation': 'asserts_forbidden_conclusion', 'prohibition_ref': 'gold:0'}]}
            with self.assertRaisesRegex(ValueError, 'affirmative'):
                experiment.decode_judge_output({'grades': {'a': packet}}, [case], protocol, [source])
        for quote, relation, prohibition in (('', 'asserts_forbidden_conclusion', 'gold:0'),
                ('Do not transfer dining permission.', 'does_not_assert', 'gold:0'),
                ('Do not transfer dining permission.', 'asserts_forbidden_conclusion', 'policy:false_merge')):
            packet = {**grade, 'critical_errors': ['project_mixing'], 'critical_witnesses': [{
                'error_type': 'project_mixing', 'answer_ref': 'A0001' if quote else '', 'assertion_mode': 'affirmative_current',
                'relation': relation, 'prohibition_ref': prohibition}]}
            with self.assertRaises(ValueError):
                experiment.decode_judge_output({'grades': {'a': packet}}, [case], protocol, [source])
        # Mechanical rejection does not pretend to establish semantic absence.
        accepted = experiment.decode_judge_output({'grades': {'a': grade}}, [case], protocol, [source])
        self.assertFalse(accepted[0]['fact_recall'][0]['recalled'])

    def test_judge_source_visibility_never_composes_gaps_or_fields(self):
        originals = {'s': {'source_role': 'tool_record', 'fields': {'body': 'alpha HIDDEN omega',
                                                                 'tool_response.output': 'alpha'}}}
        visible = [{'ref': 'R0001', 'source_id': 's', 'source_field': 'body', 'source_role': 'tool_record',
                    'start': 0, 'end': 5, 'quote': 'alpha'},
                   {'ref': 'R0002', 'source_id': 's', 'source_field': 'body', 'source_role': 'tool_record',
                    'start': 13, 'end': 18, 'quote': 'omega'}]
        catalog = experiment.judge_source_catalog(originals, visible)
        self.assertEqual([e['reader_visible'] for e in catalog], [True, True, False, False])
        self.assertEqual(catalog[2]['quote'], 'alpha HIDDEN omega')
        self.assertEqual(catalog[3]['source_field'], 'tool_response.output')

    def test_public_calibration_freeze_between_controls_blocks_the_next_native_dispatch(self):
        self.test_public_calibration_uses_original_budget_and_fixed_answers_without_hidden_labels(freeze_between=True)

    def test_public_calibration_uses_original_budget_and_fixed_answers_without_hidden_labels(self, freeze_between=False):
        path = SCRIPT.parents[1] / 'docs/evidence/project-memory-quality-2026-10-21-r3/native-judge-calibration.py'
        spec = importlib.util.spec_from_file_location('public_calibration', path)
        calibration = importlib.util.module_from_spec(spec); spec.loader.exec_module(calibration)
        controls_path = path.with_name('judge-contract-controls.json')
        controls = experiment.read(controls_path)
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(judge_evidence_contract='answer-source-witnesses-v1', reader_citation_contract='visible-span-refs-v1',
                        judge_calibration={'controls_sha256': experiment.hashlib.sha256(controls_path.read_bytes()).hexdigest(),
                                           'max_calls': 8, 'max_observed_tokens': 200000})
        protocol.pop('historical_spend', None)
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run = Path(temporary) / 'not-created'
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                plan = calibration.calibrate(experiment, run, protocol, controls_path)
                self.assertEqual(plan['status'], 'prepared_no_native_calls')
                self.assertEqual(plan['planned_native_calls'], 7)
                self.assertEqual(plan['control_count'], 6)
                self.assertFalse(run.exists()); runner.assert_not_called()
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run),
                                               'protocol_sha256': experiment.digest(protocol)})
            before = (run / 'run.json').read_bytes()
            def native(request):
                payload = json.loads(request['prompt'].split('\n', 1)[1])
                self.assertNotIn('expected', payload)
                packet_grades = {}
                for case in payload['cases']:
                    control = next(c for c in controls['controls'] if c['answer'] == case['answer']['answer'])
                    self.assertNotIn(control['id'], request['prompt'])
                    own_sources = {citation['source_id'] for citation in case['answer']['citations']}
                    source_ref = next((source['ref'] for source in payload['judge_source_catalog']
                                       if source['source_id'] in own_sources), None)
                    facts = {str(i): {'recalled': recalled,
                        'answer_refs': ['A0001'] if recalled else [],
                        'source_refs': [source_ref] if recalled else [],
                        'omission_reason': 'not_omitted' if recalled else 'semantic_omission', 'missing_anchor': ''}
                        for i, recalled in enumerate(control['expected']['fact_recall'])}
                    critical = control['expected']['critical_errors']
                    witnesses = [{'error_type': error, 'answer_ref': 'A0001',
                        'assertion_mode': 'affirmative_current', 'relation': 'asserts_forbidden_conclusion',
                        'prohibition_ref': 'policy:' + error} for error in critical]
                    packet_grades[case['id']] = {
                        'full_supported_answer': all(control['expected']['fact_recall']) and not critical,
                        'current_state_actionable': True, 'constraints_obligations_reasons_recalled': True,
                        'citations_supported': True, 'empty_or_evasive': False, 'critical_errors': critical,
                        'critical_witnesses': witnesses, 'fact_recall': facts, 'rationale': 'Public stub control.'}
                if len(payload['cases']) == 6:
                    self.assertEqual(len({case['id'] for case in payload['cases']}), 6)
                    sources = payload['judge_source_catalog']
                    self.assertEqual(len({source['source_id'] for source in sources}), 9)
                    self.assertEqual(len({source['ref'] for source in sources}), len(sources))
                return {'metrics': receipt(), 'output': {'grades': packet_grades}}
            if freeze_between:
                decode = experiment.decode_judge_output
                def freeze_after_first_contract_validation(*arguments, **keywords):
                    grades = decode(*arguments, **keywords)
                    # Freeze uses its real shared budget lock after Capture released it.
                    experiment.prepare_run_metadata(run, protocol, freeze_payload={'public_test': True})
                    return grades
                with patch('codex_mem.processor.NativeProcessorRunner') as runner, patch.object(
                        experiment, 'decode_judge_output', side_effect=freeze_after_first_contract_validation):
                    runner.return_value.side_effect = native
                    with self.assertRaisesRegex(ValueError, 'before freeze at each dispatch'):
                        calibration.calibrate(experiment, run, protocol, controls_path, live=True)
                    runner.assert_called_once()
                self.assertEqual((run / 'run.json').read_bytes(), before)
                self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 1)
                self.assertFalse((run / 'receipts/call-0002-request.json').exists())
                self.assertFalse((run / 'judge-calibration/missing-scoped-cancel.json').exists())
                with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                    with self.assertRaisesRegex(ValueError, 'before freeze'):
                        calibration.calibrate(experiment, run, protocol, controls_path, live=True)
                    runner.assert_not_called()
                return
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.side_effect = native
                result = calibration.calibrate(experiment, run, protocol, controls_path, live=True)
                self.assertEqual(result['status'], 'completed'); self.assertEqual(runner.call_count, 7)
                with self.assertRaisesRegex(ValueError, 'already attempted'):
                    calibration.calibrate(experiment, run, protocol, controls_path, live=True)
                self.assertEqual(runner.call_count, 7)
            self.assertEqual((run / 'run.json').read_bytes(), before)
            rows = experiment.ledger_calls(run / 'calls.jsonl')
            self.assertEqual(experiment.usage(rows)['attempted_calls'], 7)
            self.assertEqual({r['variant'] for r in rows}, {'calibration'})
            self.assertEqual(len(list((run / 'receipts').glob('*-response.json'))), 7)
            experiment.write(run / 'freeze.json', {})
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                with self.assertRaisesRegex(ValueError, 'before freeze'):
                    calibration.calibrate(experiment, run, protocol, controls_path, live=True)
                runner.assert_not_called()

    def test_public_calibration_rejection_and_subbudget_stop_do_not_retry(self):
        path = SCRIPT.parents[1] / 'docs/evidence/project-memory-quality-2026-10-21-r3/native-judge-calibration.py'
        spec = importlib.util.spec_from_file_location('public_calibration_reject', path)
        calibration = importlib.util.module_from_spec(spec); spec.loader.exec_module(calibration)
        controls = path.with_name('judge-contract-controls.json')
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(judge_evidence_contract='answer-source-witnesses-v1',
                        judge_calibration={'controls_sha256': experiment.hashlib.sha256(controls.read_bytes()).hexdigest(),
                                           'max_calls': 8, 'max_observed_tokens': 1})
        protocol.pop('historical_spend', None)
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run),
                                               'protocol_sha256': experiment.digest(protocol)})
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                    calibration.calibrate(experiment, run, protocol, controls, live=True)
                runner.assert_not_called()
            self.assertFalse((run / 'calls.jsonl').exists())
            protocol['judge_calibration']['max_observed_tokens'] = 200000
            experiment.write(run / 'run.json', {**experiment.read(run / 'run.json'), 'protocol_sha256': experiment.digest(protocol)})
            before = (run / 'run.json').read_bytes()
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = {'output': {'grades': {}}, 'metrics': receipt()}
                with self.assertRaisesRegex(ValueError, 'coverage'):
                    calibration.calibrate(experiment, run, protocol, controls, live=True)
                raw = (run / 'receipts/call-0001-response.json').read_bytes()
                with self.assertRaisesRegex(ValueError, 'already attempted'):
                    calibration.calibrate(experiment, run, protocol, controls, live=True)
                runner.assert_called_once()
            self.assertEqual((run / 'run.json').read_bytes(), before)
            self.assertEqual((run / 'receipts/call-0001-response.json').read_bytes(), raw)
            self.assertEqual(experiment.read(run / 'judge-calibration/codes-paraphrase.rejected.json')['questions_accepted'], 0)
            questions = [{'id': f'q{i}', 'question': 'What is visible?'} for i in range(6)]
            history = {'id': 'public-export', 'questions': questions, 'sessions': []}
            gold = run / 'public-gold.json'
            experiment.write(gold, {'questions': {'public-export/' + q['id']: {'required_facts': []} for q in questions}})
            args = types.SimpleNamespace(run=run, gold=gold, destination=run / 'export')
            experiment.export_evidence(args, {'split': 'development', 'histories': [history]}, protocol)
            with tarfile.open(args.destination / 'fictional-run-evidence.tar.gz') as archive:
                self.assertIn('judge-calibration/codes-paraphrase.rejected.json', archive.getnames())
                self.assertIn('receipts/call-0001-response.json', archive.getnames())

    def test_public_calibration_explicit_continuation_preserves_five_original_packets(self):
        path = SCRIPT.parents[1] / 'docs/evidence/project-memory-quality-2026-10-21-r3/native-judge-calibration.py'
        spec = importlib.util.spec_from_file_location('public_continuation', path)
        calibration = importlib.util.module_from_spec(spec); spec.loader.exec_module(calibration)
        old_controls_path = SCRIPT.parents[1] / 'docs/evidence/project-memory-quality-2026-10-21-r4/judge-contract-controls-v1.json'
        current_controls_path = path.with_name('judge-contract-controls.json')
        controls = experiment.read(old_controls_path)
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(judge_evidence_contract='answer-source-witnesses-v1', reader_citation_contract='visible-span-refs-v1',
            judge_calibration={'controls_sha256': calibration.file_sha256(old_controls_path),
                               'max_calls': 8, 'max_observed_tokens': 200000})
        protocol.pop('historical_spend', None)
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
            run = Path(temporary) / 'run'
            original_protocol, original_run, ack_path = (Path(temporary) / name for name in ('protocol-v1.json', 'run-v1.json', 'ack.json'))
            experiment.write(original_protocol, protocol)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run),
                                               'protocol_sha256': experiment.digest(protocol)})
            original_run.write_bytes((run / 'run.json').read_bytes())
            def native(request):
                payload = json.loads(request['prompt'].split('\n', 1)[1]); grades = {}
                for case in payload['cases']:
                    control = next(c for c in controls['controls'] if c['answer'] == case['answer']['answer'])
                    expected = control['expected']; errors = ['project_mixing'] if control['id'] == 'real-causal-conflation' else expected['critical_errors']
                    source_ids = {source['source_id'] for source in case['answer']['citations']}
                    source = next((s['ref'] for s in payload['judge_source_catalog'] if s['source_id'] in source_ids), None)
                    grades[case['id']] = {'full_supported_answer': all(expected['fact_recall']) and not errors,
                        'current_state_actionable': True, 'constraints_obligations_reasons_recalled': True,
                        'citations_supported': True, 'empty_or_evasive': False, 'critical_errors': errors,
                        'rationale': 'Synthetic unchanged semantic labels.',
                        'critical_witnesses': [{'error_type': error, 'answer_ref': 'A0001',
                            'assertion_mode': 'affirmative_current', 'relation': 'asserts_forbidden_conclusion',
                            'prohibition_ref': 'policy:' + error} for error in errors],
                        'fact_recall': {str(i): {'recalled': recalled, 'answer_refs': ['A0001'] if recalled else [],
                            'source_refs': [source] if recalled else [], 'missing_anchor': '',
                            'omission_reason': 'not_omitted' if recalled else 'semantic_omission'}
                            for i, recalled in enumerate(expected['fact_recall'])}}
                return {'output': {'grades': grades}, 'metrics': receipt()}
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.side_effect = native
                with self.assertRaisesRegex(RuntimeError, 'disagrees with human'):
                    calibration.calibrate(experiment, run, protocol, old_controls_path, live=True)
                self.assertEqual(runner.call_count, 5)
            prior_ledger = (run / 'calls.jsonl').read_bytes()
            prior_files = {p: p.read_bytes() for p in (run / 'receipts').glob('*.json')}
            prior_files.update({p: p.read_bytes() for p in (run / 'judge-calibration').glob('*.json')})
            ack = calibration.build_continuation_ack(experiment, run, original_protocol, old_controls_path, original_run)
            experiment.write(ack_path, ack)
            revised = json.loads(json.dumps(protocol))
            revised['judge_calibration'].update(controls_sha256=calibration.file_sha256(current_controls_path),
                continuation={'oracle_version': 'public-critical-taxonomy-v2',
                    'ack_manifest_sha256': calibration.file_sha256(ack_path),
                    'previous_protocol_sha256': experiment.digest(protocol)})
            experiment.prepare_run_metadata(run, revised, revise=True)
            def invoke(**kwargs):
                return calibration.calibrate(experiment, run, revised, current_controls_path, live=True,
                    continue_unattempted=True, ack_path=ack_path, **kwargs)
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                with self.assertRaisesRegex(ValueError, 'both'):
                    calibration.calibrate(experiment, run, revised, current_controls_path, live=True, continue_unattempted=True)
                runner.assert_not_called()
            snapshots = {p: p.read_bytes() for p in run.rglob('*') if p.is_file()}
            for failure in ('response_tamper', 'request_mismatch', 'result_tamper', 'changed_clock', 'extra_attempt', 'frozen', 'missing_ack_binding'):
                with self.subTest(failure=failure):
                    if failure in ('response_tamper', 'request_mismatch'):
                        suffix = 'response' if failure == 'response_tamper' else 'request'
                        target = run / 'receipts' / ('call-0001-' + suffix + '.json')
                        target.write_text('{}')
                    elif failure == 'result_tamper':
                        experiment.write(run / 'judge-calibration/real-causal-conflation.json', {'status': 'matched'})
                    elif failure == 'changed_clock':
                        experiment.write(run / 'run.json', {**experiment.read(run / 'run.json'), 'started_at': 0})
                    elif failure == 'extra_attempt':
                        for row in model_call('call-0006', 'judge', 'calibration'):
                            experiment.append(run / 'calls.jsonl', row)
                    elif failure == 'frozen':
                        experiment.prepare_run_metadata(run, revised, freeze_payload={'public_test': True})
                    else:
                        experiment.write(ack_path, {**ack, 'prior_packets': ack['prior_packets'][:4]})
                    with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                        with self.assertRaises((ValueError, RuntimeError)):
                            invoke()
                        runner.assert_not_called()
                    for target in run.rglob('*'):
                        if target.is_file() and target not in snapshots:
                            target.unlink()
                    for target, content in snapshots.items():
                        target.write_bytes(content)
                    experiment.write(ack_path, ack)
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.side_effect = native
                summary = invoke()
                self.assertEqual(runner.call_count, 2)
            self.assertEqual(summary['counts'], {'prior_matched': 4, 'acknowledged_taxonomy_mismatch': 1, 'new_matched': 2})
            self.assertEqual(summary['status'], 'completed_with_taxonomy_acknowledgment')
            self.assertEqual([r['control_id'] for r in summary['results']], ['original-only-visibility', 'grouped-six'])
            self.assertEqual(experiment.read(run / 'run.json')['started_at'], experiment.read(original_run)['started_at'])
            self.assertTrue((run / 'calls.jsonl').read_bytes().startswith(prior_ledger))
            self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 7)
            for target, content in prior_files.items():
                self.assertEqual(target.read_bytes(), content)
            self.assertEqual(experiment.read(run / 'judge-calibration/real-causal-conflation.json')['status'], 'human_label_mismatch')
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                with self.assertRaisesRegex(ValueError, 'ledger changed'):
                    invoke()
                runner.assert_not_called()

    def test_optional_judge_profile_uses_isolated_native_module_and_preserves_production_constants(self):
        from codex_mem import processor as native
        protocol = {'model': native.MODEL, 'reasoning_effort': native.REASONING_EFFORT,
                    'judge_profile': {'model': 'gpt-6-astra', 'reasoning_effort': 'medium'}}
        original = (native.MODEL, native.REASONING_EFFORT)
        clone = experiment.native_phase_adapter(protocol, 'judge')
        self.assertIsNot(clone, native)
        self.assertEqual((clone.MODEL, clone.REASONING_EFFORT), ('gpt-6-astra', 'medium'))
        self.assertEqual((native.MODEL, native.REASONING_EFFORT), original)
        self.assertIs(experiment.native_phase_adapter(protocol, 'reader'), native)
        self.assertIs(experiment.native_phase_adapter(protocol, 'generation'), native)
        self.assertIs(experiment.native_phase_adapter({k: v for k, v in protocol.items() if k != 'judge_profile'}, 'judge'), native)
        self.assertNotIn(clone.__name__, sys.modules)
        client = types.SimpleNamespace(request=lambda method, params: {'data': [
            {'model': 'gpt-6-astra', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]})
        clone._verify_luna_available(client)
        unavailable = types.SimpleNamespace(request=lambda method, params: {'data': [
            {'model': native.MODEL, 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]})
        with self.assertRaises(Exception) as caught:
            clone._verify_luna_available(unavailable)
        self.assertEqual(caught.exception.code, 'model_unavailable')
        with self.assertRaises(Exception) as caught:
            clone._verify_thread_start({'model': native.MODEL, 'reasoningEffort': 'medium',
                'modelProvider': 'openai', 'thread': {'id': 'thread-profile-test'}})
        self.assertEqual(caught.exception.code, 'model_mismatch')
        profile_request = experiment.request('Public prompt.', {}, protocol, phase='judge')
        self.assertEqual(profile_request['model'], 'gpt-6-astra')
        self.assertEqual(experiment.request('Public prompt.', {}, protocol)['model'], native.MODEL)

    def test_custom_judge_profile_rejects_fallback_and_preserves_raw_usage(self):
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(judge_profile={'model': 'gpt-6-astra', 'reasoning_effort': 'medium'})
        protocol.pop('historical_spend', None)
        adapter = experiment.native_phase_adapter(protocol, 'judge')
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            capture = experiment.Capture(run, protocol, 'judge', 'baseline', 'public-profile')
            wrong = {'output': {}, 'metrics': receipt(), 'evidence': {'thread_start': {
                'model': 'gpt-6-luna', 'reasoning_effort': 'medium', 'model_provider': 'openai'}, 'rerouted': True}}
            with patch.object(experiment, 'native_phase_adapter', return_value=adapter), patch.object(adapter, 'NativeProcessorRunner') as runner:
                runner.return_value.return_value = wrong
                with self.assertRaisesRegex(ValueError, 'actual profile'):
                    capture(experiment.request('Public fixed request.', {}, protocol, phase='judge'))
                runner.assert_called_once()
            self.assertEqual(experiment.read(run / 'receipts/call-0001-response.json'), wrong)
            usage = experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))
            self.assertEqual(usage['failed_calls'], 1)
            self.assertTrue(usage['token_accounting_complete'])
            self.assertEqual(usage['tokens']['total_tokens'], 100)
            self.assertFalse((run / 'native-request-cache.jsonl').exists())
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                with self.assertRaisesRegex(ValueError, 'declared phase'):
                    experiment.Capture(run, protocol, 'reader', 'baseline', 'public-profile')(
                        experiment.request('Wrong reader request.', {}, protocol, phase='judge'))
                runner.assert_not_called()
            self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['attempted_calls'], 1)
        for profile in ({'model': 'gpt-6-astra'}, {'model': '', 'reasoning_effort': 'medium'},
                        {'model': 'gpt-6-astra', 'reasoning_effort': 'medium', 'fallback': True}):
            with self.assertRaises(ValueError):
                experiment.phase_profile({**protocol, 'judge_profile': profile}, 'judge')

    def test_custom_judge_profile_cache_uses_actual_profile_and_inventory_verifies_availability(self):
        from codex_mem import processor as native
        protocol = experiment.read(SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        protocol.update(judge_profile={'model': 'gpt-6-astra', 'reasoning_effort': 'medium'},
                        paired_native_request_reuse='exact-request-v1', reader_citation_contract='visible-span-refs-v1')
        protocol.pop('historical_spend', None)
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            factory = experiment.native_phase_adapter
            def adapter(protocol, phase):
                result = factory(protocol, phase)
                result.NativeProcessorRunner = runner
                return result
            def response(request):
                return {'output': {}, 'metrics': receipt(), 'evidence': {'thread_start': {
                    'model': request['model'], 'reasoning_effort': request['reasoning_effort'],
                    'model_provider': 'openai'}, 'rerouted': False}}
            with patch.object(experiment, 'native_phase_adapter', side_effect=adapter):
                from unittest.mock import MagicMock
                runner = MagicMock(); runner.return_value.side_effect = response
                baseline = experiment.Capture(run, protocol, 'judge', 'baseline', 'public-profile-cache')
                baseline(experiment.request('Identical public body.', {}, protocol, phase='judge'))
                baseline.accept_for_reuse()
                changed = {**protocol, 'judge_profile': {'model': 'gpt-6-luna', 'reasoning_effort': 'medium'}}
                candidate = experiment.Capture(run, changed, 'judge', 'candidate', 'public-profile-cache')
                candidate(experiment.request('Identical public body.', {}, changed, phase='judge'))
                candidate.accept_for_reuse()
                same = experiment.Capture(run, protocol, 'judge', 'candidate', 'public-profile-cache')
                same(experiment.request('Identical public body.', {}, protocol, phase='judge'))
                self.assertEqual(runner.call_count, 2)
                self.assertTrue(same.dispatch_provenance()['reused'])
            scopes = experiment.calls(run / 'native-request-cache.jsonl')
            self.assertEqual({row['scope']['model'] for row in scopes}, {'gpt-6-luna', 'gpt-6-astra'})
            path = run / 'protocol.json'; experiment.write(path, protocol)
            def listed(method, params):
                return {'data': [{'id': model, 'model': model, 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}
                                 for model in ('gpt-6-luna', 'gpt-6-astra')]} if method == 'model/list' else {}
            with patch.object(native, '_AppServer') as server:
                server.return_value.request.side_effect = listed
                inventory = experiment.inventory(types.SimpleNamespace(run=run, protocol=path))
                self.assertTrue(inventory['available'])
                self.assertEqual(inventory['phase_profiles']['judge']['model'], 'gpt-6-astra')
                self.assertEqual(inventory['phase_profiles']['reader']['model'], 'gpt-6-luna')
                self.assertEqual(inventory['model_calls'], 0)
            def unavailable(method, params):
                return {'data': [{'id': 'gpt-6-luna', 'model': 'gpt-6-luna',
                    'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]} if method == 'model/list' else {}
            with patch.object(native, '_AppServer') as server:
                server.return_value.request.side_effect = unavailable
                inventory = experiment.inventory(types.SimpleNamespace(run=run, protocol=path))
                self.assertFalse(inventory['available'])
                self.assertFalse(inventory['phase_profiles']['judge']['available'])
            self.assertEqual((native.MODEL, native.REASONING_EFFORT), ('gpt-6-luna', 'medium'))

    def test_mixed_profile_usage_prices_reported_actual_models(self):
        rows = model_call('luna-call', 'reader', 'baseline') + model_call('astra-call', 'judge', 'baseline')
        rows[0]['model'] = 'gpt-6-luna'; rows[2]['model'] = 'gpt-6-astra'
        def price(event):
            amount = '1' if event['model'] == 'gpt-6-luna' else '3'
            return {'api_equivalent_usd': {'standard': amount, 'fast': amount}}
        with patch('codex_mem.pricing.price_event', side_effect=price) as pricing:
            result = experiment.usage(rows)
        self.assertEqual([call.args[0]['model'] for call in pricing.call_args_list], ['gpt-6-luna', 'gpt-6-astra'])
        self.assertEqual(result['hypothetical_api_equivalent_usd']['standard'], '4')
        self.assertTrue(result['token_accounting_complete'])
        self.assertIsNone(result['cost_usd'])

    def test_judge_schema_and_runtime_require_each_question_and_fact(self):
        cases = [{'id': 'answer-00', 'gold': {'required_facts': [{}, {}]}},
                 {'id': 'answer-01', 'gold': {'required_facts': [{}]}}]
        schema = experiment.judge_schema(cases)
        self.assertEqual(schema['properties']['grades']['required'], ['answer-00', 'answer-01'])
        self.assertEqual(schema['properties']['grades']['properties']['answer-00']
                         ['properties']['fact_recall']['required'], ['0', '1'])
        grade = dict(full_supported_answer=True, current_state_actionable=True,
                     constraints_obligations_reasons_recalled=True, citations_supported=True,
                     empty_or_evasive=False, critical_errors=[], rationale='Fictional test.')
        valid = {'grades': {'answer-00': {**grade, 'fact_recall': {'0': True, '1': False}},
                            'answer-01': {**grade, 'fact_recall': {'0': True}}}}
        accepted = experiment.decode_judge_output(valid, cases)
        self.assertEqual(accepted[0]['fact_recall'], [{'fact_index': 0, 'recalled': True},
                                                     {'fact_index': 1, 'recalled': False}])
        for mutate in (lambda v: v['grades'].pop('answer-01'),
                       lambda v: v['grades'].update(extra=v['grades']['answer-01']),
                       lambda v: v['grades']['answer-00']['fact_recall'].pop('1'),
                       lambda v: v['grades']['answer-00']['fact_recall'].update({'2': True}),
                       lambda v: v['grades']['answer-00']['fact_recall'].update({'0': 1}),
                       lambda v: v['grades']['answer-00'].update(required_facts_total=2)):
            invalid = json.loads(json.dumps(valid))
            mutate(invalid)
            with self.assertRaises(ValueError):
                experiment.decode_judge_output(invalid, cases)

    def test_judge_public_path_records_rejection_and_never_retries_or_salvages(self):
        evidence = SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20'
        protocol = experiment.read(evidence / 'protocol.json')
        fixture = experiment.read(evidence / 'inputs/holdout.json')
        history = next(h for h in fixture['histories'] if h['id'] == 'fiction-20')
        with tempfile.TemporaryDirectory() as temporary, tarfile.open(
                evidence / 'holdout-run/fictional-run-evidence.tar.gz') as archive:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            directory = run / 'baseline/fiction-20'
            for name in ('reader-0.json', 'contexts.json', 'generation.json'):
                experiment.write(directory / name, json.load(archive.extractfile('baseline/fiction-20/' + name)))
            archived = json.load(archive.extractfile('receipts/call-0059-response.json'))
            # Translate the saved response representation only. The missing fact stays absent.
            output = {'grades': {g['id']: {**{k: v for k, v in g.items()
                if k not in ('id', 'required_facts_total', 'required_facts_recalled', 'fact_recall')},
                'fact_recall': {str(f['fact_index']): f['recalled'] for f in g['fact_recall']}}
                for g in archived['output']['grades']}}
            args = types.SimpleNamespace(run=run, gold=evidence / 'inputs/holdout.gold.json',
                                         history=None, variant='baseline')
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = {**archived, 'output': output}
                with self.assertRaisesRegex(ValueError, 'fact_recall'):
                    experiment.judges(args, {'histories': [history]}, protocol)
                self.assertFalse((directory / 'judge-0.json').exists())
                rejected = experiment.read(directory / 'judge-0.rejected.json')
                self.assertEqual(rejected['questions_accepted'], 0)
                self.assertEqual(len(rejected['planned_question_ids']), 6)
                with self.assertRaisesRegex(ValueError, 'no automatic retry'):
                    experiment.judges(args, {'histories': [history]}, protocol)
                runner.assert_called_once()
            usage = experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))
            self.assertEqual(usage['attempted_calls'], 1)
            self.assertEqual(usage['finished_calls'], 1)
            report = experiment.aggregate(types.SimpleNamespace(run=run), {'split': 'holdout', 'histories': [history]},
                                          {'regeneration_repeat_history_ids': []})
            self.assertEqual(report['variants']['baseline']['questions_planned'], 6)
            self.assertEqual(report['variants']['baseline']['questions_graded'], 0)
            self.assertEqual(len(report['variants']['baseline']['rejected_judge_batches']), 1)
            self.assertEqual(report['decision'], 'incomplete')

    def test_complete_judge_output_is_saved_through_public_phase(self):
        evidence = SCRIPT.parents[1] / 'docs/evidence/consolidation-retention-2026-10-20'
        protocol = experiment.read(evidence / 'protocol.json')
        questions = [{'id': f'q{i}', 'question': 'What remains open?'} for i in range(6)]
        history = {'id': 'fiction-test', 'sessions': [], 'questions': questions}
        gold = {'questions': {'fiction-test/' + q['id']: {'required_facts': [{'support': []}], 'category': 'constraint'}
                              for q in questions}}
        grade = dict(full_supported_answer=False, current_state_actionable=False,
                     constraints_obligations_reasons_recalled=False, citations_supported=False,
                     empty_or_evasive=True, critical_errors=[], rationale='Synthetic abstention.',
                     fact_recall={'0': False})
        output = {'grades': {f'answer-{i:02d}': grade for i in range(6)}}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
            experiment.write(run / 'gold.json', gold)
            directory = run / 'baseline/fiction-test'
            experiment.write(directory / 'generation.json', {'source_map': {}})
            experiment.write(directory / 'contexts.json', [{'id': q['id'], 'memory': '', 'context_chars': 0}
                                                          for q in questions])
            experiment.write(directory / 'reader-0.json', {'output': {'answers': []}, 'latency_ms': 1, 'metrics': {}})
            args = types.SimpleNamespace(run=run, gold=run / 'gold.json', history=None, variant='baseline')
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = {'output': output, 'metrics': receipt()}
                experiment.judges(args, {'histories': [history]}, protocol)
            persisted = experiment.read(directory / 'judge-0.json')['grades']
            self.assertEqual({g['id'] for g in persisted}, {q['id'] for q in questions})
            self.assertEqual(len(persisted), 6)
            self.assertTrue(all(g['required_facts_total'] == 1 and g['required_facts_recalled'] == 0
                                and g['fact_recall'] == [{'fact_index': 0, 'recalled': False}] for g in persisted))
            self.assertFalse((directory / 'judge-0.rejected.json').exists())

    def test_every_native_dispatch_reserves_its_full_wall_allowance(self):
        protocol = experiment.read(Path(__file__).resolve().parents[2] /
                                   'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        limits = protocol['limits']
        for remaining, blocked in ((limits['call_seconds'] - 1, True),
                                   (limits['call_seconds'] + 60, False)):
            with self.subTest(remaining=remaining), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                experiment.write(run / 'run.json', {
                    'started_at': experiment.time.time() - limits['wall_seconds'] + remaining,
                    'budget_run': str(run),
                })
                capture = experiment.Capture(run, protocol, 'generation', 'baseline', 'fiction-01')
                with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                    runner.return_value.return_value = {'metrics': receipt()}
                    if blocked:
                        with self.assertRaisesRegex(RuntimeError, 'experiment_wall_budget_exhausted'):
                            capture({'prompt': 'Fictional test only.'})
                        runner.assert_not_called()
                        self.assertFalse((run / 'calls.jsonl').exists())
                    else:
                        capture({'prompt': 'Fictional test only.'})
                        runner.assert_called_once_with(timeout=limits['call_seconds'])
                        self.assertEqual(experiment.usage(experiment.ledger_calls(
                            run / 'calls.jsonl'))['attempted_calls'], 1)

    def test_actual_renderer_pairs_complete_long_quote_with_its_original_source(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tests'))
        from test_project_memory_consolidation import ConsolidationTests
        from codex_mem.project_memory_retrieval import build_project_brief,render_project_brief
        from codex_mem.store import Store
        case=ConsolidationTests()
        case.setUp()
        try:
            suffix='but production was not contacted.'
            body='Task LUX-54: '+('local preview detail '*18)+suffix
            source=case.remember(body,source='hook:Stop')
            self.assertEqual(case.process(case.standalone)['status'],'processed')
            with Store(case.data) as store:
                wrong=store.remember(case.project/'other-project','Unrelated tool result',
                    'Other prefix. Invisible original tail.',source='hook:PostToolUse',session_id='other-session')
                memory=render_project_brief(build_project_brief(store,case.project,include_shadow=True,budget=6000),budget=6000)
            visible=experiment.visible_source_quotes([{'memory':memory}])
            bodies={source['id']:body,wrong['id']:'Other prefix. Invisible original tail.'}
            self.assertTrue(experiment.citations_visible([{'source_id':source['id'],'quote':suffix}],visible,bodies))
            self.assertFalse(experiment.citations_visible([{'source_id':wrong['id'],'quote':suffix}],visible,bodies))
            prose=('<project-memory><field name="summary" basis="model_inference"><derived_text>Invisible original tail.</derived_text>'
                '<source source_id="'+wrong['id']+'"><quote_excerpt>Other prefix.</quote_excerpt></source></field></project-memory>')
            borrowed=experiment.visible_source_quotes([{'memory':memory+prose}])
            self.assertFalse(experiment.citations_visible([{'source_id':wrong['id'],'quote':'Invisible original tail.'}],borrowed,bodies))
        finally:
            case.doCleanups()

    def test_prior_spend_is_hash_pinned_and_separate_from_current_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'historical-report.json'
            prior=experiment.usage(model_call('old','reader','baseline',100))
            experiment.write(path,{'usage':prior})
            import hashlib
            protocol={'historical_spend':{'report':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
            current=experiment.usage(model_call('new','reader','candidate',200))
            accounting=experiment.historical_accounting(protocol,current)
            self.assertEqual(current['attempted_calls'],1)
            self.assertEqual(accounting['historical_usage']['tokens']['total_tokens'],100)
            self.assertEqual(accounting['cumulative_pilot_usage']['tokens']['total_tokens'],300)
            self.assertEqual(accounting['cumulative_pilot_usage']['attempted_calls'],2)
            self.assertIsNone(accounting['coding_research_overhead_tokens'])
            path.write_text('{}')
            with self.assertRaises(ValueError):
                experiment.historical_accounting(protocol,current)

    def test_export_pseudonymizes_diagnostics_but_preserves_fictional_turns_and_metrics(self):
        thread='11111111-1111-4111-8111-111111111111'
        turn='22222222-2222-4222-8222-222222222222'
        fictional='33333333-3333-4333-8333-333333333333'
        payload={'jobs':[{'worker_thread_id':thread,'worker_turn_id':turn}],
            'evidence':{'thread_start':{'thread_id':thread},'turnStarted':{'threadId':thread,'turnId':turn}},
            'source_records':[{'turn_id':fictional,'source_id':'fictional-source','body':'7 passed locally.'}],
            'output':{'answer':'Fictional session-1 reports 7 passed locally.'},
            'metrics':{**receipt(total=100),'evidence':{'thread_start':{'thread_id':thread},
                'turn_started':{'thread_id':thread,'turn_id':turn}}}}
        members={'receipt.json':json.dumps(payload).encode(),'calls.jsonl':b'unchanged ledger\n'}
        cleaned,note=experiment.sanitize_export_members(members)
        value=json.loads(cleaned['receipt.json'])
        self.assertEqual(value['source_records'],payload['source_records'])
        self.assertEqual(value['metrics']['usage'],payload['metrics']['usage'])
        self.assertEqual(value['output'],payload['output'])
        self.assertEqual(value['jobs'][0]['worker_thread_id'],value['evidence']['thread_start']['thread_id'])
        self.assertEqual(value['jobs'][0]['worker_turn_id'],value['evidence']['turnStarted']['turnId'])
        self.assertEqual(value['jobs'][0]['worker_turn_id'],value['metrics']['evidence']['turn_started']['turn_id'])
        self.assertNotIn(thread,cleaned['receipt.json'].decode())
        self.assertNotIn(turn,cleaned['receipt.json'].decode())
        self.assertEqual(cleaned['calls.jsonl'],members['calls.jsonl'])
        self.assertNotIn(thread,json.dumps(note))
        self.assertEqual(cleaned,experiment.sanitize_export_members(members)[0])
        archive=experiment.export_archive_bytes(cleaned)
        self.assertEqual(archive[4:8],b'\0\0\0\0')
        self.assertEqual(archive,experiment.export_archive_bytes(cleaned))

    def test_ledger_rejects_duplicate_or_mismatched_attempt_receipts(self):
        rows=model_call('a','reader','baseline')
        experiment.validate_ledger(rows)
        for invalid in (rows+[rows[1]], [rows[1]],
                        [rows[0],{**rows[1],'variant':'candidate'}],
                        [rows[0],rows[0]]):
            with self.assertRaises(ValueError):
                experiment.validate_ledger(invalid)

    def test_retrieval_timing_is_saved_without_entering_memory_payload(self):
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with Store(root/'data'):
                pass
            context=experiment.retrieve_question(root/'data',{'a':str(root/'project')},
                {'id':'q1','question':'What remains open?','project_keys':['a']},6000,root/'retrieval-timings.jsonl')
            self.assertEqual(set(context),{'id','question','memory','context_chars','context_sha256'})
            self.assertLessEqual(context['context_chars'],6000)
            timing=experiment.calls(root/'retrieval-timings.jsonl')[0]
            self.assertEqual(timing['question_id'],'q1')
            self.assertEqual(timing['status'],'completed')
            self.assertEqual(timing['projects'][0]['project_key'],'a')
            self.assertGreaterEqual(timing['wall_ms'],0)
            self.assertGreaterEqual(timing['projects'][0]['wall_ms'],0)
            self.assertEqual(timing['context_sha256'],context['context_sha256'])

    def test_tool_capture_rejects_scope_overrides_and_ambiguous_exit_status(self):
        capture={'tool_name':'Bash','tool_input':{'command':'pytest local_spec.py'},
                 'tool_response':{'exit_code':0,'output':'7 passed locally.'}}
        experiment.validate_tool_capture({'type':'tool','tool_capture':capture})
        for bad in ({**capture,'session_id':'wrong-session'},
                    {**capture,'tool_response':{'exit_code':True,'output':'7 passed locally.'}}):
            with self.assertRaises(ValueError):
                experiment.validate_tool_capture({'type':'tool','tool_capture':bad})
        with self.assertRaises(ValueError):
            experiment.validate_tool_capture({'type':'assistant','tool_capture':capture})

    def test_tool_fixture_preserves_execution_and_project_session_scope(self):
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            project=root/'fictional-project'
            other=root/'other-project'
            event={'id':'tool-event','type':'tool','text':'Command: pytest local_spec.py\nExit code: 0\nOutput: 7 passed locally. No deployment ran.',
                'tool_capture':{'tool_name':'Bash','tool_input':{'command':'pytest local_spec.py'},
                                'tool_response':{'exit_code':0,'output':'7 passed locally. No deployment ran.'}}}
            with Store(root/'data') as store:
                stored=experiment.capture_event(store,project,'fictional-session',event)
                record=store.get(project,[stored['id']])[0]
                raw=store.get_tool_uses(project,session_id='fictional-session')
                self.assertEqual(record['provenance']['record_role'],'tool_record')
                self.assertEqual(record['body'],event['text'])
                self.assertEqual(len(raw),1)
                self.assertEqual(json.loads(raw[0]['tool_input']),event['tool_capture']['tool_input'])
                self.assertEqual(json.loads(raw[0]['tool_response']),event['tool_capture']['tool_response'])
                self.assertEqual(raw[0]['turn_id'],'tool-event')
                self.assertEqual(store.get_tool_uses(other,session_id='fictional-session'),[])
                self.assertEqual(store.get_tool_uses(project,session_id='another-session'),[])

    def test_partial_or_invalid_receipts_cannot_unlock_next_call(self):
        for metrics in (receipt('partial'), receipt('unavailable'), {'usage':{'status':'reported','tokens':{'total_tokens':10}}}):
            rows=[{'status':'started'},{'status':'failed','metrics':metrics}]
            self.assertFalse(experiment.usage(rows)['token_accounting_complete'])
        self.assertFalse(experiment.usage([{'status':'started'}])['token_accounting_complete'])
        self.assertTrue(experiment.usage([{'status':'started'},{'status':'completed','metrics':receipt()}])['token_accounting_complete'])

    def test_shared_visibility_decodes_xml_and_preserves_source_pairing(self):
        contexts=[{'memory':'PROJECT p\n<project-memory><source source_id="first"><quote_excerpt>Only first.</quote_excerpt></source></project-memory>'},
                  {'memory':'PROJECT p\n<project-memory><source source_id="other"><quote_excerpt>Keep A &amp; B &lt; 4.</quote_excerpt></source></project-memory>'}]
        visible=experiment.visible_source_quotes(contexts)
        citation=[{'source_id':'other','quote':'A & B < 4'}]
        self.assertTrue(experiment.citations_visible(citation,visible,{'other':'Keep A & B < 4.'}))
        self.assertFalse(experiment.citations_visible([{'source_id':'first','quote':'A & B < 4'}],visible,{'first':'A & B < 4'}))
        self.assertFalse(experiment.citations_visible([{'source_id':'other','quote':'This was never shown'}],visible,{'other':'This was never shown'}))

    def test_failed_candidate_remains_readable_but_fails_the_pipeline_gate(self):
        self.assertEqual(experiment.reader_pipeline_status('candidate',{'status':'failed'}),'failed')
        with self.assertRaises(ValueError):
            experiment.reader_pipeline_status('candidate',{'status':'running'})
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            experiment.write(run/'candidate/fiction-01/consolidation.json',dict(status='failed',jobs=[dict(status='failed',job_kind='reconsideration')],accepted_merge_records=[],accepted_merge_count=0))
            value={'split':'holdout','histories':[{'id':'fiction-01'}]}
            report=experiment.aggregate(types.SimpleNamespace(run=run),value,{'regeneration_repeat_history_ids':[]})
            self.assertEqual(report['decision'],'fail')
            self.assertEqual(report['pipeline_failures'][0]['status'],'failed')
            self.assertEqual(report['variants']['candidate']['questions_graded'],6)

    def independent_protocol(self):
        protocol = experiment.read(SCRIPT.parents[1] /
            'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        return {**protocol, 'comparison_mode':'independent-generation',
                'regeneration_repeat_history_ids':[], 'reader_repeat_history_ids':[]}

    def test_independent_generation_captures_both_arms_without_clone(self):
        from codex_mem.store import Store
        from codex_mem import episode_processor
        protocol = self.independent_protocol()
        history = {'id':'fiction-01','project_keys':['fictional'], 'sessions':[
            {'id':'fictional-session','events':[
                {'id':'request','type':'user','text':'Keep the fictional deployment open.'}]}],
            'questions':[]}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run/'run.json', {'started_at':experiment.time.time(),'budget_run':str(run)})
            args = types.SimpleNamespace(run=run, runtime_root=SCRIPT.parents[1], history=None)
            native = {'output':{'episodes':[], 'claims':[], 'relations':[]},
                'evidence':{'thread_start':{'thread_id':'fictional-worker','model':episode_processor.MODEL,
                    'reasoning_effort':episode_processor.REASONING_EFFORT,'model_provider':'openai'},
                    'turn_started':{'thread_id':'fictional-worker','turn_id':'fictional-turn'},
                    'turn_completed':True,'no_tools':True,'rerouted':False}, 'metrics':receipt()}
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.return_value = native
                for arm in ('candidate','baseline'):
                    args.variant = arm
                    experiment.generate(args, {'histories':[history]}, protocol)
                    meta = experiment.read(run/arm/'fiction-01/generation.json')
                    self.assertEqual(meta['status'],'complete')
                    self.assertNotIn('cloned_baseline_state_sha256',meta)
                    self.assertEqual(meta['captured_history_sha256'],experiment.digest(history['sessions']))
                    with Store(run/arm/'fiction-01/data') as store:
                        source = store.get(Path(meta['projects']['fictional']),[meta['source_map']['request']])[0]
                        self.assertEqual(source['body'],history['sessions'][0]['events'][0]['text'])
                    self.assertEqual(meta['persisted_state']['event_coverage'][0]['captured_events'],1)
                    self.assertEqual(meta['persisted_state']['event_coverage'][0]['pending_events'],0)
                self.assertEqual(runner.call_count,2)
                experiment.generate(args, {'histories':[history]}, protocol)
                self.assertEqual(runner.call_count,2)
            rows = [r for r in experiment.ledger_calls(run/'calls.jsonl') if r['status']=='completed']
            self.assertEqual([r['variant'] for r in rows],['candidate','baseline'])
            self.assertTrue(all(r['phase']=='generation' for r in rows))
            with self.assertRaisesRegex(ValueError,'clone is forbidden'):
                experiment.clone(args, {'histories':[history]}, protocol)

    def test_failed_independent_candidate_generation_keeps_denominator_and_never_retries(self):
        protocol = self.independent_protocol()
        history = {'id':'fiction-01','project_keys':['fictional'], 'sessions':[
            {'id':'fictional-session','events':[
                {'id':'request','type':'user','text':'Keep this fictional task open.'}]}]}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run/'run.json', {'started_at':experiment.time.time(),'budget_run':str(run)})
            experiment.write(run/'baseline/fiction-01/generation.json',{'status':'complete'})
            args = types.SimpleNamespace(run=run, runtime_root=SCRIPT.parents[1], history=None,variant='candidate')
            with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                runner.return_value.side_effect = RuntimeError('fictional failure')
                experiment.generate(args, {'histories':[history]}, protocol)
                meta = experiment.read(run/'candidate/fiction-01/generation.json')
                self.assertEqual(meta['status'],'failed')
                experiment.generate(args, {'histories':[history]}, protocol)
                self.assertEqual(runner.call_count,1)
            # Even stale perfect grades cannot hide the failed generation receipt.
            self.report(run)
            report = experiment.aggregate(args, {'split':'holdout','histories':[history]}, protocol)
            self.assertEqual(report['variants']['candidate']['questions_planned'],6)
            self.assertEqual(report['variants']['candidate']['questions_graded'],0)
            self.assertEqual(report['variants']['candidate']['full_supported_rate'],0)
            self.assertEqual(report['variants']['candidate']['missing_histories'],['fiction-01'])
            self.assertEqual(report['pilot_gate'],'incomplete')
            self.assertTrue(any(p.get('stage')=='generation' and p['status']=='failed'
                                for p in report['pipeline_failures']))
            with self.assertRaisesRegex(ValueError,'generation must succeed'):
                experiment.consolidate(args, {'histories':[history]}, protocol)

    def test_independent_cost_uses_each_arms_generation_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.report(run)
            for arm in ('baseline','candidate'):
                experiment.write(run/arm/'fiction-01/generation.json',{'status':'complete'})
            for row in model_call('candidate-generation','generation','candidate',400):
                experiment.append(run/'calls.jsonl',row)
            report = experiment.aggregate(types.SimpleNamespace(run=run),
                {'split':'holdout','histories':[{'id':'fiction-01'}]}, self.independent_protocol())
            self.assertEqual(report['equivalent_complete_arm_tokens'],{'baseline':200,'candidate':600})
            self.assertEqual(report['incremental_processing_and_reader_tokens'],{'baseline':200,'candidate':600})
            self.assertEqual(report['comparison_mode'],'independent-generation')
            self.assertIn('stochastic extraction', ' '.join(report['limitations']))

    def test_holdout_cannot_shrink_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'fixture.json'
            experiment.write(path,dict(schema_version=1,fictional=True,split='holdout',histories=[dict(id='fiction-05')]))
            with self.assertRaisesRegex(ValueError,'exactly 16'):
                experiment.fixture(path)

    def report(self, run, lost=False):
        experiment.write(run/'run.json',dict(budget_run=str(run)))
        fixture={'split':'holdout','histories':[{'id':'fiction-01'}]}
        protocol={'regeneration_repeat_history_ids':[]}
        for variant in ('baseline','candidate'):
            rows=[]
            for index in range(6):
                recall=not (lost and variant=='candidate' and index==0)
                rows.append(dict(id=f'q{index}',full_supported_answer=recall,current_state_actionable=True,
                    constraints_obligations_reasons_recalled=recall,citations_supported=True,critical_errors=[],
                    empty_or_evasive=False,fact_recall=[dict(fact_index=0,recalled=recall)],category='constraint'))
            experiment.write(run/variant/'fiction-01/judge-0.json',dict(grades=rows))
            experiment.write(run/variant/'fiction-01/contexts.json',[dict(context_chars=100)])
        calls=[]
        calls+=model_call('a','generation','baseline',100)
        calls+=model_call('b','reader','baseline',100)
        calls+=model_call('c','reader','candidate',100)
        calls+=model_call('d','consolidation','candidate',100)
        calls+=model_call('e','reader','candidate',10000,repeat=1)
        calls+=model_call('f','consolidation','candidate',10000,run_id='replicates/regeneration-1')
        for row in calls:
            experiment.append(run/'calls.jsonl',row)
        return experiment.aggregate(types.SimpleNamespace(run=run),fixture,protocol)

    def test_equal_quality_with_extra_cost_is_not_improvement(self):
        with tempfile.TemporaryDirectory() as temporary:
            report=self.report(Path(temporary))
        self.assertEqual(report['pilot_gate'],'pass')
        self.assertEqual(report['decision'],'no_improvement')
        self.assertEqual(report['equivalent_complete_arm_tokens'],dict(baseline=200,candidate=300))
        self.assertEqual(report['usage']['tokens']['total_tokens'],20400)
        self.assertEqual(report['usage_repeats']['tokens']['total_tokens'],20000)

    def test_identical_shared_memory_answer_change_is_variation_stratum(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            for arm in ('baseline','candidate'):
                contexts=[{'id':f'q{i}','memory':'<project-memory><source source_id="same"><quote_excerpt>Stable evidence.</quote_excerpt></source></project-memory>','context_chars':100} for i in range(6)]
                answers=[{'id':f'q{i}','answer':'different' if arm=='candidate' and i==0 else 'same'} for i in range(6)]
                experiment.write(run/arm/'fiction-01/contexts.json',contexts)
                experiment.write(run/arm/'fiction-01/reader-0.json',{'output':{'answers':answers}})
            report=experiment.aggregate(types.SimpleNamespace(run=run),{'split':'holdout','histories':[{'id':'fiction-01'}]}, {'regeneration_repeat_history_ids':[]})
            attribution=report['memory_change_attribution']
            self.assertEqual(attribution['per_query_exact_memory_pairs'],{'equal':6,'different':0,'missing':0})
            self.assertEqual(attribution['shared_reader_memory_strata']['identical_shared_reader_memory']['answer_changed'],1)
            self.assertTrue(attribution['history_results'][0]['shared_reader_memory_exactly_equal'])

    def test_failed_regeneration_pipeline_blocks_safe_repeat_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            for arm in ('baseline','candidate'):
                directory=run/'replicates/regeneration-1'/arm/'fiction-01'
                experiment.write(directory/'judge-0.json',experiment.read(run/arm/'fiction-01/judge-0.json'))
                experiment.write(directory/('consolidation.json' if arm=='candidate' else 'generation.json'),
                    {'status':'failed' if arm=='candidate' else 'complete'})
            report=experiment.aggregate(types.SimpleNamespace(run=run),{'split':'holdout','histories':[{'id':'fiction-01'}]},
                {'regeneration_repeat_history_ids':['fiction-01']})
            self.assertTrue(report['repeats_complete'])
            self.assertEqual(report['candidate_repeat_critical_errors'],0)
            self.assertEqual(report['decision'],'fail')
            self.assertTrue(any(p.get('run_id')=='regeneration-1' and p['status']=='failed' for p in report['pipeline_failures']))

    def test_a_paired_constraint_loss_fails_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            report=self.report(Path(temporary),lost=True)
        self.assertEqual(report['decision'],'fail')
        self.assertFalse(report['constraints_preserved'])
        self.assertEqual(report['paired_fact_losses'][0]['fact_index'],0)


if __name__=='__main__':
    unittest.main()
