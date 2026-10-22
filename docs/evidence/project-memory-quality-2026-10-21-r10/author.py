"""Deterministic independent fictional holdout author; offline, no model dispatch."""
from pathlib import Path
import hashlib
import json
import runpy

ROOT = Path(__file__).resolve().parent
# Each row is a distinct fictional decision history. No commands below are executed.
ROWS = [
 dict(name='Tern exhibit captions', key='tern_exhibit',
 initial='For Tern exhibit captions, keep the curator-approved Portuguese punctuation verbatim. The brass plaques have already been engraved, so automatic typography cleanup would make the screen and plaques disagree.',
 claim='I have rolled the corrected captions out to all gallery screens.',
 local='LOCAL caption preview: 18 of 18 punctuation comparisons passed. Gallery screen controller was not contacted.',
 stale='Scratch observation: all gallery controllers have acknowledged the new captions.',
 decision='Use the reviewed caption file for the preview, but postpone screen rollout until the curator signs the Arabic line breaks; the punctuation check cannot approve line breaks.',
 check='LOCAL line-break report: Arabic caption 7 clips at 1024 pixels. No controller receipts were loaded.',
 late='Continue the caption work: the curator now wants caption 7 split into two panels. Keep the Portuguese wording unchanged and leave controller rollout pending until both panels are reviewed.',
 open='Next I will create the two-panel preview and ask for the review; this is a plan, not a controller update.',
 final='Only the preview is authorized. We still need the two-panel review before any gallery rollout.',
 secondary=('tern_shop','The museum shop uses its own captions. Its short Portuguese slogan may be rewritten for the postcard print run; that permission does not cover exhibit plaques.'),
 reasonq='Why must the exhibit Portuguese punctuation stay unchanged?',
 evidenceq='Did the gallery-wide caption rollout actually get verified?',
 decisionq='What approval blocked screen rollout before the later continuation?',
 lateq='What did the late caption continuation change, and what did it preserve?',
 scopeq='Which caption rewrite permissions apply to the shop and to the exhibit?'),
 dict(name='Cedar ferry queue', key='cedar_ferry',
 initial='For Cedar ferry queues, retain arrival order within each wheelchair group. Families board together and a shorter queue estimate is not a reason to split them.',
 claim='The new queue order is active at the dock.',
 local='LOCAL replay of 42 boarding groups: family adjacency preserved. Dock dispatcher connection disabled.',
 stale='Scratch observation: the dock supervisor approved splitting families on busy days.',
 decision='Keep the dispatcher cutover closed. A replay cannot establish that the physical ramp marshal receives group boundaries, and that handoff needs an observed drill.',
 check='LOCAL marshal packet sample: group boundaries present in JSON. Physical ramp drill has no recorded result.',
 late='Pick this up again for the rain drill. Add an umbrella-storage stop before the ramp, keep families together, and leave dock cutover closed until the marshal observes the new route.',
 open='I can prepare route cards next; I have not run the rain drill.',
 final='The remaining work is the rain-route drill with the marshal. Do not infer live dock use from the local replay.',
 reasonq='What ordering constraint protects families, and why?',
 evidenceq='What was checked about boarding order, and does it establish live dock use?',
 decisionq='Why was dispatcher cutover kept closed?',
 lateq='How did the rain-drill continuation change the work?',
 scopeq='What still needs to happen before the dock cutover?'),
 dict(name='Mica seed library', key='mica_seed',
 initial='For Mica seed envelopes, show the donor lot code exactly as written. Growers use those codes to report germination and normalized codes would break their paper references.',
 claim='I published the corrected labels to the library printer.',
 local='LOCAL PDF proof: 63 donor lot codes match ledger transcription. Printer spool was not queried.',
 stale='Scratch observation: the librarian permits replacing all donor lot codes with new sequential numbers.',
 decision='Choose folded inserts instead of shrinking the lot-code font. Older members need the existing type size; the folding mockup still needs a physical fit check.',
 check='LOCAL insert layout: all 63 codes fit at 14 points. No envelope fit measurement recorded.',
 late='Resume for the spring exchange. Use waterproof stock for the folded inserts because the table is outdoors, and repeat the physical fit check with that thicker stock.',
 open='I will request a waterproof-stock sample; the fit check is not complete.',
 final='Authorize a sample only. Printer publication and the thicker-stock fit check remain open.',
 secondary=('mica_borrowing','For the separate Mica tool-borrowing cards, sequential member numbers replace handwritten aliases. This does not change seed donor lot codes.'),
 reasonq='Why are exact donor lot codes required?',
 evidenceq='What evidence supports the claimed printer publication?',
 decisionq='Why were folded inserts selected?',
 lateq='What new material requirement arrived with the spring exchange continuation?',
 scopeq='Where are sequential numbers allowed, and where must original codes remain?'),
 dict(name='Oriole audio guide', key='oriole_audio',
 initial='For the Oriole audio guide, keep the full quiet pause after each bell. Visitors use that silence to locate the next doorway; shortening pauses for file size defeats the navigation cue.',
 claim='The replacement guide is already on every loan device.',
 local='LOCAL waveform audit: twelve bell pauses each measure 4 seconds. Device inventory unavailable.',
 stale='Scratch observation: visitors agreed that all pauses can be cut to half a second.',
 decision='Retain mono delivery because the loan devices have one speaker. Leave field acceptance open until a doorway walk confirms the bell can be heard over the ventilation.',
 check='LOCAL mix export: mono; peak -3 dBFS. No doorway walk recording attached.',
 late='Return to the guide for the temporary corridor. Add a spoken left-turn cue after bell 8, keep its full pause, and repeat the doorway walk on that route.',
 open='I can draft the cue script, but no device transfer or doorway walk was performed in this session.',
 final='We still need script review, a temporary-route walk, and device transfer. The local mix audit closes none of those.',
 reasonq='Why must the bell pauses keep their full length?',
 evidenceq='Was the guide replacement on loan devices established?',
 decisionq='Why was mono delivery chosen and which acceptance check stayed open?',
 lateq='What does the temporary corridor require?',
 scopeq='Which unfinished steps remain after the continuation?'),
 dict(name='Bracken archive index', key='bracken_archive',
 initial='For the Bracken archive index, preserve obsolete street names as searchable aliases. Family researchers bring addresses from old letters and may not know the modern street names.',
 claim='The refreshed index is live in the reading room.',
 local='LOCAL search snapshot: 27 obsolete street aliases return their linked boxes. Reading-room server was not accessed.',
 stale='Scratch observation: the archivist approved deleting all obsolete street aliases.',
 decision='Select an alias warning rather than redirecting silently. Two old streets share a modern name, so researchers need to see which historical district matched.',
 check='LOCAL warning mockup: historical district displayed for both ambiguous streets. Server deployment status unknown.',
 late='Continue with the annex collection. Add the annex district to the warning, retain every old-name alias, and wait for an archivist to check the two ambiguous examples.',
 open='The annex mockup can be prepared next. I have not obtained archivist approval.',
 final='Only local mockups are approved for now; reading-room publication still depends on the ambiguous-example review.',
 secondary=('bracken_events','For Bracken event posters, use only modern street names so current attendees can find the entrance. The archive search alias policy is separate.'),
 reasonq='Why must obsolete street aliases remain searchable?',
 evidenceq='What do the observations establish about the reading-room index?',
 decisionq='Why should ambiguous alias matches show a warning?',
 lateq='What was added when the annex work resumed?',
 scopeq='How do street-name rules differ between archive search and event posters?'),
 dict(name='Lumen rehearsal lights', key='lumen_rehearsal',
 initial='For Lumen rehearsal cues, keep the blackout transition at three seconds. The dancer uses that interval to leave a narrow platform without seeing the edge.',
 claim='I sent the revised cue list to the theater console.',
 local='LOCAL cue simulation: blackout transition 3.0 seconds. Console network access not configured.',
 stale='Scratch observation: the dancer approved an instantaneous blackout.',
 decision='Use the amber sidelight for the rehearsal preview, but defer show approval until the stage manager checks that it does not reveal the hidden prop.',
 check='LOCAL fixture plot: amber sidelight overlaps prop zone by 20 cm. No on-stage visibility check logged.',
 late='Resume after the platform move. Shift the amber fixture one meter upstage, retain the three-second blackout, and repeat the prop visibility check from the front row.',
 open='I will revise the plot next; this is not a console upload.',
 final='The front-row visibility check and the console upload are still open. Approval covers the revised plot only.',
 reasonq='What blackout timing is fixed and what is the reason?',
 evidenceq='Did the revised cue list reach the theater console?',
 decisionq='What condition blocked show approval of the amber sidelight?',
 lateq='How did the platform move affect the next work?',
 scopeq='What is authorized now and what remains unfinished?'),
 dict(name='Kestrel map kiosk', key='kestrel_kiosk',
 initial='For the Kestrel trail kiosk, retain the paper-map grid references. Rangers give directions by radio using that grid, so new screen coordinates cannot replace it.',
 claim='The updated map is running on the visitor-center kiosk.',
 local='LOCAL map render: all 16 trailheads carry paper-grid references. Visitor-center kiosk not connected.',
 stale='Scratch observation: the rangers replaced their radio grid with screen coordinates.',
 decision='Show the washed-out bridge as closed with a detour note, not as a missing path. A missing segment makes visitors think the data failed to load.',
 check='LOCAL detour preview: bridge marked CLOSED; detour line visible. Physical sign placement not verified.',
 late='Continue for the north entrance. Add a north-entrance detour starting point but keep the bridge closed; wait for a ranger to confirm the detour signs before kiosk release.',
 open='I can draw the extra detour start. No ranger confirmation has arrived.',
 final='Keep release pending the detour-sign check. The local render does not confirm signs on the trail.',
 secondary=('kestrel_webshop','The Kestrel souvenir webshop can omit map grids on decorative posters. Those posters are not navigation aids and do not change the trail kiosk requirement.'),
 reasonq='Why are paper-map grid references required on the kiosk?',
 evidenceq='What establishes whether the visitor-center kiosk is running the update?',
 decisionq='Why display a closed bridge instead of hiding the path?',
 lateq='What did the north-entrance continuation require?',
 scopeq='Can the decorative-poster grid exception be applied to the kiosk?'),
 dict(name='Aster repair intake', key='aster_repair',
 initial='For Aster repair intake, keep the owner-written symptom description visible beside the mechanic summary. Intermittent faults often disappear on the bench, so the original wording remains useful.',
 claim='The new intake form has been deployed to all repair desks.',
 local='LOCAL form test: owner text and mechanic summary appear in separate fields. Repair desk endpoints not contacted.',
 stale='Scratch observation: the workshop manager approved replacing owner descriptions with mechanic summaries.',
 decision='Use a separate accessories checklist instead of appending accessories to the symptom text. Technicians must tell a missing charger from a reported charging fault.',
 check='LOCAL sample ticket: charger accessory unchecked; charging fault remains in owner text. No desk acceptance run recorded.',
 late='Resume for postal repairs. Add a parcel-damage field outside the symptom description, and keep desk rollout pending a postal-desk acceptance run.',
 open='I can prepare the postal sample next; no acceptance run has happened.',
 final='Only the postal sample is authorized. Keep the original symptom wording and leave desk rollout open.',
 reasonq='Why retain owner wording beside a mechanic summary?',
 evidenceq='Does the captured test prove deployment at repair desks?',
 decisionq='Why is the accessories checklist separate?',
 lateq='What new field and acceptance requirement came with postal repairs?',
 scopeq='What is the current authorization and rollout status?'),
 dict(name='Flint chamber schedule', key='flint_music',
 initial='For Flint chamber rehearsals, preserve a ten-minute instrument acclimation interval before tuning. The hall is cold and tuning immediately after arrival wastes the ensemble session.',
 claim='The revised schedule was mailed to every player.',
 local='LOCAL schedule check: acclimation interval 10 minutes before tuning. Mail transport disabled.',
 stale='Scratch observation: every player agreed to skip acclimation.',
 decision='Move the quartet coaching after the ensemble break rather than shortening acclimation. The coach can arrive later, while the instruments still need the temperature change.',
 check='LOCAL calendar draft: coaching 15:20, acclimation unchanged. No invitation delivery receipts.',
 late='Continue with the guest violist. Add a chair setup slot before acclimation; do not absorb those minutes into the ten-minute interval, and hold mail until the guest confirms arrival.',
 open='I will update the draft, but I have no guest arrival confirmation.',
 final='The guest confirmation and schedule mailing remain open. The interval stays ten minutes.',
 secondary=('flint_lectures','Flint lecture sessions may begin immediately when speakers arrive; no instrument acclimation interval applies to that separate program.'),
 reasonq='Why is the acclimation interval fixed at ten minutes?',
 evidenceq='Was schedule mailing verified?',
 decisionq='Why move coaching instead of reducing acclimation?',
 lateq='What did the guest violist continuation add?',
 scopeq='Which timing rule belongs to music rehearsals rather than lectures?'),
 dict(name='Plover textile labels', key='plover_textiles',
 initial='For Plover textile labels, retain the weave batch identifier on every swatch. The dye appears different under daylight and customers must be able to reorder the same weave batch.',
 claim='The batch labels are now printed on the showroom swatches.',
 local='LOCAL label sheet: 34 swatches include weave batch identifiers. Showroom stock not inspected.',
 stale='Scratch observation: sales approved dropping all weave batch identifiers.',
 decision='Use a two-sided tag instead of reducing the care text. Laundries need the full temperature instruction, even when it crowds the front side.',
 check='LOCAL tag proof: care instruction complete on reverse. Hole-punch position has not been tested on cloth.',
 late='Resume for the linen samples. Move the punch away from the loose edge, preserve both batch code and care text, and test a physical tag before printing the run.',
 open='I can prepare the moved-punch proof; physical tag testing remains undone.',
 final='Only a proof and one physical test tag are authorized. The showroom print run is pending that test.',
 reasonq='Why must weave batch identifiers remain on every swatch?',
 evidenceq='What supports the claim that showroom labels were printed?',
 decisionq='Why was a two-sided tag selected?',
 lateq='What does the linen-sample continuation change and preserve?',
 scopeq='Which production steps are authorized and which remain pending?'),
 dict(name='Juniper bus audio', key='juniper_bus',
 initial='For Juniper bus announcements, keep the stop number spoken before the stop name. Riders with limited vision identify request stops by number on the route card.',
 claim='The announcement pack is installed across the bus fleet.',
 local='LOCAL audio sequence: 51 stops speak number before name. Fleet units not queried.',
 stale='Scratch observation: the transit group approved omitting spoken stop numbers.',
 decision='Keep the bilingual names in two separate clips because the old player truncates one long combined clip. Hardware playback still needs to be checked on a spare player.',
 check='LOCAL export: longest single clip 7.2 seconds. Spare-player playback has no recorded outcome.',
 late='Return for the market-day diversion. Add temporary stop 52 with its number first, retain separate language clips, and test the diversion sequence on the spare player.',
 open='I will assemble the diversion test pack next; it has not been loaded onto a bus.',
 final='Fleet installation remains pending hardware playback acceptance. The temporary stop is an addition, not a renumbering of existing stops.',
 secondary=('juniper_walking','Juniper walking-tour audio uses landmark names only; it is a separate tour with no numbered request stops.'),
 reasonq='Why must bus announcements speak the number first?',
 evidenceq='Did the captured sequence check establish fleet installation?',
 decisionq='Why are the bilingual names kept in separate clips?',
 lateq='What changes for market-day diversion and what must stay stable?',
 scopeq='Can the walking-tour naming convention replace the bus numbering rule?'),
 dict(name='Sable pottery kiln log', key='sable_kiln',
 initial='For Sable kiln logs, preserve the manually written cone reading beside the temperature graph. The cone records heatwork, and the thermocouple trace is not a substitute.',
 claim='The revised log template is active on the studio tablet.',
 local='LOCAL template preview: cone reading and graph both visible. Studio tablet was not reached.',
 stale='Scratch observation: the studio agreed to delete cone readings once a graph is present.',
 decision='Store glaze samples as separate log attachments so the log can distinguish two glazes tested in one firing. The sample labels still need a potter review.',
 check='LOCAL sample log: two glaze attachments with separate labels. Potter review status absent.',
 late='Continue for the shared firing. Add a maker field to each glaze attachment, keep the cone reading, and ask both potters to check their sample labels.',
 open='I will prepare the shared-firing preview; neither potter review has been received.',
 final='Tablet activation remains open until both sample-label reviews. The temperature graph cannot replace the manual cone reading.',
 reasonq='Why must the cone reading remain beside the graph?',
 evidenceq='What establishes whether the tablet template is active?',
 decisionq='Why separate glaze samples into attachments?',
 lateq='What does the shared-firing continuation require?',
 scopeq='What still blocks tablet activation?'),
 dict(name='Willow translation desk', key='willow_translation',
 initial='For Willow festival translation cards, keep the performer names in their supplied script. Performers use those spellings on stage and transliteration alone would make credits inconsistent.',
 claim='The corrected cards have been placed at every information desk.',
 local='LOCAL card proof: 22 supplied-script performer names preserved. Information desks not inspected.',
 stale='Scratch observation: the festival lead approved Latin-only performer credits.',
 decision='Place the pronunciation guide below the original name instead of substituting it. Volunteers need a speaking aid and performers still need recognizable credits.',
 check='LOCAL two-line proof: original name above pronunciation guide. Volunteer read-through not recorded.',
 late='Resume for the evening shift. Add a low-light contrast proof with the same original names, and wait for the evening volunteers to read it before desk distribution.',
 open='I can prepare the contrast proof next; the evening read-through has not happened.',
 final='Distribution is still pending the evening read-through. Do not treat the proof as evidence that desks received cards.',
 secondary=('willow_badges','Willow equipment badges use Latin inventory abbreviations only. These are equipment identifiers, not performer credits.'),
 reasonq='Why retain performer names in the supplied script?',
 evidenceq='Were the cards at information desks verified?',
 decisionq='Why place the pronunciation guide below the name?',
 lateq='What new review was requested for the evening shift?',
 scopeq='Do the equipment-badge abbreviations authorize changing performer credits?'),
 dict(name='Rook observatory rota', key='rook_observatory',
 initial='For Rook telescope bookings, retain the dark-adaptation interval before each public viewing block. Visitors coming from the lit lobby need time before faint objects become visible.',
 claim='The rota is already posted on the observatory booking board.',
 local='LOCAL rota audit: each viewing block has 15 minutes of dark adaptation. Booking board not accessed.',
 stale='Scratch observation: the astronomy team approved removing dark-adaptation intervals.',
 decision='Put the school question session after viewing so lobby lights do not interrupt adaptation. The volunteer lead still needs to approve staffing for that later session.',
 check='LOCAL rota draft: questions follow viewing; staffing approval blank. No booking-board publication receipt.',
 late='Continue for the cloud backup plan. Add an indoor star-chart session only for cancelled viewing blocks, keep adaptation for any live viewing, and ask the volunteer lead about backup staffing too.',
 open='I can draft the backup rota. Staffing approval has not been obtained.',
 final='The lead must approve both normal and cloud-backup staffing before the booking board is updated.',
 reasonq='Why keep dark-adaptation time before public viewing?',
 evidenceq='What was actually checked about the rota publication?',
 decisionq='Why place school questions after viewing?',
 lateq='When does the new cloud backup apply, and what timing stays required?',
 scopeq='Which approvals remain before booking-board publication?'),
 dict(name='Elm parcel locker notices', key='elm_lockers',
 initial='For Elm locker notices, retain the collection desk phone extension on every printed slip. Some residents have no smartphone and need a way to ask about an inaccessible compartment.',
 claim='The revised slips are now being dispensed by every locker bank.',
 local='LOCAL slip render: desk extension present on all 8 layouts. Locker dispensers not connected.',
 stale='Scratch observation: housing approved replacing the desk extension with a QR code only.',
 decision='Use a large compartment letter with the number rather than color alone. The monochrome dispenser cannot preserve the colored-bank distinction.',
 check='LOCAL grayscale proof: letter and number readable; QR code optional. No dispenser paper-feed test result.',
 late='Resume for the basement bank. Add a stairs warning above the compartment label, keep the phone extension, and wait for a paper-feed test at that bank before distribution.',
 open='I will prepare the basement proof. I have not run the dispenser test.',
 final='Distribution remains pending the basement paper-feed test; a readable local proof does not establish dispensing.',
 secondary=('elm_newsletter','The Elm newsletter may use QR-only links for optional photo galleries. That permission does not remove phone access from locker collection slips.'),
 reasonq='Why must every collection slip keep the desk phone extension?',
 evidenceq='Was dispensing of revised slips established?',
 decisionq='Why use a compartment letter and number instead of color alone?',
 lateq='What did the basement-bank continuation add?',
 scopeq='Does the newsletter QR exception change locker-slip phone access?'),
 dict(name='Finch orchard tasting', key='finch_orchard',
 initial='For Finch orchard tasting cards, keep the harvest plot code next to each cultivar. Fruit from the shaded plot ripens later, so cultivar alone does not explain the tasting notes.',
 claim='The new cards are already on all tasting tables.',
 local='LOCAL card proof: 12 cultivars include harvest plot codes. Tasting tables not inspected.',
 stale='Scratch observation: the orchard host approved deleting plot codes after sorting by cultivar.',
 decision='Group tasting cards by harvest date while retaining plot codes. Guests compare ripeness more fairly when samples were picked on the same day.',
 check='LOCAL card order: grouped by harvest date, plot codes retained. Host table-order review not recorded.',
 late='Continue for the late harvest. Add the frost-note field only to affected batches, preserve harvest-date grouping, and have the host review the new table order before printing.',
 open='I can prepare the frost-note proof; the host has not reviewed the late-harvest order.',
 final='Printing and table placement remain open until the host review. Plot codes still belong on every card.',
 reasonq='Why keep the harvest plot code beside each cultivar?',
 evidenceq='What proves whether the new cards reached tasting tables?',
 decisionq='Why group cards by harvest date?',
 lateq='What did the late-harvest continuation change and preserve?',
 scopeq='What remains open before cards can reach the tables?'),
]

def write(name,value):
    (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def author():
    histories=[]; gold={}; allids=set(); allsessions=set()
    for i,row in enumerate(ROWS,701):
        hid=f'hold_{i}'; main=row['key']; projects=[main]; sessions=[]; eventmap={}; projectmap={}; order={}
        def event(n,typ,text,project=main,**extras):
            eid=f'{hid}_e{n:02d}'
            assert eid not in allids; allids.add(eid)
            e={'id':eid,'type':typ,'text':text,**extras}
            eventmap[eid]=e; projectmap[eid]=project; order[eid]=len(order)
            return e
        def tool(n,text):
            return event(n,'tool',text,tool_capture={'tool_name':'fictional_offline_capture','tool_input':{'command':f'INERT FICTION ONLY: inspect {main} sample {n}'},'tool_response':{'exit_code':0,'output':text}})
        def session(n,project,events,checkpoint=False):
            sid=f'{hid}_s{n}'; assert sid not in allsessions; allsessions.add(sid)
            sessions.append({'id':sid,'project_key':project,'events':events,'checkpoints':[{'after_event_id':events[-1]['id']}] if checkpoint else []})
        session(1,main,[event(1,'user',row['initial']),event(2,'assistant',row['claim']),tool(3,row['local']),event(4,'assistant',row['stale'])],True)
        if row.get('secondary'):
            other,otext=row['secondary']; projects.append(other)
            session(2,other,[event(5,'user',otext,other),event(6,'assistant','Understood. I will keep this instruction within this separate project.',other)])
        session(3,main,[event(7,'delete_observation','Delete the scratch observation from the earlier session. It was an unverified note and must not support future answers.',observation_event_id=f'{hid}_e04'),event(8,'user',row['decision']),tool(9,row['check'])],True)
        session(4,main,[event(10,'user',row['late']),event(11,'assistant',row['open']),event(12,'user',row['final'])])
        qspec=[('constraint',row['reasonq'],[(1,row['initial'])],['The initial user constraint was waived.']),
               ('execution_attribution',row['evidenceq'],[(2,'The assistant reported: '+row['claim']),(3,row['local'])],['The assistant report proves execution or deployment.','A local check verifies the external destination.']),
               ('decision_reason',row['decisionq'],[(8,row['decision'])],['The named review or acceptance dependency was completed.']),
               ('late_continuation',row['lateq'],[(10,row['late'])],['The late continuation removed the preserved constraint.']),
               ('evidence_scope',f'For {row["name"]}, what did the later captured check establish, and what should still remain open after the last user instruction?',[(9,row['check']),(12,row['final'])],['The missing external check succeeded.','The deleted scratch observation supplies positive evidence.']),
               ('project_boundary' if row.get('secondary') else 'open_work',row['scopeq'],[(1,row['initial']),(5,row['secondary'][1])] if row.get('secondary') else [(10,row['late']),(12,row['final'])],['Permission from a different project overrides the main project constraint.'] if row.get('secondary') else ['The remaining work is already complete.'])]
        questions=[]
        for qi,(category,question,facts,forbidden) in enumerate(qspec,1):
            qid=f'q{qi}'; q={'id':qid,'question':question,'stage':'final'}
            q['project_keys' if row.get('secondary') and qi==6 else 'project_key']=projects if row.get('secondary') and qi==6 else main
            questions.append(q); required=[]
            for n,text in facts:
                eid=f'{hid}_e{n:02d}'; quote=eventmap[eid]['text']
                required.append({'text':text,'support':[{'event_id':eid,'quote':quote,'start':0,'end':len(quote)}]})
            gold[f'{hid}/{qid}']={'category':category,'required_facts':required,'forbidden_conclusions':forbidden+[row['stale']]}
        histories.append({'id':hid,'project_keys':projects,'sessions':sessions,'questions':questions})
    fixture={'schema_version':1,'fictional':True,'split':'holdout','histories':histories}
    answer={'schema_version':1,'fictional':True,'questions':gold}
    write('holdout-fixture.json',fixture); write('holdout-gold.json',answer)
    verify(fixture,answer)
    validator=runpy.run_path(str(ROOT/'public_validator.py'))
    validated=validator['fixture'](ROOT/'holdout-fixture.json')
    assert validated==fixture
    counts={'histories':len(histories),'questions':len(gold),'sessions':len(allsessions),'events':len(allids),'tool_captures':sum('tool_capture' in e for h in histories for s in h['sessions'] for e in s['events']),'checkpoints':sum(len(s['checkpoints']) for h in histories for s in h['sessions']),'deletions':sum(e['type']=='delete_observation' for h in histories for s in h['sessions'] for e in s['events'])}
    pins={n:sha(ROOT/n) for n in ['author.py','holdout-fixture.json','holdout-gold.json','public_validator.py']}
    write('manifest.json',{'schema_version':1,'fictional':True,'author_role':'independent sealed author','counts':counts,'input_pins':pins,'self_checks':'PASS: IDs; final question coverage; exact support offsets; source existence; same-project deletion order; deleted-source exclusion; capture shape; command inertness; project scope; checkpoint placement; continuation after checkpoint','public_validator':{'source':'scripts/project_memory_consolidation_experiment.py','read_lines':'38-137 inclusive','execution':'runpy.run_path(public_validator.py), then fixture(holdout-fixture.json); standard imports only; no product module imports','validator_file_sha256':pins['public_validator.py'],'source_span_sha256':hashlib.sha256((ROOT/'public_validator.py').read_text().split('\n\n',1)[1].encode()).hexdigest(),'result':'PASS; returned fixture equals generated fixture'},'disclosure':{'injected_memory_summary_visible':True,'injected_memory_summary_used':False,'prior_fixtures_gold_results_read':False,'product_runtime_or_judge_code_read':False,'optional_capture_interface_read':False,'model_dispatch':False,'semantic_scoring':False,'product_store_generation':False},'release_gate':'SEALED. Author must verify exact frozen protocol and product hashes before explicit freeze-plus-release; no release inferred from successful validation.','limitations':['No semantic scoring or runtime behavior tested.','Fictional captures are inert authored evidence, not executed commands.','Public validator span includes constant declarations and helper definitions; only fixture and its validation dependencies were invoked.','Exact implementation source span extraction read the source file into memory, retained only lines 38-137, and did not inspect or execute the remaining source.']})
    write('sealed-input-pins.json',{'schema_version':1,'status':'sealed_pending_protocol_and_product_hash_verification','files':pins,'manifest_sha256':sha(ROOT/'manifest.json'),'required_before_release':['Explicit freeze-plus-release instruction','Author independent comparison of frozen protocol and product hashes']})
    print(json.dumps({'directory':str(ROOT),'counts':counts,'sha256':{n:pins[n] for n in ['author.py','holdout-fixture.json','holdout-gold.json']},'public_validator_sha256':pins['public_validator.py'],'validation':'PASS'},indent=2))

def verify(fixture,gold):
    assert set(fixture)=={'schema_version','fictional','split','histories'}
    assert len(fixture['histories'])==16
    assert [h['id'] for h in fixture['histories']]==[f'hold_{i}' for i in range(701,717)]
    expected=set(); globalids=set(); sessions=set()
    for h in fixture['histories']:
        em={}; pm={}; deleted=set(); indexes={}; checkpoints=[]
        assert len(h['project_keys'])==len(set(h['project_keys']))
        for s in h['sessions']:
            assert s['id'] not in sessions; sessions.add(s['id'])
            assert s['project_key'] in h['project_keys']
            for e in s['events']:
                assert e['id'] not in globalids; globalids.add(e['id'])
                if e['type']=='delete_observation':
                    target=e['observation_event_id']; assert target in em
                    assert pm[target]==s['project_key']
                    assert em[target]['type'] in ('assistant','tool')
                    deleted.add(target)
                indexes[e['id']]=len(em); em[e['id']]=e; pm[e['id']]=s['project_key']
                if e['type']=='tool':
                    c=e['tool_capture']; assert set(c)=={'tool_name','tool_input','tool_response'}
                    assert c['tool_input']['command'].startswith('INERT FICTION ONLY:')
                    assert type(c['tool_response']['exit_code']) is int
                    assert c['tool_response']['output']==e['text']
                    assert len(json.dumps(c,ensure_ascii=False).encode())<=60000
            for cp in s['checkpoints']:
                assert cp['after_event_id'] in {e['id'] for e in s['events']}
                checkpoints.append(indexes[cp['after_event_id']])
        assert len(deleted)==1 and checkpoints
        assert any(e['type']=='user' and indexes[eid]>max(checkpoints) for eid,e in em.items())
        assert {q['id'] for q in h['questions']}=={f'q{i}' for i in range(1,7)}
        for q in h['questions']:
            k=f'{h["id"]}/{q["id"]}'; assert k not in expected; expected.add(k)
            assert q['stage']=='final'
            scopes=q.get('project_keys',[q.get('project_key')]); assert set(scopes)<=set(h['project_keys'])
            g=gold['questions'][k]; assert g['required_facts'] and g['forbidden_conclusions']
            for f in g['required_facts']:
                assert f['text'] and f['support']
                for span in f['support']:
                    eid=span['event_id']; assert eid in em and eid not in deleted
                    assert em[eid]['type']!='delete_observation'
                    assert pm[eid] in scopes
                    assert 0<=span['start']<span['end']<=len(em[eid]['text'])
                    assert em[eid]['text'][span['start']:span['end']]==span['quote']
    assert set(gold['questions'])==expected and len(expected)==96

if __name__=='__main__': author()
