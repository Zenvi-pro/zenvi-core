"""The executor that owns handoff work, and the registry of what is running.

Everything slow in a handoff -- node/npx renders, ffmpeg, file copies,
hashing source trees, HTTP calls to Adobe hosts -- runs on :data:`EXECUTOR`,
a two-worker ``ThreadPoolExecutor`` (``handoff-*`` threads), never on the Qt
GUI thread. Qt painting (SVG titles, fonts) must not run on a plain thread
either; :func:`run_on_qthread` sends it to a fresh QThread.

:func:`submit_job` queues ``fn(job)`` and delivers ``on_progress(job)`` and
``on_done(job)`` on the GUI thread (``qt_main_thread.invoke_on_gui``).
A tool call that already runs on a worker thread and must block until the
work ends wraps it in :func:`track_job` instead, so the UI still sees it.

Every job is in the registry while it runs: ``running_jobs()``,
``job_for(key)`` (e.g. the file id of a linked clip being re-rendered, which
``linked_media.link_state`` reports as ``rendering``), ``cancel_job(id)``.
Cancellation is cooperative: the work polls ``job.should_cancel()`` (pass it
to ``node_runtime.run_node``).
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterator, List, Optional

from classes.logger import log

EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="handoff")

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED_STATES = (DONE, FAILED, CANCELLED)
PROGRESS_INTERVAL = 0.2   # seconds between GUI progress callbacks (the last one always arrives)

_ids = itertools.count(1)
_lock = threading.RLock()
_jobs: Dict[str, "Job"] = {}
_listeners: List[Callable[["Job"], None]] = []


class JobCancelled(Exception):
    """Raised by ``job.raise_if_cancelled()``; a job ending with it is ``cancelled``, not ``failed``."""


def _gui(func: Callable[..., Any], *args: Any) -> None:
    from classes.qt_main_thread import invoke_on_gui
    try:
        invoke_on_gui(func, *args)
    except Exception:
        log.warning("handoff job callback failed", exc_info=True)


class Job:
    """One unit of handoff work: progress, cancellation and its outcome."""

    def __init__(self, label: str, *, key: Optional[str] = None, kind: str = ""):
        self.id = "h%d" % next(_ids)
        self.label = str(label)
        self.key = key
        self.kind = kind
        self.state = QUEUED
        self.progress: Optional[float] = None
        self.message = ""
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._on_progress: Optional[Callable[["Job"], None]] = None
        self._on_done: Optional[Callable[["Job"], None]] = None
        self._last_progress_emit = 0.0
        self.future: Optional[Future] = None

    # -- the work's side ------------------------------------------------------
    def should_cancel(self) -> bool:
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled(self.label)

    def report(self, progress: Optional[float] = None, message: Optional[str] = None) -> None:
        """Record progress (0..1, None = unknown) and/or a status line; throttled to the GUI."""
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        if message is not None:
            self.message = str(message)
        now = time.monotonic()
        if now - self._last_progress_emit >= PROGRESS_INTERVAL or (progress is not None and progress >= 1.0):
            self._last_progress_emit = now
            self._emit_progress()

    # -- the caller's side ----------------------------------------------------
    def cancel(self) -> bool:
        """Ask the work to stop; True when it was still queued or running."""
        if self.state in FINISHED_STATES:
            return False
        self._cancel.set()
        if self.future is not None and self.future.cancel():
            self._finish(CANCELLED)
        return True

    def wait(self, timeout: Optional[float] = None) -> Any:
        """Block until the job ends; returns its result or raises its error (TimeoutError on timeout)."""
        if not self._done.wait(timeout):
            raise TimeoutError("%s is still running" % self.label)
        if self.state == CANCELLED:
            raise JobCancelled(self.label)
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    def snapshot(self) -> dict:
        """JSON-friendly status for tools and the UI."""
        return {"job_id": self.id, "label": self.label, "key": self.key, "kind": self.kind,
                "state": self.state, "progress": None if self.progress is None else round(self.progress, 3),
                "message": self.message,
                "seconds": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1),
                "error": None if self.error is None else str(self.error)}

    # -- internals ----------------------------------------------------------
    def _start(self) -> None:
        self.state = RUNNING
        self.started_at = time.time()
        _notify(self)

    def _finish(self, state: str) -> None:
        with _lock:
            if self.state in FINISHED_STATES:
                return
            self.state = state
            self.finished_at = time.time()
            _jobs.pop(self.id, None)
        _notify(self)
        if self._on_done is not None:
            _gui(self._on_done, self)
        self._done.set()

    def _emit_progress(self) -> None:
        cb = self._on_progress
        if cb is not None:
            _gui(cb, self)
        _notify(self)

    def __repr__(self) -> str:
        return "<Job %s %r %s>" % (self.id, self.label, self.state)


def _notify(job: Job) -> None:
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        _gui(listener, job)


def add_listener(callback: Callable[[Job], None]) -> None:
    """Call *callback(job)* on the GUI thread whenever any job starts, progresses or ends."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_listener(callback: Callable[[Job], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def _register(job: Job) -> Job:
    with _lock:
        _jobs[job.id] = job
    return job


def submit_job(fn: Callable[[Job], Any], *, label: str, key: Optional[str] = None, kind: str = "",
               on_progress: Optional[Callable[[Job], None]] = None,
               on_done: Optional[Callable[[Job], None]] = None) -> Job:
    """Run ``fn(job)`` on the handoff executor.

    ``on_progress(job)`` follows ``job.report`` calls (throttled) and
    ``on_done(job)`` runs once when it ends -- both on the GUI thread. Read
    ``job.state`` (done / failed / cancelled), ``job.result`` and
    ``job.error`` in ``on_done``. Raising :class:`JobCancelled` (or ending
    after ``cancel()``) finishes it as cancelled.
    """
    job = _register(Job(label, key=key, kind=kind))
    job._on_progress = on_progress
    job._on_done = on_done

    def _run() -> None:
        if job.should_cancel():
            job._finish(CANCELLED)
            return
        job._start()
        try:
            job.result = fn(job)
            state = CANCELLED if job.should_cancel() and job.result is None else DONE
        except JobCancelled:
            state = CANCELLED
        except BaseException as exc:  # reported through on_done / wait()
            job.error = exc
            state = CANCELLED if job.should_cancel() else FAILED
            if state == FAILED:
                log.warning("Handoff job %s (%s) failed: %s", job.id, job.label, exc, exc_info=True)
        job._finish(state)

    job.future = EXECUTOR.submit(_run)
    return job


@contextlib.contextmanager
def track_job(label: str, *, key: Optional[str] = None, kind: str = "",
              on_progress: Optional[Callable[[Job], None]] = None) -> Iterator[Job]:
    """Register blocking work that already runs off the GUI thread (a tool call) as a job.

    Yields the Job: pass ``job.report`` / ``job.should_cancel`` to the work.
    The job ends ``done`` when the block exits normally, ``cancelled`` on
    JobCancelled, ``failed`` on any other exception (which propagates).
    """
    job = _register(Job(label, key=key, kind=kind))
    job._on_progress = on_progress
    job._start()
    try:
        yield job
    except JobCancelled:
        job._finish(CANCELLED)
        raise
    except BaseException as exc:
        job.error = exc
        job._finish(CANCELLED if job.should_cancel() else FAILED)
        raise
    else:
        job._finish(CANCELLED if job.should_cancel() else DONE)


def running_jobs(key: Optional[str] = None, kind: Optional[str] = None) -> List[Job]:
    """Queued and running jobs (optionally for one *key* / *kind*), oldest first."""
    with _lock:
        jobs = list(_jobs.values())
    return [j for j in jobs if (key is None or j.key == key) and (kind is None or j.kind == kind)]


def job_for(key: str) -> Optional[Job]:
    """The running job registered under *key*, if any."""
    found = running_jobs(key=key)
    return found[0] if found else None


def get_job(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(str(job_id))


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    return job.cancel() if job is not None else False


def run_on_qthread(func: Callable[[], Any], timeout_seconds: float = 6 * 60 * 60) -> Any:
    """Run *func()* on a fresh QThread and wait (Qt painting off the GUI thread).

    The editor's own helper (``editor_tools.project_export_render.run_on_qthread``):
    calls straight through without Qt (headless tests).
    """
    from classes.editor_tools.project_export_render import run_on_qthread as _run
    return _run(func, timeout_seconds)
