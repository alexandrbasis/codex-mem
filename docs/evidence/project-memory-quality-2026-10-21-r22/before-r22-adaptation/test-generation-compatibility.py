"""Actual public producer commit with only fresh fictional scratch Stores/native stub."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('r20_generation_helper',ROOT/'scripts/project_memory_consolidation_experiment.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
from codex_mem import processor,episode_processor as ep
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


class PublicGenerationCompatibilityTests(unittest.TestCase):
    def test_real_capture_generate_decode_commit_and_reject_mixed_native_shape(self):
        with tempfile.TemporaryDirectory(prefix='r20-NONEXPERIMENT-producer-') as directory:
            run=Path(directory);project=run/'fictional-project';project.mkdir();data=run/'data'
            helper.configure_store(data,[project])
            protocol={'model':'gpt-6-luna','reasoning_effort':'medium','limits':{'max_calls':4,
                'max_observed_tokens':400000,'per_call_token_reservation':100000,'wall_seconds':7200,'call_seconds':180}}
            helper.write(run/'run.json',{'started_at':helper.time.time(),'budget_run':str(run)})
            capture=helper.Capture(run,protocol,'generation','baseline','public-synthetic-generation')
            with Store(data) as store:
                helper.capture_event(store,project,'one',{'id':'request','type':'user','text':'Keep the fictional labels manual.'})
            outputs=[]
            def native(request):
                payload=json.loads(request['prompt'].split('\n',1)[1]);span=payload['events'][0]['spans'][0]
                output={'episodes':[],'claims':[{'text':span['text'].strip(),'kind':'requirement','workflow_state':'open',
                    'scope':{'environment':'','version':''},'rationale':'',
                    'evidence':{'text':[span['ref']],'workflow_state':[span['ref']],'rationale':[]},'outgoing_relations':[]}]}
                if outputs:output['relations']=[] # A mixed legacy/native shape must be rejected atomically.
                outputs.append(output)
                return {'output':output,'evidence':{'thread_start':{'thread_id':'public-thread','model':'gpt-6-luna',
                    'reasoning_effort':'medium','model_provider':'openai'},'turn_started':{'thread_id':'public-thread','turn_id':'public-turn'},
                    'turn_completed':True,'no_tools':True,'rerouted':False},
                    'metrics':{'usage':{'status':'reported','tokens':{'input_tokens':90,'cached_input_tokens':0,
                        'cache_write_input_tokens':0,'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':100}}}}
            with patch.object(processor,'NativeProcessorRunner',return_value=native):
                results=helper.process(data,[project],capture,2)
                self.assertEqual([r['status'] for r in results],['processed','idle'])
                with Store(data) as store:
                    before=ProjectMemory(store).snapshot(project)
                    self.assertEqual(len(before['claims']),1)
                    self.assertEqual(before['claims'][0]['text'],'Keep the fictional labels manual.')
                    helper.capture_event(store,project,'two',{'id':'later','type':'user','text':'Keep the fictional review manual.'})
                failed=ep.process_project_memory(project,data,runner=capture,timeout=180)
                self.assertEqual(failed['status'],'failed')
                with Store(data) as store:self.assertEqual(ProjectMemory(store).snapshot(project)['claims'],before['claims'])
            for index,output in enumerate(outputs,1):
                self.assertEqual(helper.read(run/f'receipts/call-{index:04d}-response.json')['output'],output)
            self.assertEqual(len(outputs),2)
            self.assertEqual(helper.usage(helper.ledger_calls(run/'calls.jsonl'))['attempted_calls'],2)
            self.assertFalse((run/'freeze.json').exists())

if __name__=='__main__':unittest.main()
