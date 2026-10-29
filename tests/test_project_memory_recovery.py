"""Authored exact-recovery invariants; no native or semantic model approvals."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.project_memory import ProjectMemory, capture_event
from codex_mem.store import Store


class ExactRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = str(self.root / 'project')
        self.store = Store(self.root / 'db')
        self.addCleanup(self.store.close)
        self.memory = ProjectMemory(self.store)
        self.parent = self.quarantine('one', 'Keep the import local.')
        self.sibling = self.quarantine('two', 'Keep a separate cache.')
        self.expected = self.selector(self.parent)

    def quarantine(self, session, text):
        entry = self.store.remember(self.project, 'Authored event', text,
                                    session_id=session, source='hook:UserPromptSubmit')
        self.store._write(lambda: capture_event(self.store._connection, entry['id']))
        snapshot = self.memory.claim(self.project)
        attempt = self.memory.begin_attempt(snapshot, model='authored-model', reasoning_effort='medium')
        self.memory.finalize_attempt(self.project, attempt['attempt_id'], 'failed', receipt={'code': 'invalid_response'})
        self.memory.fail(snapshot, 'invalid_response', receipt={'output_schema_version': 'authored-fixture'})
        return snapshot['job_id']

    def row(self, job):
        return dict(self.store._connection.execute('SELECT * FROM pm_jobs WHERE id=?', (job,)).fetchone())

    def selector(self, job):
        row = self.row(job)
        state = self.store._connection.execute('SELECT * FROM pm_projects WHERE project=?', (self.project,)).fetchone()
        events = self.store._connection.execute('SELECT v.*,e.body,e.superseded_by,e.source,e.session_id AS entry_session_id,e.turn_id,e.title,e.created_at FROM pm_events v JOIN entries e ON e.id=v.source_id WHERE blocked_job_id=? ORDER BY seq', (job,)).fetchall()
        return dict(job_id=job, status=row['status'], error=row['error'], attempts=row['attempts'],
                    fingerprint=row['fingerprint'], revision=row['revision'], generation=row['generation'],
                    current_revision=state['revision'], current_deletion_generation=state['deletion_generation'],
                    events=[dict(seq=e['seq'], source_id=e['source_id'], offset=e['offset'],
                                 source_sha256=hashlib.sha256(e['body'].encode()).hexdigest(), superseded_by=e['superseded_by'],
                                 provenance={k:e['entry_session_id' if k=='session_id' else k] for k in ('source','session_id','turn_id','title','created_at')}) for e in events])

    def test_exact_successor_preserves_parent_and_sibling_and_commits_only_selected_events(self):
        before = self.row(self.parent), self.row(self.sibling)
        before_usage = self.memory.attempts(self.project)
        child = self.memory.claim_recovery(self.project, expected=self.expected)
        self.assertNotIn(child['job_id'], (self.parent, self.sibling))
        self.assertEqual(before, (self.row(self.parent), self.row(self.sibling)))
        self.assertIsNone(self.memory.claim(self.project))
        result = self.memory.commit(child, dict(episodes=[], claims=[], relations=[]))
        self.assertEqual('committed', result['status'])
        events = self.store._connection.execute('SELECT source_id,done,blocked_job_id FROM pm_events ORDER BY seq').fetchall()
        self.assertEqual([1, 0], [e['done'] for e in events])
        self.assertEqual(self.sibling, events[1]['blocked_job_id'])
        self.assertEqual(before, (self.row(self.parent), self.row(self.sibling)))
        self.assertEqual(before_usage, self.memory.attempts(self.project))
        link = self.store._connection.execute('SELECT * FROM pm_job_recoveries').fetchone()
        self.assertEqual((self.parent, child['job_id']), (link['parent_job_id'], link['successor_job_id']))
        with self.assertRaises(ValueError): self.memory.claim_recovery(self.project, expected=self.expected)

    def test_mismatched_selector_never_releases_or_claims(self):
        for key, value in (('status', 'failed'), ('error', 'timeout'), ('attempts', 2),
                           ('fingerprint', 'wrong'), ('revision', 900), ('generation', 900),
                           ('current_revision', 900), ('current_deletion_generation', 900), ('events', [])):
            with self.subTest(key=key):
                selector = copy.deepcopy(self.expected); selector[key] = value
                with self.assertRaises(ValueError): self.memory.claim_recovery(self.project, expected=selector)
                self.assertEqual('quarantined', self.row(self.parent)['status'])
        with self.assertRaises(ValueError): self.memory.claim_recovery(str(self.root / 'other'), expected=self.expected)

    def test_changed_missing_superseded_or_processed_source_rejects_claim(self):
        sid = self.expected['events'][0]['source_id']
        connection = self.store._connection
        mutations = [lambda: connection.execute('UPDATE entries SET body=? WHERE id=?', ('Changed.', sid)),
                     lambda: connection.execute('UPDATE entries SET superseded_by=? WHERE id=?', (self.selector(self.sibling)['events'][0]['source_id'], sid)),
                     lambda: connection.execute('UPDATE pm_events SET done=1 WHERE source_id=?', (sid,)),
                     lambda: connection.execute('DELETE FROM pm_events WHERE source_id=?', (sid,))]
        original = dict(connection.execute('SELECT * FROM pm_events WHERE source_id=?', (sid,)).fetchone())
        for mutation in mutations:
            self.store._write(mutation)
            with self.assertRaises(ValueError): self.memory.claim_recovery(self.project, expected=self.expected)
            def restore():
                connection.execute('UPDATE entries SET body=?,superseded_by=NULL WHERE id=?', ('Keep the import local.', sid))
                connection.execute('INSERT OR REPLACE INTO pm_events (' + ','.join(original) + ') VALUES (' + ','.join('?' for _ in original) + ')', tuple(original.values()))
            self.store._write(restore)

    def test_postclaim_source_change_revokes_commit_and_failure_stays_quarantined(self):
        child = self.memory.claim_recovery(self.project, expected=self.expected)
        self.memory.fail(child, 'timeout')
        self.assertEqual('quarantined', self.row(child['job_id'])['status'])
        self.assertIsNone(self.memory.claim(self.project))
        self.assertEqual('quarantined', self.row(self.parent)['status'])
        self.assertEqual(0, self.memory.retry_failed(self.project))

    def test_postclaim_supersession_cannot_commit(self):
        child = self.memory.claim_recovery(self.project, expected=self.expected)
        sid = self.expected['events'][0]['source_id']
        self.store._write(lambda: self.store._connection.execute('UPDATE entries SET superseded_by=? WHERE id=?', (self.selector(self.sibling)['events'][0]['source_id'], sid)))
        self.assertFalse(self.memory.snapshot_current(child))
        self.assertEqual('stale', self.memory.commit(child, dict(episodes=[], claims=[], relations=[]))['status'])
        self.assertEqual(0, self.store._connection.execute('SELECT done FROM pm_events WHERE source_id=?',(sid,)).fetchone()[0])
        self.assertEqual('quarantined', self.row(child['job_id'])['status'])
        self.assertEqual('source_changed', self.row(child['job_id'])['error'])

    def test_exact_pipeline_dispatch_preserves_profile_usage_and_sibling(self):
        from codex_mem import episode_processor as ep
        from tests.test_episode_processor import receipt
        settings = dict(project_memory_mode='pilot', project_memory_projects=[self.project],
                        capture_enabled=True, capture_scope='all', excluded_projects=[], jev_quality_enabled=False)
        requests = []
        def runner(request):
            requests.append(request)
            return receipt()
        before = self.row(self.parent), self.row(self.sibling)
        with mock.patch.object(ep, 'load_config', return_value=settings), \
                mock.patch.object(ProjectMemory, 'claim', side_effect=AssertionError('broad claim forbidden')):
            result = ep.process_project_memory(self.project, self.root / 'db', runner=runner,
                                               recovery=self.expected)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, len(requests))
        self.assertEqual(dict(runtime='codex', provider='openai', model=ep.MODEL,
                              reasoning_effort=ep.REASONING_EFFORT, timeout=240.0), requests[0]['execution_profile'])
        self.assertEqual(before, (self.row(self.parent), self.row(self.sibling)))
        attempt = next(a for a in self.memory.attempts(self.project) if a['job_id'] == result['job_id'])
        self.assertEqual(result['job_id'], attempt['job_id'])
        self.assertEqual('processed', attempt['outcome'])
        self.assertEqual(ep.MODEL, attempt['actual_model'])

    def test_pipeline_mismatch_never_dispatches(self):
        from codex_mem import episode_processor as ep
        settings = dict(project_memory_mode='pilot', project_memory_projects=[self.project],
                        capture_enabled=True, capture_scope='all', excluded_projects=[], jev_quality_enabled=False)
        invalid = dict(self.expected, current_revision=999)
        runner = mock.Mock(side_effect=AssertionError('dispatch forbidden'))
        with mock.patch.object(ep, 'load_config', return_value=settings):
            result = ep.process_project_memory(self.project, self.root / 'db', runner=runner, recovery=invalid)
        self.assertEqual('blocked', result['status'])
        self.assertEqual('recovery_unavailable', result['code'])
        runner.assert_not_called()

    def test_recovery_failure_does_not_retry_and_retains_usage(self):
        from codex_mem import episode_processor as ep
        from codex_mem.processor import ProcessorFailure
        settings = dict(project_memory_mode='pilot', project_memory_projects=[self.project],
                        capture_enabled=True, capture_scope='all', excluded_projects=[], jev_quality_enabled=False)
        runner = mock.Mock(side_effect=ProcessorFailure('timeout'))
        with mock.patch.object(ep, 'load_config', return_value=settings):
            result = ep.process_project_memory(self.project, self.root / 'db', runner=runner, recovery=self.expected)
        self.assertEqual('failed', result['status'])
        self.assertEqual(1, runner.call_count)
        self.assertEqual('quarantined', self.row(result['job_id'])['status'])
        self.assertEqual('quarantined', self.row(self.parent)['status'])
        self.assertEqual('failed', next(a for a in self.memory.attempts(self.project) if a['job_id'] == result['job_id'])['outcome'])
        self.assertIsNone(self.memory.claim(self.project))

    def test_bounds_and_exact_offsets_are_required(self):
        for change in (lambda x:x['events'][0].update(offset=1),
                       lambda x:x['events'].append(copy.deepcopy(x['events'][0])),
                       lambda x:x.update(attempts=True)):
            selector = copy.deepcopy(self.expected);change(selector)
            with self.assertRaises(ValueError):self.memory.claim_recovery(self.project, expected=selector)
        with self.assertRaises(ValueError):self.memory.claim_recovery(self.project, expected=self.expected, max_events=0)
        with self.assertRaises(ValueError):self.memory.claim_recovery(self.project, expected=self.expected, max_chars=256, lease_seconds=0)

    def test_concurrent_authorization_is_consumed_once(self):
        from concurrent.futures import ThreadPoolExecutor
        second = Store(self.root / 'db')
        self.addCleanup(second.close)
        memories = [self.memory, ProjectMemory(second)]
        def claim(memory):
            try:return memory.claim_recovery(self.project, expected=self.expected)['job_id']
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(claim, memories))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertEqual(1, self.store._connection.execute('SELECT count(*) FROM pm_job_recoveries').fetchone()[0])

    def test_new_deletion_generation_cannot_reuse_old_parent(self):
        self.store._write(lambda: self.store._connection.execute('UPDATE pm_projects SET deletion_generation=1 WHERE project=?',(self.project,)))
        selector = dict(self.expected, current_deletion_generation=1)
        with self.assertRaises(ValueError):self.memory.claim_recovery(self.project, expected=selector)

    def test_failed_without_blocked_membership_is_not_reinterpreted(self):
        selector = dict(self.expected, status='failed')
        self.store._write(lambda: self.store._connection.execute("UPDATE pm_jobs SET status='failed' WHERE id=?",(self.parent,)))
        with self.assertRaises(ValueError):self.memory.claim_recovery(self.project, expected=selector)

    def test_retained_original_with_pinned_supersession_remains_pm_evidence(self):
        sid = self.expected['events'][0]['source_id']
        successor = self.selector(self.sibling)['events'][0]['source_id']
        self.store._write(lambda: self.store._connection.execute('UPDATE entries SET superseded_by=? WHERE id=?', (successor, sid)))
        selector = self.selector(self.parent)
        child = self.memory.claim_recovery(self.project, expected=selector)
        self.assertTrue(self.memory.snapshot_current(child))
        self.assertEqual(sid, child['sources'][0]['id'])
        self.assertEqual('Keep the import local.', child['sources'][0]['body'])
        self.assertEqual('committed', self.memory.commit(child, dict(episodes=[], claims=[], relations=[]))['status'])
        self.assertEqual(successor, self.store._connection.execute('SELECT superseded_by FROM entries WHERE id=?', (sid,)).fetchone()[0])

    def large_quarantine(self):
        for text in ('A' * 13000, 'B' * 13000):
            entry = self.store.remember(self.project, 'Large authored event', text,
                                        session_id='large', source='hook:UserPromptSubmit')
            self.store._write(lambda: capture_event(self.store._connection, entry['id']))
        parent = self.memory.claim(self.project)
        self.memory.fail(parent, 'invalid_response')
        return parent['job_id']

    def test_large_exact_authorization_commits_bounded_prefix_and_releases_only_remainder(self):
        parent = self.large_quarantine()
        before = self.row(parent), self.row(self.parent), self.row(self.sibling)
        child = self.memory.claim_recovery(self.project, expected=self.selector(parent))
        self.assertEqual(24000, sum(len(s['body']) for s in child['sources']))
        self.assertTrue(child['sources'][-1]['truncated'])
        self.assertEqual('committed', self.memory.commit(child, dict(episodes=[], claims=[], relations=[]))['status'])
        selected = self.store._connection.execute('SELECT * FROM pm_events WHERE session_id=? ORDER BY seq', ('large',)).fetchall()
        self.assertEqual([1, 0], [e['done'] for e in selected])
        self.assertEqual([13000, 11000], [e['offset'] for e in selected])
        self.assertIsNone(selected[1]['blocked_job_id'])
        self.assertEqual(before, (self.row(parent), self.row(self.parent), self.row(self.sibling)))
        rest = self.memory.claim(self.project)
        self.assertEqual('B' * 2000, rest['sources'][0]['body'])

    def test_expired_successor_reconciles_explicitly_without_dispatch_or_lost_lineage(self):
        child = self.memory.claim_recovery(self.project, expected=self.expected)
        self.store._write(lambda: self.store._connection.execute('UPDATE pm_jobs SET lease_until=0 WHERE id=?', (child['job_id'],)))
        selector = self.selector(child['job_id'])
        self.assertIsNone(self.memory.claim(self.project))
        result = self.memory.quarantine_expired_recovery(self.project, expected=selector)
        self.assertEqual('quarantined', result['status'])
        self.assertEqual('lease_expired', self.row(child['job_id'])['error'])
        self.assertEqual(1, self.row(child['job_id'])['attempts'])
        self.assertEqual(1, self.store._connection.execute('SELECT count(*) FROM pm_job_recoveries').fetchone()[0])
        self.assertIsNone(self.memory.claim(self.project))
        with self.assertRaises(ValueError):self.memory.quarantine_expired_recovery(self.project, expected=selector)

    def test_source_provenance_changes_after_claim_revoke_commit(self):
        fields = dict(source='hook:Stop', session_id='wrong-session', turn_id='changed-turn',
                      title='Changed title', created_at='2000-01-01T00:00:00Z')
        for field, value in fields.items():
            with self.subTest(field=field):
                parent = self.quarantine('provenance-' + field, 'Keep evidence attribution exact.')
                child = self.memory.claim_recovery(self.project, expected=self.selector(parent))
                sid = child['sources'][0]['id']
                self.store._write(lambda: self.store._connection.execute('UPDATE entries SET '+field+'=? WHERE id=?', (value, sid)))
                self.assertFalse(self.memory.snapshot_current(child))
                self.assertEqual('stale', self.memory.commit(child, dict(episodes=[], claims=[], relations=[]))['status'])
                self.assertEqual('quarantined', self.row(child['job_id'])['status'])

    def test_bounded_prefix_failure_keeps_entire_authorized_set_blocked(self):
        parent = self.large_quarantine()
        child = self.memory.claim_recovery(self.project, expected=self.selector(parent), max_events=1)
        self.assertEqual(1, len(child['sources']))
        self.assertEqual(2, len(child['recovery_events']))
        self.memory.fail(child, 'timeout')
        events = self.store._connection.execute("SELECT done,offset,blocked_job_id FROM pm_events WHERE session_id='large'").fetchall()
        self.assertTrue(all(not e['done'] and e['offset']==0 and e['blocked_job_id']==child['job_id'] for e in events))
        self.assertIsNone(self.memory.claim(self.project))

    def test_prefix_commit_still_guards_provenance_of_undispatched_tail(self):
        parent = self.large_quarantine()
        child = self.memory.claim_recovery(self.project, expected=self.selector(parent), max_events=1)
        tail = child['recovery_events'][1]['source_id']
        self.store._write(lambda: self.store._connection.execute('UPDATE entries SET title=? WHERE id=?', ('Changed tail',tail)))
        self.assertFalse(self.memory.snapshot_current(child))
        self.assertEqual('stale', self.memory.commit(child,dict(episodes=[],claims=[],relations=[]))['status'])
        self.assertEqual(2,self.store._connection.execute('SELECT count(*) FROM pm_events WHERE blocked_job_id=? AND done=0',(child['job_id'],)).fetchone()[0])

    def test_expired_reconciliation_is_guarded_and_does_not_touch_siblings(self):
        child = self.memory.claim_recovery(self.project, expected=self.expected)
        selector = self.selector(child['job_id'])
        before = self.row(self.parent),self.row(self.sibling)
        with self.assertRaises(ValueError):self.memory.quarantine_expired_recovery(self.project,expected=selector)
        self.store._write(lambda:self.store._connection.execute('UPDATE pm_jobs SET lease_until=0 WHERE id=?',(child['job_id'],)))
        with self.assertRaises(ValueError):self.memory.quarantine_expired_recovery(str(self.root/'foreign'),expected=selector)
        bad=copy.deepcopy(selector);bad['events'][0]['offset']=1
        with self.assertRaises(ValueError):self.memory.quarantine_expired_recovery(self.project,expected=bad)
        self.assertEqual('running',self.row(child['job_id'])['status'])
        self.assertEqual(before,(self.row(self.parent),self.row(self.sibling)))

    def test_ordinary_new_event_does_not_silently_stale_expired_recovery(self):
        child=self.memory.claim_recovery(self.project,expected=self.expected)
        self.store._write(lambda:self.store._connection.execute('UPDATE pm_jobs SET lease_until=0 WHERE id=?',(child['job_id'],)))
        entry=self.store.remember(self.project,'New authored event','Inspect a separate change.',session_id='one',source='hook:UserPromptSubmit')
        self.store._write(lambda:capture_event(self.store._connection,entry['id']))
        normal=self.memory.claim(self.project)
        self.assertEqual([entry['id']],[s['id']for s in normal['sources']])
        self.assertEqual('quarantined',self.row(child['job_id'])['status'])
        self.assertEqual('lease_expired',self.row(child['job_id'])['error'])
        self.assertEqual(1,self.store._connection.execute('SELECT count(*) FROM pm_job_recoveries').fetchone()[0])
        self.assertEqual(child['job_id'],self.store._connection.execute('SELECT blocked_job_id FROM pm_events WHERE source_id=?',(self.expected['events'][0]['source_id'],)).fetchone()[0])

    def test_stale_expired_recovery_is_explicitly_addressable(self):
        child=self.memory.claim_recovery(self.project,expected=self.expected)
        self.store._write(lambda:self.store._connection.execute("UPDATE pm_jobs SET status='stale',lease_until=0,input_json='{}' WHERE id=?",(child['job_id'],)))
        selector=self.selector(child['job_id'])
        result=self.memory.quarantine_expired_recovery(self.project,expected=selector)
        self.assertEqual('quarantined',result['status'])
        # A new, separately inspected selector authorizes exactly one further successor.
        following=self.memory.claim_recovery(self.project,expected=self.selector(child['job_id']))
        self.assertNotEqual(child['job_id'],following['job_id'])
        self.assertEqual(2,self.store._connection.execute('SELECT count(*) FROM pm_job_recoveries').fetchone()[0])

    def test_preclaim_provenance_mutation_rejects_original_selector(self):
        sid=self.expected['events'][0]['source_id']
        self.store._write(lambda:self.store._connection.execute('UPDATE entries SET source=? WHERE id=?',('hook:Stop',sid)))
        with self.assertRaises(ValueError):self.memory.claim_recovery(self.project,expected=self.expected)
        self.assertEqual('quarantined',self.row(self.parent)['status'])

    def test_disclosed_historical_source_provenance_is_also_pinned(self):
        history_parent=self.quarantine('history','TASK-101: Keep the old evidence.')
        history_child=self.memory.claim_recovery(self.project,expected=self.selector(history_parent))
        historical=dict(history_child['sources'][0],historical=True)
        self.memory.commit(history_child,dict(episodes=[],claims=[],relations=[]))
        # This disclosure adapter only supplies an existing, fully consumed
        # original source. The storage/commit/provenance guards remain real.
        def disclose(project,state,sources,max_chars,max_events):
            sources.append(historical)
            return dict(disclosed_sources=1)
        with mock.patch.object(self.memory,'_disclose_history',side_effect=disclose):
            child=self.memory.claim_recovery(self.project,expected=self.selector(self.parent))
        self.assertTrue(self.memory.snapshot_current(child))
        self.store._write(lambda:self.store._connection.execute('UPDATE entries SET turn_id=? WHERE id=?',('changed-history-turn',historical['id'])))
        self.assertFalse(self.memory.snapshot_current(child))
        self.assertEqual('stale',self.memory.commit(child,dict(episodes=[],claims=[],relations=[]))['status'])
