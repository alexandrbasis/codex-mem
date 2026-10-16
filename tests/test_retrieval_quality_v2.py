"""Fictional regressions for bilingual answer ranking and linked evidence."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.store import Store
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


class QualityV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = str((Path(self.temp.name) / 'atlas').resolve())

    def test_russian_turn_batch_question_recalls_english_evidence(self):
        data = Path(self.temp.name) / 'data'
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            target = store.remember(self.project, 'Event batching preserves turn boundaries',
                'Known turns are separated into jobs. Unknown turns can group. '
                'Processing starts after five seconds; Stop flushes immediately.',
                observation={'type': 'discovery'})
            result = semantic.search(store, self.project,
                'Объединяет ли плагин события разных ходов в одно задание и когда отправляет накопленные события на обработку?',
                mode='auto', intent='resume', limit=1, backend=Backend())
            self.assertEqual([target['id']], [r['id'] for r in result['results']])

    def test_queue_cause_outranks_generic_validation_neighbors(self):
        rows = [summary('old', title='Nebula queue validation', body='Nebula queue tests passed.')]
        rows += [{'id':str(i), 'kind':'note', 'title':'Nebula queue test evidence',
                  'body':'Nebula queue tests passed; more verification remains.',
                  'observation':{'type':'discovery'}} for i in range(5)]
        rows += [{'id':'cause','kind':'note','title':'Nebula queue stall fixed',
                  'body':'Nebula queue stalled because retained tool JSON was double encoded. '
                         'Hydration now decodes the payload and keeps original captures.',
                  'observation':{'type':'bugfix'}}]
        result = semantic.search(CandidateStore(rows, {r['id']:event('2026-10-10T10:00:00Z') for r in rows}),
            self.project, 'Почему зависала очередь Nebula и каким изменением это исправили?',
            mode='auto', intent='resume',limit=2,backend=Backend())
        self.assertIn('cause', [r['id'] for r in result['results']])

    def test_rewards_question_recalls_early_credit_gate(self):
        data = Path(self.temp.name) / 'data'
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            target = store.remember(self.project,'Reward funding and early credit gate',
                'Reward economics remain unproven. Early credit requires funding; '
                'the pilot does not establish unconditional instant payout.',
                observation={'type':'discovery'})
            result = semantic.search(store,self.project,
                'Какие предположения об экономике вознаграждений SDK ещё нужно проверить и можно ли обещать мгновенное начисление?',
                mode='auto',intent='resume',limit=1,backend=Backend())
            self.assertEqual([target['id']],[r['id'] for r in result['results']])

    def test_current_installation_with_configuration_outcomes_precedes_old_summary(self):
        rows=[summary('old','old',title='Atlas release verified with Nebula',
                      body='Atlas version 1.0.0 installed. Nebula enabled.'),
              {'id':'new','kind':'note','title':'Atlas installation verified',
               'body':'Atlas version 4.2.1 installed. Nebula filter, quality and retrieval disabled.',
               'observation':{'type':'discovery'}}]
        store=CandidateStore(rows,{'old':event('2026-09-01T10:00:00Z'),'new':event('2026-10-10T10:00:00Z')})
        result=semantic.search(store,self.project,
            'Какая версия Atlas сейчас установлена и включены ли фильтрация, проверка качества и поиск через Nebula?',
            mode='auto',intent='resume',limit=1,backend=Backend())
        self.assertEqual(['new'],[r['id'] for r in result['results']])

    def test_topical_later_handoff_supplies_completed_answer(self):
        old=summary('old','same',project=self.project,title='Nebula notification decision log initialized',
                    body='Nebula notification decision log has no decisions yet.')
        new=summary('new','same',project=self.project,title='Nebula notifications agreed',
                    body='Nebula registration sends an immediate alert; a daily overdue digest follows.',
                    source='processor:test')
        class LinkedStore(CandidateStore):
            def get(self,project,ids):return [new]
        store=LinkedStore([old],{'old':event('2026-10-10T09:00:00Z',later_summary_id='new'),
                                 'new':event('2026-10-10T10:00:00Z')})
        result=semantic.search(store,self.project,
            'Какие уведомления Nebula решили делать при регистрации и долгом ожидании проверки?',
            mode='auto',intent='resume',limit=1,backend=Backend())
        self.assertEqual(['new'],[r['id'] for r in result['results']])

    def test_exact_issue_query_recovers_pre_id_discovery_from_shared_task_file(self):
        data=Path(self.temp.name)/'data'
        configure(data,semantic_enabled=False,processor_enabled=False,service_enabled=False)
        with Store(data) as store:
            target=store.remember(self.project,'Feed operational checks remain open',
                'Schedule, geography, SLA and credential functionality have not been independently checked.',
                session_id='task-session',observation={'type':'discovery',
                    'files_read':['tasks/feed-import/discovery.md']})
            store.remember(self.project,'ACME-754 task created',
                'ACME-754 is a Task in Backlog; implementation remains unverified.',
                session_id='task-session',observation={'type':'decision',
                    'files_read':['tasks/feed-import/discovery.md']})
            result=semantic.search(store,self.project,
                'Какие операционные условия ACME-754 ещё не проверены: расписание, география, SLA и выданные доступы?',
                mode='auto',intent='lookup',limit=5,backend=Backend())
            self.assertIn(target['id'],[r['id'] for r in result['results']])

    def test_pre_id_association_requires_same_session_shared_file_and_exact_origin(self):
        from codex_mem.retrieval import issue_discovery_association
        origin={'id':'issue','project':self.project,'session_id':'one',
                'title':'ACME-754 task created','observation':{'files_read':['tasks/feed/discovery.md']}}
        candidate={'id':'before','project':self.project,'session_id':'one',
                   'title':'Feed operational checks','body':'SLA remains unverified.',
                   'observation':{'files_read':['tasks/feed/discovery.md']}}
        query='Какие условия ACME-754 ещё не проверены?'
        self.assertTrue(issue_discovery_association(origin,candidate,query))
        self.assertTrue(issue_discovery_association(origin,candidate,query+' Какой SLA?'))
        for changed in (dict(candidate,session_id='two'),dict(candidate,project='/other'),
                        dict(candidate,title='ACME-755 unrelated task'),
                        dict(candidate,observation={'files_read':['tasks/other/discovery.md']})):
            self.assertFalse(issue_discovery_association(origin,changed,query))
        self.assertFalse(issue_discovery_association(origin,candidate,'Какие условия ACME-999999 ещё не проверены?'))
        self.assertFalse(issue_discovery_association(origin,candidate,query+' Раньше?'))

    def test_installation_ordering_hint_does_not_relax_history_or_exact_versions(self):
        from codex_mem.retrieval import current_installation_question
        self.assertFalse(current_installation_question('Какая версия Atlas раньше была установлена?',self.project))
        self.assertFalse(current_installation_question('Какая версия Atlas 1.0.0 установлена?',self.project))

    def test_topical_handoff_requires_explicit_link_and_original_subject(self):
        from codex_mem.retrieval import linked_handoff_matches
        origin={'id':'old','project':self.project,'session_id':'same','title':'Nebula notifications',
                'later_summary_id':'new'}
        later={'id':'new','project':self.project,'session_id':'same','title':'Nebula notifications agreed'}
        query='Какие уведомления Nebula решили делать?'
        self.assertTrue(linked_handoff_matches(origin,later,query,self.project))
        self.assertFalse(linked_handoff_matches(dict(origin,later_summary_id='other'),later,query,self.project))
        self.assertFalse(linked_handoff_matches(origin,dict(later,title='Mercury notifications'),query,self.project))
        self.assertFalse(linked_handoff_matches(origin,later,query+' Раньше?',self.project))

    def test_bilingual_candidate_survives_full_semantic_window(self):
        neighbors=[{'id':str(i),'kind':'note','title':'Generic repository activity',
                    'body':'Documentation reviewed.','observation':{'type':'discovery'}} for i in range(100)]
        target={'id':'answer','kind':'note','title':'Event turn batching policy',
                'body':'Known turns form separate event jobs. Unknown turns can group; '
                       'processing starts after five seconds.','observation':{'type':'discovery'}}
        class HintStore(CandidateStore):
            def search(self,project,query,**kwargs):
                return [target] if query=='event turn' else []
        store=HintStore(neighbors,{r['id']:event('2026-10-10T10:00:00Z') for r in neighbors+[target]})
        result=semantic.search(store,self.project,
            'Объединяет ли плагин события разных ходов в одно задание и когда отправляет накопленные события на обработку?',
            mode='auto',intent='resume',limit=1,backend=Backend())
        self.assertEqual(['answer'],[r['id'] for r in result['results']])

    def test_issue_operational_evidence_outranks_five_issue_metadata_neighbors(self):
        rows=[{'id':str(i),'project':self.project,'session_id':'task','kind':'note',
               'title':'ACME-754 issue verification','body':'ACME-754 Task in Backlog, reviewed and approved.',
               'observation':{'type':'discovery','files_read':['tasks/feed/discovery.md']}} for i in range(5)]
        target={'id':'operational','project':self.project,'session_id':'task','kind':'note',
                'title':'Feed operational checks','body':'Schedule, geography, SLA and credential functionality remain unverified.',
                'observation':{'type':'discovery','files_read':['tasks/feed/discovery.md']}}
        class PeersStore(CandidateStore):
            def retrieval_session_peers(self,project,session_id,**kwargs):return [target]
        store=PeersStore(rows,{r['id']:event('2026-10-10T10:00:00Z') for r in rows+[target]})
        result=semantic.search(store,self.project,
            'Какие операционные условия ACME-754 ещё не проверены: расписание, география, SLA и выданные доступы?',
            mode='auto',intent='lookup',limit=5,backend=Backend())
        self.assertIn('operational',[r['id'] for r in result['results']])

    def test_pre_id_association_rejects_shared_code_and_task_templates(self):
        from codex_mem.retrieval import issue_discovery_association
        for path in ('src/shared/helpers.py','docs/shared/README.md',
                     'tasks/templates/feed/discovery.md','tasks/shared/discovery.md',
                     'tasks/common/requirements.md','tasks/feed/helpers.py'):
            with self.subTest(path=path):
                metadata={'files_read':[path]}
                origin={'id':'issue','project':self.project,'session_id':'one',
                        'title':'ACME-123 task created','observation':metadata}
                candidate={'id':'other','project':self.project,'session_id':'one',
                           'title':'Unrelated invoice discovery','observation':metadata}
                self.assertFalse(issue_discovery_association(origin,candidate,'Какие условия ACME-123 остались открыты?'))

    def test_foreign_package_version_question_keeps_dependency_evidence(self):
        from codex_mem.retrieval import current_installation_question
        query='Какая версия Redis установлена?'
        self.assertFalse(current_installation_question(query,self.project))
        rows=[{'id':'redis','kind':'note','title':'Redis version 7.4.0 installed',
               'body':'Redis is installed as a dependency.','observation':{'type':'discovery'}},
              {'id':'atlas','kind':'note','title':'Atlas version 4.2.1 installed',
               'body':'Atlas runtime verified.','observation':{'type':'discovery'}}]
        store=CandidateStore(rows,{'redis':event('2026-10-10T10:00:00Z'),'atlas':event('2026-10-11T10:00:00Z')})
        result=semantic.search(store,self.project,query,mode='auto',intent='resume',limit=1,backend=Backend())
        self.assertEqual(['redis'],[r['id'] for r in result['results']])

    def test_owner_scope_qualifier_does_not_replace_version_subject(self):
        from codex_mem.retrieval import current_installation_question
        rows=[{'id':'redis','kind':'note','title':'Redis version 7.4.0 installed in Atlas',
               'body':'Redis is installed as an Atlas dependency.','observation':{'type':'discovery'}},
              {'id':'atlas','kind':'note','title':'Atlas version 4.2.1 installed',
               'body':'Atlas runtime verified.','observation':{'type':'discovery'}}]
        for query in ('Какая версия Redis установлена в Atlas?',
                      'Какая версия Redis установлена в проекте Atlas?',
                      'What version of Redis is installed in Atlas?',
                      'Atlas: какая версия Redis установлена?'):
            with self.subTest(query=query):
                self.assertFalse(current_installation_question(query,self.project))
                store=CandidateStore(rows,{'redis':event('2026-10-10T10:00:00Z'),
                                           'atlas':event('2026-10-11T10:00:00Z')})
                result=semantic.search(store,self.project,query,mode='auto',intent='resume',limit=1,backend=Backend())
                self.assertEqual(['redis'],[r['id'] for r in result['results']])

    def test_natural_acronym_hyphens_recall_separate_english_terms(self):
        data=Path(self.temp.name)/'data'
        configure(data,semantic_enabled=False,processor_enabled=False,service_enabled=False)
        with Store(data) as store:
            target=store.remember(self.project,'E2E HTTP server collision fixed',
                'E2E tests reached the wrong HTTP server because IPv6 and IPv4 listeners collided. '
                'The test server now binds 127.0.0.1 explicitly.',observation={'type':'bugfix'})
            store.remember(self.project,'Unit test HTTP server review',
                'Unit tests verified an HTTP server.',observation={'type':'discovery'})
            result=semantic.search(store,self.project,
                'Почему E2E-тесты могли попасть не в свой HTTP-сервер и как это исправили?',
                mode='auto',intent='resume',limit=5,backend=Backend())
            self.assertEqual([target['id']],[r['id'] for r in result['results']])

    def test_quoted_compounds_paths_issues_and_code_remain_literal(self):
        from codex_mem.jev_retrieval import exact_constraints
        for token in ('`E2E-тесты`','"E2E-тесты"','`HTTP-сервер`','PRM-951','src/HTTP-сервер.py','worker_name'):
            with self.subTest(token=token):
                self.assertIn(token.strip('`"').casefold(),exact_constraints('Почему '+token+' не работает?')['identifiers'])
        self.assertIn('e2e-тесты',exact_constraints('E2E-тесты')['identifiers'])

    def test_network_account_decisions_outrank_product_overviews(self):
        rows=[summary('overview','overview',title='PhotonLink product overview',
                      body='PhotonLink roadmap and product direction were reviewed.')]
        rows += [{'id':str(i),'kind':'note','title':'PhotonLink overview',
                  'body':'PhotonLink product documentation was reviewed.',
                  'observation':{'type':'discovery'}} for i in range(5)]
        target={'id':'decisions','kind':'note','title':'PhotonLink network account decisions',
                'body':'PhotonLink allows accounts only from already integrated networks. '
                       'The platform receives the first click for routing. '
                       'Implementation location and ownership remain unresolved.',
                'observation':{'type':'decision'}}
        rows.append(target)
        metadata={row['id']:event('2026-10-10T10:00:00Z') for row in rows}
        query='Что согласовали по PhotonLink, какие аккаунты сетей можно подключать и что ещё не решили по реализации?'
        result=semantic.search(CandidateStore(rows,metadata),self.project,query,
                               mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertIn('decisions',[row['id'] for row in result['results']])
        history=semantic.search(CandidateStore(rows,metadata),self.project,query+' Покажи историю.',
                                mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertNotIn('decisions',[row['id'] for row in history['results']])

    def test_requested_batch_timing_and_boundary_caveat_beat_stop_only_neighbors(self):
        rows=[{'id':str(i),'kind':'note','title':'Event processing job batch Stop flush',
               'body':'Event processing creates jobs for accumulated events. The first Stop flushes immediately.',
               'observation':{'type':'discovery'}} for i in range(5)]
        rows += [{'id':'timing','kind':'note','title':'Reported batch boundary and deadline repair',
                  'body':'Fresh batches stop before a different known turn ID. Tool captures coalesce for '
                         'up to five seconds; Stop and PreCompact flush immediately.',
                  'observation':{'type':'discovery'}},
                 {'id':'legacy','kind':'note','title':'Reported batch boundary limits',
                  'body':'Fresh batches stop before a different known turn ID. Unknown-turn grouping '
                         'and legacy behavior remain supported.',
                  'observation':{'type':'discovery'}}]
        metadata={row['id']:event('2026-10-10T10:00:00Z') for row in rows}
        query='Объединяет ли плагин события разных ходов в одно задание и когда отправляет накопленные события на обработку?'
        for intent in ('lookup','resume'):
            with self.subTest(intent=intent):
                result=semantic.search(CandidateStore(rows,metadata),self.project,query,
                    mode='auto',intent=intent,limit=5,backend=Backend())
                ids=[row['id'] for row in result['results']]
                self.assertIn('timing',ids)
                self.assertIn('legacy',ids)
        history=semantic.search(CandidateStore(rows,metadata),self.project,query+' Покажи историю.',
            mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertEqual([str(i) for i in range(5)],[row['id'] for row in history['results']])

    def test_starting_stage_keeps_research_and_backend_sequence_above_model_overviews(self):
        rows=[summary('overview','overview',title='Photon Network model overview',
                      body='Photon Network is the first commercial offer; SDK is a later track.')]
        rows += [{'id':str(i),'kind':'note','title':'Photon Network and SDK models',
                  'body':'Photon Network and SDK models have different audiences. Network comes first commercially.',
                  'observation':{'type':'discovery'}} for i in range(5)]
        target={'id':'research','kind':'note','title':'SDK browser research before implementation',
                'body':'Photon Network remains the first commercial offer. SDK browser research is the near-term '
                       'technical priority and can begin before shared-backend dependencies are resolved. '
                       'Research does not prove implementation readiness.',
                'observation':{'type':'discovery'}}
        rows.append(target)
        metadata={row['id']:event('2026-10-10T10:00:00Z') for row in rows}
        query='Что сейчас приоритетнее для Photon: Network или SDK, и с какой стадии SDK начинаем?'
        result=semantic.search(CandidateStore(rows,metadata),self.project,query,
                               mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertIn('research',[row['id'] for row in result['results']])
        history=semantic.search(CandidateStore(rows,metadata),self.project,query+' Покажи историю.',
                                mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertNotIn('research',[row['id'] for row in history['results']])

    def test_deadline_and_missing_turn_qualifications_outweigh_acceptance_vocabulary(self):
        rows=[{'id':str(i),'kind':'note','title':'Acceptance script defines event processing invariants',
               'body':'Acceptance criteria cover processing events, batch jobs, distinct known turn boundaries '
                      'and accumulated event grouping. No execution was captured.',
               'observation':{'type':'discovery'}} for i in range(5)]
        rows += [{'id':'deadline','kind':'note','title':'Automatic captures coalesce before processing',
                  'body':'Automatic captures queue with a five second coalescing delay. The service keeps the '
                         'first deadline so a continuous stream cannot postpone processing indefinitely.',
                  'observation':{'type':'discovery'}},
                 {'id':'legacy','kind':'note','title':'Batching stops at distinct known turns',
                  'body':'Selection stops at a different known turn ID. Legacy events without turn IDs can still be grouped.',
                  'observation':{'type':'discovery'}}]
        metadata={row['id']:event('2026-10-10T10:00:00Z') for row in rows}
        query='Объединяет ли плагин события разных ходов в одно задание и когда отправляет накопленные события на обработку?'
        result=semantic.search(CandidateStore(rows,metadata),self.project,query,
                               mode='auto',intent='resume',limit=5,backend=Backend())
        self.assertIn('deadline',[row['id'] for row in result['results']])
        self.assertIn('legacy',[row['id'] for row in result['results']])

    def test_variant_probes_recover_batch_records_outside_initial_window(self):
        data=Path(self.temp.name)/'data'
        configure(data,semantic_enabled=False,processor_enabled=False,service_enabled=False)
        with Store(data) as store:
            neighbors=[]
            for i in range(100):
                neighbors.append(store.remember(self.project,f'Generic processing processing overview {i}',
                    'Processing events batch jobs and accumulated event grouping.',observation={'type':'discovery'}))
            boundary=store.remember(self.project,'Observation batching stops at distinct known turns',
                'Selection stops at a different known turn ID. Legacy events without turn IDs can still be grouped.',
                observation={'type':'discovery'})
            deadline=store.remember(self.project,'Automatic captures changed to coalesce',
                'Automatic captures queue with a five second coalescing delay. Processing keeps the first deadline '
                'so a continuous stream cannot postpone processing indefinitely.',observation={'type':'discovery'})
            with patch.object(semantic,'_semantic_results',return_value=neighbors):
                result=semantic.search(store,self.project,
                    'Объединяет ли плагин события разных ходов в одно задание и когда отправляет накопленные события на обработку?',
                    mode='auto',intent='resume',limit=5,backend=Backend())
            ids=[row['id'] for row in result['results']]
            self.assertIn(boundary['id'],ids)
            self.assertIn(deadline['id'],ids)

    def test_check_outcomes_and_provider_limits_outrank_pending_verification(self):
        rows=[{'id':str(i),'kind':'note','title':'PR 123 verification remains pending',
               'body':'PR 123 was opened. Current-head CI and runtime verification remain pending.',
               'observation':{'type':'discovery'}} for i in range(5)]
        rows += [{'id':'gates','kind':'note','title':'PR 123 local CI gates passed',
                  'body':'Local result files for PR 123 reported that all CI gates passed. '
                         'Real-provider tests were disabled; deployed behavior was not verified.',
                  'observation':{'type':'discovery'}},
                 {'id':'counts','kind':'note','title':'PR 123 records local CI results and limits',
                  'body':'PR 123 description reports passing mandatory local CI: 480 integration tests '
                         'and 150 E2E tests with 2 pending cases. Real external-provider suites were disabled. '
                         'These are description claims, not independently captured test executions.',
                  'observation':{'type':'discovery'}}]
        metadata={row['id']:event('2026-10-10T10:00:00Z') for row in rows}
        query='Какие локальные проверки прошёл PR 123 и запускались ли реальные провайдеры?'
        result=semantic.search(CandidateStore(rows,metadata),self.project,query,
                               mode='auto',intent='lookup',limit=5,backend=Backend())
        ids=[row['id'] for row in result['results']]
        self.assertIn('gates',ids)
        self.assertIn('counts',ids)
