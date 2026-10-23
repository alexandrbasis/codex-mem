"""Render the shipped summary with a minimal DOM; no browser or user data."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import tempfile


@unittest.skipUnless(shutil.which("node"), "Node is needed for the shipped JavaScript renderer")
class DashboardPresentationTests(unittest.TestCase):
    def test_tables_preserve_real_ledger_unknown_partial_and_zero_values(self):
        from codex_mem.config import configure
        from codex_mem.store import Store
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.execution import ExecutionProfile
        from codex_mem.usage_api import usage_report
        cases = [
            ([{}], ['Unknown'] * 3, 'Unavailable'),
            ([dict(input_tokens=100, output_tokens=20, total_tokens=120)], ['100', 'Unknown', '20'], 'Unavailable'),
            ([dict(input_tokens=0, cached_input_tokens=0, output_tokens=0, total_tokens=0)], ['0'] * 3, '$0.00'),
            ([dict(input_tokens=0, cached_input_tokens=0, output_tokens=0, total_tokens=0), {}], ['Unknown total · 0 known subtotal'] * 3, '$0.00 · partial'),
            ([dict(input_tokens=100000, cached_input_tokens=0, output_tokens=20000, total_tokens=120000, usage_status='partial')], ['Unknown total · 100,000 known subtotal', 'Unknown total · 0 known subtotal', 'Unknown total · 20,000 known subtotal'], ['$0.02 · partial', '$0.04 · partial']),
        ]
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {constructor(tag){this.tag=tag;this.children=[];this.dataset={};} append(...v){this.children.push(...v);} setAttribute(){}}
const document={querySelectorAll:()=>[],createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag)};
const context=vm.createContext({document,Intl,Node:Element});
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(source.slice(0,source.indexOf('  function attemptCost('))+'globalThis.models=models;globalThis.streams=streams;globalThis.pricing=pricing;})();',context);
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const rows=node=>node.tag==='tbody'?node.children.map(row=>row.children.map(cell=>cell.textContent||null)):node.children.flatMap(rows);
const texts=node=>[node.textContent,...node.children.flatMap(texts)].filter(Boolean);
console.log(JSON.stringify({models:rows(context.models(data)),streams:rows(context.streams(data)),pricing:texts(context.pricing(data))}));
"""
        for receipts, expected_tokens, expected_cost in cases:
            with self.subTest(receipts=receipts), tempfile.TemporaryDirectory() as directory:
                project = str(Path(directory) / 'project')
                configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
                with Store(directory) as store:
                    store.remember(project, 'Prompt', 'Keep work open.', source='hook:UserPromptSubmit')
                    memory = ProjectMemory(store)
                    job = memory.claim(project)
                    profile = ExecutionProfile(runtime='pi', provider='openai', model='gpt-6-luna', reasoning_effort='low', timeout=120)
                    for receipt in receipts:
                        if 'cached_input_tokens' in receipt:
                            receipt = dict(cache_write_input_tokens=0, reasoning_output_tokens=0) | receipt
                        attempt = memory.begin_attempt(job, model=profile.model, reasoning_effort='low', profile=profile)
                        memory.finalize_attempt(project, attempt['attempt_id'], 'failed', dict(usage_status='reported', code='timeout') | receipt)
                report = usage_report(directory, from_date='2020-01-01', to_date='2030-01-01', group_by=['model'])
                # All three shipped presentations read the same real ledger aggregate.
                stream = report['observer']
                data = dict(observer=stream, combined=stream, models={'observer':report['groups']['observer']['model']['rows']})
                result = subprocess.run([shutil.which('node'), '-e', script, str(Path(__file__).parents[1] / 'codex_mem/ui_static/app.js')], input=json.dumps(data), capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                rendered = json.loads(result.stdout)
                self.assertEqual(expected_tokens, rendered['models'][0][3:6])
                expected_costs = [expected_cost, expected_cost] if isinstance(expected_cost, str) else expected_cost
                self.assertEqual(expected_costs, rendered['models'][0][8:10])
                self.assertEqual(expected_costs, rendered['streams'][1][2:4])
                values = rendered['pricing']
                self.assertEqual(expected_costs[0], values[values.index('Standard scenario') + 1])
                self.assertEqual(expected_costs[1], values[values.index('Fast scenario') + 1])

    def test_share_requires_complete_totals_and_positive_denominator(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.dataset = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
}
const document = {querySelectorAll:()=>[], createElement:()=>new Element(), createElementNS:()=>new Element()};
const context = vm.createContext({document, Intl});
const source = fs.readFileSync(process.argv[1], 'utf8');
// Exercise the production renderer before application startup attaches listeners.
vm.runInContext(source.slice(0, source.indexOf('  function streams(')) + 'globalThis.render = summary; globalThis.renderExecution = execution; })();', context);
const stream = (subtotal, total) => ({total_tokens:subtotal, token_totals:{total_tokens:{total,known_subtotal:subtotal,known_events:1}}});
const cases = [[50,50,100,100], [50,null,100,100], [50,50,100,null], [0,0,100,100], [0,0,0,0], [0,null,100,100]];
const refusal=context.renderExecution({execution:{latest_failure:{error_code:'configuration_error',reason_code:'unsupported_reasoning',started_at:'2026-10-23T10:00:00Z',failure_recorded_at:'2026-10-23T10:04:00Z',failure_time_basis:'finished_at'}}});
const texts=node=>[node.textContent,...node.children.flatMap(texts)].filter(Boolean);
if (!texts(refusal).some(text=>text.includes('1:04'))) throw Error('Refusal uses start time instead of completion time');
const fallback=context.renderExecution({execution:{latest_failure:{failure_time_basis:'started_at_fallback',failure_recorded_at:'2026-10-23T10:00:00Z'}}});
if (!texts(fallback).includes('Refusal time unavailable · attempt started')) throw Error('Historical fallback is not labelled');
if (!texts(refusal).includes('configuration_error · unsupported_reasoning')) throw Error('Latest refusal omits its specific reason');
console.log(JSON.stringify(cases.map(([a,b,c,d])=>context.render({observer:stream(a,b),main:stream(c,d)}).children[1].children[2].textContent)));
"""
        result = subprocess.run([shutil.which("node"), "-e", script,
                                 str(Path(__file__).parents[1] / "codex_mem/ui_static/app.js")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["50% of main Codex", "Share of main Codex unknown",
                          "Share of main Codex unknown", "0% of main Codex",
                          "Share of main Codex unknown", "Share of main Codex unknown"],
                         json.loads(result.stdout))
