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


def bind_source_metadata(release,archive,*,evidence_prefix,archive_path,helper_path):
    """Consume the flat release/files and archive/runtime_files public shapes."""
    require(isinstance(release,dict) and isinstance(archive,dict),'source_metadata_objects_required')
    require('files' in release and 'core_files' in release and 'runtime_files' in archive
            and 'runtime_archive_sha256' in archive and 'helper_sha256' in archive,'source_metadata_required_fields_missing')
    public=file_map(release['files'],'release_files');core=file_map(release['core_files'],'core_files')
    runtime=file_map(archive['runtime_files'],'runtime_files')
    require(all(re.fullmatch(r'codex_mem/[A-Za-z_]\w*\.py',name) for name in runtime),'runtime_module_paths_required')
    pins={evidence_prefix.rstrip('/')+'/'+name:pin for name,pin in public.items()}
    for name,pin in core.items():
        require(name not in pins or pins[name]==pin,'conflicting_source_pin');pins[name]=pin
    for name,pin in runtime.items():
        require(name not in pins or pins[name]==pin,'conflicting_source_pin');pins[name]=pin
    fixed=file_map({archive_path:archive['runtime_archive_sha256'],helper_path:archive['helper_sha256']},'archive_pins')
    for name,pin in fixed.items():
        require(name not in pins or pins[name]==pin,'conflicting_source_pin');pins[name]=pin
    return {'status':'source_metadata_bound_not_actual_audit','files':pins,'public_files':len(public),
            'runtime_modules':len(runtime),'actual_export_audit_performed':False,
            'limit':'Schema/pin expansion only; owning actual audit must independently verify bytes/export after an actual export exists.'}
