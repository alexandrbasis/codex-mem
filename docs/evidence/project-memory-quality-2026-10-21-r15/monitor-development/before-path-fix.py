from pathlib import Path
import importlib.util,json,collections,time,hashlib,sys
root=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public')
sys.path.insert(0,str(root))
run=Path('/private/tmp/codex-mem-quality-20261021-r15-experiment')
s=importlib.util.spec_from_file_location('r15_status_helper',root/'scripts/project_memory_consolidation_experiment.py')
h=importlib.util.module_from_spec(s);s.loader.exec_module(h)
if (run/'freeze.json').exists():
 f=h.read(run/'freeze.json');assert f['helper_sha256']==hashlib.sha256(Path(h.__file__).read_bytes()).hexdigest()
else:
 protocol=h.read(root/'docs/evidence/project-memory-quality-2026-10-21-r14/protocol.json')
 assert h.read(run/'run.json')['protocol_sha256']==h.digest(protocol)
u=h.usage(h.ledger_calls(run/'calls.jsonl'))
generations={}
for arm in ('baseline','candidate'):
 counts=collections.Counter()
 for i in range(1101,1117):
  p=run/arm/f'hold_{i}'/'generation.json'
  counts[h.read(p).get('status','unknown') if p.exists() else 'missing']+=1
 generations[arm]=dict(counts)
readers={arm:len(list((run/arm).glob('hold_*/reader-0.q-*.json'))) for arm in ('baseline','candidate')}
judges={arm:len(list((run/arm).glob('hold_*/judge-0.json'))) for arm in ('baseline','candidate')}
phase=None
p=run/'baseline/holdout-phase-execution.jsonl'
if p.exists():
 lines=p.read_text().splitlines()
 if lines:
  value=json.loads(lines[-1]);phase={k:v for k,v in value.items() if k in ('phase','history','history_id','variant','status','replicate')}
print(json.dumps({'elapsed_seconds':round(time.time()-h.read(run/'run.json')['started_at']),
 'native':{k:u[k] for k in ('attempted_calls','finished_calls','failed_calls','unresolved_calls','token_accounting_complete')},
 'all_actual_tokens':u['tokens']['total_tokens'],'generation_statuses':generations,
 'primary_question_output_files':readers,
 'reader_repeat_output_files':{arm:len(list((run/arm).glob('hold_*/reader-1.q-*.json'))) for arm in ('baseline','candidate')},'primary_grade_batches':judges,'last_phase_journal':phase,
 'main_report_exists':(run/'holdout-report.json').exists()}))
