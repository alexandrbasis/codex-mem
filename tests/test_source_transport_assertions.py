"""Malformed XML must not make representation-aware assertions pass."""
import unittest
from xml.etree import ElementTree as ET
from source_transport_assertions import SourceRegistry


class SourceTransportAssertionTests(unittest.TestCase):
    def tree(self, reference='', extra=''):
        return ET.fromstring('<root><item id="s1" environment="production"><source id="s1" '
            'source_id="original" source_role="user_intent" source_field="body" start="10" end="16">'
            '<quote_excerpt>abcdef</quote_excerpt></source></item>' + extra + reference + '</root>')

    def test_exact_and_subinterval_refs_resolve_without_association_inheritance(self):
        root = self.tree('<field environment="local"><quote_ref ref="s1" start="11" end="14" '
                         'context_basis="same_source_adjacent_user_statement"/></field>')
        result = SourceRegistry(root).field_sources(root.find('field'))[0]
        self.assertEqual((11, 14, 'bcd'), (result['start'], result['end'], result['quote']))
        self.assertEqual(result['source_id'], 'original')
        self.assertEqual(result['context_basis'], 'same_source_adjacent_user_statement')
        self.assertNotIn('environment', result)
        self.assertEqual(SourceRegistry(root).resolve(root.find('.//source'))['quote'], 'abcdef')
        root.find('.//source').set('context_basis', 'target_only_must_not_be_inherited')
        root.find('.//quote_ref').attrib.pop('context_basis')
        self.assertIsNone(SourceRegistry(root).resolve(root.find('.//quote_ref'))['context_basis'])

    def test_duplicate_source_ids_and_item_only_or_missing_targets_reject(self):
        duplicate = '<source id="s1" source_id="other" start="0" end="1"><quote_excerpt>x</quote_excerpt></source>'
        with self.assertRaisesRegex(AssertionError, 'duplicate'):
            SourceRegistry(self.tree(extra=duplicate))
        for target in ('missing', 'item-only'):
            root = self.tree('<quote_ref ref="' + target + '"/>', '<item id="item-only"/>')
            with self.assertRaisesRegex(AssertionError, 'missing'):
                SourceRegistry(root).resolve(root.find('quote_ref'))

    def test_ambiguous_out_of_bounds_or_changed_provenance_rejects(self):
        for attrs in ('start="11"', 'start="9" end="14"', 'start="12" end="17"',
                      'start="13" end="13"', 'source_role="assistant_report"',
                      'source_field="tool_response.output"', 'source_id="other"', 'tool_use_id="forged"'):
            with self.subTest(attrs=attrs):
                root = self.tree('<quote_ref ref="s1" ' + attrs + '/>')
                with self.assertRaises(AssertionError):
                    SourceRegistry(root).resolve(root.find('quote_ref'))

    def test_partial_or_inconsistent_target_cannot_prove_complete_quote(self):
        for change in ('truncated', 'length'):
            root = self.tree('<quote_ref ref="s1"/>')
            if change == 'truncated': root.find('.//source').set('quote_truncated', 'True')
            else: root.find('.//source').set('end', '17')
            with self.assertRaisesRegex(AssertionError, 'complete'):
                SourceRegistry(root).resolve(root.find('quote_ref'))
