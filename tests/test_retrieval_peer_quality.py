"""Query-relevant session peers are selected before their bounded window."""
from pathlib import Path
import tempfile
import unittest

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.store import Store
from tests.test_resume_current_state import Backend


class PeerQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project=str((Path(self.temp.name)/'atlas').resolve())
        self.data=Path(self.temp.name)/'data'
        configure(self.data,semantic_enabled=False,processor_enabled=False,service_enabled=False)
        self.store=Store(self.data)
        self.addCleanup(self.store.close)

    def records(self):
        for index in range(120):
            self.store.remember(self.project,f'Invoice review {index}','Invoice totals were inspected.',
                session_id='task',observation={'type':'discovery','files_read':['tasks/other/discovery.md']})
        target=self.store.remember(self.project,'Feed schedule and geography remain unverified',
            'Feed schedule, geography, SLA and credential functionality have not been independently checked.',
            session_id='task',observation={'type':'discovery','files_read':['tasks/feed/discovery.md']})
        origin=self.store.remember(self.project,'ACME-754 task created','ACME-754 is a Task in Backlog.',
            session_id='task',observation={'type':'decision','files_read':['tasks/feed/discovery.md']})
        return target,origin

    def test_late_pre_id_issue_evidence_recalled_after_irrelevant_session_prefix(self):
        target,origin=self.records()
        result=semantic.search(self.store,self.project,
            'Какие операционные условия ACME-754 ещё не проверены: расписание, география, SLA и выданные доступы?',
            mode='auto',intent='lookup',limit=5,backend=Backend())
        self.assertIn(target['id'],[row['id'] for row in result['results']])
        self.assertEqual(origin['id'],next(row for row in result['results'] if row['id']==target['id'])['issue_association']['origin_id'])

    def test_query_selection_precedes_limit_and_default_stays_chronological(self):
        target,_=self.records()
        self.assertNotIn(target['id'],[r['id'] for r in self.store.retrieval_session_peers(self.project,'task')])
        rows=self.store.retrieval_session_peers(self.project,'task',query='Feed schedule geography SLA',limit=1)
        self.assertEqual([target['id']],[r['id'] for r in rows])

    def test_scope_and_curated_guards(self):
        target,_=self.records()
        excluded=[]
        for project,session,source,kind in [
                (self.project,'other',None,'note'),
                (self.project+'-foreign','task',None,'note'),
                (self.project,'task','hook:Stop','note'),
                (self.project,'task',None,'tool')]:
            row=self.store.remember(project,'Feed schedule geography SLA','Feed schedule geography SLA',
                session_id=session,source=source,kind=kind)
            excluded.append(row['id'])
        rows=self.store.retrieval_session_peers(self.project,'task',query='Feed schedule geography SLA')
        ids=[r['id'] for r in rows]
        self.assertIn(target['id'],ids)
        self.assertFalse(set(excluded)&set(ids))

    def test_missing_issue_does_not_admit_session_neighbors(self):
        self.records()
        result=semantic.search(self.store,self.project,'Какие условия ACME-999999: расписание и SLA?',
            mode='auto',intent='lookup',limit=5,backend=Backend())
        self.assertEqual([],result['results'])

    def test_literal_versions_and_paths_remain_required(self):
        self.records()
        for query in ['Какие условия ACME-754 версии 7.8.9: расписание?',
                      'Какие условия ACME-754 в `src/private/settings.py`: расписание?']:
            self.assertEqual([],self.store.retrieval_session_peers(self.project,'task',query=query))

    def test_query_cap_and_superseded_guard(self):
        old=self.store.remember(self.project,'Feed schedule obsolete','Feed schedule old.',session_id='task')
        self.store.remember(self.project,'Replacement invoice','Invoice replacement.',session_id='task',
                            source_ids=[old['id']])
        for index in range(70):
            self.store.remember(self.project,f'Feed schedule {index}','Feed schedule.',session_id='task')
        rows=self.store.retrieval_session_peers(self.project,'task',query='Feed schedule')
        self.assertEqual(64,len(rows))
        self.assertNotIn(old['id'],[r['id'] for r in rows])

    def test_private_and_history_queries_do_not_expand(self):
        self.records()
        for query in ['<private>Feed schedule SLA</private>',
                      'Как раньше менялись условия ACME-754?']:
            self.assertEqual([],self.store.retrieval_session_peers(self.project,'task',query=query))

    def test_explicit_filters_disable_association(self):
        target,_=self.records()
        result=semantic.search(self.store,self.project,'Какие условия ACME-754: расписание и SLA?',
            mode='auto',intent='lookup',limit=5,backend=Backend(),kinds=['note'])
        self.assertNotIn(target['id'],[r['id'] for r in result['results']])

    def test_bounded_and_invalid_arguments(self):
        self.records()
        for limit in [0,65,True]:
            with self.assertRaises(ValueError):
                self.store.retrieval_session_peers(self.project,'task',query='Feed schedule',limit=limit)
        for query in ['', 'x'*1001]:
            with self.assertRaises(ValueError):
                self.store.retrieval_session_peers(self.project,'task',query=query)
