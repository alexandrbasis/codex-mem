"""Pinned public mechanical validation and independent round count provenance."""
import collections,hashlib,importlib.util,json
from pathlib import Path
def validate_counts(base,fixture,gold,protocol,release,manifest,contract,check):
 sha=lambda data:hashlib.sha256(data).hexdigest()
 def require(value,label):
  check(value,label)
  if not value:raise ValueError(label)
 require(contract['contract']=='pinned-public-mechanical-counts-v1','count_contract_selected')
 validator=base/'public-fixture-contract.py'
 require(sha(validator.read_bytes())==contract['validator_sha256']==manifest['generic_pack_files']['public-fixture-contract.py'],'count_validator_pin')
 evidence_path=base/'release-verification.json'
 require(sha(evidence_path.read_bytes())==contract['release_verification_sha256'],'count_parent_evidence_pin')
 parent=json.loads(evidence_path.read_bytes())
 require(parent['formal_release_sha256']==sha((base/'formal-release.json').read_bytes()),'count_parent_release_binding')
 require(parent['archive_sha256']==release['archive_sha256'],'count_parent_archive_binding')
 require(sha((base/'holdout/fixture.json').read_bytes())==contract['fixture_sha256'],'count_fixture_pin')
 require(sha((base/'holdout/gold.json').read_bytes())==contract['gold_sha256'],'count_gold_pin')
 # Executed code is the pinned public validator, never author code.
 spec=importlib.util.spec_from_file_location('pinned_public_mechanical_counts_v003',validator);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 require(json.loads((base/'holdout/fixture.json').read_bytes())==fixture and json.loads((base/'holdout/gold.json').read_bytes())==gold,'count_input_identity')
 mechanical=module.validate_files(base/'holdout/fixture.json',base/'holdout/gold.json')
 roles=collections.Counter(e['type'] for h in fixture['histories'] for s in h['sessions'] for e in s['events'])
 facts=sum(len(q['required_facts']) for q in gold['questions'].values())
 anchors=sum(len(f['support']) for q in gold['questions'].values() for f in q['required_facts'])
 histories=fixture['histories'];observations=lambda h:sum(e['type']!='delete_observation' for s in h['sessions'] for e in s['events']);checkpoints=lambda h:sum(len(s['checkpoints']) for s in h['sessions'])
 slots=sum(len(h['questions'])*2*(1+int(h['id'] in protocol['reader_repeat_history_ids'])+int(h['id'] in protocol['regeneration_repeat_history_ids'])) for h in histories)
 counts=dict(histories=len(histories),questions=sum(len(h['questions']) for h in histories),planned_answer_slots=slots,sessions=sum(len(h['sessions']) for h in histories),events=sum(observations(h) for h in histories),deletions=roles['delete_observation'],checkpoints=sum(checkpoints(h) for h in histories),max_history_events=max(map(observations,histories)),max_history_checkpoints=max(map(checkpoints,histories)),required_facts=facts,source_roles=dict(roles))
 if 'author_count_contract' in contract:
  require(contract['author_count_contract']=='r22-resource-counts-v1','author_count_contract_selected')
  from author_counts import derive_resources,validate_resource_shape
  validate_resource_shape(manifest['counts'])
  resources=derive_resources(fixture,gold)
  require(resources==manifest['counts']==release['counts'],'independent_complete_author_resource_counts')
  counts['max_history_sessions']=max(len(h['sessions']) for h in histories)
 require(counts==parent['counts'],'independent_all_parent_counts')
 require(facts==manifest['counts']['required_facts']==release['counts']['required_facts'],'independent_fact_count_bindings')
 require(mechanical['status']==parent['mechanical_status']=='positive_source_anchors_representable','mechanical_representability_status')
 require(anchors==mechanical['positive_anchors']==parent['positive_anchors'],'independent_anchor_count_bindings')
 require(mechanical['histories']==counts['histories'] and mechanical['questions']==counts['questions'],'mechanical_question_counts')
 require(mechanical['deletion_controls']==parent['deletion_controls']==counts['deletions'],'mechanical_deletion_counts')
 return {'counts':counts,'positive_anchors':anchors,'mechanical_status':mechanical['status'],'provenance':contract}
