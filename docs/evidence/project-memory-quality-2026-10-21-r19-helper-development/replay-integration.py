"""Synthetic accounting replay against the actual copied helper; no native calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

ROOT=Path(__file__).resolve().parent
path=ROOT/'scripts/project_memory_consolidation_experiment.py'
spec=importlib.util.spec_from_file_location('isolated_actual_experiment',path)
helper=importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

def rows(name,total):
    return [{'call_id':name,'status':'started','model':'gpt-6-luna'},
            {'call_id':name,'status':'completed','metrics':{'usage':{'status':'reported','tokens':{
                'input_tokens':total-10,'cached_input_tokens':0,'cache_write_input_tokens':0,
                'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':total}}}}]

prior=helper.usage(rows('synthetic-known',100)+[{'call_id':'synthetic-unavailable','status':'started'}])
steps=[]
with tempfile.TemporaryDirectory(prefix='r19-offline-accounting-',dir='/private/tmp') as directory:
    previous=Path(directory)/'prior.json'
    for index,total in enumerate((200,50,25),1):
        helper.write(previous,{'usage':prior})
        protocol={'historical_spend':{'report':str(previous),'sha256':hashlib.sha256(previous.read_bytes()).hexdigest()}}
        current=helper.usage(rows('synthetic-round-'+str(index),total))
        prior=helper.historical_accounting(protocol,current)['cumulative_pilot_usage']
        steps.append({'round':index,'current_known_tokens':total,'cumulative':prior})
receipt={'status':'passed_synthetic_actual_consumer_replay','helper_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
         'actual_native_calls':0,'model_api_calls':0,'synthetic_only':True,'steps':steps,
         'expected_known_reported_totals':[300,350,375],
         'meaning':'Native attempt counters inside each cumulative object describe simulated records only, not actual dispatch or cost.'}
assert [step['cumulative']['reported_tokens_lower_bound']['total_tokens'] for step in steps]==[300,350,375]
assert all(step['cumulative']['tokens']['total_tokens'] is None and step['cumulative']['token_accounting_complete'] is False for step in steps)
(ROOT/'evidence/three-round-integration.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'status':receipt['status'],'actual_native_calls':0,'known_reported_totals':[300,350,375]}))
