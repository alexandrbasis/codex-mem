"""Conservative private-prompt markers independent of the shared state lock.

The synchronous worker may be cancelled while opening SQLite. Establish the
marker first so a later tool hook cannot mistake that cancellation for a
public prompt. Markers contain no prompt text or session identifiers.
"""

from __future__ import annotations

import hashlib
import os
import stat
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


def mark_private_delivery(session_key: str, delivery_key: str | None,
                          data_dir: str | os.PathLike[str] | None) -> bool:
    """Protect a new private hook immediately; skip a known applied delivery.

    The body-free receipt uses native identity, never prompt text. A crash
    after the privacy marker but before the receipt can conservatively mark
    again. No SQLite or shared-state lock precedes the privacy marker.
    """
    receipt = None
    if delivery_key:
        digest = hashlib.sha256((session_key + ':' + delivery_key).encode('utf-8')).hexdigest()
        receipt = data_dir_path(data_dir) / 'private-prompt-deliveries' / digest
        try:
            metadata = receipt.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
                raise OSError('invalid private delivery receipt')
            return False
        except FileNotFoundError:
            pass
        except OSError:
            mark_private(session_key, data_dir)
            raise
    mark_private(session_key, data_dir)
    if receipt is not None:
        receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
    return True
