"""Compare constructed public model requests to held R22 bytes. No dispatch."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import types

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2];sys.path.insert(0,str(ROOT))
PUBLIC=HERE
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def compare():
    old_path=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r22/frozen-helper.py'
    assert hashlib.sha256(old_path.read_bytes()).hexdigest()=='9e555332205828540cd7c44b3ac2becbc408bbc813ff5279b03726a720801d87'
    old=load('r23_request_old_helper',old_path)
    new=load('r23_request_new_helper',ROOT/'scripts/project_memory_consolidation_experiment.py')
    probe=load('r23_request_public_probe',PUBLIC/'prepare-reader-probe.py')
    test=load('r23_request_public_stubs',PUBLIC/'test-reader-contract-compatibility.py')
    corpus=new.read(PUBLIC/'public-reader-regressions.json')
    protocol=probe.default_protocol(corpus);protocol['pipeline_receipt_contract']='complete-shared-pipeline-v1';first=corpus['cases'][0]
    rows=[]
    def pair(name,left,right):
        a,b=old.serialized_request(left),new.serialized_request(right)
        assert a==b,name
        rows.append({'builder':name,'request_bytes_equal':True,'serialized_sha256':hashlib.sha256(a).hexdigest(),
                     'serialized_bytes':len(a),'model':right['model'],'reasoning_effort':right['reasoning_effort']})
    pair('legacy_reader_request',old.request('Public fictional context only.',old.object_schema({'answer':old.TEXT}),protocol),
         new.request('Public fictional context only.',new.object_schema({'answer':new.TEXT}),protocol))
    for mode in ('visible_union_v1','isolated_reader_v1','clause-support-v2','assertion-owned-citations-v1'):
        chosen=dict(protocol)
        if mode in ('visible_union_v1','isolated_reader_v1'):chosen.pop('reader_evidence_contract',None)
        else:chosen['reader_evidence_contract']=mode
        if mode=='assertion-owned-citations-v1':chosen['reader_fidelity_contract']='quantified-source-fidelity-v1'
        else:chosen.pop('reader_fidelity_contract',None)
        if mode=='visible_union_v1':chosen.pop('reader_context_isolation',None)
        args=([first['context']],first['original_source_fields'])
        oldcat=old.reader_citation_catalog(*args);newcat=new.reader_citation_catalog(*args)
        assert oldcat==newcat
        pair(mode,old.typed_reader_request([first['context']],oldcat,chosen),
             new.typed_reader_request([first['context']],newcat,chosen))
    protocol['reader_evidence_contract']='assertion-owned-citations-v1'
    protocol['reader_fidelity_contract']='quantified-source-fidelity-v1'
    prepared=probe.prepare(new,protocol,corpus);outputs=test.native_readers(prepared)
    payload_old=probe.judge_payload(old,protocol,corpus,outputs)
    payload_new=probe.judge_payload(new,protocol,corpus,outputs)
    assert payload_old==payload_new
    pair('witness_judge',old.judge_request(payload_old,protocol),new.judge_request(payload_new,protocol))
    legacy=dict(protocol);legacy.pop('judge_evidence_contract')
    pair('legacy_judge',old.judge_request(payload_old,legacy),new.judge_request(payload_new,legacy))
    from codex_mem import episode_processor as producer
    archive=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r22/frozen-product.tar.gz'
    with tarfile.open(archive) as tar:
        members=[m for m in tar.getmembers() if m.name.endswith('codex_mem/episode_processor.py')]
        assert len(members)==1
        raw=tar.extractfile(members[0]).read()
    assert hashlib.sha256(raw).hexdigest()==hashlib.sha256(Path(producer.__file__).read_bytes()).hexdigest()
    frozen=types.ModuleType('codex_mem._r20_public_request_comparison')
    frozen.__package__='codex_mem';frozen.__file__=producer.__file__
    exec(compile(raw,producer.__file__,'exec'),frozen.__dict__)
    snapshot={'project':'/public/fictional','session_id':'public-session','sources':[{'id':'public-source','seq':1,
        'body':'Keep the fictional labels manual.','source_role':'user_intent'}], 'state':{'claims':[],'episodes':[]}}
    pair('native_v12_generation',frozen.runner_request(snapshot,180),producer.runner_request(snapshot,180))
    return {'status':'constructed_public_request_bytes_equal_not_native_trial','old_helper_sha256':hashlib.sha256(old_path.read_bytes()).hexdigest(),
        'new_helper_sha256':hashlib.sha256(Path(new.__file__).read_bytes()).hexdigest(),'comparisons':rows,
        'actual_native_calls':0,'generator_producer_source_unchanged':True,
        'limit':'Only deterministic public inputs/modes listed; helper identity/source pins and reuse keys necessarily change. No quality assertion.'}

if __name__=='__main__':
    result=compare();(HERE/'request-byte-compatibility.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'request_builders_equal':len(result['comparisons']),'native_calls':0}))
