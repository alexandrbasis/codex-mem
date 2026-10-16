from pathlib import Path
import tempfile
import unittest

from codex_mem.project_scopes import discover_project_scopes
from codex_mem.store import Store


class ProjectScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'memory'
        self.parent = self.root / 'workspace'
        self.project = self.parent / 'app'
        self.chat = self.root / 'chat'
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        self.config = {'capture_enabled': True, 'capture_scope': 'all', 'excluded_projects': [], 'included_projects': []}

    def discover(self, **kwargs):
        return discover_project_scopes(self.store, self.project, config=self.config, **kwargs)

    def seed(self, scope, **kwargs):
        return self.store.remember(scope, 'Evidence', 'Retained project evidence', **kwargs)

    def test_empty_child_discloses_parent_without_switching_scope(self):
        self.seed(self.parent)
        result = self.discover()
        self.assertEqual(str(self.project), result['selected_project'])
        self.assertEqual(0, result['exact_entry_count'])
        self.assertEqual([str(self.parent)], [c['project'] for c in result['candidates']])
        self.assertEqual('parent_has_history', result['candidates'][0]['reason'])
        self.assertTrue(result['candidates'][0]['requires_explicit_selection'])

    def test_nonempty_exact_scope_never_proposes_parent_fallback(self):
        self.seed(self.project)
        self.seed(self.parent)
        self.assertEqual([], self.discover()['candidates'])

    def test_sibling_or_name_similarity_never_establishes_ownership(self):
        self.seed(self.root / 'workspace-copy')
        self.seed(self.parent / 'app-other')
        self.assertEqual([], self.discover()['candidates'])

    def test_projectless_file_attribution_is_disclosed_without_merging_results(self):
        self.seed(self.project)
        self.seed(self.chat, observation={'type': 'bugfix', 'files_modified': [str(self.project / 'src/main.py')]})
        result = self.discover(candidate_projects=[self.chat])
        self.assertEqual(str(self.project), result['selected_project'])
        self.assertEqual('file_attribution', result['candidates'][0]['reason'])
        self.assertEqual(str(self.chat), result['candidates'][0]['project'])
        self.assertNotIn('results', result)
        self.assertNotIn('body', str(result))

    def test_relative_or_prefix_lookalike_paths_do_not_establish_association(self):
        self.seed(self.chat, observation={'type': 'bugfix', 'files_modified': ['src/main.py', str(self.project) + '-other/main.py']})
        self.assertEqual([], self.discover(candidate_projects=[self.chat])['candidates'])

    def test_excluded_requested_and_candidate_projects_fail_closed(self):
        self.seed(self.parent)
        self.config['excluded_projects'] = [str(self.parent)]
        self.assertEqual([], self.discover()['candidates'])
        self.assertEqual('scope_disabled', self.discover()['status'])

    def test_selected_child_does_not_authorize_parent_metadata(self):
        self.seed(self.parent)
        self.config.update(capture_scope='selected', included_projects=[str(self.project)])
        self.assertEqual([], self.discover()['candidates'])

    def test_private_evidence_does_not_create_scope_candidates(self):
        self.seed(self.parent, tags=['private'])
        self.seed(self.chat, tags=['private'], observation={'type': 'bugfix', 'files_read': [str(self.project / 'secret.py')]})
        self.assertEqual([], self.discover(candidate_projects=[self.chat])['candidates'])

    def test_excluded_associated_chat_is_never_suggested(self):
        self.seed(self.chat, observation={'type': 'bugfix', 'files_read': [str(self.project / 'src/main.py')]})
        self.config['excluded_projects'] = [str(self.chat)]
        self.assertEqual([], self.discover(candidate_projects=[self.chat])['candidates'])

    def test_inventory_discovery_requires_file_attribution_not_body_mentions(self):
        self.store.remember(self.chat, 'App discussion', 'Discussed changes under ' + str(self.project))
        self.assertEqual([], self.discover()['candidates'])
        self.seed(self.chat, observation={'type': 'bugfix', 'files_read': [str(self.project / 'src/main.py')]})
        self.assertEqual([str(self.chat)], [c['project'] for c in self.discover()['candidates']])

    def test_multiple_parents_remain_separate_explicit_choices(self):
        self.seed(self.parent)
        self.seed(self.root)
        result = self.discover()
        self.assertEqual(str(self.project), result['selected_project'])
        self.assertEqual([str(self.parent), str(self.root)], [c['project'] for c in result['candidates']])
        self.assertTrue(all(c['requires_explicit_selection'] for c in result['candidates']))

    def test_disabled_capture_config_does_not_disclose_discovery(self):
        self.seed(self.parent)
        self.config['capture_enabled'] = False
        result = self.discover()
        self.assertEqual('scope_disabled', result['status'])
        self.assertIsNone(result['exact_entry_count'])
        self.assertEqual([], result['candidates'])

    def test_candidate_limit_and_parent_depth_are_bounded(self):
        with self.assertRaises(ValueError):
            self.discover(limit=11)
        with self.assertRaises(ValueError):
            self.discover(parent_depth=5)
        with self.assertRaises(ValueError):
            self.discover(candidate_projects=[self.root / str(i) for i in range(129)])

    def test_discovery_does_not_modify_store(self):
        self.seed(self.parent)
        before = self.store._connection.total_changes
        self.discover()
        self.assertEqual(before, self.store._connection.total_changes)

    def test_closed_store_returns_unavailable_without_candidates(self):
        self.store.close()
        result = self.discover()
        self.assertEqual('unavailable', result['status'])
        self.assertEqual([], result['candidates'])
