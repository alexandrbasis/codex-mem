#!/usr/bin/env python3
"""Run from a Codex plugin cache without installing Python packages."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Native synchronous hooks need only SQLite and must fit their short deadline.
# Avoid starting a second Python runtime or importing optional search/CLI code.
if __name__ == "__main__" and sys.argv[1:] == ["hook"]:
    from codex_mem.hooks import main as hook_main
    raise SystemExit(hook_main())

runtime = Path.home() / ".local/share/codex-mem/runtime"
python = runtime / "bin/python3"
if (runtime / ".codex-mem-semantic-runtime.json").is_file() and python.is_file():
    if Path(sys.prefix).resolve() != runtime.resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])

from codex_mem.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
