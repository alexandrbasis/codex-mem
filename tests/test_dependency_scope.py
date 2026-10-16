"""Owning-project names cannot substitute for a requested dependency."""
import unittest
from codex_mem.semantic import _resume_selection


class DependencyScopeTests(unittest.TestCase):
    def test_foreign_dependency_remains_mandatory_with_owner_qualifiers(self):
        rows = [
            {'id': 'own', 'project': '/apps/atlas', 'kind': 'note',
             'title': 'Atlas installation', 'body': 'Atlas version 4.2 installed.',
             'observation': {'type': 'change'}},
            {'id': 'redis', 'project': '/apps/atlas', 'kind': 'note',
             'title': 'Redis installation for Atlas',
             'body': 'Redis version 7.4 installed for Atlas.',
             'observation': {'type': 'change'}},
        ]
        for query in ('Какая версия Redis установлена в Atlas?',
                      'Какая версия Redis установлена в проекте Atlas?',
                      'В Atlas какая версия Redis установлена?',
                      'Atlas: какая версия Redis установлена?',
                      'What version of Redis is installed in Atlas?'):
            with self.subTest(query=query):
                result = _resume_selection(rows, 1, query=query, project='/apps/atlas')
                self.assertEqual(['redis'], [row['id'] for row in result])


if __name__ == '__main__':
    unittest.main()
