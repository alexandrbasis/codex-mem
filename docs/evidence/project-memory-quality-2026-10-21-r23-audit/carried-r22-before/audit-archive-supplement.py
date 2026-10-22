#!/usr/bin/env python3
"""R21 read-only archive reconstruction. Requires released canonical export."""
import argparse,hashlib,importlib.util,json,tarfile
from pathlib import Path
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--formal-release-confirmed',action='store_true',required=True);p.add_argument('--complete-export-confirmed',action='store_true',required=True);p.add_argument('--base',type=Path,required=True);p.add_argument('--run',type=Path,required=True);p.add_argument('--export',dest='export_dir',type=Path,required=True);p.add_argument('--binding-json',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 assert not a.output.exists()
 B=Path(__file__).resolve().parent;S=a.base.resolve();E=a.export_dir.resolve();R=a.run.resolve()
 bind=json.loads(a.binding_json.read_bytes());sha=lambda v:hashlib.sha256(v).hexdigest()
 spec=importlib.util.spec_from_file_location('r21_archive_helper',S/'frozen-helper.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
 assert sha((S/'frozen-helper.py').read_bytes())==bind['helper_sha256']
 with tarfile.open(E/'fictional-run-evidence.tar.gz') as t:
  items=[m for m in t.getmembers() if m.isfile()];assert len(items)==len({m.name for m in items});pub={m.name:t.extractfile(m).read() for m in items}
 raw={n:(R/n).read_bytes() for n in pub};clean,sanit=h.sanitize_export_members(raw);declared=json.loads((E/'sanitization.json').read_bytes())
 checks={'all_members_reproduced':clean==pub,'changed_members_exact':sanit['changed_members']==declared['changed_members'],'private_archive_hash':sha(h.export_archive_bytes(raw))==declared['private_original_archive_sha256'],'public_archive_bytes':h.export_archive_bytes(clean)==(E/'fictional-run-evidence.tar.gz').read_bytes(),'no_db_or_configs':not any(any(x in ('data','projects','__pycache__') for x in Path(n).parts) for n in pub)}
 out={'status':'passed' if all(checks.values()) else 'gaps_found','checks':checks,'members':len(pub),'member_hashes':{n:{'private':sha(raw[n]),'public':sha(pub[n])} for n in pub},'native_model_regrade_calls':0}
 a.output.write_text(json.dumps(out,indent=2)+'\n');print(sha(a.output.read_bytes()))
if __name__=='__main__':main()
