"""Source-only metadata binding contract. Does not perform an export/data audit."""
from pathlib import PurePosixPath
import re


def require(condition,reason):
    if not condition:raise ValueError(reason)


def file_map(value,label):
    require(isinstance(value,dict) and bool(value),label+'_mapping_missing')
    result={}
    for name,pin in value.items():
        require(isinstance(name,str) and bool(name) and not PurePosixPath(name).is_absolute()
                and '..' not in PurePosixPath(name).parts and '\\' not in name,label+'_path_invalid')
        require(isinstance(pin,str) and re.fullmatch('[0-9a-f]{64}',pin) is not None,label+'_flat_sha256_required')
        result[name]=pin
    return result


def bind_source_metadata(release,archive,*,evidence_prefix,archive_path,helper_path,public_native_calls):
    """Consume actual flat formal release + runtime archive metadata, without data reads."""
    require(isinstance(release,dict) and isinstance(archive,dict),'source_metadata_objects_required')
    require(type(public_native_calls) is int and public_native_calls == 10, 'ten_public_calls_required')
    require(type(release.get('schema_version')) is int and release['schema_version'] == 1
            and release.get('status') == 'formally_released_after_pinned_freeze_and_native_public_gate',
            'unrecognized_formal_release_schema')
    required={'archive_members_and_hashes','prerequisite_pins','actual_freeze_sha256','protocol_canonical_digest',
              'archive_sha256','archive_member_count','manifest_basename','manifest_sha256','preserved_manifest_file_count',
              'verified_frozen_root_pin_count','helper_sha256','runtime_pin_count'}
    require(required <= set(release) and {'runtime_files','runtime_archive_sha256','helper_sha256'} <= set(archive),
            'formal_release_and_source_archive_fields_required')
    members=file_map(release['archive_members_and_hashes'],'archive_members')
    prerequisites=file_map(release['prerequisite_pins'],'prerequisite_pins')
    runtime=file_map(archive['runtime_files'],'runtime_files')
    require(all(re.fullmatch(r'codex_mem/[A-Za-z_]\w*\.py',name) for name in runtime),'runtime_module_paths_required')
    require(type(release['archive_member_count']) is int and release['archive_member_count']==len(members)
            and type(release['preserved_manifest_file_count']) is int
            and release['preserved_manifest_file_count']==len(members)-1
            and type(release['verified_frozen_root_pin_count']) is int
            and release['verified_frozen_root_pin_count']==sum('/' not in name for name in members), 'formal_archive_counts_inconsistent')
    require(release['manifest_basename'] in members
            and members[release['manifest_basename']]==release['manifest_sha256'],'formal_manifest_binding_inconsistent')
    require(type(release['runtime_pin_count']) is int and release['runtime_pin_count']==len(runtime)
            and release['helper_sha256']==archive['helper_sha256'],'runtime_helper_metadata_inconsistent')
    file_map({evidence_prefix:'0'*64},'evidence_prefix')
    pins={evidence_prefix.rstrip('/')+'/'+name:pin for name,pin in prerequisites.items()}
    pins.update(runtime)
    fixed=file_map({archive_path:archive['runtime_archive_sha256'],helper_path:archive['helper_sha256'],
                   evidence_prefix.rstrip('/')+'/released-author-evidence.tar':release['archive_sha256']},'archive_pins')
    for name,pin in fixed.items():
        require(name not in pins or pins[name]==pin,'conflicting_source_pin');pins[name]=pin
    file_map({'actual_freeze':release['actual_freeze_sha256'],'canonical_protocol':release['protocol_canonical_digest']},'formal_identity')
    return {'status':'source_metadata_bound_not_actual_audit','public_native_calls':public_native_calls,'files':pins,'runtime_modules':len(runtime),
            'actual_freeze_sha256':release['actual_freeze_sha256'],'protocol_canonical_digest':release['protocol_canonical_digest'],
            'author_archive':{'members':members,'member_count':len(members),
                              'preserved_nonself_manifest_files':release['preserved_manifest_file_count'],
                              'verified_root_pins':release['verified_frozen_root_pin_count']},
            'actual_export_audit_performed':False,
            'limit':'Source-only metadata schema/pin expansion. No author archive/private contents or actual run read. Owning actual audit must verify bytes, manifest/archive identities and export after export exists.'}
