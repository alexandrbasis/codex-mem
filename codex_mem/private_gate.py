"""Conservative private-prompt markers independent of the shared state lock.

The synchronous worker may be cancelled while opening SQLite. Establish the
marker first so a later tool hook cannot mistake that cancellation for a
public prompt. Markers contain no prompt text or session identifiers.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import data_dir_path


def _marker(session_key: str, data_dir: str | os.PathLike[str] | None) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return data_dir_path(data_dir) / "private-prompt-gates" / digest


def mark_private(session_key: str, data_dir: str | os.PathLike[str] | None) -> None:
    path = _marker(session_key, data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Creating an empty file is atomic. There is no temporary unprotected state
    # between deleting an old marker and writing its replacement.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)


def private_active(session_key: str, data_dir: str | os.PathLike[str] | None) -> bool:
    try:
        _marker(session_key, data_dir).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Unreadable privacy state cannot authorize tool capture.
        return True


def clear_private(session_key: str, data_dir: str | os.PathLike[str] | None) -> None:
    try:
        _marker(session_key, data_dir).unlink()
    except FileNotFoundError:
        pass
