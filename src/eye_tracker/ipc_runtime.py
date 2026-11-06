"""Private runtime directory for the local command socket (phase 5).

Extracted from ``ipc.py`` so the security-sensitive fallback lives in one small, testable
place. The socket may only be created in a directory this user owns and that others cannot
write to; when no such directory exists the command channel is disabled.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile

from . import APP_SLUG

log = logging.getLogger(__name__)


def _private_temp_dir() -> str | None:
    """``<tmp>/eye-tracker-<uid>``, created with mode 0700.

    ``None`` when it cannot be created or is not safely ours: another account
    may have created it first, which must not let it choose our socket.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    uid = getuid()
    path = os.path.join(tempfile.gettempdir(), f"{APP_SLUG}-{uid}")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        log.debug("Cannot create %s", path, exc_info=True)
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        log.warning("Not using %s for the instance socket: it is not a private directory", path)
        return None
    return path
