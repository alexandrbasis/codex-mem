"""Strict read-only actual R20 flat release-schema validator."""
import hashlib,json,tarfile
from pathlib import Path
def validate_release(base,author_root,run,release,protocol_digest,helper_hash,check):
 sha=lambda data:hashlib.sha256(data).hexdigest()
 check(release['status']=='formally_released_after_pinned_freeze_and_native_public_gate','release_status')
 check(release['actual_freeze_sha256']==sha((run/'freeze.json').read_bytes()),'actual_freeze_release_binding')
 check(release['protocol_canonical_digest']==protocol_digest,'formal_release_protocol_binding')
 check(release['helper_sha256']==helper_hash,'release_helper_pin')
 check(release['runtime_pin_count']==48,'release_48_runtime_pins')
 check(release['archive_member_type']=='regular_files_only','release_regular_file_archive_contract')
 check(release['independent_review_status']=='accepted' and release['unresolved_findings']==0,'release_independent_review')
 check(release['all_preserved_and_frozen_input_bytes_unchanged'] is True,'release_continuity_attestation')
 for name,expected in release['prerequisite_pins'].items():check(sha((base/name).read_bytes())==expected,'release_prerequisite:'+name)
 archive_path=base/'released-author-evidence.tar'
 check(sha(archive_path.read_bytes())==release['archive_sha256'],'author_archive_release_pin')
 with tarfile.open(archive_path) as t:
  entries=t.getmembers()
  if any(not m.isfile() or Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in entries):raise ValueError('unsafe/nonregular author archive member')
  if len(entries)!=len({m.name for m in entries}):raise ValueError('duplicate author archive name')
  author={m.name:t.extractfile(m).read() for m in entries}
 check(len(author)==release['archive_member_count']==32,'author_32_archive_members')
 check({n:sha(data) for n,data in author.items()}==release['archive_members_and_hashes'],'author_exact_member_hashes')
 name=release['manifest_basename'];manifest=json.loads(author[name])
 check(sha(author[name])==release['manifest_sha256'],'author_manifest_pin')
 check(len(manifest['files'])==release['preserved_manifest_file_count']==31,'author_31_preserved_files')
 check(set(manifest['files'])==set(author)-{name},'manifest_covers_exact_nonself_archive_members')
 for n,expected in manifest['files'].items():check(sha(author[n])==expected,'author_manifest_member:'+n)
 check(sum('/' not in n for n in author)==release['verified_frozen_root_pin_count']==22,'author_22_frozen_root_pins')
 for n,data in author.items():check((author_root/n).read_bytes()==data,'released_author_private_identity:'+n)
 for n,expected in manifest['generic_contract_pins'].items():check(sha((base/n).read_bytes())==expected,'author_generic_contract_pin:'+n)
 check(manifest['counts']==release['counts'],'author_release_count_binding')
 return author
