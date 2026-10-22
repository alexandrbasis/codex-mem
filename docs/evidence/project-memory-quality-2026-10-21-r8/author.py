"""Independent sealed fictional R8 author. Commands below are inert corpus strings.
Exposure: platform MEMORY_SUMMARY was injected and visible but not used. No prior
fixtures, answers, reports, runtime algorithms, or memory files were consulted.
Only public validator source lines 38-131 were read. This is agent authorship,
not blind human validation. No model/native/API calls are performed by this file.
"""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path('/private/tmp/codex-mem-quality-20261021-r8-sealed')
# Each record is independently worded fictional material. The tuple fields are
# explicit source fragments, not outputs of a production memory implementation.
D = [
 dict(project='copper_atlas',other='lime_festival',
 goal='Make a printable route atlas for the Copper volunteer couriers.',reason='The riders lose reception in the tunnels.',
 constraints='Keep house numbers out of the atlas. Use only the west-district streets.',
 initial_extra='Include a folding cover.',
 revise='Replace the folding cover with a flat title sheet; the printable route atlas is still the deliverable. House numbers must still be omitted and the west-district limit still applies.',
 current_goal='A printable route atlas with a flat title sheet for volunteer couriers, needed because tunnel reception fails.',
 active='Omit house numbers and use only west-district streets; a flat title sheet replaces the folding cover.',
 old='Archive atlas report: 24 routes exported; folding cover attached; house numbers suppressed.',
 output='atlas check\ninput: draft-c\nwest routes: 24\nhouse-number check: PASS\ntitle sheet: MISSING\nexport: NOT RUN',code=4,command='atlas-sim inspect draft-c --west',
 observed='The latest atlas check passed the house-number check but found the title sheet missing; export did not run.',
 claim='I finished the flat title sheet and exported the whole atlas.',
 gate='I still need to approve the title-sheet proof before you may export. No proof has reached me yet; keep that approval open.',
 open='The title-sheet proof and user approval are outstanding; export must wait for that approval.',
 othertext='For Lime Festival only, publish the poster bundle without sending me a proof.',
 boundary='Lime Festival permits poster publication without a proof; that does not remove Copper Atlas approval.',
 forbidden='The old archived atlas report proves that the current flat-sheet atlas was exported.',
 questions=['What are we making now, and why does it need to work on paper?','Which layout request changed, and what location/privacy limits survived?','Did the current run actually produce the finished atlas?','What must happen before the export can go ahead?','Does the festival publishing permission let you skip the atlas review?','Can the removed report establish completion of the current export?']),
 dict(project='moth_archive',other='river_catalog',
 goal='Prepare a searchable index of the Moth theatre costume boxes.',reason='The wardrobe crew needs to find pieces during quick changes.',constraints='Use box codes rather than performer names. Leave the original scans untouched.',initial_extra='Add thumbnail previews.',
 revise='Drop thumbnails from this index. I want a text-only search index now, with box codes and unmodified source scans just as before.',current_goal='A text-only searchable costume-box index for quick-change lookup.',active='Thumbnails are canceled; box codes and preservation of original scans remain binding.',
 old='Previous indexing batch: thumbnails 61/61; index committed; scan fingerprint unchanged.',
 output='moth-index validate\nboxes parsed: 61\nperformer names: 0\nsource writes: 0\nsearch file: absent\ncommit skipped',code=7,command='moth-sim validate text-index',observed='Validation parsed 61 boxes without performer names or source writes, but the search file was absent and commit was skipped.',claim='The text search file is committed and the wardrobe team can use it.',
 gate='Before committing, wait for my list of approved box abbreviations. I have not sent that list. Ask me for it when you resume.',open='The approved abbreviation list is missing; the user must supply it before commit.',
 othertext='The River Catalog sandbox may overwrite its synthetic scans and commit immediately.',boundary='River Catalog overwrite and commit permission applies to its sandbox only, not Moth Archive sources or commit gate.',forbidden='The removed thumbnail batch is evidence that the text-only search file is committed.',
 questions=['What is the revised index meant to do for the crew?','Are previews still required, and can source scans be edited?','What did the recorded validation establish versus the assistant report?','What input is missing before committing the index?','Can the synthetic-scan permission be used on the theatre scans?','After deletion, what basis remains for saying the text index was committed?']),
 dict(project='cedar_seedbank',other='opal_club',
 goal='Create a seed-packet count sheet for Cedar Seedbank.',reason='Two volunteers must reconcile stock before the spring swap.',constraints='List batch IDs only, never donor names. Keep quantities in whole packets.',initial_extra='Sort by harvest month.',
 revise='Change the sorting to shelf number and remove the harvest-month grouping. The reconciliation sheet is still needed; batch IDs only and whole packets remain the rules.',current_goal='A shelf-sorted seed-packet count sheet for two-volunteer stock reconciliation before the swap.',active='Shelf-number sorting replaces harvest-month grouping; donor names stay excluded and amounts stay in whole packets.',
 old='seedbank export v1: harvest-month sheet ready; totals 310 packets; upload complete.',
 output='seed-sheet check shelf-v2\nrows: 43\ndonor fields: absent\nquantity units: integer packets\nshelf map: unresolved for 8 rows\nupload: blocked',code=3,command='cedar-sim check shelf-v2',observed='The shelf-v2 check found eight unresolved shelf mappings and blocked upload; donor fields were absent and quantities were integer packets.',claim='All shelves are resolved and the revised sheet is uploaded.',
 gate='I owe you the shelf conversion table. Until I send it, do not guess the eight positions or upload the sheet.',open='The user owes the shelf conversion table; eight positions must not be guessed and upload must wait.',
 othertext='For Opal Club raffle slips, round fractions upward and upload drafts without checking with me.',boundary='Opal Club rounding and draft-upload permission does not apply to Cedar Seedbank packet counts or its table gate.',forbidden='The deleted harvest-month export proves the shelf-number sheet was uploaded.',
 questions=['What is the count sheet supposed to support after the change?','Which sorting instruction is obsolete and which data rules still hold?','Was the shelf version uploaded according to the retained run?','Who owes the next input, and what does its absence prevent?','May the raffle rounding and upload rules govern the seed sheet?','Is the erased earlier export a valid completion source for this version?']),
 dict(project='violet_beacon',other='granite_demo',
 goal='Build a vibration-only alert pattern for the Violet museum pager.',reason='The night guard must receive alerts without waking visitors at the overnight exhibit.',constraints='Never enable sound. Keep every pulse shorter than 400 milliseconds.',initial_extra='Use three pulses per alert.',
 revise='Use two pulses instead of three. This is still the quiet museum pager: sound stays disabled and the 400-millisecond pulse ceiling remains.',current_goal='A two-pulse vibration-only museum pager alert that avoids waking overnight visitors.',active='Two pulses replace three; sound remains disabled and every pulse must be shorter than 400 milliseconds.',
 old='pager build 12: three-pulse profile flashed; vibration test passed; ready.',
 output='pager profile audit\nprofile: two-pulse\nsound: off\npulses_ms: [250, 250]\ndevice handshake: timeout\nflash attempted: no',code=12,command='violet-sim audit two-pulse',observed='The profile audit showed sound off and two 250-millisecond pulses, but handshake timed out and no flash was attempted.',claim='I flashed the two-pulse profile to the museum pager successfully.',
 gate='I have not connected the spare pager. Wait for me to connect it and explicitly confirm the device label before any flash.',open='The user must connect the spare pager and confirm its device label before flashing.',
 othertext='Granite Demo is a bench simulator; turn on its buzzer and flash any sample profile automatically.',boundary='Granite Demo buzzer and automatic-flash permission is restricted to that bench simulator, not Violet Beacon.',forbidden='The removed build-12 log establishes that the two-pulse profile is flashed.',
 questions=['Which alert pattern is wanted now and what is the reason for quiet operation?','What changed about pulse count, and what limits remain?','Was the pager flashed in the latest captured operation?','What human step is needed before a flash?','Does the bench-demo instruction authorize sound on the museum pager?','Can you cite the deleted build log as proof of the new profile being on-device?']),
 dict(project='cobalt_labels',other='amber_samples',
 goal='Generate shelf labels for the Cobalt tool library.',reason='Borrowers should match returned tools to the correct bay without asking staff.',constraints='Print bay codes in at least 18-point type. Do not put borrower identifiers on labels.',initial_extra='Add a QR code to each label.',
 revise='Forget the QR codes; replace them with plain item numbers. Keep the large bay codes and the ban on borrower identifiers.',current_goal='Shelf labels using plain item numbers and large bay codes to help tool returns without staff assistance.',active='Plain item numbers replace QR codes; bay codes must be at least 18 point and borrower identifiers remain forbidden.',
 old='label job 90: QR plates rendered; all 18-point bay codes; spool accepted.',
 output='label-job preview plain-91\nitem numbers: present\nbay font: 18pt\nborrower identifiers: none\npaper stock: unknown\nspool request: suppressed',code=6,command='cobalt-sim preview plain-91',observed='Preview showed item numbers, 18-point bay codes, and no borrower identifiers, but unknown paper stock suppressed the spool request.',claim='The plain-number labels are printed and sitting in the output tray.',
 gate='I need to measure the label stock before printing. I have not supplied its dimensions, so leave the print job pending.',open='Label-stock dimensions from the user are missing and printing remains pending.',
 othertext='Amber Samples uses disposable sheets. Send its sample labels straight to the printer on the default stock.',boundary='Amber Samples default-stock printing permission does not supply Cobalt label dimensions or permit its pending print.',forbidden='The erased QR spool receipt proves the plain-number labels were printed.',
 questions=['What should the labels help borrowers do, and what identifier is wanted now?','Are QR codes required anymore, and which typography/privacy rules remain?','What can be said about actual printing from the latest capture?','Why is the print still pending?','Can the sample-sheet default-stock permission resolve this print gate?','Does the deleted QR receipt establish that the revised labels reached the tray?']),
 dict(project='juniper_audio',other='saffron_stage',
 goal='Assemble a spoken orientation track for Juniper Garden volunteers.',reason='New volunteers need directions while their hands are occupied carrying trays.',constraints='Keep the track under four minutes. Include the emergency meeting point verbatim.',initial_extra='Put music under the introduction.',
 revise='Remove the music request entirely. A dry spoken track is what I want; retain the under-four-minute limit and the exact emergency meeting-point wording.',current_goal='A dry spoken orientation track for hands-busy garden volunteers.',active='Background music is canceled; duration must remain under four minutes and emergency meeting-point wording must be verbatim.',
 old='orientation mix 2: music bed attached; duration 3:42; final delivered.',
 output='orientation render-check dry3\nduration: 3:36\nmusic layers: 0\nemergency line: placeholder\nrender file: not created',code=9,command='juniper-sim render-check dry3',observed='The check reported a 3:36 dry track with a placeholder emergency line and no rendered file.',claim='The finished dry orientation audio has been rendered and delivered.',
 gate='The safety steward owes me the exact meeting-point sentence; I will pass it on. I have not received it, and delivery must wait for that wording.',open='The safety steward must supply the exact emergency sentence through the user before delivery.',
 othertext='For Saffron Stage rehearsal audio, use any temporary safety line and deliver a rough mix today.',boundary='Saffron Stage permits a temporary line for rehearsal only; Juniper still requires exact emergency wording before delivery.',forbidden='The deleted music-mix delivery establishes delivery of the dry orientation track.',
 questions=['Describe the current audio deliverable and the practical need behind it.','What was canceled and which timing/content requirements remain?','Has the current spoken version actually been rendered?','Whose wording is still awaited before delivery?','May the rehearsal exception be applied to the garden safety sentence?','Can the removed mix record close the current delivery task?']),
 dict(project='pearl_census',other='elm_workshop',
 goal='Prepare a nesting-box census table for Pearl Marsh.',reason='The field team must compare occupied boxes across two walks.',constraints='Record box tags without observer names. Express dates in day-month-year order.',initial_extra='Color-code occupied rows.',
 revise='Replace row colors with an occupied/unoccupied text column. Keep the two-walk comparison, anonymous observers, and day-month-year dates.',current_goal='A text-status nesting-box census comparison across two walks.',active='A text status column replaces row colors; observer names remain excluded and dates stay day-month-year.',
 old='census alpha: colored rows verified; all walks merged; share link generated.',
 output='census merge-check beta\nwalk one: 38 boxes\nwalk two: missing\nobserver names: absent\ndate format: DD-MM-YYYY\nshare link: not generated',code=5,command='pearl-sim merge-check beta',observed='The merge check had 38 boxes for the first walk but no second walk; no share link was generated.',claim='Both walks are compared and the new text-status table is shared.',
 gate='I still owe you the second walk notebook. Do not fill its cells from the first walk; wait until I upload it before sharing.',open='The user must upload the second walk notebook; the second walk must not be inferred from the first and sharing must wait.',
 othertext='Elm Workshop examples may duplicate the first walk into the second column and be shared as teaching material.',boundary='Elm Workshop may duplicate teaching examples, but Pearl Marsh cannot substitute first-walk values for the missing second notebook.',forbidden='The deleted colored-table record proves both walks are present in the text-status census.',
 questions=['What comparison is the census now meant to provide?','What replaces the colors, and what anonymity/date rules survive?','What does the captured merge check actually confirm about sharing?','What is missing for the next step, and who supplies it?','Can the teaching-material shortcut fill the missing field observations?','Does the erased earlier merge justify saying both walks are included?']),
 dict(project='quartz_roster',other='marigold_demo',
 goal='Make a shift roster for Quartz Observatory guides.',reason='Every public telescope session needs a trained guide at the entrance.',constraints='Schedule only certified guides. Do not expose private phone numbers.',initial_extra='Assign three-hour blocks.',
 revise='Shorten the blocks to two hours; three-hour blocks are no longer wanted. Certified guides only, and no private numbers in the roster.',current_goal='A roster of certified guides in two-hour blocks so each telescope session has entrance coverage.',active='Two-hour blocks replace three-hour blocks; certification and exclusion of private phone numbers remain required.',
 old='roster r1: three-hour slots filled; coverage complete; published.',
 output='roster verify r2\nslot length: 2h\nphone fields: omitted\ncertification feed: unavailable\ncoverage: unverified\npublish: not executed',code=8,command='quartz-sim verify r2',observed='Verification confirmed two-hour slots and omitted phone fields, but certification data was unavailable, coverage unverified, and publication not executed.',claim='The certified two-hour roster is fully covered and published.',
 gate='The trainer has not sent the renewed certification list. I will forward it when it arrives; hold publication until we have checked that list.',open='The trainer must send the renewed certification list through the user, and that list must be checked before publication.',
 othertext='Marigold Demo can use invented guides without certificates and publish its sample rota now.',boundary='Marigold Demo invented-guide permission does not waive certification or publication checks for Quartz Observatory.',forbidden='The deleted r1 publication record proves the two-hour certified roster is published.',
 questions=['What kind of roster is needed now and why?','Which shift-length request changed and which eligibility/privacy limits persist?','Is there retained execution evidence of publication and certified coverage?','What must arrive and be checked before the roster is published?','Do the demo permissions allow uncertified observatory guides?','Can the removed r1 record certify publication of r2?']),
 dict(project='indigo_access',other='spruce_lab',
 goal='Draft an accessible entrance map for Indigo Gallery.',reason='Guests using wheelchairs need a route that avoids the stairs.',constraints='Do not mark an unmeasured slope as accessible. Use high-contrast black and white.',initial_extra='Include the north gate.',
 revise='Remove the north gate from the map and show the east gate instead. Keep black and white, and only call a slope accessible after it has been measured.',current_goal='A black-and-white accessible entrance map through the east gate for guests using wheelchairs.',active='East gate replaces north gate; high contrast remains required and unmeasured slopes cannot be labeled accessible.',
 old='entrance map N: north gate approved; all routes accessible; PDF exported.',
 output='entrance map east validation\ncolors: black/white\neast slope measurement: absent\naccessibility badge: withheld\npdf export: skipped',code=11,command='indigo-sim validate east-map',observed='The east-map validation found no slope measurement, withheld the accessibility badge, and skipped PDF export.',claim='The east route is verified accessible and its PDF is exported.',
 gate='Our facilities lead still needs to measure the east slope. I have no reading yet. Wait for that measurement and my review before export.',open='The facilities lead must measure the east slope and the user must review the measurement before export.',
 othertext='Spruce Lab is a mock map exercise: label guessed slopes accessible and export the teaching PDF.',boundary='Spruce Lab guessed-slope permission is for a mock exercise; Indigo Gallery still needs measured accessibility and review.',forbidden='The deleted north-gate approval verifies the east gate accessibility or PDF export.',
 questions=['Which entrance belongs in the current map, and whom should it help?','What gate instruction was withdrawn and what map rules remain?','Was east-route accessibility established by the retained operation?','What measurements and review are still outstanding?','Can the mock-map guessing rule be transferred to the gallery?','Can an erased north-gate approval establish the east-route result?']),
 dict(project='birch_menu',other='coral_print',
 goal='Lay out a meal card for the Birch camp kitchen.',reason='Servers need to distinguish the two lunch options quickly.',constraints='Keep every allergen line exactly as supplied. Use at least 14-point text.',initial_extra='Place a food photo beside each option.',
 revise='Swap the photos for simple option numbers. The kitchen card still needs the exact allergen lines and text of 14 point or larger.',current_goal='A numbered meal card that helps camp servers distinguish the two lunch options.',active='Simple option numbers replace food photos; exact supplied allergen wording and a minimum 14-point size remain binding.',
 old='meal card photo proof: allergen lines checked; 14pt text; plates sent.',
 output='meal-card numbered proof check\noption numbers: 1,2\nsmallest type: 14pt\nallergen line option 2: not supplied\nplate output: disabled',code=10,command='birch-sim proof-check numbered',observed='The numbered proof had 14-point minimum text but no supplied allergen line for option 2, so plate output was disabled.',claim='The numbered card is complete and its print plates have been sent.',
 gate='The cook has not given me option two’s allergen text. I must get that exact line and approve the proof before plates go out.',open='The cook must provide option-two allergen text and the user must approve the proof before plate output.',
 othertext='Coral Print is a typography sampler. Invent food descriptions and send its plates without a content review.',boundary='Coral Print permits invented sampler descriptions, not invented Birch allergen text or skipped Birch proof approval.',forbidden='The deleted photo proof demonstrates that the numbered card plates were sent.',
 questions=['What is the revised kitchen card for?','Are photos still part of it, and what text rules have not changed?','What does the actual proof check say about plate output?','Which exact content and approval are still missing?','Does the typography sampler exception cover the kitchen card?','Can the erased photographic proof establish completion of this numbered version?']),
 dict(project='silver_pump',other='dahlia_test',
 goal='Prepare a pump-maintenance reminder schedule for Silver allotments.',reason='The volunteer team keeps missing the filter rinse.',constraints='Keep every interval at least seven days. Do not activate live reminders until I approve.',initial_extra='Send reminders by text message.',
 revise='Use email drafts instead of text messages. The seven-day minimum still applies, and my approval is still required before any live reminder is activated.',current_goal='An email-draft pump-maintenance reminder schedule to prevent missed filter rinses.',active='Email drafts replace text messages; intervals remain at least seven days and live activation still requires user approval.',
 old='SMS schedule v0: interval 7 days; activated for test window; delivery OK.',
 output='pump-plan inspect mail-v1\ninterval_days: 7\nemail drafts: 4\nrecipient mapping: incomplete\nactivation call: none',code=2,command='silver-sim inspect mail-v1',observed='Inspection found a seven-day interval and four email drafts, but incomplete recipient mapping and no activation call.',claim='The new email reminders are live and will reach everyone.',
 gate='I have yet to send the role-to-mailbox map. After I send it, show me the final recipients for approval; I have not approved activation.',open='The user must provide the mailbox map and then approve final recipients before live activation.',
 othertext='Dahlia Test may activate hourly reminders to its disposable inbox without asking me.',boundary='Dahlia Test hourly automatic activation does not relax Silver’s seven-day minimum or user approval gate.',forbidden='The deleted SMS test log proves live email reminder activation.',
 questions=['What is the current reminder deliverable meant to prevent?','What channel was replaced and what timing/approval rules survive?','Are email reminders live according to the captured command?','What user input and decision are still pending?','Does the disposable-inbox authorization permit hourly Silver reminders?','Can the deleted SMS log prove the new email schedule is active?']),
 dict(project='ochre_tiles',other='lilac_mock',
 goal='Create a tile-count estimate for the Ochre reading room.',reason='The caretaker needs an order quantity before reserving the installer.',constraints='Do not round area measurements before the final count. Exclude the fireplace footprint.',initial_extra='Include a decorative border.',
 revise='Cancel the decorative border; estimate plain tiles only. Continue using unrounded areas and excluding the fireplace footprint.',current_goal='A plain-tile count estimate for ordering before reserving the reading-room installer.',active='The decorative border is canceled; intermediate areas must remain unrounded and the fireplace footprint excluded.',
 old='tile estimate border-A: 286 tiles; fireplace deducted; supplier quote filed.',
 output='tile-calc plain-B audit\nrounding mode: final count only\nfireplace deduction: enabled\nroom width: missing\nquantity: not calculated\nquote request: not sent',code=13,command='ochre-sim audit plain-B',observed='The audit retained final-only rounding and fireplace exclusion but lacked room width; no quantity was calculated and no quote requested.',claim='The plain-tile quantity is final and I requested the supplier quote.',
 gate='I still need to measure the room width. Wait for my measurement before calculating the count or requesting a quote.',open='The user must measure and supply room width before calculation or a quote request.',
 othertext='Lilac Mock is an arithmetic exercise; assume any width and round each intermediate area.',boundary='Lilac Mock assumptions and intermediate rounding are exercise permissions only; they do not apply to Ochre’s real estimate.',forbidden='The deleted border estimate establishes the plain-tile order quantity or a current quote request.',
 questions=['What estimate is wanted now and what decision depends on it?','Which tile feature was dropped and which calculation rules remain?','Did the captured calculation produce a quantity or send a quote request?','What measurement must the user supply next?','Can the arithmetic exercise assumptions be used for this estimate?','Does the erased bordered estimate settle the plain-tile quantity?']),
 dict(project='teal_badges',other='poppy_sketch',
 goal='Produce reusable role badges for the Teal repair café.',reason='Visitors should be able to find a suitable volunteer without learning private details.',constraints='Use roles, not personal names. Make the lettering readable from two metres.',initial_extra='Use magnetic fasteners.',
 revise='Replace magnets with clip holders. Keep the reusable badges role-only, and preserve the two-metre readability requirement.',current_goal='Reusable role badges with clip holders so repair-café visitors can find volunteers without private details.',active='Clip holders replace magnetic fasteners; badges remain role-only and readable from two metres.',
 old='badge batch magnets: legibility passed; role-only plates; production finished.',
 output='badge clip prototype audit\nname fields: none\nholder: clip\nview distance trial: not run\nproduction queue: empty',code=0,command='teal-sim audit clip-prototype',observed='The audit completed with exit code zero and showed role-only clip prototypes, but no distance trial and an empty production queue.',claim='The clip badges passed the distance test and are in production.',
 gate='I have not arranged the two-metre reading trial. I need to run it with a volunteer and report the result before you release production.',open='The user must arrange and report a volunteer’s two-metre readability trial before production release.',
 othertext='Poppy Sketch can put fictional full names in tiny type; it is just a visual exercise and can go straight to its mock production queue.',boundary='Poppy Sketch fictional-name and tiny-type permissions do not apply to Teal role badges or their readability gate.',forbidden='The deleted magnet-batch test establishes readability or production of the clip version.',
 questions=['What should the revised badges achieve for visitors?','What fastening choice changed and which privacy/legibility requirements remain?','Does a zero exit code mean the distance test passed and production began?','What human check remains before production release?','May the visual-exercise permissions change the repair-café badge rules?','Can the removed magnet-batch result validate the clip badges?']),
 dict(project='russet_locker',other='azure_puzzle',
 goal='Design locker assignment cards for Russet canoe storage.',reason='Members need to identify their allocated storage space without exposing contact details.',constraints='Print locker codes only. Keep the existing locker numbering intact.',initial_extra='Group cards by boat length.',
 revise='Group by rack row instead of boat length. Locker codes only remains mandatory, and existing locker numbers must not be changed.',current_goal='Rack-row grouped locker-code cards for finding allocated canoe storage without exposing contact details.',active='Rack-row grouping replaces boat-length grouping; cards use locker codes only and preserve existing locker numbering.',
 old='length-group cards: numbering preserved; allocation import complete; cards issued.',
 output='locker-card row grouping check\nnumbering changes: 0\ncontact fields: 0\nallocation ledger: unavailable\ncards issued: 0',code=14,command='russet-sim check row-cards',observed='The row-grouping check found no numbering changes or contact fields, but the allocation ledger was unavailable and zero cards were issued.',claim='The row-grouped allocation cards are issued to all members.',
 gate='The storage secretary has not returned the allocation ledger. I will obtain it; until then, do not assign lockers or issue cards.',open='The user must obtain the allocation ledger from the storage secretary before assignments or card issuance.',
 othertext='Azure Puzzle may renumber lockers and assign imaginary people freely for its puzzle layout.',boundary='Azure Puzzle renumbering and imaginary assignments are confined to the puzzle, not Russet storage allocation.',forbidden='The deleted length-group issue receipt proves that row-grouped cards were issued.',
 questions=['What grouping and purpose does the current card set have?','What sorting rule was replaced and which numbering/privacy rules hold?','Were any current cards issued according to the retained check?','What is blocking real assignments and who will obtain it?','Can the puzzle renumbering permission change real locker codes?','Does the deleted old receipt establish issuance of the new cards?']),
 dict(project='ivory_walk',other='fern_class',
 goal='Prepare a self-guided heritage walk sheet for Ivory Quay.',reason='Visitors arrive before the guide desk opens.',constraints='Use public paths only. Do not include private residents’ names.',initial_extra='Include an audio link for every stop.',
 revise='Remove audio links and use short printed descriptions at each stop. Keep all paths public and omit residents’ names.',current_goal='A printed heritage walk with short stop descriptions for visitors arriving before the guide desk opens.',active='Printed descriptions replace audio links; routes remain on public paths and private residents’ names remain excluded.',
 old='audio walk package: six public stops; links tested; bundle published.',
 output='walk-sheet print audit\nstops: 6\nresident names: none\npath at stop 4: status unknown\npublic-path check: incomplete\npublish operation: skipped',code=15,command='ivory-sim audit print-walk',observed='The print audit found six stops and no resident names, but stop 4 path status was unknown and publishing was skipped.',claim='The printed walk is verified entirely public and has been published.',
 gate='I am waiting for the quay office to confirm whether the stop-four path is public. I will send its answer; do not publish until we receive it.',open='The quay office must confirm stop-four path status through the user before publication.',
 othertext='Fern Class can route its fictional geography worksheet across private land and publish it as a classroom example.',boundary='Fern Class fictional routing permission does not allow private paths in Ivory’s actual walk or waive its confirmation gate.',forbidden='The deleted audio-package publication proves the printed route is public and published.',
 questions=['What format is the walk now in and why is it needed?','Which media request was dropped and what route/privacy limits remain?','Does the retained audit establish a published all-public route?','What confirmation is still outstanding before publication?','Can classroom routing permissions be used for this visitor walk?','Can the erased audio bundle verify the revised printed walk?']),
 dict(project='umber_water',other='mint_trial',
 goal='Create a weekly water-meter log for Umber community greenhouse.',reason='The gardeners want to spot leaks without recording individual attendance.',constraints='Keep readings in litres. Do not collect gardener names.',initial_extra='Add daily attendance totals.',
 revise='Drop attendance totals altogether; make this a meter-reading log only. Litres and the ban on gardener names still apply.',current_goal='A weekly meter-reading-only water log for detecting greenhouse leaks without attendance tracking.',active='Attendance totals are canceled; readings remain in litres and gardener names must not be collected.',
 old='water log with attendance: baseline loaded; leak comparison ready; workbook shared.',
 output='water-log baseline check\nunits: litres\npersonal fields: none\nattendance fields: none\nopening meter reading: missing\nleak comparison: not computed\nshare action: none',code=16,command='umber-sim check meter-only',observed='The meter-only check showed litres and no personal or attendance fields, but the opening reading was missing, no leak comparison was computed, and no sharing occurred.',claim='The meter-only workbook is shared and its leak comparison is ready.',
 gate='I have not photographed the opening meter reading yet. Wait for my reading before calculating the comparison or sharing the workbook.',open='The user must supply the opening meter reading before leak comparison or workbook sharing.',
 othertext='Mint Trial is a synthetic dashboard: invent a starting reading and include named attendance records if useful.',boundary='Mint Trial synthetic baseline and named-attendance permission does not apply to Umber’s real meter-only log.',forbidden='The deleted attendance-workbook record proves the meter-only comparison is ready or shared.',
 questions=['What is the revised log intended to detect, and what data does it focus on?','What attendance requirement was canceled and which data rules survive?','What actually happened to comparison and sharing in the current run?','What user-provided evidence is still needed?','May the synthetic dashboard’s invented baseline or names be used here?','Can the deleted attendance workbook close the meter-only task?'])
]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    histories=[]
    gold={}
    checkpoints=[]
    for n,d in enumerate(D,501):
        hid=f'hold_{n}'; s1=f'{hid}_opening'; s2=f'{hid}_elsewhere'; s3=f'{hid}_return'
        def event(suffix,kind,text,command=None,code=None):
            e={'id':f'{hid}_{suffix}','type':kind,'text':text}
            if kind=='tool':
                e['tool_capture']={'tool_name':'fictional_capture','tool_input':{'command':command},'tool_response':{'exit_code':code,'output':text}}
            return e
        initial=event('intent','user',' '.join([d['goal'],d['reason'],d['constraints'],d['initial_extra']]))
        old=event('old_source','tool',d['old'],f"fictional-record inspect {d['project']} prior",0)
        # The recap precedes the checkpoint. It remains an attributed assistant
        # report after its underlying old tool observation is deleted.
        recap=event('old_recap','assistant','My summary of that earlier run: '+d['old'])
        elsewhere=event('permission','user',d['othertext'])
        revision=event('revision','user',d['revise'])
        observed=event('current_capture','tool',d['output'],d['command'],d['code'])
        claim=event('claimed_done','assistant',d['claim'])
        gate=event('open_gate','user',d['gate'])
        deletion={'id':f'{hid}_deletion','type':'delete_observation','observation_event_id':old['id']}
        sessions=[{'id':s1,'project_key':d['project'],'events':[initial,old,recap],'checkpoints':[{'after_event_id':recap['id']}]},
                  {'id':s2,'project_key':d['other'],'events':[elsewhere]},
                  {'id':s3,'project_key':d['project'],'events':[revision,observed,claim,gate,deletion]}]
        qs=[]
        def support(session,index,e,fragment=None):
            return {'session_id':session,'event_index':index,'event_id':e['id'],'fragment':fragment or e['text']}
        def fact(text,*refs):return {'text':text,'support':list(refs)}
        specs=[
            ('current_goal_reason',[fact(d['current_goal'],support(s1,0,initial,d['goal']),support(s1,0,initial,d['reason']),support(s3,0,revision))],
             [{'text':'The original optional feature remains the current deliverable unchanged.','error_type':'false_merge'}],'Proceed with the revised deliverable while preserving its original purpose.'),
            ('active_and_canceled_constraints',[fact(d['active'],support(s1,0,initial,d['constraints']),support(s3,0,revision))],
             [{'text':'Changing the optional feature also cancels the retained binding constraints.','error_type':'false_merge'}],'Follow the replacement and surviving constraints; do not reinstate the canceled requirement.'),
            ('execution_vs_assistant_claim',[fact(d['observed'],support(s3,1,observed)),fact(f"The captured inert command is {d['command']}; its exit code is {d['code']}. The assistant separately claimed: {d['claim']}",support(s3,1,observed),support(s3,2,claim))],
             [{'text':d['claim'],'error_type':'false_closure'}],'Report the captured result and distinguish the assistant completion claim from verified execution.'),
            ('open_obligation_and_trigger',[fact(d['open'],support(s3,3,gate)),fact(d['observed'],support(s3,1,observed))],
             [{'text':'All required human inputs and approvals have arrived and the work is closed.','error_type':'false_closure'}],'Keep the obligation open until the named missing input or decision arrives.'),
            ('cross_project_permission',[fact(d['boundary'],support(s2,0,elsewhere),support(s1,0,initial,d['constraints']),support(s3,3,gate))],
             [{'text':f"Permissions in {d['other']} authorize bypassing the requirements of {d['project']}.",'error_type':'project_mixing'}],'Apply each permission only to its project; retain the primary-project gate.'),
            ('deleted_source_no_resurrection',[fact(d['observed'],support(s3,1,observed)),fact('The current assistant completion statement is a report, not independent proof of completion.',support(s3,2,claim),support(s3,1,observed)),fact(d['open'],support(s3,3,gate))],
             [{'text':d['forbidden'],'error_type':'deleted_resurrection'},{'text':d['claim'],'error_type':'false_closure'}],'Use retained current evidence; do not reconstruct the deleted tool observation from the assistant recap or checkpoint.')]
        for j,(category,facts,forbidden,action) in enumerate(specs,1):
            q={'id':f'q{j}','question':d['questions'][j-1],'stage':'final','project_key':d['project']}
            if j==5:q['project_keys']=[d['project'],d['other']]
            qs.append(q)
            gold[f'{hid}/q{j}']={'required_facts':facts,'forbidden_inferences':forbidden,'current_state_required':True,'category':category,'expected_actionability':action}
        histories.append({'id':hid,'project_keys':[d['project'],d['other']],'sessions':sessions,'questions':qs})
        checkpoints.append({'history_id':hid,'session_id':s1,'after_event_id':recap['id'],'deleted_event_id':old['id'],'deletion_event_id':deletion['id']})
    fixture={'schema_version':1,'split':'holdout','fictional':True,'histories':histories}
    answers={'schema_version':1,'questions':gold}
    dump(ROOT/'holdout.json',fixture);dump(ROOT/'holdout-gold.json',answers)
    # Execute only the supplied public validators, never load the experiment
    # module or any runtime algorithms. Read precisely the permitted lines.
    validator_path=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public/scripts/project_memory_consolidation_experiment.py')
    with validator_path.open() as stream:
        lines=[]
        for line_no,line in enumerate(stream,1):
            if line_no>131:break
            if line_no>=38:lines.append(line)
    source=''.join(lines)
    # The approved range ends at the fixture's final stage if statement;
    # complete only its error body and return value, matching the visible guard.
    source += "                raise ValueError('questions require final stage')\n    return value\n"
    tree=ast.parse(source)
    allowed={'read','identifier','validate_tool_capture','fixture'}
    tree.body=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in allowed]
    env={'json':json,'Path':Path,'Any':object}
    exec(compile(tree,'public-fixture-validators','exec'),env)
    env['fixture'](ROOT/'holdout.json')
    assert len(histories)==16 and len(gold)==96
    all_sessions=set();all_events=set();count=0
    for h in histories:
        assert len(h['sessions'])==3
        index={};deleted=set();cp_positions=[];position=0
        for session in h['sessions']:
            assert session['id'] not in all_sessions;all_sessions.add(session['id'])
            assert session['project_key'] in h['project_keys']
            for i,e in enumerate(session['events']):
                assert e['id'] not in all_events;all_events.add(e['id']);count+=1
                if e['type']=='delete_observation':
                    assert e['observation_event_id'] in index
                    assert index[e['observation_event_id']][2]['type']=='tool'
                    assert cp_positions and cp_positions[-1]<position
                    deleted.add(e['observation_event_id'])
                index[e['id']]=(session['id'],i,e,position)
                for cp in session.get('checkpoints',[]):
                    if cp['after_event_id']==e['id']:cp_positions.append(position)
                if e['type']=='tool':
                    assert type(e['tool_capture']['tool_response']['exit_code']) is int
                    assert e['tool_capture']['tool_response']['output']==e['text']
                position+=1
        assert len(deleted)==1 and len(cp_positions)==1
        for q in h['questions']:
            assert q['project_key']==h['project_keys'][0]
            assert set(q.get('project_keys',[q['project_key']]))<=set(h['project_keys'])
            a=gold[h['id']+'/'+q['id']]
            for f in a['required_facts']:
                assert f['support']
                for r in f['support']:
                    assert r['event_id'] not in deleted
                    sid,i,e,_=index[r['event_id']]
                    assert sid==r['session_id'] and i==r['event_index']
                    assert r['fragment'] and r['fragment'] in e['text']
    assert 128<=count<=160
    assert set(gold)=={h['id']+'/'+q['id'] for h in histories for q in h['questions']}
    pins={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['holdout.json','holdout-gold.json','author.py']}
    dump(ROOT/'sealed-input-pins.json',{'schema_version':1,'files':pins})
    manifest={'schema_version':1,'round':'r8','sealed':True,'fictional':True,
        'counts':{'histories':len(histories),'questions':len(gold),'sessions':len(all_sessions),'events':count,'checkpoints':len(checkpoints),'deletions':len(checkpoints),'tool_captures':32},
        'authorship':'Independently authored by a delegated agent; no blind human validation claimed.',
        'exposure_limitations':['Platform MEMORY_SUMMARY was injected and visible, but not used.','Parent task instructions and schema were supplied.','Only public fixture/tool validator lines 38-131 were inspected.','No prior fixtures, gold, reports, results, runtime algorithms, or memory files were read.','No native, model, or API calls; fictional commands are inert strings and were not executed.'],
        'validation':['Exact 96-question coverage','Public fixture and tool validators passed','Globally unique session/event IDs','Project scope, checkpoint/deletion order checked','Gold support bounds and retained exact fragments checked','Exact capture output/text identity and integer exits checked'],
        'checkpoints':checkpoints,'files':pins,
        'release_policy':'Contents remain sealed pending explicit new R8 freeze and release authorization.'}
    dump(ROOT/'manifest.json',manifest)
    print(json.dumps({'counts':manifest['counts'],'files':pins,'manifest_sha256':hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest(),'pins_sha256':hashlib.sha256((ROOT/'sealed-input-pins.json').read_bytes()).hexdigest()}))

if __name__=='__main__':main()
