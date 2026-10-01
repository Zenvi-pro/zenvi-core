"""Bounded inference gate for Phase 5 speech / ML work.

One shared semaphore so ASR, VAD, and later diarize/embed jobs do not stampede
CPU/GPU. Cancel tokens are cooperative — long loops check ``is_cancelled``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

# Single in-process bound. Palmier serializes VAD; we do the same for ASR.
_MAX_CONCURRENT = 1
_gate = threading.BoundedSemaphore(_MAX_CONCURRENT)
_tls = threading.local()


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise InterruptedError("speech job cancelled")


@contextmanager
def inference_slot(token: Optional[CancelToken] = None) -> Iterator[CancelToken]:
    """Acquire the global inference slot; release on exit."""
    tok = token or CancelToken()
    acquired = _gate.acquire(blocking=True)
    if not acquired:
        raise RuntimeError("speech inference gate failed to acquire")
    prev = getattr(_tls, "token", None)
    _tls.token = tok
    try:
        tok.raise_if_cancelled()
        yield tok
    finally:
        _tls.token = prev
        _gate.release()


def current_token() -> Optional[CancelToken]:
    return getattr(_tls, "token", None)
