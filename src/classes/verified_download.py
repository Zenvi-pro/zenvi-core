"""
 @file
 @brief Fetch one file the app needs only sometimes (a speech or recognition model), checked against a pinned hash.

 The rule in one place: the file is staged in a temporary file beside its destination and moved into place only when it is whole and its
 SHA-256 (and size, when known) match what the release was tested with, so a failed, cancelled or tampered download leaves nothing half-written.
 Blocks on the network: call it from a worker, never the GUI thread.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, Callable, Optional


class DownloadError(RuntimeError):
    """The download did not finish; the message says why. ``cancelled`` is True when the caller asked to stop."""

    def __init__(self, message: str, cancelled: bool = False):
        super().__init__(message)
        self.cancelled = cancelled


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_intact(path: str, sha256: str, size: Optional[int] = None) -> bool:
    try:
        return (size is None or os.path.getsize(path) == size) and sha256_of(path) == sha256
    except OSError:
        return False


def fetch_verified(url: str, dest: str, *, sha256: str, size: Optional[int] = None,
                   progress: Optional[Callable[[int], None]] = None, should_cancel: Optional[Callable[[], bool]] = None,
                   opener: Optional[Callable[[str], Any]] = None) -> None:
    """Download *url* to *dest* (replacing it only when the new file is intact). *progress* gets the bytes read so far.

    ``opener`` (url -> readable context manager) exists for tests. Raises DownloadError.
    """
    import urllib.request
    open_url = opener or (lambda u: urllib.request.urlopen(u, timeout=60))
    folder = os.path.dirname(os.path.abspath(dest))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(dest) + "_", suffix=".part", dir=folder)
    os.close(fd)
    try:
        read = 0
        with open_url(url) as resp, open(tmp, "wb") as out:
            while True:
                if should_cancel and should_cancel():
                    raise DownloadError("cancelled", cancelled=True)
                block = resp.read(1 << 18)
                if not block:
                    break
                out.write(block)
                read += len(block)
                if progress:
                    progress(read)
        if not is_intact(tmp, sha256, size):
            raise DownloadError("the file did not match its checksum")
        os.replace(tmp, dest)
        tmp = ""
    except DownloadError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which shows it
        raise DownloadError(str(exc)[:160]) from exc
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
