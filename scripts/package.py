#!/usr/bin/env python3
"""Create a reproducible plugin archive and per-file integrity manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from install import FILES, ROOT, manifest


def package() -> dict:
    version = manifest(ROOT)["version"]
    paths = []
    for name in FILES:
        path = ROOT / name
        if not path.exists():
            raise ValueError(f"Release component missing: {name}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValueError("Release files must not be symlinks")
            if not candidate.is_file() or "__pycache__" in candidate.parts or candidate.suffix == ".pyc":
                continue
            paths.append(candidate)
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in sorted(set(paths))}
    hashes = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in sorted(files.items()))
    files["SHA256SUMS"] = hashes.encode()
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"codex-mem-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"codex-mem/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return {"archive": str(archive), "sha256": digest, "files": len(files), "bytes": archive.stat().st_size}


if __name__ == "__main__":
    print(json.dumps(package(), indent=2))
