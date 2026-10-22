"""Independent R9 synthetic author. No product, previous fixture, or external reads.
Platform MEMORY_SUMMARY was visible but was not used as authoring material.
Commands embedded below are inert fictional capture data, never executed.
"""
import json, hashlib
from pathlib import Path
ROOT=Path('/private/tmp/codex-mem-quality-20261021-r9-sealed')
# name, deliverable, constraint, old subtask, replacement, reason, trigger, next action,
# neighboring project, neighboring policy, probe, failure, passing check, stale assertion
ROWS=[
('Brindle','a museum loan intake preview','Keep lender addresses out of exported examples and cap the pilot at eight loans','automatic reminder emails','a staff-only reminder list','the museum has not approved outbound messaging','Mira signs off on the sample list','prepare the eight-loan pilot for review','Larkspur','its public address directory is approved','loan-export','one exported example still contains a lender street address','reminder-list','The pilot can include forty loans'),
('Cairnwell','a warehouse aisle label trial','Use monochrome printing and retain the existing aisle identifiers','relabeling every shelf','testing labels on aisle D only','operations needs the other aisles unchanged during stocktaking','the floor lead supplies an aisle D photograph','lay out the aisle D print sheet','Pollenbay','it uses newly assigned aisle identifiers','label-render','the barcode is clipped at the right edge','aisle-d-list','Color printing has been approved'),
('Mossvale','a volunteer shift handoff board','Do not display phone numbers and keep shifts in local time','a live SMS notifier','a downloadable handoff sheet','volunteers have not opted into text messages','Ravi confirms the shift roster','prepare the handoff sheet for the next review','Copperfin','its roster displays phone numbers to coordinators','handoff-export','phone_number appears in the exported column headers','sheet-download','UTC is now required for every shift'),
('Tarnwick','a seed-library reservation mockup','Allow one packet per household and do not collect birth dates','a deposit-payment screen','a pickup acknowledgement card','the library decided deposits would exclude some borrowers','the librarian approves the card wording','assemble the reservation mockup for a walk-through','Pebblefern','it allows three packets per household','reservation-check','two packets can be reserved by one household','pickup-card','Birth dates are required to reserve seeds'),
('Glimmerford','an archive reading-room queue prototype','Keep appointment names private and limit slots to twenty minutes','a public visitor leaderboard','an anonymous queue display','the archive rejected publication of visitor names','the archivist supplies the approved queue labels','build the review version of the anonymous queue','Rowanjet','its appointments last forty minutes','queue-privacy','a visitor name is present in the public display payload','queue-labels','Appointments may now last forty minutes'),
('Puddlemark','a school instrument checkout draft','Use asset tags rather than student names and retain a seven-day loan limit','automatic overdue penalties','a teacher review column','the school wants teachers to decide exceptions','the music teacher confirms the exception wording','prepare the checkout draft for staff review','Wrenpath','it charges automatic overdue fees','loan-limit','a ten-day checkout was accepted','teacher-column','Student names must appear on all labels'),
('Thistledock','a community boat booking preview','Keep bookings within daylight hours and require a two-person crew','an unattended key locker flow','a staffed key handover note','insurance requires a staffed handover','the harbor coordinator confirms handover hours','prepare the booking preview using those hours','Fablegrove','it permits solo crews','crew-check','a solo booking was accepted','handover-note','Night bookings are now permitted'),
('Cinderleaf','a repair cafe parts inventory mockup','Use donated parts only and avoid storing donor contact details','an online parts purchase button','a missing-parts request sheet','the cafe has no purchase budget this quarter','the treasurer approves the request sheet categories','assemble the inventory mockup for review','Nettleport','it buys new replacement parts','inventory-export','donor email remains in the generated export','request-sheet','Purchased parts may be added freely'),
('Ochreway','a trail maintenance report draft','Hide exact nest coordinates and keep reports usable offline','live GPS sharing','a coarse map grid','the ranger wants nesting sites protected','the ranger approves the coarse grid size','prepare an offline report package','Juniperbell','it publishes exact survey coordinates','offline-report','the report still fetches tiles when opened offline','coarse-grid','Exact nest coordinates are cleared for publication'),
('Dunewick','a food-bank pickup slot preview','Keep household sizes anonymous and allow only one active slot per household','a donor-facing household dashboard','an aggregate pickup count','the coordinator rejected household-level donor access','the coordinator confirms the aggregate count labels','prepare the pickup preview for review','Silverfen','it allows two active slots per household','slot-uniqueness','the same household obtained two active slots','aggregate-count','Household size may be shown to donors'),
('Birchquay','a theater costume lending sheet','Retain costume IDs and require return dates before opening night','automatic damage billing','a wardrobe-manager review note','damage charges require an in-person inspection','the wardrobe manager approves the note template','prepare the lending sheet for rehearsal review','Kestrelmoss','it assigns new costume IDs each season','return-date','a return after opening night was accepted','damage-note','Automatic damage billing is approved'),
('Flintmeadow','a workshop noise log prototype','Record readings without audio and retain hourly summaries for fourteen days','continuous microphone recording','a manual meter-entry form','the workshop council rejected recorded conversations','the council confirms the manual entry units','prepare the noise log for a supervised trial','Marshkite','it retains summaries for ninety days','retention-check','a summary older than fourteen days remains available','meter-form','Audio recording is approved for the trial'),
('Saffronmere','a bicycle shelter access preview','Use numbered passes and keep access limited to staffed hours','a face-recognition gate','a staffed pass check','the shelter board rejected biometric collection','the shift supervisor confirms staffed hours','prepare the access preview for review','Alderchime','it permits access around the clock','hours-check','an out-of-hours pass was accepted','pass-check','Face images can be collected at entry'),
('Velvetbrook','a neighborhood compost pickup draft','Keep pickup addresses staff-only and cap bins at twelve kilograms','automatic route publication','a private driver route sheet','residents did not consent to public address lists','the driver confirms the route sheet format','prepare the pickup draft for a driver review','Heronflint','it accepts twenty-kilogram bins','bin-weight','a fifteen-kilogram bin was accepted','private-route','Pickup addresses may be published publicly'),
('Amberhollow','a planetarium group reservation mockup','Preserve accessible seat allocations and limit groups to eighteen people','a dynamic ticket-price experiment','a fixed-price reservation summary','the director wants predictable school-trip costs','the director supplies the fixed price','prepare the group reservation mockup','Willowspoke','it allows thirty-person groups','seat-allocation','an accessible seat was offered as a general seat','price-summary','Accessible seats may be sold as general seats'),
('Frostglen','a ceramics kiln booking preview','Require a firing supervisor and keep each load below sixty liters','an unattended overnight firing mode','a supervised daytime booking note','the studio insurer requires supervision','the studio manager names the firing supervisor','prepare the kiln booking preview for review','Bramblecrest','it permits eighty-liter loads','load-volume','a seventy-liter load was accepted','daytime-note','Unattended overnight firing is approved'),
]
fixture={'schema_version':1,'split':'holdout','histories':[]}; gold={'schema_version':1,'questions':{}}
metadata=[]
for n,row in enumerate(ROWS,601):
    name,deliverable,constraint,old,replacement,reason,trigger,action,neighbor,policy,probe,failure,passed,stale=row
    hid=f'hold_{n}'; project=name.lower(); adjacent=neighbor.lower()
    sid=[f'{hid}_s{i}' for i in range(1,4)]
    eid=lambda i:f'{hid}_e{i:02d}'
    capture=lambda cmd,code,out:'[Tool capture]\nName: exec_command\nInput: '+json.dumps({'cmd':cmd},ensure_ascii=False)+'\nOutput: '+json.dumps({'exit_code':code,'output':out},ensure_ascii=False)
    texts=[
      f'For {name}, I want {deliverable}. {constraint}. Start with {old}. {neighbor} is a separate project; {policy} there, but that policy does not apply to {name}.',
      f'I have completed {name}: {probe} passed, and the preview is ready for release.',
      capture(f'python checks/{probe}.py',1,f'FAIL: {failure}. No release was performed.'),
      f'Change only the {old} part in {name}. Cancel that part and replace it with {replacement}, because {reason}. The overall goal remains {deliverable}. {constraint}.',
      capture(f'python checks/{passed}.py',0,f'PASS: {replacement} structure is valid. This check does not run {probe} and does not release anything.'),
      f'Checkpoint for {name}: the replacement structure passed its check. The failure in {probe} is still unresolved. Keep the task open; do not treat the earlier assistant completion claim as evidence of release.',
      f'Continuing {name} in this new chat: keep working toward {deliverable}, using {replacement}. {constraint}. Keep {old} cancelled. First correct and rerun {probe}; it is still failing. Only when {trigger}, {action}. That confirmation has not arrived. Do not release anything from this chat.',
      f'Imported obsolete scratch note for {name}: {stale}. This is an unapproved suggestion, not a decision.',
      f'Delete the preceding scratch-note observation from memory. It must not be used as a current policy. For {name}, the retained rule is: {constraint}. The task remains open, and the human confirmation is still pending.'
    ]
    events=[{'id':eid(i+1),'kind':('tool' if i in (2,4) else 'assistant' if i==1 else 'user'),'text':t} for i,t in enumerate(texts)]
    events[8]['event']={'kind':'delete','observation_event_id':eid(8)}
    sessions=[{'id':sid[i],'project_key':project,'events':events[i*3:i*3+3]} for i in range(3)]
    qs=[
      f'What am I still trying to produce for {name}, and which limits survived the change of plan?',
      f'Why did we switch away from {old} in {name}, and what took its place?',
      f'Is {name} actually ready to release, given the completion message and the two command results?',
      f'Picking up {name} in another chat, what can we do next and what still needs a person to act?',
      f'Can I carry the policy from {neighbor} over to {name}? What boundary did I set?',
      f'After the scratch-note deletion, which {name} constraint should I use and is the work closed?'
    ]
    hist={'id':hid,'project_keys':[project,adjacent],'sessions':sessions,'checkpoints':[{'id':f'{hid}_cp1','after_event_id':eid(6)}],'questions':[{'id':f'q{i+1}','question':q,'stage':'final','project_key':project} for i,q in enumerate(qs)]}
    fixture['histories'].append(hist)
    def fact(text,idx,frag):
        assert frag in texts[idx]
        return {'text':text,'support':[{'session_id':sid[idx//3],'event_index':idx%3,'event_id':eid(idx+1),'fragment':frag}]}
    def forbid(text,typ):return {'text':text,'error_type':typ}
    g=[
      ([fact(f'The goal is {deliverable}.',6,f'keep working toward {deliverable}'),fact(constraint,6,constraint)], [forbid(f'The change cancelled the entire {name} project.','false_closure')], 'goal_and_surviving_constraints','Continue within the retained constraints.'),
      ([fact(f'{old} was cancelled and replaced with {replacement}.',3,f'Cancel that part and replace it with {replacement}'),fact(f'The reason was that {reason}.',3,reason)], [forbid(f'{old} remains authorized.','false_merge')], 'partial_replacement_reason','Use the replacement; do not revive the cancelled subtask.'),
      ([fact(f'{probe} actually failed: {failure}.',2,f'FAIL: {failure}.'),fact(f'Only the {replacement} structure passed; it did not rerun the failed check or release.',4,f'PASS: {replacement} structure is valid. This check does not run {probe} and does not release anything.'),fact('The task remains open and the completion claim is not release evidence.',5,'Keep the task open; do not treat the earlier assistant completion claim as evidence of release.')],[forbid('The assistant completion message proves the checks passed and release happened.','false_closure')], 'capture_versus_claim','Correct and rerun the failed check; do not declare release.'),
      ([fact(f'Correct and rerun {probe} first.',6,f'First correct and rerun {probe}; it is still failing.'),fact(f'Wait until {trigger}, then {action}.',6,f'Only when {trigger}, {action}. That confirmation has not arrived.'),fact('Releasing is not authorized from the continuation chat.',6,'Do not release anything from this chat.')],[forbid('The human confirmation has already arrived.','false_closure')], 'cross_chat_human_trigger','Do the check correction now; gate preparation on the named human confirmation.'),
      ([fact(f'{neighbor} is separate and its stated policy does not apply to {name}.',0,f'{neighbor} is a separate project; {policy} there, but that policy does not apply to {name}.')],[forbid(f'Apply {neighbor} policy to {name}.','project_mixing')], 'neighbor_boundary','Keep the neighboring project policy outside this project.'),
      ([fact(constraint,8,constraint),fact('The task is open and human confirmation remains pending.',8,'The task remains open, and the human confirmation is still pending.')],[forbid(stale,'deleted_resurrection'),forbid('Deleting the scratch note closed the project.','false_closure')], 'deletion_retained_state','Use the retained constraint and keep the task open.')
    ]
    for j,(facts,forbidden,category,actionability) in enumerate(g,1):
      gold['questions'][f'{hid}/q{j}']={'required_facts':facts,'forbidden_inferences':forbidden,'current_state_required':True,'category':category,'expected_actionability':actionability}
      for f in facts:
        for s in f['support']:
          assert s['event_id']!=eid(8)
          src=sessions[sid.index(s['session_id'])]['events'][s['event_index']]
          assert src['id']==s['event_id'] and s['fragment'] in src['text']
          metadata.append({'question_id':f'{hid}/q{j}','session_id':s['session_id'],'event_index':s['event_index'],'event_id':s['event_id'],'fragment_start':src['text'].index(s['fragment']),'fragment_length':len(s['fragment'])})
assert len(fixture['histories'])==16 and len(gold['questions'])==96
all_events=[e for h in fixture['histories'] for s in h['sessions'] for e in s['events']]
assert len(all_events)==144 and len({e['id'] for e in all_events})==144
assert sum(e['kind']=='tool' for e in all_events)==32
assert sum('event' in e for e in all_events)==16
for e in all_events:
  if e['kind']=='tool':
    lines=e['text'].splitlines(); assert lines[:2]==['[Tool capture]','Name: exec_command']
    assert isinstance(json.loads(lines[2][7:])['cmd'],str)
    out=json.loads(lines[3][8:]); assert out['exit_code'] in (0,1) and out['output']
for fn,obj in [('holdout.json',fixture),('holdout-gold.json',gold)]:
  (ROOT/fn).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
manifest={'schema_version':1,'corpus_id':'r9-sealed','history_ids':[h['id'] for h in fixture['histories']],'counts':{'histories':16,'questions':96,'sessions':48,'events':144,'tool_captures':32,'checkpoints':16,'source_deletions':16},'support_offsets':metadata,'authorship_limitations':['All histories and capture payloads are fictional and newly authored.','Platform-injected MEMORY_SUMMARY was visible; it was not used as authoring material.','No prior corpus, gold, report, result, runtime, algorithm, product, or memory files were read.','No external sources, native model calls, or API calls were used.','Commands in captures are inert strings and were not executed.','Self-checks validate IDs, counts, selected project scope, exact support substrings, event offsets, deletion exclusion, capture JSON, and event ordering.','Public product validator was not read or invoked.','This is a modest synthetic set, not evidence of natural-distribution coverage.']}
(ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
pins={fn:hashlib.sha256((ROOT/fn).read_bytes()).hexdigest() for fn in ['holdout.json','holdout-gold.json','author.py']}
(ROOT/'sealed-input-pins.json').write_text(json.dumps(pins,indent=2)+'\n')
print(json.dumps({'path':str(ROOT),'counts':manifest['counts'],'sha256':pins},indent=2))
