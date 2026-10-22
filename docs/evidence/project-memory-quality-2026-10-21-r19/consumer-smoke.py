"""Offline actual-consumer smoke. Synthetic run only; never a trial clock/native call."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
# The emitted protocol supplies the reviewed prospective helper pin.
FORBIDDEN={'holdout-fixture.json','holdout-gold.json','sealed-semantic-review.json','author-fact-rationale.json','author.py'}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def require(condition,reason):
    if not condition:raise ValueError(reason)

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli_output_path(output):
    path=Path(output).resolve()
    require(path!=HERE and path.is_relative_to(HERE),'smoke_cli_output_must_be_inside_R19_evidence')
    return path


def smoke(protocol,output,source_root=REPO):
    """source_root is a toy dependency for unit tests; CLI always uses actual repo."""
    output=Path(output).resolve()
    if Path(source_root).resolve()==REPO:
        output=cli_output_path(output)
    else:
        require(output!=Path(source_root).resolve() and output.is_relative_to(Path(source_root).resolve()),
                'synthetic_smoke_output_must_be_inside_toy_root')
    require(not output.exists(),'smoke_output_already_exists')
    helper_path=REPO/'scripts/project_memory_consolidation_experiment.py'
    helper_sha=protocol['public_preflight']['file_sha256']['scripts/project_memory_consolidation_experiment.py']
    require(digest(helper_path)==helper_sha,'pinned_helper_required')
    sys.path.insert(0,str(REPO))
    helper=load('r18_smoke_unchanged_helper',helper_path)
    source_root=Path(source_root).resolve()
    require(helper.code_hash(source_root)==protocol['source_repairs']['runtime_files'],'runtime_pins_differ')
    pins=protocol['public_preflight']['file_sha256']
    require(pins.get('scripts/project_memory_consolidation_experiment.py')==helper_sha,'protocol_helper_pin_differs')
    sealed=Path(protocol['authorship']['private_directory']).resolve()
    require(not output.is_relative_to(sealed) and not sealed.is_relative_to(output),
            'smoke_output_must_not_overlap_sealed_directory')
    observed={}
    for name,expected in pins.items():
        path=(source_root/name).resolve()
        require(path.name not in FORBIDDEN and not path.is_relative_to(sealed),'smoke_must_not_open_sealed_content')
        require(digest(path)==expected,'source_pin_changed:'+name)
        observed[str(path)]=expected
    report_path=helper.ROOT/protocol['historical_spend']['report']
    require(report_path.name not in FORBIDDEN and not report_path.resolve().is_relative_to(sealed),
            'historical_pointer_must_not_open_sealed_content')
    historical=helper.historical_accounting(protocol,helper.usage([]))
    helper.assert_baseline_runtime(Path(protocol['baseline_runtime_root']),protocol)
    public_toys=load('r18_smoke_public_toy_fixture',HERE/'test_public_fixture_contract_v2.py')
    fixture,gold=public_toys.toy()
    repeats=[*protocol['regeneration_repeat_history_ids'],*protocol['reader_repeat_history_ids']]
    require(len(repeats)==2 and len(set(repeats))==2,'smoke_requires_one_of_each_repeat')
    names=repeats+[f'consumer_smoke_{index:02d}' for index in range(2,16)]
    require(len(set(names))==16,'synthetic_smoke_id_collision')
    remapped={}
    for history,new in zip(fixture['histories'],names):
        old=history['id'];history['id']=new
        for question in history['questions']:
            remapped[new+'/'+question['id']]=gold['questions'][old+'/'+question['id']]
    gold['questions']=remapped
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='r18-consumer-smoke-NONEXPERIMENT-',dir='/private/tmp') as directory:
        run=Path(directory)/'synthetic-run';run.mkdir()
        helper.write(run/'run.json',{'started_at':0,'budget_run':str(run),
            'purpose':'NONEXPERIMENT synthetic consumer compatibility only; not an actual native run clock'})
        gold_path=Path(directory)/'synthetic-public-gold.json';helper.write(gold_path,gold)
        args=types.SimpleNamespace(run=run,destination=output/'canonical-export',gold=gold_path)
        report=helper.aggregate(args,fixture,protocol)
        require(report['decision']=='incomplete' and all(report['variants'][arm]['questions_planned']==96
                and report['variants'][arm]['questions_graded']==0 for arm in ('baseline','candidate')),
                'all_missing_aggregate_denominator_changed')
        exported=helper.export_evidence(args,fixture,protocol)
        questions=helper.read(args.destination/'per-question.json')['questions']
        attempts=[attempt for question in questions for attempt in question['attempts']]
        require(len(questions)==96 and len(attempts)==216
                and all(attempt['status']=='missing_counts_as_failure' and attempt['answer'] is None and attempt['grade'] is None
                        for attempt in attempts),'all_missing_export_denominator_changed')
        require((args.destination/'historical-report.json').read_bytes()==report_path.read_bytes(),
                'canonical_history_copy_differs')
        manifest=helper.read(args.destination/'export-manifest.json')
        for name,expected in manifest['files'].items():
            require(digest(args.destination/name)==expected,'export_manifest_file_differs')
        with tarfile.open(args.destination/'fictional-run-evidence.tar.gz','r:gz') as archive:
            require('run.json' in archive.getnames(),'synthetic_archive_missing_run_metadata')
        require(not (run/'calls.jsonl').exists() and not list(run.rglob('*.sqlite*')),'unexpected_native_or_store_artifacts')
        for name,expected in observed.items():require(digest(Path(name))==expected,'source_changed_during_smoke')
        receipt={'status':'passed_actual_unchanged_consumer_smoke_not_trial_readiness',
            'protocol_object_sha256':helper.digest(protocol),'helper_sha256':helper_sha,
            'source_root':str(source_root),'source_files_verified':len(observed),
            'historical_report_sha256':digest(report_path),'historical_accounting':historical,
            'aggregate_decision':report['decision'],'canonical_export_decision':exported['decision'],
            'question_records':96,'planned_missing_answer_records':216,'graded_answers':0,
            'native_attempts':0,'store_calls':0,'actual_trial_clock_started':False,'actual_freeze_created':False,
            'synthetic_run_metadata':'Temporary NONEXPERIMENT directory, started_at0 sentinel, removed after smoke.',
            'fresh_author_inputs_opened':False,'fixture_source':'Existing public mechanical-contract toy only; opaque repeat IDs from protocol.',
            'export_manifest_sha256':digest(args.destination/'export-manifest.json'),
            'archive_sha256':digest(args.destination/'fictional-run-evidence.tar.gz'),
            'limitations':'Consumer compatibility only. Missing synthetic slots are not real trial outcomes. No old answer is rescored; review metadata remains attestation.'}
    helper.write(output/'consumer-smoke-receipt.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol',type=Path,required=True)
    parser.add_argument('--protocol-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output=cli_output_path(args.output)
    raw=args.protocol.read_bytes()
    require(hashlib.sha256(raw).hexdigest()==args.protocol_sha256,'emitted_protocol_bytes_changed')
    protocol=json.loads(raw)
    receipt=smoke(protocol,args.output)
    receipt['protocol_file']={'path':str(args.protocol.resolve()),'sha256':args.protocol_sha256}
    (args.output/'consumer-smoke-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'status':receipt['status'],'receipt':str(args.output/'consumer-smoke-receipt.json'),
                      'native_attempts':0,'actual_trial_clock_started':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())
