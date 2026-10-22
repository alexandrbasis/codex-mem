"""Actual closed R20 public metadata shapes; no actual R21 export audit."""
from copy import deepcopy
import importlib.util,json
from pathlib import Path
import unittest
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('source_binding',HERE/'source-audit-bindings.py')
binding=importlib.util.module_from_spec(spec);spec.loader.exec_module(binding)

class SourceAuditBindingTests(unittest.TestCase):
    def setUp(self):
        self.old=HERE.with_name(HERE.name[:-2]+'20')
        self.release=json.loads((self.old/'formal-release.json').read_text())
        self.archive=json.loads((self.old/'frozen-source-archive.json').read_text())
    def bind(self,release=None,archive=None):
        return binding.bind_source_metadata(release or self.release,archive or self.archive,
            evidence_prefix='docs/evidence/project-memory-quality-2026-10-21-r20',
            archive_path='docs/evidence/project-memory-quality-2026-10-21-r20/frozen-product.tar.gz',
            helper_path='scripts/project_memory_consolidation_experiment.py',public_native_calls=10)
    def test_actual_r20_flat_release_and_archive_fields_are_preserved(self):
        result=self.bind()
        self.assertEqual(result['runtime_modules'],48)
        self.assertEqual(result['files']['scripts/project_memory_consolidation_experiment.py'],self.archive['helper_sha256'])
        self.assertEqual(result['files']['docs/evidence/project-memory-quality-2026-10-21-r20/frozen-product.tar.gz'],self.archive['runtime_archive_sha256'])
        self.assertEqual(result['author_archive']['members'],self.release['archive_members_and_hashes'])
        self.assertEqual(result['author_archive']['member_count'],32)
        self.assertEqual(result['author_archive']['preserved_nonself_manifest_files'],31)
        self.assertEqual(result['author_archive']['verified_root_pins'],22)
        self.assertEqual(result['actual_freeze_sha256'],self.release['actual_freeze_sha256'])
        self.assertEqual(result['protocol_canonical_digest'],self.release['protocol_canonical_digest'])
        self.assertFalse(result['actual_export_audit_performed'])
        self.assertEqual(result['public_native_calls'],10)
    def test_nested_old_assumptions_and_missing_maps_reject_without_synthesis(self):
        unknown=deepcopy(self.release);unknown['status']='unknown'
        with self.assertRaisesRegex(ValueError,'unrecognized_formal_release_schema'):self.bind(unknown)
        for field in ('archive_members_and_hashes','prerequisite_pins','actual_freeze_sha256','protocol_canonical_digest'):
            value=deepcopy(self.release);value.pop(field)
            with self.assertRaises(ValueError):self.bind(value)
        for field in ('runtime_files','runtime_archive_sha256','helper_sha256'):
            value=deepcopy(self.archive);value.pop(field)
            with self.assertRaises(ValueError):self.bind(archive=value)
        value=deepcopy(self.release);key=next(iter(value['archive_members_and_hashes']));value['archive_members_and_hashes'][key]={'sha256':value['archive_members_and_hashes'][key]}
        with self.assertRaisesRegex(ValueError,'flat_sha256_required'):self.bind(value)
        value=deepcopy(self.archive);value['runtime_files']={'../escape.py':'a'*64}
        with self.assertRaises(ValueError):self.bind(archive=value)

if __name__=='__main__':unittest.main()
