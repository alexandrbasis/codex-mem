from pathlib import Path
import json, hashlib
ROOT=Path(__file__).resolve().parent
CONTRACT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public/docs/evidence/project-memory-quality-2026-10-21-r11/public-fixture-contract.py')
RECEIPT=CONTRACT.with_name('public-fixture-contract-receipt.json')
# Entirely fictional authored cases. No external datasets or runtime implementation.
CASES=[
 dict(key='seed_archive',other='seed_shop',name='Seed archive',task='packet index',neighbor='packet labels',constraint='Keep accession numbers as text, including leading zeroes, because the cabinet cards use those exact strings.',old='The packet index uses the provisional location field shelf_old.',current='The packet index now uses drawer_code as its location field. This replaces shelf_old.',local='Seed archive packet index: local import of 12 invented packets passed. No shared installation was contacted.',report='I believe the packet index was installed on the shared kiosk yesterday; I have no installation log in this chat.',open='The packet index still needs a curator check of the drawer mapping before installation can be approved.',tail='For the packet index, retain the drawer mapping work but cancel the optional seed-photo thumbnails. Next time show me the mapping first.',boundary='Seed shop packet labels use numeric stock numbers. That shop rule does not apply to Seed archive accession numbers.'),
 dict(key='pool_rota',name='Pool rota',task='lane schedule',neighbor='lesson schedule',constraint='Show lane bookings in quarter-hour blocks; the printed pool clock has only quarter-hour marks.',old='The lane schedule draft starts weekday sessions at 06:00.',current='The lane schedule weekday start is 06:30, replacing the 06:00 draft.',local='Pool rota lane schedule: a local overlap check passed for Monday. The public board was not updated.',report='I said the public lane board looked updated, but that was my impression rather than a recorded board check.',open='Ask the duty coordinator to approve the Monday lane allocation; the local overlap check does not fulfill that request.',tail='Continue the lane allocation in this chat. Drop the proposed colour legend, but keep the text lane names and the approval request.',boundary='The lesson schedule can begin at 08:00. It is a separate task and does not change the lane schedule start.'),
 dict(key='ceramic_log',other='glass_log',name='Ceramic log',task='firing register',neighbor='firing labels',constraint='Record temperatures in whole degrees Celsius because the workshop kiln display has no decimal setting.',old='Firing register batches were temporarily identified with prefix TMP.',current='Firing register batches use prefix CLAY. The temporary TMP prefix is retired.',local='Ceramic log firing register: local CSV round-trip preserved five sample rows. No workshop tablet was accessed.',report='My earlier message claimed the workshop tablet had the register; that claim has no tablet output attached.',open='The workshop lead must still review the cooling-stage names before the tablet rollout.',tail='Carry the register work into the next chat. Remove the optional glaze-photo column from this delivery, and keep the cooling-stage review.',boundary='Glass log firing labels allow fractional Celsius values for its test controller. That exception belongs only to Glass log.'),
 dict(key='trail_signs',name='Trail signs',task='junction inventory',neighbor='distance inventory',constraint='Use metres without rounding to kilometres, since several junctions lie less than one kilometre apart.',old='Junction inventory exported its marker identifier under marker_temp.',current='The junction inventory export field is marker_id; marker_temp is obsolete.',local='Trail signs junction inventory: local export contains 18 marker_id entries. No field signs were replaced.',report='I reported that crews had replaced the signs, but I did not obtain a crew confirmation.',open='A ranger still needs to compare the inventory with the north-loop junctions in person.',tail='Resume the north-loop comparison task here. Cancel the decorative map border only; the inventory export and ranger comparison remain required.',boundary='Distance inventory groups segments by trail colour. That separate inventory does not change junction marker identifiers.'),
 dict(key='choir_library',other='theatre_library',name='Choir library',task='score checkout',neighbor='script checkout',constraint='Keep arranger credits visible in the checkout slip because different arrangements share the same song title.',old='Score checkout had a draft loan duration of 21 days.',current='Score checkout loans last 28 days. The 21-day draft is withdrawn.',local='Choir library score checkout: local slip preview includes arranger credits. No circulation desk deployment was performed.',report='I wrote that the circulation desk was already using the new slips, without observing a desk transaction.',open='The librarian must still approve the overdue wording before desk deployment.',tail='Keep the checkout-slip change moving in the next chat. Cancel the proposed reminder email, but retain the overdue wording review.',boundary='Theatre library script checkout uses 14-day loans. Its loan policy has no effect on Choir library.'),
 dict(key='orchard_notes',name='Orchard notes',task='pruning checklist',neighbor='watering checklist',constraint='List tree rows from west to east because crews enter through the western gate.',old='The pruning checklist draft used a section called winter_old.',current='The pruning checklist section is dormant_season, replacing winter_old.',local='Orchard notes pruning checklist: local print preview has west-to-east row order. No crew handheld was synchronized.',report='I assumed the handhelds had synchronized overnight; I did not inspect their synchronization status.',open='The foreman still needs to walk row seven and confirm the pruning exceptions.',tail='Continue the row-seven work from this chat. Skip the optional bark photos now; preserve the exception notes and foreman walk.',boundary='The watering checklist follows pipe order instead of tree-row order. This is a different checklist and does not override pruning order.'),
 dict(key='museum_audio',other='museum_shop',name='Museum audio',task='gallery track list',neighbor='shop playlist',constraint='Preserve a three-second pause between tracks because visitors need time to move the handheld away from the display.',old='The gallery track list used the draft language code en_old.',current='The gallery track list language code is en_GB. The en_old draft code is no longer used.',local='Museum audio gallery track list: offline timing check measured three-second gaps. Visitor devices were not loaded.',report='I told you visitor devices were loaded, but that was an unverified report from my own notes.',open='The access coordinator has yet to listen to the transition after track four.',tail='For the track list, cancel the optional opening chime while keeping all three-second gaps. Continue with the track-four listening review.',boundary='Museum shop playlist has no gaps between tracks. This applies to shop speakers only, not Museum audio handhelds.'),
 dict(key='repair_queue',name='Repair queue',task='bicycle intake',neighbor='tool intake',constraint='Allow an unknown wheel size at intake because some bicycles arrive without either wheel.',old='Bicycle intake used a temporary status called waiting_old.',current='Bicycle intake now calls that status awaiting_inspection; waiting_old has been removed from the design.',local='Repair queue bicycle intake: local form accepted unknown wheel size. No reception terminal was changed.',report='I claimed reception was on the new form, although I have no terminal check to support that statement.',open='The mechanic must still test the missing-frame-number case before reception rollout.',tail='Continue bicycle intake testing here. Cancel the optional customer-print copy; keep the missing-frame-number test in scope.',boundary='Tool intake requires a tool category, not a wheel size. Its mandatory-category rule is separate from bicycle intake.'),
 dict(key='ferry_notices',other='bus_notices',name='Ferry notices',task='pier notice sheet',neighbor='stop notice sheet',constraint='Print both pier names in every notice because travellers sometimes board from the reverse direction.',old='Pier notice sheet was provisionally set to A5 paper.',current='Pier notice sheet uses A4 paper. The provisional A5 setting is obsolete.',local='Ferry notices pier notice sheet: local PDF preview includes both pier names. No pier printer received a job.',report='I said the pier printer had produced the notice, but I never retrieved a print result.',open='A dispatcher still must confirm the alternate-pier wording before notices go up.',tail='Carry the pier sheet into the next chat. Remove the optional QR code, but keep both pier names and the dispatcher wording check.',boundary='Bus notices stop notice sheet uses A5 paper. That size is specific to Bus notices and does not change Ferry notices.'),
 dict(key='bookbind_queue',name='Bookbind queue',task='repair ticket',neighbor='supply ticket',constraint='Keep the original spine title even when misspelled because binders match tickets to the physical volume.',old='Repair ticket priority was temporarily a field named urgency_old.',current='Repair ticket uses priority_band instead of the retired urgency_old field.',local='Bookbind queue repair ticket: local ticket sample preserved the misspelled spine title. The bindery service was not updated.',report='I described the bindery service as updated, but I have not seen a service version or a completed ticket there.',open='The senior binder still owes a review of the loose-cover checklist.',tail='Resume the repair-ticket work here. Drop the optional cover thumbnail and retain the loose-cover checklist review.',boundary='Supply ticket normalizes catalogue spelling for orders. That behaviour does not apply to original titles on repair tickets.'),
 dict(key='market_stalls',other='night_market',name='Market stalls',task='morning placement plan',neighbor='evening placement plan',constraint='Reserve a two-metre aisle beside the bread stand because delivery carts must turn there.',old='Morning placement plan labelled the bread stand slot B-OLD.',current='Morning placement plan labels the bread stand slot B-07. B-OLD was a placeholder.',local='Market stalls morning placement plan: local geometry check leaves a two-metre aisle. The steward display was not published.',report='I reported that the steward display showed this layout, but no display capture confirms it.',open='The steward must still verify the delivery-cart turn before publication.',tail='Continue the morning plan in the next chat. Cancel decorative stall icons only; keep the aisle clearance and cart-turn verification.',boundary='Night market evening placement plan uses slot B-12 for its bread stand. This does not relabel Market stalls slot B-07.'),
 dict(key='costume_store',name='Costume store',task='cloak inventory',neighbor='shoe inventory',constraint='Store cloak lengths in centimetres because the cutting table is marked in centimetres.',old='Cloak inventory had a draft rack code R0.',current='Cloak inventory uses rack code R4. The draft R0 assignment is cancelled.',local='Costume store cloak inventory: local import retained centimetre lengths for eight cloaks. No wardrobe tablet was touched.',report='I previously said the wardrobe tablet was updated; I have no tablet readback for that claim.',open='The wardrobe manager still needs to count the lined cloaks on rack R4.',tail='Continue from this inventory chat. Cancel the optional fabric-swatch attachment but keep the lined-cloak count.',boundary='Shoe inventory records European sizes. It is separate from cloak inventory and does not change cloak length units.'),
 dict(key='bird_hide',other='lake_walk',name='Bird hide',task='observation board',neighbor='walk board',constraint='Use silhouettes instead of species photographs because the board printer produces monochrome output.',old='Observation board listed the hide opening time as a provisional 05:00.',current='Observation board lists hide opening at 05:45. The provisional 05:00 entry is obsolete.',local='Bird hide observation board: local monochrome preview retained silhouette outlines. No hide noticeboard was installed.',report='I wrote that the hide noticeboard had been installed, but that was not based on an installation record.',open='A volunteer still must check the board readability through the hide window.',tail='Take the observation-board work into the next chat. Remove the optional migration chart, while preserving silhouettes and the window readability check.',boundary='Lake walk walk board advertises a 07:00 start. That start belongs to Lake walk, not Bird hide.'),
 dict(key='bakery_batches',name='Bakery batches',task='dough worksheet',neighbor='icing worksheet',constraint='Express water as grams because the bench scale does not measure volume.',old='Dough worksheet used a provisional rest duration of 35 minutes.',current='Dough worksheet rest duration is 45 minutes; the provisional 35-minute value is withdrawn.',local='Bakery batches dough worksheet: local calculation used grams for water. No bakery workstation received the worksheet.',report='I said the bakery workstation had the worksheet; I have only my message, not a workstation response.',open='The shift baker still needs to check the worksheet against one actual mixing batch.',tail='Resume the dough worksheet here. Cancel the optional humidity graph, but keep the actual-batch comparison before rollout.',boundary='Icing worksheet permits millilitres for flavouring. That separate worksheet does not change the dough water unit.'),
 dict(key='puppet_workshop',other='stage_props',name='Puppet workshop',task='joint checklist',neighbor='hinge checklist',constraint='Describe left and right from the puppeteer viewpoint because the assembly bench faces the puppet back.',old='Joint checklist used a draft component code wrist_tmp.',current='Joint checklist component code is wrist_link. The wrist_tmp draft code has been retired.',local='Puppet workshop joint checklist: local sample labels follow the puppeteer viewpoint. No assembly station was updated.',report='I claimed the assembly station was updated, without an assembly-station output to confirm it.',open='The maker still needs to test the wrist joint with the heavy sleeve fitted.',tail='Continue joint-checklist work in the next chat. Cancel only the optional pose photographs; retain the heavy-sleeve test.',boundary='Stage props hinge checklist defines left and right from the audience viewpoint. That convention does not apply to Puppet workshop.'),
 dict(key='recycling_rounds',name='Recycling rounds',task='glass collection route',neighbor='paper collection route',constraint='Keep the school stop after 09:00 because the collection vehicle must avoid the morning arrival crowd.',old='Glass collection route assigned the school stop identifier S-TEMP.',current='Glass collection route uses school stop identifier S-19. S-TEMP is obsolete.',local='Recycling rounds glass collection route: local schedule check placed the school stop at 09:20. No driver device was synchronized.',report='I reported that drivers had received the route, although I did not inspect any device acknowledgement.',open='The route supervisor still must confirm access at the narrow school gate.',tail='Continue the glass route from this chat. Drop the optional route-colour redesign; preserve the after-09:00 rule and gate-access confirmation.',boundary='Paper collection route reaches the school at 11:00. It is a separate route and does not set the glass route arrival time.'),
]

def dump(name,value):
    (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
fixture={'schema_version':1,'fictional':True,'split':'holdout','histories':[]}
gold={'schema_version':1,'fictional':True,'questions':{}}
for n,c in enumerate(CASES,801):
    hid=f'hold_{n}'; key=c['key']; events={}
    def event(suffix,typ,text):
        v={'id':f'{hid}_{suffix}','type':typ,'text':text}; events[suffix]=v
        if typ=='tool':
            v['tool_capture']={'tool_name':'exec_command','tool_input':{'command':f'FICTIONAL ONLY: read {c["name"]} {suffix} snapshot'},'tool_response':{'output':text,'exit_code':0}}
        return v
    old=event('obsolete','tool',c['name']+': '+c['old'])
    scope=event('request','user',f'In project {c["name"]}, work on the {c["task"]}. Treat the {c["neighbor"]} as a distinct task. '+c['constraint'])
    ack=event('ack','assistant',f'I will prepare the {c["task"]} locally and keep its review step visible.')
    local=event('local','tool',c['local'])
    current=event('current','user',f'Continuing the same {c["task"]} from our first chat. '+c['current'])
    deletion={'id':f'{hid}_delete','type':'delete_observation','observation_event_id':old['id']}
    explanation=event('forget','user','Delete the obsolete draft observation above from memory. Use the replacement I just gave for the current task; the old draft must not return as current guidance.')
    report=event('report','assistant',c['report'])
    pending=event('pending','user',c['open'])
    tail=event('tail','user',c['tail'])
    boundary=event('boundary','user',c['boundary'])
    sessions=[{'id':f'{hid}_chat1','project_key':key,'events':[old,scope,ack,local],'checkpoints':[{'after_event_id':local['id']}]},
              {'id':f'{hid}_chat2','project_key':key,'events':[current,deletion,explanation,report,pending],'checkpoints':[{'after_event_id':pending['id']}]}]
    projects=[key]
    if c.get('other'):
        projects.append(c['other'])
        sessions.append({'id':f'{hid}_otherchat','project_key':c['other'],'events':[boundary],'checkpoints':[]})
    else:
        sessions[1]['events'].append(boundary)
    # Last event of the last primary chat is a late user instruction after its checkpoint.
    sessions[1]['events'].append(tail)
    def fact(suffix,quote):
        e=events[suffix]; start=e['text'].index(quote)
        return {'text':quote,'support':[{'event_id':e['id'],'quote':quote,'start':start,'end':start+len(quote)}]}
    specifications=[
      ('constraint',f'What requirement governs the {c["task"]}, and why was it chosen?',[fact('request',c['constraint'])],[f'The {c["neighbor"]} rule overrides the stated requirement for the {c["task"]}.']),
      ('late_user',f'What did the final user instruction remove from the {c["task"]}, and what must continue?',[fact('tail',c['tail'])],[f'The final instruction cancelled the entire {c["task"]}.']),
      ('open_work',f'Which review or real-world check remains open for the {c["task"]}?',[fact('pending',c['open'])],['The local successful check completed the pending human or field review.']),
      ('execution_attribution',f'What does the captured execution actually establish for the {c["task"]}, and how should the assistant claim about shared use be treated?',[fact('local',c['local']),fact('report',c['report'])],['The assistant claim is observed execution evidence of shared deployment.','The captured local output proves rollout completed.']),
      ('project_boundary',f'What rule belongs to the separate {c["neighbor"]}, and may it change the {c["task"]}?',[fact('boundary',c['boundary'])],[f'The separate {c["neighbor"]} rule changes the current {c["task"]}.']),
      ('evidence_scope',f'After the obsolete observation was deleted, what replacement setting remains authoritative for the {c["task"]}?',[fact('current',c['current'])],['The deleted draft is still the current setting.'])
    ]
    questions=[]
    for i,(cat,q,facts,bad) in enumerate(specifications,1):
        question={'id':f'q{i}','question':q,'project_key':key,'stage':'final'}
        if i==5 and len(projects)==2:
            del question['project_key']; question['project_keys']=projects[:]
        questions.append(question)
        gold['questions'][f'{hid}/q{i}']={'category':cat,'required_facts':facts,'forbidden_conclusions':bad}
    fixture['histories'].append({'id':hid,'project_keys':projects,'sessions':sessions,'questions':questions})
dump('holdout-fixture.json',fixture); dump('holdout-gold.json',gold)
# Execute complete corrected standalone public validator unchanged.
namespace={}
exec(compile(CONTRACT.read_text(),str(CONTRACT),'exec'),namespace)
namespace['fixture'](ROOT/'holdout-fixture.json')
assert len(fixture['histories'])==16 and len(gold['questions'])==96
support_count=0; captures=0
for h in fixture['histories']:
    seen={}; deleted=set(); locations={}; ids=set()
    for s in h['sessions']:
        assert s['id'] not in ids; ids.add(s['id'])
        localids=[e['id'] for e in s['events']]
        for cp in s['checkpoints']: assert cp['after_event_id'] in localids
        for e in s['events']:
            if e['type']=='delete_observation':
                ref=e['observation_event_id']; assert ref in seen and ref not in deleted
                assert locations[ref]==s['project_key']; deleted.add(ref)
            else:
                assert e['id'] not in seen; seen[e['id']]=e; locations[e['id']]=s['project_key']
                if e['type']=='tool':
                    cap=e['tool_capture']; assert cap['tool_name']=='exec_command'
                    assert cap['tool_response']['output']==e['text']; captures+=1
    primary=h['sessions'][1]
    assert primary['events'][-1]['type']=='user'
    assert primary['events'][-1]['id'].endswith('_tail')
    assert [e['id'] for e in primary['events']].index(primary['checkpoints'][-1]['after_event_id']) < len(primary['events'])-1
    assert sum(len(s['checkpoints']) for s in h['sessions'])==2
    for q in h['questions']:
        entry=gold['questions'][h['id']+'/'+q['id']]
        scopes=q.get('project_keys',[q.get('project_key')])
        assert 1<=len(entry['required_facts'])<=3 and entry['forbidden_conclusions']
        for f in entry['required_facts']:
            assert f['support']
            for sup in f['support']:
                eid=sup['event_id']; assert eid in seen and eid not in deleted
                assert locations[eid] in scopes
                assert seen[eid]['text'][sup['start']:sup['end']]==sup['quote']==f['text']
                support_count+=1
manifest={
 'schema_version':1,'fictional':True,'status':'sealed',
 'counts':{'histories':16,'questions':96,'required_facts':support_count,'tool_captures':captures,'checkpoints':32,'deleted_observations':16,'two_project_histories':8,'one_project_histories':8},
 'hashes':{n:sha(ROOT/n) for n in ['author.py','holdout-fixture.json','holdout-gold.json']},
 'allowed_reads':[{'path':str(p),'sha256':sha(p)} for p in [CONTRACT,RECEIPT]],
 'validation':{'complete_public_fixture_validator':'passed; corrected standalone source executed unchanged with empty initial globals','gold_coverage':'96/96; hold_801 through hold_816; q1 through q6','source_offsets':'all exact Python Unicode substring offsets; no deleted source cited','scope':'all gold supports in requested scope; deletion target precedes delete in same project','checkpoints':'two per history, same-session target; final primary instruction follows last checkpoint','captures':'exec_command only; inert command strings never executed; output equals full event text','facts':'all required facts are full source quotations; assistant assertions explicitly unverified; open human checks distinct from local checks','repeatability':'deterministic author; two-run byte equality verified externally before handoff','public_preparation_issue':'First public extraction omitted typing.Any; parent corrected import before final validation and sealing. No fixture content was changed in response.'},
 'independence':{'new_fictional_authorship':True,'single_author_histories_and_gold':True,'platform_historical_summary_visible':True,'platform_historical_summary_used':False,'private_fixture_gold_report_reads':False,'product_runtime_helper_judge_reads':False,'other_chats_or_memories_read':False,'native_model_store_api_calls':False,'limitations':['Single author designed both histories and gold.','Shared public-schema-driven structure can make the cases easier than unconstrained histories.','Short fictional histories do not establish real-world performance.']},
 'read_audit':'Only the two allowed public contract files and newly authored output were read. No other files were inspected. Skill files were not loaded because the author isolation boundary forbids them.',
 'disclosure':'Fixture and gold contents remain private pending parent freeze and explicit release authorization.'}
dump('sealed-manifest.json',manifest)
dump('sealed-input-pins.json',{'files':{n:sha(ROOT/n) for n in ['holdout-fixture.json','holdout-gold.json','author.py','sealed-manifest.json']}})
print(json.dumps({'counts':manifest['counts'],'pins_sha256':sha(ROOT/'sealed-input-pins.json'),'manifest_sha256':sha(ROOT/'sealed-manifest.json')},sort_keys=True))
