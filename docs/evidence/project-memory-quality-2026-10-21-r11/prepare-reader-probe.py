#!/usr/bin/env python3
"""Prepare public reader requests and evaluator references. Never dispatch models."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFAULT_CORPUS = HERE / 'public-reader-regressions.json'


def load_helper(path=ROOT / 'scripts/project_memory_consolidation_experiment.py'):
    spec = importlib.util.spec_from_file_location('r11_reader_probe_helper', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def default_protocol(corpus):
    return {**corpus['reader_contract'], 'judge_evidence_contract': 'answer-source-witnesses-v1',
            'judge_profile': {'model': 'gpt-6-astra', 'reasoning_effort': 'medium'}}


def prepare(helper, protocol, corpus):
    if (corpus.get('fictional') is not True or len(corpus.get('cases', [])) != 6
            or protocol.get('reader_evidence_contract') != 'clause-support-v2'
            or helper.reader_prompt_version(protocol) != 'role-attribution-per-question-v2'):
        raise ValueError('public probe requires six fictional isolated v2-reader cases')
    helper.phase_profile(protocol, 'reader')  # Validate any declared experiment-only override.
    requests, references = [], []
    for case in corpus['cases']:
        context = case['context']
        if (len(context['memory']) != context['context_chars']
                or hashlib.sha256(context['memory'].encode()).hexdigest() != context['context_sha256']):
            raise ValueError('public context bytes changed')
        catalog = helper.reader_citation_catalog([context], case['original_source_fields'])
        # Verify that the selected old required facts have visible support. Do not
        # demand recall of hidden evidence in this targeted public reader probe.
        visibility = []
        for fact_index, fact in enumerate(case['gold']['required_facts']):
            if not fact.get('support'):
                raise ValueError('public required fact has no auditable visible support')
            for support in fact.get('support', []):
                start, end = support['start'], support['end']
                body = case['original_source_fields'][support['source_id']]['fields']['body']
                if body[start:end] != support['quote']:
                    raise ValueError('public required-fact support differs from its original source span')
                spans = sorted((span['start'], span['end']) for span in catalog
                               if span['source_id'] == support['source_id'] and span['source_field'] == 'body')
                cursor = start
                for lower, upper in spans:
                    if lower <= cursor:
                        cursor = max(cursor, upper)
                if cursor < end:
                    raise ValueError('public oracle requires a fact outside its selected visible source spans')
                visibility.append({'fact_index': fact_index, 'source_id': support['source_id'],
                    'source_field': 'body', 'start': start, 'end': end, 'quote': support['quote'],
                    'reader_visible': True,
                    'covering_citation_refs': [span['ref'] for span in catalog
                        if span['source_id'] == support['source_id'] and span['source_field'] == 'body'
                        and span['start'] < end and span['end'] > start]})
        native_request = helper.typed_reader_request([context], catalog, protocol)
        requests.append({'case_id': case['id'], 'request': native_request})
        references.append({'case_id': case['id'], 'context': context, 'catalog': catalog,
                           'originals': case['original_source_fields'], 'gold': case['gold'],
                           'original_history': case['original_history'], 'oracle': case['oracle'],
                           'required_fact_visibility': visibility})
    return {'status': 'prepared_no_native_calls', 'reader_requests': requests,
            'evaluator_references_not_reader_input': references,
            'reader_prompt_version': helper.reader_prompt_version(protocol),
            'helper_sha256': hashlib.sha256(Path(helper.__file__).read_bytes()).hexdigest(),
            'protocol_sha256': helper.digest(protocol),
            'request_sha256': [helper.digest(item['request']) for item in requests]}


def judge_payload(helper, protocol, corpus, native_reader_outputs):
    prepared = prepare(helper, protocol, corpus)
    if len(native_reader_outputs) != 6:
        raise ValueError('probe judging requires all six fresh reader outputs; no partial batch salvage')
    cases, catalogs, original_history = [], {}, []
    for index, (reference, output) in enumerate(zip(prepared['evaluator_references_not_reader_input'], native_reader_outputs)):
        resolved = helper.resolve_reader_output(output, [reference['context']], reference['catalog'])
        case_id = f'answer-{index+1:02d}'
        answer = {**helper.reader_answer_for_judge(resolved['answers'][0], protocol), 'id': case_id}
        cases.append({'id': case_id, 'question': reference['context']['question'], 'answer': answer,
                      'gold': reference['gold'], 'retrieved_context_id': reference['context']['id'],
                      'reader_visible_memory': [reference['context']['memory']]})
        catalogs[case_id] = helper.judge_source_catalog(reference['originals'], reference['catalog'])
        original_history.extend(reference['original_history'])
    payload = {'cases': cases, 'case_source_catalogs': catalogs, 'original_history': original_history}
    return payload


def judge_request(helper, protocol, corpus, native_reader_outputs):
    return helper.judge_request(judge_payload(helper, protocol, corpus, native_reader_outputs), protocol)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--protocol', type=Path)
    parser.add_argument('--output', type=Path, help='New preparation directory; cannot overwrite prior evidence.')
    args = parser.parse_args()
    helper = load_helper()
    corpus = helper.read(args.corpus)
    protocol = helper.read(args.protocol) if args.protocol else default_protocol(corpus)
    prepared = prepare(helper, protocol, corpus)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        for index, item in enumerate(prepared['reader_requests']):
            helper.write(args.output / f'reader-request-{index+1:02d}.json', item['request'])
        helper.write(args.output / 'evaluator-references-not-reader-input.json', prepared['evaluator_references_not_reader_input'])
        helper.write(args.output / 'preparation.json', {key: value for key, value in prepared.items()
                                                      if key not in ('reader_requests', 'evaluator_references_not_reader_input')})
    print(json.dumps({key: value for key, value in prepared.items()
                      if key not in ('reader_requests', 'evaluator_references_not_reader_input')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
