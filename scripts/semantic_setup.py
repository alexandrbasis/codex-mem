#!/usr/bin/env python3
"""Install the optional local semantic runtime and its pinned model explicitly."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / ".local/share/codex-mem/runtime"
MARKER = ".codex-mem-semantic-runtime.json"
OWNER = ".codex-mem-runtime-owner"
PACKAGES = ["fastembed==0.8.0", "onnxruntime==1.23.2"]


FASTEMBED_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/2a/e8/"
    "26b7d78bb8972498c467ca34cb12ee2e60d26ba5eae6d8443189a1af37a5/"
    "fastembed-0.8.0-py3-none-any.whl"
)
FASTEMBED_WHEEL_SHA256 = "40bee672657574a1009e35ec50030a55f2b426842cb011845379817641bbbbd0"
PATCH_METADATA = "fastembed-0.8.0.dist-info/METADATA"
PATCH_RECORD = "fastembed-0.8.0.dist-info/RECORD"
PATCH_OLD_REQUIREMENT = 'Requires-Dist: pillow (>=11.0.0,<13.0) ; python_version == "3.13"'
PATCH_NEW_REQUIREMENT = 'Requires-Dist: pillow (>=13.0.0,<14.0) ; python_version == "3.13"'


def security_wheel(source: bytes) -> bytes:
    """Rebuild the pinned wheel with the locally verified Python 3.13 Pillow bound.

    No executable upstream code changes. A different upstream archive fails
    closed. The patch was checked against E5 embeddings and image transforms.
    """
    if hashlib.sha256(source).hexdigest() != FASTEMBED_WHEEL_SHA256:
        raise ValueError("FastEmbed source wheel checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    metadata = files[PATCH_METADATA].decode("utf-8")
    if metadata.count(PATCH_OLD_REQUIREMENT) != 1:
        raise ValueError("FastEmbed Pillow requirement differs from the reviewed patch")
    files[PATCH_METADATA] = metadata.replace(PATCH_OLD_REQUIREMENT, PATCH_NEW_REQUIREMENT).encode("utf-8")
    records = io.StringIO()
    writer = csv.writer(records, lineterminator="\n")
    for name, contents in files.items():
        if name != PATCH_RECORD:
            digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).decode().rstrip("=")
            writer.writerow((name, "sha256=" + digest, str(len(contents))))
    writer.writerow((PATCH_RECORD, "", ""))
    files[PATCH_RECORD] = records.getvalue().encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            # Fixed timestamps make the patch receipt reproducible.
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), contents)
    return output.getvalue()


def install_packages(version: tuple[int, ...]) -> tuple[list[str], dict | None]:
    """Keep the Python 3.13 local compatibility patch across setup reruns."""
    if version != (3, 13):
        return PACKAGES, None
    directory = RUNTIME.parent / "security-patches" / "pillow13"
    if directory.is_symlink():
        raise ValueError("The security patch directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_path = directory / "fastembed-0.8.0.upstream.whl"
    if source_path.is_symlink():
        raise ValueError("The security patch source must not be a symlink")
    if source_path.exists():
        with source_path.open("rb") as handle:
            source = handle.read(1024 * 1024 + 1)
    else:
        with urlopen(FASTEMBED_WHEEL_URL, timeout=30) as response:
            source = response.read(1024 * 1024 + 1)
    if len(source) > 1024 * 1024:
        raise ValueError("FastEmbed source wheel exceeds the 1 MiB limit")
    patched = security_wheel(source)
    source_path.write_bytes(source)
    wheel_path = directory / "fastembed-0.8.0-py3-none-any.whl"
    if wheel_path.is_symlink():
        raise ValueError("The security patch wheel must not be a symlink")
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(patched)
    try:
        temporary.replace(wheel_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {"patch": "fastembed-0.8.0-python313-pillow13", "source": FASTEMBED_WHEEL_URL,
               "source_sha256": FASTEMBED_WHEEL_SHA256,
               "wheel_sha256": hashlib.sha256(patched).hexdigest(), "wheel": str(wheel_path),
               "scope": "Python 3.13 Pillow requirement only; upstream executable code unchanged"}
    return [str(wheel_path), "onnxruntime==1.23.2", "pillow==13.0.0"], receipt


def interpreter_version(python: str) -> tuple[int, ...]:
    checked = subprocess.run([python, "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"],
                             text=True, capture_output=True, check=True, timeout=10)
    version = tuple(json.loads(checked.stdout))
    if not (3, 10) <= version <= (3, 13):
        raise ValueError("Use Python 3.10 through 3.13 for the optional semantic runtime")
    return version


def setup(python: str, *, model: bool = True) -> dict:
    interpreter_version(python)
    if RUNTIME.is_symlink() or (RUNTIME.exists() and not (RUNTIME / OWNER).is_file()):
        raise ValueError("The runtime path belongs to another installation")
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    (RUNTIME / OWNER).write_text("codex-mem optional semantic runtime v1\n")
    interpreter = RUNTIME / "bin/python3"
    if not interpreter.exists():
        subprocess.run([python, "-m", "venv", str(RUNTIME)], check=True, timeout=120)
    # An existing environment can use a different minor than the requested base.
    version = interpreter_version(str(interpreter))
    packages, security_patch = install_packages(version)
    subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check", *packages],
                   check=True, timeout=600, stdout=sys.stderr)
    if security_patch:
        # pip otherwise skips a local wheel when upstream has the same version.
        # Resolve dependencies above, then replace only the reviewed wheel.
        subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check",
                        "--force-reinstall", "--no-deps", packages[0]],
                       check=True, timeout=120, stdout=sys.stderr)
    subprocess.run([str(interpreter), "-m", "pip", "check"], check=True, timeout=30, stdout=sys.stderr)
    subprocess.run([str(interpreter), "-c", "import fastembed,onnxruntime"], check=True, timeout=30)
    receipt = {"runtime": str(RUNTIME), "python": str(interpreter), "packages": PACKAGES}
    if security_patch:
        receipt["security_patch"] = security_patch
    if model:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([str(interpreter), "-c",
            "import json;from codex_mem.semantic import prepare_model;print(json.dumps(prepare_model()))"],
            env=environment, text=True, capture_output=True, check=True, timeout=600)
        receipt["model"] = json.loads(result.stdout)
        if receipt["model"].get("status") != "ready":
            raise ValueError("The pinned semantic model is not ready")
    temporary = RUNTIME / (MARKER + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(RUNTIME / MARKER)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--runtime-only", action="store_true", help="Install dependencies without downloading the model")
    args = parser.parse_args()
    print(json.dumps(setup(args.python, model=not args.runtime_only), indent=2))
