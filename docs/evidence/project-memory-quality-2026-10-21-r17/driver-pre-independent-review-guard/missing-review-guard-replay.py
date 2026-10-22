"""Replay copied pre-guard main with inert helper/native stubs only."""
import hashlib,importlib.util,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
BASE=Path.cwd()/'docs/evidence/project-memory-quality-2026-10-21-r17'
spec=importlib.util.spec_from_file_location('held_driver',BASE/'driver-pre-independent-review-guard/run-experiment.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
spec=importlib.util.spec_from_file_location('public_toy',BASE/'test_public_fixture_contract_v2.py');toy=importlib.util.module_from_spec(spec);spec.loader.exec_module(toy)
class MissingGuard(unittest.TestCase):
 def runTest(self):
  with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
   run=Path(temporary);fp=run/'holdout-fixture.json';gp=run/'holdout-gold.json';fixture,gold=toy.toy();fp.write_text(json.dumps(fixture));gp.write_text(json.dumps(gold))
   (run/'sealed-semantic-review-metadata.json').write_text('{"status":"changes_required","unresolved_findings":1}')
   protocol={'driver_sha256':hashlib.sha256(Path(old.__file__).read_bytes()).hexdigest(),'comparison_mode':'shared-prefix','candidate_processing_mode':'read_only_snapshot','regeneration_repeat_history_ids':[], 'independent_review_contract':{'schema_version':1},'prospective_author_contract':{'sha256':'0'*64},'limits':{}}
   journal=[];dispatch=[]
   helper=SimpleNamespace(read=lambda path:protocol if path.name=='protocol.json' else {'started_at':time.time()},fixture=lambda path:fixture,append=lambda path,row:journal.append(row),ledger_calls=lambda path:[],usage=lambda calls:{'token_accounting_complete':True,'attempted_calls':0,'tokens':{'total_tokens':0}})
   original_spec,original_module=old.importlib.util.spec_from_file_location,old.importlib.util.module_from_spec
   helper_spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))
   def find_spec(name,path):return helper_spec if name=='experiment' else original_spec(name,path)
   def make_module(spec):return helper if spec is helper_spec else original_module(spec)
   def inert_dispatch(command,**kwargs):dispatch.append(command[2]);return SimpleNamespace(returncode=0)
   argv=['driver','--run',str(run),'--baseline','/private/tmp/public-baseline','--fixture',str(fp),'--gold',str(gp)]
   # The already-tested mechanical guard and all external/native work are stubs.
   # This checks only whether old main enforces a declared review before journal.
   with patch.object(old.sys,'argv',argv),patch.dict(old.os.environ,{'CODEX_MEM_DISABLED':'0'}),patch.object(old.importlib.util,'spec_from_file_location',side_effect=find_spec),patch.object(old.importlib.util,'module_from_spec',side_effect=make_module),patch.object(old.subprocess,'run',side_effect=inert_dispatch),patch.object(old,'validate_author_inputs',return_value=None),patch.object(old,'phase_plan',return_value=[]):old.main()
   self.assertEqual(journal, [], 'pre-guard main starts phase journal despite rejected declared review')
result=unittest.TextTestRunner().run(unittest.TestSuite([MissingGuard()]));raise SystemExit(not result.wasSuccessful())
