"""Synthetic public receipts/runtime only. No sealed input reads or native calls."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent
REPO = HERE.resolve().parents[2]
spec = importlib.util.spec_from_file_location('protocol_builder', HERE / 'build-protocol.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ProtocolBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / 'codex_mem').mkdir()
        shutil.copyfile(REPO / 'codex_mem/episode_processor.py', self.repo / 'codex_mem/episode_processor.py')
        (self.repo / 'codex_mem/public_toy.py').write_text('"""Inert toy runtime."""\n')
        self.runtime = builder.runtime_files(self.repo)
        self.guide = json.loads((REPO / 'docs/evidence/project-memory-quality-2026-10-21-r16/protocol.json').read_text())
        self.baseline = self.repo / 'baseline'
        (self.baseline / 'codex_mem').mkdir(parents=True)
        (self.baseline / '.experiment-baseline-commit').write_text('dd60f1c8bc7788b856e80b626975b2f609aa394a\n')
        for number in range(47):
            (self.baseline / 'codex_mem' / f'module_{number}.py').write_text(f'# public baseline toy {number}\n')
        # Pure seam supplies a toy guide. CLI always pins the real R16 guide SHA.
        self.guide['baseline_runtime_hashes'] = builder.runtime_files(self.baseline)
        harness = {}
        for name in builder.PUBLIC:
            target = self.repo / builder.PREFIX / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / name, target)
            harness[builder.PREFIX + name] = builder.sha(target.read_bytes())
        helper = 'scripts/project_memory_consolidation_experiment.py'
        (self.repo / 'scripts').mkdir()
        shutil.copyfile(REPO / helper, self.repo / helper)
        harness[helper] = builder.sha((self.repo / helper).read_bytes())
        self.config = {'schema_version':1,'sealed_directory':str(self.repo/'never-open-sealed'),
                       'baseline_runtime_root':str(self.baseline),'harness_files':harness,
                       'repeats':{'regeneration':'h04','reader':'h13'}}
        self.config['helper_sha256'] = harness[helper]
        self.config['reader_evidence_contract'] = 'assertion-owned-citations-v1'
        self.config['reader_fidelity_contract'] = 'quantified-source-fidelity-v1'
        prep='docs/evidence/project-memory-quality-2026-10-21-r21-preparation/'
        for key,name in [('repeat_selection','repeat-selection.json'),('resource_counts','resource-counts.v001.json'),('budget_binding','budget-binding.v001.json')]:
            target=self.repo/prep/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/prep/name,target)
            self.config[key]={'path':prep+name,'sha256':builder.sha(target.read_bytes())}
        resources=json.loads((self.repo/prep/'resource-counts.v001.json').read_text())
        raw=resources['author_receipt']['path'];target=self.repo/raw;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/raw,target)
        self.config['native_capacity'] = self.save('capacity.json', {'status':'native_capacity_observation','reported_state':'synthetic_only_not_availability_inference','observed_at':'synthetic-offline-observation','scope':'informational_not_reservation'})
        self.config['harness_reviews'] = {role:self.save('harness-'+role+'.json', {'status':'clear','unresolved_findings':0,'files':harness}) for role in ('spec','standards')}
        self.config['runtime_receipt'] = self.save('runtime.json', {'status':'final_source_candidate','runtime_files':self.runtime})
        source_manifest = self.save('source-manifest.json', {'runtime_files':self.runtime})
        self.originals = {}
        for role in ('tests','spec','standards'):
            original = {'status':'passed' if role=='tests' else 'clear','runtime_files':self.runtime}
            if role=='tests':
                log = self.save('toy-tests.log', 'Synthetic offline evidence only.\n', raw=True)
                original.update(tests_run=20,failed=0,errors=0,full_suite=True,acceptance_hooks=True,protocol_tests=True,
                                evidence_files={log['path']:log['sha256']})
            else:
                original.update(unresolved_findings=0,source_manifest=source_manifest)
            self.originals[role] = self.save(role+'-original.json', original)
            self.config[role+'_binding'] = self.save(role+'-binding.json',
                {'status':original['status'],'runtime_files':self.runtime,'original':self.originals[role]})
        fake_hash = lambda label: hashlib.sha256(('public toy sealed '+label).encode()).hexdigest()
        self.metadata = {'schema_version':1,'status':'accepted','unresolved_findings':0,
            'fixture_sha256':fake_hash('fixture'),'gold_sha256':fake_hash('gold'),
            'mechanical_contract_sha256':harness[builder.PREFIX+'public-fixture-contract.py'],
            'semantic_guidance_sha256':harness[builder.PREFIX+'prospective-gold-semantics-contract.json'],
            'sealed_review_receipt_sha256':fake_hash('findings'),
            'independence_attestation':'independent_author_inputs_only_no_product_prior_inputs_answers_or_grades',
            'coverage':{'questions':96,'required_facts':128,'all_questions_reviewed':True,
                        'all_required_facts_reviewed':True,'negative_cases_reviewed':True}}
        self.refresh_sealed_metadata()
        self.config['historical_spend'] = self.save('spend.json', {'status':'public_toy_no_billing_claim'})

    def save(self, name, value, raw=False):
        path = self.repo / name
        path.write_text(value if raw else json.dumps(value))
        return {'path':str(path),'sha256':builder.sha(path.read_bytes())}

    def refresh_sealed_metadata(self):
        self.config['review_metadata'] = self.save('review-metadata.json', self.metadata)
        files = {'holdout-fixture.json':self.metadata['fixture_sha256'],'holdout-gold.json':self.metadata['gold_sha256'],
                 'sealed-semantic-review.json':self.metadata['sealed_review_receipt_sha256'],
                 'sealed-semantic-review-metadata.json':self.config['review_metadata']['sha256']}
        self.config['author_manifest'] = self.save('author-manifest.json', {'files':files,'counts':{'questions':96,'required_facts':128}})
        resource_path=Path(self.config['resource_counts']['path'])
        resources=json.loads((resource_path if resource_path.is_absolute() else self.repo/resource_path).read_text())
        resources['author_receipt']=self.config['author_manifest']
        self.config['resource_counts']=self.save('toy-resource-binding.json',resources)
        budget_path=Path(self.config['budget_binding']['path'])
        budget=json.loads((budget_path if budget_path.is_absolute() else self.repo/budget_path).read_text())
        budget['resource_counts']=self.config['resource_counts']
        self.config['budget_binding']=self.save('toy-budget-binding.json',budget)

        self.config['sealed_pins'] = self.save('sealed-pins.json', {'files':files,'counts':{'questions':96,'required_facts':128}})
        self.config['review_readiness'] = self.save('readiness.json',
            {'status':'accepted_sealed_semantic_review_metadata_only_not_trial_readiness',
             'metadata_sha256':self.config['review_metadata']['sha256'],
             'pins_sha256':self.config['sealed_pins']['sha256'],
             'manifest_sha256':self.config['author_manifest']['sha256'],'review_unresolved_findings':0})

    def build(self):
        return builder.build_protocol(self.config, self.repo, self.guide)

    def test_new_reader_contract_and_capacity_require_final_helper_and_harness_reviews(self):
        result=self.build()
        self.assertEqual(result['reader_evidence_contract'],'assertion-owned-citations-v1')
        self.assertEqual(result['public_preflight']['reader_contract'],'assertion-owned-citations-v1')
        self.assertEqual(result['historical_spend']['usage_field'],'cumulative_pilot_usage')
        self.assertNotIn('optional clause-support-v2',result['reader'])
        self.assertEqual(result['public_preflight']['file_sha256']['scripts/project_memory_consolidation_experiment.py'],self.config['helper_sha256'])
        self.assertEqual(result['native_capacity_observation'],self.config['native_capacity'])
        for key in ('helper_sha256','native_capacity','harness_reviews'):
            original=self.config.pop(key)
            with self.subTest(key=key), self.assertRaises((ValueError,KeyError)):self.build()
            self.config[key]=original
        self.config['reader_evidence_contract']='clause-support-v2'
        with self.assertRaisesRegex(ValueError,'R21_requires_reader_fidelity_contract'):self.build()

    def test_real_count_budget_and_repeat_pins_are_required_without_topic_text(self):
        result=self.build()
        self.assertEqual(result['limits']['max_calls'],420)
        self.assertEqual(result['regeneration_repeat_history_ids'],['h04'])
        self.assertEqual(result['reader_repeat_history_ids'],['h13'])
        self.assertEqual(result['public_preflight']['planned_native_calls'],10)
        self.assertEqual(sum(result['planned_call_allocation'].values()),420)
        for key in ('resource_counts','budget_binding','repeat_selection'):
            saved=self.config.pop(key)
            with self.subTest(missing=key),self.assertRaises(KeyError):self.build()
            self.config[key]=saved
        resource=json.loads((self.repo/self.config['resource_counts']['path']).read_text())
        resource['case_text']='must not enter this counts-only receipt'
        self.config['resource_counts']=self.save('bad-resources.json',resource)
        with self.assertRaisesRegex(ValueError,'resource_counts_schema'):self.build()

    def test_stale_author_resource_and_unapproved_budget_reject(self):
        author_path=Path(self.config['author_manifest']['path']);author=json.loads(author_path.read_text())
        author['public_toy_version']='newer-accepted-version-needs-new-resource-binding'
        self.config['author_manifest']=self.save('new-author-manifest.json',author)
        with self.assertRaisesRegex(ValueError,'resource_author_version_not_final'):self.build()
        self.refresh_sealed_metadata()
        budget=json.loads(Path(self.config['budget_binding']['path']).read_text());budget['status']='proposed'
        self.config['budget_binding']=self.save('unapproved-budget.json',budget)
        with self.assertRaisesRegex(ValueError,'approved_resource_bound_budget_required'):self.build()

    def test_final_contents_free_v002_bindings_build_only_with_synthetic_source_dependencies(self):
        prep='docs/evidence/project-memory-quality-2026-10-21-r21-preparation/'
        files={'author_manifest':prep+'author-manifest.v002.json','review_metadata':prep+'sealed-semantic-review-metadata.json',
            'sealed_pins':prep+'sealed-pins.v001.json','resource_counts':prep+'resource-counts.v002.json',
            'budget_binding':prep+'budget-binding.v002.json','review_readiness':builder.PREFIX+'metadata-readiness-binding.v001.json'}
        for key,name in files.items():
            target=self.repo/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,target)
            self.config[key]={'path':name,'sha256':builder.sha(target.read_bytes())}
        # Actual accepted author metadata is the dependency under test. The only
        # source/test/review bindings remain explicitly synthetic in this temp root.
        result=self.build()
        self.assertEqual(result['limits']['max_calls'],420)
        self.assertEqual(result['independent_review_contract']['metadata_receipt_sha256'],self.config['review_metadata']['sha256'])
        self.assertEqual(result['public_preflight']['planned_native_calls'],10)
        self.assertFalse((REPO/builder.PREFIX/'protocol.json').exists())

    def test_changed_helper_review_and_informational_capacity_semantics(self):
        old=self.config['helper_sha256'];self.config['helper_sha256']=builder.sha(b'not the reviewed helper')
        with self.assertRaisesRegex(ValueError,'held_helper_or_driver_changed'):self.build()
        self.config['helper_sha256']=old
        self.config['harness_reviews']['spec']=self.save('rejected-harness.json',{'status':'clear','unresolved_findings':1,'files':self.config['harness_files']})
        with self.assertRaisesRegex(ValueError,'harness_review_not_final'):self.build()
        self.config['harness_reviews']['spec']=self.save('clear-harness.json',{'status':'clear','unresolved_findings':0,'files':self.config['harness_files']})
        self.config['native_capacity']=self.save('observed-no-inference.json',{'status':'native_capacity_observation','observed_at':'synthetic-offline','scope':'informational_not_reservation','usageAllowed':False,'credits':0})
        self.assertEqual(self.build()['native_capacity_observation'],self.config['native_capacity'])

    def test_complete_pins_build_in_memory_with_all_strict_gates_and_no_clock(self):
        result = self.build()
        self.assertEqual(result['source_repairs']['runtime_files'], self.runtime)
        self.assertEqual(result['limits']['max_calls'],420)
        self.assertEqual(result['limits']['max_observed_tokens'],7000000)
        self.assertEqual(result['limits']['wall_seconds'],7200)
        self.assertEqual(result['protocol_preparation']['planned_answer_records'],216)
        self.assertEqual(result['protocol_preparation']['candidate_min_primary_supported'],87)
        self.assertEqual(result['pass_criteria']['individual_required_fact_loss_max'],0)
        self.assertEqual(result['independent_review_contract']['metadata_receipt_sha256'],self.config['review_metadata']['sha256'])
        self.assertEqual(result['public_preflight']['selected_disclosed_cases'],self.guide['public_preflight']['selected_disclosed_cases'])
        self.assertFalse((self.repo/'run.json').exists())
        self.assertFalse((self.repo/'freeze.json').exists())
        self.assertFalse((self.repo/'protocol.json').exists())

    def test_missing_original_or_changed_original_or_nonclear_verdict_rejects(self):
        for role in ('tests','spec','standards'):
            path=Path(self.originals[role]['path']);original=path.read_bytes()
            with self.subTest(role=role,kind='missing'):
                path.unlink()
                with self.assertRaises(FileNotFoundError):self.build()
                path.write_bytes(original)
            with self.subTest(role=role,kind='changed'):
                path.write_text('{}')
                with self.assertRaisesRegex(ValueError,'pinned_file_changed'):self.build()
                path.write_bytes(original)
            with self.subTest(role=role,kind='bad_status_even_when_rebound'):
                value=json.loads(original);value['status']='failed'
                old_binding=deepcopy(self.config[role+'_binding'])
                self.originals[role]=self.save(role+'-original.json',value)
                self.config[role+'_binding']=self.save(role+'-binding-rejected.json',
                    {'status':'passed' if role=='tests' else 'clear','runtime_files':self.runtime,'original':self.originals[role]})
                with self.assertRaisesRegex(ValueError,'original_not_final'):self.build()
                path.write_bytes(original);self.config[role+'_binding']=old_binding

    def test_added_changed_missing_runtime_and_stale_final_bindings_reject(self):
        path=self.repo/'codex_mem/public_toy.py';raw=path.read_bytes()
        path.write_text('# changed\n')
        with self.assertRaisesRegex(ValueError,'runtime_receipt'):self.build()
        path.write_bytes(raw);extra=self.repo/'codex_mem/extra.py';extra.write_text('# omitted from receipt\n')
        with self.assertRaisesRegex(ValueError,'runtime_receipt'):self.build()
        extra.unlink();path.unlink()
        with self.assertRaisesRegex(ValueError,'runtime_receipt'):self.build()
        path.write_bytes(raw)
        binding=json.loads(Path(self.config['spec_binding']['path']).read_text());binding['runtime_files']={}
        self.config['spec_binding']=self.save('stale-spec.json',binding)
        with self.assertRaisesRegex(ValueError,'spec_binding_not_final'):self.build()

    def test_review_source_manifest_must_match_every_actual_runtime_file(self):
        path=self.repo/'source-manifest.json';value=json.loads(path.read_text());value['runtime_files']={}
        ref=self.save('changed-source-manifest.json',value)
        original=json.loads(Path(self.originals['standards']['path']).read_text());original['source_manifest']=ref
        original_ref=self.save('rebound-review.json',original)
        self.config['standards_binding']=self.save('rebound-binding.json',
            {'status':'clear','runtime_files':self.runtime,'original':original_ref})
        with self.assertRaisesRegex(ValueError,'source_manifest_differs'):self.build()

    def test_missing_test_logs_failed_tests_or_review_findings_reject(self):
        path=self.repo/'toy-tests.log';path.unlink()
        with self.assertRaises(FileNotFoundError):self.build()

    def test_failed_test_counts_and_review_findings_cannot_be_rebound_as_clear(self):
        for role, change in (('tests', {'failed':1}), ('tests', {'errors':1}),
                             ('tests', {'full_suite':False}), ('spec', {'unresolved_findings':1})):
            with self.subTest(role=role,change=change):
                original = json.loads(Path(self.originals[role]['path']).read_text())
                changed = dict(original, **change)
                original_ref = self.save('bad-'+role+'-original.json', changed)
                old = self.config[role+'_binding']
                self.config[role+'_binding'] = self.save('bad-'+role+'-binding.json',
                    {'status':'passed' if role=='tests' else 'clear','runtime_files':self.runtime,'original':original_ref})
                with self.assertRaises(ValueError):self.build()
                self.config[role+'_binding'] = old

    def test_missing_harness_or_changed_common_producer_cannot_be_called_final(self):
        key=builder.PREFIX+'test-driver.py';pin=self.config['harness_files'].pop(key)
        with self.assertRaisesRegex(ValueError,'harness_pins_incomplete'):self.build()
        self.config['harness_files'][key]=pin
        producer=self.repo/'codex_mem/episode_processor.py';producer.write_text('# changed producer\n')
        runtime=builder.runtime_files(self.repo)
        self.config['runtime_receipt']=self.save('runtime-rebound.json', {'status':'final_source_candidate','runtime_files':runtime})
        with self.assertRaisesRegex(ValueError,'common_producer_changed'):self.build()

    def test_cli_refuses_noncanonical_history_before_protocol_creation(self):
        wrong=self.save('wrong-history-input.json',{'historical_spend':{'path':'not-canonical.json','sha256':builder.sha(b'wrong report')}})
        output=self.repo/'must-not-create-protocol.json'
        argv=['builder','--inputs',wrong['path'],'--inputs-sha256',wrong['sha256'],'--output',str(output)]
        with patch('sys.argv',argv),self.assertRaisesRegex(ValueError,'R21_requires_exact_R20_canonical_history'):builder.main()
        self.assertFalse(output.exists())

    def test_cli_never_overwrites_existing_protocol_or_reads_inputs_on_that_path(self):
        output=self.repo/'protocol.json';output.write_text('preserve original')
        argv=['builder','--inputs',str(self.repo/'nonexistent.json'),'--inputs-sha256','0'*64,'--output',str(output)]
        with patch('sys.argv',argv),self.assertRaisesRegex(ValueError,'already_exists'):builder.main()
        self.assertEqual(output.read_text(),'preserve original')

    def test_rejected_unresolved_incomplete_and_text_leaking_metadata_reject(self):
        for change in ({'status':'changes_required'},{'unresolved_findings':1},{'case_text':'not metadata'}):
            with self.subTest(change=change):
                original=deepcopy(self.metadata);self.metadata.update(change);self.refresh_sealed_metadata()
                with self.assertRaises(ValueError):self.build()
                self.metadata=original;self.refresh_sealed_metadata()

    def test_builder_never_opens_sealed_directory_or_named_content(self):
        old_read=Path.read_bytes
        def guarded(path):
            if path.is_relative_to(self.repo/'never-open-sealed') or path.name in builder.SEALED_CONTENT:
                raise AssertionError('sealed content opened')
            return old_read(path)
        with patch.object(Path,'read_bytes',guarded):self.build()
        self.config['review_metadata']={'path':str(self.repo/'never-open-sealed/sealed-semantic-review-metadata.json'),'sha256':self.config['review_metadata']['sha256']}
        with self.assertRaisesRegex(ValueError,'sealed_content_must_not_be_opened'):self.build()

    def test_baseline_helper_producer_placeholder_or_missing_harness_reject(self):
        path=self.baseline/'codex_mem/module_0.py';path.write_text('# drift\n')
        with self.assertRaisesRegex(ValueError,'baseline_runtime'):self.build()
        with self.assertRaisesRegex(ValueError,'placeholder'):builder.digest('0'*64)
        with self.assertRaisesRegex(ValueError,'placeholder'):builder.digest('TODO')


if __name__=='__main__':unittest.main()
