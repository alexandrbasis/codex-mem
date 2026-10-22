"""Build prospective R22 pins from final contents-free evidence. Never run/freeze."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

# The final prospective helper must be explicitly pinned after source reviews.
DRIVER_SHA = '46296af7d87ae092f77946a5d8868129d375026821de0b242d620cbf4dc6bf8c'
PRODUCER_SHA = 'e80bb9f6c0adbe41c03bee3e63f6cf9d44bfa2895fa6a4369ebdeaeda504c92a'
HISTORICAL_REPORT = 'docs/evidence/project-memory-quality-2026-10-21-r21/holdout/report.json'
HISTORICAL_REPORT_SHA = '945747db507fb1e02740d92adcdd81f39ad1071fc4b8686c555e89fbf915b76d'
GUIDE_SHA = '94b2cb4c826762af984099d8a7b653dba72e3296fe27540dfbc2046a0653bfa8'
HERE = Path(__file__).resolve().parent
PREFIX = 'docs/evidence/project-memory-quality-2026-10-21-r22/'
PUBLIC = ('run-experiment.py', 'test-driver.py', 'run-public-preflight.py', 'test-public-preflight.py',
          'prepare-reader-probe.py', 'public-calibration-library.py', 'public-reader-regressions.json',
          'judge-contract-controls.json', 'public-fixture-contract.py', 'public-fixture-contract-receipt.json',
          'prospective-gold-semantics-contract.json', 'sealed-semantic-review-metadata.schema.json',
          'sealed-semantic-review-findings.schema.json', 'public-contract-pack-receipt-v2.json',
          'public-contract-spec-review-v2.json', 'public-contract-standards-review-v2.json',
          'build-protocol.py', 'test-protocol-builder.py', 'protocol-builder-input-contract.json',
          'test_public_fixture_contract_v2.py', 'test-consumer-compatibility.py',
          'consumer-smoke.py', 'protocol-consumer-contract.json', 'test-reader-contract-compatibility.py', 'test-canonical-history-consumer.py',
          'test-generation-compatibility.py', 'request-byte-compatibility.py',
          'legacy-public-reader-regressions.json', 'source-audit-bindings.py', 'test-source-audit-bindings.py', 'test-public-fidelity-pack.py')
SEALED_CONTENT = {'holdout-fixture.json', 'holdout-gold.json', 'sealed-semantic-review.json',
                  'author-fact-rationale.json', 'author.py'}
STABLE = ('schema_version', 'baseline_commit', 'model', 'reasoning_effort', 'jev', 'data_policy', 'histories',
          'context_chars', 'retrieval', 'comparison', 'reader', 'judge', 'limits', 'planned_call_allocation',
          'pass_criteria', 'critical_errors', 'full_supported_answer', 'metrics', 'failure_policy', 'freeze_policy',
          'holdout_primary_questions', 'grouped_reader_dependence', 'evidence_roles', 'capture_fidelity',
          'retrieval_timing', 'export_privacy', 'criteria_source', 'implementation_base_commit',
          'dispatch_wall_reservation', 'comparison_mode', 'candidate_processing_mode', 'reader_citation_contract',
          'paired_native_request_reuse', 'paired_reuse_policy', 'judge_evidence_contract', 'judge_schema_version',
          'judge_profile', 'reader_context_isolation', 'explicit_project_scope_context', 'reader_evidence_contract',
          'reader_profile', 'shared_generation_runtime', 'external_stop_policy')


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def digest(value) -> str:
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
            and len(set(value)) > 1, 'missing_or_placeholder_sha256')
    return value


class Pins:
    def __init__(self, repo: Path, sealed_directory: Path):
        self.repo, self.sealed = repo.resolve(), sealed_directory.resolve()
        self.files = {}

    def path(self, name: str) -> Path:
        path = Path(name)
        path = (path if path.is_absolute() else self.repo / path).resolve()
        require(path.name not in SEALED_CONTENT and not path.is_relative_to(self.sealed),
                'sealed_content_must_not_be_opened')
        return path

    def read(self, ref: dict, *, parse: bool = True):
        require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'}, 'invalid_file_pin')
        path = self.path(ref['path'])
        raw = path.read_bytes()
        expected = digest(ref['sha256'])
        require(sha(raw) == expected, 'pinned_file_changed:' + str(path))
        self.files[str(path)] = expected
        return json.loads(raw) if parse else raw

    def mapping(self, files: dict) -> None:
        require(isinstance(files, dict) and bool(files), 'empty_file_mapping')
        for path, pin in files.items():
            self.read({'path': path, 'sha256': pin}, parse=False)


def runtime_files(root: Path) -> dict:
    return {str(path.relative_to(root)): sha(path.read_bytes())
            for path in sorted((root / 'codex_mem').glob('*.py'))}


def evidence(pins: Pins, ref: dict, runtime: dict, role: str) -> dict:
    """Bindings cannot replace an original machine verdict and source manifest."""
    binding = pins.read(ref)
    verdict = 'passed' if role == 'tests' else 'clear'
    require(binding.get('status') == verdict and binding.get('runtime_files') == runtime,
            role + '_binding_not_final')
    original = pins.read(binding['original'])
    require(original.get('status') == verdict and original.get('runtime_files') == runtime,
            role + '_original_not_final')
    if role == 'tests':
        require(type(original.get('tests_run')) is int and original['tests_run'] > 0
                and type(original.get('failed')) is int and original['failed'] == 0
                and type(original.get('errors')) is int and original['errors'] == 0
                and original.get('full_suite') is True and original.get('acceptance_hooks') is True
                and original.get('protocol_tests') is True, 'final_test_gates_missing')
        pins.mapping(original['evidence_files'])
    else:
        require(type(original.get('unresolved_findings')) is int and original['unresolved_findings'] == 0,
                role + '_unresolved_findings')
        manifest = pins.read(original['source_manifest'])
        require(manifest.get('runtime_files') == runtime, role + '_source_manifest_differs')
    return binding


def build_protocol(config: dict, repo: Path, guide: dict) -> dict:
    """Pure preparation seam. CLI supplies the fixed, pinned historical guide."""
    require(config.get('schema_version') == 1, 'invalid_builder_config')
    pins = Pins(repo, Path(config['sealed_directory']))
    final = pins.read(config['runtime_receipt'])
    runtime = runtime_files(repo)
    require(final.get('status') == 'final_source_candidate' and final.get('runtime_files') == runtime
            and bool(runtime), 'runtime_receipt_not_final_or_files_changed')
    require(runtime.get('codex_mem/episode_processor.py') == PRODUCER_SHA, 'common_producer_changed')
    pins.mapping(runtime)
    for role in ('tests', 'spec', 'standards'):
        evidence(pins, config[role + '_binding'], runtime, role)
    baseline = Path(config['baseline_runtime_root']).resolve()
    require(runtime_files(baseline) == guide['baseline_runtime_hashes']
            and len(guide['baseline_runtime_hashes']) == 47, 'baseline_runtime_not_exact_47')
    require(guide['baseline_commit'] == 'dd60f1c8bc7788b856e80b626975b2f609aa394a', 'wrong_baseline_commit')
    harness = config['harness_files']
    require(isinstance(harness, dict) and all(PREFIX + name in harness for name in PUBLIC), 'harness_pins_incomplete')
    pins.mapping(harness)
    require(harness.get('scripts/project_memory_consolidation_experiment.py') == digest(config['helper_sha256'])
            and harness.get(PREFIX + 'run-experiment.py') == DRIVER_SHA, 'held_helper_or_driver_changed')
    for name in ('judge-contract-controls.json',
                 'public-calibration-library.py'):
        prior = 'docs/evidence/project-memory-quality-2026-10-21-r16/' + name
        require(harness[PREFIX + name] == guide['public_preflight']['file_sha256'][prior],
                'carried_public_regression_changed:' + name)
    for role in ('spec', 'standards'):
        review = pins.read(config['harness_reviews'][role])
        require(review.get('status') == 'clear' and type(review.get('unresolved_findings')) is int
                and review['unresolved_findings'] == 0 and review.get('files') == harness,
                'harness_review_not_final:' + role)
    capacity = pins.read(config['native_capacity'])
    require(capacity.get('status') == 'native_capacity_observation'
            and isinstance(capacity.get('observed_at'), str) and bool(capacity['observed_at'].strip())
            and capacity.get('scope') == 'informational_not_reservation', 'native_capacity_observation_required')
    reader_contract = config['reader_evidence_contract']
    require(reader_contract in ('clause-support-v2', 'assertion-owned-citations-v1'), 'unsupported_reader_contract')
    legacy = pins.read({'path':PREFIX+'legacy-public-reader-regressions.json',
                        'sha256':harness[PREFIX+'legacy-public-reader-regressions.json']})
    require(harness[PREFIX+'legacy-public-reader-regressions.json'] == guide['public_preflight']['file_sha256'][
                'docs/evidence/project-memory-quality-2026-10-21-r16/public-reader-regressions.json'], 'carried_six_cases_changed')
    corpus = pins.read({'path':PREFIX+'public-reader-regressions.json', 'sha256':harness[PREFIX+'public-reader-regressions.json']})
    require(len(corpus.get('cases', [])) == 8 and corpus['cases'][:6] == legacy['cases']
            and corpus.get('planned_native_calls', {}).get('total') == 10, 'eight_public_case_contract_changed')
    require(config.get('reader_fidelity_contract') == 'quantified-source-fidelity-v1'
            and reader_contract == 'assertion-owned-citations-v1', 'R22_requires_reader_fidelity_contract')
    repeats_receipt = pins.read(config['repeat_selection'])
    require(config['repeat_selection']['sha256'] == '5a49eaf99c427749eae49d1c3c41aac9d64f7131e5fef8f33b2b11e6d20c05e5'
            and repeats_receipt.get('regeneration_repeat_history_ids') == ['h07']
            and repeats_receipt.get('reader_repeat_history_ids') == ['h15'], 'prospective_repeat_selection_changed')
    resources = pins.read(config['resource_counts'])
    require(set(resources) == {'schema_version','status','history_ids','question_ids','counts','author_receipt'}
            and type(resources['schema_version']) is int and resources['schema_version'] == 1 and resources['status'] == 'counts_only_author_resources', 'resource_counts_schema')
    require(resources['history_ids'] == [f'h{i:02d}' for i in range(1,17)]
            and resources['question_ids'] == [f'q{i}' for i in range(1,7)], 'resource_stable_ids_changed')
    counts = resources['counts']
    count_keys = {'histories','questions','planned_answer_slots','sessions','events','user_events','assistant_events',
                  'tool_events','deletions','checkpoints','source_chars','max_history_source_chars',
                  'max_history_events','max_history_sessions','max_history_checkpoints','estimated_generation_jobs','required_facts'}
    require(isinstance(counts, dict) and set(counts) <= count_keys and all(type(v) is int and v >= 0 for v in counts.values())
            and counts.get('histories') == 16 and counts.get('questions') == 96
            and counts.get('planned_answer_slots') == 216, 'resource_counts_must_be_counts_only')
    require(all(type(counts.get(key)) is int and 0 < counts[key] <= maximum for key, maximum in
                (('events',144),('sessions',48),('checkpoints',32),('max_history_events',9),('max_history_sessions',3),('max_history_checkpoints',2))),
            'resource_per_history_or_total_cap_exceeded_or_missing')
    pins.read(resources['author_receipt'], parse=False)
    budget = pins.read(config['budget_binding'])
    require(set(budget) == {'schema_version','status','resource_counts','limits','planned_call_allocation','public_native_calls'}
            and type(budget['schema_version']) is int and budget['schema_version'] == 1 and budget['status'] == 'approved_bounded_budget'
            and budget['resource_counts'] == config['resource_counts'] and budget['public_native_calls'] == 10,
            'approved_resource_bound_budget_required')
    limits, allocation = budget['limits'], budget['planned_call_allocation']
    require(isinstance(limits, dict) and set(limits) == set(guide['limits'])
            and all(type(v) is int and v > 0 for v in limits.values())
            and limits['call_seconds'] == 180 and limits['per_call_token_reservation'] == 100000
            and limits['wall_seconds'] == 7200 and limits['max_observed_tokens'] == 7000000
            and limits['max_calls'] == 420
            and limits['jobs_per_checkpoint'] == guide['limits']['jobs_per_checkpoint']
            and limits['jobs_per_consolidation'] == guide['limits']['jobs_per_consolidation'],
            'invalid_explicit_bounded_limits')
    require(isinstance(allocation, dict) and set(allocation) == {'main_generation','regeneration_generation','isolated_readers','judges','public_readers','public_judges','explicit_reserve'}
            and all(type(v) is int and v >= 0 for v in allocation.values())
            and allocation['isolated_readers'] == 216 and allocation['judges'] == 36
            and allocation['main_generation'] == 144 and counts.get('events') <= 144
            and allocation['regeneration_generation'] == 9 and counts.get('max_history_events') <= 9
            and allocation['public_readers'] == 8 and allocation['public_judges'] == 2
            and allocation['explicit_reserve'] == 5 and sum(allocation.values()) == limits['max_calls'],
            'invalid_explicit_call_allocation')
    metadata = pins.read(config['review_metadata'])
    author = pins.read(config['author_manifest'])
    require(resources['author_receipt']['sha256'] == config['author_manifest']['sha256'], 'resource_author_version_not_final')
    sealed = pins.read(config['sealed_pins'])
    readiness = pins.read(config['review_readiness'])
    spend = pins.read(config['historical_spend'])
    require(metadata.get('status') == 'accepted' and type(metadata.get('unresolved_findings')) is int
            and metadata['unresolved_findings'] == 0
            and metadata.get('independence_attestation') == 'independent_author_inputs_only_no_product_prior_inputs_answers_or_grades',
            'independent_review_not_accepted')
    require(set(metadata) == {'schema_version', 'status', 'unresolved_findings', 'fixture_sha256',
            'gold_sha256', 'mechanical_contract_sha256', 'semantic_guidance_sha256',
            'sealed_review_receipt_sha256', 'independence_attestation', 'coverage'}
            and type(metadata.get('schema_version')) is int and metadata['schema_version'] == 1,
            'review_metadata_not_strict_schema')
    coverage = metadata.get('coverage', {})
    if 'required_facts' in counts:
        require(counts['required_facts'] == coverage.get('required_facts'), 'resource_fact_count_not_final')
    require(isinstance(coverage, dict) and set(coverage) == {'questions', 'required_facts',
            'all_questions_reviewed', 'all_required_facts_reviewed', 'negative_cases_reviewed'},
            'review_metadata_coverage_shape')
    require(type(coverage.get('questions')) is int and coverage['questions'] == 96
            and type(coverage.get('required_facts')) is int and coverage['required_facts'] > 0
            and all(coverage.get(key) is True for key in ('all_questions_reviewed', 'all_required_facts_reviewed', 'negative_cases_reviewed')),
            'independent_review_coverage_incomplete')
    files = sealed.get('files', {})
    require(isinstance(files, dict) and bool(files), 'sealed_pins_empty')
    for name, pin in files.items():
        require(isinstance(name, str) and name not in ('', '.', '..') and '/' not in name and '\\' not in name,
                'sealed_pin_not_basename')
        digest(pin)
    for name in ('holdout-fixture.json', 'holdout-gold.json', 'sealed-semantic-review.json', 'sealed-semantic-review-metadata.json'):
        digest(files.get(name))
    for field, name in (('fixture_sha256', 'holdout-fixture.json'), ('gold_sha256', 'holdout-gold.json'),
                        ('sealed_review_receipt_sha256', 'sealed-semantic-review.json')):
        require(metadata.get(field) == files[name] == author['files'].get(name), 'sealed_review_input_binding_differs')
    require(config['review_metadata']['sha256'] == files['sealed-semantic-review-metadata.json']
            == author['files'].get('sealed-semantic-review-metadata.json'), 'review_metadata_bytes_not_sealed')
    require(metadata.get('mechanical_contract_sha256') == harness[PREFIX + 'public-fixture-contract.py']
            and metadata.get('semantic_guidance_sha256') == harness[PREFIX + 'prospective-gold-semantics-contract.json'],
            'review_public_contract_differs')
    require(sealed.get('counts', {}).get('questions') == 96
            and sealed['counts'].get('required_facts') == coverage['required_facts']
            and readiness.get('status') == 'accepted_sealed_semantic_review_metadata_only_not_trial_readiness'
            and readiness.get('metadata_sha256') == config['review_metadata']['sha256']
            and readiness.get('pins_sha256') == config['sealed_pins']['sha256']
            and readiness.get('manifest_sha256') == config['author_manifest']['sha256']
            and author.get('counts', {}).get('questions') == 96
            and author['counts'].get('required_facts') == coverage['required_facts']
            and readiness.get('review_unresolved_findings') == 0, 'metadata_readiness_binding_differs')
    repeats = config['repeats']
    regen, reader = repeats['regeneration'], repeats['reader']
    require((regen, reader) == ('h07','h15'), 'prospective_repeat_ids_changed')
    require(isinstance(regen, str) and isinstance(reader, str) and regen != reader
            and all(re.fullmatch('[A-Za-z0-9_-]{1,100}', value) and value.lower() not in ('todo', 'unknown', 'placeholder', 'pending')
                    for value in (regen, reader)), 'invalid_explicit_repeat_ids')
    result = {key: deepcopy(guide[key]) for key in STABLE}
    # These descriptions identify the current trial, not historical outcomes.
    for key in ('reader', 'grouped_reader_dependence'):
        result[key] = result[key].replace('R16', 'R22')
    result.update(pipeline_receipt_contract='complete-shared-pipeline-v1', limits=limits, planned_call_allocation=allocation, reader_fidelity_contract=config['reader_fidelity_contract'], reader_evidence_contract=reader_contract, native_capacity_observation=config['native_capacity'], experiment='project-memory-quality-20261021-r22', driver_sha256=DRIVER_SHA,
        baseline_runtime_root=str(baseline), baseline_runtime_hashes=guide['baseline_runtime_hashes'],
        repeat_history_ids=[regen, reader], regeneration_repeat_history_ids=[regen], reader_repeat_history_ids=[reader],
        blind_shuffle_seed='project-memory-quality-r22-fixed-before-native',
        repeats='Regenerate one fresh shared prefix for '+regen+' and repeat isolated readers for '+reader+'. All primary and repeated questions remain required.',
        budget_note='All public, primary and repeat attempts share one original clock and ledger. No retry, grade repair or post-freeze edits. Actual billing remains unknown.',
        historical_spend={'report':config['historical_spend']['path'], 'sha256':config['historical_spend']['sha256'], 'usage_field':'cumulative_pilot_usage'},
        source_repairs={'status':'final_source_candidate_not_quality_acceptance', 'runtime_receipt':config['runtime_receipt'],
                        'runtime_files':runtime, 'common_producer_sha256':PRODUCER_SHA},
        prospective_author_contract={'module':PREFIX+'public-fixture-contract.py','sha256':metadata['mechanical_contract_sha256']},
        authorship={'manifest':config['author_manifest'], 'pins':config['sealed_pins'],
                    'private_directory':str(pins.sealed), 'formal_release':'Sealed content stays closed until actual freeze and successful public gate and author formal release.'})
    require(result['model'] == 'gpt-6-luna' and result['reasoning_effort'] == 'medium'
            and result['reader_profile'] == {'model':'gpt-6.1-sol','reasoning_effort':'medium'}
            and result['judge_profile'] == {'model':'gpt-6-astra','reasoning_effort':'medium'}
            and result['context_chars'] == 6000, 'fixed_profiles_or_budget_changed')
    require(result['pass_criteria']['critical_errors_max'] == 0
            and result['pass_criteria']['candidate_fully_supported_min'] == 0.9
            and result['pass_criteria']['all_questions_must_be_graded'] is True
            and result['pass_criteria']['constraint_obligation_reason_recall_regression_max'] == 0,
            'strict_quality_gates_changed')
    result['pass_criteria']['individual_required_fact_loss_max'] = 0
    result['independent_review_contract'] = {'schema_version':1,
        'metadata_receipt_basename':'sealed-semantic-review-metadata.json',
        'metadata_receipt_sha256':config['review_metadata']['sha256'],
        'metadata_schema_sha256':harness[PREFIX+'sealed-semantic-review-metadata.schema.json'],
        **{key:metadata[key] for key in ('fixture_sha256','gold_sha256','mechanical_contract_sha256','semantic_guidance_sha256','sealed_review_receipt_sha256')}}
    result['public_preflight'] = deepcopy(guide['public_preflight'])
    result['public_preflight'].update(reader_fidelity_contract=config['reader_fidelity_contract'], fresh_reader_calls=8, planned_native_calls=10, reader_contract=reader_contract, attempt_id='r22-public-v1', file_sha256={**runtime, **harness})
    result['public_preflight']['gate'] = result['public_preflight']['gate'].replace('Six fresh', 'Eight fresh')
    result['reader'] += ' The explicit quantified-source-fidelity-v1 mode preserves relevant quantifiers, negation, necessary conditions and stated causal links in delivered assertions with speaker/scope, and avoids unrelated current status. Schema and grading remain unchanged.'
    result['public_preflight']['independence_limit'] = ('Selected disclosed regressions and a fresh known-control packet only, not independent quality evidence. Prospective helper/reader bytes require fresh public validation. No native success is carried forward.')
    if reader_contract == 'assertion-owned-citations-v1':
        result['reader'] = result['reader'].replace('The optional clause-support-v2 contract requires a cited basis for every factual clause and causal or event-identity relation.', 'The assertion-owned-citations-v1 contract requires each delivered assertion to own exact visible source refs. Code joins only assertion text and resolves only those refs; auxiliary check text and refs never enter the answer or earn credit. Citation ownership is structural and does not prove semantic support; every factual clause and causal or event-identity relation still requires evidence.')
    result['judge_calibration'] = deepcopy(guide['judge_calibration'])
    result['judge_calibration'].update(attempt_id='r22-public-v1', controls=PREFIX+'judge-contract-controls.json',
        library=PREFIX+'public-calibration-library.py')
    result['protocol_preparation'] = {'status':'pins_only_not_clock_freeze_or_trial_readiness',
        'builder_sha256':sha(Path(__file__).read_bytes()), 'input_pins':pins.files,
        'repeat_selection':config['repeat_selection'], 'resource_counts':config['resource_counts'], 'budget_binding':config['budget_binding'],
        'public_native_calls':10, 'harness_review_bindings':config['harness_reviews'], 'helper_sha256':config['helper_sha256'],
        'capacity_limit':'Known observation only; not a reservation or guarantee of future availability. Recheck before actual dispatch.',
        'final_gate_bindings':{role:config[role+'_binding'] for role in ('tests','spec','standards')},
        'sealed_content_opened':False,'actual_input_counts_recomputed':False,
        'count_limit':'After permitted release the held driver recomputes exact pinned fixture/gold totals. Metadata remains an attestation.',
        'planned_answer_records':216, 'candidate_min_primary_supported':87, 'any_individual_fact_loss_max':0,
        'critical_errors_including_repeats_max':0}
    require(runtime_files(repo) == runtime and runtime_files(baseline) == guide['baseline_runtime_hashes'],
            'runtime_changed_during_protocol_preparation')
    for path, expected in pins.files.items():
        require(sha(Path(path).read_bytes()) == expected, 'pinned_file_changed_during_preparation:' + path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--inputs-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'protocol_output_already_exists')
    raw = args.inputs.read_bytes()
    require(sha(raw) == digest(args.inputs_sha256), 'builder_inputs_changed')
    repo = HERE.parents[2]
    guide_path = repo / 'docs/evidence/project-memory-quality-2026-10-21-r16/protocol.json'
    guide_raw = guide_path.read_bytes()
    require(sha(guide_raw) == GUIDE_SHA, 'historical_protocol_guide_changed')
    config = json.loads(raw)
    historical = config['historical_spend']
    report_path = Path(historical['path'])
    report_path = (report_path if report_path.is_absolute() else repo / report_path).resolve()
    require(report_path == (repo / HISTORICAL_REPORT).resolve()
            and historical['sha256'] == HISTORICAL_REPORT_SHA, 'R22_requires_exact_R21_canonical_history')
    protocol = build_protocol(config, repo, json.loads(guide_raw))
    protocol['protocol_preparation']['builder_inputs'] = {'path':str(args.inputs.resolve()), 'sha256':args.inputs_sha256}
    with args.output.open('x') as target:
        target.write(json.dumps(protocol, indent=2) + '\n')
    print(json.dumps({'protocol_sha256':sha(args.output.read_bytes()), 'clock_started':False, 'frozen':False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
