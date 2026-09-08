#!/usr/bin/env python3
"""Install the optional local semantic runtime and its pinned model explicitly."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / ".local/share/codex-mem/runtime"
MARKER = ".codex-mem-semantic-runtime.json"
OWNER = ".codex-mem-runtime-owner"
PACKAGES = ["fastembed==0.8.0", "onnxruntime==1.23.2"]


def setup(python: str, *, model: bool = True) -> dict:
    checked = subprocess.run([python, "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"],
                             text=True, capture_output=True, check=True, timeout=10)
    version = tuple(json.loads(checked.stdout))
    if not (3, 10) <= version <= (3, 13):
        raise ValueError("Use Python 3.10 through 3.13 for the optional semantic runtime")
    if RUNTIME.is_symlink() or (RUNTIME.exists() and not (RUNTIME / OWNER).is_file()):
        raise ValueError("The runtime path belongs to another installation")
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    (RUNTIME / OWNER).write_text("codex-mem optional semantic runtime v1\n")
    interpreter = RUNTIME / "bin/python3"
    if not interpreter.exists():
        subprocess.run([python, "-m", "venv", str(RUNTIME)], check=True, timeout=120)
    subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check", *PACKAGES],
                   check=True, timeout=600, stdout=sys.stderr)
    subprocess.run([str(interpreter), "-c", "import fastembed,onnxruntime"], check=True, timeout=30)
    receipt = {"runtime": str(RUNTIME), "python": str(interpreter), "packages": PACKAGES}
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
