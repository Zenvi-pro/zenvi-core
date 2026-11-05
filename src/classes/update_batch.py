"""
 @file
 @brief Hold back the expensive reactions to project changes while an edit makes many of them, then do them once.

 Every clip insert, move or property change tells every listener to react: libopenshot applies a diff to the timeline (about a second on a
 real project), the preview frame is re-rendered (another second) and the minimap rebuilds. An edit that touches 85 clips used to pay all of
 that 85 times. Inside a batch the project data and the undo history are still written action by action, exactly as before; only the listeners
 that register a flush callback postpone their reaction, and each runs once when the batch ends.

 GUI thread only (the listeners run there). A batch can span several short GUI-thread hops, so a long edit does not freeze the interface; it
 carries a lease so that a caller that never gets to close it (a crashed worker) cannot leave the timeline un-synced: the next change after
 the lease ends flushes and closes the batch by itself.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, List

from classes.logger import log

DEFAULT_LEASE_SECONDS = 120.0

_depth = 0
_deadline = 0.0
_flushers: List[Callable[[], None]] = []


def register(flush: Callable[[], None]) -> None:
    """A listener that postpones its work inside a batch gives the function that does it once at the end (kept once)."""
    if flush not in _flushers:
        _flushers.append(flush)


def unregister(flush: Callable[[], None]) -> None:
    if flush in _flushers:
        _flushers.remove(flush)


def active() -> bool:
    """True while a batch is open. A batch whose lease ran out is closed (and flushed) first, so an abandoned one cannot stick."""
    global _depth
    if _depth > 0 and time.monotonic() > _deadline:
        log.warning("update batch lease ran out; flushing and closing it")
        _depth = 1
        end()
    return _depth > 0


def begin(lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
    global _depth, _deadline
    _depth += 1
    _deadline = max(_deadline, time.monotonic() + float(lease_seconds)) if _depth > 1 else time.monotonic() + float(lease_seconds)


def end() -> None:
    """Close one level; closing the outermost runs every listener's flush once."""
    global _depth
    if _depth <= 0:
        return
    _depth -= 1
    if _depth == 0:
        for flush in list(_flushers):
            try:
                flush()
            except Exception:  # noqa: BLE001 - one listener failing must not stop the others catching up
                log.error("update batch flush failed for %r", flush, exc_info=True)


@contextmanager
def batch(lease_seconds: float = DEFAULT_LEASE_SECONDS):
    """``with batch(): ...`` the listeners react once at the end, even when the block raises (they must catch up with what was written)."""
    begin(lease_seconds)
    try:
        yield
    finally:
        end()
