"""
 @file
 @brief Fast partial fingerprints for media identity and relinking
"""

import hashlib
import os

from classes.logger import log

_SAMPLE_BYTES = 1024 * 1024  # 1 MB from each end


def fingerprint(path):
    """Return a fingerprint dict for *path*, or None if it cannot be hashed.

    Shape: ``{"size": int, "mtime": float, "sha256": str}``.

    Image-sequence patterns (paths containing ``%``) and missing/unreadable
    files return None. Hashes the first and last 1 MB plus the size so large
    files stay cheap to fingerprint.
    """
    if not path or "%" in str(path):
        return None
    try:
        if not os.path.isfile(path):
            return None
        st = os.stat(path)
        size = int(st.st_size)
        mtime = float(st.st_mtime)
    except OSError:
        return None

    try:
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with open(path, "rb") as fh:
            head = fh.read(_SAMPLE_BYTES)
            digest.update(head)
            if size > _SAMPLE_BYTES:
                fh.seek(max(0, size - _SAMPLE_BYTES))
                digest.update(fh.read(_SAMPLE_BYTES))
        return {
            "size": size,
            "mtime": mtime,
            "sha256": digest.hexdigest(),
        }
    except OSError:
        log.debug("Could not fingerprint %s", path, exc_info=1)
        return None


def fingerprints_match(a, b, ignore_mtime=True):
    """True when two fingerprint dicts identify the same content."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("sha256") and b.get("sha256") and a.get("sha256") == b.get("sha256"):
        if a.get("size") == b.get("size"):
            return True
    if not ignore_mtime:
        return (
            a.get("size") == b.get("size")
            and a.get("mtime") == b.get("mtime")
            and a.get("sha256") == b.get("sha256")
        )
    return False


def scan_folder_for_fingerprints(folder, wanted=None):
    """Walk *folder* and return ``{sha256: path}`` for files that match *wanted*.

    *wanted* is an optional set of sha256 hex digests. When provided, scanning
    stops early once every wanted digest is found.
    """
    found = {}
    if not folder or not os.path.isdir(folder):
        return found
    remaining = set(wanted) if wanted else None
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            fp = fingerprint(path)
            if not fp:
                continue
            digest = fp.get("sha256")
            if not digest:
                continue
            if remaining is not None and digest not in remaining:
                continue
            if digest not in found:
                found[digest] = path
            if remaining is not None:
                remaining.discard(digest)
                if not remaining:
                    return found
    return found
