#!/usr/bin/env python3
"""Read-only deterministic archive, release and startup evidence verification."""
from pathlib import Path
import hashlib,importlib.util,json,tarfile
B=Path(__file__).resolve().parent;S=B.parent/'project-memory-quality-2026-10-21-r19';E=S/'holdout';R=Path('/private/tmp/codex-mem-quality-20261021-r19-experiment')
def sha(b):return hashlib.sha256(b).hexdigest()
def read(p):return json.loads(p.read_bytes())
spec=importlib.util.spec_from_file_location('r19_immutable_helper',S/'frozen-helper.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
def archive(p):
 with tarfile.open(p) as t:
  names=[m.name for m in t if m.isfile()];assert len(names)==len(set(names))
  return {n:t.extractfile(n).read() for n in names}
pub=archive(E/'fictional-run-evidence.tar.gz');raw={n:(R/n).read_bytes() for n in pub};clean,sanit=h.sanitize_export_members(raw);declared=read(E/'sanitization.json');checks={}
def check(k,v):checks[k]=bool(v)
check('all_member_bytes_reproduced',clean==pub)
check('changed_members_exact',sanit['changed_members']==declared['changed_members'])
check('private_archive_hash',sha(h.export_archive_bytes(raw))==declared['private_original_archive_sha256'])
check('public_archive_bytes',h.export_archive_bytes(clean)==(E/'fictional-run-evidence.tar.gz').read_bytes())
check('tar_parent_pin',sha((E/'fictional-run-evidence.tar.gz').read_bytes())=='fc349e2069f7232b463bca20b5e4b2291feaca9a107a624964d0352b00820e1a')
check('report_parent_pin',sha((E/'report.json').read_bytes())=='e3681ede54fdb23e4c5342b09a42f597e9494a603526981ad5e2b95d19e7056a')
check('manifest_parent_pin',sha((E/'export-manifest.json').read_bytes())=='f422a96ec1c2cae79a2915684f3e0d698ecc35f2d4c71ede9b4b21da79298768')
release=read(S/'formal-release.json');author=archive(S/'released-author-evidence.tar');check('author_archive_parent_pin',sha((S/'released-author-evidence.tar').read_bytes())=='bec2abc13e1209075af2e9c3109871a0e611e784f0a9e430b19544e2c09e1b2e')
for name,expected in release['continuity']['all_private_file_hashes'].items():check('released_private:'+name,sha((Path('/private/tmp/codex-mem-quality-20261021-r19-sealed')/name).read_bytes())==expected)
for name,expected in release['continuity']['frozen_root_file_hashes'].items():
 p=Path(name);p=p if p.is_absolute() else Path('/private/tmp/codex-mem-quality-20261021-r19-sealed')/name
 check('release_frozen_root:'+name,p.exists() and sha(p.read_bytes())==expected)
# Every authored archive member is independently bound to released original or final manifest.
author_member_pins={n:sha(data) for n,data in author.items()}
check('author_members_count',len(author)==56)
check('author_member_private_identity',all((Path('/private/tmp/codex-mem-quality-20261021-r19-sealed')/n).exists() and (Path('/private/tmp/codex-mem-quality-20261021-r19-sealed')/n).read_bytes()==data for n,data in author.items()))
deviation=read(S/'operator-startup-deviation.json');logs={}
for label,name in [('rejected','main-experiment.log'),('actual','main-experiment-normal-environment.log')]:
 p=Path('/private/tmp/codex-mem-quality-20261021-r19-'+name)
 if not p.exists():p=R/name
 logs[label]={'exists':p.exists(),'sha256':sha(p.read_bytes()) if p.exists() else None}
 if label=='rejected':check('rejected_startup_log_hash',p.exists() and sha(p.read_bytes())==deviation['raw_log_sha256'])
check('actual_startup_log_retained',logs['actual']['exists'])
check('original_clock_unchanged',read(R/'run.json')['started_at']==release['original_started_at']==1792636925.937918)
check('failed_startup_zero_native_attempts',deviation['main_model_attempts']==0 and deviation['native_attempts_before']==deviation['native_attempts_after']==8)
check('no_db_config_or_cache_in_archive',not any(any(p in ('data','projects','__pycache__') for p in Path(n).parts) for n in pub))
result={'status':'passed' if all(checks.values()) else 'gaps_found','checks':checks,'check_count':len(checks),'gaps':[k for k,v in checks.items() if not v],'archive_members':len(pub),'sanitized_changed_members':len(sanit['changed_members']),'member_private_public_sha256':{n:{'private':sha(raw[n]),'public':sha(pub[n])} for n in sorted(pub)},'author_archive_members':len(author),'author_member_sha256':author_member_pins,'startup_logs':logs,'primary_audit_sha256':sha((B/'final-export-audit.json').read_bytes()),'auditor_sha256':sha((B/'audit-final-export.py').read_bytes()),'native_calls_or_regrades':0,'limits':['Diagnostic-only metadata sanitization reproduced; no claim of semantic entailment.','Rejected operator command is not a native attempt; elapsed clock and public attempts remain charged.']}
out=B/'archive-integrity-supplement-v2.json';assert not out.exists();out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'sha256':sha(out.read_bytes()),'checks':len(checks),'gaps':result['gaps']}))
