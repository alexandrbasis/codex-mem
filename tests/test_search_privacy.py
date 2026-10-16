"""Search presentation hides contact details while explicit reads retain evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.retrieval import preview_records
from codex_mem.store import Store


class SearchPrivacyTests(unittest.TestCase):
    def test_compact_preview_hides_unneeded_contact_identifiers(self):
        row = {'id': 'entry-123456789', 'project': '/fictional/team',
               'title': 'Notification settings for jane@example.test',
               'preview': 'Full name: Jane Example\nPhone: +972 50 123 4567\nTelegram user ID: 123456789\nDelivery remains unverified.'}
        value = json.dumps(preview_records([row]))
        for personal in ('jane@example.test', 'Jane Example', '+972 50 123 4567'):
            self.assertNotIn(personal, value)
        self.assertIn('entry-123456789', value)
        self.assertIn('Delivery remains unverified.', value)
        self.assertEqual('Notification settings for jane@example.test', row['title'])

    def test_full_search_details_hide_nested_personal_fields(self):
        row = {'id': 'n1', 'project': '/fictional/team', 'title': 'Roster updated',
               'observation': {'facts': ['Email jane@example.test, delivery unverified.'],
                               'full_name': 'Jane Example', 'telegram_id': 123456789},
               'source_ids': ['source-123456789']}
        value = preview_records([row], detail='full')[0]
        self.assertNotIn('Jane Example', json.dumps(value))
        self.assertNotIn('jane@example.test', json.dumps(value))
        self.assertEqual(['source-123456789'], value['source_ids'])
        self.assertEqual('Jane Example', row['observation']['full_name'])

    def test_explicit_get_keeps_nonsecret_contact_evidence(self):
        with tempfile.TemporaryDirectory() as directory, Store(directory) as store:
            row = store.remember('/fictional/team', 'Contact receipt',
                                 'Full name: Jane Example\nEmail: jane@example.test')
            stored = store.get('/fictional/team', [row['id']])[0]
            self.assertIn('Jane Example', stored['body'])
            self.assertIn('jane@example.test', stored['body'])

    def test_versions_issue_numbers_and_timing_are_preserved(self):
        row = {'id': 'n2', 'title': 'PRM-951 version 1.11.14',
               'preview': '393 tests passed on 2026-10-16. Retry after 120 seconds.'}
        self.assertEqual(row['preview'], preview_records([row])[0]['preview'])

    def test_unlabelled_full_person_name_is_hidden_in_search_only(self):
        row = {'id': 'n3', 'preview': 'Ответственный Алексей Иванов. Доставка не проверена.'}
        value = preview_records([row])[0]['preview']
        self.assertNotIn('Алексей Иванов', value)
        self.assertIn('Доставка не проверена.', value)
        self.assertIn('Алексей Иванов', row['preview'])


if __name__ == '__main__':
    unittest.main()
