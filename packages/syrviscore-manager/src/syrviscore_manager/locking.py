"""
Installation-level locking for SyrvisCore Manager.

Every mutating operation (install, activate, uninstall, rollback, restore,
cleanup) holds an exclusive flock on ``<home>/.syrviscore.lock`` so that
concurrent invocations (interactive CLI, cron, MCP server) cannot interleave
and corrupt the symlink/manifest state.
"""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from .errors import LockError

LOCK_FILENAME = ".syrviscore.lock"


@contextmanager
def hold_lock(home: Path, timeout_message: str = ""):
    """Hold the exclusive installation lock for the duration of the block.

    Non-blocking: raises LockError immediately if another process holds it,
    which is the right behavior for both interactive use and automation.
    """
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockError(
                "Another syrvisctl operation is in progress "
                f"(lock held on {lock_path}). {timeout_message}".strip()
            )
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
