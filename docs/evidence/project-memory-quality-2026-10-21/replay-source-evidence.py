"""Rebuild disclosed development contexts; never generate answers or alter grades."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if 'holdout' in args.fixture.name.lower():
        raise ValueError('This disclosed regression must not open holdout inputs')
    fixture = json.loads(args.fixture.read_text())
    if fixture['split'] != 'development':
        raise ValueError('Only disclosed development material is permitted')
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo))
    helper_path = repo / 'scripts/project_memory_consolidation_experiment.py'
    spec = importlib.util.spec_from_file_location('experiment', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    targets = {
        'dev_01': ('d2a9b6edd6724e92802ce39a42ca2de3',
                   'I prepared a schedule and believe the missing-measurement list is finished.'),
        'dev_03': ('cdb4ec85faea47059c1d86fe28382116',
                   'I expect the Opal print sample to pass.'),
        'dev_04': ('3656d79939694777afc80f6d68ced3d6',
                   'The cooldown duration is 12 minutes according to my estimate.'),
    }
    results = []
    for history in fixture['histories']:
        original = args.run / 'candidate' / history['id']
        before = helper.state_hash(original / 'data')
        meta = helper.read(original / 'generation.json')
        previous = helper.read(original / 'contexts.json')
        with tempfile.TemporaryDirectory(prefix='codex-mem-source-replay-') as temporary:
            data = Path(temporary) / 'data'
            shutil.copytree(original / 'data', data)
            contexts = [helper.retrieve_question(
                data, meta['projects'], question, 6000,
                Path(temporary) / 'retrieval-timings.jsonl') for question in history['questions']]
        current_quotes = helper.visible_source_quotes(contexts)
        prior_quotes = helper.visible_source_quotes(previous)
        lost = [{'source_id': source_id, 'quote': quote}
                for source_id, quotes in prior_quotes.items() for quote in sorted(set(quotes))
                if not any(quote in current for current in current_quotes.get(source_id, []))]
        target = targets.get(history['id'])
        source_check = None if target is None else {
            'source_id': target[0], 'quote': target[1],
            'previously_visible': any(target[1] in quote for quote in prior_quotes.get(target[0], [])),
            'now_visible': any(target[1] in quote for quote in current_quotes.get(target[0], [])),
        }
        if helper.state_hash(original / 'data') != before:
            raise AssertionError('Original development Store changed')
        results.append({'history_id': history['id'], 'contexts': contexts,
                        'assistant_report_check': source_check,
                        'previous_visible_excerpts_no_longer_visible': lost,
                        'max_context_chars': max(context['context_chars'] for context in contexts),
                        'original_store_unchanged': True})
    payload = {'kind': 'disclosed_regression_source_availability_only',
               'model_calls': 0, 'answers_or_grades_replaced': False,
               'quality_improvement_established': False,
               'fixture_sha256': hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
               'runtime_hashes': helper.code_hash(repo), 'results': results}
    args.output.write_text(json.dumps(payload, indent=2) + '\n')
    checks = [result['assistant_report_check'] for result in results
              if result['assistant_report_check'] is not None]
    print(json.dumps({'histories': len(results), 'assistant_reports_visible':
                      sum(check['now_visible'] for check in checks), 'reports_checked': len(checks),
                      'previous_visible_excerpts_lost': sum(len(result[
                          'previous_visible_excerpts_no_longer_visible']) for result in results),
                      'max_context_chars': max(result['max_context_chars'] for result in results),
                      'model_calls': 0, 'quality_claim': False}))


if __name__ == '__main__':
    main()
