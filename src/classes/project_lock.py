"""
 @file
 @brief One Zenvi session owns a project file at a time.

A headless session and the desktop window can both open a project; without a
lock the later save silently replaces the other's changes. The lock lives in
the Zenvi profile (nothing is written next to the project, which may be in a
synced or read-only folder) and is a QLockFile, so a lock left by a crashed
session is noticed and taken over.
"""

import hashlib
import os
import threading

from classes import info
from classes.logger import log

_mutex = threading.Lock()
_held = {"key": None, "lock": None}
# Projects the user chose to open although another session holds them.
_overridden = set()


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(path))


def lock_path(path) -> str:
    """The profile lock file for project *path*."""
    digest = hashlib.sha1(_key(path).encode("utf-8")).hexdigest()
    return os.path.join(info.USER_PATH, "project-locks", digest + ".lock")


def _new_lock(path):
    from qt_api import QLockFile

    target = lock_path(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    lock = QLockFile(target)
    lock.setStaleLockTime(0)  # stale only once its process is gone
    return lock


def _holder_pid(lock):
    try:
        lock_info = lock.getLockInfo()
        if isinstance(lock_info, tuple) and len(lock_info) > 1 and lock_info[0]:
            return lock_info[1]
    except Exception:
        pass
    return None


def claim(path):
    """Own *path* (giving up the previous project's lock).

    Returns ``(True, None)``, or ``(False, holder_pid)`` when another session
    holds it.
    """
    key = _key(path)
    with _mutex:
        if _held["key"] == key:
            return True, None
        try:
            lock = _new_lock(path)
            if not lock.tryLock(0):
                return False, _holder_pid(lock)
        except Exception:
            # A profile we cannot write must not stop the project opening.
            log.warning("Could not lock project %s", path, exc_info=True)
            return True, None
        _release_locked()
        _held.update(key=key, lock=lock)
        return True, None


def override(path):
    """The user opens *path* anyway: their saves to it are their choice."""
    with _mutex:
        _release_locked()
        _overridden.add(_key(path))


def may_save(path):
    """Whether this session may write *path*; ``(False, holder_pid)`` if another owns it."""
    with _mutex:
        if _key(path) in _overridden:
            return True, None
    return claim(path)


def release():
    with _mutex:
        _release_locked()


def _release_locked():
    lock = _held["lock"]
    _held.update(key=None, lock=None)
    if lock is not None:
        try:
            lock.unlock()
        except Exception:
            pass


def in_use_message(path, pid) -> str:
    who = "another Zenvi session (process %s)" % pid if pid else "another Zenvi session"
    return ("%s is open in %s. If both save, one will overwrite the other's changes. "
            "Close it there first (a headless session stops with shutdown_headless_tool)."
            % (os.path.basename(path), who))
