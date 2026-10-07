"""
Tool handlers — execute OpenShot tool calls received from the backend.

When the backend's LangChain agent calls a tool that needs the running Qt
application (project state, playback, timeline manipulation), the backend
delegates the call to the frontend via WebSocket.  This module maps tool
names to the actual functions that interact with the live Qt application.

Usage (from ai_chat_ui.py):
    from classes.tool_handlers import execute_tool
    result = execute_tool(tool_name, tool_args)
"""

import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid as uuid_module
from collections import Counter
from time import monotonic as _monotonic
from typing import Optional

from classes.ffmpeg_cli import run_ffmpeg
from classes.logger import log
from classes.clip_placement import (
    blind_trim_rejected,
    butt_against_previous_clip,
    compute_clip_trim_bounds,
    default_underlay_layer_number,
    end_bounds_keep_window,
    file_looks_like_image,
    parse_seconds_arg,
    parse_timecode_token,
    placement_watch_query,
    quantize_placement_seconds,
    should_watch_placement,
    source_window_for_file,
)
from classes.agent_tools.handlers import (
    PHASE3_DISPLAY_LABELS,
    PHASE3_HANDLERS,
    PHASE4_DISPLAY_LABELS,
    PHASE4_HANDLERS,
    PHASE5_DISPLAY_LABELS,
    PHASE5_HANDLERS,
)
from classes.image_types import is_audio_only_media
from classes.track_display import (
    format_track_label_for_llm,
    layer_number_to_display_index,
    layers_sorted_by_number,
    normalize_track_or_layer_arg,
)

try:
    from qt_api import (
        QObject, QThread, pyqtSignal, pyqtSlot,
        QEventLoop, QPointF, QTimer,
    )
except ImportError:
    QObject = object
    QThread = None
    pyqtSignal = None
    pyqtSlot = lambda x: x
    QEventLoop = None
    QPointF = None
    QTimer = None

try:
    from qt_api import QApplication
except ImportError:
    QApplication = None


# ---------------------------------------------------------------------------
# Main-thread dispatcher (signal-based)
# ---------------------------------------------------------------------------
# QTimer.singleShot(0, fn) called from a *background* thread creates the
# timer on that thread's event loop — if the thread is blocked (as the AI-
# chat WebSocket loop is), the callback never fires and the caller times
# out.  Instead we use a QObject that lives on the main thread and deliver
# the callable via a cross-thread signal which Qt routes through the main
# event loop.

if pyqtSignal is not None:

    class _MainThreadDispatcher(QObject):
        """Singleton helper that runs callables on the Qt main (GUI) thread."""

        _dispatch = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._dispatch.connect(self._on_dispatch)

        @pyqtSlot(object)
        def _on_dispatch(self, job):
            job.run()

else:

    class _MainThreadDispatcher:
        """Headless fallback when no Qt binding is available."""

        def run(self, fn):
            return fn()


_dispatcher = None
_dispatcher_lock = threading.Lock()


def _get_dispatcher():
    """Return (and lazily create) the singleton main-thread dispatcher."""
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher
    with _dispatcher_lock:
        if _dispatcher is not None:
            return _dispatcher
        d = _MainThreadDispatcher()
        # Ensure the dispatcher lives on the main thread so that signals
        # emitted from background threads are delivered via QueuedConnection.
        app = QApplication.instance() if QApplication is not None else None
        if app is not None:
            d.moveToThread(app.thread())
        _dispatcher = d
        return d


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_app():
    from classes.app import get_app
    return get_app()


def _pause_player():
    import time as _time
    try:
        app = _get_app()
        player = app.window.preview_thread.player
        import openshot
        was_playing = player.Mode() == openshot.PLAYBACK_PLAY
        player.Pause()
        _time.sleep(0.05)
        return was_playing
    except Exception:
        return False


def _resume_player(was_playing):
    try:
        if was_playing:
            _get_app().window.preview_thread.player.Play()
    except Exception:
        pass


# How long a marshalled call waits for the GUI thread before giving up.
MAIN_THREAD_TIMEOUT_SECONDS = 30


class MainThreadTimeout(TimeoutError):
    """The Qt main thread never ran a marshalled call.

    Raised as a distinct type so an unattended MCP/harness run can tell "the
    editor is wedged" apart from an ordinary tool error: read-only tools keep
    answering from the worker thread even when the GUI thread is stuck, so this
    is the only signal that the event loop has stopped draining.

    The call is withdrawn before this is raised, so it can never run later
    behind the caller's back -- where a retry would apply the edit twice.
    """


class MainThreadStillRunning(MainThreadTimeout):
    """A marshalled call started on the GUI thread but outlived the wait.

    Too late to withdraw: it will finish on its own, so the caller must not
    retry it.  ``job_id`` identifies it to wait_for_main_thread_job().
    """

    def __init__(self, message, job_id):
        super().__init__(message)
        self.job_id = job_id


class _MainThreadJob:
    """One call marshalled onto the GUI thread.

    The GUI thread claims the job before running it, and a caller whose wait
    ran out withdraws it; both go through one lock, so a timed-out call has
    either not run and never will, or is known to be running.
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    WITHDRAWN = "withdrawn"

    def __init__(self, func, args):
        self._func = func
        self._args = args
        self._lock = threading.Lock()
        self.state = self.PENDING
        self.result = None
        self.error = None
        self.done = threading.Event()
        self.started_at = None
        self.finished_at = None
        self.late_id = None

    def run(self):
        """GUI-thread side: run the call, unless its caller already withdrew it."""
        with self._lock:
            if self.state != self.PENDING:
                return
            self.state = self.RUNNING
            self.started_at = _monotonic()
        try:
            self.result = self._func(*self._args)
        except Exception as exc:
            self.error = exc
        finally:
            with self._lock:
                self.state = self.DONE
                self.finished_at = _monotonic()
                late_id = self.late_id
            self.done.set()
            if late_id:
                log.info(
                    "Main-thread job %s finished after %.1fs, past its caller's wait",
                    late_id, self.finished_at - self.started_at,
                )

    def withdraw(self):
        """Caller side, once its wait ran out: cancel the call if it has not
        started.  Returns the state the job is left in."""
        with self._lock:
            if self.state == self.PENDING:
                self.state = self.WITHDRAWN
            return self.state


# Calls still running when their caller stopped waiting, by job id, so a later
# wait_for_main_thread_job() can report how they ended.  The oldest finished
# job is dropped first; an unfinished one is kept so its caller can still ask.
_LATE_JOBS_MAX = 32
_late_jobs = {}
_late_jobs_lock = threading.Lock()


def _remember_late_job(job) -> str:
    job_id = uuid_module.uuid4().hex[:12]
    with _late_jobs_lock:
        _late_jobs[job_id] = job
        excess = len(_late_jobs) - _LATE_JOBS_MAX
        if excess > 0:
            done = [jid for jid, j in _late_jobs.items() if j.state in (j.DONE, j.WITHDRAWN)]
            for jid in done[:excess]:
                del _late_jobs[jid]
    with job._lock:
        job.late_id = job_id
    return job_id


def wait_for_main_thread_job(job_id, timeout) -> dict:
    """Wait up to *timeout* seconds for a call that outlived its caller's wait.

    Blocks on the job's own completion event, never on the GUI thread, so it is
    safe to call while that thread is still busy.  Returns ``{"state": ...}``:
    "unknown" (no such job this session), "running" (with ``seconds`` so far),
    or "done" (with ``result``, ``error`` and ``seconds`` it ran for).
    """
    with _late_jobs_lock:
        job = _late_jobs.get(str(job_id or "").strip())
    if job is None:
        return {"state": "unknown"}
    job.done.wait(timeout=max(0.0, float(timeout)))
    with job._lock:
        if job.state != job.DONE:
            return {"state": "running", "seconds": _monotonic() - job.started_at}
        return {
            "state": "done",
            "result": job.result,
            "error": job.error,
            "seconds": job.finished_at - job.started_at,
        }


def _run_on_main_thread(func, *args, timeout=None):
    """Schedule *func(*args)* on the Qt main thread and block until it
    finishes.  Returns the value returned by *func*.

    Slice_Triggered (and other timeline-mutating code) relies on Qt signals
    being delivered **synchronously** (direct connection) — specifically the
    IgnoreUpdates signal that prevents partial UI refreshes mid-transaction.
    When those signals are emitted from a background thread they become
    **queued** connections and arrive too late, leading to stale cached
    frames and visual glitches.  By routing the work through the main
    thread's event loop we get the same behaviour as a manual keyboard /
    mouse-driven slice.
    """
    if timeout is None:
        timeout = MAIN_THREAD_TIMEOUT_SECONDS

    if QThread is None:
        # Fallback: no Qt — just call directly (unit-test scenario)
        return func(*args)

    # If we are already on the main thread, run directly
    app = _get_app()
    if QThread.currentThread() is app.thread():
        return func(*args)

    # transaction_id is per-thread (classes/updates.UpdateManager), so the work
    # we are about to queue would otherwise run on the main thread with the
    # main thread's id -- i.e. outside our group.  Carry the caller's id across
    # the hop so a composite operation that ripples in one hop and places in
    # the next still collapses into a single undo step, without every handler
    # having to thread a tid through its signature.
    caller_tid = app.updates.transaction_id

    def _with_caller_transaction(*a):
        previous = app.updates.transaction_id
        app.updates.transaction_id = caller_tid
        try:
            return func(*a)
        finally:
            app.updates.transaction_id = previous

    job = _MainThreadJob(_with_caller_transaction, args)
    dispatcher = _get_dispatcher()
    dispatcher._dispatch.emit(job)

    if not job.done.wait(timeout=timeout):
        # Left queued, the call would still run whenever the GUI thread drains,
        # after the caller had reported failure -- and the agent's retry would
        # then apply the same edit twice.  Withdraw it, or say it is running.
        state = job.withdraw()
        if state == job.WITHDRAWN:
            log.warning("Main-thread call withdrawn: not started within %ss", timeout)
            raise MainThreadTimeout(
                f"MAIN_THREAD_TIMEOUT: the Qt GUI thread did not respond within "
                f"{timeout}s. The call was withdrawn before it started and will "
                f"not run later. The editor is up but its event loop is not "
                f"draining (a modal dialog, or startup never finished). "
                f"Read-only tools still work; call mcp_health_tool to confirm."
            )
        if state == job.RUNNING:
            job_id = _remember_late_job(job)
            log.warning(
                "Main-thread call still running after %ss; tracking it as job %s",
                timeout, job_id,
            )
            raise MainThreadStillRunning(
                f"MAIN_THREAD_STILL_RUNNING: this call started on the Qt GUI "
                f"thread but was still running after {timeout}s (job_id="
                f"{job_id}). Do NOT retry it: it will finish on its own, and a "
                f"retry would apply the edit twice. Call "
                f"wait_for_editor_job_tool(job_id=\"{job_id}\") to wait for its "
                f"outcome, or check get_timeline_state_tool once it has finished.",
                job_id,
            )
        # DONE: it finished between the wait running out and the withdrawal.

    if job.error is not None:
        raise job.error
    return job.result


def _audio_role_of(clip_data, file_data, ctx=None) -> str:
    """speech / music / sfx / ambient / silent / unknown for a timeline listing.

    Reuses the context's already-materialized metadata so listings stay cheap.
    """
    try:
        from classes import audio_mix
        effective = getattr(ctx, "effective_metadata", None) if ctx is not None else None
        return audio_mix.classify_clip_audio_role(clip_data, file_data, effective)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Undo/redo transaction helpers
# ---------------------------------------------------------------------------
# UpdateAction auto-assigns a fresh uuid4 transaction id per mutation whenever
# UpdateManager.transaction_id is unset (see classes/updates.py).  That means a
# handler performing two mutations for one user-facing action (e.g. place a
# clip, then trim it) lands as *two* undo steps, so a single undo only reverts
# half the operation.  Wrapping the mutations in _transaction() gives them one
# shared id, which UpdateManager.undo() reverses as a single group.

# Upper bound on a single undo_tool/redo_tool call.  Mirrors the backend's
# ZENVI_HEURISTIC_MAX_FANOUT default so "undo 999" behaves the same on both
# sides of the WebSocket.
_MAX_UNDO_STEPS = 20


def _new_transaction_id() -> str:
    """Mint an id for a composite operation spanning several main-thread hops."""
    return str(uuid_module.uuid4())


@contextlib.contextmanager
def _transaction(app, tid=None):
    """Group every project mutation made inside this block into ONE undo step.

    Restores the *previous* transaction id rather than clearing it, so nesting
    is safe: an inner block cannot silently detach the outer group.

    Composite operations that mutate across *several* main-thread hops (ripple
    the timeline, then place the clip) pass the same explicit *tid* to each hop.
    UpdateManager groups by transaction id, so the hops collapse into a single
    undo step without anyone having to hold ``transaction_id`` across a thread
    boundary — it stays set only while the main thread is inside the block.
    Use ``_new_transaction_id()`` to mint one.

    With *tid* omitted, an already-active transaction is joined rather than
    nested, so a helper that opens its own transaction still contributes to the
    caller's group instead of splitting off a second undo step.
    """
    prev = app.updates.transaction_id
    if tid is None:
        if prev:
            yield prev
            return
        tid = _new_transaction_id()
    app.updates.transaction_id = tid
    try:
        yield tid
    finally:
        app.updates.transaction_id = prev


@contextlib.contextmanager
def _ignore_history(app):
    """Apply updates inside this block without recording them in history.

    Always restores the flag — a leaked ignore_history=True disables undo
    globally for every subsequent action.
    """
    prev = app.updates.ignore_history
    app.updates.ignore_history = True
    try:
        yield
    finally:
        app.updates.ignore_history = prev


def _atomic(app, func, tid=None):
    """Wrap *func* so every mutation it makes lands in ONE undo transaction.

    Handy for the ``_run_on_main_thread(_do_x)`` handlers: the wrapping has to
    happen inside the main-thread hop, and this keeps the mutation body itself
    untouched.  Pass *tid* to join a composite operation's group.
    """
    def _wrapped(*args, **kwargs):
        with _transaction(app, tid):
            return func(*args, **kwargs)
    return _wrapped


def _coerce_steps(value) -> int:
    """Coerce an LLM-supplied step count to a sane int in [1, _MAX_UNDO_STEPS].

    Tolerates ints, numeric strings and the small number words the backend's
    heuristic router understands, since tool args arrive as raw JSON.
    """
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "once": 1, "twice": 2, "thrice": 3, "couple": 2,
        "few": 3,
    }
    n = 1
    if isinstance(value, bool):
        n = 1
    elif isinstance(value, int):
        n = value
    elif isinstance(value, float):
        n = int(value)
    elif isinstance(value, str):
        token = value.strip().lower()
        if token.isdigit():
            n = int(token)
        elif token in words:
            n = words[token]
        else:
            try:
                n = int(float(token))
            except ValueError:
                n = 1
    return max(1, min(int(n), _MAX_UNDO_STEPS))


def _describe_group(actions) -> str:
    """Summarise one transaction for the agent, using only what it carries.

    Returns e.g. "insert x2 on clips".  Never invents clip names — the
    UpdateAction only reliably holds a type and a key path.
    """
    try:
        if not actions:
            return ""
        counts = Counter(getattr(a, "type", "?") or "?" for a in actions)
        parts = []
        for kind, n in counts.most_common():
            parts.append(f"{kind} x{n}" if n > 1 else kind)
        summary = ", ".join(parts)
        key = getattr(actions[0], "key", None)
        if isinstance(key, (list, tuple)) and key and isinstance(key[0], str):
            summary = f"{summary} on {key[0]}"
        return summary
    except Exception:
        return ""


def _source_fps_parts(file_data):
    """(num, den, fps_float) for a file reader's own fps."""
    fps_data = file_data.get("fps") or {}
    num = parse_timecode_token(fps_data.get("num")) or 30.0
    den = parse_timecode_token(fps_data.get("den")) or 1.0
    if num <= 0:
        num = 30.0
    if den <= 0:
        den = 1.0
    return int(num), int(den), num / den


def _source_duration_seconds(file_data):
    """Source duration in seconds, preferring real seconds over frame counts."""
    start = parse_timecode_token(file_data.get("start")) or 0.0
    end = parse_timecode_token(file_data.get("end")) or 0.0
    if end > start:
        return end - start
    duration = parse_timecode_token(file_data.get("duration"))
    if duration and duration > 0:
        return duration
    reader = file_data.get("reader") or {}
    duration = parse_timecode_token(reader.get("duration"))
    if duration and duration > 0:
        return duration
    frames = parse_timecode_token(file_data.get("video_length")) or 0.0
    _, _, fps_float = _source_fps_parts(file_data)
    return frames / fps_float if frames > 0 and fps_float > 0 else 0.0


def _unreliable_source_fps(file_data):
    """True when a file's own fps must not be used for frame math.

    Audio and some broken imports report 1/1 with video_length counted in that
    1 fps space, which is what sends agents into a place/retry loop.
    """
    _, _, fps_float = _source_fps_parts(file_data)
    if is_audio_only_media(file_data):
        return True
    return fps_float <= 2.0 and _source_duration_seconds(file_data) > 2.0


def _describe_file_for_llm(file_id, file_data):
    fps_num, fps_den, _ = _source_fps_parts(file_data)
    duration = _source_duration_seconds(file_data)
    project_fps = _get_app().project.get("fps") or {}
    p_num = int(parse_timecode_token(project_fps.get("num")) or 30)
    p_den = int(parse_timecode_token(project_fps.get("den")) or 1)
    media_type = file_data.get("media_type") or ("audio" if is_audio_only_media(file_data) else "video")
    line = (
        f"file_id={file_id} path={file_data.get('path','')} "
        f"duration_seconds={duration:.2f} source_fps={fps_num}/{fps_den} "
        f"project_fps={p_num}/{p_den} media_type={media_type}"
    )
    if _unreliable_source_fps(file_data):
        line += (
            f"\nNOTE: source_fps is {fps_num}/{fps_den} and cannot be used for frame math. "
            "Pass start_seconds/end_seconds (in source seconds) - never frame numbers."
        )
    return line


def _project_fps_float(fps) -> float:
    """Project fps as a float, tolerating string or malformed num/den."""
    fps = fps if isinstance(fps, dict) else {}
    num = parse_timecode_token(fps.get("num"))
    den = parse_timecode_token(fps.get("den"))
    if not num or num <= 0:
        num = 30.0
    if not den or den <= 0:
        den = 1.0
    return num / den


def _timeline_signature(app):
    """A cheap, comparable snapshot of what is actually on the timeline.

    Undo used to infer success from the history stack shrinking, which is how
    it could report "Undid 1 action" while the clip the user asked about was
    still there: the step it popped was a trailing metadata update, not the
    insert.  Comparing this before and after answers the question the user
    actually asked -- did the timeline change?

    Returns a dict keyed by clip id so the caller can name what appeared or
    disappeared, not just count it.  Never raises: a signature we could not
    build degrades to "unknown", and the caller falls back to stack counting.
    """
    try:
        out = {}
        for clip in app.project.get("clips") or []:
            data = clip if isinstance(clip, dict) else getattr(clip, "data", None)
            if not isinstance(data, dict):
                continue
            cid = str(data.get("id") or "")
            if not cid:
                continue
            out[cid] = (
                data.get("layer"),
                round(float(data.get("position", 0) or 0), 3),
                round(float(data.get("start", 0) or 0), 3),
                round(float(data.get("end", 0) or 0), 3),
                _clip_content_digest(data),
            )
        return out
    except Exception as e:
        log.debug("_timeline_signature: %s", e)
        return None


# Clip keys that are caches or bookkeeping, not what the clip looks or sounds
# like: a waveform refresh or an AI summary must not read as an edit.
_SIGNATURE_SKIP_KEYS = frozenset({"id", "layer", "position", "start", "end", "reader", "ui", "ai_metadata"})


def _clip_content_digest(data):
    """Fingerprint of a clip's effects, keyframes and properties.

    Without it an undone effect or property edit (a blur, a fade, a volume
    curve) left the (layer, position, start, end) signature unchanged, and undo
    reported "the timeline did not change" for a step it really reverted.
    """
    try:
        rest = {k: v for k, v in data.items() if k not in _SIGNATURE_SKIP_KEYS}
        # The reader is mostly cached metadata, but which media it plays is the
        # clip: a relink, an edited title (its clips point at the new SVG) or a new
        # image-sequence frame rate changes only this.
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        rest["_media"] = [reader.get(k) for k in ("path", "duration", "fps", "video_length")]
        return hash(json.dumps(rest, sort_keys=True, default=str))
    except Exception:
        return None


def _describe_timeline_delta(before, after):
    """Say what changed between two signatures, in the agent's vocabulary.

    Returns "" when nothing changed, or None when it cannot tell.
    """
    if before is None or after is None:
        return None
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    common = set(before) & set(after)
    moved = sorted(cid for cid in common if before[cid][:4] != after[cid][:4])
    restyled = sorted(
        cid for cid in common
        if before[cid][:4] == after[cid][:4] and before[cid][4:] != after[cid][4:]
    )
    parts = []
    if removed:
        parts.append(f"removed {len(removed)} clip(s) ({', '.join(removed[:4])})")
    if added:
        parts.append(f"restored {len(added)} clip(s) ({', '.join(added[:4])})")
    if moved:
        parts.append(f"moved/retrimmed {len(moved)} clip(s) ({', '.join(moved[:4])})")
    if restyled:
        parts.append(
            f"changed effects/properties of {len(restyled)} clip(s) ({', '.join(restyled[:4])})"
        )
    return "; ".join(parts)


def _resolve_timeline_clip_for_tool(**kwargs):
    """Resolve target clip from timeline_clip_id, clip_query, or single-clip shortcut."""
    from classes.clip_resolver import resolve_timeline_clip

    def _do_resolve():
        pos_near = kwargs.get("position_near")
        if pos_near is None:
            pos_near = kwargs.get("prefer_position_near")
        if isinstance(pos_near, str) and not str(pos_near).strip():
            pos_near = None
        occ = kwargs.get("occurrence", 0)
        try:
            occ = int(float(str(occ).strip() or 0))
        except (TypeError, ValueError):
            occ = 0
        return resolve_timeline_clip(
            timeline_clip_id=str(kwargs.get("timeline_clip_id") or "").strip(),
            clip_query=str(kwargs.get("clip_query") or "").strip(),
            prefer_track=str(kwargs.get("prefer_track") or kwargs.get("track") or "").strip(),
            track=str(kwargs.get("track") or kwargs.get("prefer_track") or "").strip(),
            position_near=pos_near,
            occurrence=occ,
        )

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            return _run_on_main_thread(_do_resolve)
    return _do_resolve()


def _resolve_clip_pair_for_tool(**kwargs):
    """Resolve adjacent clip pair with Qt main thread marshalling."""
    from classes.clip_resolver import resolve_clip_pair

    def _do_resolve():
        return resolve_clip_pair(
            clip_a_id=str(kwargs.get("clip_a_id") or "").strip(),
            clip_b_id=str(kwargs.get("clip_b_id") or "").strip(),
            clip_a_query=str(kwargs.get("clip_a_query") or "").strip(),
            clip_b_query=str(kwargs.get("clip_b_query") or "").strip(),
        )

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            return _run_on_main_thread(_do_resolve)
    return _do_resolve()


def _get_source_file_for_clip(clip_obj):
    try:
        from classes.query import File
        data = clip_obj.data if hasattr(clip_obj, "data") and isinstance(clip_obj.data, dict) else {}
        file_id = data.get("file_id")
        if file_id:
            f = File.get(id=str(file_id))
            if f:
                return f
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        path = reader.get("path")
        if path:
            return File.get(path=path)
    except Exception:
        return None
    return None


def _fmt_mmss(seconds: float) -> str:
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 0.0
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


MAX_PLACE_SPAN_SEC = 20.0


def _hit_peak(hit, seg_s: float, seg_e: float) -> float:
    try:
        if hit.get("peak") is not None and str(hit.get("peak")).strip() != "":
            return float(hit.get("peak"))
    except (TypeError, ValueError):
        pass
    return (float(seg_s) + float(seg_e)) / 2.0


def _hit_is_degraded(hit) -> bool:
    if not isinstance(hit, dict):
        return False
    if hit.get("degraded") is True:
        return True
    return str(hit.get("role") or "").strip().lower() == "orientation"


def _format_search_window(hit, seg_s: float, seg_e: float) -> str:
    if _hit_is_degraded(hit):
        return (
            "chapter-level match only — window not action-bounded; "
            "re-index or narrow the query"
        )
    peak = _hit_peak(hit, seg_s, seg_e)
    return (
        f"start_seconds={float(seg_s):.3f} end_seconds={float(seg_e):.3f} "
        f"peak_seconds={peak:.3f} ({_fmt_mmss(seg_s)}–{_fmt_mmss(seg_e)})"
    )


def _as_error(message) -> str:
    """A refusal or failure for the model: always ``Error: ...``.

    Credit blocks (credits_client), provider errors and download failures come
    back as plain prose; the backend, the chat UI and the MCP server read
    anything that does not start with "Error" as success.
    """
    text = " ".join(str(message or "").split()) or "the operation was refused"
    return text if text.startswith("Error") else "Error: " + text


def _ffmpeg_run(args):
    try:
        p = run_ffmpeg(
            args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "ffmpeg failed")
        return True, ""
    except FileNotFoundError:
        return False, "ffmpeg not found."
    except Exception as e:
        return False, str(e)


def _ffprobe_video_duration(path) -> float:
    """Return the video duration in seconds, or 0.0 on error."""
    try:
        p = run_ffmpeg(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        val = (p.stdout or "").strip()
        if val and val != "N/A":
            return float(val)
        # Fallback: use format duration
        p2 = run_ffmpeg(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        val2 = (p2.stdout or "").strip()
        return float(val2) if val2 and val2 != "N/A" else 0.0
    except Exception:
        return 0.0


def _ffprobe_has_audio(path):
    try:
        p = run_ffmpeg(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        return bool((p.stdout or "").strip())
    except Exception:
        return False


def _is_extreme_for_4_seconds(prompt):
    text = (prompt or "").strip().lower()
    if len(text) < 2:
        return True, "Prompt is too short."
    multi_markers = [
        "then ", "after that", "afterwards", "meanwhile", "next ",
        "cut to", "scene change", "montage", "several", "multiple",
        "a series of", "over the course of", "gradually",
        "time-lapse", "timelapse",
    ]
    if sum(1 for m in multi_markers if m in text) >= 2:
        return True, "Multiple steps/scenes."
    extreme_markers = [
        "explode", "nuke", "earthquake", "tsunami", "apocalypse",
        "destroy the city", "teleport", "time travel", "turn into",
        "transform into", "grow wings", "summon", "giant",
        "entire crowd", "army", "hundreds of", "thousands of",
    ]
    if any(m in text for m in extreme_markers):
        return True, "Too extreme for 4s."
    if len(text) > 240:
        return True, "Prompt too detailed for 4s."
    return False, ""


def _twelvelabs_search_in_window(index_id, query_text, *, page_limit=30, video_id=""):
    try:
        from classes.api_client import get_backend_client
        if not str(index_id or "").strip():
            return [], "TwelveLabs index_id is missing for this file."
        client = get_backend_client()
        resp = client.search(
            query=query_text,
            index_id=index_id,
            video_id=video_id,
            page_limit=page_limit,
            top_k=page_limit,
        )
        if isinstance(resp, dict) and resp.get("error"):
            return [], resp["error"]
        results = resp.get("results", []) if isinstance(resp, dict) else []
        items = [type("SearchItem", (), r)() for r in results]
        if video_id:
            items = [it for it in items if str(getattr(it, "video_id", "")) == str(video_id)]
        return items, None
    except Exception as e:
        return [], str(e)


def _output_path_for_generated_video(ext=".mp4"):
    """Return an absolute path for a new generated video (preview-safe)."""
    from classes.assets import durable_media_path
    ext = ext if str(ext).startswith(".") else f".{ext}"
    if ext.lower() not in (".mp4", ".webm", ".mov", ".mkv"):
        ext = ".mp4"
    return durable_media_path(ext=ext)


def _canonical_media_path(path):
    """Normalize to an absolute, expanded path for libopenshot and preview."""
    if not path:
        return path
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def _cleanup_scratch_parent(path, prefix):
    """Remove a tempfile.mkdtemp parent when *path* sits under a matching prefix."""
    from classes.assets import cleanup_scratch_parent
    cleanup_scratch_parent(path, prefix)


def _download_video_url_to_path(video_url: str, dest_path: str, timeout: int = 180) -> Optional[str]:
    """Download a remote generated video to dest_path. Returns None on success, else an error message."""
    if not video_url:
        return "No video URL returned from generation."
    try:
        import requests

        resp = requests.get(
            video_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Zenvi/1.0"},
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    fh.write(chunk)
        if os.path.getsize(dest_path) > 0:
            return None
        return "Downloaded file is empty"
    except Exception as exc:
        return f"Download failed: {exc}"


def _upload_generation_assets(client, seed_path=None, frame_specs=None):
    """Upload seed/frame files for remote generation; returns (seed_file_id, frame_images_paths, error)."""
    seed_file_id = None
    if seed_path and os.path.isfile(seed_path):
        uid = f"gen_seed_{uuid_module.uuid4().hex[:8]}"
        up = client.upload_media_file(seed_path, file_id=uid)
        if not up.get("success"):
            return None, None, up.get("error", "Failed to upload seed video")
        seed_file_id = uid

    frame_images_paths = []
    for idx, spec in enumerate(frame_specs or []):
        path = spec.get("path") or ""
        frame = spec.get("frame", "first")
        if not path or not os.path.isfile(path):
            continue
        uid = f"gen_frame_{idx}_{uuid_module.uuid4().hex[:6]}"
        up = client.upload_media_file(path, file_id=uid)
        if not up.get("success"):
            return None, None, up.get("error", f"Failed to upload frame: {path}")
        frame_images_paths.append({"file_id": uid, "frame": frame})

    return seed_file_id, frame_images_paths or None, None


# Kling O1 Pro via Runware — desktop-side constraints (mirror backend constants).
_KLING_O1_ALLOWED_DURATIONS = [5, 10]
_KLING_O1_MIN_DIM = 720
_KLING_O1_MAX_DIM = 2160


def _snap_kling_o1_duration(duration):
    """Snap to Kling O1 Pro duration: default 5s; use 10s only when clearly requested (>= 8)."""
    try:
        val = float(duration)
    except (TypeError, ValueError):
        return 5
    val = max(1, min(10, val))
    if val >= 8:
        return 10
    return 5


# Managed generation (Grok Imagine) takes any whole-second length in this range.
_GENERATION_MIN_SECONDS = 2
_GENERATION_MAX_SECONDS = 15
# Video edits keep the input's length, which the provider caps at 8.7 s.
_GENERATION_EDIT_MAX_SECONDS = 8.0


def _clamp_generation_duration(duration, default=5):
    """Whole seconds within the managed provider's 2-15 s range; default when unset or unparseable."""
    try:
        val = int(float(duration))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(_GENERATION_MIN_SECONDS, min(_GENERATION_MAX_SECONDS, val))


def _kling_o1_output_dims(width, height):
    """Snap arbitrary dimensions to Kling O1 Pro supported output or video-edit range."""
    w = int(width or 1920)
    h = int(height or 1080)
    if w < _KLING_O1_MIN_DIM or h < _KLING_O1_MIN_DIM:
        scale_f = max(_KLING_O1_MIN_DIM / max(w, 1), _KLING_O1_MIN_DIM / max(h, 1))
        w = int(w * scale_f)
        h = int(h * scale_f)
    w += w % 2
    h += h % 2
    if w > _KLING_O1_MAX_DIM or h > _KLING_O1_MAX_DIM:
        scale_d = min(_KLING_O1_MAX_DIM / max(w, 1), _KLING_O1_MAX_DIM / max(h, 1))
        w = int(w * scale_d)
        h = int(h * scale_d)
        w += w % 2
        h += h % 2
    return w, h


def _project_kling_o1_t2v_dims():
    """Resolve T2V width/height from project settings, snapped for Kling O1 Pro."""
    try:
        proj = _get_app().project
        w = int(proj.get("width") or 1920)
        h = int(proj.get("height") or 1080)
    except Exception:
        w, h = 1920, 1080
    aspect = w / max(h, 1)
    if aspect > 1.2:
        return 1920, 1080
    if aspect < 0.8:
        return 1080, 1920
    return 1440, 1440


def _kling_o1_scale_vf(width, height):
    """FFmpeg scale+pad filter for Kling O1 video-edit dimension range."""
    w, h = _kling_o1_output_dims(width, height)
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    ), w, h


# Context for add_clip_to_timeline (remembers last split file id per chat session)
_last_split_file_id_by_chat_session = {}


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------

def list_files(**_kw) -> str:
    """List media already in the project media bin (does not import from disk).

    To add local folders or files into Project Files, call import_files_tool
    (dry_run=true first for folders). list_files_tool only reports what is
    already imported.
    """
    try:
        import os
        from classes.query import File
        from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

        files = File.filter()
        if not files:
            return (
                "No files in project media bin. "
                "list_files_tool does not import from disk — call "
                "import_files_tool with folder=\"Downloads\" (or a path; prefer "
                "C:/Users/... on Windows), dry_run=true first for folders, and "
                "media_types=video when the user asked for videos only."
            )
        lines = []
        visible = 0
        for f in files:
            d = f.data if isinstance(f.data, dict) else {}
            if d.get("zenvi_subclip"):
                continue
            visible += 1
            name = d.get("name") or os.path.basename(str(d.get("path") or "")) or "?"
            dur = float(d.get("duration", 0) or 0)
            ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
            analyzed = bool(ai.get("analyzed"))
            indexed = twelvelabs_is_indexed(get_index_block(ai))
            preview = _summary_preview_for_file_data(d)
            lines.append(
                f"  media_bin_file_id={f.id} name={name!r} duration={dur:.2f}s "
                f"analyzed={analyzed} indexed={indexed} "
                f"summary_preview={preview!r} "
                f"path={os.path.basename(d.get('path', ''))}"
            )
        if not lines:
            return (
                "No files in project media bin. "
                "list_files_tool does not import from disk — call "
                "import_files_tool with folder=\"Downloads\" or paths= "
                "(folder or file), dry_run=true first for folders."
            )
        return f"Media bin files ({visible}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


_TAGS_PREVIEW_MAX = 80


def _summary_preview_for_file_data(file_data: dict, clip_data: dict | None = None) -> str:
    """Top objects/scenes from effective metadata for compact clip listing."""
    from classes.ai_metadata_utils import build_summary_preview, get_effective_ai_metadata

    if not isinstance(file_data, dict):
        return ""
    effective = get_effective_ai_metadata(file_data, clip_data=clip_data, rebased=True)
    return build_summary_preview(effective)


def _file_is_analyzed(file_data: dict) -> bool:
    if not isinstance(file_data, dict):
        return False
    ai = file_data.get("ai_metadata")
    if not isinstance(ai, dict):
        return False
    return bool(ai.get("analyzed"))


def list_clips(layer="", **_kw) -> str:
    try:
        from classes.query import Clip

        app = _get_app()
        layers_raw = app.project.get("layers") or []
        kwargs = {}
        if layer and str(layer).strip():
            resolved, err = normalize_track_or_layer_arg(str(layer).strip(), layers_raw)
            if err:
                return err
            kwargs["layer"] = resolved
        clips = Clip.filter(**kwargs)
        if not clips:
            return "No clips in project."
        from classes.timeline_clip_context import build_timeline_clip_context, clear_metadata_lookup_cache

        clear_metadata_lookup_cache()
        file_cache: dict[str, dict] = {}

        file_dupes: dict[str, list] = {}
        for c in clips:
            fid = str(c.data.get("file_id") or "")
            layer = c.data.get("layer")
            if fid:
                file_dupes.setdefault(f"{fid}:{layer}", []).append(c)

        lines = []
        for c in clips:
            d = c.data
            lid = d.get("layer", "")
            try:
                lid_int = int(lid) if lid != "" and lid is not None else None
            except (TypeError, ValueError):
                lid_int = None
            ui = layer_number_to_display_index(lid_int, layers_raw) if lid_int is not None else None
            ui_part = f" ui_track={ui}" if ui is not None else ""
            tids = (
                [
                    str(L.get("id", ""))
                    for L in layers_raw
                    if int(L.get("number") or 0) == lid_int
                ]
                if lid_int is not None
                else []
            )
            tid_part = f" track_id={tids[0]}" if tids and tids[0] else ""
            title = d.get("title") or d.get("label") or ""
            fid = d.get("file_id", "")
            fname = ""
            summary_preview = ""
            parent_file_id = ""
            audio_role = ""
            source_start = d.get("start", 0)
            source_end = d.get("end", 0)
            timeline_end = float(d.get("position", 0) or 0)
            if fid:
                try:
                    from classes.query import File as _File
                    if fid not in file_cache:
                        fobj = _File.get(id=str(fid))
                        file_cache[fid] = fobj.data if fobj and isinstance(fobj.data, dict) else None
                    fdata = file_cache.get(fid)
                    if fdata:
                        import os
                        fname = (
                            fdata.get("name")
                            or os.path.basename(str(fdata.get("path") or ""))
                        )
                        ctx = build_timeline_clip_context(c, d, fdata, layers=layers_raw)
                        summary_preview = ctx.summary_preview
                        parent_file_id = ctx.parent_file_id
                        source_start = ctx.source_start
                        source_end = ctx.source_end
                        timeline_end = ctx.timeline_end
                        audio_role = _audio_role_of(d, fdata, ctx)
                except Exception:
                    pass
            summary_part = f" summary_preview={summary_preview!r}" if summary_preview else ""
            occ_hint = ""
            dup_key = f"{fid}:{lid_int}"
            dupes = file_dupes.get(dup_key, [])
            if len(dupes) > 1:
                ranked = sorted(dupes, key=lambda x: float(x.data.get("position", 0) or 0))
                for idx, dc in enumerate(ranked, 1):
                    if dc.id == c.id:
                        occ_hint = f" occurrence_hint={idx}"
                        break
            parent_part = f" parent_file_id={parent_file_id}" if parent_file_id and parent_file_id != str(fid) else ""
            lines.append(
                f"  timeline_clip_id={c.id} media_bin_file_id={fid}{parent_part} "
                f"title={title!r} file={fname!r}{summary_part}{occ_hint} "
                f"layer_number={lid_int if lid_int is not None else lid}{ui_part}{tid_part} "
                f"position={d.get('position',0)} timeline_end={timeline_end:.2f} "
                f"source_start={source_start} source_end={source_end}"
                f"{f' audio_role={audio_role}' if audio_role else ''}"
            )
        from classes.agent_tools.receipt import ToolReceipt
        structured = []
        for c in clips:
            d = c.data if isinstance(c.data, dict) else {}
            lid = d.get("layer", "")
            try:
                lid_int = int(lid) if lid != "" and lid is not None else None
            except (TypeError, ValueError):
                lid_int = None
            ui = layer_number_to_display_index(lid_int, layers_raw) if lid_int is not None else None
            structured.append({
                "id": str(c.id),
                "file_id": str(d.get("file_id") or ""),
                "title": str(d.get("title") or d.get("label") or ""),
                "layer": lid_int,
                "track": ui,
                "position": float(d.get("position") or 0),
                "start": float(d.get("start") or 0),
                "end": float(d.get("end") or 0),
            })
        return ToolReceipt.applied(
            "list_clips_tool",
            f"Timeline clips ({len(clips)}).",
            undo_steps=0,
            data={"clips": structured, "legacy_text": f"Timeline clips ({len(clips)}):\n" + "\n".join(lines)},
        ).to_json()
    except Exception as e:
        return f"Error: {e}"


def list_layers(**_kw) -> str:
    try:
        from classes.track_display import build_track_stack, track_stack_json

        layers = _get_app().project.get("layers") or []
        if not layers:
            return "No layers in project."
        stack = build_track_stack(layers)
        lock_by_num = {
            int(L.get("number") or 0): bool(L.get("lock", False)) for L in layers
        }
        n = len(stack)
        bottom = stack[0] if stack else {}
        top = stack[-1] if stack else {}
        lines = [
            f"Layers ({n}). Z-ORDER uses layer_number only (higher covers lower). "
            "Track labels/names are cosmetic — they can be anything and do NOT imply priority.",
            f"BOTTOM (drawn under): layer_number={bottom.get('layer_number')} "
            f"label={bottom.get('label')!r}",
            f"TOP (covers all below): layer_number={top.get('layer_number')} "
            f"label={top.get('label')!r}",
            "Stack bottom→top:",
        ]
        for e in stack:
            lines.append(
                f"  layer_number={e['layer_number']} ui_track={e['ui_track']} "
                f"z_from_bottom={e['z_from_bottom']} label={e.get('label')!r} "
                f"track_id={e.get('track_id')!r} "
                f"lock={lock_by_num.get(e['layer_number'], False)}"
            )
        lines.append(f"TRACK_STACK_JSON={track_stack_json(layers)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Playback & history
# ---------------------------------------------------------------------------

_WATCH_CLIP_DEFAULT_PATH = os.path.expanduser(
    "~/Downloads/Feral - Concept Trailer.mp4"
)


def watch_clip_and_play(file_path: str = "", **_kw) -> str:
    """Import a video file into the media bin, add it to the timeline, and start playback.

    If file_path is empty, defaults to the Feral concept trailer test video.
    This is the handler for natural-language commands like 'watch clip',
    'play the feral trailer', 'show me the clip', etc.
    """
    try:
        resolved_path = (file_path or _WATCH_CLIP_DEFAULT_PATH).strip()
        if not os.path.isfile(resolved_path):
            return f"Error: File not found: {resolved_path}"

        from classes.query import File as _File
        from qt_api import QUrl as _QUrl

        app = _get_app()
        win = app.window

        # Step 1: Import into media bin (must run on main thread — uses libopenshot)
        def _do_import():
            existing = _File.get(path=resolved_path)
            if existing:
                return existing.id
            win.files_model.add_files([resolved_path], quiet=True, prevent_image_seq=True)
            added = _File.get(path=resolved_path)
            return added.id if added else None

        file_id = _run_on_main_thread(_do_import)
        if not file_id:
            return f"Error: Could not import file into media bin: {resolved_path}"

        # Step 2: Add to timeline (position 0, top video track)
        add_result = add_clip_to_timeline(file_id=file_id, position_seconds="0", **_kw)
        if add_result.startswith("Error"):
            return add_result

        # Step 3: Seek to start + play
        def _do_play():
            win.actionJumpStart_trigger()
            # Ensure player is playing (actionPlay_trigger toggles, so check mode)
            try:
                import openshot
                player = win.preview_thread.player
                if player.Mode() != openshot.PLAYBACK_PLAY:
                    win.actionPlay_trigger()
            except Exception:
                win.actionPlay_trigger()

        _run_on_main_thread(_do_play)
        return f"Loaded and playing: {os.path.basename(resolved_path)}"
    except Exception as e:
        log.error("watch_clip_and_play failed: %s", e, exc_info=True)
        return f"Error: {e}"


def go_to_start(**_kw) -> str:
    try:
        _get_app().window.actionJumpStart_trigger()
        return "Seeked to start."
    except Exception as e:
        return f"Error: {e}"


def go_to_end(**_kw) -> str:
    try:
        _get_app().window.actionJumpEnd_trigger()
        return "Seeked to end."
    except Exception as e:
        return f"Error: {e}"


def _undo_redo(app, direction, steps) -> str:
    """Apply *steps* sequential undo/redo operations and report what happened.

    Runs entirely on the Qt main thread (one hop), because UpdateManager.undo()
    touches window selections and calls processEvents().

    UpdateManager.undo()/redo() return None and silently no-op on an empty
    stack, so "did that step do anything?" is answered by watching the stack
    length rather than by changing the UpdateManager contract.
    """
    undoing = direction == "undo"
    label = "undo" if undoing else "redo"
    stack = app.updates.actionHistory if undoing else app.updates.redoHistory
    apply_one = app.updates.undo if undoing else app.updates.redo

    if not stack:
        return f"Error: nothing to {label}."

    signature_before = _timeline_signature(app)

    done = 0
    described = None
    touched_clips = False
    for _ in range(steps):
        if not stack:
            break
        before = len(stack)
        tail_tid = stack[-1].transaction
        group = [a for a in stack if a.transaction == tail_tid]
        if described is None:
            described = _describe_group(group)
        # Only a group that edits clips is expected to move the timeline;
        # undoing a marker, export setting or track rename legitimately
        # leaves the clip signature identical.
        for action in group:
            key = getattr(action, "key", None)
            if isinstance(key, (list, tuple)) and key and key[0] == "clips":
                touched_clips = True
                break
        apply_one()
        if len(stack) >= before:
            # Nothing moved — stop rather than spin.
            break
        done += 1

    # Update the preview exactly like main_window.actionUndo_trigger does.
    # Emitted once at the end: the final frame is the same, and N repaints
    # would eat into the blocking main-thread budget in _run_on_main_thread.
    try:
        app.window.refreshFrameSignal.emit()
    except Exception:
        pass

    if done == 0:
        return f"Error: nothing to {label}."

    # Did the project actually change?  A history step can pop cleanly and
    # still leave the thing the user pointed at on the timeline -- that is
    # exactly the failure this now reports instead of hiding.
    delta = _describe_timeline_delta(signature_before, _timeline_signature(app))
    # signature_before being empty means there was nothing on the timeline to
    # change, so an unchanged signature proves nothing -- fall through to the
    # history-based report rather than claiming a failure.
    if delta == "" and touched_clips and signature_before:
        return (
            f"Error: {label} applied {done} history step(s) but the timeline "
            f"did not change. The edit you meant may span several steps -- "
            f"check list_clips_tool, then {label} again with steps=N, or "
            f"delete the clip directly."
        )

    verb = "Undid" if undoing else "Redid"
    noun = "action" if done == 1 else "actions"
    # Only describe the group when there was exactly one — naming the first of
    # several would read as if every step had been that kind of change.
    # Prefer what actually changed on the timeline over the history-action
    # summary; fall back to the summary when no signature was available.
    if delta:
        detail = f": {delta}"
    elif described and done == 1:
        detail = f" ({described})"
    else:
        detail = ""

    if done < steps:
        return (
            f"{verb} {done} of {steps} requested{detail}; "
            f"nothing left to {label}."
        )

    remaining = len({a.transaction for a in stack})
    if remaining == 1:
        tail = f" 1 {label} step remains."
    elif remaining:
        tail = f" {remaining} {label} steps remain."
    else:
        tail = f" Nothing left to {label}."
    return f"{verb} {done} {noun}{detail}.{tail}"


def undo(steps=1, **_kw) -> str:
    try:
        app = _get_app()
        return _run_on_main_thread(_undo_redo, app, "undo", _coerce_steps(steps))
    except Exception as e:
        return f"Error: {e}"


def redo(steps=1, **_kw) -> str:
    try:
        app = _get_app()
        return _run_on_main_thread(_undo_redo, app, "redo", _coerce_steps(steps))
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Timeline / view
# ---------------------------------------------------------------------------

def _locked_track_error(app, layer_num):
    """Return an error string if *layer_num* is a locked track, else ''."""
    project = getattr(app, "project", None)
    get = getattr(project, "get", None)
    if callable(get):
        layers_out = get("layers") or []
    elif isinstance(project, dict):
        layers_out = project.get("layers") or []
    else:
        layers_out = []
    track_lbl = format_track_label_for_llm(layer_num, layers_out)
    for L in layers_out:
        try:
            if int(L.get("number") or 0) == layer_num and bool(L.get("lock", False)):
                return f"Error: Track {track_lbl} is locked."
        except Exception:
            continue
    return ""


def _delete_one_clip(app, resolved, ripple=False) -> str:
    """Delete a single resolved timeline clip. The caller owns the transaction.

    With *ripple*, later clips on the same track move left to close the gap,
    inside the same undo step (the Shift+Delete rule, timeline_ops.close_gap_at).
    """
    win = app.window
    clip_obj = resolved.clip
    clip_id = str(getattr(clip_obj, "id", "") or "")
    clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
    try:
        layer_num = int(clip_data.get("layer") or 0)
    except (TypeError, ValueError):
        layer_num = 0
    try:
        position = float(clip_data.get("position", 0.0) or 0.0)
    except (TypeError, ValueError):
        position = 0.0
    title = str(clip_data.get("title") or clip_data.get("label") or "clip")
    try:
        length = max(0.0, float(clip_data.get("end", 0.0) or 0.0) - float(clip_data.get("start", 0.0) or 0.0))
    except (TypeError, ValueError):
        length = 0.0
    moved = []

    locked = _locked_track_error(app, layer_num)
    if locked:
        return locked

    track_lbl = format_track_label_for_llm(layer_num, app.project.get("layers") or [])

    def _do_delete():
        # Join execute_tool's transaction when present; otherwise mint one so
        # direct callers (remove_clip alias / unit tests) still get a single
        # undo step and a non-None transaction_id during delete.
        with _transaction(app):
            try:
                if hasattr(win, "removeSelection"):
                    win.removeSelection(clip_id, "clip")
            except Exception:
                pass
            clip_obj.delete()
            if ripple:
                from classes.timeline_ops import close_gap_at
                moved.extend(close_gap_at(layer_num, position, length))

            # A deleted clip may still be referenced by the preview widget's
            # transform state; clear it before the next paint dereferences a freed
            # native object (see main_window.actionRemoveClip_trigger).
            try:
                win.videoPreview.clearTransformState()
            except Exception:
                pass
            try:
                win.refreshFrameSignal.emit()
            except Exception:
                pass

    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_do_delete)
    else:
        _do_delete()

    if ripple:
        return (
            f"Deleted timeline clip {clip_id} ({title!r}) from track {track_lbl} "
            f"at {position:.2f}s and closed the gap: {len(moved)} later item(s) on "
            f"that track moved left. 1 undo step."
        )
    return (
        f"Deleted timeline clip {clip_id} ({title!r}) from track {track_lbl} "
        f"at {position:.2f}s. Other clips on that track are unchanged "
        f"(gap left, no ripple). 1 undo step."
    )


def _delete_whole_track(app, track, include_transitions) -> str:
    """Delete every clip (and optionally transition) on one track."""
    from classes.query import Clip, Transition

    win = app.window
    layers = app.project.get("layers") or []

    layer_num, err = normalize_track_or_layer_arg(str(track).strip(), layers)
    if err:
        return err
    if layer_num is None:
        return "Error: Unknown track or layer."
    layer_num = int(layer_num)

    locked = _locked_track_error(app, layer_num)
    if locked:
        return locked

    track_lbl = format_track_label_for_llm(layer_num, app.project.get("layers") or [])

    # Avoid stale selections pointing at soon-to-be-deleted objects.
    if hasattr(win, "clearSelections"):
        win.clearSelections()

    clips = Clip.filter(layer=layer_num)
    transitions = Transition.filter(layer=layer_num) if include_transitions else []

    def _do_delete_track():
        with _transaction(app):
            # Delete transitions first (they may reference clip time ranges).
            for t in transitions:
                try:
                    if hasattr(win, "removeSelection"):
                        win.removeSelection(t.id, "transition")
                except Exception:
                    pass
                t.delete()

            for c in clips:
                try:
                    if hasattr(win, "removeSelection"):
                        win.removeSelection(c.id, "clip")
                except Exception:
                    pass
                c.delete()

            # Refresh preview frame to reflect the new timeline immediately.
            try:
                win.refreshFrameSignal.emit()
            except Exception:
                pass

    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_do_delete_track)
    else:
        _do_delete_track()

    return (
        f"Deleted {len(clips)} clips and {len(transitions)} transitions on "
        f"track {track_lbl}. 1 undo step."
    )


def delete_from_timeline(
    timeline_clip_id: str = "",
    clip_query: str = "",
    track: str = "",
    scope: str = "auto",
    occurrence: str = "0",
    position_near=None,
    include_transitions: bool = True,
    ripple: bool = False,
    **_kw,
) -> str:
    """Delete from the timeline: one clip placement, or an entire track.

    This is the ONLY timeline delete tool. Target it one of three ways:
      * timeline_clip_id - an id from list_clips_tool or the timeline snapshot
      * clip_query       - a description, narrowed with track / occurrence
                           (1-based) / position_near (timeline seconds)
      * track            - clear that whole track (UI "Track 1".."N", bottom=1,
                           or a storage layer_number)

    scope is normally "auto": a clip id or query deletes ONE placement, a bare
    track clears the track. Pass scope="clip" or scope="track" to force the
    branch. include_transitions also removes transitions sitting on the track
    (track scope only). Deleting leaves a gap unless ripple=true, which closes
    it: later clips on the same track move left (one clip only).

    The whole call is a single undo step, whether it removes one clip or fifty.
    """
    try:
        app = _get_app()

        has_clip_target = bool(
            str(timeline_clip_id or "").strip() or str(clip_query or "").strip()
        )
        has_track = bool(str(track or "").strip())

        mode = str(scope or "auto").strip().lower()
        if mode not in ("auto", "clip", "track"):
            return f"Error: scope must be 'auto', 'clip' or 'track' (got {scope!r})."
        if mode == "auto":
            # A clip target wins over a bare track: `track` doubles as a
            # disambiguator for clip_query ("the b-roll on track 3"), and
            # deleting one clip is the recoverable reading if the caller
            # actually meant to clear the track. The result string names what
            # was deleted, so a wrong guess is visible immediately.
            mode = "clip" if has_clip_target else ("track" if has_track else "")

        if not mode:
            return (
                "Error: delete_from_timeline_tool needs a target. Pass "
                "timeline_clip_id (from list_clips_tool) or clip_query to "
                "delete one placement, or track to clear a whole track."
            )

        if mode == "track":
            if not has_track:
                return "Error: scope='track' needs track."
            if has_clip_target:
                # Refuse the destructive reading of a contradictory call: the
                # caller named ONE clip and also asked to clear the track.
                # Silently clearing would delete everything on it.
                return (
                    "Error: scope='track' clears the whole track, but a single "
                    "clip was also named (timeline_clip_id/clip_query). Drop the "
                    "clip target to clear the track, or use scope='clip' to "
                    "delete just that clip."
                )
            return _delete_whole_track(app, track, include_transitions)

        # Targeting is mandatory: the agent does not own UI selection, and the
        # resolver's playhead / single-clip shortcuts must never be reachable
        # from an argless call (that is the unsafe path this tool replaced).
        if not has_clip_target:
            return (
                "Error: scope='clip' needs timeline_clip_id or clip_query. Use "
                "list_clips_tool to get a timeline_clip_id, or pass clip_query "
                "with track/occurrence/position_near."
            )

        resolved = _resolve_timeline_clip_for_tool(
            timeline_clip_id=timeline_clip_id,
            clip_query=clip_query,
            track=track,
            occurrence=occurrence,
            position_near=position_near,
        )
        if not resolved.ok or not resolved.clip:
            # Includes the candidate list for ambiguous queries. Never widen to
            # a track-wide delete.
            return resolved.error or "Error: Could not resolve timeline clip."

        wants_ripple = ripple is True or str(ripple).strip().lower() in ("1", "true", "yes", "on")
        return _delete_one_clip(app, resolved, ripple=wants_ripple)
    except Exception as e:
        return f"Error: {e}"


def remove_clip(
    timeline_clip_id: str = "",
    clip_query: str = "",
    track: str = "",
    occurrence: str = "0",
    position_near=None,
    **_kw,
) -> str:
    """Deprecated alias for delete_from_timeline (scope="clip").

    Kept so stored plans and in-flight sessions that still name
    remove_clip_tool keep working. The agent catalog exposes only
    delete_from_timeline_tool.
    """
    return delete_from_timeline(
        timeline_clip_id=timeline_clip_id,
        clip_query=clip_query,
        track=track,
        scope="clip",
        occurrence=occurrence,
        position_near=position_near,
    )


def delete_clips_on_track(track: str = "", include_transitions: bool = True, **_kw) -> str:
    """Deprecated alias for delete_from_timeline (scope="track")."""
    if track is None or (isinstance(track, str) and not track.strip()):
        return "Error: track is required."
    return delete_from_timeline(
        track=track, scope="track", include_transitions=include_transitions
    )


def zoom_in(**_kw) -> str:
    try:
        _get_app().window.actionTimelineZoomIn_trigger()
        return "Timeline zoomed in."
    except Exception as e:
        return f"Error: {e}"


def zoom_out(**_kw) -> str:
    try:
        _get_app().window.actionTimelineZoomOut_trigger()
        return "Timeline zoomed out."
    except Exception as e:
        return f"Error: {e}"


def center_on_playhead(**_kw) -> str:
    try:
        _get_app().window.actionCenterOnPlayhead_trigger()
        return "Centered on playhead."
    except Exception as e:
        return f"Error: {e}"


# Extensions collected when a directory is imported. Explicit file paths are
# passed through unfiltered — libopenshot decides whether it can read them.
_IMPORT_VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv",
})
_IMPORT_AUDIO_EXTS = frozenset({
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
})
_IMPORT_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
})
_IMPORT_MEDIA_EXTS = _IMPORT_VIDEO_EXTS | _IMPORT_AUDIO_EXTS | _IMPORT_IMAGE_EXTS

# Cap tool responses so a large folder does not flood the model context.
_IMPORT_RESULT_LINE_CAP = 25

# A whole folder of media can take minutes to probe; the default 30s budget is
# for small interactive edits, not a bulk import.
_IMPORT_MAIN_THREAD_TIMEOUT = 900


def _coerce_path_list(paths) -> list:
    """Accept a list, a JSON array, or a comma/newline-separated string."""
    if paths is None:
        return []
    if isinstance(paths, (list, tuple)):
        items = list(paths)
    else:
        text = str(paths).strip()
        if not text:
            return []
        items = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = None
        if items is None:
            items = re.split(r"[,\n]", text) if ("," in text or "\n" in text) else [text]
    return [str(p).strip().strip('"').strip("'") for p in items if str(p).strip()]


def _import_media_kind(path: str) -> str:
    """Classify a path by extension for dry-run counts (video/audio/image/other)."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _IMPORT_VIDEO_EXTS:
        return "video"
    if ext in _IMPORT_AUDIO_EXTS:
        return "audio"
    if ext in _IMPORT_IMAGE_EXTS:
        return "image"
    return "other"


def _format_capped_lines(lines, cap=_IMPORT_RESULT_LINE_CAP) -> str:
    """Join lines, truncating after *cap* with a remainder note."""
    if not lines:
        return ""
    if len(lines) <= cap:
        return "\n".join(lines)
    rest = len(lines) - cap
    return (
        "\n".join(lines[:cap])
        + "\n... and %d more. Use list_files_tool to see the rest." % rest
    )


def _import_truthy(value, default=False) -> bool:
    """Accept bools (backend) and common string forms (MCP / Claude Code)."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "y", "on")


def _tool_flag(*values, default=False) -> bool:
    """First explicitly provided bool/string wins; empty values are skipped.

    Defaults like dry_run="false" must not shadow camelCase aliases (dryRun).
    Pass aliases before snake_case defaults, or use empty-string defaults.
    """
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
    return default


def _allowed_exts_for_media_types(media_types) -> frozenset:
    """Extension set for dir/glob filtering. Default = all editor media."""
    text = str(media_types if media_types is not None else "all").strip().lower()
    if not text or text in ("all", "*", "any", "media"):
        return _IMPORT_MEDIA_EXTS
    kinds = {p.strip() for p in re.split(r"[,|\s]+", text) if p.strip()}
    exts: set[str] = set()
    if kinds & {"video", "videos"}:
        exts |= _IMPORT_VIDEO_EXTS
    if kinds & {"audio", "audios", "sound", "music"}:
        exts |= _IMPORT_AUDIO_EXTS
    if kinds & {"image", "images", "photo", "photos", "picture", "pictures"}:
        exts |= _IMPORT_IMAGE_EXTS
    return frozenset(exts) if exts else _IMPORT_MEDIA_EXTS


def _expand_import_paths(entries, allowed_exts=None) -> tuple:
    """Return (media_paths, missing, skipped_non_media).

    Directories are walked for allowed media extensions only. Explicit file
    paths are kept unfiltered. *skipped_non_media* counts files skipped during
    dir walks (wrong type or non-media).
    """
    if allowed_exts is None:
        allowed_exts = _IMPORT_MEDIA_EXTS
    resolved, missing, seen = [], [], set()
    skipped_non_media = 0
    for entry in entries:
        path = os.path.abspath(os.path.expanduser(str(entry))) if entry else ""
        if path and os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if os.path.splitext(name)[1].lower() in allowed_exts:
                        if full not in seen:
                            seen.add(full)
                            resolved.append(full)
                    else:
                        skipped_non_media += 1
        elif path and os.path.isfile(path):
            if path not in seen:
                seen.add(path)
                resolved.append(path)
        else:
            missing.append(str(entry))
    return resolved, missing, skipped_non_media


def import_files(
    paths="",
    path="",
    folder="",
    skip_indexing="false",
    dry_run="false",
    media_types="all",
    files="",
    **_kw
) -> str:
    """Import local media by path into Project Files — never opens a file dialog; use dry_run=true to preview.

    Call with the user's path immediately (dry_run=true for folders). Bare names
    like folder=Downloads or Desktop work. For “all videos”, pass
    media_types=video. Do not preflight with Glob/Read or invent /mnt/c mounts —
    this tool resolves Windows C:/… and Git Bash /c/… paths. Exact match first;
    slight typos may resolve adjacently (ask if several). Prefer forward-slash
    Windows paths so JSON backslashes cannot mangle them. Never ask for
    individual file paths when the user named a folder.
    """
    import glob as _glob
    from classes.file_drop import (
        is_user_home_directory,
        normalize_agent_fs_path,
        resolve_agent_import_target,
    )

    entries = []
    for value in (paths, path, folder, files):
        if value:
            entries.extend(_coerce_path_list(value))
    if not entries:
        return ("Error: paths is required for MCP/harness import. Pass the media "
                "files or folders to import, e.g. folder=\"Downloads\", "
                "paths=[\"C:/Users/you/Downloads/clips\"], or "
                "paths=[\"~/Desktop/clips\"]. This tool never opens a file dialog.")

    notes = []
    normalized = []
    adjacent_notes = []
    allowed_exts = _allowed_exts_for_media_types(media_types)
    glob_skipped = 0
    for entry in entries:
        candidate = normalize_agent_fs_path(entry)
        if _glob.has_magic(candidate) or _glob.has_magic(str(entry)):
            matches = _glob.glob(candidate, recursive=True)
            if not matches:
                matches = _glob.glob(os.path.expanduser(str(entry)), recursive=True)
            if not matches:
                notes.append("No files matched: %s" % entry)
                continue
            # A glob is a folder listing, not a list of named files: filter it
            # by media_types like a directory walk.
            for match in matches:
                if os.path.isdir(match) or os.path.splitext(match)[1].lower() in allowed_exts:
                    normalized.append(match)
                else:
                    glob_skipped += 1
            continue

        target = resolve_agent_import_target(entry)
        if target.get("status") == "ambiguous":
            cands = target.get("candidates") or []
            if target.get("elsewhere"):
                head = ("Error: %r does not exist. A file with a similar name is in "
                        "another folder — ask the user whether it is the one:" % entry)
            else:
                head = "Error: Multiple paths match %r — ask the user which one:" % entry
            lines = [head]
            for cand in cands:
                lines.append("  %s" % cand)
            lines.append(
                "Call import_files_tool again with the exact path. Do not guess."
            )
            return "\n".join(lines)
        if target.get("status") == "ok":
            resolved_path = target["path"]
            normalized.append(resolved_path)
            if target.get("match") == "adjacent":
                adjacent_notes.append(
                    "adjacent: %r → %s" % (entry, resolved_path)
                )
            continue

        notes.append(
            "Not found: %s (tried %s; no adjacent match under parent or "
            "Desktop/Downloads/Movies/Videos/Documents/Pictures). Ask the "
            "user for the full path, or Glob those folders then call "
            "import_files_tool with the path found. Do not invent /mnt/c "
            "mounts."
            % (entry, target.get("tried") or entry)
        )

    home_hits = [p for p in normalized if is_user_home_directory(p)]
    if home_hits:
        return (
            "Error: Refusing to import the entire home folder. Pass a specific "
            "subfolder such as Desktop, Downloads, Movies, Videos, Documents, "
            "or Pictures (e.g. folder=\"Downloads\")."
        )

    resolved, missing, skipped_non_media = _expand_import_paths(
        normalized, allowed_exts=allowed_exts,
    )
    skipped_non_media += glob_skipped
    if not resolved:
        detail = "; ".join(notes) if notes else (
            "no media files found in: %s" % ", ".join(entries)
        )
        if missing and not notes:
            detail = "not found: %s" % ", ".join(missing)
        return f"Error: Nothing to import ({detail})."

    preview = _import_truthy(dry_run, default=False)
    if preview:
        counts = {"video": 0, "audio": 0, "image": 0, "other": 0}
        for media_path in resolved:
            counts[_import_media_kind(media_path)] += 1
        roots = []
        for item in normalized:
            abs_item = os.path.abspath(os.path.expanduser(item))
            if os.path.exists(abs_item) and abs_item not in roots:
                roots.append(abs_item)
        sample = [os.path.basename(p) for p in resolved]
        lines = [
            "dry_run=true — nothing imported.",
            "resolved=%s" % (", ".join(roots) if roots else ", ".join(entries)),
        ]
        if adjacent_notes:
            lines.append("match=adjacent")
            lines.extend(["  %s" % note for note in adjacent_notes])
        lines.append(
            "would_import=%d (video=%d audio=%d image=%d)" % (
                len(resolved), counts["video"], counts["audio"], counts["image"],
            )
        )
        mt = str(media_types or "all").strip() or "all"
        if mt.lower() not in ("all", "*", "any", "media"):
            lines.append("media_types=%s" % mt)
        lines.append("sample:")
        sample_body = _format_capped_lines(
            ["  %s" % name for name in sample], cap=_IMPORT_RESULT_LINE_CAP,
        )
        if sample_body:
            lines.append(sample_body)
        lines.append("skipped_non_media=%d" % skipped_non_media)
        if missing:
            lines.append("not found: %s" % ", ".join(missing))
        if notes:
            lines.append("Notes: " + "; ".join(notes))
        lines.append(
            "Ask the user to confirm, then call again with dry_run=false."
        )
        return "\n".join(lines)

    skip = _import_truthy(skip_indexing, default=False)

    try:
        from classes.query import File as _File

        def _do_add():
            return _get_app().window.files_model.add_files(
                resolved, quiet=True, prevent_image_seq=True, skip_indexing=skip,
            )

        added = _run_on_main_thread(_do_add, timeout=_IMPORT_MAIN_THREAD_TIMEOUT)

        by_path = {}
        if isinstance(added, (list, tuple)):
            for f in added:
                d = getattr(f, "data", None)
                if isinstance(d, dict) and d.get("path"):
                    by_path[os.path.abspath(str(d["path"]))] = f

        if isinstance(added, (list, tuple)) and len(added) == 0:
            detail = "; ".join(notes) if notes else "the files could not be opened"
            return f"Error: Nothing was added to the media bin ({detail})."

        lines = []
        ids = []
        for media_path in resolved:
            key = os.path.abspath(media_path)
            f = by_path.get(key) or _File.get(path=media_path)
            if not f and key != media_path:
                f = _File.get(path=key)
            if not f:
                continue
            fid = getattr(f, "id", "?")
            lines.append("file_id=%s path=%s" % (fid, media_path))
            if fid and fid != "?":
                ids.append(fid)

        if not lines:
            detail = "; ".join(notes) if notes else "the files could not be opened"
            return f"Error: Nothing was added to the media bin ({detail})."

        chat_session_id = str(_kw.get("chat_session_id", "") or "default")
        if ids:
            _last_split_file_id_by_chat_session[chat_session_id] = ids[-1]
    except Exception as e:
        return f"Error: {e}"

    head = "Imported %d file(s). indexing_started=%s" % (
        len(lines), "false" if skip else "true")
    if adjacent_notes:
        head += "\n" + "\n".join(adjacent_notes)
    if skipped_non_media:
        head += " skipped_non_media=%d" % skipped_non_media
    if missing:
        head += " (not found: %s)" % ", ".join(missing)
    if notes:
        head += "\nNotes: " + "; ".join(notes)
    return head + "\n" + _format_capped_lines(lines)



def wait_until_project_indexed(timeout_seconds=1800, **_kw) -> str:
    """Block until every media file in the project has finished indexing.

    Returns once all files report analyzed, or lists the file ids still pending
    when ``timeout_seconds`` runs out. Use this after import_files_tool and
    before prompting the assistant, so it plans against indexed footage.
    """
    import time

    try:
        budget = max(30, int(float(timeout_seconds)))
    except Exception:
        budget = 1800

    try:
        from classes.query import File as _File

        files_model = _get_app().window.files_model
        targets = [f for f in (_File.filter() or [])
                   if isinstance(getattr(f, "data", None), dict)
                   and not f.data.get("zenvi_subclip")]
        if not targets:
            return "No project files to index."

        deadline = time.time() + budget
        done, pending = [], []
        idle_grace = 10.0
        for f in targets:
            fid = str(getattr(f, "id", "") or f.data.get("id") or "")
            remaining = int(max(1, deadline - time.time()))
            err = _wait_for_file_indexing(fid, files_model, timeout_sec=remaining, idle_grace=idle_grace)
            if err:
                pending.append((fid, err))
                if err == _NOT_BEING_INDEXED:
                    idle_grace = 1.0  # the queue had its chance; don't wait 10 s per file
            else:
                done.append(fid)
    except Exception as e:
        return f"Error: {e}"

    if not pending:
        return "All %d project file(s) indexed." % len(done)
    if all(err == _NOT_BEING_INDEXED for _fid, err in pending):
        return (
            "Error: %d of %d project file(s) are not indexed and nothing is indexing them (their import "
            "skipped indexing, or indexing is off), so there is nothing to wait for. Not indexed: %s. "
            "reindex_project_file_tool indexes one; captions can also come from an .srt or cues." % (
                len(pending), len(targets), ", ".join(fid for fid, _err in pending))
        )
    # Not an answer the caller can plan on: say which files and why.
    return (
        "Error: indexing did not finish for %d of %d project file(s) within %ss "
        "(%d indexed). Pending: %s. Wait again, or call reindex_project_file_tool "
        "for files that failed." % (
            len(pending), len(targets), budget, len(done),
            "; ".join("%s (%s)" % (fid, err) for fid, err in pending))
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

# A full render runs on the GUI thread and easily outlives the 30s budget meant
# for small interactive edits. Chat/agent exports of a real project can take
# hours; a short wait reports "Export failed" while encoding continues.
_EXPORT_MAIN_THREAD_TIMEOUT = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Clipping (split, slice, add to timeline)
# ---------------------------------------------------------------------------

def get_file_info(file_id="", **_kw) -> str:
    try:
        from classes.query import File
        if not file_id:
            return "Error: file_id is required."
        f = File.get(id=file_id.strip())
        if not f:
            return f"Error: File not found for id={file_id}."
        return _describe_file_for_llm(file_id, f.data)
    except Exception as e:
        return f"Error: {e}"


def _coerce_time_arg(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _positive_int_arg(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        n = int(float(s))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def split_file_add_clip(
    file_id="",
    start_frame=0,
    end_frame=0,
    name="",
    start_seconds="",
    end_seconds="",
    query="",
    **_kw,
) -> str:
    try:
        from classes.query import File
        from classes import time_parts
        from classes.ai_metadata_utils import get_effective_ai_metadata, filter_tags_string_for_window

        chat_session_id = str(_kw.get("chat_session_id", "") or "default")
        query = str(query or _kw.get("query") or "").strip()

        if not file_id:
            return "Error: file_id is required."
        file_id = str(file_id).strip()
        f = File.get(id=file_id)
        if not f:
            return f"Error: File not found for id={file_id}."
        fps_data = f.data.get("fps") or {}
        fps_num = int(fps_data.get("num", 30))
        fps_den = int(fps_data.get("den", 1))
        fps = float(fps_num) / float(fps_den) if fps_den else 0.0
        if fps <= 0:
            return "Error: Invalid fps."

        src_start, src_end = source_window_for_file(f.data)
        t0 = _coerce_time_arg(start_seconds if str(start_seconds or "").strip() else _kw.get("start_seconds"))
        t1 = _coerce_time_arg(end_seconds if str(end_seconds or "").strip() else _kw.get("end_seconds"))
        sf = _positive_int_arg(start_frame if start_frame not in (None, "", 0, "0") else _kw.get("start_frame"))
        ef = _positive_int_arg(end_frame if end_frame not in (None, "", 0, "0") else _kw.get("end_frame"))
        if t0 is None or t1 is None:
            if sf is None or ef is None:
                return (
                    "Error: Provide start_seconds+end_seconds (preferred) or "
                    "start_frame+end_frame (1-based)."
                )
            t0 = src_start + (sf - 1) / fps
            t1 = src_start + ef / fps
            log.warning(
                "split_file_add_clip used 1-based frames file=%s start_frame=%s end_frame=%s",
                file_id, sf, ef,
            )
        if t1 < t0:
            t0, t1 = t1, t0
        t0 = max(src_start, min(float(t0), src_end))
        t1 = max(src_start, min(float(t1), src_end))
        if t1 <= t0 + 1e-3:
            return (
                f"Error: split window is empty after clamping to the file "
                f"[{src_start:.2f}s–{src_end:.2f}s]."
            )

        skip_watch = _parse_explicit_source_time_range_sec(query) is not None
        watched_note = ""
        watch_record = {}
        orig_span = float(t1) - float(t0)
        require_visual = bool(_kw.get("require_visual_match")) or orig_span > MAX_PLACE_SPAN_SEC
        if not skip_watch and not _window_is_dialogue_driven(f.data, t0, t1):
            path, dur, cues = _lookup_watch_meta(file_id, file_data=f.data)
            if not path:
                try:
                    path = f.absolute_path() if hasattr(f, "absolute_path") else ""
                except Exception:
                    path = str(f.data.get("path") or "")
            watched = _watch_confirm_cut(
                path, t0, t1, query or name or "the matching action in this window",
                fallback=(t0 + t1) / 2.0,
                duration=dur,
                transcript_cues=cues,
                fallback_in=t0,
                fallback_out=t1,
            )
            in_s = float(watched.get("in_source") if watched.get("in_source") is not None else t0)
            out_s = float(watched.get("out_source") if watched.get("out_source") is not None else t1)
            in_s = max(src_start, min(in_s, src_end))
            out_s = max(src_start, min(out_s, src_end))
            if out_s > in_s + 1e-3:
                t0, t1 = in_s, out_s
            if watched.get("used_fallback"):
                if require_visual:
                    return (
                        "Error: no visual match in this chapter-level window "
                        f"[{orig_span:.1f}s]. Narrow the query or re-index the file "
                        "so action-bounded scenes exist; do not place the chapter start."
                    )
                watched_note = " (text-index window; no visual match)"
            elif watched.get("reason"):
                watched_note = f" (watched: {str(watched.get('reason'))[:120]})"
            log.info(
                "split_file_add_clip watch file=%s window=%.3f-%.3f in=%.3f out=%.3f matched=%s",
                file_id, float(watched.get("window_start") or t0),
                float(watched.get("window_end") or t1), t0, t1,
                watched.get("matched"),
            )
            watch_record = dict(watched)

        # Vision picks frames, not words: keep both edges off a mid-phrase cue.
        t0, t1, _snapped = _snap_window_off_boundaries(f.data, t0, t1)
        start_sec, end_sec = t0, t1
        result_box = [None]
        error_box = [None]

        def _do_save():
            try:
                new_file = File()
                new_file.data = copy.deepcopy(f.data)
                new_file.data.pop("name", None)
                new_file.id = None
                new_file.key = None
                new_file.type = "insert"
                q_start, q_end = quantize_placement_seconds(start_sec, end_sec)
                new_file.data["start"] = q_start
                new_file.data["end"] = q_end
                new_file.data["parent_file_id"] = file_id

                if "ai_metadata" in new_file.data and new_file.data["ai_metadata"].get("analyzed"):
                    from classes.timeline_clip_context import resolve_root_ai_metadata
                    from classes.ai_metadata_utils import materialize_clip_ai_metadata

                    root_ai, _ = resolve_root_ai_metadata(f.data, file_id=file_id)
                    if root_ai:
                        effective = materialize_clip_ai_metadata(
                            root_ai, q_start, q_end, rebased=True,
                        )
                    else:
                        effective = get_effective_ai_metadata(
                            f.data,
                            clip_data={"start": q_start, "end": q_end},
                            rebased=True,
                        )
                    new_file.data["ai_metadata"] = effective
                    if new_file.data.get("tags"):
                        new_file.data["tags"] = filter_tags_string_for_window(
                            str(new_file.data.get("tags") or ""),
                            effective,
                        )

                if name and isinstance(name, str) and name.strip():
                    new_file.data["name"] = name.strip()
                else:
                    t = time_parts.secondsToTime(start_sec, fps_num, fps_den)
                    timestamp = "{}:{}:{}:{}".format(t["hour"], t["min"], t["sec"], t["frame"])
                    base = os.path.splitext(os.path.basename(f.data.get("path") or f.data.get("name", "clip")))[0]
                    new_file.data["name"] = f"{base} ({timestamp})"
                new_file.data["zenvi_subclip"] = True
                if watch_record:
                    new_file.data["zenvi_watch_confirmed"] = not bool(watch_record.get("used_fallback"))
                    try:
                        new_file.data["zenvi_watch_confidence"] = float(watch_record.get("confidence") or 0)
                    except (TypeError, ValueError):
                        new_file.data["zenvi_watch_confidence"] = 0.0
                    new_file.data["zenvi_watch_sparse"] = bool(watch_record.get("sparse"))
                new_file.save()
                result_box[0] = (new_file.id, new_file.data.get("name", ""))
            except Exception as exc:
                error_box[0] = str(exc)

        _run_on_main_thread(_do_save)
        if error_box[0]:
            return f"Error: {error_box[0]}"
        if not result_box[0]:
            return "Error: Failed to save subclip."
        new_id, clip_name = result_box[0]
        _last_split_file_id_by_chat_session[chat_session_id] = new_id
        return (
            f'Subclip created: "{clip_name}" (file_id={new_id}) '
            f'from {_fmt_mmss(start_sec)}–{_fmt_mmss(end_sec)} source'
            f'{watched_note}. '
            f'Call add_clip_to_timeline_tool(file_id="{new_id}") to place it '
            f'(do not pass duration_seconds — this subclip is already trimmed).'
        )
    except Exception as e:
        return f"Error: {e}"


# Where a placement was planned to end, for each one whose length snapping or a
# watch changed: {timeline_clip_id: (position placed at, planned timeline end)}.
# A later position planned on that end is what butt_against_previous_clip fixes.
_planned_end_by_clip_id = {}


def _resized_placements_on_track(track_num) -> list:
    """(planned_end, actual_end) for clips on *track_num* this tool resized.

    A clip moved since it was placed is skipped: its plan no longer says where
    the next clip was meant to go. Must run on the main thread.
    """
    from classes.query import Clip

    out = []
    for c in Clip.filter():
        d = c.data if isinstance(getattr(c, "data", None), dict) else {}
        plan = _planned_end_by_clip_id.get(str(getattr(c, "id", "") or ""))
        if not plan or d.get("layer", 0) != track_num:
            continue
        placed_at, planned_end = plan
        try:
            pos = float(d.get("position", 0) or 0)
            actual_end = pos + float(d.get("end", 0) or 0) - float(d.get("start", 0) or 0)
        except (TypeError, ValueError):
            continue
        if abs(pos - placed_at) <= 1e-6:
            out.append((planned_end, actual_end))
    return out


def add_clip_to_timeline(
    file_id="",
    position_seconds="",
    track="",
    duration_seconds="",
    start_seconds="",
    end_seconds="",
    query="",
    full_file="",
    transaction_id=None,
    **_kw,
) -> str:
    """Place a clip on the timeline.

    *transaction_id* is internal: callers that ripple the timeline first pass
    the id they used for the ripple so the whole operation is ONE undo step.
    It is never supplied by the LLM.
    """
    try:
        from classes.query import File, Track, Clip

        chat_session_id = str(_kw.get("chat_session_id", "") or "default")
        query = str(query or _kw.get("query") or "").strip()

        if not file_id or (isinstance(file_id, str) and not file_id.strip()):
            file_id = _last_split_file_id_by_chat_session.get(chat_session_id)
            if not file_id:
                return (
                    "Error: No clip was just created. "
                    "Pass tool_args.file_id with a media_bin file id, or run "
                    "split_file_add_clip_tool / import_files_tool / import_stock_media_tool "
                    "immediately before this step (empty file_id only works right after those "
                    "tools in the same session)."
                )
        else:
            file_id = str(file_id).strip()
        f = File.get(id=file_id)
        if not f:
            return f"Error: File not found for id={file_id}."

        file_data = f.data
        _is_audio_only = is_audio_only_media(file_data)


        src_start, src_end = source_window_for_file(file_data)
        source_len = max(0.0, src_end - src_start)
        # Optional trim window (stock / beat placement)
        try:
            trim_start = max(0.0, parse_seconds_arg(start_seconds, default=0.0, field="start_seconds"))
            trim_dur = parse_seconds_arg(duration_seconds, default=None, field="duration_seconds")
            trim_end = parse_seconds_arg(end_seconds, default=None, field="end_seconds")
            pos_arg = parse_seconds_arg(position_seconds, default=None, field="position_seconds")
        except ValueError as exc:
            return f"Error: {exc}"
        # end_seconds is the keep-window form (place_moment); duration wins if both
        # disagree. Only the winner bounds the out-point - an overridden end_seconds
        # is not a keep window, it is a leftover arg.
        end_bounds_window = end_bounds_keep_window(trim_start, trim_dur, trim_end)
        if end_bounds_window:
            if trim_end <= trim_start:
                return (
                    f"Error: end_seconds {trim_end} must be greater than "
                    f"start_seconds {trim_start}."
                )
            trim_dur = trim_end - trim_start
        if trim_dur is not None:
            trim_dur = max(0.0, trim_dur)

        # A music bed stretched from 0 over the whole sequence is almost never what
        # was asked for. Make the agent name the section, or opt in explicitly.
        _whole_file = str(full_file or "").strip().lower() in ("1", "true", "yes")
        if _is_audio_only and not _whole_file and (pos_arg is None or trim_dur is None):
            return (
                "Error: audio placement needs position_seconds (where on the timeline) and "
                "duration_seconds (how much to use), plus start_seconds for the source in-point. "
                "Pass full_file=\"true\" only when the user asked for one continuous bed."
            )
        # start_seconds alone keeps the rest of the file from there. Without a
        # length nothing below trims, so a long file placed from 0 instead.
        if trim_dur is None and trim_start > 0 and not _whole_file:
            if trim_start >= source_len:
                return (
                    f"Error: start_seconds {trim_start:g} is past the end of this file "
                    f"({source_len:.2f}s long)."
                )
            trim_dur = source_len - trim_start

        watched_start = watched_end = None
        watched_info = {}
        skip_watch = _parse_explicit_source_time_range_sec(query) is not None
        _is_image = file_looks_like_image(file_data)
        watch_q = placement_watch_query(file_data, query)
        win_s = src_start + trim_start
        if trim_dur:
            win_e = min(src_end, win_s + max(float(trim_dur), 4.0))
        else:
            win_e = src_end
        # No window asked for - no trim at all, or full_file - means the whole
        # file. A watch refines a window the caller named; it must never invent
        # one (full_file="true" on a 24.6 s video placed the 1 s it matched).
        wants_window = bool(trim_dur) and not _whole_file
        if wants_window and should_watch_placement(
            is_audio=_is_audio_only,
            is_image=_is_image,
            skip_explicit_times=skip_watch,
            is_already_watched_subclip=bool(file_data.get("zenvi_subclip")),
            explicit_query=bool(query),
            window_sec=win_e - win_s,
        ) and not _window_is_dialogue_driven(file_data, win_s, win_e):
            path, dur, cues = _lookup_watch_meta(file_id, file_data=file_data)
            if not path:
                path = str(file_data.get("path") or "")
            watched = _watch_confirm_cut(
                path, win_s, win_e, watch_q,
                fallback=(win_s + win_e) / 2.0,
                duration=dur,
                transcript_cues=cues,
                fallback_in=win_s,
                fallback_out=min(src_end, win_s + (trim_dur or (win_e - win_s))),
            )
            in_s = float(watched.get("in_source") if watched.get("in_source") is not None else win_s)
            out_s = float(watched.get("out_source") if watched.get("out_source") is not None else win_e)
            in_s = max(src_start, min(in_s, src_end))
            out_s = max(src_start, min(out_s, src_end))
            if out_s <= in_s + 1e-3:
                in_s, out_s = win_s, min(src_end, win_e)
            if trim_dur and (out_s - in_s) > float(trim_dur) + 1e-6:
                peak = float(watched.get("cut_source") or ((in_s + out_s) / 2.0))
                peak = max(in_s, min(peak, out_s))
                in_s = max(src_start, peak - float(trim_dur) / 2.0)
                out_s = min(src_end, in_s + float(trim_dur))
                if out_s - in_s < float(trim_dur):
                    in_s = max(src_start, out_s - float(trim_dur))
            watched_start, watched_end = in_s, out_s
            watched_info = dict(watched)
            log.info(
                "add_clip_to_timeline watch file=%s in=%.3f out=%.3f matched=%s query=%r",
                file_id, in_s, out_s, watched.get("matched"), watch_q[:80],
            )

        if blind_trim_rejected(
            trim_dur=trim_dur,
            watched_start=watched_start,
            has_explicit_end=end_bounds_window,
            has_explicit_start=trim_start > 0,
            is_audio=_is_audio_only,
            is_image=_is_image,
            is_subclip=bool(file_data.get("zenvi_subclip")),
        ):
            # Never name a remedy the caller already applied - that turns a
            # recoverable refusal into a retry loop (#167).
            if trim_end is not None:
                return (
                    f"Error: duration_seconds={trim_dur:g} overrides end_seconds={trim_end:g}, so "
                    f"this would keep the first {trim_dur:g}s of a file nothing has looked at. "
                    "Drop duration_seconds and pass start_seconds (where the section begins) "
                    "with end_seconds."
                )
            return (
                "Error: duration_seconds alone cannot trim the first N seconds of a file "
                "nothing has looked at. Name both edges of the section you want: pass "
                "start_seconds and end_seconds (the keep window search_clips returned), "
                "or place_moment with that window."
                + (" Times written in query are not read as the window." if skip_watch else "")
            )

        result_box = [None]
        error_box = [None]

        def _do_add():
            try:
                app = _get_app()
                win = app.window
                fps = app.project.get("fps") or {}
                fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)

                if not track or (isinstance(track, str) and not track.strip()):
                    layers = app.project.get("layers") or []
                    if _is_audio_only:
                        track_num = default_underlay_layer_number(layers)
                    else:
                        selected = getattr(win, "selected_tracks", []) or []
                        if selected:
                            t = Track.get(id=selected[0])
                            track_num = int(t.data.get("number", 1)) if t else 1
                        else:
                            track_num = default_underlay_layer_number(layers)
                else:
                    layers_for_track = app.project.get("layers") or []
                    resolved, err = normalize_track_or_layer_arg(str(track).strip(), layers_for_track)
                    if err:
                        error_box[0] = err
                        return
                    track_num = resolved

                if pos_arg is None:
                    if _is_audio_only:
                        pos_sec = 0.0
                    else:
                        same_layer = [c for c in Clip.filter() if c.data.get("layer", 0) == track_num]
                        _one_frame = 1.0 / max(fps_float, 1.0)
                        if same_layer:
                            last_end = max(
                                c.data.get("position", 0) + (c.data.get("end", 0) - c.data.get("start", 0))
                                for c in same_layer
                            )
                            pos_sec = last_end + _one_frame
                        else:
                            pos_sec = 0.0
                elif _is_audio_only:
                    pos_sec = pos_arg
                else:
                    pos_sec = butt_against_previous_clip(
                        pos_arg, _resized_placements_on_track(track_num),
                    )

                if QPointF is None:
                    from qt_api import QPointF as _QPointF
                    pos = _QPointF(pos_sec, 0.0)
                else:
                    pos = QPointF(pos_sec, 0.0)

                snapped = False
                new_clip = win.timeline.addClip(file_id, pos, track_num)
                apply_trim =watched_start is not None or (trim_dur is not None and trim_dur > 0)
                if new_clip and apply_trim:
                    if watched_start is not None:
                        start_sec, end_sec = watched_start, watched_end
                    else:
                        start_sec, end_sec = compute_clip_trim_bounds(
                            source_len,
                            trim_start=trim_start,
                            trim_dur=trim_dur,
                            file_start=src_start,
                            min_duration=1.0 / max(fps_float, 1.0),
                        )
                    # Do not start or end a placement mid-phrase: pull both edges
                    # off any transcript cue or chapter they land inside.
                    start_sec, end_sec, snapped = _snap_window_off_boundaries(
                        file_data, start_sec, end_sec,
                    )
                    start_sec, end_sec = quantize_placement_seconds(start_sec, end_sec)
                    new_clip["start"] = start_sec
                    new_clip["end"] = end_sec
                    new_clip["duration"] = max(0.0, end_sec - start_sec)
                    if watched_info:
                        new_clip["zenvi_watch_confirmed"] = not bool(watched_info.get("used_fallback"))
                        try:
                            new_clip["zenvi_watch_confidence"] = float(watched_info.get("confidence") or 0)
                        except (TypeError, ValueError):
                            new_clip["zenvi_watch_confidence"] = 0.0
                    win.timeline.update_clip_data(
                        new_clip, only_basic_props=False, ignore_refresh=False
                    )
                new_id = str((new_clip or {}).get("id") or "")
                if new_id:
                    # The caller planned this clip to end at its own position plus
                    # the length it asked for; snapping, a watch or butting can
                    # move the real end, and the next planned position with it.
                    planned_end = (pos_sec if pos_arg is None else pos_arg) + (trim_dur or source_len)
                    try:
                        actual_end = pos_sec + float(new_clip.get("end", 0)) - float(new_clip.get("start", 0))
                    except (TypeError, ValueError):
                        actual_end = planned_end
                    if abs(actual_end - planned_end) > 1e-6:
                        _planned_end_by_clip_id[new_id] = (pos_sec, planned_end)
                result_box[0] = (new_clip, pos_sec, track_num, snapped)
            except Exception as exc:
                error_box[0] = str(exc)

        # Place + trim is ONE user-facing action, so it must be ONE undo step.
        # Without a shared transaction id the insert and the trim land as two
        # transactions and a single undo only reverts the trim.  When a caller
        # rippled the timeline to make room, transaction_id joins that group so
        # the ripple and the placement undo together.
        app = _get_app()
        _run_on_main_thread(_atomic(app, _do_add, tid=transaction_id))
        if error_box[0]:
            return error_box[0] if str(error_box[0]).startswith("Error") else f"Error: {error_box[0]}"
        if not result_box[0]:
            return "Error: Failed to add clip to timeline."

        placed, pos_sec, track_num, snapped = result_box[0]
        _snap_note = ", moved off mid-sentence" if snapped else ""
        if pos_arg is not None and abs(pos_sec - pos_arg) > 1e-9:
            _snap_note += f", moved from {pos_arg}s to butt against the previous clip"
        _last_split_file_id_by_chat_session.pop(chat_session_id, None)
        layers_out = app.project.get("layers") or []
        track_lbl = format_track_label_for_llm(int(track_num), layers_out)
        placed = placed or {}
        eff_dur = None
        try:
            if placed:
                eff_dur = float(placed.get("end", 0)) - float(placed.get("start", 0))
        except (TypeError, ValueError):
            eff_dur = None
        dur_part = f" duration={eff_dur:.2f}s" if eff_dur is not None and eff_dur > 0 else ""
        clip_id = placed.get("id", "") if isinstance(placed, dict) else ""
        id_part = f" timeline_clip_id={clip_id}" if clip_id else ""
        watch_part = " (watched)" if watched_start is not None else ""
        return (
            f"Added clip to timeline at position {pos_sec}s on track {track_lbl}"
            f"{dur_part}{id_part}{_snap_note}{watch_part}."
        )
    except Exception as e:
        return f"Error: {e}"


def slice_clip_at_playhead(**_kw) -> str:
    """Slice every unlocked clip and transition under the playhead, keeping both sides.

    slice_clips_tool targets one clip, a track or the selection; this keeps the
    old everything-under-the-playhead behaviour. Items on locked tracks and
    items whose edge sits exactly at the playhead (a cut there would leave a
    zero-length clip) are not sliced and not counted.
    """
    try:
        from windows.views.timeline_backend.enums import MenuSlice

        # Read state and perform the slice entirely on the main thread
        result_box = [None]

        def _do_slice():
            from classes.query import Clip, Transition
            app = _get_app()
            win = app.window
            fps = app.project.get("fps") or {}
            fps_float = _project_fps_float(fps)
            playhead_position = float(win.preview_thread.current_frame - 1) / fps_float
            half_frame = 0.5 / fps_float
            locked = {t.get("number") for t in (app.project.get("layers") or []) if t.get("lock")}

            def _sliceable(item):
                start = float(item.data.get("position", 0.0) or 0.0)
                end = start + float(item.data.get("end", 0.0) or 0.0) - float(item.data.get("start", 0.0) or 0.0)
                return (item.data.get("layer") not in locked
                        and start + half_frame < playhead_position < end - half_frame)

            clip_ids = [c.id for c in Clip.filter(intersect=playhead_position) if _sliceable(c)]
            tran_ids = [t.id for t in Transition.filter(intersect=playhead_position) if _sliceable(t)]
            if not clip_ids and not tran_ids:
                result_box[0] = (f"Error: no unlocked clip or transition under the playhead "
                                 f"({playhead_position:.2f} s); nothing was sliced.")
                return
            win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, clip_ids, tran_ids, playhead_position)
            n = len(clip_ids) + len(tran_ids)
            result_box[0] = f"Sliced {n} item(s) at the playhead ({playhead_position:.2f} s); both sides kept."

        _run_on_main_thread(_do_slice)

        return result_box[0] or "Error: the slice did not run."
    except Exception as e:
        return f"Error: {e}"


def reverse_clip(
    timeline_clip_id="",
    clip_query="",
    track="",
    occurrence="0",
    mode="reverse",
    **_kw,
) -> str:
    """Reverse a timeline clip (or reset time remapping).

    Same as Timeline → Speed → Reverse / Reset. mode='reverse' plays backward;
    mode='reset' clears reverse/speed time curves back to forward 1x.
    Resolve with timeline_clip_id or clip_query (+ track/occurrence if needed).
    """
    action = str(mode or "reverse").strip().lower()
    if action in ("reverse", "backward", "backwards"):
        menu_action_name = "REVERSE"
        done = "Reversed"
    elif action in ("reset", "none", "forward", "unreverse"):
        menu_action_name = "NONE"
        done = "Reset time on"
    else:
        return "Error: mode must be 'reverse' or 'reset'."

    if not str(timeline_clip_id or "").strip() and not str(clip_query or "").strip():
        return "Error: reverse_clip_tool requires timeline_clip_id or clip_query."

    try:
        resolved = _resolve_timeline_clip_for_tool(
            timeline_clip_id=timeline_clip_id,
            clip_query=clip_query,
            track=track,
            occurrence=occurrence,
        )
        if not resolved.ok or not resolved.clip:
            return resolved.error or "Error: Could not resolve timeline clip."

        clip_id = str(getattr(resolved.clip, "id", "") or "")
        if not clip_id:
            return "Error: Resolved clip has no id."

        result_box = [None]

        def _do_reverse():
            from classes.query import Clip
            from windows.views.retime import time_curve_is_reversed
            from windows.views.timeline_backend.enums import MenuTime

            app = _get_app()
            timeline = getattr(app.window, "timeline", None)
            if timeline is None or not hasattr(timeline, "Time_Triggered"):
                result_box[0] = "Error: Timeline view is not available."
                return
            clip = Clip.get(id=clip_id)
            if clip is None:
                result_box[0] = f"Error: timeline_clip_id={clip_id} is no longer on the timeline."
                return
            # Timeline > Speed > Reverse toggles, so asking a reversed clip to
            # reverse would play it forward again; a no-op must not add an undo step.
            time_data = clip.data.get("time")
            points = time_data.get("Points") if isinstance(time_data, dict) else None
            if menu_action_name == "REVERSE" and time_curve_is_reversed(time_data):
                result_box[0] = f"timeline_clip_id={clip_id} is already reversed; nothing changed."
                return
            if menu_action_name == "NONE" and (not isinstance(points, list) or len(points) <= 1):
                result_box[0] = f"timeline_clip_id={clip_id} already plays forward at 1x; nothing changed."
                return
            menu_action = getattr(MenuTime, menu_action_name)
            timeline.Time_Triggered(menu_action, [clip_id], "1X")
            result_box[0] = f"{done} timeline_clip_id={clip_id}."

        _run_on_main_thread(_do_reverse)
        return result_box[0] or f"{done} timeline_clip_id={clip_id}."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Search (project-wide video index + in-clip scenes)
# ---------------------------------------------------------------------------

def get_project_catalog(**_kw) -> str:
    """Orientation pass: list short summaries for all indexed project media."""
    try:
        from classes.api_client import get_backend_client
        from classes.app import get_app

        project_id = ""
        try:
            project_id = str(get_app().project.get("id") or "")
        except Exception:
            pass
        if not project_id:
            return "Error: project_id unavailable."
        client = get_backend_client()
        data = client.get_project_catalog(project_id)
        if data.get("error"):
            return f"Error: {data['error']}"
        items = data.get("items") or []
        if not items:
            return (
                "Catalog is empty — index/summarize project videos, images, and audio first, "
                "then call get_project_catalog_tool again."
            )
        lines = [f"Project catalog ({len(items)} media items):"]
        for it in items:
            name = it.get("filename") or it.get("file_id") or it.get("video_id")
            summary = (it.get("short_summary") or "").strip() or "(no summary)"
            mt = str(it.get("media_type") or "video")
            dur = it.get("duration_sec")
            dur_s = f" [{dur:.1f}s]" if isinstance(dur, (int, float)) and mt != "image" else ""
            lines.append(
                f"- [{mt}] {name}{dur_s} file_id={it.get('file_id')}: {summary}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


_ORDINAL_MAP = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
}


def _search_rank_key(hit):
    """Sort key that puts the best-ranked hit first; unranked hits sort last."""
    try:
        return float(hit.get("rank"))
    except (TypeError, ValueError):
        return float("inf")


def _detect_ordinal(query: str) -> int:
    words = (query or "").lower().split()
    for word in words:
        if word in _ORDINAL_MAP:
            return _ORDINAL_MAP[word]
    return 0


def search_clips(query="", top_k="5", look_for="", **_kw) -> str:
    """Project-wide video index search on this project's shared index.
    look_for="on_screen" when the query describes who or what is visible ("the
    guy with the iPad"), "spoken" when it describes what is said; omit for both.

    Returns media_bin_file_id + timestamp (deeper than Gemini tags). The first
    paragraph above is the MCP tool description, the only place an external
    agent learns what look_for takes.
    """
    q = str(query or "").strip()
    if not q:
        return "Error: query is required."
    try:
        k = int(float(top_k)) if str(top_k).strip() else 5
    except Exception:
        k = 5
    k = max(1, min(k, 20))

    try:
        from collections import defaultdict

        from classes.api_client import get_backend_client
        from classes.project_tl_index import (
            collect_project_twelvelabs_index,
            map_search_hit_to_file,
        )

        info = collect_project_twelvelabs_index()
        if info.get("error") and not info.get("index_id"):
            return (
                f"Error: {info['error']} "
                "Index/summarize project videos first, then search again."
            )
        index_id = str(info.get("index_id") or "").strip()
        if not index_id:
            return (
                "Error: No project video index_id on project files. "
                "Reindex clips so they share the project index, then retry."
            )
        video_map = info.get("video_map") or {}
        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Error: Video indexing is not configured on the backend."

        page_limit = max(30, k * 10)
        resp = client.search(
            q,
            top_k=page_limit,
            index_id=index_id,
            page_limit=page_limit,
            look_for=str(look_for or "").strip() or None,
        )
        if resp.get("error"):
            return f"Error: {resp['error']}"
        results = resp.get("results") or []
        if not results:
            return (
                f"No index matches for '{q}' in this project's index "
                f"({info.get('index_name') or index_id}, "
                f"{info.get('indexed_count', 0)} indexed media item(s)). "
                "Try a more specific description, or check indexing finished."
            )

        requested_nth = _detect_ordinal(q)
        grouped: dict = defaultdict(list)
        for r in results:
            if not isinstance(r, dict):
                continue
            vid = str(r.get("video_id") or "").strip()
            fid, fname = map_search_hit_to_file(r, video_map)
            key = fid or vid or fname or "unknown"
            grouped[key].append({**r, "_file_id": fid, "_fname": fname, "_vid": vid})

        lines = [
            f"Found {len(results)} match(es) across {len(grouped)} project media item(s) "
            f"(index_id={index_id}, index_name={info.get('index_name') or ''}):",
        ]
        shown = 0
        for key, hits in grouped.items():
            if shown >= k and requested_nth == 0:
                break
            fid = hits[0].get("_file_id") or ""
            fname = hits[0].get("_fname") or key
            vid = hits[0].get("_vid") or ""
            mt = str(hits[0].get("media_type") or "video")
            id_part = f" media_bin_file_id={fid}" if fid else " media_bin_file_id=(unmapped)"
            vid_part = f" twelvelabs_video_id={vid}" if vid else ""
            type_part = f" media_type={mt}"

            hits_sorted = sorted(hits, key=lambda x: float(x.get("start") or 0))
            if len(hits_sorted) == 1 and requested_nth == 0:
                r = hits_sorted[0]
                seg_s = float(r.get("start") or 0)
                seg_e = float(r.get("end") or 0)
                win = _format_search_window(r, seg_s, seg_e)
                lines.append(
                    f"  • {fname}{id_part}{vid_part}{type_part} — {win} "
                    f"(rank={r.get('rank')})"
                )
                shown += 1
                continue

            if requested_nth > 0:
                idx = min(requested_nth - 1, len(hits_sorted) - 1)
                r = hits_sorted[idx]
                seg_s = float(r.get("start") or 0)
                seg_e = float(r.get("end") or 0)
                win = _format_search_window(r, seg_s, seg_e)
                lines.append(
                    f"  • {fname}{id_part}{vid_part} — occurrence #{requested_nth} "
                    f"{win}"
                )
                shown += 1
            else:
                lines.append(
                    f"  • {fname}{id_part}{vid_part} — {len(hits_sorted)} occurrences:"
                )
                # Pick WHICH occurrences to show by rank, then show them in time
                # order. Truncating the chronological list drops the best match
                # whenever it sits late in the file, and an unmarked rank= is easy
                # to read past - both send the agent to the wrong window.
                by_rank = sorted(hits, key=_search_rank_key)
                best = by_rank[0] if by_rank else None
                # Number each row by its occurrence index in the FULL chronological
                # list - that is what an ordinal ("the 3rd time") resolves against,
                # so rank selection must not renumber the rows it kept.
                nth_of = {id(h): n for n, h in enumerate(hits_sorted, 1)}
                for r in sorted(by_rank[:8], key=lambda x: float(x.get("start") or 0)):
                    seg_s = float(r.get("start") or 0)
                    seg_e = float(r.get("end") or 0)
                    win = _format_search_window(r, seg_s, seg_e)
                    marker = "  <-- best match" if r is best else ""
                    lines.append(
                        f"      {nth_of.get(id(r), '?')}. {win} "
                        f"(rank={r.get('rank')}){marker}"
                    )
                if len(hits_sorted) > 1:
                    lines.append(
                        "      Place the best match unless the user asked for a "
                        "different one (e.g. 'the 2nd time')."
                    )
                shown += 1

        unmapped = sum(1 for hits in grouped.values() if not hits[0].get("_file_id"))
        if unmapped:
            lines.append(
                f"Note: {unmapped} hit group(s) had no media_bin_file_id mapping — "
                "reindex those files into this project index."
            )
        lines.append(
            "To PLACE a moment: place_moment(file_id=..., start_seconds=<keep in>, "
            "end_seconds=<keep out>, query=<same description>, track=..., position_seconds=...). "
            "Watch+trim+place are built in — do not call watch_clip_window_tool or convert to frames. "
            "To SLICE an already-placed clip: slice_moment(query, clip_query=... or timeline_clip_id=...)."
        )
        return "\n".join(lines)
    except Exception as e:
        log.error("search_clips: %s", e, exc_info=True)
        return f"Error: {e}"


def _search_hit_value(hit, *names):
    """First non-empty field of a search hit (a SearchItem object or a dict)."""
    for name in names:
        value = hit.get(name) if isinstance(hit, dict) else getattr(hit, name, None)
        if value:
            return value
    return ""


def _no_scene_matches(query, index_notes) -> str:
    """No hit: a plain answer when the index was searched, an error when it could not be."""
    if any(note.startswith("the index search failed") for note in index_notes):
        return (
            f"Error: {'; '.join(index_notes)}, and the clip's scene descriptions have no match "
            f"for {query!r}."
        )
    if index_notes:
        return f"No matches found for {query!r} in the scene descriptions ({'; '.join(index_notes)})."
    return "No matches found."


def search_clip_scenes(
    query="",
    top_k="5",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Find where something happens inside ONE timeline clip (semantic video search within its trimmed range).

    For "where in the interview does she mention pricing?" or "find the goal in this clip".
    Returns keep in/out and peak times relative to the clip's start (m:ss). Uses the clip's
    video index; when the video is not indexed (or the index search fails) it falls back to
    the clip's scene descriptions and says so. Read-only. For the whole project use
    search_clips_tool.
    """
    try:
        k = int(float(top_k)) if str(top_k).strip() else 5
    except Exception:
        k = 5

    try:
        from classes.ai_metadata_utils import get_effective_ai_metadata
        from classes.api_client import get_backend_client
        from classes.timeline_clip_context import build_timeline_clip_context, resolve_parent_file_data
        from classes.twelvelabs_match import select_hits_for_display, get_index_block

        resolved = _resolve_timeline_clip_for_tool(
            clip_query=clip_query,
            timeline_clip_id=timeline_clip_id,
            **_kw,
        )
        if not resolved.ok or not resolved.clip:
            return resolved.error or "Error: Could not resolve timeline clip."

        clip_obj = resolved.clip
        clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        source_file = _get_source_file_for_clip(clip_obj)
        file_data = source_file.data if source_file and isinstance(source_file.data, dict) else None
        ctx = build_timeline_clip_context(clip_obj, clip_data, file_data)
        clip_start = ctx.source_start
        clip_end = ctx.source_end
        clip_name = ctx.title or "Timeline clip"

        per_clip_ai = clip_data.get("ai_metadata") if isinstance(clip_data.get("ai_metadata"), dict) else None
        parent_data = resolve_parent_file_data(file_data, file_id=ctx.file_id)
        source_ai = None
        if parent_data:
            source_ai = parent_data.get("ai_metadata") if isinstance(parent_data.get("ai_metadata"), dict) else None

        watch_path, watch_dur, watch_cues = _lookup_watch_meta(
            ctx.file_id, file_data=file_data, parent_data=parent_data,
        )
        if not watch_path:
            watch_path = str(getattr(ctx, "source_path", "") or "")

        client = get_backend_client()
        nth = _parse_occurrence(str(_kw.get("occurrence", "0")), query)
        # Why the index could not answer, so a fallback result (or none) says so.
        index_notes = []

        # TwelveLabs search (parent index + trim window)
        if not client.is_indexing_configured():
            index_notes.append("video indexing is not configured on the backend")
        else:
            tw = get_index_block(source_ai or {})
            status = (tw.get("status") or "").lower()
            index_id = tw.get("index_id") or ""
            video_id = tw.get("video_id") or ""
            if not (status == "ready" and index_id and video_id):
                index_notes.append(
                    "the clip's video is not indexed yet" if status in ("", "ready")
                    else f"the clip's video index is {status}"
                )

            if status == "ready" and index_id and video_id:
                search_query = _semantic_search_query(query)
                items, err = _tl_search_items_in_window(
                    str(index_id), search_query, page_limit=max(30, k * 10), video_id=str(video_id),
                )
                if err:
                    index_notes.append(f"the index search failed ({err})")
                if not err and items:
                    matches = select_hits_for_display(
                        items,
                        clip_start=clip_start,
                        clip_end=clip_end,
                        occurrence=nth,
                        top_k=k,
                    )
                    if matches:
                        matches = [
                            _apply_watch_to_match(m, watch_path, query, watch_dur, watch_cues)
                            for m in matches
                        ]
                        lines = [
                            f"Index matches in '{clip_name}' "
                            f"({_fmt_mmss(clip_start)} - {_fmt_mmss(clip_end)}):"
                        ]
                        for m in matches:
                            rel_cut = m["cut_source"] - clip_start
                            in_s = float(m.get("in_source") if m.get("in_source") is not None else m["start"])
                            out_s = float(m.get("out_source") if m.get("out_source") is not None else m["end"])
                            rel_in = in_s - clip_start
                            rel_out = out_s - clip_start
                            lines.append(
                                f"- keep {_fmt_mmss(rel_in)}-{_fmt_mmss(rel_out)}"
                                f" peak {_fmt_mmss(rel_cut)}"
                                f" (rank={m.get('rank')}, overlap={m['overlap_ratio']:.2f})"
                            )
                            if m.get("transcription"):
                                lines.append(
                                    f"  transcript: {str(m['transcription']).strip()[:180]}"
                                )
                            if m.get("_watch_fallback"):
                                lines.append("  (text-index time; no visual match in watch window)")
                        warn = next((m.get("_watch_warning") for m in matches if m.get("_watch_warning")), "")
                        if warn:
                            lines.append(f"Note: {warn}")
                        return "\n".join(lines)

                # Broader project search filtered to this video before tag fallback
                search_query = _semantic_search_query(query)
                broad_items, broad_err = _tl_search_items_in_window(
                    str(index_id), search_query, page_limit=max(50, k * 15), video_id="",
                )
                if not broad_err and broad_items:
                    # Search hits are SearchItem objects (or dicts from older callers).
                    filtered = [
                        it for it in broad_items
                        if str(_search_hit_value(it, "video_id", "twelvelabs_video_id")) == str(video_id)
                    ]
                    if filtered:
                        matches = select_hits_for_display(
                            filtered,
                            clip_start=clip_start,
                            clip_end=clip_end,
                            occurrence=nth,
                            top_k=k,
                        )
                        if matches:
                            matches = [
                                _apply_watch_to_match(m, watch_path, query, watch_dur, watch_cues)
                                for m in matches
                            ]
                            lines = [
                                f"Index matches in '{clip_name}' "
                                f"({_fmt_mmss(clip_start)} - {_fmt_mmss(clip_end)}):"
                            ]
                            for m in matches:
                                rel_cut = m["cut_source"] - clip_start
                                in_s = float(m.get("in_source") if m.get("in_source") is not None else m.get("start") or rel_cut)
                                out_s = float(m.get("out_source") if m.get("out_source") is not None else m.get("end") or rel_cut)
                                lines.append(
                                    f"- keep {_fmt_mmss(in_s - clip_start)}-{_fmt_mmss(out_s - clip_start)} "
                                    f"peak {_fmt_mmss(rel_cut)} (project search)"
                                )
                            warn = next((m.get("_watch_warning") for m in matches if m.get("_watch_warning")), "")
                            if warn:
                                lines.append(f"Note: {warn}")
                            return "\n".join(lines)

        # Local chapter / description fallback (Pegasus chapters or legacy scenes)
        local_ai = per_clip_ai
        if local_ai is None:
            local_ai = get_effective_ai_metadata(
                parent_data or file_data,
                clip_data=clip_data,
                rebased=True,
            )

        candidates = []
        for ch in (local_ai or {}).get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            summary = (ch.get("summary") or ch.get("title") or "").strip()
            if not summary:
                continue
            candidates.append({
                "time": float(ch.get("start", 0.0) or 0.0),
                "description": summary,
            })
        for s in (local_ai or {}).get("scene_descriptions") or []:
            if not isinstance(s, dict):
                continue
            desc = (s.get("description") or "").strip()
            if not desc:
                continue
            candidates.append({
                "time": float(s.get("time", 0.0) or 0.0),
                "description": desc,
            })
        # Also score transcript / sounds as whole-clip hints (time=0)
        for key in ("transcript", "sounds", "description", "short_summary"):
            text = str((local_ai or {}).get(key) or "").strip()
            if text:
                candidates.append({"time": float(clip_start or 0.0), "description": text[:500]})

        if not candidates:
            return _no_scene_matches(query, index_notes)
        scored = []
        q_lower = query.lower()
        for s in candidates:
            desc = (s.get("description") or "").strip()
            if not desc:
                continue
            score = 0.0
            if q_lower in desc.lower():
                score = 10.0
            else:
                q_words = set(q_lower.split())
                d_words = set(desc.lower().split())
                overlap = len(q_words & d_words)
                if overlap:
                    score = overlap / (len(q_words) ** 0.5 * len(d_words) ** 0.5)
            if score > 0:
                scored.append({"time": float(s.get("time", 0.0)), "description": desc, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:k]
        if not results:
            return _no_scene_matches(query, index_notes)
        lines = [f"Description matches in '{clip_name}':"]
        if index_notes:
            lines.append(f"Note: {'; '.join(index_notes)}, so these come from the scene descriptions.")
        for r in results:
            watched = _watch_confirm_cut(
                watch_path, r["time"], r["time"], query,
                fallback=r["time"], duration=watch_dur, transcript_cues=watch_cues,
            )
            cut = watched.get("cut_source", r["time"])
            lines.append(f"- [{_fmt_mmss(cut)}] score={r['score']:.3f}: {r['description'][:200]}")
            if watched.get("warning"):
                lines.append(f"  Note: {watched['warning']}")
        return "\n".join(lines)
    except Exception as e:
        log.error("search_clip_scenes: %s", e, exc_info=True)
        return f"Error: {e}"


# A time written into a watch query ("visible at 72.5 seconds", "45-62s") that
# belongs in start/end - left there, the tool silently watched a search hit.
_QUERY_TIME_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:s|secs?|seconds?)\b|\b\d{1,2}:\d{2}\b|\bat\s+\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)


def watch_clip_window(
    query="",
    start="",
    end="",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Vision-check a window of a placed clip: confirm the query is on screen.
    Call this after you place, slice, trim, or modify a clip to verify your own
    edit. Read-only: start/end and every reported time are source seconds.

    Reports the frames watched, the shot cuts in the window, and where the query
    is visible. Put times in start/end, never in query. A shot boundary is in the
    shot cuts line - do not re-watch to refine it. Distinct from watch_clip_tool,
    which plays the clip in the editor.
    """
    try:
        from classes.timeline_clip_context import build_timeline_clip_context, resolve_parent_file_data

        # A time that does not parse ("0:14" used to) must not silently become
        # "no window" - that watched a search hit instead of the window asked for.
        try:
            t0 = parse_seconds_arg(start, default=None, field="start")
            t1 = parse_seconds_arg(end, default=None, field="end")
        except ValueError as exc:
            return f"Error: {exc}"

        resolved = _resolve_timeline_clip_for_tool(
            clip_query=clip_query,
            timeline_clip_id=timeline_clip_id,
            **_kw,
        )
        if not resolved.ok or not resolved.clip:
            return resolved.error or "Error: Could not resolve timeline clip."

        clip_obj = resolved.clip
        clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        source_file = _get_source_file_for_clip(clip_obj)
        file_data = source_file.data if source_file and isinstance(source_file.data, dict) else None
        ctx = build_timeline_clip_context(clip_obj, clip_data, file_data)
        parent_data = resolve_parent_file_data(file_data, file_id=ctx.file_id)
        watch_path, watch_dur, watch_cues = _lookup_watch_meta(
            ctx.file_id, file_data=file_data, parent_data=parent_data,
        )
        if not watch_path:
            watch_path = str(getattr(ctx, "source_path", "") or "")

        if (t0 is None) != (t1 is None):
            return (
                "Error: Pass both start and end (source seconds), or neither to watch "
                "the best search match."
            )
        if t0 is None and _QUERY_TIME_RE.search(query or ""):
            return (
                "Error: The query names a time but start/end are empty. Put the window in "
                "start and end (source seconds) and describe only what to look for in query."
            )
        if t0 is not None and t1 is not None:
            lo, hi = min(t0, t1), max(t0, t1)
            if hi <= ctx.source_start or lo >= ctx.source_end:
                return (
                    f"Error: Window {lo:.2f}-{hi:.2f}s is outside this clip's source range "
                    f"{ctx.source_start:.2f}-{ctx.source_end:.2f}s (start/end are source seconds)."
                )
            # Only what this clip plays can confirm an edit to it.
            t0, t1 = max(lo, ctx.source_start), min(hi, ctx.source_end)
        else:
            search_query = _semantic_search_query(query)
            hit_start = None
            hit_end = None
            if search_query:
                from classes.api_client import get_backend_client
                from classes.twelvelabs_match import get_index_block, select_hits_for_display

                source_ai = (
                    parent_data.get("ai_metadata")
                    if parent_data and isinstance(parent_data.get("ai_metadata"), dict)
                    else None
                )
                tw = get_index_block(source_ai or {})
                client = get_backend_client()
                if client.is_indexing_configured() and tw.get("index_id") and tw.get("video_id"):
                    items, err = _tl_search_items_in_window(
                        str(tw.get("index_id")), search_query, page_limit=30,
                        video_id=str(tw.get("video_id")),
                    )
                    if not err and items:
                        matches = select_hits_for_display(
                            items,
                            clip_start=ctx.source_start,
                            clip_end=ctx.source_end,
                            occurrence=_parse_occurrence(str(_kw.get("occurrence", "0")), query),
                            top_k=1,
                        )
                        if matches:
                            hit_start = float(matches[0].get("start") or 0)
                            hit_end = float(matches[0].get("end") or hit_start)
            if hit_start is None:
                t0 = ctx.source_start
                t1 = ctx.source_end
            else:
                t0 = hit_start
                t1 = hit_end
        if t1 < t0:
            t0, t1 = t1, t0

        watched = _watch_confirm_cut(
            watch_path, t0, t1, query,
            fallback=t0, duration=watch_dur, transcript_cues=watch_cues,
        )
        cut = float(watched.get("cut_source") or t0)
        in_s = float(watched.get("in_source") if watched.get("in_source") is not None else t0)
        out_s = float(watched.get("out_source") if watched.get("out_source") is not None else t1)

        def _secs(times):
            return ", ".join(f"{float(t):.2f}" for t in times)

        frame_times = watched.get("frame_times") or []
        scene_times = watched.get("scene_times") or []
        clip_lo, clip_hi = float(ctx.source_start), float(ctx.source_end)
        lines = [
            f"Watched {len(frame_times)} frames of '{ctx.title or 'clip'}' "
            f"(source seconds; this clip plays {clip_lo:.2f}-{clip_hi:.2f}): "
            f"{_secs(frame_times) or 'none'}.",
            f"Shot cuts in window (source s): {_secs(scene_times)}."
            if scene_times else "Shot cuts in window: none.",
        ]
        # The watch pads its window for context, so a match can sit in frames this
        # clip never plays. Only what the clip plays confirms (or refutes) an edit.
        visible = [float(t) for t in watched.get("visible_at") or []]
        visible_in_clip = [t for t in visible if clip_lo - 1e-3 <= t <= clip_hi + 1e-3]
        seen = bool(watched.get("matched")) and not watched.get("used_fallback")
        outside_only = (bool(visible) and not visible_in_clip) or out_s < clip_lo or in_s > clip_hi
        if seen and not outside_only:
            in_c, out_c = max(in_s, clip_lo), min(out_s, clip_hi)
            cut_c = max(in_c, min(cut, out_c))
            lines.append(
                f"Visible {in_c:.3f}s–{out_c:.3f}s source; peak {cut_c:.3f}s source "
                f"({_fmt_mmss(cut_c - clip_lo)} into the clip)."
                + (f" Seen in frames: {_secs(visible_in_clip)}." if visible_in_clip else "")
            )
        elif seen:
            lines.append(
                "Not visible in this clip's frames"
                + (f" - only outside it, at {_secs(visible)}s source." if visible else ".")
            )
        else:
            lines.append("Not visible in these frames.")
        if watched.get("reason"):
            lines.append(str(watched.get("reason")))
        if watched.get("warning"):
            lines.append(str(watched.get("warning")))
        return "\n".join(lines)
    except Exception as e:
        log.error("watch_clip_window: %s", e, exc_info=True)
        return f"Error: {e}"


def _parse_occurrence(occurrence_str: str, query: str) -> int:
    """Return 1-based occurrence index (0 = best overlap+rank match)."""
    try:
        n = int(float(str(occurrence_str).strip()))
        if n > 0:
            return n
    except Exception:
        pass
    q_lower = (query or "").lower()
    for word, n in _ORDINAL_MAP.items():
        if word in q_lower.split():
            return n
    return 0


def _semantic_search_query(query: str) -> str:
    """Strip ordinal words so TwelveLabs search uses semantic content only."""
    if not query or not str(query).strip():
        return ""
    kept = []
    for word in str(query).split():
        bare = word.lower().strip(".,;:!?\"'")
        if bare in _ORDINAL_MAP:
            continue
        kept.append(word)
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else str(query).strip()


def _lookup_watch_meta(file_id_str, file_data=None, parent_data=None):
    """Local path, duration, and transcript cues for a watch window."""
    fd = file_data if isinstance(file_data, dict) else {}
    parent = parent_data if isinstance(parent_data, dict) else None
    if not fd and file_id_str:
        try:
            from classes.query import File
            from classes.timeline_clip_context import resolve_parent_file_data

            fobj = File.get(id=str(file_id_str))
            fd = fobj.data if fobj and isinstance(fobj.data, dict) else {}
            parent = resolve_parent_file_data(fd, file_id=str(file_id_str)) or fd
        except Exception:
            parent = parent or fd
    data = parent or fd or {}
    path = str(data.get("path") or fd.get("path") or "")
    try:
        dur = float(data.get("duration") or fd.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    ai = data.get("ai_metadata") if isinstance(data.get("ai_metadata"), dict) else {}
    cues = ai.get("transcript_cues") if isinstance(ai.get("transcript_cues"), list) else []
    return path, dur, cues


def _watch_confirm_cut(
    source_path,
    start,
    end,
    query,
    *,
    fallback=None,
    duration=0.0,
    transcript_cues=None,
    fallback_in=None,
    fallback_out=None,
):
    """Layer-3 watch: JPEG window then vision confirm. Fail-soft to fallback time."""
    fb = fallback if fallback is not None else start
    try:
        fb = float(fb)
    except (TypeError, ValueError):
        fb = float(start or 0)
    try:
        fb_in = float(fallback_in if fallback_in is not None else start)
    except (TypeError, ValueError):
        fb_in = float(start or 0)
    try:
        fb_out = float(fallback_out if fallback_out is not None else end)
    except (TypeError, ValueError):
        fb_out = float(end or start or 0)
    if not source_path:
        return {
            "cut_source": fb,
            "in_source": fb_in,
            "out_source": fb_out,
            "matched": False,
            "used_fallback": True,
            "warning": "",
            "reason": "No source path for watch window",
            "window_start": float(start or 0),
            "window_end": float(end or 0),
        }
    from classes.watch_window import confirm_watch_window

    return confirm_watch_window(
        source_path=str(source_path),
        start=float(start),
        end=float(end),
        query=query or "",
        duration=float(duration or 0),
        transcript_cues=transcript_cues,
        fallback_cut=fb,
        fallback_in=fb_in,
        fallback_out=fb_out,
    )


def _apply_watch_to_match(match, source_path, query, duration, cues):
    m = dict(match or {})
    try:
        start = float(m.get("start") if m.get("start") is not None else m.get("cut_source") or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(m.get("end") if m.get("end") is not None else start)
    except (TypeError, ValueError):
        end = start
    if end < start:
        start, end = end, start
    watched = _watch_confirm_cut(
        source_path, start, end, query,
        fallback=m.get("cut_source", (start + end) / 2.0),
        duration=duration,
        transcript_cues=cues,
        fallback_in=start,
        fallback_out=end,
    )
    m["cut_source"] = watched.get("cut_source", start)
    m["in_source"] = watched.get("in_source", start)
    m["out_source"] = watched.get("out_source", end)
    m["_watch_warning"] = watched.get("warning") or ""
    m["_watch_matched"] = watched.get("matched")
    m["_watch_fallback"] = watched.get("used_fallback")
    return m


def _audio_biased_tl_query(query: str, source_ai=None) -> str:
    """Build an audio/dialogue-oriented TwelveLabs query."""
    q = _semantic_search_query(query)
    if not q:
        return "spoken dialogue"
    import re
    if re.search(r"[^\x00-\x7F]", q):
        return f"spoken words: {q}"
    return f"spoken dialogue about {q}"


def _tl_search_items_in_window(index_id, query_text, *, page_limit=30, video_id=""):
    """Run TL search; on zero hits retry with audio-biased query."""
    items, err = _twelvelabs_search_in_window(
        index_id, query_text, page_limit=page_limit, video_id=video_id,
    )
    if err or items:
        return items, err
    audio_q = _audio_biased_tl_query(query_text)
    if audio_q != query_text:
        return _twelvelabs_search_in_window(
            index_id, audio_q, page_limit=page_limit, video_id=video_id,
        )
    return items, err


def _scene_description_cut_source(
    clip_start: float,
    clip_end: float,
    source_ai,
    query: str,
    occurrence: int,
    per_clip_ai=None,
):
    """Return a source-file cut time from Pegasus chapters or legacy scenes, or None."""
    from classes.ai_metadata_utils import adjust_scene_descriptions_for_subclip

    local_ai = per_clip_ai
    if local_ai is None and source_ai is not None:
        local_ai = adjust_scene_descriptions_for_subclip(source_ai, clip_start, clip_end)

    candidates = []
    for ch in (local_ai or {}).get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        summary = (ch.get("summary") or ch.get("title") or "").strip()
        if not summary:
            continue
        candidates.append({"time": float(ch.get("start", 0.0) or 0.0), "description": summary})
    for s in (local_ai or {}).get("scene_descriptions") or []:
        if not isinstance(s, dict):
            continue
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        candidates.append({"time": float(s.get("time", 0.0) or 0.0), "description": desc})

    if not candidates:
        return None
    q_lower = (query or "").lower()
    scored = []
    for s in candidates:
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        t = float(s.get("time", 0.0) or 0.0)
        if t < clip_start - 1e-3 or t > clip_end + 1e-3:
            continue
        score = 0.0
        if q_lower and q_lower in desc.lower():
            score = 10.0
        elif q_lower:
            qw = set(q_lower.split())
            dw = set(desc.lower().split())
            overlap = len(qw & dw)
            if overlap:
                score = overlap / (len(qw) ** 0.5 * len(dw) ** 0.5)
        else:
            score = 0.01
        if score > 0:
            scored.append((t, score))
    if not scored:
        return None
    scored.sort(key=lambda x: x[1], reverse=True)
    if occurrence and occurrence > 0:
        idx = min(occurrence, len(scored)) - 1
        return scored[idx][0]
    return scored[0][0]


def _clip_transcript_cues(clip_id_str):
    """Effective transcript cues for a timeline clip, in source seconds."""
    try:
        from classes.query import Clip, File
        from classes.ai_metadata_utils import get_effective_ai_metadata

        clip_obj = Clip.get(id=clip_id_str)
        if not clip_obj or not isinstance(clip_obj.data, dict):
            return []
        data = clip_obj.data
        file_data = None
        file_id = str(data.get("file_id") or "")
        if file_id:
            file_obj = File.get(id=file_id)
            if file_obj and isinstance(file_obj.data, dict):
                file_data = file_obj.data
        effective = get_effective_ai_metadata(
            file_data, data, clip_ai_metadata=data.get("ai_metadata")
        )
        cues = (effective or {}).get("transcript_cues")
        return [c for c in cues if isinstance(c, dict)] if isinstance(cues, list) else []
    except Exception as exc:
        log.debug("_clip_transcript_cues(%s): %s", clip_id_str, exc)
        return []


# Above this share of spoken audio a window is carried by dialogue, not by what
# changes on screen. Stills of a talking head look identical, so the watch just
# echoes the span it was shown - the transcript is the better boundary source.
DIALOGUE_COVERAGE = 0.6


def _window_is_dialogue_driven(file_data, start_sec, end_sec):
    """True when transcript cues already describe this window better than frames."""
    from classes import audio_mix as am

    try:
        cues = am.speech_windows((file_data or {}).get("ai_metadata"))
        if not cues:
            return False
        coverage = am.cue_coverage(cues, start_sec, end_sec)
        if coverage >= DIALOGUE_COVERAGE:
            log.info(
                "watch skipped: [%.2f-%.2f]s is %.0f%% speech - cutting on transcript "
                "cues instead of stills",
                start_sec, end_sec, coverage * 100,
            )
            return True
    except Exception as exc:
        log.debug("_window_is_dialogue_driven: %s", exc)
    return False


def _snap_window_off_boundaries(file_data, start_sec, end_sec):
    """(start, end, moved) - keep a placement window off mid-phrase edges."""
    from classes import audio_mix as am

    try:
        ai = (file_data or {}).get("ai_metadata")
        s, e, moved = am.snap_window_to_boundaries(start_sec, end_sec, ai)
        if moved:
            log.info(
                "placement snapped off mid-phrase: [%.2f-%.2f] -> [%.2f-%.2f]s",
                start_sec, end_sec, s, e,
            )
        return s, e, moved
    except Exception as exc:
        log.debug("_snap_window_off_boundaries: %s", exc)
        return start_sec, end_sec, False


def _snap_cut_off_speech(clip_id_str, cut_source):
    """(cut, moved) - shift a cut that lands mid-sentence to the cue boundary."""
    from classes import audio_mix as am

    cues = _clip_transcript_cues(clip_id_str)
    if not cues:
        return cut_source, False
    snapped, cue = am.snap_cut_out_of_speech(cut_source, cues)
    return snapped, cue is not None


def _slice_at_source_cut(
    clip_id_str: str,
    clip_start: float,
    clip_end: float,
    clip_pos: float,
    cut_source: float,
    *,
    label: str = "match",
) -> str:
    from classes.twelvelabs_match import snap_source_time_to_frame, snap_timeline_position
    from windows.views.timeline_backend.enums import MenuSlice

    fps = _get_app().project.get("fps") or {}
    fps_num = float(fps.get("num", 30))
    fps_den = float(fps.get("den", 1)) or 1.0

    # Never cut through a spoken line - snap to the nearer transcript boundary.
    cut_source, moved_off_cue = _snap_cut_off_speech(clip_id_str, float(cut_source))
    if moved_off_cue:
        label = f"{label}, moved off speech"

    cut_source = snap_source_time_to_frame(float(cut_source), fps_num, fps_den)
    slice_pos = snap_timeline_position(
        clip_pos + (cut_source - clip_start), fps_num, fps_den,
    )
    clip_timeline_end = clip_pos + (clip_end - clip_start)
    if slice_pos <= clip_pos or slice_pos >= clip_timeline_end:
        return (
            f"Error: Computed slice position ({slice_pos:.3f}s) is outside "
            f"the clip range [{clip_pos:.3f}s – {clip_timeline_end:.3f}s]."
        )
    slice_error_box = [None]

    def _do_slice():
        try:
            _get_app().window.timeline.Slice_Triggered(
                MenuSlice.KEEP_BOTH, [clip_id_str], [], slice_pos,
            )
        except Exception as exc:
            slice_error_box[0] = str(exc)

    _run_on_main_thread(_do_slice)
    if slice_error_box[0]:
        return f"Error during slice: {slice_error_box[0]}"
    return f"Sliced at {_fmt_mmss(cut_source - clip_start)} ({label})."


def _parse_mmss_or_hhmmss_token(tok: str):
    """Return seconds for 'SS', 'M:SS', or 'H:M:SS' tokens, else None."""
    if not isinstance(tok, str):
        return None
    return parse_timecode_token(tok)


def _parse_explicit_source_time_range_sec(query: str):
    """If *query* names a concrete time range in source seconds, return (t0, t1).

    t0/t1 are absolute times in the same frame as timeline clip start/end (source media).
    Returns None when no explicit numeric range is detected (caller uses semantic search).
    """
    if not query or not isinstance(query, str):
        return None
    s = query.strip()
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\s+to\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*s\b\s+to\s+(\d+(?:\.\d+)?)\s*s\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"(\d+:\d{2}(?::\d{2})?)\s+to\s+(\d+:\d{2}(?::\d{2})?)",
        s,
        re.IGNORECASE,
    )
    if m:
        t0 = _parse_mmss_or_hhmmss_token(m.group(1))
        t1 = _parse_mmss_or_hhmmss_token(m.group(2))
        if t0 is not None and t1 is not None and t1 > t0:
            return (t0, t1)
    if re.search(r"\bsec", s, re.IGNORECASE):
        m = re.search(
            r"from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\b",
            s,
            re.IGNORECASE,
        )
        if m:
            t0, t1 = float(m.group(1)), float(m.group(2))
            if t1 > t0:
                return (t0, t1)
    return None


def _slice_timeline_clip_at_source_times(
    clip_id_str: str,
    clip_start: float,
    clip_end: float,
    clip_pos: float,
    layer_num: int,
    file_id_str: str,
    t0: float,
    t1: float,
) -> str:
    """Slice once or twice so the middle segment is approximately source [t0, t1]."""
    from windows.views.timeline_backend.enums import MenuSlice
    from classes.query import Clip

    app = _get_app()
    win = app.window
    fps = app.project.get("fps") or {}
    fps_num = float(fps.get("num", 30))
    fps_den = float(fps.get("den", 1)) or 1.0

    def snap(pos: float) -> float:
        return float(round((float(pos) * fps_num) / fps_den) * fps_den) / fps_num

    eps = 1e-3
    clip_tl_end = clip_pos + (clip_end - clip_start)

    def _find_right_segment(after_pos: float, src_start: float) -> Optional[str]:
        best_id, best_score = None, 1e9
        for c in Clip.filter():
            try:
                c_ly = int(float(c.data.get("layer", 0) or 0))
            except (TypeError, ValueError):
                continue
            if c_ly != int(layer_num):
                continue
            try:
                p = float(c.data.get("position", 0))
                st = float(c.data.get("start", 0))
            except (TypeError, ValueError):
                continue
            if file_id_str and str(c.data.get("file_id") or "") != file_id_str:
                continue
            score = abs(p - after_pos) + abs(st - src_start)
            if score < best_score:
                best_score = score
                best_id = str(c.id)
        if best_id is not None and best_score < 0.25:
            return best_id
        return None

    # Degenerate: full clip
    if t0 <= clip_start + eps and t1 >= clip_end - eps:
        return "Nothing to slice: the requested range spans the whole clip."

    # Only upper boundary inside clip
    if t0 <= clip_start + eps:
        pos2 = snap(clip_pos + (t1 - clip_start))
        if pos2 <= clip_pos + eps or pos2 >= clip_tl_end - eps:
            return (
                f"Error: End time maps outside the clip "
                f"(clip source {clip_start:.3f}s–{clip_end:.3f}s on timeline)."
            )
        win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos2)
        return f"Sliced at {_fmt_mmss(t1 - clip_start)} from clip start; middle+right kept."

    # Only lower boundary inside clip
    if t1 >= clip_end - eps:
        pos1 = snap(clip_pos + (t0 - clip_start))
        if pos1 <= clip_pos + eps or pos1 >= clip_tl_end - eps:
            return (
                f"Error: Start time maps outside the clip "
                f"(clip source {clip_start:.3f}s–{clip_end:.3f}s on timeline)."
            )
        win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos1)
        return f"Sliced at {_fmt_mmss(t0 - clip_start)} from clip start; left+middle kept."

    pos1 = snap(clip_pos + (t0 - clip_start))
    pos2_abs = snap(clip_pos + (t1 - clip_start))
    if pos1 <= clip_pos + eps or pos1 >= clip_tl_end - eps:
        return "Error: First slice position is outside the clip on the timeline."
    if pos2_abs <= pos1 + eps or pos2_abs >= clip_tl_end - eps:
        return "Error: Second slice position is outside the clip on the timeline."

    win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos1)
    right_id = _find_right_segment(pos1, t0)
    if not right_id:
        return (
            "Error: The first cut was made but the new right-hand segment could not be found "
            "for the second cut. Undo, or slice the remaining boundary at the playhead."
        )

    pos2 = snap(pos1 + (t1 - t0))
    right_tl_end = pos1 + (clip_end - t0)
    if pos2 <= pos1 + eps or pos2 >= right_tl_end - eps:
        return "Error: Second slice maps outside the trimmed segment."

    win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [right_id], [], pos2)
    return (
        f"Sliced at {_fmt_mmss(t0 - clip_start)} and {_fmt_mmss(t1 - clip_start)} "
        f"(source). Three segments: before, selected range, after."
    )


_SIBLING_EPS = 1e-3


def _sibling_clips_from_same_file(file_id: str, exclude_clip_id: str = "") -> list:
    """Other timeline placements cut from the same source file.

    Returns ``[(clip_id, source_start, source_end, position, layer), ...]``
    sorted by source_start. Must run on the main thread (reads project data).
    Any failure yields ``[]`` so callers fall back to the single-clip path.
    """
    if not str(file_id or "").strip():
        # No source identity: every other id-less clip would "match", and a cut
        # outside this clip would land on a clip from a different file.
        return []
    try:
        from classes.query import Clip
        from classes.ai_metadata_utils import get_source_window
    except Exception:
        return []
    out = []
    try:
        for c in Clip.filter():
            d = c.data if isinstance(getattr(c, "data", None), dict) else {}
            if str(d.get("file_id") or "") != str(file_id or ""):
                continue
            if str(c.id) == str(exclude_clip_id):
                continue
            sf = _get_source_file_for_clip(c)
            fd = sf.data if sf and isinstance(sf.data, dict) else None
            cs, ce = get_source_window(d, fd)
            try:
                layer = int(d.get("layer", 1) or 1)
            except (TypeError, ValueError):
                layer = 1
            out.append((str(c.id), float(cs), float(ce), float(d.get("position", 0.0) or 0.0), layer))
    except Exception as exc:
        log.debug("sibling clip scan failed: %s", exc)
        return []
    out.sort(key=lambda t: t[1])
    return out


def _sibling_containing(siblings, t0: float, t1: float):
    """First sibling whose source window contains [t0, t1] with room to cut.

    A point cut (t0 == t1) must be strictly inside the sibling: a cut on its
    edge would split off nothing. A range may touch one edge but not both.
    """
    for sib in siblings or []:
        _sid, cs, ce, _pos, _layer = sib
        if t1 - t0 <= _SIBLING_EPS:
            if cs + _SIBLING_EPS < t0 < ce - _SIBLING_EPS:
                return sib
            continue
        if cs - _SIBLING_EPS <= t0 and t1 <= ce + _SIBLING_EPS:
            if t0 - cs > _SIBLING_EPS or ce - t1 > _SIBLING_EPS:
                return sib
    return None


def _describe_siblings(siblings) -> str:
    if not siblings:
        return "it is the only clip from this file on the timeline"
    parts = [
        f"[{cs:.2f}s–{ce:.2f}s] at {_fmt_mmss(pos)} on track {layer} (timeline_clip_id={sid})"
        for sid, cs, ce, pos, layer in siblings
    ]
    return "other clips from this file cover " + "; ".join(parts)


def slice_clip_at_best_match(
    query="",
    occurrence="0",
    clip_query="",
    timeline_clip_id="",
    start_seconds="",
    end_seconds="",
    **_kw,
) -> str:
    """Cut a timeline clip where a described moment happens, or at explicit source times.

    query is either a description ("when the dog jumps") -- searched in the clip's video
    index, confirmed by watching, moved off speech -- or a range in the clip's SOURCE time
    ("from 4 seconds to 10 seconds", "0:04 to 0:10"), which cuts so that range becomes its own
    segment. No match, an unindexed video or a cut outside the clip is an Error and nothing
    is cut.
    """
    try:
        from classes.api_client import get_backend_client

        # Explicit source seconds (from a watch or the user) skip search + watch.
        # One that does not parse must refuse, not fall through to a search and
        # cut wherever the best match happens to be.
        try:
            t_in = parse_seconds_arg(start_seconds, default=None, field="start_seconds")
            t_out = parse_seconds_arg(end_seconds, default=None, field="end_seconds")
        except ValueError as exc:
            return f"Error: {exc}"

        clip_info_box = [None]
        error_box_pre = [None]
        siblings_box: list = [[]]

        def _read_clip_info():
            try:
                from classes.clip_resolver import resolve_timeline_clip
                from classes.ai_metadata_utils import get_source_window
                from classes.timeline_clip_context import resolve_parent_file_data
                from classes.twelvelabs_match import get_index_block

                occ = _parse_occurrence(str(occurrence or _kw.get("occurrence", "0")), query)
                resolved = resolve_timeline_clip(
                    timeline_clip_id=str(timeline_clip_id or "").strip(),
                    clip_query=str(clip_query or "").strip(),
                    track=str(_kw.get("track") or _kw.get("prefer_track") or "").strip(),
                    occurrence=occ,
                )
                if not resolved.ok or not resolved.clip:
                    error_box_pre[0] = resolved.error or "Error: Could not resolve timeline clip."
                    return
                obj = resolved.clip
                d = obj.data if isinstance(obj.data, dict) else {}
                sf = _get_source_file_for_clip(obj)
                fd = sf.data if sf and isinstance(sf.data, dict) else None
                cs, ce = get_source_window(d, fd)
                cp = float(d.get("position", 0.0) or 0.0)
                ly = d.get("layer", 1)
                try:
                    layer_num = int(ly) if ly is not None else 1
                except (TypeError, ValueError):
                    layer_num = 1
                fid = str(d.get("file_id") or "")
                parent_data = resolve_parent_file_data(fd, file_id=fid)
                sa = (
                    parent_data.get("ai_metadata")
                    if parent_data and isinstance(parent_data.get("ai_metadata"), dict)
                    else None
                )
                # Extract TwelveLabs info (may be absent for old imports)
                tw = get_index_block(sa or {})
                tw_status = (tw.get("status") or "").lower()
                iid = tw.get("index_id") or ""
                vid = tw.get("video_id") or ""
                if not iid:
                    log.warning(
                        "TwelveLabs metadata missing for source file; "
                        "falling back to default index lookup."
                    )
                tw_err = str(tw.get("error") or "").strip()
                clip_info_box[0] = (
                    str(obj.id), cs, ce, cp, str(iid), str(vid), layer_num, fid, tw_status, tw_err
                )
                siblings_box[0] = _sibling_clips_from_same_file(fid, exclude_clip_id=str(obj.id))
            except Exception as exc:
                error_box_pre[0] = f"Error: {exc}"

        _run_on_main_thread(_read_clip_info)

        if error_box_pre[0]:
            return error_box_pre[0]
        if not clip_info_box[0]:
            return "Error: Could not read clip metadata."

        clip_id_str, clip_start, clip_end, clip_pos, index_id, video_id, layer_num, file_id_str, tw_status, tw_error = clip_info_box[0]

        siblings = siblings_box[0] or []
        if (t_in is None) != (t_out is None):
            cut_at = t_in if t_out is None else t_out
            if not clip_start < cut_at < clip_end:
                # A cut on the clip's own edge splits off nothing. Say so
                # plainly (not as an error) so the caller does not retry it.
                if abs(cut_at - clip_start) <= _SIBLING_EPS:
                    return (
                        f"Nothing to slice: {cut_at:.2f}s is already the start of this clip "
                        f"(source window [{clip_start:.2f}s–{clip_end:.2f}s])."
                    )
                if abs(cut_at - clip_end) <= _SIBLING_EPS:
                    return (
                        f"Nothing to slice: {cut_at:.2f}s is already the end of this clip "
                        f"(source window [{clip_start:.2f}s–{clip_end:.2f}s])."
                    )
                sib = _sibling_containing(siblings, cut_at, cut_at)
                if sib:
                    sid, s_cs, s_ce, s_pos, _s_layer = sib
                    return _slice_at_source_cut(
                        sid, s_cs, s_ce, s_pos, cut_at,
                        label=f"requested time, on the clip that holds it: timeline_clip_id={sid}",
                    )
                return (
                    f"Error: Requested cut {cut_at:.2f}s is outside this clip's "
                    f"source window [{clip_start:.2f}s–{clip_end:.2f}s], and "
                    f"{_describe_siblings(siblings)}. Pick a time inside a clip's window, "
                    f"or pass that clip's timeline_clip_id."
                )
            return _slice_at_source_cut(
                clip_id_str, clip_start, clip_end, clip_pos, cut_at, label="requested time",
            )
        time_rng = _parse_explicit_source_time_range_sec(query or "")
        if t_in is not None:
            time_rng = (min(t_in, t_out), max(t_in, t_out))
        if time_rng is not None:
            t0, t1 = time_rng
            eps = 1e-3
            target = (clip_id_str, clip_start, clip_end, clip_pos, layer_num)
            if t0 < clip_start - eps or t1 > clip_end + eps:
                sib = _sibling_containing(siblings, t0, t1)
                if sib is None:
                    return (
                        f"Error: Requested range [{t0:.2f}s–{t1:.2f}s] is outside this clip's "
                        f"source window [{clip_start:.2f}s–{clip_end:.2f}s], and "
                        f"{_describe_siblings(siblings)}. Pick a range inside one clip's window, "
                        f"or pass that clip's timeline_clip_id."
                    )
                target = sib
            elif abs(t0 - clip_start) <= eps and abs(t1 - clip_end) <= eps:
                return (
                    f"Nothing to slice: [{t0:.2f}s–{t1:.2f}s] is already exactly this clip's "
                    f"source window."
                )
            tgt_id, tgt_cs, tgt_ce, tgt_pos, tgt_layer = target

            def _do_time_slice():
                return _slice_timeline_clip_at_source_times(
                    tgt_id,
                    tgt_cs,
                    tgt_ce,
                    tgt_pos,
                    tgt_layer,
                    file_id_str,
                    t0,
                    t1,
                )

            try:
                return _run_on_main_thread(_do_time_slice)
            except Exception as exc:
                log.error("time-based slice failed: %s", exc, exc_info=True)
                return f"Error: {exc}"

        if tw_status == "failed":
            detail = f" ({tw_error})" if tw_error else ""
            return (
                "Error: Video indexing failed for this clip's source"
                + detail
                + ". Re-import the file or run reindex_project_file_tool to upload and index again. "
                "You can still slice by explicit times, e.g. 'from 4 seconds to 10 seconds'."
            )
        if tw_status == "indexing":
            return (
                "Error: This clip's video is still being indexed; nothing was sliced. "
                "Call wait_until_project_indexed_tool, or slice by explicit times "
                "(e.g. 'from 4 seconds to 10 seconds')."
            )

        # ── 2. Check backend connectivity (can run on any thread)
        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Error: TwelveLabs is not configured."

        if not index_id:
            return (
                "Error: This clip's source file has no TwelveLabs index_id. "
                "Re-import or re-index the file."
            )
        if not video_id:
            return (
                "Error: This clip's source file has no TwelveLabs video_id. "
                "Re-import or re-index the file so searches target the correct video."
            )

        # ── 3. TwelveLabs search (REST call – fine from background thread)
        from classes.twelvelabs_match import (
            select_twelvelabs_match,
            snap_source_time_to_frame,
            snap_timeline_position,
        )

        search_query = _semantic_search_query(query)
        if not search_query:
            return "Error: Empty search query."

        items, err = _twelvelabs_search_in_window(
            index_id, search_query, page_limit=30, video_id=video_id,
        )
        if err:
            return f"Error: {err}"
        if not items:
            sa_fb = None
            try:
                from classes.query import File
                fobj = File.get(id=file_id_str)
                if fobj and isinstance(fobj.data, dict):
                    raw = fobj.data.get("ai_metadata")
                    sa_fb = raw if isinstance(raw, dict) else None
            except Exception:
                pass
            cut_from_scenes = _scene_description_cut_source(
                clip_start, clip_end, sa_fb, query,
                _parse_occurrence(occurrence, query),
            )
            if cut_from_scenes is not None:
                path, dur, cues = _lookup_watch_meta(file_id_str)
                watched = _watch_confirm_cut(
                    path, cut_from_scenes, cut_from_scenes, query,
                    fallback=cut_from_scenes, duration=dur, transcript_cues=cues,
                )
                return _slice_at_source_cut(
                    clip_id_str, clip_start, clip_end, clip_pos,
                    watched.get("cut_source", cut_from_scenes),
                    label="scene description match",
                )
            return (
                f"Error: No match for {query!r} in this clip (index and scene descriptions); "
                "nothing was sliced."
            )

        nth = _parse_occurrence(occurrence, query)
        chosen = select_twelvelabs_match(
            items,
            clip_start=clip_start,
            clip_end=clip_end,
            occurrence=nth,
            cut_mode="mid",
        )
        if not chosen:
            sa_fb = None
            try:
                from classes.query import File
                fobj = File.get(id=file_id_str)
                if fobj and isinstance(fobj.data, dict):
                    raw = fobj.data.get("ai_metadata")
                    sa_fb = raw if isinstance(raw, dict) else None
            except Exception:
                pass
            cut_from_scenes = _scene_description_cut_source(
                clip_start, clip_end, sa_fb, query, nth,
            )
            if cut_from_scenes is not None:
                path, dur, cues = _lookup_watch_meta(file_id_str)
                watched = _watch_confirm_cut(
                    path, cut_from_scenes, cut_from_scenes, query,
                    fallback=cut_from_scenes, duration=dur, transcript_cues=cues,
                )
                return _slice_at_source_cut(
                    clip_id_str, clip_start, clip_end, clip_pos,
                    watched.get("cut_source", cut_from_scenes),
                    label="scene description match",
                )
            return (
                f"Error: The index found {query!r} elsewhere in the source video but not inside "
                "this clip's trimmed range; nothing was sliced."
            )

        ordinal_label = f"occurrence #{nth}" if nth > 0 else "best match"

        path, dur, cues = _lookup_watch_meta(file_id_str)
        try:
            win_s = float(chosen.get("start") if chosen.get("start") is not None else chosen["cut_source"])
        except (TypeError, ValueError, KeyError):
            win_s = float(chosen["cut_source"])
        try:
            win_e = float(chosen.get("end") if chosen.get("end") is not None else win_s)
        except (TypeError, ValueError):
            win_e = win_s
        watched = _watch_confirm_cut(
            path, win_s, win_e, query,
            fallback=chosen["cut_source"], duration=dur, transcript_cues=cues,
            fallback_in=win_s, fallback_out=win_e,
        )
        confirmed = watched.get("cut_source", chosen["cut_source"])

        fps = _get_app().project.get("fps") or {}
        fps_num = float(fps.get("num", 30))
        fps_den = float(fps.get("den", 1)) or 1.0
        # Never cut through a spoken line - snap to the nearer cue boundary.
        raw_cut, moved_off_cue = _snap_cut_off_speech(clip_id_str, float(confirmed))
        if moved_off_cue:
            ordinal_label += ", moved off speech"
        cut_source = snap_source_time_to_frame(raw_cut, fps_num, fps_den)
        slice_pos = snap_timeline_position(
            clip_pos + (cut_source - clip_start), fps_num, fps_den,
        )

        log.info(
            "slice_clip_at_best_match: clip_id=%s rank=%s overlap=%.3f "
            "cut_source=%.3f slice_pos=%.3f (%s)",
            clip_id_str,
            chosen.get("rank"),
            chosen.get("overlap_ratio", 0.0),
            cut_source,
            slice_pos,
            ordinal_label,
        )

        # ── 4. Validate: slice_pos must fall inside the clip on the timeline
        clip_timeline_end = clip_pos + (clip_end - clip_start)
        if slice_pos <= clip_pos or slice_pos >= clip_timeline_end:
            return (
                f"Error: Computed slice position ({slice_pos:.3f}s) is outside "
                f"the clip range [{clip_pos:.3f}s – {clip_timeline_end:.3f}s]."
            )

        # ── 5. Perform the slice on the Qt main thread
        from windows.views.timeline_backend.enums import MenuSlice

        slice_error_box = [None]

        def _do_slice():
            try:
                _get_app().window.timeline.Slice_Triggered(
                    MenuSlice.KEEP_BOTH, [clip_id_str], [], slice_pos,
                )
            except Exception as exc:
                slice_error_box[0] = str(exc)

        _run_on_main_thread(_do_slice)

        if slice_error_box[0]:
            return f"Error during slice: {slice_error_box[0]}"

        return f"Sliced at {_fmt_mmss(cut_source - clip_start)} ({ordinal_label})."
    except Exception as e:
        log.error("slice_clip_at_best_match failed: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

def _pause_auto_save():
    """Pause auto-save timer to prevent backup interference during generation."""
    result_box = [False]

    def _do():
        try:
            app = _get_app()
            app._generation_in_progress = True
            timer = getattr(app.window, "auto_save_timer", None)
            if timer and timer.isActive():
                timer.stop()
                result_box[0] = True
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do, timeout=10)
        else:
            _do()
    else:
        _do()
    return result_box[0]


def _resume_auto_save(was_active):
    """Resume auto-save timer if it was previously active."""

    def _clear_flag():
        try:
            _get_app()._generation_in_progress = False
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_clear_flag, timeout=10)
        else:
            _clear_flag()
    else:
        _clear_flag()

    if not was_active:
        return

    def _restart():
        try:
            app = _get_app()
            timer = getattr(app.window, "auto_save_timer", None)
            if timer:
                timer.start()
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_restart, timeout=10)
        else:
            _restart()
    else:
        _restart()


def _reencode_for_openshot(input_path, output_path=None, width=1920, height=1080):
    """Re-encode a video with a clean container so libopenshot can read it.

    AI-generated downloads often have missing/corrupt moov atoms, wrong
    timebases, or missing audio streams.  A quick re-encode with libx264
    + aac fixes all of that.

    Returns (output_path, None) on success, (None, error) on failure.
    """
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_clean{ext}"

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    ok, err = _ffmpeg_run(cmd)
    if not ok:
        # Try without audio (source may have no audio stream)
        cmd_no_audio = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        ok, err = _ffmpeg_run(cmd_no_audio)
        if not ok:
            return None, f"Re-encode failed: {err}"
    return output_path, None


def _ffprobe_video_size(path):
    """(width, height) of the first video stream, rounded down to even numbers; None when unknown."""
    try:
        p = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        w, h = (int(v) for v in (p.stdout or "").strip().splitlines()[0].split("x")[:2])
    except Exception:
        return None
    if w < 16 or h < 16:
        return None
    return _fit_generated_size(w, h)


def _fit_generated_size(w, h, long_edge=1920):
    """Keep the aspect, cap the long edge at 1920 (what the old fixed 1920x1080 box did for 16:9), even sizes."""
    scale = min(1.0, float(long_edge) / float(max(w, h)))
    w, h = int(round(w * scale)), int(round(h * scale))
    return max(16, w - (w % 2)), max(16, h - (h % 2))


def _ffprobe_pix_fmt(path) -> str:
    """Return primary video pix_fmt or empty string."""
    try:
        p = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        return (p.stdout or "").strip().lower()
    except Exception:
        return ""


def _ffprobe_alpha_mode(path) -> str:
    """Return stream alpha_mode / ALPHA_MODE tag (HyperFrames VP9 WebM) or empty."""
    try:
        p = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream_tags=alpha_mode,ALPHA_MODE",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        for line in (p.stdout or "").splitlines():
            val = line.strip().lower()
            if val:
                return val
        return ""
    except Exception:
        return ""


def _ffprobe_has_alpha(path) -> bool:
    """True when stream has alpha pix_fmt OR VP9 WebM ALPHA_MODE=1 (sidecar alpha)."""
    pix = _ffprobe_pix_fmt(path)
    if pix and (
        ("yuva" in pix)
        or pix.startswith("rgba")
        or pix.startswith("bgra")
        or pix.startswith("argb")
        or pix.startswith("abgr")
        or pix.startswith("gbra")
    ):
        return True
    # HyperFrames / libvpx WebM: ffprobe often reports yuv420p + ALPHA_MODE=1
    mode = _ffprobe_alpha_mode(path)
    return mode in ("1", "true", "yes")


def _ffprobe_has_explicit_yuva(path) -> bool:
    """True when primary pix_fmt is already yuva* (rare for libvpx WebM)."""
    pix = _ffprobe_pix_fmt(path)
    return bool(pix and "yuva" in pix)


def _verify_decoded_alpha_pixels(path, *, force_libvpx=None) -> bool:
    """Decode one frame and confirm some pixels are actually transparent.

    VP9 WebM: must force libvpx before -i (native VP9 decode drops alpha → solid
    black). qtrle/png/prores MOV: native decode preserves alpha — match OpenShot.
    Fail closed on ffmpeg errors or fully opaque frames.
    """
    import tempfile

    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if force_libvpx is None:
        force_libvpx = ext in (".webm", ".mkv")
    tmp_png = None
    try:
        fd, tmp_png = tempfile.mkstemp(suffix=".png", prefix="zenvi_alpha_")
        os.close(fd)
        cmd = ["ffmpeg", "-y"]
        if force_libvpx:
            cmd += ["-c:v", "libvpx-vp9"]
        cmd += [
            "-i", path,
            "-frames:v", "1",
            "-update", "1",
            "-pix_fmt", "rgba",
            tmp_png,
        ]
        ok, _err = _ffmpeg_run(cmd)
        if not ok or not os.path.isfile(tmp_png) or os.path.getsize(tmp_png) < 32:
            return False
        try:
            from PIL import Image

            im = Image.open(tmp_png).convert("RGBA")
            w, h = im.size
            if w < 1 or h < 1:
                return False
            samples = [
                im.getpixel((0, 0)),
                im.getpixel((w - 1, 0)),
                im.getpixel((0, h - 1)),
                im.getpixel((w - 1, h - 1)),
                im.getpixel((w // 2, h // 2)),
            ]
            return any(len(px) >= 4 and px[3] < 250 for px in samples)
        except Exception:
            try:
                from qt_api import QImage

                img = QImage(tmp_png)
                if img.isNull():
                    return False
                img = img.convertToFormat(QImage.Format_RGBA8888)
                w, h = img.width(), img.height()
                if w < 1 or h < 1:
                    return False
                pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, h // 2)]
                for x, y in pts:
                    c = img.pixelColor(x, y)
                    if c.alpha() < 250:
                        return True
                return False
            except Exception:
                return False
    finally:
        if tmp_png and os.path.isfile(tmp_png):
            try:
                os.remove(tmp_png)
            except OSError:
                pass


def _openshot_transparent_ok(path) -> bool:
    """True when OpenShot's native decoder will composite with real alpha.

    Requires alpha in the container pix_fmt (argb/rgba/yuva*) — typically qtrle
    MOV from `_reencode_alpha_for_openshot`. VP9 WebM with ALPHA_MODE=1 alone is
    NOT ok: libopenshot uses native VP9 which drops alpha to opaque black.
    """
    if not path:
        return False
    pix = _ffprobe_pix_fmt(path)
    if not pix:
        return False
    if not (
        ("yuva" in pix)
        or pix.startswith("rgba")
        or pix.startswith("bgra")
        or pix.startswith("argb")
        or pix.startswith("abgr")
        or pix.startswith("gbra")
    ):
        return False
    # Native decode path OpenShot uses — do not force libvpx
    return _verify_decoded_alpha_pixels(path, force_libvpx=False)


def _looks_like_alpha_video(path) -> bool:
    """HyperFrames transparent overlays are WebM (VP9+alpha); confirm others via ffprobe."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext == ".webm":
        probed = _ffprobe_has_alpha(path)
        if probed:
            return True
        # If probe fails (ffprobe missing), still treat .webm as alpha-intent for MG.
        return True
    return _ffprobe_has_alpha(path)


def _reencode_alpha_for_openshot(input_path, output_path=None, width=1920, height=1080):
    """Re-encode HyperFrames VP9 WebM into qtrle MOV so OpenShot keeps alpha.

    Must decode with libvpx-vp9 BEFORE -i (native VP9 drops alpha → solid black).
    Encode QuickTime Animation (qtrle + argb): OpenShot/libopenshot native decode
    preserves alpha. Do NOT leave as VP9 WebM — that looks transparent to libvpx
    probes but composites as opaque black in the editor.

    Returns (output_path, None) on success, (None, error) on failure.
    """
    if output_path is None:
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}_alpha.mov"
    # Always deliver .mov for OpenShot alpha overlays
    if not str(output_path).lower().endswith(".mov"):
        output_path = os.path.splitext(output_path)[0] + ".mov"

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,setsar=1,format=rgba"
    )
    # libvpx-vp9 before -i = VP9+alpha decoder; qtrle = OpenShot-safe alpha encoder
    cmd = [
        "ffmpeg", "-y",
        "-c:v", "libvpx-vp9",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "qtrle", "-pix_fmt", "argb",
        "-an",
        output_path,
    ]
    ok, err = _ffmpeg_run(cmd)
    if not ok:
        return None, f"Alpha re-encode failed: {err}"
    # Verify with NATIVE decode (what OpenShot does) — not libvpx
    if not _verify_decoded_alpha_pixels(output_path, force_libvpx=False):
        return None, (
            "Alpha re-encode produced opaque plate "
            "(no transparent pixels after native decode)"
        )
    return output_path, None


def _normalize_imported_file_path(file_obj, final_path):
    """Store an absolute path on imported File metadata (panel + thumbnails)."""
    if not file_obj:
        return
    final_path = _canonical_media_path(final_path)
    changed = False
    if file_obj.data.get("path") != final_path:
        file_obj.data["path"] = final_path
        changed = True
    reader = file_obj.data.get("reader")
    if isinstance(reader, dict) and reader.get("path") != final_path:
        reader["path"] = final_path
        changed = True
    if changed:
        file_obj.save()


def _refresh_imported_file_thumbnail(file_id, file_path):
    """Refresh the Project Files thumbnail of a re-encoded import (GUI thread).

    FileUpdated has the thumbnail worker regenerate it with a fresh cache, like
    any other file change. Decoding the frame here held the GUI thread for as
    long as libopenshot took to seek, a minute on long-GOP media.
    """
    if not file_id or not file_path:
        return
    try:
        _get_app().window.FileUpdated.emit(str(file_id))
    except Exception as exc:
        log.warning("_refresh_imported_file_thumbnail: could not refresh UI: %s", exc)


def _merge_baked_transition_metadata(
    file_a,
    start_a,
    end_a,
    file_b,
    start_b,
    end_b,
    seg_a_duration,
    morph_duration,
    prompt_hint="",
):
    """Merge tags/metadata for baked clip A + morph + B with segment-correct scene times."""
    from classes.ai_metadata_utils import collect_scene_descriptions_for_baked_segment

    tag_tokens = []
    seen_tags = set()
    list_keys = ("objects", "scenes", "activities", "mood")
    ai_merged = {
        "analyzed": True,
        "tags": {key: [] for key in list_keys},
    }
    seen_lists = {key: set() for key in list_keys}
    descriptions = []
    scene_descriptions = []

    def _absorb_file_tags(file_obj):
        if not file_obj or not isinstance(getattr(file_obj, "data", None), dict):
            return
        for part in str(file_obj.data.get("tags") or "").split(","):
            token = part.strip()
            if not token:
                continue
            norm = token.lower()
            if norm in seen_tags:
                continue
            seen_tags.add(norm)
            tag_tokens.append(token)

    def _absorb_ai_lists(ai):
        if not isinstance(ai, dict):
            return
        tags = ai.get("tags")
        if not isinstance(tags, dict):
            return
        for key in list_keys:
            vals = tags.get(key) or []
            if not isinstance(vals, list):
                continue
            for val in vals:
                text = str(val).strip()
                if not text:
                    continue
                norm = text.lower()
                if norm in seen_lists[key]:
                    continue
                seen_lists[key].add(norm)
                ai_merged["tags"][key].append(text)
        desc = ai.get("description")
        if desc and str(desc).strip():
            descriptions.append(str(desc).strip())

    _absorb_file_tags(file_a)
    _absorb_file_tags(file_b)

    ai_a = file_a.data.get("ai_metadata") if file_a and isinstance(file_a.data, dict) else None
    ai_b = file_b.data.get("ai_metadata") if file_b and isinstance(file_b.data, dict) else None

    if isinstance(ai_a, dict):
        _absorb_ai_lists(ai_a)
        scene_descriptions.extend(
            collect_scene_descriptions_for_baked_segment(
                ai_a, start_a, end_a, 0.0,
            )
        )

    clip_b_offset = max(0.0, float(seg_a_duration)) + max(0.0, float(morph_duration))
    if isinstance(ai_b, dict):
        _absorb_ai_lists(ai_b)
        scene_descriptions.extend(
            collect_scene_descriptions_for_baked_segment(
                ai_b, start_b, end_b, clip_b_offset,
            )
        )

    hint = str(prompt_hint or "").strip()
    if hint:
        scene_descriptions.append({
            "time": max(0.0, float(seg_a_duration)) + max(0.1, float(morph_duration) * 0.5),
            "description": hint[:500],
        })

    scene_descriptions.sort(key=lambda item: float(item.get("time", 0) or 0))

    if descriptions:
        ai_merged["description"] = " | ".join(descriptions[:3])
    elif scene_descriptions:
        ai_merged["description"] = " ".join(
            str(s.get("description", "")).strip()
            for s in scene_descriptions[:6]
            if str(s.get("description", "")).strip()
        )

    if scene_descriptions:
        ai_merged["scene_descriptions"] = scene_descriptions[:24]

    return ", ".join(tag_tokens), ai_merged


def _merge_file_tags_and_metadata(*file_objs):
    """Merge comma-separated tags and ai_metadata tag lists from File objects."""
    tag_tokens = []
    seen_tags = set()
    list_keys = ("objects", "scenes", "activities", "mood")
    ai_merged = {
        "analyzed": True,
        "tags": {key: [] for key in list_keys},
    }
    seen_lists = {key: set() for key in list_keys}
    descriptions = []
    scene_descriptions = []

    for file_obj in file_objs:
        if not file_obj or not isinstance(getattr(file_obj, "data", None), dict):
            continue
        data = file_obj.data
        for part in str(data.get("tags") or "").split(","):
            token = part.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen_tags:
                continue
            seen_tags.add(key)
            tag_tokens.append(token)

        ai = data.get("ai_metadata")
        if not isinstance(ai, dict):
            continue
        tags = ai.get("tags")
        if isinstance(tags, dict):
            for key in list_keys:
                vals = tags.get(key) or []
                if not isinstance(vals, list):
                    continue
                for val in vals:
                    text = str(val).strip()
                    if not text:
                        continue
                    norm = text.lower()
                    if norm in seen_lists[key]:
                        continue
                    seen_lists[key].add(norm)
                    ai_merged["tags"][key].append(text)
        desc = ai.get("description")
        if desc and str(desc).strip():
            descriptions.append(str(desc).strip())
        for scene in ai.get("scene_descriptions") or []:
            if isinstance(scene, dict) and scene.get("description"):
                scene_descriptions.append(scene)

    if descriptions:
        ai_merged["description"] = " | ".join(descriptions[:3])
    if scene_descriptions:
        ai_merged["scene_descriptions"] = scene_descriptions[:24]

    return ", ".join(tag_tokens), ai_merged


def _clip_source_range(clip_data, file_data, fallback_duration=0.0):
    """Return (start, end) source trim range for a timeline clip."""
    start = float(clip_data.get("start", 0) or 0)
    end = float(clip_data.get("end", 0) or 0)
    if end <= start:
        file_dur = float((file_data or {}).get("duration", 0) or 0)
        end = file_dur if file_dur > start else start + max(0.1, float(fallback_duration or 0.1))
    return start, end


def _bake_transition_video(
    path_a,
    start_a,
    end_a,
    morph_path,
    path_b,
    start_b,
    end_b,
    width,
    height,
    output_path,
):
    """Concatenate clip A + AI morph + clip B into one MP4."""
    dur_a = max(0.01, end_a - start_a)
    dur_b = max(0.01, end_b - start_b)
    morph_dur = _ffprobe_video_duration(morph_path)
    if morph_dur < 0.1:
        morph_dur = 5.0

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps=24"
    )
    video_filters = (
        f"[0:v]trim=start={start_a}:end={end_a},setpts=PTS-STARTPTS,{vf}[va];"
        f"[1:v]setpts=PTS-STARTPTS,{vf}[vb];"
        f"[2:v]trim=start={start_b}:end={end_b},setpts=PTS-STARTPTS,{vf}[vc];"
        f"[va][vb][vc]concat=n=3:v=1:a=0[vout]"
    )

    has_audio_a = _ffprobe_has_audio(path_a)
    has_audio_m = _ffprobe_has_audio(morph_path)
    has_audio_b = _ffprobe_has_audio(path_b)
    want_audio = has_audio_a or has_audio_m or has_audio_b

    if want_audio:
        def _audio_filter(input_idx, has_audio, trim_start=None, trim_end=None, null_dur=0.0):
            if has_audio and trim_start is not None and trim_end is not None:
                return (
                    f"[{input_idx}:a]atrim=start={trim_start}:end={trim_end},"
                    f"asetpts=PTS-STARTPTS"
                )
            if has_audio:
                return f"[{input_idx}:a]asetpts=PTS-STARTPTS"
            return (
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=start=0:end={null_dur},asetpts=PTS-STARTPTS"
            )

        audio_filters = (
            f"{_audio_filter(0, has_audio_a, start_a, end_a, dur_a)}[aa];"
            f"{_audio_filter(1, has_audio_m, null_dur=morph_dur)}[ab];"
            f"{_audio_filter(2, has_audio_b, start_b, end_b, dur_b)}[ac];"
            f"[aa][ab][ac]concat=n=3:v=0:a=1[aout]"
        )
        filter_complex = f"{video_filters};{audio_filters}"
        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", morph_path, "-i", path_b,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", morph_path, "-i", path_b,
            "-filter_complex", video_filters,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    return _ffmpeg_run(cmd)


# ---------------------------------------------------------------------------
# Installing a baked AI clip in place of the originals (modify_clip / morph)
# ---------------------------------------------------------------------------
# All three run in ONE main-thread hop inside the calling tool's transaction, so
# the import, the deletes, the new clip and any ripple are a single undo step.
# The originals are re-read by id in that hop: the generation took minutes and
# the person may have moved (or removed) them meanwhile.

_BAKE_EPS = 1e-3


def _timeline_span(data):
    pos = float(data.get("position") or 0.0)
    return pos, pos + max(0.0, float(data.get("end") or 0.0) - float(data.get("start") or 0.0))


def _bake_receipt(summary, **receipt):
    return " ".join(str(summary).split()) + "\n" + json.dumps(receipt, separators=(",", ":"), default=str)


def _delete_timeline_clips(win, clip_ids):
    from classes.query import Clip
    for cid in clip_ids:
        clip = Clip.get(id=cid)
        if not clip:
            continue
        if hasattr(win, "removeSelection"):
            try:
                win.removeSelection(cid, "clip")
            except Exception as exc:
                log.debug("could not clear selection of %s: %s", cid, exc)
        clip.delete()


def _place_baked_clip(win, file_id, position, layer, length=None):
    """Timeline.addClip (the drop path), optionally trimmed to *length* seconds; returns the saved clip data."""
    from classes.query import Clip
    if QPointF is None:
        from qt_api import QPointF as _QPointF
    else:
        _QPointF = QPointF
    new_clip = win.timeline.addClip(file_id, _QPointF(float(position), 0.0), int(layer), call_manual_move=False)
    if not isinstance(new_clip, dict) or not new_clip.get("id"):
        raise RuntimeError("the baked clip could not be placed on the timeline")
    if length is not None:
        start = float(new_clip.get("start") or 0.0)
        new_clip["end"] = start + float(length)
        new_clip["duration"] = float(length)
        win.timeline.update_clip_data(new_clip, only_basic_props=False, ignore_refresh=False)
    saved = Clip.get(id=new_clip["id"])
    return saved.data if saved else new_clip


def _shift_layer_items(layer, from_position, delta, exclude_ids=(), until=None):
    """Move clips and transitions on *layer* starting at/after *from_position* (and before *until*) by *delta*."""
    from classes.query import Clip, Transition
    app = _get_app()
    moved = []
    for kind, key in ((Clip, "clips"), (Transition, "effects")):
        for item in list(kind.filter(layer=int(layer))):
            if item.id in exclude_ids:
                continue
            pos = float(item.data.get("position") or 0.0)
            if pos < float(from_position) - _BAKE_EPS:
                continue
            if until is not None and pos >= float(until) - _BAKE_EPS:
                continue
            app.updates.update([key, {"id": item.id}], {"position": max(0.0, pos + float(delta))})
            moved.append(item.id)
    return moved


def _install_baked_insert(clip_id, baked_file_id, insert_timeline_offset):
    """modify_clip insert: the baked file (A + AI insert + C) replaces the clip; later items on its track make room."""
    from classes.query import Clip
    win = _get_app().window
    orig = Clip.get(id=clip_id)
    if not orig:
        raise RuntimeError(f"clip {clip_id} is no longer on the timeline (the new footage is in Project Files, "
                           f"file_id={baked_file_id})")
    layer = int(orig.data.get("layer") or 0)
    pos, orig_end = _timeline_span(orig.data)
    _delete_timeline_clips(win, [clip_id])
    new = _place_baked_clip(win, baked_file_id, pos, layer)
    new_pos, new_end = _timeline_span(new)
    growth = new_end - orig_end
    moved = []
    if growth > _BAKE_EPS:
        moved = _shift_layer_items(layer, pos + float(insert_timeline_offset), growth, exclude_ids={new["id"]})
    return {"timeline_clip_id": new["id"], "file_id": baked_file_id, "replaced": [clip_id], "layer": layer,
            "position": round(new_pos, 3), "end": round(new_end, 3), "moved_later_items": moved,
            "moved_by": round(growth, 3) if moved else 0.0}


def _install_baked_head(clip_id, new_file_id, replaced_source_seconds, generated_length):
    """modify_clip replace: the AI edit replaces the clip's first N seconds; the rest of the clip continues after it."""
    from classes.query import Clip
    win = _get_app().window
    orig = Clip.get(id=clip_id)
    if not orig:
        raise RuntimeError(f"clip {clip_id} is no longer on the timeline (the edited footage is in Project Files, "
                           f"file_id={new_file_id})")
    layer = int(orig.data.get("layer") or 0)
    pos, _orig_end = _timeline_span(orig.data)
    start = float(orig.data.get("start") or 0.0)
    end = float(orig.data.get("end") or 0.0)
    fps = _get_app().project.get("fps") or {}
    frame = float(fps.get("den", 1) or 1) / float(fps.get("num", 30) or 30)
    placed = max(frame, min(float(generated_length), float(replaced_source_seconds)))
    remainder = end - (start + float(replaced_source_seconds))
    kept = None
    if remainder > frame:
        orig.data["start"] = start + float(replaced_source_seconds)
        orig.data["position"] = pos + placed
        orig.save()
        kept = clip_id
    else:
        _delete_timeline_clips(win, [clip_id])
    new = _place_baked_clip(win, new_file_id, pos, layer, length=placed)
    new_pos, new_end = _timeline_span(new)
    return {"timeline_clip_id": new["id"], "file_id": new_file_id, "layer": layer, "position": round(new_pos, 3),
            "end": round(new_end, 3), "replaced": [] if kept else [clip_id], "rest_of_original": kept,
            "rest_starts_at": round(pos + placed, 3) if kept else None}


def _install_baked_morph(clip_a_id, clip_b_id, baked_file_id):
    """generate_transition_clip: A + morph + B replaces A, B and the transitions at their cut."""
    from classes.query import Clip, Transition
    win = _get_app().window
    a, b = Clip.get(id=clip_a_id), Clip.get(id=clip_b_id)
    if not a or not b:
        raise RuntimeError("clip A or B is no longer on the timeline (the baked clip is in Project Files, "
                           f"file_id={baked_file_id})")
    layer = int(a.data.get("layer") or 0)
    pos_a, a_end = _timeline_span(a.data)
    pos_b, b_end = _timeline_span(b.data)
    lo, hi = min(a_end, pos_b), max(a_end, pos_b)
    junction = []
    for t in Transition.filter(layer=layer):
        t0, t1 = _timeline_span(t.data)
        if t0 <= hi + _BAKE_EPS and t1 >= lo - _BAKE_EPS:
            junction.append(t.id)
    for tid in junction:
        tr = Transition.get(id=tid)
        if tr:
            tr.delete()
    _delete_timeline_clips(win, [clip_a_id, clip_b_id])
    new = _place_baked_clip(win, baked_file_id, pos_a, layer)
    new_pos, new_end = _timeline_span(new)
    shift = new_end - b_end            # = morph length - gap between A and B
    moved = []
    if shift > _BAKE_EPS:
        moved = _shift_layer_items(layer, pos_b, shift, exclude_ids={new["id"]})
    elif shift < -_BAKE_EPS:
        # B's content starts earlier inside the baked clip: its own fades follow it; later clips stay put.
        moved = _shift_layer_items(layer, pos_b, shift, exclude_ids={new["id"]}, until=b_end)
    return {"timeline_clip_id": new["id"], "file_id": baked_file_id, "replaced": [clip_a_id, clip_b_id],
            "removed_transitions": junction, "layer": layer, "position": round(new_pos, 3),
            "end": round(new_end, 3), "moved_later_items": moved, "moved_by": round(shift, 3) if moved else 0.0}


def _check_bake_target(app, clip_obj):
    """Before spending credits: the clip's track must accept edits."""
    try:
        layer = int((clip_obj.data or {}).get("layer") or 0)
    except (TypeError, ValueError, AttributeError):
        return ""
    return _locked_track_error(app, layer)


def _check_morph_pair(app, clip_a, clip_b):
    """(error, clip_a, clip_b): the pair must sit on one unlocked track with nothing between them."""
    from classes.query import Clip
    la, lb = int(clip_a.data.get("layer") or 0), int(clip_b.data.get("layer") or 0)
    if clip_a.id == clip_b.id:
        return "Error: clip A and clip B are the same clip.", clip_a, clip_b
    if la != lb:
        return ("Error: the two clips are on different tracks; a morph transition joins neighbouring clips on "
                "one track.", clip_a, clip_b)
    if _timeline_span(clip_b.data)[0] < _timeline_span(clip_a.data)[0]:
        clip_a, clip_b = clip_b, clip_a
    locked = _locked_track_error(app, la)
    if locked:
        return locked, clip_a, clip_b
    a_end = _timeline_span(clip_a.data)[1]
    pos_b = _timeline_span(clip_b.data)[0]
    lo, hi = min(a_end, pos_b), max(a_end, pos_b)
    between = [c.id for c in Clip.filter(layer=la) if c.id not in (clip_a.id, clip_b.id)
               and _timeline_span(c.data)[0] < hi - _BAKE_EPS and _timeline_span(c.data)[1] > lo + _BAKE_EPS]
    if between:
        return (f"Error: clip(s) {', '.join(between)} sit between the two clips; pick neighbouring clips.",
                clip_a, clip_b)
    return "", clip_a, clip_b


def _import_generated_video(video_path, *, preserve_alpha=None):
    """Import a generated video into the project with clean metadata.

    Re-encodes the video first to a permanent location (via
    _output_path_for_generated_video), then adds it using skip_indexing=True
    to avoid nested event loops and metadata corruption.

    When preserve_alpha is True (or auto-detected for WebM), re-encodes to
    qtrle MOV (argb) so OpenShot's native decoder keeps transparency.
    VP9 WebM is never imported as-is — native VP9 drops alpha to solid black.
    Alpha failure is fail-closed — never silent yuv420p/MP4 fallback for overlays.

    Returns (File object, None) on success, (None, error_string) on failure.
    """
    from classes.query import File

    want_alpha = bool(preserve_alpha) if preserve_alpha is not None else _looks_like_alpha_video(video_path)
    # Keep the source's own frame size (a 9:16 generation must stay 9:16, not be
    # pillarboxed into 1920x1080); fall back to 1080p when it cannot be probed.
    out_w, out_h = _ffprobe_video_size(video_path) or (1920, 1080)

    if want_alpha:
        perm_path = _canonical_media_path(_output_path_for_generated_video(ext=".mov"))
        # Always re-encode through libvpx→qtrle. Even "good" WebM composites black
        # in OpenShot because FFmpegReader uses the native VP9 decoder.
        clean_path, err = _reencode_alpha_for_openshot(
            video_path, output_path=perm_path, width=out_w, height=out_h)
        if err:
            return None, f"alpha import failed (no opaque fallback): {err}"
    else:
        perm_path = _canonical_media_path(_output_path_for_generated_video(ext=".mp4"))
        clean_path, err = _reencode_for_openshot(video_path, output_path=perm_path, width=out_w, height=out_h)
        if err:
            log.warning("Re-encode failed, using original: %s", err)
            # Copy scratch/original into the durable destination before import so
            # caller scratch cleanup cannot delete the only project copy.
            try:
                if os.path.abspath(video_path) != os.path.abspath(perm_path):
                    import shutil
                    os.makedirs(os.path.dirname(perm_path), exist_ok=True)
                    shutil.copy2(video_path, perm_path)
                    clean_path = perm_path
                else:
                    clean_path = video_path
            except Exception as copy_err:
                return None, (
                    "re-encode failed (%s) and could not copy original: %s"
                    % (err, copy_err)
                )

    final_path = _canonical_media_path(clean_path)

    # Import into project on the main thread (quiet: no modal box for an unreadable file)
    def _do_import():
        _get_app().window.files_model.add_files([final_path], quiet=True, skip_indexing=True)
    _run_on_main_thread(_do_import, timeout=30)

    def _find_and_normalize():
        # Look up the File object
        found = File.get(path=final_path)
        if not found:
            found = File.get(path=os.path.normpath(final_path))
        if not found:
            found = File.get(path=os.path.realpath(final_path))
        if not found:
            for candidate in File.filter():
                try:
                    if getattr(candidate, "absolute_path", None) and candidate.absolute_path() == final_path:
                        found = candidate
                        break
                except Exception:
                    continue
        if found:
            _normalize_imported_file_path(found, final_path)   # saves: GUI thread
            _refresh_imported_file_thumbnail(found.id, final_path)
        return found

    f = _run_on_main_thread(_find_and_normalize, timeout=30)
    if not f:
        return None, f"the re-encoded file could not be imported ({final_path})"
    return f, None


def _stamp_generated_video_metadata(file_obj, prompt=""):
    """Agent-facing metadata for an AI-generated clip — does not enqueue Gemini.

    Generated media is imported with skip_indexing=True, so without this the
    scene panel and clip search have nothing to show for the clip the agent
    just made. The generation prompt is the summary.

    Returns False when the metadata could not be saved, True otherwise.
    """
    summary = (prompt or "").strip()
    if not file_obj or not summary:
        return True
    try:
        tags = file_obj.data.get("tags") if isinstance(file_obj.data, dict) else None
        if isinstance(tags, str):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tag_list = [str(t).strip() for t in tags if str(t).strip()]
        else:
            tag_list = []
        if "ai_generated" not in tag_list:
            tag_list.append("ai_generated")
        file_obj.data["tags"] = ", ".join(tag_list)

        ai = file_obj.data.get("ai_metadata")
        if not isinstance(ai, dict):
            ai = {}
        ai["short_summary"] = summary[:400]
        ai["description"] = summary[:400]
        # analyzed=True so get_effective_ai_metadata / Scene panel show the text.
        ai["analyzed"] = True
        ai["source"] = "ai_video_generation"
        file_obj.data["ai_metadata"] = ai
        if not file_obj.data.get("name"):
            file_obj.data["name"] = summary[:120]

        def _save():
            # Project listeners update Qt models: save on the GUI thread.
            file_obj.save()
            try:
                _get_app().window.FileUpdated.emit(str(file_obj.id))
            except Exception:
                pass

        _run_on_main_thread(_save)
        return True
    except Exception as exc:
        log.warning("Could not stamp generated-video metadata: %s", exc)
        return False


def _download_motion_graphics_file(url, default_name="motion_segment.mp4"):
    """Download a Supabase video to a fresh temp path. Returns (dest_path, size_mb)."""
    import tempfile
    import urllib.request
    from urllib.parse import unquote

    url_path = url.split("?")[0].rstrip("/")
    raw_name = unquote(url_path.split("/")[-1] or default_name)
    root, ext = os.path.splitext(raw_name)
    if ext.lower() not in (".mp4", ".webm", ".mov", ".mkv", ".avi"):
        # Infer from URL path fragments
        lower = url_path.lower()
        if lower.endswith(".webm") or "/output.webm" in lower:
            ext = ".webm"
        else:
            ext = ".mp4"
        raw_name = f"{root or 'motion_segment'}{ext}"

    tmp_dir = tempfile.mkdtemp(prefix="zenvi_hyperframes_")
    dest_path = os.path.join(tmp_dir, raw_name)

    log.info("Downloading HyperFrames video from Supabase: %s → %s", url, dest_path)
    req = urllib.request.Request(url, headers={"User-Agent": "ZenviApp/1.0"})
    with urllib.request.urlopen(req, timeout=300) as response, open(dest_path, "wb") as out:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            out.write(chunk)

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    size_bytes = os.path.getsize(dest_path)
    if size_bytes <= 0:
        raise ValueError(f"Downloaded file is empty (0 bytes): {dest_path}")
    if size_mb < 0.1:
        log.info("Download complete: %s (%.0f KB)", dest_path, size_bytes / 1024.0)
    else:
        log.info("Download complete: %s (%.1f MB)", dest_path, size_mb)
    return dest_path, size_mb


def _stamp_motion_graphics_file_metadata(file_obj, label="", transparent=None):
    """Agent-facing metadata only — does not enqueue Gemini indexing."""
    if not file_obj:
        return
    summary = (label or "").strip()
    if not summary:
        log.warning("MG stamp refused empty summary — using generic placeholder")
        summary = "HyperFrames motion graphic"
    try:
        tags = file_obj.data.get("tags") if isinstance(file_obj.data, dict) else None
        if isinstance(tags, str):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tag_list = [str(t).strip() for t in tags if str(t).strip()]
        else:
            tag_list = []
        if "motion_graphics" not in tag_list:
            tag_list.append("motion_graphics")
        if transparent and "transparent_overlay" not in tag_list:
            tag_list.append("transparent_overlay")
        file_obj.data["tags"] = ", ".join(tag_list)

        ai = file_obj.data.get("ai_metadata")
        if not isinstance(ai, dict):
            ai = {}
        ai["short_summary"] = summary
        # Mirror AI-gen style: description carries the same human text for panels/search
        ai["description"] = summary
        # Must be True so get_effective_ai_metadata / Scene panel show the summary
        # (skip_indexing still avoids Gemini — agent authored this text).
        ai["analyzed"] = True
        ai["source"] = "hyperframes_motion_graphics"
        if transparent is not None:
            ai["transparent"] = bool(transparent)
        file_obj.data["ai_metadata"] = ai
        # Prefer a readable title in the media bin (like generated clips)
        if summary and summary != "HyperFrames motion graphic":
            short_title = summary.split("(")[0].strip()
            if short_title and len(short_title) <= 120:
                file_obj.data["name"] = short_title[:120]
        elif not file_obj.data.get("name"):
            file_obj.data["name"] = "HyperFrames motion graphic"
        file_obj.save()
        try:
            _get_app().window.FileUpdated.emit(str(file_obj.id))
        except Exception:
            pass
    except Exception as exc:
        log.warning("Could not stamp motion-graphics metadata: %s", exc)


def _resolve_motion_graphics_label_from_job(render_job_id="", fallback=""):
    """When the agent omits label=, recover summary from HyperFrames job status.

    Returns (label, job_meta_dict).
    """
    job_id = (render_job_id or "").strip()
    if not job_id:
        return (fallback or "").strip(), {}
    import json
    import urllib.request

    api = os.environ.get(
        "HYPERFRAMES_URL",
        os.environ.get("REMOTION_URL", "http://localhost:4500/api/v1"),
    ).rstrip("/")
    for kind in ("motion", "demo"):
        try:
            req = urllib.request.Request(
                f"{api}/{kind}/jobs/{job_id}",
                headers={"User-Agent": "ZenviApp/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode() or "{}")
            summary = str(data.get("summary") or "").strip()
            if summary:
                return summary, data
            titles = data.get("segment_titles") or []
            block_id = data.get("block_id") or ""
            if titles or block_id:
                title0 = titles[0] if titles else ""
                transparent = data.get("transparent")
                bits = []
                if block_id:
                    bits.append(f"HyperFrames {block_id}")
                else:
                    bits.append("HyperFrames motion graphic")
                if title0 and title0 not in ("motion", block_id):
                    bits.append(str(title0))
                if transparent:
                    bits.append("transparent overlay")
                label = ": ".join(bits[:2]) + (f" ({bits[2]})" if len(bits) > 2 else "")
                return label, data
            return (fallback or "").strip(), data
        except Exception as exc:
            log.debug("MG label lookup %s/%s failed: %s", kind, job_id, exc)
    return (fallback or "").strip(), {}


def _download_and_import_one(url, label="", job_transparent=None):
    """Download one Supabase video and import it as a project file (skip_indexing).

    Returns (file_id, size_mb, error, transparent_ok, pix_fmt).
    job_transparent: when True, force alpha-preserving import and fail closed (no opaque MP4).
    """
    try:
        last_err = None
        dest_path = None
        size_mb = 0.0
        for attempt in (1, 2):
            try:
                dest_path, size_mb = _download_motion_graphics_file(url)
                break
            except Exception as e:
                last_err = e
                log.warning("Download attempt %d failed for %s: %s", attempt, url, e)
        if dest_path is None:
            return "", 0.0, f"download failed: {last_err}", False, ""

        try:
            if job_transparent is True:
                preserve_alpha = True
            elif job_transparent is False:
                preserve_alpha = False
            else:
                preserve_alpha = _looks_like_alpha_video(dest_path)

            f, err = _import_generated_video(dest_path, preserve_alpha=preserve_alpha)
            if err:
                return "", size_mb, f"import failed: {err}", False, ""
        finally:
            _cleanup_scratch_parent(dest_path, "zenvi_hyperframes_")

        imported_path = None
        try:
            imported_path = f.absolute_path() if f and hasattr(f, "absolute_path") else None
        except Exception:
            imported_path = None
        if not imported_path and f and isinstance(getattr(f, "data", None), dict):
            imported_path = f.data.get("path")

        pix_fmt = _ffprobe_pix_fmt(imported_path) if imported_path else ""
        alpha_mode = _ffprobe_alpha_mode(imported_path) if imported_path else ""
        # libvpx VP9 WebM probes as yuv420p + ALPHA_MODE=1 (never yuva*). Accept that
        # when decoded pixels actually have transparency.
        transparent_ok = bool(imported_path and _openshot_transparent_ok(imported_path))
        stamp_transparent = (
            bool(job_transparent) if job_transparent is not None else transparent_ok
        )
        probe_note = f"pix_fmt={pix_fmt or 'unknown'} alpha_mode={alpha_mode or 'none'}"
        if job_transparent and not transparent_ok:
            return (
                "",
                size_mb,
                (
                    f"transparent job imported without usable VP9 alpha ({probe_note}) "
                    "— re-compose as WebM; refusing solid plate"
                ),
                False,
                pix_fmt,
            )

        _stamp_motion_graphics_file_metadata(f, label=label, transparent=stamp_transparent)
        # Encode probe bits into pix_fmt field for fetch messaging: "yuva420p;alpha_mode=1"
        probe_field = pix_fmt or "unknown"
        if alpha_mode:
            probe_field = f"{probe_field};alpha_mode={alpha_mode}"
        return (f.id if f else ""), size_mb, None, transparent_ok or stamp_transparent, probe_field
    except Exception as e:
        log.error("download/import failed for %s: %s", url, e, exc_info=True)
        return "", 0.0, str(e), False, ""


def _motion_graphics_cleanup_storage(supabase_path="", render_job_id=""):
    """Best-effort DELETE {HYPERFRAMES_URL}/cleanup. Non-critical — failures are logged only."""
    import json
    import urllib.request

    if not (supabase_path or render_job_id):
        return
    api = os.environ.get(
        "HYPERFRAMES_URL",
        os.environ.get("REMOTION_URL", "http://localhost:4500/api/v1"),
    ).rstrip("/")
    try:
        payload = {}
        if supabase_path:
            payload["supabase_path"] = supabase_path
        if render_job_id:
            payload["job_id"] = render_job_id
        body = json.dumps(payload).encode()
        cleanup_req = urllib.request.Request(
            f"{api}/cleanup",
            data=body,
            method="DELETE",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(cleanup_req, timeout=60) as cleanup_resp:
            log.info("Supabase cleanup after import: %s", cleanup_resp.read().decode()[:500])
    except Exception as cleanup_err:
        log.warning("Supabase cleanup failed (non-critical): %s", cleanup_err)


# Session-scoped URL → file_id so package/hand re-fetch is idempotent.
_MG_IMPORTED_URLS: dict = {}


def fetch_motion_graphics_video(
    segment_urls=None,
    supabase_url="",
    supabase_path="",
    render_job_id="",
    label="",
    **_kw,
) -> str:
    """Import rendered HyperFrames motion/demo segments into the project files panel.

    Preferred: pass segment_urls — the ordered list of per-segment Supabase URLs.
    Each segment is imported as its own clip; storage is cleaned up once after success.
    Imports use skip_indexing=True (no Gemini summarize). Optional label stamps short_summary.

    Legacy: pass a single supabase_url to import one video.
    """
    import json
    import re

    global _MG_IMPORTED_URLS

    _GENERIC_LABELS = (
        "hyperframes motion graphic",
        "motion graphic",
        "motion",
    )

    def _norm_url(u: str) -> str:
        return (u or "").strip().split("?")[0].rstrip("/")

    def _already_imported(urls: list):
        ids = []
        for u in urls:
            fid = _MG_IMPORTED_URLS.get(_norm_url(u))
            if not fid:
                return None
            ids.append(fid)
        if not ids:
            return None
        return (
            f"Already imported file_id={ids[0]} (file_ids: {ids}) — "
            "place only if missing. Do NOT re-download these URLs."
        )

    def _is_generic(text: str) -> bool:
        return (text or "").strip().lower() in _GENERIC_LABELS

    job_meta = {}
    stamp_label = (label or "").strip()
    recovered = ""
    if render_job_id:
        recovered, job_meta = _resolve_motion_graphics_label_from_job(
            render_job_id, fallback=stamp_label
        )
        # Prefer explicit agent label; else job.summary; never keep generic when job has better text
        if not stamp_label:
            stamp_label = recovered
        elif _is_generic(stamp_label) and recovered and not _is_generic(recovered):
            stamp_label = recovered
    if not stamp_label:
        stamp_label = "HyperFrames motion graphic"

    job_transparent = job_meta.get("transparent")
    if job_transparent is not None:
        job_transparent = bool(job_transparent)

    generic = _is_generic(stamp_label)
    generic_warn = ""
    if generic:
        if render_job_id and job_meta.get("summary"):
            # Job had summary but we somehow still stamped generic — surface loudly
            generic_warn = (
                " ⚠️ short_summary is still generic despite job.summary — "
                "pass label= from compose suggested_label."
            )
        else:
            generic_warn = (
                " ⚠️ short_summary is generic — pass label= from compose suggested_label "
                "(or ensure render_job_id is set so job.summary can be recovered)."
            )

    # The LLM may pass segment_urls as a JSON-encoded string.
    if isinstance(segment_urls, str):
        try:
            segment_urls = json.loads(segment_urls)
        except Exception:
            segment_urls = [segment_urls]
    segment_urls = [u.strip() for u in (segment_urls or []) if isinstance(u, str) and u.strip()]

    # Prefer WebM when job is transparent but URLs still point at .mp4
    if job_transparent and segment_urls:
        fixed = []
        for u in segment_urls:
            if u.split("?")[0].lower().endswith(".mp4"):
                webm = re.sub(r"\.mp4(\?|$)", r".webm\1", u, count=1, flags=re.I)
                log.warning(
                    "Job %s is transparent but URL is MP4 — trying WebM: %s",
                    render_job_id,
                    webm,
                )
                fixed.append(webm)
            else:
                fixed.append(u)
        segment_urls = fixed

    if segment_urls:
        cached = _already_imported(segment_urls)
        if cached:
            return cached

    # ---- Multi-segment import (preferred) ----
    if segment_urls:
        # Deterministic timeline order: sort by the numeric index in segment_NN.mp4,
        # independent of the order the agent passed the URLs in.
        def _seg_index(u):
            name = u.split("?")[0].rsplit("/", 1)[-1]
            m = re.search(r"segment[_-]?(\d+)", name, re.IGNORECASE)
            return int(m.group(1)) if m else 1_000_000

        ordered = sorted(enumerate(segment_urls), key=lambda iu: (_seg_index(iu[1]), iu[0]))

        file_ids = []
        failures = []  # (original_index, url, error)
        total_mb = 0.0
        any_transparent_ok = False
        last_pix_fmt = ""
        for orig_i, url in ordered:
            path_base = url.lower().split("?")[0]
            if job_transparent and path_base.endswith(".mp4"):
                failures.append(
                    (
                        orig_i,
                        url,
                        "transparent job URL is .mp4 and WebM rewrite failed — "
                        "re-compose as WebM; refusing opaque MP4 import",
                    )
                )
                continue

            file_id, size_mb, err, transparent_ok, pix_fmt = _download_and_import_one(
                url, label=stamp_label, job_transparent=job_transparent
            )
            # Fail-closed: never import opaque MP4 as success for transparent jobs
            if err and job_transparent and path_base.endswith(".webm"):
                failures.append(
                    (
                        orig_i,
                        url,
                        f"{err} — re-compose WebM (no opaque MP4 fallback)",
                    )
                )
                log.warning("Segment %d transparent WebM import failed (no MP4 fallback): %s", orig_i, err)
                continue
            if err:
                failures.append((orig_i, url, err))
                log.warning("Segment %d import failed: %s", orig_i, err)
            else:
                file_ids.append(file_id)
                total_mb += size_mb
                any_transparent_ok = any_transparent_ok or bool(transparent_ok)
                if pix_fmt:
                    last_pix_fmt = pix_fmt
                _MG_IMPORTED_URLS[_norm_url(url)] = file_id

        n = len(segment_urls)
        if failures:
            # Leave storage intact so the failed segments can be re-fetched without a re-render.
            failed_lines = "\n".join(f"  [{i}] {u} ({e})" for i, u, e in failures)
            return (
                f"Error: Imported only {len(file_ids)}/{n} motion segments; {len(failures)} failed. "
                f"Storage was NOT cleaned up so you can retry the failed ones. "
                f"file_ids: {file_ids}\nFailed segments:\n{failed_lines}{generic_warn}"
            )

        # All segments imported — clean up Supabase storage once.
        _motion_graphics_cleanup_storage(render_job_id=render_job_id, supabase_path=supabase_path)
        alpha_note = (
            "Transparent WebM alpha preserved — place as overlay on a HIGHER track than footage."
            if any_transparent_ok or job_transparent
            else "Opaque MP4 — prefer standalone/mid-layer placement away from hero peaks."
        )
        probe_bits = (
            f" transparent_ok={str(any_transparent_ok).lower()}"
            f" pix_fmt={last_pix_fmt or 'unknown'}"
        )
        return (
            f"✅ Imported {len(file_ids)}/{n} HyperFrames segments as separate clips "
            f"(file_id={file_ids[0]}, file_ids: {file_ids}, total {total_mb:.1f} MB). "
            f"Indexing skipped (motion_graphics tag).{probe_bits}. {alpha_note}\n"
            "MUST call place_motion_graphic_tool(file_id=..., mode=overlay|gap|cut_in) "
            "for each file_id (transparent → mode=overlay high track; opaque → mode=gap or cut_in). "
            "Do not use add_clip_to_timeline_tool for MG renders."
            f"{generic_warn}"
        )

    # ---- Legacy single-video import (back-compat) ----
    supabase_url = (supabase_url or "").strip()
    if not supabase_url:
        return "Error: segment_urls or supabase_url is required."

    if job_transparent and supabase_url.split("?")[0].lower().endswith(".mp4"):
        supabase_url = re.sub(r"\.mp4(\?|$)", r".webm\1", supabase_url, count=1, flags=re.I)

    if job_transparent and supabase_url.split("?")[0].lower().endswith(".mp4"):
        return (
            "Error: transparent job URL is still .mp4 after WebM rewrite — "
            "re-compose as WebM; refusing opaque MP4 import."
            f"{generic_warn}"
        )

    cached_one = _already_imported([supabase_url])
    if cached_one:
        return cached_one

    file_id, size_mb, err, transparent_ok, pix_fmt = _download_and_import_one(
        supabase_url, label=stamp_label, job_transparent=job_transparent
    )
    if err:
        return f"Error importing video: {err}{generic_warn}"
    _MG_IMPORTED_URLS[_norm_url(supabase_url)] = file_id
    _motion_graphics_cleanup_storage(supabase_path=supabase_path, render_job_id=render_job_id)
    alpha_note = (
        "Transparent WebM alpha preserved — overlay on a HIGHER track than footage."
        if transparent_ok or job_transparent
        else "Opaque MP4 — prefer standalone/mid-layer placement away from hero peaks."
    )
    return (
        f"✅ HyperFrames motion graphic imported into project files "
        f"(file_id={file_id}, size: {size_mb:.1f} MB). Indexing skipped (motion_graphics tag). "
        f"transparent_ok={str(bool(transparent_ok)).lower()} pix_fmt={pix_fmt or 'unknown'}. "
        f"{alpha_note}\n"
        "MUST call place_motion_graphic_tool(file_id=..., mode=overlay|gap|cut_in) "
        "with the placement mode above. Do not use add_clip_to_timeline_tool for MG renders."
        f"{generic_warn}"
    )


# Hard-cut alias kept only so any stale backend tool name still resolves during one deploy.
fetch_remotion_video_from_supabase = fetch_motion_graphics_video
_download_remotion_file = _download_motion_graphics_file
_remotion_cleanup_storage = _motion_graphics_cleanup_storage


_KLING_O1_DEFAULT_T2V_DURATION = 5


def import_video_url_and_add_to_timeline(video_url="", track="", position_seconds="", **_kw) -> str:
    """Download a video from a public URL, import it into project files, and place it on the timeline.

    Used for server-rendered clips (e.g. Manim) delivered as a public URL. One shot:
    download → re-encode/import (via _import_generated_video) → add_clip_to_timeline.
    YouTube page URLs are not direct files — use ingest_web_video_tool instead.
    """
    import tempfile
    import urllib.request

    video_url = (video_url or "").strip()
    if not video_url:
        return "Error: video_url is required."

    url_path = video_url.split("?")[0].rstrip("/")
    lower_path = url_path.lower()
    _audio_exts = (".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma")
    if "freesound.org" in lower_path or any(lower_path.endswith(ext) for ext in _audio_exts):
        return (
            "Error: import_video_url_and_add_to_timeline_tool is for video URLs only. "
            "For Freesound / music / SFX use stock_music(query=..., track=...)."
        )
    try:
        from classes.web_video_ingest import is_youtube_url
        if is_youtube_url(video_url) and not any(
            lower_path.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi")
        ):
            return (
                "Error: that is a YouTube page URL, not a direct video file. "
                "Use ingest_web_video_tool(url=..., intent=\"timeline\") instead."
            )
    except Exception as exc:
        return f"Error: could not classify video URL: {exc}"

    # Check the target track before downloading anything.
    if track and str(track).strip():
        _app = _get_app()
        _layer, _track_err = normalize_track_or_layer_arg(str(track).strip(), _app.project.get("layers") or [])
        if _track_err or _layer is None:
            return _as_error(_track_err or f"no track matches {track!r}")
        _locked = _locked_track_error(_app, _layer)
        if _locked:
            return _as_error(_locked)

    try:
        # Derive a clean .mp4 filename from the URL path.
        raw_name = url_path.split("/")[-1] or "video.mp4"
        root, ext = os.path.splitext(raw_name)
        if ext.lower() not in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
            raw_name = f"{root or 'video'}.mp4"

        tmp_dir = tempfile.mkdtemp(prefix="zenvi_url_import_")
        dest_path = os.path.join(tmp_dir, raw_name)

        try:
            log.info("Downloading video from URL: %s → %s", video_url, dest_path)
            req = urllib.request.Request(video_url, headers={"User-Agent": "ZenviApp/1.0"})
            with urllib.request.urlopen(req, timeout=300) as response, open(dest_path, "wb") as out:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)

            size_mb = os.path.getsize(dest_path) / (1024 * 1024)
            log.info("Download complete: %s (%.1f MB)", dest_path, size_mb)

            # Import into project files (re-encodes for libopenshot compatibility).
            f, err = _import_generated_video(dest_path)
            if err:
                return f"Error importing video: {err}"
            file_id = f.id if f else ""
            if not file_id:
                return "Error: video imported but its file_id could not be resolved."

            # Place it on the timeline.
            placement = add_clip_to_timeline(
                file_id=file_id, position_seconds=position_seconds, track=track, **_kw
            )
            if not placement or str(placement).startswith("Error"):
                return (
                    f"Error: Video imported (file_id={file_id}, {size_mb:.1f} MB) but placing it on the "
                    f"timeline failed: {placement or 'unknown'}. Do NOT download it again: call "
                    f"add_clip_to_timeline_tool(file_id='{file_id}', track=..., position_seconds=...)."
                )
            return (
                f"✅ Video imported (file_id: {file_id}, {size_mb:.1f} MB) and added to the timeline.\n"
                f"{placement}"
            )
        finally:
            _cleanup_scratch_parent(dest_path, "zenvi_url_import_")
    except Exception as e:
        log.error("import_video_url_and_add_to_timeline failed: %s", e, exc_info=True)
        return f"Error importing video from URL: {e}"


def _byok_generation_kwargs():
    """Route generation through the user's own Higgsfield key when one is stored (#60).

    Returns (kwargs, error). BYOK calls bill the user's provider account, so Zenvi
    credits are skipped. A stored key that cannot be read is an error, never a
    silent fall-back to billed Zenvi generation.
    """
    from classes.provider_keys import KeyUnreadable, get_key

    try:
        key = get_key("higgsfield", strict=True)
    except KeyUnreadable:
        return {}, (
            "Your Higgsfield key could not be read. Re-enter or remove it in "
            "Preferences → AI → Integrations."
        )
    return ({"provider": "higgsfield", "provider_key": key} if key else {}), None
def _ingest_web_video_fail(reason: str, *, url: str = "", intent: str = "") -> str:
    """Stable JSON failure — agents should report this briefly and stop, not cascade."""
    msg = str(reason or "Ingest failed.").strip()
    # Never return multi-line stack noise to the model.
    msg = " ".join(msg.split())
    if len(msg) > 280:
        msg = msg[:277] + "..."
    return json.dumps({
        "ok": False,
        "status": "failed",
        "error": msg,
        "url": url or "",
        "intent": intent or "",
        "file_id": "",
        "placed": False,
        "message": msg,
        "next": "Tell the user what failed in one short sentence. Do not retry the same URL in a loop.",
    })


# Claude Code / Desktop MCP clients abort tool calls around ~60s. Long YouTube
# pulls often need 2–3+ minutes. Run the download on a daemon thread, wait a
# short budget on the first call, then long-poll on job_id until done.
_INGEST_SYNC_BUDGET_S = 25.0
_INGEST_POLL_BUDGET_S = 45.0
_INGEST_JOB_TTL_S = 30 * 60
_ingest_jobs_lock = threading.Lock()
_ingest_jobs = {}  # job_id -> dict
_ingest_inflight = {}  # (url_key, intent, place_flag) -> job_id


def _wait_ingest_job(job: dict, timeout_s: float) -> str:
    """Block until the job finishes or *timeout_s* elapses, then snapshot."""
    if (job.get("status") or "running") == "running":
        ev = job.get("event")
        if ev is not None:
            ev.wait(timeout=max(0.0, float(timeout_s)))
    return _ingest_job_public(job)


def _ingest_job_public(job: dict) -> str:
    """JSON snapshot for agents (running / completed / failed)."""
    status = job.get("status") or "running"
    if status == "running":
        jid = job.get("job_id") or ""
        return json.dumps({
            "ok": True,
            "status": "running",
            "job_id": jid,
            "url": job.get("url") or "",
            "intent": job.get("intent") or "",
            "file_id": "",
            "placed": False,
            "message": (
                "YouTube/web download still in progress — long videos can take "
                "a few minutes. This is not a failure."
            ),
            "next": (
                f'Call ingest_web_video_tool(job_id="{jid}") again — the tool '
                "waits until the download finishes or ~45s, then returns. "
                "Do not start another download of the same URL. A client "
                "'operation timed out' is not failure — keep calling this job_id."
            ),
        })
    raw = job.get("result")
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            data = {
                "ok": status == "completed",
                "status": status,
                "message": raw,
            }
    else:
        data = {
            "ok": False,
            "status": "failed",
            "error": job.get("error") or "Ingest failed.",
            "message": job.get("error") or "Ingest failed.",
        }
    if not isinstance(data, dict):
        data = {"ok": False, "status": status, "message": str(data)}
    data["status"] = (
        status if status in ("completed", "failed") else data.get("status") or status
    )
    data["job_id"] = job.get("job_id") or data.get("job_id") or ""
    if data.get("ok") and data.get("status") == "completed":
        data.setdefault(
            "next",
            data.get("next") or "Done. Reply briefly; do not re-ingest the same URL.",
        )
    return json.dumps(data)


def _purge_stale_ingest_jobs(now: Optional[float] = None) -> None:
    cutoff = float(now if now is not None else time.time()) - _INGEST_JOB_TTL_S
    dead = [
        jid for jid, job in _ingest_jobs.items()
        if float(job.get("updated_at") or 0) < cutoff
        and job.get("status") in ("completed", "failed")
    ]
    for jid in dead:
        job = _ingest_jobs.pop(jid, None)
        if not job:
            continue
        key = job.get("dedupe_key")
        if key is not None and _ingest_inflight.get(key) == jid:
            _ingest_inflight.pop(key, None)


def _run_ingest_job(job: dict, kwargs: dict) -> None:
    from classes import web_video_ingest as wvi

    try:
        # execute_tool's outer _atomic ends when we return "running"; group
        # import+place here so one undo still reverses the whole ingest.
        # Headless unit tests stub Qt — skip _atomic when the app is unavailable.
        try:
            app = _get_app()
            use_atomic = getattr(app, "updates", None) is not None
        except Exception:
            app = None
            use_atomic = False
        if use_atomic:
            result = _atomic(app, _ingest_web_video_impl)(**kwargs)
        else:
            result = _ingest_web_video_impl(**kwargs)
        try:
            parsed = json.loads(result) if isinstance(result, str) else {}
            ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
        except Exception:
            ok = False
            parsed = {}
        with _ingest_jobs_lock:
            job["result"] = result if isinstance(result, str) else json.dumps(parsed)
            job["status"] = "completed" if ok else "failed"
            if not ok and isinstance(parsed, dict):
                job["error"] = parsed.get("error") or parsed.get("message") or ""
            job["updated_at"] = time.time()
            key = job.get("dedupe_key")
            if key is not None and _ingest_inflight.get(key) == job.get("job_id"):
                _ingest_inflight.pop(key, None)
    except Exception as e:
        log.error("ingest_web_video job failed: %s", e, exc_info=True)
        fail = _ingest_web_video_fail(
            wvi.humanize_ingest_failure(e),
            url=str(kwargs.get("url") or kwargs.get("video_url") or ""),
            intent=str(kwargs.get("intent") or ""),
        )
        with _ingest_jobs_lock:
            job["result"] = fail
            job["status"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = time.time()
            key = job.get("dedupe_key")
            if key is not None and _ingest_inflight.get(key) == job.get("job_id"):
                _ingest_inflight.pop(key, None)
    finally:
        ev = job.get("event")
        if ev is not None:
            ev.set()


def ingest_web_video(
    url="",
    video_url="",
    intent="reference",
    place="false",
    track="",
    position_seconds="",
    write_subs="",
    job_id="",
    **_kw,
) -> str:
    """Fetch a YouTube/web video URL via yt-dlp into Imports/media bin (one undo).

    FIRST tool for youtube/youtu.be (or similar) links — do not ToolSearch or use
    import_video_url_and_add_to_timeline_tool / import_files_tool for a page URL.

    Long downloads return status=running with a job_id before MCP clients time
    out (~60s). Call ingest_web_video_tool(job_id=...) again — each call waits
    until completed/failed or ~45s. A client "operation timed out" is NOT
    failure — keep calling the same job_id; do not start a second download.

    Intents (pick from what the user asked — do NOT place unless they asked):
      reference — DEFAULT. Media bin only. Colour/look: then match_color_to_reference_tool
                  (referenceFileId). Never put the YouTube clip on the timeline for colour.
      timeline  — ONLY if user asked to add/edit/clip that YouTube video on the timeline.
      recreate  — Media bin watch-only reference; rebuild with user clips/stock/video_gen.
                  Do not leave the YouTube original on the cut unless asked.

    place=true is ignored for recreate; only timeline intent places.
    Failure → JSON ok=false with a short reason (restricted / pull failed / upload MP4).
    """
    from classes import web_video_ingest as wvi

    jid = str(job_id or _kw.get("job_id") or "").strip()
    if jid:
        with _ingest_jobs_lock:
            _purge_stale_ingest_jobs()
            job = _ingest_jobs.get(jid)
        if not job:
            return _ingest_web_video_fail(
                f"Unknown ingest job_id {jid!r}. Pass the job_id from the "
                "status=running reply, or call again with url= to start fresh.",
                url=str(url or video_url or ""),
                intent=str(intent or ""),
            )
        return _wait_ingest_job(job, _INGEST_POLL_BUDGET_S)

    raw_url = str(url or video_url or _kw.get("webpage_url") or "").strip()
    if not raw_url:
        return _ingest_web_video_fail(
            "Paste a YouTube (or other site) video link — url is required."
        )
    try:
        intent_norm = wvi.normalize_intent(intent or "reference")
    except wvi.WebVideoIngestError as exc:
        return _ingest_web_video_fail(str(exc), url=raw_url)

    place_flag = str(place or "").strip().lower() in ("1", "true", "yes", "on")
    if intent_norm == "timeline":
        place_flag = True
    if intent_norm == "recreate":
        place_flag = False

    dedupe_key = (wvi.url_cache_key(raw_url), intent_norm, bool(place_flag))
    kwargs = {
        "url": raw_url,
        "video_url": "",
        "intent": intent_norm,
        "place": "true" if place_flag else "false",
        "track": track,
        "position_seconds": position_seconds,
        "write_subs": write_subs,
    }

    reused = False
    with _ingest_jobs_lock:
        _purge_stale_ingest_jobs()
        existing_id = _ingest_inflight.get(dedupe_key)
        job = _ingest_jobs.get(existing_id) if existing_id else None
        if job and job.get("status") == "running":
            reused = True
        else:
            new_id = str(uuid_module.uuid4())
            event = threading.Event()
            job = {
                "job_id": new_id,
                "status": "running",
                "url": raw_url,
                "intent": intent_norm,
                "dedupe_key": dedupe_key,
                "event": event,
                "result": None,
                "error": "",
                "updated_at": time.time(),
            }
            _ingest_jobs[new_id] = job
            _ingest_inflight[dedupe_key] = new_id
            threading.Thread(
                target=_run_ingest_job,
                args=(job, kwargs),
                name=f"ingest-web-video-{new_id[:8]}",
                daemon=True,
            ).start()

    budget = _INGEST_POLL_BUDGET_S if reused else _INGEST_SYNC_BUDGET_S
    return _wait_ingest_job(job, budget)


def _ingest_web_video_impl(
    url="",
    video_url="",
    intent="reference",
    place="false",
    track="",
    position_seconds="",
    write_subs="",
    **_kw,
) -> str:

    from classes import web_video_ingest as wvi
    from classes import info

    raw_url = str(url or video_url or _kw.get("webpage_url") or "").strip()
    if not raw_url:
        return _ingest_web_video_fail(
            "Paste a YouTube (or other site) video link — url is required."
        )

    try:
        intent_norm = wvi.normalize_intent(intent or "reference")
    except wvi.WebVideoIngestError as exc:
        return _ingest_web_video_fail(str(exc), url=raw_url)

    place_flag = str(place or "").strip().lower() in ("1", "true", "yes", "on")
    if intent_norm == "timeline":
        place_flag = True
    if intent_norm == "recreate":
        place_flag = False

    if str(write_subs or "").strip() == "":
        # Default: grab English subs when available for reference/recreate.
        write_subs_flag = intent_norm in ("reference", "recreate")
    else:
        write_subs_flag = str(write_subs).strip().lower() in ("1", "true", "yes", "on")

    # Direct file URL (mp4/mov/…) — keep existing Manim path; do not force yt-dlp.
    lower = raw_url.split("?")[0].lower()
    if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi")):
        try:
            wvi.assert_public_http_url(raw_url)
        except wvi.WebVideoIngestError as exc:
            return _ingest_web_video_fail(str(exc), url=raw_url, intent=intent_norm)
        if place_flag or intent_norm == "timeline":
            placed_msg = import_video_url_and_add_to_timeline(
                video_url=raw_url, track=track, position_seconds=position_seconds, **_kw
            )
            placed_text = str(placed_msg or "")
            if placed_text.startswith("Error:"):
                return _ingest_web_video_fail(placed_text, url=raw_url, intent=intent_norm)
            return json.dumps({
                "ok": True,
                "intent": intent_norm,
                "placed": True,
                "url": raw_url,
                "message": placed_text,
                "next": "Direct media URL imported and placed on the timeline.",
                "product_note": (
                    "User-initiated local import. Do not re-upload the original as your own."
                ),
            })
        # Import only: download then import without place.
        # Fall through to yt-dlp for page URLs; for direct files use urllib path.
        try:
            import tempfile
            import urllib.request
            tmp_dir = tempfile.mkdtemp(prefix="zenvi_web_direct_")
            raw_name = lower.rsplit("/", 1)[-1] or "video.mp4"
            dest = os.path.join(tmp_dir, raw_name)
            req = urllib.request.Request(raw_url, headers={"User-Agent": "ZenviApp/1.0"})
            with urllib.request.urlopen(req, timeout=300) as response, open(dest, "wb") as out:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
            f, err = _import_generated_video(dest)
            _cleanup_scratch_parent(dest, "zenvi_web_direct_")
            if err:
                return _ingest_web_video_fail(f"Could not import the file into the media bin: {err}", url=raw_url, intent=intent_norm)
            file_id = f.id if f else ""
            return json.dumps({
                "ok": True,
                "intent": intent_norm,
                "file_id": file_id,
                "placed": False,
                "url": raw_url,
                "message": "Direct media URL imported to media bin.",
                "next": (
                    "Colour: match_color_to_reference_tool(referenceFileId=file_id). "
                    "Timeline: add_clip_to_timeline_tool(file_id=...)."
                ),
                "product_note": (
                    "User-initiated local import. Do not re-upload the original as your own."
                ),
            })
        except Exception as e:
            return _ingest_web_video_fail(wvi.humanize_ingest_failure(e) if hasattr(wvi, "humanize_ingest_failure") else str(e), url=raw_url, intent=intent_norm)

    root = wvi.cache_root(getattr(info, "USER_PATH", "") or "")
    try:
        purged = wvi.purge_expired_cache(root)
    except Exception:
        purged = 0

    cache_dir = os.path.join(root, wvi.url_cache_key(raw_url))
    try:
        meta = wvi.download_web_video(
            raw_url,
            cache_dir=cache_dir,
            intent=intent_norm,
            write_subs=write_subs_flag,
        )
    except wvi.WebVideoIngestError as exc:
        return _ingest_web_video_fail(str(exc), url=raw_url, intent=intent_norm)
    except Exception as e:
        log.error("ingest_web_video download failed: %s", e, exc_info=True)
        return _ingest_web_video_fail(wvi.humanize_ingest_failure(e), url=raw_url, intent=intent_norm)

    video_path = meta.get("video_path") or ""
    f, err = _import_generated_video(video_path)
    if err:
        return _ingest_web_video_fail(f"Downloaded OK but import into the project failed: {err}", url=raw_url, intent=intent_norm)
    file_id = f.id if f else ""
    if not file_id:
        return _ingest_web_video_fail("Downloaded OK but could not find the new media-bin file id.", url=raw_url, intent=intent_norm)

    # Tag as web reference for recreate / colour.
    tag_warning = ""
    try:
        tags = f.data.get("tags") if isinstance(f.data, dict) else None
        if isinstance(tags, str):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tag_list = [str(t).strip() for t in tags if str(t).strip()]
        else:
            tag_list = []
        for tag in ("web_video", intent_norm, "youtube" if meta.get("is_youtube") else "web"):
            if tag and tag not in tag_list:
                tag_list.append(tag)
        if intent_norm == "recreate" and "reference_only" not in tag_list:
            tag_list.append("reference_only")

        def _save_tags():
            f.data["tags"] = ", ".join(tag_list)
            title = meta.get("title") or ""
            if title and isinstance(f.data, dict):
                f.data["name"] = title[:120]
            f.save()

        _run_on_main_thread(_save_tags)
    except Exception as tag_err:
        log.warning("ingest_web_video tag failed: %s", tag_err)
        tag_warning = f"Could not save media tags/title: {tag_err}"
        if intent_norm == "recreate":
            return _ingest_web_video_fail(
                f"Imported as {file_id} but failed to mark reference_only: {tag_err}",
                url=raw_url,
                intent=intent_norm,
            )

    placement = ""
    placed = False
    if place_flag:
        try:
            placement = add_clip_to_timeline(
                file_id=file_id, position_seconds=position_seconds, track=track, **_kw
            )
            placed = not str(placement).startswith("Error:")
            if not placed:
                placement = str(placement)
        except Exception as place_err:
            log.error("ingest_web_video place failed: %s", place_err, exc_info=True)
            placement = f"In media bin as {file_id}, but could not place on timeline: {place_err}"
            placed = False

    next_steps = []
    if intent_norm in ("reference", "recreate"):
        next_steps.append(
            f"Colour match: match_color_to_reference_tool(referenceFileId=\"{file_id}\")."
        )
    if intent_norm == "recreate":
        next_steps.append(
            "Recreate with the user's clips / stock_video / video_gen — "
            "do not leave the YouTube original on the story cut unless asked."
        )
    if intent_norm == "timeline" and not placed:
        next_steps.append(f"Place with add_clip_to_timeline_tool(file_id=\"{file_id}\").")
    if meta.get("subtitle_paths"):
        next_steps.append(
            "Subtitles cached (paths in subtitle_paths); caption tool can consume later."
        )

    result = {
        "ok": (not place_flag) or placed,
        "intent": intent_norm,
        "file_id": file_id,
        "placed": placed,
        "url": meta.get("webpage_url") or raw_url,
        "title": meta.get("title"),
        "duration_seconds": meta.get("duration_seconds"),
        "extractor": meta.get("extractor"),
        "is_youtube": bool(meta.get("is_youtube")),
        "subtitle_paths": meta.get("subtitle_paths") or [],
        "cache_purged": purged,
        "placement": placement if place_flag else "",
        "message": (
            f"Ingested “{meta.get('title') or 'video'}” as media_bin file_id={file_id}."
        ),
        "next": " ".join(next_steps),
        "product_note": meta.get("product_note"),
        "undo": "one step",
    }
    if tag_warning:
        result["warnings"] = [tag_warning]
    if place_flag and not placed:
        result["error"] = placement or "Timeline placement failed after import."
        result["message"] = (
            f"Imported to media bin as file_id={file_id}, but timeline placement failed."
        )
        result["next"] = f"Place with add_clip_to_timeline_tool(file_id=\"{file_id}\")."
    return json.dumps(result)


def _generated_video_ripple_plan(app, position_seconds, track):
    """(position or None, layer to ripple or None, error) -- checked before any credits are spent.

    With a position, later clips on the target track (the explicit track, else the
    track of the first clip at/after the position) move right to make room.
    """
    from classes.query import Clip
    pos = None
    if position_seconds is not None and str(position_seconds).strip():
        try:
            pos = parse_seconds_arg(position_seconds)
        except Exception:
            pos = None
        if pos is None:
            return None, None, f"Error: position_seconds {position_seconds!r} is not a time (e.g. 12, 12.5, 0:12)."
    layer = None
    if track and str(track).strip():
        layer, err = normalize_track_or_layer_arg(str(track).strip(), app.project.get("layers") or [])
        if err or layer is None:
            return None, None, _as_error(err or f"no track matches {track!r}")
        locked = _locked_track_error(app, layer)
        if locked:
            return None, None, locked
    if pos is None:
        return None, layer, ""
    ripple_layer = layer
    if ripple_layer is None:
        later = [c for c in Clip.filter() if float(c.data.get("position", 0) or 0) >= pos - 0.001]
        if later:
            ripple_layer = min(later, key=lambda c: float(c.data.get("position", 0) or 0)).data.get("layer")
            locked = _locked_track_error(app, int(ripple_layer or 0))
            if locked:
                return None, None, locked + " Pick an unlocked track (track=...) for the generated clip."
    return pos, ripple_layer, ""


def generate_video_and_add_to_timeline(prompt="", duration_seconds="", position_seconds="", track="", **_kw) -> str:
    """Generate a short AI video clip from a text prompt (cloud text-to-video, uses credits) and place it on the timeline.

    For "generate a 5 second shot of waves at sunset", "make an AI b-roll of a busy city".
    duration_seconds is any whole number from 2 to 15 (default 5); the clip is 720p and follows
    the project's aspect (16:9, 9:16 or square). position_seconds (timeline seconds) inserts it there and moves later clips on
    that track right to make room; without it the clip goes after the last clip on the track.
    Returns the placement line with timeline_clip_id. The generation, import and placement
    are one undo step. Not for local ComfyUI (create_media_with_comfyui_tool).
    """
    if QThread is None or QEventLoop is None:
        return "Error: Requires a Qt binding."
    app = _get_app()
    prompt = (prompt or "").strip()
    if len(prompt) < 2:
        return "Error: Prompt must be at least 2 characters."
    _pos, _ripple_layer, plan_err = _generated_video_ripple_plan(app, position_seconds, track)
    if plan_err:
        return plan_err

    explicit_dur = str(duration_seconds or "").strip()
    duration = _clamp_generation_duration(explicit_dur, default=_KLING_O1_DEFAULT_T2V_DURATION)
    t2v_w, t2v_h = _project_kling_o1_t2v_dims()

    output_path = _canonical_media_path(_output_path_for_generated_video())

    # Pause auto-save during generation to prevent backup interference
    auto_save_was_active = _pause_auto_save()
    try:
        from classes.credits_client import check_operation, credits

        byok, byok_err = _byok_generation_kwargs()
        if byok_err:
            return f"Error: {byok_err}"
        if not byok:
            _, _, blocked = check_operation("video_generation", "video generation")
            if blocked:
                return _as_error(blocked)
        from classes.api_client import get_backend_client
        client = get_backend_client()
        result = client.generate_video(
            prompt,
            duration_seconds=duration,
            width=t2v_w,
            height=t2v_h,
            mode="t2v",
            **byok,
        )
        video_url = result.get("video_url", "")
        err = result.get("error", "")
        if err:
            return f"Error: {err}"

        dl_err = _download_video_url_to_path(video_url, output_path)
        if dl_err:
            return f"Error: {dl_err}"

        from classes.credits_client import credits

        credits.award_bonus("first_export")   # idempotent — only fires once ever

        try:
            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Video generated but failed to import into project files"
                    + (f": {import_err}" if import_err else ".")
                )

            stamped = _stamp_generated_video_metadata(f, prompt)

            # The import, the ripple and the placement are one user action: join
            # the tool call's transaction (execute_tool) so they undo as ONE step.
            # The id is thread-local, so read it here and hand it to each hop.
            _composite_tid = app.updates.transaction_id or _new_transaction_id()
            _rippled = []

            if _pos is not None:
                # Compute the generated clip's duration from the file metadata
                _gen_dur = float(f.data.get("duration") or duration)

                from classes.query import Clip as _Clip
                _app_ref = app
                _snap_tol = 0.001

                def _do_ripple_insert():
                    for c in list(_Clip.filter()):
                        c_pos = float(c.data.get("position", 0))
                        c_layer = c.data.get("layer", 0)
                        cid = c.data.get("id")
                        if (cid
                                and c_pos >= _pos - _snap_tol
                                and (_ripple_layer is None or c_layer == _ripple_layer)):
                            _app_ref.updates.update(
                                ["clips", {"id": cid}], {"position": c_pos + _gen_dur}
                            )
                            _rippled.append((cid, c_pos))

                _run_on_main_thread(
                    _atomic(_app_ref, _do_ripple_insert, tid=_composite_tid)
                )

            def _stamp_prompt():
                ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
                ai["prompt"] = prompt[:500]
                if not str(ai.get("short_summary") or "").strip():
                    ai["short_summary"] = prompt[:200]
                ai["source"] = ai.get("source") or "kling_t2v"
                f.data["ai_metadata"] = ai
                if hasattr(f, "save"):
                    f.save()

            try:
                _run_on_main_thread(_atomic(app, _stamp_prompt, tid=_composite_tid))
            except Exception as stamp_exc:
                log.debug("generated-video prompt stamp failed: %s", stamp_exc)

            was_playing = _pause_player()
            try:
                msg = add_clip_to_timeline(
                    file_id=f.id,
                    position_seconds=position_seconds or "",
                    track=track or "",
                    query=prompt,
                    duration_seconds=str(duration) if explicit_dur else "",
                    transaction_id=_composite_tid,
                )
            finally:
                _resume_player(was_playing)
            if not msg or str(msg).lower().startswith("error"):
                if _rippled:
                    # Put the clips that moved to make room back where they were.
                    def _undo_ripple():
                        for cid, old_pos in _rippled:
                            app.updates.update(["clips", {"id": cid}], {"position": old_pos})
                    try:
                        _run_on_main_thread(_atomic(app, _undo_ripple, tid=_composite_tid))
                    except Exception as exc:
                        log.error("generate_video: could not move rippled clips back: %s", exc)
                return (
                    f"Error: Video imported (file_id={f.id}) but timeline placement failed: "
                    f"{msg or 'unknown'}. "
                    + ("The clips moved to make room were put back. " if _rippled else "")
                    + f"Do NOT regenerate — call add_clip_to_timeline_tool(file_id='{f.id}', "
                    f"track=<layer_number from list_layers_tool>, position_seconds=...)."
                )
            if not stamped:
                return (
                    f"{msg} Warning: the generated clip's metadata (name, tags, summary) "
                    f"could not be saved for file_id={f.id}; it may be missing after reload."
                )
            return msg
        except Exception as e:
            return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def insert_v2v_into_clip(
    query="",
    fade_ms="400",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Find best match in resolved clip, generate a V2V insert via Kling O1 Pro."""
    if QThread is None or QEventLoop is None:
        return "Error: Requires a Qt binding."

    resolved = _resolve_timeline_clip_for_tool(
        clip_query=clip_query, timeline_clip_id=timeline_clip_id, **_kw,
    )
    if not resolved.ok or not resolved.clip:
        return resolved.error or "Error: Could not resolve timeline clip."
    clip_obj = resolved.clip
    locked = _check_bake_target(_get_app(), clip_obj)
    if locked:
        return locked

    query = (query or "").strip()
    too_extreme, reason = _is_extreme_for_4_seconds(query)
    if too_extreme:
        return f"Error: {reason}"

    try:
        fm = int(float(fade_ms)) if str(fade_ms).strip() else 400
    except Exception:
        fm = 400
    fade_s = max(0.05, min(0.49, float(fm) / 1000.0))

    clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
    clip_start = float(clip_data.get("start", 0.0) or 0.0)
    clip_end = float(clip_data.get("end", 0.0) or 0.0)

    source_file = _get_source_file_for_clip(clip_obj)
    if not source_file or not getattr(source_file, "absolute_path", None) or not source_file.absolute_path():
        return "Error: Could not find source video."
    source_path = source_file.absolute_path()

    source_ai = (
        source_file.data.get("ai_metadata")
        if isinstance(source_file.data, dict) and isinstance(source_file.data.get("ai_metadata"), dict)
        else None
    )

    from classes.twelvelabs_match import select_twelvelabs_match

    best_mid = None

    # Strategy 1: TwelveLabs overlap-aware match; cut after the scene ends.
    if source_ai:
        tw = source_ai.get("twelvelabs") if isinstance(source_ai.get("twelvelabs"), dict) else {}
        status = (tw.get("status") or "").lower()
        index_id = tw.get("index_id") or ""
        video_id = tw.get("video_id") or ""
        if (status == "ready" and index_id and video_id):
            search_query = _semantic_search_query(query)
            items, err = _twelvelabs_search_in_window(
                str(index_id), search_query, page_limit=30, video_id=str(video_id),
            )
            if not err and items:
                chosen = select_twelvelabs_match(
                    items,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    cut_mode="end",
                )
                if chosen:
                    cues = (source_ai or {}).get("transcript_cues") or []
                    try:
                        dur = float((source_file.data or {}).get("duration") or 0)
                    except (TypeError, ValueError):
                        dur = 0.0
                    watched = _watch_confirm_cut(
                        source_path,
                        chosen.get("start", chosen["cut_source"]),
                        chosen.get("end", chosen["cut_source"]),
                        query,
                        fallback=chosen["cut_source"],
                        duration=dur,
                        transcript_cues=cues,
                    )
                    insertion = watched.get("cut_source", chosen["cut_source"])
                    if insertion > clip_end - 1.0:
                        insertion = max(clip_start, clip_end - 1.0)
                    best_mid = insertion
                    log.info(
                        "insert_v2v: rank=%s segment [%.2f, %.2f] → insertion at %.2f",
                        chosen.get("rank"),
                        chosen["start"],
                        chosen["end"],
                        best_mid,
                    )

    # Strategy 2: Scene descriptions
    if best_mid is None and source_ai:
        scenes = source_ai.get("scene_descriptions", [])
        q_lower = query.lower()
        for sc in (scenes or []):
            if not isinstance(sc, dict):
                continue
            desc = (sc.get("description") or "").lower()
            t = float(sc.get("time", 0.0) or 0.0)
            if q_lower in desc and clip_start <= t <= clip_end:
                best_mid = t
                break

    # Strategy 3: fallback — 80% through the clip (biased toward the end)
    if best_mid is None:
        best_mid = clip_start + (clip_end - clip_start) * 0.8
        log.info("insert_v2v: no search results, using 80%% fallback point %.2fs", best_mid)

    # Get video dimensions — clamp to Kling O1 Pro video-edit range [720, 2160]
    vid_width = int(source_file.data.get("width", 1920))
    vid_height = int(source_file.data.get("height", 1080))
    vf, vid_width, vid_height = _kling_o1_scale_vf(vid_width, vid_height)
    log.info("insert_v2v: target dims %dx%d", vid_width, vid_height)

    # Pause auto-save during the generation pipeline
    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_v2v_")
        try:
            # ---- Step 1: Extract 3 s of footage before the insertion point as V2V seed ----
            # Sending Kling a reference video keeps the generated insert visually consistent
            # with the original clip (same scene, lighting, style).
            seed_mp4 = os.path.join(tmpdir, "seed.mp4")
            insert_mp4 = os.path.join(tmpdir, "insert.mp4")

            ref_dur = min(3.0, best_mid - clip_start)
            ref_start = max(0.0, best_mid - ref_dur)
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(ref_start), "-i", source_path,
                "-t", str(ref_dur), "-vf", vf, "-r", "24", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", seed_mp4,
            ])
            if not ok:
                return f"Error: Failed to extract seed video: {err}"

            # ---- Step 2: Generate V2V insert (video-edit only; no frame constraints) ----
            prompt = (
                f"{query}\n\n"
                "Continue the scene naturally from the source footage. "
                "Preserve camera motion, lighting, and visual style."
            )
            from classes.credits_client import check_operation

            _, _, blocked = check_operation("video_generation", "video generation")
            if blocked:
                return _as_error(blocked)
            from classes.api_client import get_backend_client
            client = get_backend_client()
            seed_fid, _, up_err = _upload_generation_assets(client, seed_path=seed_mp4)
            if up_err:
                return f"Error: {up_err}"
            result = client.generate_video(
                prompt,
                seed_video_file_id=seed_fid,
                mode="v2v_edit",
                keep_original_sound=_ffprobe_has_audio(source_path),
            )
            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            dl_err = _download_video_url_to_path(video_url, insert_mp4)
            if dl_err:
                return f"Error: {dl_err}"

            # ---- Step 4: Bake updated clip with crossfades ----
            output_path = _canonical_media_path(_output_path_for_generated_video())
            dur_a = max(0.0, best_mid - clip_start)
            dur_c = max(0.0, clip_end - best_mid)
            # Probe the actual duration of the generated clip — do NOT assume it equals
            # gen_duration.  Even a 1-second mismatch makes xfade offsets wrong → corruption.
            insert_dur = _ffprobe_video_duration(insert_mp4)
            if insert_dur < 0.5:
                insert_dur = 3.0
                log.warning("insert_v2v: could not probe insert duration, using %s", insert_dur)

            # Clamp fade so xfade offsets are valid
            fade = float(fade_s)
            fade = min(fade, 0.49)
            fade = min(fade, max(0.01, dur_a / 2.0) if dur_a > 0 else 0.01)
            fade = min(fade, max(0.01, dur_c / 2.0) if dur_c > 0 else 0.01)
            fade = min(fade, max(0.01, insert_dur / 2.0) if insert_dur > 0 else 0.01)
            fade = max(0.01, fade)

            vf_bake = (
                f"scale={vid_width}:{vid_height}:force_original_aspect_ratio=decrease,"
                f"pad={vid_width}:{vid_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps=24"
            )
            off1 = max(0.0, dur_a - fade)
            off2 = max(0.0, dur_a + insert_dur - (2.0 * fade))

            has_audio = _ffprobe_has_audio(source_path)
            if has_audio:
                filter_complex = (
                    f"[0:v]trim=start={clip_start}:end={best_mid},setpts=PTS-STARTPTS,{vf_bake}[va];"
                    f"[1:v]setpts=PTS-STARTPTS,{vf_bake}[vb];"
                    f"[0:v]trim=start={best_mid}:end={clip_end},setpts=PTS-STARTPTS,{vf_bake}[vc];"
                    f"[va][vb]xfade=transition=fade:duration={fade}:offset={off1}[vab];"
                    f"[vab][vc]xfade=transition=fade:duration={fade}:offset={off2}[vout];"
                    f"[0:a]atrim=start={clip_start}:end={best_mid},asetpts=PTS-STARTPTS,"
                    f"afade=t=out:st={max(0.0, dur_a - fade)}:d={fade}[aa];"
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=start=0:end={insert_dur}[ab];"
                    f"[0:a]atrim=start={best_mid}:end={clip_end},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d={fade}[ac];"
                    f"[aa][ab][ac]concat=n=3:v=0:a=1[aout]"
                )
                bake_cmd = [
                    "ffmpeg", "-y", "-i", source_path, "-i", insert_mp4,
                    "-filter_complex", filter_complex,
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    output_path,
                ]
            else:
                filter_complex = (
                    f"[0:v]trim=start={clip_start}:end={best_mid},setpts=PTS-STARTPTS,{vf_bake}[va];"
                    f"[1:v]setpts=PTS-STARTPTS,{vf_bake}[vb];"
                    f"[0:v]trim=start={best_mid}:end={clip_end},setpts=PTS-STARTPTS,{vf_bake}[vc];"
                    f"[va][vb]xfade=transition=fade:duration={fade}:offset={off1}[vab];"
                    f"[vab][vc]xfade=transition=fade:duration={fade}:offset={off2}[vout]"
                )
                bake_cmd = [
                    "ffmpeg", "-y", "-i", source_path, "-i", insert_mp4,
                    "-filter_complex", filter_complex,
                    "-map", "[vout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-an",
                    "-movflags", "+faststart",
                    output_path,
                ]

            ok, bake_err = _ffmpeg_run(bake_cmd)
            if not ok:
                return f"Error: Failed to bake updated clip: {bake_err}"

            # ---- Step 5: Import the baked clip and place on timeline ----
            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Failed to import baked clip into project files"
                    + (f": {import_err}" if import_err else ".")
                )
            insert_offset = best_mid - clip_start
            try:
                placed = _run_on_main_thread(lambda: _install_baked_insert(clip_obj.id, f.id, insert_offset))
            except Exception as exc:
                return (
                    f"Error: The combined clip was generated and imported (file_id={f.id}) but replacing "
                    f"clip {clip_obj.id} on the timeline failed: {exc}"
                )
            moved = placed["moved_later_items"]
            return _bake_receipt(
                f"Replaced clip {clip_obj.id} with the combined clip {placed['timeline_clip_id']} "
                f"(a {insert_dur:.1f}s AI insert at {_fmt_mmss(insert_offset)} into the clip, "
                f"{int(fade * 1000)}ms crossfades) at {placed['position']:.2f}s"
                + (f"; moved {len(moved)} later item(s) on the track right by {placed['moved_by']:.2f}s"
                   if moved else "")
                + ". One undo restores the original.",
                **placed,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("insert_v2v_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def replace_object_in_clip(
    description="",
    duration_seconds="",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Replace or update an object/visual element in a timeline clip using Kling O1 Pro V2V edit."""
    if QThread is None or QEventLoop is None:
        return "Error: Requires a Qt binding."

    resolved = _resolve_timeline_clip_for_tool(
        clip_query=clip_query, timeline_clip_id=timeline_clip_id, **_kw,
    )
    if not resolved.ok or not resolved.clip:
        return resolved.error or "Error: Could not resolve timeline clip."
    clip_obj = resolved.clip
    locked = _check_bake_target(_get_app(), clip_obj)
    if locked:
        return locked

    description = (description or "").strip()
    if not description:
        return "Error: A description of what to replace/update is required."

    clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
    clip_start = float(clip_data.get("start", 0.0) or 0.0)
    clip_end = float(clip_data.get("end", 0.0) or 0.0)
    clip_duration = max(0.1, clip_end - clip_start)

    source_file = _get_source_file_for_clip(clip_obj)
    if not source_file or not getattr(source_file, "absolute_path", None) or not source_file.absolute_path():
        return "Error: Could not find source video for selected clip."
    source_path = source_file.absolute_path()

    # Default 5s segment for V2V edit; honor duration_seconds when set (max 8s).
    if str(duration_seconds).strip():
        try:
            extract_dur = min(float(duration_seconds), _GENERATION_EDIT_MAX_SECONDS, clip_duration)
        except (TypeError, ValueError):
            extract_dur = min(5.0, clip_duration)
    else:
        extract_dur = min(5.0, clip_duration)

    vid_width = int(source_file.data.get("width", 1920))
    vid_height = int(source_file.data.get("height", 1080))
    vf, vid_width, vid_height = _kling_o1_scale_vf(vid_width, vid_height)

    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_replace_")
        try:
            ref_mp4 = os.path.join(tmpdir, "ref.mp4")

            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(clip_start), "-i", source_path,
                "-t", str(extract_dur), "-vf", vf, "-r", "24",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", ref_mp4,
            ])
            if not ok:
                # Retry without audio if mux fails
                ok, err = _ffmpeg_run([
                    "ffmpeg", "-y", "-ss", str(clip_start), "-i", source_path,
                    "-t", str(extract_dur), "-vf", vf, "-r", "24", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", ref_mp4,
                ])
            if not ok:
                return f"Error: Failed to extract reference clip: {err}"

            prompt = (
                f"{description}\n\n"
                "Apply the change throughout the entire video while preserving the original "
                "camera motion, scene composition, and lighting."
            )
            from classes.credits_client import check_operation

            _, _, blocked = check_operation("video_generation", "video generation")
            if blocked:
                return _as_error(blocked)

            from classes.api_client import get_backend_client
            client = get_backend_client()
            seed_fid, _, up_err = _upload_generation_assets(client, seed_path=ref_mp4)
            if up_err:
                return f"Error: {up_err}"
            has_audio = _ffprobe_has_audio(ref_mp4)
            result = client.generate_video(
                prompt,
                seed_video_file_id=seed_fid,
                mode="v2v_edit",
                keep_original_sound=has_audio,
            )
            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            output_path = _canonical_media_path(_output_path_for_generated_video())
            dl_err = _download_video_url_to_path(video_url, output_path)
            if dl_err:
                return f"Error: {dl_err}"

            gen_duration = _ffprobe_video_duration(output_path)
            if gen_duration < 0.5:
                gen_duration = extract_dur

            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Failed to import generated video into project files"
                    + (f": {import_err}" if import_err else ".")
                )
            try:
                placed = _run_on_main_thread(
                    lambda: _install_baked_head(clip_obj.id, f.id, extract_dur, gen_duration))
            except Exception as exc:
                return (
                    f"Error: The edited footage was generated and imported (file_id={f.id}) but placing it "
                    f"over clip {clip_obj.id} failed: {exc}"
                )
            rest = (f"; the rest of the original continues at {placed['rest_starts_at']:.2f}s"
                    if placed.get("rest_of_original") else "; it covered the whole clip")
            return _bake_receipt(
                f"Replaced the first {extract_dur:.1f}s of clip {clip_obj.id} with the AI edit "
                f"('{description}', new clip {placed['timeline_clip_id']}) at {placed['position']:.2f}s"
                f"{rest}. One undo restores the original.",
                **placed,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("replace_object_in_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def generate_transition_clip(
    clip_a_id="",
    clip_b_id="",
    clip_a_query="",
    clip_b_query="",
    prompt_hint="",
    duration_seconds="",
    **_kw,
) -> str:
    """Join two neighbouring clips on one track with an AI morph (cloud generation, uses credits).

    duration_seconds is the morph's length, any whole number from 2 to 15 (default 5).
    The last frame of clip A morphs into the first frame of clip B; A, the morph and B are baked
    into one new clip that replaces both (and any transition at their cut) in one undo step, and
    later clips on the track move right to make room. Pass clip_a_id/clip_b_id (any order) or
    queries; clips on different tracks, with clips between them, or on a locked track are
    refused before anything is generated.
    """
    from classes.query import Clip
    _get_app()

    pair = _resolve_clip_pair_for_tool(
        clip_a_id=clip_a_id,
        clip_b_id=clip_b_id,
        clip_a_query=clip_a_query,
        clip_b_query=clip_b_query,
    )
    if not pair.ok or not pair.clip_a or not pair.clip_b:
        return pair.error or "Error: Could not resolve transition clip pair."
    pair_error, clip_a, clip_b = _check_morph_pair(_get_app(), pair.clip_a, pair.clip_b)
    if pair_error:
        return pair_error

    file_a = _get_source_file_for_clip(clip_a)
    file_b = _get_source_file_for_clip(clip_b)
    if not file_a or not file_b:
        return "Error: Could not find source files for the clips."

    path_a = file_a.absolute_path() if hasattr(file_a, 'absolute_path') else file_a.data.get('path', '')
    path_b = file_b.absolute_path() if hasattr(file_b, 'absolute_path') else file_b.data.get('path', '')
    if not path_a or not os.path.isfile(path_a):
        return f"Error: Source video for clip A not found: {path_a}"
    if not path_b or not os.path.isfile(path_b):
        return f"Error: Source video for clip B not found: {path_b}"

    # Timeline positions and source trim ranges
    pos_a = float(clip_a.data.get("position", 0))
    start_a, end_a = _clip_source_range(clip_a.data, file_a.data)
    start_b, end_b = _clip_source_range(clip_b.data, file_b.data)
    duration_a = max(0.01, end_a - start_a)
    dur_b = max(0.01, end_b - start_b)

    log.info(
        "generate_transition: clip_a id=%s source=%.3f-%.3fs file=%s | "
        "clip_b id=%s source=%.3f-%.3fs file=%s",
        clip_a.id, start_a, end_a, os.path.basename(path_a),
        clip_b.id, start_b, end_b, os.path.basename(path_b),
    )

    layer = clip_a.data.get("layer")

    prompt = (prompt_hint or "").strip()
    if not prompt:
        prompt = (
            "Gradually evolve the opening scene into the closing scene through a fluid, "
            "continuous motion. Begin on the first frame composition and evolve smoothly "
            "toward the last frame image. Preserve the appearance and identity of all people "
            "and key objects while naturally transitioning pose, setting, and lighting. "
            "The movement should feel organic and cinematic, with no abrupt cuts."
        )

    morph_duration = _clamp_generation_duration(duration_seconds)

    # Scale extracted frames to project dimensions for consistent morph output
    t2v_w, t2v_h = _project_kling_o1_t2v_dims()
    frame_vf = (
        f"scale={t2v_w}:{t2v_h}:force_original_aspect_ratio=decrease,"
        f"pad={t2v_w}:{t2v_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )

    # Pause auto-save during the generation pipeline
    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_morph_")
        try:
            frame_a_path = os.path.join(tmpdir, "frame_a.jpg")
            frame_b_path = os.path.join(tmpdir, "frame_b.jpg")

            # Last frame of clip A → morph start (first constraint)
            time_a = max(start_a, end_a - 0.1) if end_a > start_a else start_a
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(time_a), "-i", path_a,
                "-frames:v", "1", "-vf", frame_vf, "-q:v", "2", frame_a_path,
            ])
            if not ok:
                return f"Error: Failed to extract last frame from clip A at {time_a:.3f}s: {err}"
            if not os.path.isfile(frame_a_path) or os.path.getsize(frame_a_path) < 512:
                return f"Error: Extracted frame A is empty (time={time_a:.3f}s, path={path_a})"

            # First frame of clip B → morph end (last constraint)
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(start_b), "-i", path_b,
                "-frames:v", "1", "-vf", frame_vf, "-q:v", "2", frame_b_path,
            ])
            if not ok:
                return f"Error: Failed to extract first frame from clip B at {start_b:.3f}s: {err}"
            if not os.path.isfile(frame_b_path) or os.path.getsize(frame_b_path) < 512:
                return f"Error: Extracted frame B is empty (time={start_b:.3f}s, path={path_b})"

            log.info(
                "generate_transition: extracted frames A@%ss (%d bytes) B@%ss (%d bytes)",
                f"{time_a:.3f}",
                os.path.getsize(frame_a_path),
                f"{start_b:.3f}",
                os.path.getsize(frame_b_path),
            )

            from classes.credits_client import check_operation

            byok, byok_err = _byok_generation_kwargs()
            if byok_err:
                return f"Error: {byok_err}"
            if not byok:
                _, _, blocked = check_operation("morph_generation", "morph generation")
                if blocked:
                    return _as_error(blocked)

            from classes.api_client import get_backend_client
            client = get_backend_client()

            log.info("generate_transition: Kling O1 Pro frame morph (last frame A → first frame B)")
            _, frame_images_paths, up_err = _upload_generation_assets(
                client,
                frame_specs=[
                    {"path": frame_a_path, "frame": "first"},
                    {"path": frame_b_path, "frame": "last"},
                ],
            )
            if up_err:
                return f"Error: {up_err}"
            result = client.generate_video(
                prompt,
                duration_seconds=int(morph_duration),
                frame_images_paths=frame_images_paths,
                mode="frame_morph",
                **byok,
            )

            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            morph_path = os.path.join(tmpdir, "morph_video.mp4")
            dl_err = _download_video_url_to_path(video_url, morph_path)
            if dl_err:
                return f"Error: {dl_err}"

            baked_path = _canonical_media_path(_output_path_for_generated_video())
            ok, bake_err = _bake_transition_video(
                path_a, start_a, end_a,
                morph_path,
                path_b, start_b, end_b,
                t2v_w, t2v_h,
                baked_path,
            )
            if not ok:
                return f"Error: Failed to bake transition clip: {bake_err}"

            morph_dur_actual = _ffprobe_video_duration(morph_path)
            if morph_dur_actual < 0.1:
                morph_dur_actual = float(morph_duration)

            f, import_err = _import_generated_video(baked_path)
            if not f:
                return "Error: Baked transition clip could not be added to project."

            merged_tags, merged_ai = _merge_baked_transition_metadata(
                file_a,
                start_a,
                end_a,
                file_b,
                start_b,
                end_b,
                duration_a,
                morph_dur_actual,
                prompt_hint=prompt,
            )
            if merged_tags:
                f.data["tags"] = merged_tags
            existing_ai = f.data.get("ai_metadata")
            if not isinstance(existing_ai, dict):
                existing_ai = {}
            existing_ai.update(merged_ai)
            f.data["ai_metadata"] = existing_ai
            def _save_merged():
                # Saves reach Qt listeners: GUI thread.
                _normalize_imported_file_path(f, f.absolute_path() if hasattr(f, "absolute_path") else baked_path)
                f.save()
                _get_app().window.FileUpdated.emit(str(f.id))

            try:
                _run_on_main_thread(_save_merged)
            except Exception as exc:
                log.warning("generate_transition: could not save merged tags: %s", exc)

            baked_duration = _ffprobe_video_duration(
                f.absolute_path() if hasattr(f, "absolute_path") else baked_path
            )
            if baked_duration < 0.5:
                baked_duration = duration_a + morph_dur_actual + dur_b
                log.warning(
                    "generate_transition: could not probe baked duration, using %.3fs",
                    baked_duration,
                )
            else:
                log.info("generate_transition: probed baked duration=%.3fs", baked_duration)

            try:
                placed = _run_on_main_thread(lambda: _install_baked_morph(clip_a.id, clip_b.id, f.id))
            except Exception as exc:
                return (
                    f"Error: The morph clip was baked and imported (file_id={f.id}) but replacing clips "
                    f"{clip_a.id} and {clip_b.id} failed: {exc}"
                )
            moved = placed["moved_later_items"]
            return _bake_receipt(
                f"Replaced clips {clip_a.id} and {clip_b.id} with the baked {baked_duration:.2f}s clip "
                f"{placed['timeline_clip_id']} (clip A + {morph_dur_actual:.1f}s AI morph + clip B) at "
                f"{placed['position']:.2f}s"
                + (f"; removed {len(placed['removed_transitions'])} transition(s) at their cut"
                   if placed["removed_transitions"] else "")
                + (f"; moved {len(moved)} later item(s) on the track by {placed['moved_by']:+.2f}s"
                   if moved else "")
                + ". One undo restores both clips.",
                **placed,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("generate_transition_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


# ---------------------------------------------------------------------------
# Transitions tools
# ---------------------------------------------------------------------------

def list_transitions(category="all", **_kw) -> str:
    """List the transitions of the Transitions dock (common, extra and the user folder)."""
    try:
        from classes import transition_ops

        category = str(category or "all").strip().lower()
        wanted = ("common", "extra", "user") if category == "all" else (category,)
        transitions = [
            {k: e[k] for k in ("name", "filename", "category", "path")}
            for e in transition_ops.catalog(wanted)
        ]
        if not transitions:
            return "No transitions found."

        data = {
            "total": len(transitions),
            "transitions": transitions[:50] if len(transitions) > 50 else transitions,
        }
        if len(transitions) > 50:
            data["note"] = (f"Showing first 50 of {len(transitions)} transitions; "
                            "search_transitions_tool finds the rest by name.")
        return json.dumps(data, indent=2)
    except Exception as e:
        log.error("list_transitions: %s", e, exc_info=True)
        return f"Error: {e}"


def search_transitions(query="", **_kw) -> str:
    """Search the transitions of the Transitions dock by name (common, extra and the user folder)."""
    try:
        from classes import transition_ops

        query_lower = (query or "").lower()
        matches = [
            {k: e[k] for k in ("name", "filename", "category", "path")}
            for e in transition_ops.catalog()
            if query_lower in e["name"].lower() or query_lower in e["key"]
        ]

        if not matches:
            return f"No transitions found matching '{query}'."

        return json.dumps({"query": query, "matches": len(matches), "transitions": matches}, indent=2)
    except Exception as e:
        log.error("search_transitions: %s", e, exc_info=True)
        return f"Error: {e}"


def apply_transition(
    clip1_id="",
    clip2_id="",
    transition_name="",
    duration="1.0",
    placement="between",
    **_kw,
) -> str:
    """Apply an OpenShot transition: placement='between' (two clips) or 'start'/'end' (one clip)."""
    place = (placement or "between").lower().strip()
    if place == "between":
        if not clip2_id:
            return "Error: clip2_id is required when placement='between'."
        return add_transition_between_clips(
            clip1_id, clip2_id, transition_name, duration, **_kw
        )
    if place in ("start", "end"):
        return add_transition_to_clip(
            clip1_id, transition_name, position=place, duration=duration, **_kw
        )
    return "Error: placement must be 'between', 'start', or 'end'."


def add_transition_between_clips(clip1_id="", clip2_id="", transition_name="", duration="1.0", **_kw) -> str:
    """Add a transition between two clips."""
    try:
        from classes.query import Clip
        from classes import info

        app = _get_app()
        win = app.window
        clip1 = Clip.get(id=clip1_id)
        clip2 = Clip.get(id=clip2_id)
        if not clip1:
            return f"Error: Clip '{clip1_id}' not found."
        if not clip2:
            return f"Error: Clip '{clip2_id}' not found."

        # Find transition file
        transitions_dir = os.path.join(info.PATH, "transitions")
        transition_path = None
        search_name = (transition_name or "").lower().replace(" ", "_")
        for cat in ["common", "extra"]:
            cat_dir = os.path.join(transitions_dir, cat)
            if os.path.exists(cat_dir):
                for fn in os.listdir(cat_dir):
                    fb = os.path.splitext(fn)[0]
                    if search_name in fb.lower() or fb.lower() in search_name:
                        transition_path = os.path.join(cat_dir, fn)
                        break
            if transition_path:
                break
        if not transition_path:
            return f"Error: Transition '{transition_name}' not found. Use search_transitions_tool."

        clip1_end = clip1.data.get("position", 0) + (clip1.data.get("end", 0) - clip1.data.get("start", 0))
        try:
            dur = float(duration)
        except ValueError:
            dur = 1.0

        layer1 = clip1.data.get("layer", 0)
        layer2 = clip2.data.get("layer", 0)
        target_layer_for_trans = max(layer1, layer2)
        transition_title = os.path.splitext(os.path.basename(transition_path))[0]

        # OpenShot Mask transitions require clips to OVERLAP to cross-dissolve.
        # Move clip2 backward by `dur` to create the overlap region, then place
        # the Mask at the overlap start.  Only clip2 is moved — downstream clips
        # are NOT rippled.
        clip2_pos = float(clip2.data.get("position", 0))
        clip2_id_val = clip2.data.get("id", clip2_id)
        new_clip2_pos = max(clip2_pos - dur, 0)

        # ALL Qt/openshot calls MUST run on the Qt main thread
        result_box = [None]

        def _do_transition():
            import openshot as _os

            # Build proper keyframe objects (required by OpenShot's Mask renderer)
            fps_data = app.project.get("fps") or {}
            fps_f = float(fps_data.get("num", 30)) / float(fps_data.get("den", 1) or 1)
            snap = lambda t: round(t * fps_f) / fps_f
            snapped_dur = snap(dur)
            snapped_c2_pos = snap(new_clip2_pos)

            # Step 1: Move clip2 backward to create overlap with clip1
            app.updates.update(["clips", {"id": clip2_id_val}], {"position": snapped_c2_pos})

            # Step 2: Place Mask transition in the overlap region
            brightness = _os.Keyframe()
            brightness.AddPoint(1, 1.0, _os.BEZIER)
            brightness.AddPoint(round(snapped_dur * fps_f) + 1, -1.0, _os.BEZIER)
            contrast = _os.Keyframe(3.0)
            trans_reader = _os.QtImageReader(transition_path)

            tid = str(uuid_module.uuid4())
            transition_data = {
                "id": tid,
                "layer": target_layer_for_trans,
                "position": snapped_c2_pos,   # start of the overlap region
                "start": 0.0,
                "end": snapped_dur,
                "brightness": json.loads(brightness.Json()),
                "contrast": json.loads(contrast.Json()),
                "reader": json.loads(trans_reader.Json()),
                "replace_image": False,
                "type": "Mask",
                "title": transition_title,
            }
            win.timeline.update_transition_data(transition_data, only_basic_props=False)
            result_box[0] = (tid, snapped_dur, snapped_c2_pos)

        # Moving clip2 + inserting the Mask is one user action -> one undo step.
        _run_on_main_thread(_atomic(app, _do_transition))
        tid, actual_dur, actual_pos = result_box[0] if result_box[0] else ("?", dur, new_clip2_pos)
        return (
            f"Added '{transition_name}' transition between clips (overlap: {actual_dur:.2f}s).\n"
            f"Clip2 moved to {actual_pos:.2f}s. Transition ID: {tid}"
        )
    except Exception as e:
        log.error("add_transition_between_clips: %s", e, exc_info=True)
        return f"Error: {e}"


def add_transition_to_clip(clip_id="", transition_name="", position="start", duration="1.0", **_kw) -> str:
    """Add a transition (fade in/out) to a single clip."""
    try:
        from classes.query import Clip
        from classes import info

        app = _get_app()
        clip = Clip.get(id=clip_id)
        if not clip:
            return f"Error: Clip '{clip_id}' not found."

        transitions_dir = os.path.join(info.PATH, "transitions")
        transition_path = None
        search_name = (transition_name or "").lower().replace(" ", "_")
        for cat in ["common", "extra"]:
            cat_dir = os.path.join(transitions_dir, cat)
            if os.path.exists(cat_dir):
                for fn in os.listdir(cat_dir):
                    fb = os.path.splitext(fn)[0]
                    if search_name in fb.lower() or fb.lower() in search_name:
                        transition_path = os.path.join(cat_dir, fn)
                        break
            if transition_path:
                break
        if not transition_path:
            return f"Error: Transition '{transition_name}' not found. Use search_transitions_tool."

        clip_position = clip.data.get("position", 0)
        clip_start = clip.data.get("start", 0)
        clip_end = clip.data.get("end", 0)
        clip_duration = clip_end - clip_start
        clip_layer = clip.data.get("layer", 0)
        win = app.window
        transition_title = os.path.splitext(os.path.basename(transition_path))[0]

        try:
            dur = float(duration)
        except ValueError:
            dur = 1.0

        if (position or "").lower() == "end":
            trans_position = clip_position + clip_duration - dur
        else:
            trans_position = clip_position

        result_box = [None]

        # Must run on Qt main thread — openshot.QtImageReader and update_transition_data
        # both touch Qt objects and dispatch to Qt listeners (properties_model, timeline, etc.)
        _is_fade_out = (position or "").lower() == "end"

        def _do_insert():
            import openshot as _os
            fps_data = app.project.get("fps") or {}
            fps_f = float(fps_data.get("num", 30)) / float(fps_data.get("den", 1) or 1)
            snap = lambda t: round(t * fps_f) / fps_f
            snapped_dur = snap(dur)

            brightness = _os.Keyframe()
            if _is_fade_out:
                # Fade OUT: clip starts visible (-1.0) and goes hidden (1.0)
                brightness.AddPoint(1, -1.0, _os.BEZIER)
                brightness.AddPoint(round(snapped_dur * fps_f) + 1, 1.0, _os.BEZIER)
            else:
                # Fade IN: clip starts hidden (1.0) and becomes visible (-1.0)
                brightness.AddPoint(1, 1.0, _os.BEZIER)
                brightness.AddPoint(round(snapped_dur * fps_f) + 1, -1.0, _os.BEZIER)
            contrast = _os.Keyframe(3.0)
            trans_reader = _os.QtImageReader(transition_path)

            tid = str(uuid_module.uuid4())
            transition_data = {
                "id": tid,
                "layer": clip_layer,
                "position": snap(trans_position),
                "start": 0.0,
                "end": snapped_dur,
                "brightness": json.loads(brightness.Json()),
                "contrast": json.loads(contrast.Json()),
                "reader": json.loads(trans_reader.Json()),
                "replace_image": False,
                "type": "Mask",
                "title": transition_title,
            }
            win.timeline.update_transition_data(transition_data, only_basic_props=False)
            result_box[0] = (tid, snapped_dur, snap(trans_position))

        _run_on_main_thread(_do_insert)
        tid, actual_dur, actual_pos = result_box[0] if result_box[0] else ("?", dur, trans_position)
        return (
            f"Added '{transition_name}' transition at {position} of clip.\n"
            f"ID: {tid}, Duration: {actual_dur:.2f}s, Position: {actual_pos:.2f}s"
        )
    except Exception as e:
        log.error("add_transition_to_clip: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Stock media / resummarize / reindex / planning handlers
# ---------------------------------------------------------------------------

def import_stock_media(
    source="",
    video_id="",
    link="",
    sound_id="",
    preview_url="",
    filename="",
    local_path="",
    **kwargs,
) -> str:
    """Download stock media (pexels|freesound) and import into Project Files, or import local_path."""
    path = (local_path or "").strip()
    if path:
        return add_stock_media_to_project(local_path=path, **kwargs)

    src = (source or "").lower().strip()
    if src == "pexels":
        dl = download_pexels_video_tool(
            video_id=video_id, link=link, filename=filename, **kwargs
        )
    elif src == "freesound":
        dl = download_freesound_music_tool(
            sound_id=sound_id, preview_url=preview_url, filename=filename, **kwargs
        )
    else:
        return "Error: source must be 'pexels' or 'freesound' (or pass local_path)."

    if dl.startswith("Error"):
        return dl
    if "Downloaded to:" in dl:
        path = dl.split("Downloaded to:", 1)[1].strip()
        return add_stock_media_to_project(local_path=path, **kwargs)
    return _as_error(dl)


def modify_clip(
    mode="replace",
    description="",
    query="",
    fade_ms="400",
    duration_seconds="",
    clip_query="",
    timeline_clip_id="",
    **kwargs,
) -> str:
    """AI-edit footage in a timeline clip (cloud video-to-video, uses credits): replace an object/look, or insert a new shot.

    mode="replace": regenerates the clip's first duration_seconds (default 5, max 8) with the
    change in description ("make the car red"); that part of the clip is replaced and the rest
    of the original continues after it. mode="insert": finds the moment described by the clip's
    index (or 80 % in), generates a ~3-5 s continuation shot and bakes it into the clip with
    crossfades; the clip gets longer and later clips on its track move right. The originals
    are replaced (not stacked on) in one undo step. Locked tracks are refused.
    """
    m = (mode or "replace").lower().strip()
    if m not in ("replace", "insert"):
        return f"Error: mode must be 'replace' or 'insert', got {mode!r}."
    text = (description or query or "").strip()
    if m == "insert":
        return insert_v2v_into_clip(
            query=text,
            fade_ms=fade_ms,
            clip_query=clip_query,
            timeline_clip_id=timeline_clip_id,
            **kwargs,
        )
    return replace_object_in_clip(
        description=text,
        duration_seconds=duration_seconds,
        clip_query=clip_query,
        timeline_clip_id=timeline_clip_id,
        **kwargs,
    )


def download_pexels_video_tool(video_id: str = "", link: str = "", filename: str = "", **kwargs) -> str:
    """Download a Pexels MP4 from the search-result link to the local machine."""
    try:
        if not link:
            return "Error: link is required (use the MP4 URL from search_pexels_videos_tool)."
        from classes.credits_client import charge_operation_on_success, check_operation

        _, _, blocked = check_operation("stock_add", "stock media download")
        if blocked:
            return _as_error(blocked)

        from classes.api_client import get_backend_client
        vid = int(video_id) if str(video_id).strip().isdigit() else 0
        result = get_backend_client().pexels_download(vid, link, filename=filename or "")
        err = result.get("error", "")
        path = result.get("local_path", "")
        if err or not path:
            return f"Error: Pexels download failed: {err or 'no file path'}"
        charge_operation_on_success(
            True, "stock_add", "stock_add", provider="pexels", note=f"video {vid}"
        )
        return f"Downloaded to: {path}"
    except Exception as exc:
        return f"Error downloading Pexels video: {exc}"


def download_freesound_music_tool(sound_id: str = "", preview_url: str = "", filename: str = "", **kwargs) -> str:
    """Download a Freesound HQ MP3 preview to the local machine."""
    try:
        if not preview_url:
            return "Error: preview_url is required (from search_freesound_music_tool)."
        try:
            sid = int(sound_id)
        except (ValueError, TypeError):
            return f"Error: sound_id must be numeric Freesound ID, not '{sound_id}'."

        from classes.credits_client import charge_operation_on_success, check_operation

        _, _, blocked = check_operation("stock_add", "stock media download")
        if blocked:
            return _as_error(blocked)

        from classes.api_client import get_backend_client
        result = get_backend_client().freesound_download(sid, preview_url, filename=filename or "")
        err = result.get("error", "")
        path = result.get("local_path", "")
        if err or not path:
            return f"Error: Freesound download failed: {err or 'no file path'}"
        charge_operation_on_success(
            True, "stock_add", "stock_add", provider="freesound", note=f"sound {sid}"
        )
        return f"Downloaded to: {path}"
    except Exception as exc:
        return f"Error downloading Freesound audio: {exc}"


def add_stock_media_to_project(local_path: str = "", **kwargs) -> str:
    """Import a downloaded stock file into Project Files."""
    try:
        if not local_path:
            return "Error: local_path is required."
        import os
        if not os.path.isfile(local_path):
            return f"Error: File not found: {local_path}"
        app = _get_app()
        files_model = app.window.files_model
        from classes.query import File

        def _resolve_imported_file(path: str):
            """Match project File after add_files (path keys often differ on Windows)."""
            variants = []
            for candidate in (path, os.path.normpath(path), os.path.realpath(path)):
                if candidate and candidate not in variants:
                    variants.append(candidate)
            for p in variants:
                f = File.get(path=p)
                if f:
                    return f
            base = os.path.basename(path).lower()
            best = None
            for candidate in File.filter():
                try:
                    cpath = candidate.data.get("path") or ""
                    abs_path = ""
                    try:
                        abs_path = candidate.absolute_path() or ""
                    except Exception:
                        abs_path = ""
                    names = {
                        os.path.basename(cpath).lower(),
                        os.path.basename(abs_path).lower(),
                    }
                    if base not in names:
                        continue
                    # Prefer exact absolute-path match when several share a basename.
                    if abs_path and os.path.normcase(os.path.normpath(abs_path)) in {
                        os.path.normcase(os.path.normpath(v)) for v in variants
                    }:
                        return candidate
                    if best is None:
                        best = candidate
                except Exception:
                    continue
            return best

        existing = _resolve_imported_file(local_path)
        if existing:
            chat_session_id = str(kwargs.get("chat_session_id", "") or "default")
            _last_split_file_id_by_chat_session[chat_session_id] = existing.id
            return (
                f"File already in project (file_id={existing.id}): {local_path}. "
                f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{existing.id}' "
                f"(or empty file_id to use this just-imported file) to place it on the timeline."
            )

        # MUST run on main thread — files_model.add_files touches Qt objects
        def _do_add():
            # quiet: a file libopenshot cannot read is reported below, not in a modal box.
            files_model.add_files([local_path], quiet=True)

        _run_on_main_thread(_do_add, timeout=30)

        f = _resolve_imported_file(local_path)
        if f:
            chat_session_id = str(kwargs.get("chat_session_id", "") or "default")
            _last_split_file_id_by_chat_session[chat_session_id] = f.id
            # Stock imports must finish Gemini indexing before the agent continues.
            wait_err = _wait_for_file_indexing(f.id, files_model, timeout_sec=1800)
            summary = ""
            try:
                ai = f.data.get("ai_metadata") if isinstance(f.data, dict) else {}
                if isinstance(ai, dict):
                    summary = str(ai.get("short_summary") or "").strip()
                    # Reload file in case worker updated metadata during wait
                    refreshed = File.get(id=f.id)
                    if refreshed and isinstance(refreshed.data, dict):
                        ai2 = refreshed.data.get("ai_metadata") or {}
                        if isinstance(ai2, dict) and ai2.get("short_summary"):
                            summary = str(ai2.get("short_summary") or "").strip()
                            f = refreshed
            except Exception:
                pass
            if wait_err:
                log.warning("Stock media indexing wait: %s", wait_err)
                return (
                    f"Added to project: {local_path} (file_id={f.id}) but indexing "
                    f"did not finish: {wait_err}. "
                    f"Call reindex_project_file_tool before relying on search. "
                    f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{f.id}' "
                    f"(or empty file_id to use this just-imported file) to place it on the timeline."
                )
            log.info("Stock media added to project: %s (id=%s)", local_path, f.id)
            summary_bit = f" Summary: {summary}" if summary else ""
            return (
                f"Added to project and indexed: {local_path} (file_id={f.id})."
                f"{summary_bit} "
                f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{f.id}' "
                f"(or empty file_id to use this just-imported file) to place it on the timeline."
            )
        log.error("Stock media import produced no File record: %s", local_path)
        return (
            f"Error: Failed to import into project files (no file_id): {local_path}. "
            f"The download may be corrupt or unsupported by the media engine."
        )
    except Exception as e:
        log.error("add_stock_media_to_project: %s", e, exc_info=True)
        return f"Error: {e}"


_NOT_BEING_INDEXED = "not being indexed"


def _wait_for_file_indexing(file_id: str, files_model, timeout_sec: int = 1800, idle_grace: float = 10.0) -> str:
    """Block until Gemini indexing for file_id finishes. Returns error string or ''.

    A file that stays unindexed with no queued or running indexer for *idle_grace* seconds is
    reported as not being indexed (its import skipped indexing, or indexing is off) instead of
    being waited on until the timeout.
    """
    import time
    from classes.query import File
    from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

    fid = str(file_id or "")
    if not fid:
        return "missing file_id"
    deadline = time.time() + max(1, int(timeout_sec))
    idle_since = None
    # Give the queue a moment to start the worker
    time.sleep(0.5)
    while time.time() < deadline:
        try:
            if hasattr(files_model, "is_file_indexing") and files_model.is_file_indexing(fid):
                time.sleep(1.0)
                continue
            f = File.get(id=fid)
            if not f or not isinstance(f.data, dict):
                time.sleep(0.5)
                continue
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            idx = get_index_block(ai)
            if twelvelabs_is_indexed(idx):
                return ""
            status = str((idx or {}).get("status") or "").lower()
            if status in ("failed", "skipped"):
                return str((idx or {}).get("error") or status)
            if status in ("", "ready") and ai.get("analyzed"):
                return ""
            # Still queued / not started — keep waiting while queue may drain
            if hasattr(files_model, "_indexing_queue"):
                queued = any(str(qid) == fid for qid, _ in (files_model._indexing_queue or []))
                active = any(
                    str(getattr(w, "file_data", {}).get("id", "")) == fid
                    for w in (getattr(files_model, "_active_indexers", None) or [])
                )
                if not queued and not active and status not in ("indexing", "uploading"):
                    # No worker and not ready — treat as finished-or-never-started
                    if twelvelabs_is_indexed(idx) or ai.get("analyzed"):
                        return ""
                    if status in ("failed", "skipped"):
                        return str((idx or {}).get("error") or status)
                    # Media type may not have been queued yet; small grace then fail soft
                    now = time.time()
                    idle_since = idle_since or now
                    if now - idle_since >= idle_grace:
                        return _NOT_BEING_INDEXED
                    time.sleep(1.0)
                    continue
                idle_since = None
        except Exception as exc:
            log.debug("wait indexing poll: %s", exc)
        time.sleep(1.0)
    return f"timed out after {timeout_sec}s"


def resummarize_project_file(file_id: str = "", **kwargs) -> str:
    """Re-run audiovisual summary for an already-indexed project file.

    Gemini indexing has no summarize-only path — callers should reindex instead.
    """
    try:
        if not file_id:
            return "Error: file_id is required."

        def _read_file_meta():
            from classes.query import File
            from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block
            f = File.get(id=file_id)
            if not f:
                return None
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            idx = get_index_block(ai)
            return {
                "path": f.data.get("path", ""),
                "duration": f.data.get("duration", 0) or 0,
                "indexed": twelvelabs_is_indexed(idx),
                "video_id": idx.get("video_id") or "",
                "provider": str(idx.get("provider") or "").lower(),
            }

        meta = _run_on_main_thread(_read_file_meta, timeout=10)
        if meta is None:
            return f"Error: File not found (id={file_id})."

        if "gemini" in (meta.get("provider") or ""):
            return (
                "Error: Summarize-only is not supported for Gemini indexing. "
                "Reindex the clip to refresh descriptions."
            )

        MAX_SECONDS = 30 * 60
        if meta["duration"] > MAX_SECONDS:
            return (
                f"Error: Clip is {meta['duration'] / 60:.1f} min — exceeds the "
                f"30-minute summarize limit."
            )
        if not meta.get("indexed") or not meta.get("video_id"):
            return (
                f"Error: File {file_id} is not indexed yet. "
                "Call reindex_project_file_tool first, then resummarize."
            )

        def _kick_off_summarize():
            try:
                files_model = _get_app().window.files_model
                # There is no summarize-only run any more: re-index to refresh it.
                files_model._index_file_async(file_id, force=True)
            except Exception as exc:
                log.warning("resummarize_project_file: failed to start summarize: %s", exc)

        try:
            # Fire and forget: nothing waits on this job, so nothing withdraws it.
            dispatcher = _get_dispatcher()
            dispatcher._dispatch.emit(_MainThreadJob(_kick_off_summarize, ()))
        except Exception:
            _kick_off_summarize()

        return (
            f"Summarize started for file {file_id} "
            f"(video_id={meta.get('video_id')}, {meta['path']})."
        )
    except Exception as e:
        log.error("resummarize_project_file: %s", e, exc_info=True)
        return f"Error: {e}"


def reindex_project_file(file_id: str = "", force: str = "false", **kwargs) -> str:
    """Re-index a project file and refresh its descriptions and transcript.

    Skips a file whose last index finished cleanly unless force=true; a failed
    or interrupted index always runs again. The fresh analysis is stored the
    way an import stores it. Runs entirely on a worker thread.  Only the brief
    project-data reads (file path, duration, project id) and storing the
    result are marshalled to the Qt main thread; the long-running reindex
    upload runs off the GUI thread.
    """
    try:
        if not file_id:
            return "Error: file_id is required."

        force_reindex = str(force or kwargs.get("force", "false")).strip().lower() in (
            "1", "true", "yes", "force",
        )

        def _read_project_state():
            from classes.query import File
            from classes.twelvelabs_match import get_index_block, index_is_complete
            f = File.get(id=file_id)
            if not f:
                return None
            project_id = ""
            try:
                project_id = _get_app().project.get("id") or ""
            except Exception:
                pass
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            tl = get_index_block(ai)
            return {
                "path": f.data.get("path", ""),
                "duration": f.data.get("duration", 0) or 0,
                "project_id": project_id,
                "existing_index_id": tl.get("index_id") or "",
                "twelvelabs": tl,
                # Ready handles left by a failed run do not count as indexed.
                "already_indexed": index_is_complete(ai),
            }

        state = _run_on_main_thread(_read_project_state, timeout=10)
        if state is None:
            return f"Error: File not found (id={file_id})."

        if state.get("already_indexed") and not force_reindex:
            tl = state.get("twelvelabs") or {}
            return (
                f"File {file_id} is already indexed "
                f"(index_id={tl.get('index_id', '')}, video_id={tl.get('video_id', '')}). "
                "Pass force=true to index it again, e.g. after the media file was replaced."
            )

        MAX_SECONDS = 30 * 60
        if state["duration"] > MAX_SECONDS:
            return (
                f"Error: Clip is {state['duration'] / 60:.1f} min — exceeds the "
                f"30-minute re-indexing limit."
            )

        from classes.api_client import get_backend_client
        from classes.credits_client import charge_operation_on_success, check_operation

        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Error: Gemini indexing is not configured on the backend, so re-indexing is unavailable."

        duration = float(state["duration"])
        _, _, blocked = check_operation(
            "indexing_per_minute",
            "video re-indexing",
            duration_seconds=duration,
        )
        if blocked:
            return _as_error(blocked)

        from classes.project_tl_index import build_project_index_name

        index_name = build_project_index_name(state["project_id"])

        result = client.reindex_video(
            file_id,
            state["path"],
            index_name=index_name,
            existing_index_id=state.get("existing_index_id") or "",
            force=force_reindex,
        )
        if isinstance(result, dict) and result.get("success"):
            charge_operation_on_success(
                True,
                "indexing_per_minute",
                provider="gemini",
                note=f"reindex {file_id}",
                duration_seconds=duration,
            )

            video_id = str(result.get("video_id") or "")
            index_block = {
                "status": "ready",
                "index_id": result.get("index_id", ""),
                "video_id": video_id,
                "index_name": index_name,
                "provider": "gemini",
            }
            # The indexing job returns the whole fresh analysis (description,
            # scenes, transcript cues); Gemini has no separate summarize step.
            fresh = result.get("ai_metadata")
            metadata = dict(fresh) if isinstance(fresh, dict) else {}
            metadata["index"] = {**(metadata.get("index") or {}), **index_block}
            metadata["twelvelabs"] = dict(metadata["index"])  # legacy key for older readers
            _run_on_main_thread(_store_indexing_result, file_id, metadata, timeout=10)

            note = "" if metadata.get("analyzed") else " No description came back with the new index."
            return (
                f"Re-indexing complete for file {file_id}. "
                f"index_id={result.get('index_id', '')}  video_id={video_id}."
                f"{note}"
            )

        failure = result if isinstance(result, dict) else {}
        reason = failure.get("error") or failure.get("message") or "unknown"
        fail_block = {
            "status": "failed",
            "error": reason,
            "index_name": index_name,
            "provider": "gemini",
        }
        # Record this attempt's error, so the file does not keep showing the last one.
        _run_on_main_thread(
            _store_indexing_result,
            file_id,
            {"error": reason, "index": fail_block, "twelvelabs": dict(fail_block)},
            timeout=10,
        )
        return f"Error: Re-indexing failed: {reason}"
    except Exception as e:
        log.error("reindex_project_file: %s", e, exc_info=True)
        return f"Error: {e}"


def _store_indexing_result(file_id, metadata):
    """Main thread: store an indexing outcome exactly as an import's indexing result."""
    files_model = getattr(getattr(_get_app(), "window", None), "files_model", None)
    if files_model is None:
        log.warning("No Project Files model; indexing result for %s was not stored", file_id)
        return
    files_model.apply_indexing_result(file_id, metadata)


def get_clips_with_full_metadata(detail_level="summary", **kwargs) -> str:
    """Return project files with AI metadata for planning (excludes hidden subclips by default)."""
    try:
        from classes.query import File
        from classes.timeline_clip_context import resolve_parent_file_id

        level = str(detail_level or kwargs.get("detail_level") or "summary").lower().strip()
        files = File.filter()
        if not files:
            return "No files in project."
        lines = ["Project files with full metadata:"]
        for f in files:
            d = f.data
            if d.get("zenvi_subclip") and level != "full":
                continue
            dur = d.get("duration", 0) or 0
            m, s = divmod(int(dur), 60)
            media_type = d.get("media_type", "?")
            name = d.get("name") or d.get("path", "?").split("/")[-1]
            ai = d.get("ai_metadata") or {}
            analyzed = ai.get("analyzed", False)
            tl = ai.get("twelvelabs", {}) or {}
            from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block
            from classes.tl_search_strategy import infer_tl_search_hint
            indexed = twelvelabs_is_indexed(tl)
            hint = infer_tl_search_hint(ai, name)
            scene_count = len(ai.get("scene_descriptions") or [])
            chapter_count = len(ai.get("chapters") or [])
            short = (ai.get("short_summary") or "")[:160]
            desc = (ai.get("description") or "")[:400]
            sounds = (ai.get("sounds") or "")[:160]
            transcript = (ai.get("transcript") or "")[:200]
            parent_id = resolve_parent_file_id(d, file_id=str(f.id or ""))
            alias_part = ""
            if d.get("zenvi_subclip") and parent_id and parent_id != str(f.id):
                alias_part = f"  alias_of={parent_id}\n"
            lines.append(
                f"\n  media_bin_file_id={f.id}  name={name}  type={media_type}  "
                f"duration={m}:{s:02d}\n"
                f"{alias_part}"
                f"    analyzed={analyzed}  indexed={indexed}  "
                f"chapter_count={chapter_count}  scene_count={scene_count}\n"
                f"    tl_search_hint={hint}\n"
                f"    twelvelabs_index_id={tl.get('index_id', '')}\n"
                f"    twelvelabs_index_name={tl.get('index_name', '')}\n"
                f"    twelvelabs_video_id={tl.get('video_id', '')}\n"
                f"    short_summary={short}\n"
                f"    description={desc}"
            )
            # Pre-Pegasus projects (or failed summarize) may only have Gemini tags.
            if not short and not desc:
                tags = ai.get("tags") if isinstance(ai.get("tags"), dict) else {}
                legacy_bits = []
                for key in ("objects", "scenes", "activities", "mood"):
                    vals = tags.get(key) or []
                    if isinstance(vals, list) and vals:
                        legacy_bits.append(
                            f"{key}=[{', '.join(str(v) for v in vals[:8] if v)}]"
                        )
                if legacy_bits:
                    lines.append(f"    legacy_tags={' '.join(legacy_bits)}")
                elif sounds or transcript:
                    if sounds:
                        lines.append(f"    sounds={sounds}")
                    if transcript:
                        lines.append(f"    transcript={transcript}")
            if level == "full":
                if sounds:
                    lines.append(f"    sounds={sounds}")
                if transcript:
                    lines.append(f"    transcript={transcript}")
            chapters = ai.get("chapters") or []
            if chapters:
                limit = len(chapters) if level == "full" else min(3, len(chapters))
                lines.append("    chapter_snippets:")
                for ch in chapters[:limit]:
                    if isinstance(ch, dict) and (ch.get("summary") or ch.get("title")):
                        t0 = float(ch.get("start", 0) or 0)
                        t1 = float(ch.get("end", t0) or t0)
                        title = str(ch.get("title") or "")
                        summary = str(ch.get("summary") or "")[:160]
                        lines.append(
                            f"      [{_fmt_mmss(t0)}-{_fmt_mmss(t1)}] {title}: {summary}"
                        )
            snippets = ai.get("scene_descriptions") or []
            if snippets and not chapters:
                limit = len(snippets) if level == "full" else min(3, len(snippets))
                lines.append("    scene_snippets:")
                for sc in snippets[:limit]:
                    if isinstance(sc, dict) and sc.get("description"):
                        t = float(sc.get("time", 0) or 0)
                        lines.append(f"      [{_fmt_mmss(t)}] {str(sc['description'])[:160]}")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_clips_with_full_metadata: %s", e, exc_info=True)
        return f"Error: {e}"


def _overlay_index_boosts(contexts) -> list:
    """Optional TwelveLabs search boosts mapped onto timeline times.

    Falls back to [] when indexing is unavailable (lexical scoring still applies).
    """
    boosts = []
    try:
        from classes.api_client import get_backend_client
        from classes.project_tl_index import (
            collect_project_twelvelabs_index,
            map_search_hit_to_file,
        )
        from classes.mg_placement import AVOID_QUERY, PREFER_QUERY

        info = collect_project_twelvelabs_index()
        index_id = str((info or {}).get("index_id") or "").strip()
        if not index_id:
            return []
        client = get_backend_client()
        if not client.is_indexing_configured():
            return []
        video_map = (info or {}).get("video_map") or {}
        queries = (
            (AVOID_QUERY, -3, True, False),
            (PREFER_QUERY, 3, False, True),
        )
        by_file = {}
        for ctx in contexts or []:
            fid = str(getattr(ctx, "file_id", "") or "")
            pfid = str(getattr(ctx, "parent_file_id", "") or "")
            for key in {fid, pfid}:
                if key:
                    by_file.setdefault(key, []).append(ctx)

        for query, delta, is_avoid, is_prefer in queries:
            resp = client.search(query, top_k=12, index_id=index_id, page_limit=24)
            if resp.get("error"):
                continue
            for r in resp.get("results") or []:
                if not isinstance(r, dict):
                    continue
                fid, _fname = map_search_hit_to_file(r, video_map)
                fid = str(fid or "").strip()
                if not fid or fid not in by_file:
                    continue
                try:
                    hit_start = float(r.get("start") or 0)
                except (TypeError, ValueError):
                    continue
                for ctx in by_file[fid]:
                    pos = float(getattr(ctx, "timeline_position", 0) or 0)
                    end = float(getattr(ctx, "timeline_end", pos + 1) or (pos + 1))
                    src_start = float(getattr(ctx, "source_start", 0) or 0)
                    clip_dur = max(0.1, end - pos)
                    local = hit_start - src_start
                    if 0 <= local <= clip_dur:
                        boosts.append(
                            {
                                "t": pos + local,
                                "score_delta": delta,
                                "avoid": is_avoid,
                                "prefer": is_prefer,
                            }
                        )
    except Exception as exc:
        log.debug("propose_overlay_windows: index boost skipped: %s", exc)
    return boosts


def propose_overlay_windows(beat_count="4", prefer_transparent="true", **_kw) -> str:
    """Propose safe timeline windows for MG overlays/plates.

    Uses scene metadata + lexical/embedding-index scoring. Returns JSON with
    windows[{t, score, avoid, prefer, scene_label, track_hint, confidence,
    suggest_transparent, layout_region}].

    Agent must: bake layout_region into session/draft.html; publish with matching
    transparent flag; place via place_motion_graphic_tool (overlay|gap|cut_in).
    """
    import json

    from classes.mg_placement import (
        apply_embedding_time_boosts,
        layout_region_for,
        score_scene_blob,
    )

    try:
        n = int(float(str(beat_count or "4").strip() or "4"))
    except Exception:
        n = 4
    n = max(1, min(8, n))
    prefer_tr = str(prefer_transparent or "true").strip().lower() not in ("false", "0", "no")

    try:
        from classes.timeline_clip_context import enumerate_timeline_contexts

        contexts = enumerate_timeline_contexts() or []
    except Exception as exc:
        log.warning("propose_overlay_windows: timeline read failed: %s", exc)
        contexts = []

    overlay_layer = 3000000
    mid_layer = 2000000
    try:
        app = _get_app()
        layers = app.project.get("layers") or []
        nums = sorted(int(L.get("number", 0)) for L in layers if isinstance(L, dict))
        if nums:
            overlay_layer = nums[-1] + 1000000 if nums[-1] < 9000000 else nums[-1]
            mid_layer = nums[len(nums) // 2] if len(nums) > 1 else nums[0]
    except Exception:
        pass

    segments = []
    windows = []
    timeline_end = 0.0
    for ctx in contexts:
        pos = float(ctx.timeline_position or 0)
        end = float(ctx.timeline_end or (pos + 1.0))
        timeline_end = max(timeline_end, end)
        ai = ctx.effective_metadata or {}
        src_start = float(getattr(ctx, "source_start", 0) or 0)
        clip_dur = max(0.1, end - pos)

        text_bits = [
            str(ctx.summary_preview or ""),
            str(ai.get("short_summary") or ""),
            str(ai.get("description") or ""),
        ]
        for ch in ai.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            text_bits.append(str(ch.get("title") or ""))
            text_bits.append(str(ch.get("summary") or ""))
            ch_start = ch.get("start")
            try:
                if ch_start is not None:
                    local = float(ch_start) - src_start
                    if 0 <= local <= clip_dur:
                        t = pos + local
                        sc, avoid, prefer = score_scene_blob(
                            f"{ch.get('title') or ''} {ch.get('summary') or ''}"
                        )
                        windows.append(
                            {
                                "t": t,
                                "score": sc,
                                "avoid": avoid,
                                "prefer": prefer,
                                "label": str(ch.get("title") or ch.get("summary") or "")[:80],
                            }
                        )
            except (TypeError, ValueError):
                pass
        for scn in ai.get("scene_descriptions") or []:
            if not isinstance(scn, dict):
                continue
            desc = str(scn.get("description") or "")
            text_bits.append(desc)
            try:
                st = scn.get("time")
                if st is None:
                    st = scn.get("start")
                if st is not None:
                    local = float(st) - src_start
                    if 0 <= local <= clip_dur:
                        t = pos + local
                        sc, avoid, prefer = score_scene_blob(desc)
                        windows.append(
                            {
                                "t": t,
                                "score": sc + 1,
                                "avoid": avoid,
                                "prefer": prefer,
                                "label": desc[:80],
                            }
                        )
            except (TypeError, ValueError):
                pass

        blob = " ".join(text_bits)
        sc, avoid, prefer = score_scene_blob(blob)
        segments.append(
            {
                "position": pos,
                "end": end,
                "avoid": avoid,
                "prefer": prefer,
                "score": sc,
            }
        )
        for frac, bonus in ((0.08, 0), (0.5, 0), (0.88, 0)):
            t = pos + clip_dur * frac
            windows.append(
                {
                    "t": t,
                    "score": sc + bonus,
                    "avoid": avoid,
                    "prefer": prefer,
                    "label": (ctx.title or blob)[:80],
                }
            )

    # Index / embedding search boosts (best-effort)
    apply_embedding_time_boosts(windows, _overlay_index_boosts(contexts))

    segments.sort(key=lambda s: s["position"])
    gaps = []
    if segments:
        if segments[0]["position"] > 0.4:
            gaps.append(max(0.0, segments[0]["position"] * 0.15))
        for a, b in zip(segments, segments[1:]):
            if b["position"] - a["end"] >= 0.4:
                gaps.append((a["end"] + b["position"]) / 2.0)
                windows.append(
                    {
                        "t": a["end"] - 0.15,
                        "score": 2,
                        "avoid": False,
                        "prefer": True,
                        "label": "near cut",
                    }
                )
    else:
        timeline_end = 24.0
        for t in (0.0, 6.0, 12.0, 18.0, 22.0):
            windows.append({"t": t, "score": 1, "avoid": False, "prefer": True, "label": ""})

    if timeline_end <= 0:
        timeline_end = 24.0

    if n == 1:
        anchors = [0.0 if timeline_end < 1 else min(1.0, timeline_end * 0.1)]
    else:
        anchors = [timeline_end * (i / (n - 1)) for i in range(n)]

    def _snap(target, used):
        target = max(0.0, min(float(timeline_end), float(target)))
        cands = sorted(windows, key=lambda w: (abs(w["t"] - target), -w["score"]))
        for w in cands:
            t = max(0.0, min(timeline_end, float(w["t"])))
            if prefer_tr and w.get("avoid") and w["score"] < 0:
                continue
            if all(abs(t - u) >= 1.2 for u in used):
                conf = (
                    "high"
                    if w.get("prefer") or w["score"] >= 2
                    else ("low" if w.get("avoid") else "medium")
                )
                return (
                    t,
                    w.get("label") or "",
                    conf,
                    bool(w.get("avoid")),
                    bool(w.get("prefer")),
                    int(w["score"]),
                )
        return target, "", "medium", False, False, 0

    used_positions = []
    out_windows = []
    for i in range(n):
        use_gap = (not prefer_tr) or (i == max(1, n // 2) and gaps)
        pos = None
        scene_label = ""
        conf = "medium"
        avoid = False
        prefer = False
        score = 0
        track_hint = overlay_layer
        is_gap = False
        if use_gap and gaps:
            for g in gaps:
                g = max(0.0, min(timeline_end, float(g)))
                if all(abs(g - u) >= 1.0 for u in used_positions):
                    pos = g
                    scene_label = "gap — opaque plate candidate"
                    conf = "high"
                    track_hint = mid_layer
                    is_gap = True
                    prefer = True
                    break
        if pos is None:
            pos, scene_label, conf, avoid, prefer, score = _snap(anchors[i], used_positions)
            track_hint = overlay_layer
            is_gap = False
        pos = round(max(0.0, min(timeline_end, float(pos))), 2)
        used_positions.append(pos)
        region = layout_region_for(
            avoid=avoid, prefer=prefer, is_gap=is_gap, score=score
        )
        suggest_tr = bool(prefer_tr and track_hint == overlay_layer and not is_gap)
        out_windows.append(
            {
                "t": pos,
                "score": score,
                "avoid": avoid,
                "prefer": prefer,
                "scene_label": scene_label[:80],
                "track_hint": int(track_hint),
                "confidence": conf,
                "suggest_transparent": suggest_tr,
                "layout_region": region,
                "place_mode": (
                    "gap" if is_gap else ("overlay" if suggest_tr else "cut_in")
                ),
            }
        )

    payload = {
        "timeline_end": round(timeline_end, 2),
        "clip_count": len(segments),
        "windows": out_windows,
        "guidance": (
            "For each beat: decide transparent vs opaque first. "
            "layout_region → bake into session/draft.html "
            "(lower_third/corner_* = non-blocking overlay HTML; full_frame = sting; "
            "mid_plate = opaque plate). "
            "publish_session_draft_tool(transparent=true|false) matching suggest_transparent. "
            "Then place_motion_graphic_tool(mode=overlay|gap|cut_in) — never stack opaque "
            "plates over hero footage with add_clip on the top overlay track."
        ),
    }
    return json.dumps(payload, indent=2)


def place_motion_graphic(
    file_id="",
    position_seconds="",
    duration_seconds="",
    mode="overlay",
    track="",
    layout_region="",
    query="",
    **_kw,
) -> str:
    """Place a HyperFrames render with overlay/gap/cut_in enforcement.

    mode=overlay → transparent file on a HIGH layer (refuses opaque).
    mode=gap → opaque only when primary track is clear at [t,t+dur).
    mode=cut_in → opaque: ripple primary-track clips at/after t, then place as a cut.
    """
    from classes.mg_placement import (
        file_looks_transparent,
        primary_track_overlaps,
        ripple_positions,
    )
    from classes.query import Clip, File
    from classes.track_display import (
        format_track_label_for_llm,
        normalize_track_or_layer_arg,
    )

    try:
        fid = str(file_id or "").strip()
        if not fid:
            return "Error: file_id is required for place_motion_graphic_tool"
        f = File.get(id=fid)
        if not f:
            return f"Error: File not found for id={fid}."
        file_data = dict(f.data or {})
        is_transparent = file_looks_transparent(file_data)
        mode_s = str(mode or "overlay").strip().lower() or "overlay"
        if mode_s not in ("overlay", "gap", "cut_in"):
            return "Error: mode must be overlay|gap|cut_in"

        try:
            t = float(str(position_seconds).strip() or "0")
        except (TypeError, ValueError):
            return "Error: position_seconds must be a number"
        t = max(0.0, t)
        try:
            dur = float(str(duration_seconds).strip() or "0")
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            try:
                dur = float(file_data.get("duration") or 0) or float(
                    (file_data.get("reader") or {}).get("duration") or 0
                )
            except (TypeError, ValueError):
                dur = 3.0
        dur = max(0.5, min(dur, 60.0))

        app = _get_app()
        layers = app.project.get("layers") or []
        nums = sorted(
            int(L.get("number", 0))
            for L in layers
            if isinstance(L, dict) and L.get("number") is not None
        )
        if not nums:
            nums = [1000000, 2000000, 3000000]
        primary_layer = nums[0]
        mid_layer = nums[len(nums) // 2] if len(nums) > 1 else nums[0]
        overlay_layer = nums[-1]

        if str(track or "").strip():
            resolved, err = normalize_track_or_layer_arg(str(track).strip(), layers)
            if err:
                return err
            track_num = int(resolved)
        elif mode_s == "overlay":
            track_num = int(overlay_layer)
        elif mode_s == "gap":
            track_num = int(mid_layer)
        else:
            track_num = int(primary_layer)

        clips_raw = [
            dict(c.data or {})
            for c in Clip.filter()
            if isinstance(getattr(c, "data", None), dict)
        ]

        if mode_s == "overlay":
            if not is_transparent:
                return (
                    "Error: mode=overlay requires a transparent HyperFrames import "
                    "(ai_metadata.transparent / transparent_overlay tag / webm). "
                    "Use mode=gap or mode=cut_in for opaque plates — never stack opaque "
                    "over hero footage."
                )
            if track_num < mid_layer:
                track_num = int(overlay_layer)
        else:
            # gap / cut_in → opaque path
            if is_transparent:
                return (
                    "Error: mode=%s is for opaque plates. Transparent overlays must use "
                    "mode=overlay on a high track." % mode_s
                )
            if mode_s == "gap":
                if primary_track_overlaps(
                    clips_raw, layer=int(primary_layer), t0=t, t1=t + dur
                ):
                    return (
                        "Error: gap mode refused — primary track has footage overlapping "
                        f"[{t:.2f}, {t + dur:.2f}). Use mode=cut_in to ripple clips, or "
                        "pick a true gap from propose_overlay_windows_tool."
                    )

        region = str(layout_region or "").strip()
        watch_query = placement_watch_query(
            file_data,
            str(query or _kw.get("query") or "").strip(),
            extra=region,
        )

        shift_box = [[]]

        def _ripple_and_stamp():
            if mode_s == "cut_in":
                shifts = ripple_positions(
                    clips_raw, layer=int(primary_layer), t=t, delta=dur
                )
                shift_box[0] = shifts
                for cid, new_pos in shifts:
                    app.updates.update(
                        ["clips", {"id": cid}],
                        {"position": float(new_pos)},
                    )
            try:
                ai = dict(file_data.get("ai_metadata") or {})
                ai["mg_placement"] = {
                    "mode": mode_s,
                    "layout_region": region,
                    "transparent": bool(is_transparent),
                    "position_seconds": t,
                    "duration_seconds": dur,
                    "track": track_num,
                }
                if not is_transparent:
                    ai["transparent"] = False
                f.data["ai_metadata"] = ai
                if hasattr(f, "save"):
                    f.save()
                else:
                    app.updates.update(
                        ["files", {"id": fid}],
                        {"ai_metadata": ai},
                    )
            except Exception as stamp_exc:
                log.debug("mg_placement stamp failed: %s", stamp_exc)
            return True

        # Ripple + metadata stamp + placement are one user action, so they
        # share a transaction id and undo as a single step.
        _composite_tid = _new_transaction_id()
        _run_on_main_thread(_atomic(app, _ripple_and_stamp, tid=_composite_tid))
        # add_clip marshals Qt mutations itself
        result = add_clip_to_timeline(
            file_id=fid,
            position_seconds=str(t),
            track=str(track_num),
            duration_seconds=str(dur),
            query=watch_query,
            chat_session_id=_kw.get("chat_session_id", ""),
            transaction_id=_composite_tid,
        )

        track_lbl = format_track_label_for_llm(int(track_num), layers)
        if isinstance(result, str) and result.startswith("Error"):
            if mode_s == "cut_in" and shift_box[0]:
                def _undo_ripple():
                    for cid, new_pos in shift_box[0]:
                        app.updates.update(
                            ["clips", {"id": cid}],
                            {"position": float(new_pos) - float(dur)},
                        )
                try:
                    _run_on_main_thread(_undo_ripple)
                except Exception as undo_exc:
                    log.warning("cut_in ripple rollback failed: %s", undo_exc)
            return result
        return (
            f"{result} [mg_place mode={mode_s} layout_region={region or 'n/a'} "
            f"transparent={is_transparent} track={track_lbl}]. "
            "NEXT: get_timeline_state_tool once to verify, then continue next beat."
        )
    except Exception as e:
        log.error("place_motion_graphic: %s", e, exc_info=True)
        return f"Error: {e}"


def suggest_motion_graphics_placements(brief="", beat_count="4", **_kw) -> str:
    """Deprecated — use propose_overlay_windows_tool (timing) + agent-authored beats_json."""
    return (
        "Error: suggest_motion_graphics_placements_tool is removed. "
        "Call propose_overlay_windows_tool(beat_count=...) for timing/layout_region, "
        "edit session/draft.html (lint optional), publish_session_draft_tool, "
        "fetch_motion_graphics_video_tool, then place_motion_graphic_tool(mode=overlay|gap|cut_in). "
        "Never put the creative brief into on-screen title text."
    )


def get_timeline_placements_metadata(detail_level="summary", **_kw) -> str:
    """Return one row per timeline clip with trim-aware effective metadata."""
    try:
        from classes.timeline_clip_context import enumerate_timeline_contexts

        level = str(detail_level or _kw.get("detail_level") or "summary").lower().strip()
        contexts = enumerate_timeline_contexts()
        if not contexts:
            return "No clips on timeline."

        dupe_groups: dict[str, list] = {}
        for ctx in contexts:
            key = f"{ctx.file_id}:{ctx.layer}"
            dupe_groups.setdefault(key, []).append(ctx)

        lines = [f"Timeline placements ({len(contexts)}):"]
        for ctx in sorted(contexts, key=lambda c: (int(c.layer or 0), c.timeline_position)):
            dup_key = f"{ctx.file_id}:{ctx.layer}"
            dupes = dupe_groups.get(dup_key, [])
            occ_hint = ""
            if len(dupes) > 1:
                ranked = sorted(dupes, key=lambda c: c.timeline_position)
                for idx, dc in enumerate(ranked, 1):
                    if dc.timeline_clip_id == ctx.timeline_clip_id:
                        occ_hint = f" occurrence_hint={idx}"
                        break
            group_id = dup_key if len(dupes) > 1 else ""
            ai = ctx.effective_metadata or {}
            scenes = ai.get("scene_descriptions") or []
            scene_limit = len(scenes) if level == "full" else min(3, len(scenes))
            lines.append(
                f"\n  timeline_clip_id={ctx.timeline_clip_id} file_id={ctx.file_id} "
                f"parent_file_id={ctx.parent_file_id}{occ_hint}\n"
                f"    title={ctx.title!r} track={ctx.layer} ui_track={ctx.ui_track} "
                f"position={ctx.timeline_position:.2f}s timeline_end={ctx.timeline_end:.2f}s\n"
                f"    source_window={ctx.source_start:.2f}-{ctx.source_end:.2f}s "
                f"index_status={ctx.index_status!r} duplicate_group={group_id!r}\n"
                f"    summary_preview={ctx.summary_preview!r}"
            )
            if scenes and scene_limit:
                lines.append("    effective_scenes:")
                for sc in scenes[:scene_limit]:
                    if isinstance(sc, dict) and sc.get("description"):
                        t = float(sc.get("time", 0) or 0)
                        lines.append(f"      [{_fmt_mmss(t)}] {str(sc['description'])[:160]}")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_timeline_placements_metadata: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool name → handler mapping
# ---------------------------------------------------------------------------

def get_timeline_state(**_kw) -> str:
    """Return a structured snapshot of the current timeline: tracks, clips with positions, and effects.

    Always lists every project layer (including empty tracks) so planners can pick ui_track values
    even when the timeline has no clips yet.
    """
    try:
        from classes.query import Clip
        from classes.timeline_clip_context import build_timeline_clip_context

        app = _get_app()

        layers = app.project.get("layers") or []

        clips = Clip.filter()
        effects_raw = app.project.get("effects") or []

        # Group clips by layer (store tuple of clip object and data)
        by_layer = {}
        for c in clips:
            d = c.data
            layer = d.get("layer", 0)
            by_layer.setdefault(layer, []).append((c, d))

        def _track_heading(layer_num, layer_obj=None):
            ui = layer_number_to_display_index(int(layer_num), layers)
            tid = ""
            label = ""
            if layer_obj is not None:
                tid = str(layer_obj.get("id", ""))
                label = (layer_obj.get("label") or layer_obj.get("name") or "").strip()
            else:
                for L in layers:
                    if int(L.get("number") or 0) == int(layer_num):
                        tid = str(L.get("id", ""))
                        label = (L.get("label") or L.get("name") or "").strip()
                        break
            z_from_bottom = (ui - 1) if ui is not None else "?"
            parts = [f"layer_number={layer_num}", f"z_from_bottom={z_from_bottom}"]
            if ui is not None:
                parts.append(f"ui_track={ui}")
            if tid:
                parts.append(f"track_id={tid}")
            if label:
                parts.append(f"label={label!r}")
            return " | ".join(parts)

        lines = ["=== TIMELINE STATE ==="]
        lines.append(
            "Z-ORDER: higher layer_number covers lower. "
            "Track labels are cosmetic names only — never infer priority from the label text. "
            "Call list_layers_tool for TRACK_STACK_JSON before multi-track placement. "
            "Hero/foreground → highest layer_number; backgrounds → lowest."
        )
        if not clips and not effects_raw:
            lines.append("Timeline is empty — no clips or effects have been added yet.")

        # Always emit every project track (high layer number first = top of stack).
        from classes.track_display import track_stack_json

        asc = layers_sorted_by_number(layers)
        emitted_layer_nums = set()
        if asc:
            for L in reversed(asc):
                layer_num = int(L.get("number") or 0)
                emitted_layer_nums.add(layer_num)
                lines.append(f"\n{_track_heading(layer_num, L)}:")
                layer_clips = by_layer.get(layer_num) or []
                if not layer_clips:
                    lines.append("  (empty)")
                    continue
                for c, d in sorted(layer_clips, key=lambda x: x[1].get("position", 0)):
                    clip_dur = float(d.get("end", 0) or 0) - float(d.get("start", 0) or 0)
                    if clip_dur <= 0:
                        try:
                            from classes.query import File as _FileDur
                            _fo = _FileDur.get(id=d.get("file_id", ""))
                            if _fo:
                                from classes.ai_metadata_utils import get_source_window
                                ss, se = get_source_window(d, _fo.data)
                                clip_dur = se - ss
                        except Exception:
                            clip_dur = 0
                    clip_end = d.get("position", 0) + clip_dur
                    summary_preview = ""
                    analyzed_part = ""
                    source_part = ""
                    role_part = ""
                    try:
                        from classes.query import File as _File
                        fobj = _File.get(id=d.get("file_id", ""))
                        if fobj and isinstance(fobj.data, dict):
                            fname = (
                                fobj.data.get("name")
                                or os.path.basename(str(fobj.data.get("path") or ""))
                                or d.get("file_id", "?")
                            )
                            ctx = build_timeline_clip_context(c, d, fobj.data, layers=layers)
                            summary_preview = ctx.summary_preview
                            source_part = (
                                f" source={ctx.source_start:.1f}-{ctx.source_end:.1f}s"
                            )
                            role_part = f" audio_role={_audio_role_of(d, fobj.data, ctx)}"
                            if not _file_is_analyzed(fobj.data):
                                analyzed_part = " analyzed=False"
                        else:
                            fname = d.get("file_id", "?")
                    except Exception:
                        fname = d.get("file_id", "?")
                    summary_part = f" summary_preview={summary_preview!r}" if summary_preview else ""
                    lines.append(
                        f"  timeline_clip_id={c.id} media_bin_file_id={d.get('file_id','')} "
                        f"file={fname!r} title={(d.get('title') or d.get('label') or '')!r}"
                        f"{summary_part}{analyzed_part}{source_part}{role_part}"
                        f" @ {d.get('position',0):.2f}s–{clip_end:.2f}s (dur={clip_dur:.2f}s)"
                    )
        else:
            lines.append("\n(No layers/tracks in project.)")

        # Orphan clips on layer numbers not in project.layers
        for layer_num in sorted(by_layer.keys(), reverse=True):
            if int(layer_num) in emitted_layer_nums:
                continue
            lines.append(f"\n{_track_heading(layer_num)}:")
            for c, d in sorted(by_layer[layer_num], key=lambda x: x[1].get("position", 0)):
                clip_dur = float(d.get("end", 0) or 0) - float(d.get("start", 0) or 0)
                clip_end = d.get("position", 0) + clip_dur
                lines.append(
                    f"  timeline_clip_id={c.id} media_bin_file_id={d.get('file_id','')} "
                    f"file={d.get('file_id','')!r} title={(d.get('title') or d.get('label') or '')!r}"
                    f" @ {d.get('position',0):.2f}s–{clip_end:.2f}s (dur={clip_dur:.2f}s)"
                )

        # Summarise effects/transitions
        if effects_raw:
            lines.append(f"\nEffects/Transitions ({len(effects_raw)}):")
            for e in effects_raw:
                lines.append(
                    f"  id={e.get('id','')} title={e.get('title','?')!r}"
                    f" layer={e.get('layer','')} @ {e.get('position',0):.2f}s end={e.get('end',0):.2f}s"
                )

        # Total timeline duration
        if not clips:
            lines.append("\nTotal timeline duration: 0.00s")
        else:
            all_ends = [
                d.get("position", 0) + (d.get("end", 0) - d.get("start", 0))
                for c in clips for d in [c.data]
            ]
            if all_ends:
                lines.append(f"\nTotal timeline duration: {max(all_ends):.2f}s")

        lines.append(f"\nTRACK_STACK_JSON={track_stack_json(layers)}")

        structured_clips = []
        for c in clips:
            d = c.data if isinstance(c.data, dict) else {}
            layer = int(d.get("layer") or 0)
            ui = layer_number_to_display_index(layer, layers)
            structured_clips.append({
                "id": str(c.id),
                "file_id": str(d.get("file_id") or ""),
                "title": str(d.get("title") or d.get("label") or ""),
                "layer": layer,
                "track": ui,
                "position": float(d.get("position") or 0),
                "start": float(d.get("start") or 0),
                "end": float(d.get("end") or 0),
            })
        structured_tracks = []
        for L in layers_sorted_by_number(layers):
            layer_num = int(L.get("number") or 0)
            structured_tracks.append({
                "id": str(L.get("id") or ""),
                "layer": layer_num,
                "track": layer_number_to_display_index(layer_num, layers),
                "label": str(L.get("label") or L.get("name") or ""),
            })
        from classes.agent_tools.receipt import ToolReceipt
        return ToolReceipt.applied(
            "get_timeline_state_tool",
            f"Timeline: {len(structured_clips)} clip(s), {len(structured_tracks)} track(s).",
            undo_steps=0,
            data={
                "clips": structured_clips,
                "tracks": structured_tracks,
                "effects": list(effects_raw),
                "legacy_text": "\n".join(lines),
            },
        ).to_json()
    except Exception as e:
        log.error("get_timeline_state: %s", e, exc_info=True)
        return f"Error: {e}"


def build_editor_snapshot_for_chat(max_chars: int = 5500) -> str:
    """Compact media-bin + timeline for LLM grounding. Call from Qt GUI thread.

    Empty timeline must NOT look like an empty project — always list media-bin
    files (with summary_preview when available) so the agent can plan from them.
    """
    try:
        import os
        from classes.query import File
        from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

        files = File.filter() or []
        media_lines: list[str] = []
        for f in files:
            d = f.data if isinstance(getattr(f, "data", None), dict) else {}
            if d.get("zenvi_subclip"):
                continue
            name = d.get("name") or os.path.basename(str(d.get("path") or "")) or "?"
            dur = float(d.get("duration", 0) or 0)
            ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
            analyzed = bool(ai.get("analyzed"))
            indexed = twelvelabs_is_indexed(get_index_block(ai))
            preview = _summary_preview_for_file_data(d)
            media_lines.append(
                f"  media_bin_file_id={f.id} name={name!r} duration={dur:.2f}s "
                f"analyzed={analyzed} indexed={indexed} "
                f"summary_preview={preview!r}"
            )

        n_visible = len(media_lines)
        head = (
            f"[Editor snapshot]\n"
            f"Project files count: {n_visible}\n"
            f"NOTE: media-bin files exist independently of the timeline; "
            f"an empty timeline does NOT mean no project files.\n"
        )
        if media_lines:
            # Cap listing so snapshot stays within max_chars with timeline.
            shown = media_lines[:20]
            head += "MEDIA_BIN:\n" + "\n".join(shown)
            if n_visible > len(shown):
                head += f"\n  ... and {n_visible - len(shown)} more (use list_files_tool / get_clips_with_full_metadata_tool)\n"
            else:
                head += "\n"
        else:
            head += "MEDIA_BIN: (empty)\n"

        tl = get_timeline_state()
        body = (tl or "").strip()
        out = f"{head}TIMELINE:\n{body}\n" if body else f"{head}TIMELINE: (empty or unavailable)\n"
        if len(out) > max_chars:
            out = out[: max(0, max_chars - 24)].rstrip() + "\n... (truncated)\n"
        return out + "[/Editor snapshot]\n"
    except Exception as e:
        log.debug("build_editor_snapshot_for_chat: %s", e)
        return ""


# --------------------------------------------------------------------------
# Audio mixing / context-aware ducking
# --------------------------------------------------------------------------

def _audio_float(value, default=None):
    """Parse an LLM-supplied numeric arg; blank/garbage falls back to default."""
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _audio_bool(value, default=False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "y", "on")


def _audio_id_list(value) -> list:
    """Comma/space separated clip ids to a list, preserving order."""
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = str(value or "").replace(",", " ").split()
    out = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _collect_timeline_audio(app, layer_filter=None):
    """Every timeline clip with audio, tagged with role + speech windows.

    Roles are derived from data that already exists (reader streams + indexed
    transcript cues) — see classes.audio_mix.classify_clip_audio_role.
    """
    from classes.query import Clip, File
    from classes.ai_metadata_utils import get_effective_ai_metadata
    from classes.timeline_clip_context import clear_metadata_lookup_cache
    from classes import audio_mix as am

    clear_metadata_lookup_cache()
    layers_raw = app.project.get("layers") or []
    fps = app.project.get("fps") or {"num": 30, "den": 1}
    file_cache: dict = {}
    entries = []

    for clip_obj in Clip.filter():
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        try:
            layer_num = int(data.get("layer") or 0)
        except (TypeError, ValueError):
            layer_num = 0
        if layer_filter is not None and layer_num != layer_filter:
            continue

        file_id = str(data.get("file_id") or "")
        if file_id and file_id not in file_cache:
            try:
                fobj = File.get(id=file_id)
                file_cache[file_id] = fobj.data if fobj and isinstance(fobj.data, dict) else None
            except Exception:
                file_cache[file_id] = None
        file_data = file_cache.get(file_id)

        try:
            effective = get_effective_ai_metadata(
                file_data, data, clip_ai_metadata=data.get("ai_metadata")
            )
        except Exception:
            effective = {}

        role = am.classify_clip_audio_role(data, file_data, effective)
        if role == am.ROLE_SILENT:
            continue

        windows = am.speech_windows_from_cues(data, effective)
        window_source = "cues" if windows else ""
        if windows:
            refined = am.refine_windows_with_energy(windows, data)
            if refined != windows:
                window_source = "cues+energy"
            windows = refined

        tl_start, tl_end = am.clip_timeline_extent(data)
        entries.append(
            {
                "clip": clip_obj,
                "id": str(clip_obj.id),
                "data": data,
                "file_data": file_data,
                "role": role,
                "layer": layer_num,
                "track_label": format_track_label_for_llm(layer_num, layers_raw),
                "title": str(data.get("title") or data.get("label") or "clip"),
                "start": tl_start,
                "end": tl_end,
                "windows": windows,
                "window_source": window_source,
                "level": am.current_static_level(data),
                "points": len(am.curve_points(data)),
                "analyzed": bool(effective.get("analyzed")),
                "fps": fps,
            }
        )

    entries.sort(key=lambda e: (e["start"], e["layer"]))
    return entries, layers_raw, fps


def _describe_audio_clip(entry) -> str:
    from classes import audio_mix as am

    level = entry["level"]
    if level is not None:
        level_part = f"level={level:.2f} ({am.gain_to_db(level):+.1f} dB)"
    else:
        level_part = f"level=automated({entry['points']} points)"
    return (
        f"  timeline_clip_id={entry['id']} audio_role={entry['role']} "
        f"track={entry['track_label']} title={entry['title']!r} "
        f"{entry['start']:.2f}s-{entry['end']:.2f}s {level_part} "
        f"indexed={'yes' if entry['analyzed'] else 'no'}"
    )


def _fmt_windows(windows, limit=8) -> str:
    shown = [f"{s:.2f}-{e:.2f}" for s, e in windows[:limit]]
    if len(windows) > limit:
        shown.append(f"... +{len(windows) - limit} more")
    return ", ".join(shown)


def _speech_overlaps(entries) -> list:
    """Pairs of speech clips that overlap in time — the known stacking bug."""
    speech = [e for e in entries if e["role"] == "speech"]
    clashes = []
    for i, a in enumerate(speech):
        for b in speech[i + 1:]:
            if min(a["end"], b["end"]) - max(a["start"], b["start"]) > 0.05:
                clashes.append((a, b))
    return clashes


def _locked_layer_error(layer_num, layers_raw, track_label):
    for L in layers_raw:
        try:
            if int(L.get("number") or 0) == int(layer_num) and bool(L.get("lock", False)):
                return f"Error: Track {track_label} is locked."
        except (TypeError, ValueError):
            continue
    return None


def _write_volume_points(clip_obj, points):
    """Partial save of a volume curve (keeps reader/ai_metadata intact).

    The caller sets app.updates.transaction_id so one undo reverts the pass.
    """
    clip_obj.data = {"volume": {"Points": list(points)}}
    clip_obj.save()


def _refresh_audio_ui(app, refreshed, tid):
    """Redraw waveforms for clips that already have cached audio data."""
    if refreshed:
        try:
            from classes.waveform import get_audio_data
            get_audio_data(refreshed, transaction_id=tid)
        except Exception as e:
            log.debug("audio mix waveform refresh skipped: %s", e)
    try:
        app.window.refreshFrameSignal.emit()
    except Exception:
        pass


def analyze_timeline_audio(track="", timeline_clip_id="", detail="summary", **_kw) -> str:
    """Report the audio role, level and speech windows of every timeline clip.

    Roles: speech (has transcript cues), music/sfx (audio-only, no cues),
    ambient (video, no cues), unknown (not indexed yet). Use this before mixing
    so you know which clips are beds and which carry the voice. detail='windows'
    also lists the detected speech ranges in timeline seconds.
    """
    try:
        app = _get_app()
        layers_raw = app.project.get("layers") or []
        layer_filter = None
        if str(track or "").strip():
            layer_filter, err = normalize_track_or_layer_arg(str(track).strip(), layers_raw)
            if err:
                return err

        entries, layers_raw, _fps = _collect_timeline_audio(app, layer_filter)
        wanted = str(timeline_clip_id or "").strip()
        if wanted:
            entries = [e for e in entries if e["id"] == wanted]
            if not entries:
                return f"Error: No timeline clip with id={wanted} (or it has no audio)."
        if not entries:
            return "No timeline clips with audio."

        show_windows = str(detail or "").strip().lower() == "windows"
        lines = [f"Timeline audio ({len(entries)} clip(s) with sound):"]
        for entry in entries:
            lines.append(_describe_audio_clip(entry))
            if show_windows and entry["windows"]:
                lines.append(
                    f"    speech windows ({entry['window_source']}, timeline s): "
                    f"{_fmt_windows(entry['windows'])}"
                )

        speech = [e for e in entries if e["role"] == "speech"]
        beds = [e for e in entries if e["role"] in ("music", "sfx")]
        unknown = [e for e in entries if e["role"] == "unknown"]
        lines.append(
            f"Summary: {len(speech)} speech, {len(beds)} music/sfx bed(s), "
            f"{len(unknown)} unknown."
        )
        if unknown:
            lines.append(
                "  unknown = source not indexed yet; index it or pass the clip id "
                "explicitly to mix it."
            )

        for a, b in _speech_overlaps(entries):
            lines.append(
                f"WARNING: speech clips {a['id']} and {b['id']} overlap in time "
                f"({max(a['start'], b['start']):.2f}s-{min(a['end'], b['end']):.2f}s). "
                "Two voices at once cannot be fixed by ducking — move or trim one."
            )

        for bed in beds:
            higher = [
                s for s in speech
                if s["layer"] < bed["layer"]
                and min(s["end"], bed["end"]) - max(s["start"], bed["start"]) > 0.05
            ]
            if higher:
                lines.append(
                    f"NOTE: bed {bed['id']} sits above speech on track {bed['track_label']}. "
                    "Duck its level instead of restacking tracks."
                )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def set_clip_volume(
    timeline_clip_id="",
    clip_query="",
    track="",
    occurrence="0",
    level_db="",
    level="",
    start_seconds="",
    end_seconds="",
    fade_ms="150",
    mode="replace",
    **_kw,
) -> str:
    """Set a timeline clip's audio level, over the whole clip or one time window.

    Give exactly one of level_db (decibels, negative = quieter) or level
    (0.0-1.3 linear). mode='replace' sets the level outright and replaces the
    clip's whole volume curve, fades included; mode='scale' multiplies the
    existing volume automation and keeps fades -- set levels first and add
    fades last. start_seconds/end_seconds are TIMELINE seconds; omit both to set
    a flat level for the whole clip. With no speech in the edit, music stays
    near full level.
    """
    try:
        from classes import audio_mix as am

        if not str(timeline_clip_id or "").strip() and not str(clip_query or "").strip():
            return "Error: set_clip_volume requires timeline_clip_id or clip_query."

        db = _audio_float(level_db)
        lin = _audio_float(level)
        if db is not None and lin is not None:
            return "Error: pass either level_db or level, not both."
        if db is None and lin is None:
            return "Error: set_clip_volume requires level_db or level."
        target = am.db_to_gain(db) if db is not None else lin

        resolved = _resolve_timeline_clip_for_tool(
            timeline_clip_id=timeline_clip_id,
            clip_query=clip_query,
            track=track,
            occurrence=occurrence,
        )
        if not resolved.ok or not resolved.clip:
            return resolved.error or "Error: Could not resolve timeline clip."

        clip_obj = resolved.clip
        clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        app = _get_app()
        layers_raw = app.project.get("layers") or []
        try:
            layer_num = int(clip_data.get("layer") or 0)
        except (TypeError, ValueError):
            layer_num = 0
        track_label = format_track_label_for_llm(layer_num, layers_raw)
        locked = _locked_layer_error(layer_num, layers_raw, track_label)
        if locked:
            return locked
        if not am.has_audio_stream(clip_data, _file_data_for_clip(clip_obj)):
            return f"Error: timeline clip {clip_obj.id} has no audio stream."

        fps = app.project.get("fps") or {"num": 30, "den": 1}
        before = am.current_static_level(clip_data)
        points = am.build_static_level_points(
            clip_data,
            fps,
            target,
            start_seconds=_audio_float(start_seconds),
            end_seconds=_audio_float(end_seconds),
            fade=max(0.0, (_audio_float(fade_ms, 150.0) or 0.0) / 1000.0),
            scale=str(mode or "").strip().lower() == "scale",
        )
        if not points:
            return (
                f"Error: the requested window does not overlap timeline clip "
                f"{clip_obj.id} ({am.clip_timeline_extent(clip_data)[0]:.2f}s-"
                f"{am.clip_timeline_extent(clip_data)[1]:.2f}s)."
            )

        clip_id = str(clip_obj.id)
        title = str(clip_data.get("title") or clip_data.get("label") or "clip")
        file_id = str(clip_data.get("file_id") or "")
        has_waveform = bool((clip_data.get("ui") or {}).get("audio_data"))

        def _do_set():
            # Join the outer execute_tool transaction when present; only mint
            # (and clear) an id when called without one (Phase 3 defect E).
            owned = False
            tid = app.updates.transaction_id
            if not tid:
                tid = _new_transaction_id()
                app.updates.transaction_id = tid
                owned = True
            try:
                _write_volume_points(clip_obj, points)
            finally:
                if owned:
                    app.updates.transaction_id = None
            _refresh_audio_ui(app, {file_id: [clip_id]} if (has_waveform and file_id) else {}, tid)

        if QThread is not None and QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do_set)
        else:
            _do_set()

        before_text = f"{before:.2f}" if before is not None else "automated"
        window_text = "whole clip"
        if _audio_float(start_seconds) is not None or _audio_float(end_seconds) is not None:
            tl_start, tl_end = am.clip_timeline_extent(clip_data)
            s = _audio_float(start_seconds, tl_start)
            e = _audio_float(end_seconds, tl_end)
            window_text = f"{s:.2f}s-{e:.2f}s (timeline)"
        return (
            f"Set volume on timeline_clip_id={clip_id} (track {track_label}, "
            f"{title!r}): {before_text} -> {target:.3f} "
            f"({am.gain_to_db(target):+.1f} dB) over {window_text}; "
            f"{len(points)} volume point(s) written. Track and position unchanged."
        )
    except Exception as e:
        return f"Error: {e}"


def _file_data_for_clip(clip_obj):
    f = _get_source_file_for_clip(clip_obj)
    return f.data if f is not None and isinstance(getattr(f, "data", None), dict) else None


def duck_under_speech(
    bed_clip_ids="",
    bed_query="",
    bed_track="",
    speech_clip_ids="auto",
    duck_db="auto",
    attack_ms="150",
    release_ms="400",
    pad_before_ms="200",
    pad_after_ms="300",
    boost_speech_db="0",
    dry_run="false",
    **_kw,
) -> str:
    """Duck music/SFX beds under speech with volume keyframes, restoring in gaps.

    Writes timeline volume automation only — no media is re-encoded and no clip
    changes track or position. With speech_clip_ids='auto' the speech clips are
    detected from indexed transcript cues; beds default to every music/sfx clip
    that overlaps speech. Use dry_run='true' to preview the envelope first.

    duck_db='auto' (the default) derives the attenuation per bed from its own
    measured level against the speech it overlaps, so the result depends on the
    material instead of always being the same envelope. Pass a number to force one.
    """
    try:
        from classes import audio_mix as am

        app = _get_app()
        layers_raw = app.project.get("layers") or []
        layer_filter = None
        if str(bed_track or "").strip():
            layer_filter, err = normalize_track_or_layer_arg(str(bed_track).strip(), layers_raw)
            if err:
                return err

        entries, layers_raw, fps = _collect_timeline_audio(app)
        if not entries:
            return "No timeline clips with audio — nothing to mix."
        by_id = {e["id"]: e for e in entries}

        # --- speech sources -------------------------------------------------
        explicit_speech = _audio_id_list(speech_clip_ids)
        if explicit_speech and explicit_speech != ["auto"]:
            speech = []
            missing = []
            for cid in explicit_speech:
                if cid in by_id:
                    speech.append(by_id[cid])
                else:
                    missing.append(cid)
            if missing:
                return f"Error: no timeline clip with audio for id(s): {', '.join(missing)}."
            # A declared speech clip with no cues falls back to VAD, then energy.
            for entry in speech:
                if not entry["windows"]:
                    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                    path = str(((data.get("reader") or {}) if isinstance(data.get("reader"), dict) else {}).get("path") or "")
                    windows, src = am.speech_windows_best(data, None, media_path=path)
                    if windows:
                        entry["windows"] = windows
                        entry["window_source"] = src
        else:
            speech = [e for e in entries if e["role"] == "speech" and e["windows"]]
            if not speech:
                for entry in entries:
                    if entry.get("windows"):
                        continue
                    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                    path = str(((data.get("reader") or {}) if isinstance(data.get("reader"), dict) else {}).get("path") or "")
                    if not path:
                        continue
                    windows, src = am.speech_windows_best(data, None, media_path=path)
                    if windows and src == "local_vad":
                        entry["windows"] = windows
                        entry["window_source"] = src
                        entry["role"] = "speech"
                        speech.append(entry)

        if not speech:
            unknown = [e["id"] for e in entries if e["role"] == "unknown"]
            hint = (
                f" {len(unknown)} clip(s) are not indexed yet ({', '.join(unknown[:5])}); "
                "index them or pass speech_clip_ids explicitly."
                if unknown else ""
            )
            return (
                "No speech detected on the timeline, so there is nothing to duck under."
                + hint
                + " Use set_clip_volume_tool for a static level change."
            )

        speech_windows = am.merge_windows(
            [w for entry in speech for w in entry["windows"]]
        )
        if not speech_windows:
            return (
                "Could not resolve any speech time windows for "
                + ", ".join(e["id"] for e in speech)
                + " (no transcript cues and no usable waveform). Index the source, "
                "or use set_clip_volume_tool with an explicit time range."
            )

        # --- beds -----------------------------------------------------------
        speech_ids = {e["id"] for e in speech}
        explicit_beds = _audio_id_list(bed_clip_ids)
        warnings = []
        if explicit_beds:
            beds = []
            for cid in explicit_beds:
                if cid not in by_id:
                    return f"Error: no timeline clip with audio for id={cid}."
                beds.append(by_id[cid])
            for bed in beds:
                if bed["role"] == "speech":
                    warnings.append(
                        f"WARNING: {bed['id']} carries speech; ducking it will "
                        "attenuate its own dialogue too."
                    )
        elif str(bed_query or "").strip():
            resolved = _resolve_timeline_clip_for_tool(
                clip_query=bed_query, track=bed_track
            )
            if not resolved.ok or not resolved.clip:
                return resolved.error or "Error: Could not resolve the bed clip."
            bed_id = str(resolved.clip.id)
            if bed_id not in by_id:
                return f"Error: timeline clip {bed_id} has no audio stream."
            beds = [by_id[bed_id]]
        else:
            beds = [
                e for e in entries
                if e["role"] in am.BED_ROLES
                and e["id"] not in speech_ids
                and (layer_filter is None or e["layer"] == layer_filter)
                and am.clamp_windows(speech_windows, e["start"], e["end"])
            ]

        if not beds:
            return (
                "No music/SFX bed overlaps the detected speech, so no ducking was "
                "needed. Speech windows (timeline s): "
                f"{_fmt_windows(speech_windows)}"
            )

        # --- build envelopes ------------------------------------------------
        duck_arg = str(duck_db or "").strip().lower()
        auto_duck = duck_arg in ("", "auto")
        fixed_gain = None if auto_duck else am.db_to_gain(
            _audio_float(duck_db, am.DEFAULT_DUCK_DB)
        )
        speech_levels = [e["level"] for e in speech if e["level"] is not None]
        speech_level = min(speech_levels) if speech_levels else None
        attack = max(0.0, (_audio_float(attack_ms, 150.0) or 0.0) / 1000.0)
        release = max(0.0, (_audio_float(release_ms, 400.0) or 0.0) / 1000.0)
        pad_before = max(0.0, (_audio_float(pad_before_ms, 200.0) or 0.0) / 1000.0)
        pad_after = max(0.0, (_audio_float(pad_after_ms, 300.0) or 0.0) / 1000.0)

        planned = []
        for bed in beds:
            locked = _locked_layer_error(bed["layer"], layers_raw, bed["track_label"])
            if locked:
                return locked
            windows = am.clamp_windows(speech_windows, bed["start"], bed["end"])
            if not windows:
                continue
            if fixed_gain is not None:
                duck_gain = fixed_gain
            else:
                duck_gain = am.db_to_gain(am.auto_duck_db(bed["level"], speech_level))
            points = am.build_duck_points(
                bed["data"], fps, windows, duck_gain=duck_gain,
                attack=attack, release=release,
                pad_before=pad_before, pad_after=pad_after,
            )
            if not points:
                continue
            held = am.ducked_windows(
                bed["data"], windows, attack=attack, release=release,
                pad_before=pad_before, pad_after=pad_after,
            )
            planned.append((bed, points, held, duck_gain))

        boost_db = _audio_float(boost_speech_db, 0.0) or 0.0
        boosted = []
        if abs(boost_db) > 1e-6:
            boost_level = am.db_to_gain(boost_db)
            for entry in speech:
                if _locked_layer_error(entry["layer"], layers_raw, entry["track_label"]):
                    continue
                pts = am.build_static_level_points(
                    entry["data"], fps, min(am.MAX_LEVEL, boost_level)
                )
                if pts:
                    boosted.append((entry, pts))

        if not planned and not boosted:
            return (
                "The music/SFX beds do not overlap the detected speech windows, so "
                "no volume automation was written."
            )

        # --- report ---------------------------------------------------------
        header = (
            f"{'Would duck' if _audio_bool(dry_run) else 'Ducked'} {len(planned)} bed clip(s) "
            f"under {len(speech)} speech clip(s). "
            + (
                "duck=auto (per bed, from measured levels)"
                if fixed_gain is None
                else f"duck={am.gain_to_db(fixed_gain):+.1f} dB (gain {fixed_gain:.3f})"
            )
        )
        lines = [header, ""]
        for bed, points, held, duck_gain in planned:
            base = bed["level"]
            base_text = f"{base:.2f}" if base is not None else "automated"
            lines.append(
                f"bed timeline_clip_id={bed['id']} track={bed['track_label']} "
                f"title={bed['title']!r}"
            )
            if base is not None:
                lines.append(
                    f"  role={bed['role']} base={base_text} -> {base * duck_gain:.2f} "
                    f"({am.gain_to_db(duck_gain):+.1f} dB)"
                )
            else:
                lines.append(
                    f"  role={bed['role']} base=automated "
                    f"(existing curve scaled by {duck_gain:.3f} under speech)"
                )
            lines.append(
                f"  {len(held)} duck window(s), {len(points)} volume points"
            )
            lines.append(f"  ducked (timeline s): {_fmt_windows(held)}")
            gaps = am.invert_windows(held, bed["start"], bed["end"])
            if gaps:
                lines.append(f"  restored (timeline s): {_fmt_windows(gaps)}")
        if boosted:
            lines.append(
                f"speech boosted by {boost_db:+.1f} dB on: "
                + ", ".join(e["id"] for e, _ in boosted)
            )
        lines.append(
            "speech sources: "
            + ", ".join(
                f"{e['id']} ({e['window_source'] or 'declared'}, {len(e['windows'])} windows)"
                for e in speech
            )
        )
        skipped = [e for e in entries if e["role"] == "unknown"]
        if skipped:
            lines.append(
                "skipped: "
                + ", ".join(f"{e['id']} role=unknown (not indexed)" for e in skipped[:5])
            )
        for warning in warnings:
            lines.append(warning)
        for a, b in _speech_overlaps(entries):
            lines.append(
                f"WARNING: speech clips {a['id']} and {b['id']} overlap; ducking "
                "cannot separate two voices — move or trim one."
            )

        if _audio_bool(dry_run):
            lines.append("")
            lines.append("dry_run=true — nothing was written.")
            return "\n".join(lines)

        # --- write ----------------------------------------------------------
        refreshed: dict = {}
        for bed, points, _held, _gain in planned:
            if (bed["data"].get("ui") or {}).get("audio_data"):
                fid = str(bed["data"].get("file_id") or "")
                if fid:
                    refreshed.setdefault(fid, []).append(bed["id"])
        for entry, _pts in boosted:
            if (entry["data"].get("ui") or {}).get("audio_data"):
                fid = str(entry["data"].get("file_id") or "")
                if fid:
                    refreshed.setdefault(fid, []).append(entry["id"])

        def _do_duck():
            # Join the outer execute_tool transaction when present (Phase 3 defect E).
            owned = False
            tid = app.updates.transaction_id
            if not tid:
                tid = _new_transaction_id()
                app.updates.transaction_id = tid
                owned = True
            try:
                for bed_entry, pts, _w, _g in planned:
                    _write_volume_points(bed_entry["clip"], pts)
                for speech_entry, pts in boosted:
                    _write_volume_points(speech_entry["clip"], pts)
            finally:
                if owned:
                    app.updates.transaction_id = None
            _refresh_audio_ui(app, refreshed, tid)

        if QThread is not None and QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do_duck)
        else:
            _do_duck()

        lines.append("")
        lines.append(
            "No clip changed track, position or trim. One undo reverts the whole mix."
        )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _parse_color_patch_kwargs(kwargs: dict) -> dict:
    """Build a merge patch from tool kwargs (scalars, lut, wheels, curves, color)."""
    from classes import color_agent as ca

    patch = {}
    color_raw = kwargs.get("color")
    if color_raw not in (None, ""):
        if isinstance(color_raw, str):
            try:
                color_raw = json.loads(color_raw)
            except json.JSONDecodeError as exc:
                raise ValueError("color must be a JSON object") from exc
        if not isinstance(color_raw, dict):
            raise ValueError("color must be an object")
        patch["color"] = color_raw

    for key in (
        list(ca.SCALAR_KEYS)
        + list(ca.SCALAR_ALIASES.keys())
        + list(ca.DELTA_KEYS.keys())
    ):
        if key in kwargs and kwargs[key] not in (None, ""):
            patch[key] = kwargs[key]

    if kwargs.get("lut_path") not in (None, ""):
        patch["lut_path"] = kwargs["lut_path"]
    lut_raw = kwargs.get("lut")
    if lut_raw not in (None, ""):
        if isinstance(lut_raw, str):
            try:
                lut_raw = json.loads(lut_raw)
            except json.JSONDecodeError as exc:
                raise ValueError("lut must be a JSON object") from exc
        patch["lut"] = lut_raw

    wheels_raw = kwargs.get("wheels")
    if wheels_raw not in (None, ""):
        if isinstance(wheels_raw, str):
            try:
                wheels_raw = json.loads(wheels_raw)
            except json.JSONDecodeError as exc:
                raise ValueError("wheels must be a JSON object") from exc
        patch["wheels"] = wheels_raw

    curve_keys = list(ca.CURVE_KEYS) + list(ca.CURVE_ALIASES.keys())
    for key in curve_keys:
        if key in kwargs and kwargs[key] not in (None, ""):
            raw = kwargs[key]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{key} must be JSON points") from exc
            patch[key] = raw
    return patch


def _write_clip_effects(clip_obj, effects):
    clip_obj.data = {"effects": list(effects)}
    clip_obj.save()


def _selected_timeline_clip_ids():
    """Clip ids currently selected in the editor (empty if none / unavailable).

    Raises on read failure so callers do not silently retarget to the playhead.
    """
    window = _get_app().window
    selected = list(getattr(window, "selected_clips", None) or [])
    return [str(cid) for cid in selected if cid]


def _clip_is_audio_only(clip) -> bool:
    """Music / SFX / voice-over clips: a colour grade has nothing to change."""
    data = clip.data if isinstance(getattr(clip, "data", None), dict) else {}
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
    return is_audio_only_media(reader)


def _all_timeline_clip_ids():
    """Every timeline clip id that has a picture (stable order from project).

    Raises on Clip.query failure so all-clips tools do not pretend the timeline
    is empty.
    """
    from classes.query import Clip

    out = []
    for clip in Clip.filter() or []:
        cid = str(getattr(clip, "id", "") or "").strip()
        if cid and cid not in out and not _clip_is_audio_only(clip):
            out.append(cid)
    return out


def _playhead_timeline_clip_ids():
    """Picture clip ids intersecting the playhead (may be several on stacked tracks)."""
    try:
        from classes.query import Clip

        app = _get_app()
        fps = app.project.get("fps") or {"num": 30, "den": 1}
        try:
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            fps_float = 30.0
        frame = int(getattr(app.window.preview_thread, "current_frame", 1) or 1)
        playhead_sec = max(0.0, (frame - 1) / max(fps_float, 1e-6))
        out = []
        for clip in Clip.filter(intersect=playhead_sec) or []:
            cid = str(getattr(clip, "id", "") or "").strip()
            if cid and cid not in out and not _clip_is_audio_only(clip):
                out.append(cid)
        return out
    except Exception:
        return []


def _resolve_color_target_ids(
    clip_ids="",
    clipIds="",
    timeline_clip_id="",
    clipId="",
    all_clips=False,
):
    """Resolve which timeline clips a colour tool should mutate.

    Order: explicit ids → all/both/* token or all_clips → selection → playhead.
    """
    from classes import color_agent as ca

    raw_bits = [
        str(clip_ids or "").strip(),
        str(clipIds or "").strip(),
        str(timeline_clip_id or "").strip(),
        str(clipId or "").strip(),
    ]
    joined = " ".join(b for b in raw_bits if b).strip().lower()
    want_all = bool(all_clips) or joined in ("all", "*", "both", "every", "everything")
    if not want_all:
        # JSON list / comma list that is only the all-token
        try:
            parsed = ca.parse_clip_ids(
                clip_ids=clip_ids,
                clipIds=clipIds,
                timeline_clip_id=timeline_clip_id,
                clipId=clipId,
            )
        except Exception:
            parsed = []
        if parsed and all(str(p).strip().lower() in ("all", "*", "both") for p in parsed):
            want_all = True
        elif parsed:
            return parsed

    if want_all:
        return _all_timeline_clip_ids()

    try:
        selected = _selected_timeline_clip_ids()
    except Exception as exc:
        raise RuntimeError(f"Could not read selected clips: {exc}") from exc
    if selected:
        return selected
    under_playhead = _playhead_timeline_clip_ids()
    if under_playhead:
        return under_playhead
    return []


def _color_target_error(action: str) -> str:
    available = _all_timeline_clip_ids()
    avail = ", ".join(available[:12]) if available else "(none)"
    if len(available) > 12:
        avail += ", …"
    return (
        f"Error: {action} needs a target clip. Select clip(s), park the playhead "
        f"on a clip, pass timeline_clip_id / clipIds, or clipIds=\"all\" for every "
        f"timeline clip. On timeline now: {avail}"
    )


def _filter_unlocked_color_clip_ids(app, ids, warnings):
    """Drop locked-track clips before opening a colour undo group."""
    from classes.query import Clip

    kept = []
    for cid in ids:
        clip_obj = Clip.get(id=cid)
        if not clip_obj:
            warnings.append(f"clip {cid} not found")
            continue
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        try:
            layer = int(data.get("layer") or 0)
        except (TypeError, ValueError):
            layer = 0
        if _locked_track_error(app, layer):
            warnings.append(f"clip {cid}: track locked — skipped")
            continue
        kept.append(cid)
    return kept


def _resolve_lut_file(raw) -> str:
    """Absolute path of an existing .cube for a LUT id, bundled path or file.

    Raises ValueError before any undo group opens: libopenshot only logs a LUT
    it cannot open, so a relative or mistyped path "applied" a grade that did
    nothing. Touches the filesystem, so callers run off the GUI thread.
    """
    from classes import color_agent as ca
    from classes import info

    text = str(raw or "").strip()
    look = ca.resolve_look_id(text)
    if look.get("kind") != "lut":
        raise ValueError(f"'{text}' is a {look.get('kind')} look, not a LUT; use apply_look_tool")
    path = ca.resolve_lut_filesystem_path(
        look["lut_path"],
        colors_path=getattr(info, "COLORS_PATH", ""),
        user_colors_path=getattr(info, "USER_COLORS_PATH", ""),
    )
    if not os.path.isfile(path):
        raise ValueError(f"LUT file not found: {look['lut_path']}. Call list_looks_tool for LUT ids.")
    return path


def apply_color(
    clipIds="",
    clip_ids="",
    timeline_clip_id="",
    clipId="",
    reset="false",
    color="",
    exposure="",
    contrast="",
    saturation="",
    vibrance="",
    temperature="",
    tint="",
    temperature_delta="",
    tint_delta="",
    exposure_delta="",
    vibrance_delta="",
    highlights="",
    shadows="",
    mix="",
    lut_path="",
    lut_intensity="",
    lut="",
    wheels="",
    curve_all="",
    curve_red="",
    curve_green="",
    curve_blue="",
    masterCurve="",
    redCurve="",
    greenCurve="",
    blueCurve="",
    all_clips="",
    **kwargs,
) -> str:
    """Merge ColorGrade knobs/curves/wheels/LUT onto timeline clip(s); one undo.

    Pass clipIds (JSON list or comma-separated) and only the fields to change.
    Unset fields are kept (merge). reset=true removes ColorGrade. color= pastes
    a full grade object. temperature_delta / tint_delta / exposure_delta /
    vibrance_delta nudge from the current grade (clamped to [-1, 1]). Prefer
    deltas for \"a bit sunnier/warmer\". Validate-before-undo: bad args leave
    history untouched. If no clip ids are passed: selected clips, else clip(s)
    under the playhead. clipIds=\"all\" / all_clips=true grades every timeline clip.
    """
    try:
        from classes import color_agent as ca

        all_flag = _tool_flag(kwargs.get("allClips"), all_clips, kwargs.get("all_clips"), default=False)
        ids = _resolve_color_target_ids(
            clip_ids=clip_ids,
            clipIds=clipIds,
            timeline_clip_id=timeline_clip_id,
            clipId=clipId,
            all_clips=all_flag,
        )
        if not ids:
            return _color_target_error("apply_color")

        reset_flag = str(reset or "").strip().lower() in ("1", "true", "yes", "on")
        merged_kwargs = {
            "color": color,
            "exposure": exposure if exposure != "" else kwargs.get("exposure", ""),
            "contrast": contrast if contrast != "" else kwargs.get("contrast", ""),
            "saturation": saturation if saturation != "" else kwargs.get("saturation", ""),
            "vibrance": vibrance if vibrance != "" else kwargs.get("vibrance", ""),
            "temperature": temperature if temperature != "" else kwargs.get("temperature", ""),
            "tint": tint if tint != "" else kwargs.get("tint", ""),
            "temperature_delta": (
                temperature_delta if temperature_delta != ""
                else kwargs.get("temperature_delta", "")
            ),
            "tint_delta": tint_delta if tint_delta != "" else kwargs.get("tint_delta", ""),
            "exposure_delta": (
                exposure_delta if exposure_delta != ""
                else kwargs.get("exposure_delta", "")
            ),
            "vibrance_delta": (
                vibrance_delta if vibrance_delta != ""
                else kwargs.get("vibrance_delta", "")
            ),
            "highlights": highlights if highlights != "" else kwargs.get("highlights", ""),
            "shadows": shadows if shadows != "" else kwargs.get("shadows", ""),
            "mix": mix if mix != "" else kwargs.get("mix", ""),
            "lut_path": lut_path if lut_path != "" else kwargs.get("lut_path", ""),
            "lut_intensity": lut_intensity if lut_intensity != "" else kwargs.get("lut_intensity", ""),
            "lut": lut if lut != "" else kwargs.get("lut", ""),
            "wheels": wheels if wheels != "" else kwargs.get("wheels", ""),
            "curve_all": curve_all if curve_all != "" else kwargs.get("curve_all", ""),
            "curve_red": curve_red if curve_red != "" else kwargs.get("curve_red", ""),
            "curve_green": curve_green if curve_green != "" else kwargs.get("curve_green", ""),
            "curve_blue": curve_blue if curve_blue != "" else kwargs.get("curve_blue", ""),
            "masterCurve": masterCurve if masterCurve != "" else kwargs.get("masterCurve", ""),
            "redCurve": redCurve if redCurve != "" else kwargs.get("redCurve", ""),
            "greenCurve": greenCurve if greenCurve != "" else kwargs.get("greenCurve", ""),
            "blueCurve": blueCurve if blueCurve != "" else kwargs.get("blueCurve", ""),
            "temp": kwargs.get("temp", ""),
            "sat": kwargs.get("sat", ""),
        }
        # Drop empty string markers so merge only sees provided fields.
        merged_kwargs = {k: v for k, v in merged_kwargs.items() if v not in (None, "")}

        patch = {}
        if not reset_flag:
            patch = _parse_color_patch_kwargs(merged_kwargs)
            if not patch:
                return (
                    "Error: apply_color needs reset=true or at least one colour "
                    "field (exposure, wheels, lut, color, …)."
                )
            ca.validate_color_patch(patch)
            if patch.get("lut_path"):
                patch["lut_path"] = _resolve_lut_file(patch["lut_path"])
            lut = patch.get("lut")
            if isinstance(lut, dict) and lut.get("path"):
                patch["lut"] = dict(lut, path=_resolve_lut_file(lut["path"]))

        app = _get_app()
        receipts = []
        warnings = []
        error_box = [None]

        def _filter_ids():
            return _filter_unlocked_color_clip_ids(app, ids, warnings)

        if QThread is not None and QThread.currentThread() is not app.thread():
            ids = _run_on_main_thread(_filter_ids)
        else:
            ids = _filter_ids()
        if not ids:
            if warnings:
                return "Error: " + "; ".join(warnings)
            return _color_target_error("apply_color")

        def _do_apply():
            from classes.query import Clip as _Clip

            changed = 0
            with _transaction(app):
                for cid in ids:
                    clip_obj = _Clip.get(id=cid)
                    if not clip_obj:
                        warnings.append(f"clip {cid} not found")
                        continue
                    data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
                    effects = list(data.get("effects") or [])
                    if reset_flag:
                        new_effects = [e for e in effects if not ca.is_color_grade_effect(e)]
                        if len(new_effects) == len(effects):
                            receipts.append({
                                "timeline_clip_id": cid,
                                "status": "noop",
                                "color": {"present": False},
                            })
                            continue
                        _write_clip_effects(clip_obj, new_effects)
                        changed += 1
                        receipts.append({
                            "timeline_clip_id": cid,
                            "status": "reset",
                            "color": {"present": False},
                        })
                        continue

                    existing = ca.find_color_grade(effects)
                    if existing is None:
                        effect = ca.create_color_grade_effect_json(
                            app.project.generate_id
                        )
                        effects.append(effect)
                        existing = effect
                    merged = ca.merge_color_grade(existing, patch)
                    # Replace first ColorGrade; drop duplicates.
                    replaced = False
                    new_effects = []
                    for effect in effects:
                        if ca.is_color_grade_effect(effect):
                            if not replaced:
                                new_effects.append(merged)
                                replaced = True
                        else:
                            new_effects.append(effect)
                    if not replaced:
                        new_effects.append(merged)
                    _write_clip_effects(clip_obj, new_effects)
                    changed += 1
                    receipts.append({
                        "timeline_clip_id": cid,
                        "status": "applied",
                        "effect_id": merged.get("id"),
                        "color": ca.summarize_color_grade(merged),
                    })
                if changed == 0 and not receipts:
                    error_box[0] = "Error: no matching timeline clips."
            try:
                app.window.refreshFrameSignal.emit()
            except Exception:
                pass

        if QThread is not None and QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do_apply)
        else:
            _do_apply()

        if error_box[0]:
            return error_box[0]
        if not receipts:
            return "Error: no clips were updated."
        result = {
            "ok": True,
            "clips": receipts,
            "warnings": warnings,
            "undo": "one step",
        }
        # Evidence for vision closed-loop: solo AFTER of first graded clip.
        try:
            from classes.query import Clip as _Clip
            first_id = receipts[0].get("timeline_clip_id") or receipts[0].get("id")
            clip_obj = None
            if QThread is not None and QThread.currentThread() is not app.thread():
                clip_obj = _run_on_main_thread(lambda: _Clip.get(id=first_id))
            else:
                clip_obj = _Clip.get(id=first_id)
            data = clip_obj.data if clip_obj and isinstance(clip_obj.data, dict) else {}
            fps = app.project.get("fps") or {"num": 30, "den": 1}
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
            after = _render_clip_isolated(data, 1, fps_float, with_jpeg=True)
            after_scopes = after.get("scopes") or {}
            result["after_preview_jpeg"] = after.get("preview_jpeg") or ""
            result["after_scopes"] = after_scopes
            outdoor = ca.is_outdoor_bright_profile(
                ca.build_look_profile(after_scopes) if after_scopes.get("present") else {}
            )
            # Nuke risk vs a neutral goal is weak; flag clipped highlights alone.
            hi = after_scopes.get("clipped_highlights")
            try:
                hi_f = float(hi) if hi is not None else 0.0
            except (TypeError, ValueError):
                hi_f = 0.0
            result["nuke_risk"] = bool(hi_f >= 0.12 or (outdoor and hi_f >= 0.08))
            result["outdoor_bright"] = outdoor
        except Exception as exc:
            result["after_preview_jpeg"] = ""
            result["nuke_risk"] = False
            result["warnings"] = list(warnings) + [f"after preview failed: {exc}"]
        return json.dumps(result)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def _framescope_from_frame(frame) -> dict:
    """Run FrameScope on an openshot Frame object."""
    import openshot
    from classes import color_agent as ca

    scope = openshot.FrameScope()
    try:
        scope.SetWaveformColumns(128)
    except Exception:
        pass
    try:
        scope.SetVectorscopeSize(96)
    except Exception:
        pass
    scope.SetFrame(frame)
    if not scope.HasVideo():
        return {"present": False}
    video = {
        "present": True,
        "histogram": {
            "luma": list(scope.GetVideoHistogramLuma()),
            "red": list(scope.GetVideoHistogramRed()),
            "green": list(scope.GetVideoHistogramGreen()),
            "blue": list(scope.GetVideoHistogramBlue()),
        },
        "summary": {
            "avg_luma": scope.GetVideoAverageLuma(),
            "clipped_shadows": scope.GetVideoClippedShadows(),
            "clipped_highlights": scope.GetVideoClippedHighlights(),
        },
    }
    return ca.scope_from_raw_video(video)


def _measure_frame_scopes(frame_number: int) -> dict:
    """Run FrameScope on a composited timeline frame (safe off the GUI thread)."""
    app = _get_app()
    timeline = getattr(getattr(app.window, "timeline_sync", None), "timeline", None)
    if not timeline:
        return {"present": False, "error": "no timeline"}
    try:
        frame = timeline.GetFrame(int(frame_number))
        return _framescope_from_frame(frame)
    except Exception as exc:
        return {"present": False, "error": str(exc)}


def _clip_local_frame_number(clip_data: dict, timeline_frame: int, fps_float: float) -> int:
    """Map a timeline frame into a 1-based frame on a solo timeline (clip at position 0)."""
    try:
        pos = float(clip_data.get("position") or 0)
        start = float(clip_data.get("start") or 0)
        end = float(clip_data.get("end") or (start + 1.0 / max(fps_float, 1e-6)))
        duration = max(1.0 / max(fps_float, 1e-6), end - start)
        timeline_sec = max(0.0, (int(timeline_frame) - 1) / max(fps_float, 1e-6))
        # Time into the clip's trimmed window.
        local_sec = timeline_sec - pos
        if local_sec < 0 or local_sec > duration:
            local_sec = duration * 0.5
        return max(1, int(local_sec * fps_float) + 1)
    except (TypeError, ValueError):
        return 1


def _frame_to_jpeg_b64(frame, long_edge: int = 480, quality: int = 72) -> str:
    """Encode an openshot Frame as a small JPEG base64 string (empty on failure)."""
    import base64

    if frame is None:
        return ""
    path = ""
    try:
        try:
            w = max(1, int(frame.GetWidth()))
            h = max(1, int(frame.GetHeight()))
        except Exception:
            w, h = long_edge, long_edge
        scale = min(1.0, float(long_edge) / float(max(w, h)))
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        frame.Save(path, float(scale), "JPG", int(quality))
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except Exception as exc:
        log.debug("_frame_to_jpeg_b64 failed: %s", exc)
        return ""
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _solo_render_size(width: int, height: int, long_edge: int = 640) -> tuple[int, int]:
    """Small render canvas with the project's aspect ratio.

    Clamping each side on its own (640 x 360) turned a portrait project into a
    near-square canvas, pillarboxing the clip; the black bars then dragged the
    scopes (avg luma, histograms) and showed up in the preview the model reads.
    """
    width, height = max(1, int(width)), max(1, int(height))
    scale = min(1.0, float(long_edge) / float(max(width, height)))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _render_clip_isolated(
    clip_data: dict,
    timeline_frame: int,
    fps_float: float,
    *,
    with_jpeg: bool = True,
) -> dict:
    """Solo-render one clip (effects on) → scopes + optional preview JPEG.

    Track/layer count does not matter: overlapping clips on other tracks are
    not in this temporary timeline, so match/inspect cannot false-zero.
    """
    import openshot
    from classes.query import File

    if not isinstance(clip_data, dict):
        return {"scopes": {"present": False, "error": "invalid clip"}, "preview_jpeg": ""}

    app = _get_app()
    project = app.project
    try:
        width = int(project.get("width") or 1280)
        height = int(project.get("height") or 720)
        sample_rate = int(project.get("sample_rate") or 48000)
        channels = int(project.get("channels") or 2)
        channel_layout = int(project.get("channel_layout") or 3)
        fps = project.get("fps") or {"num": 30, "den": 1}
        fps_num = int(fps.get("num", 30) or 30)
        fps_den = int(fps.get("den", 1) or 1)
    except (TypeError, ValueError):
        width, height, sample_rate, channels, channel_layout = 1280, 720, 48000, 2, 3
        fps_num, fps_den = 30, 1

    width, height = _solo_render_size(width, height)

    solo = copy.deepcopy(clip_data)
    solo["position"] = 0.0
    solo["layer"] = 1000000
    try:
        reader = solo.get("reader") if isinstance(solo.get("reader"), dict) else {}
        path = str(reader.get("path") or "").strip()
        if not path or not os.path.exists(path):
            fid = str(solo.get("file_id") or reader.get("id") or "").strip()
            if fid:
                fobj = File.get(id=fid)
                if fobj and hasattr(fobj, "absolute_path"):
                    ap = fobj.absolute_path()
                    if ap:
                        if not isinstance(solo.get("reader"), dict):
                            solo["reader"] = {}
                        solo["reader"]["path"] = ap
    except Exception:
        pass

    local_frame = _clip_local_frame_number(clip_data, timeline_frame, fps_float)
    temp = None
    try:
        temp = openshot.Timeline(
            width,
            height,
            openshot.Fraction(fps_num, fps_den),
            sample_rate,
            channels,
            channel_layout,
        )
        try:
            temp.SetMaxSize(int(width), int(height))
        except Exception:
            pass
        clip_os = openshot.Clip()
        clip_os.SetJson(json.dumps(solo))
        temp.AddClip(clip_os)
        temp.Open()
        frame = temp.GetFrame(int(local_frame))
        scopes = _framescope_from_frame(frame)
        scopes["isolated"] = True
        scopes["local_frame"] = int(local_frame)
        jpeg = _frame_to_jpeg_b64(frame) if with_jpeg else ""
        return {"scopes": scopes, "preview_jpeg": jpeg, "local_frame": int(local_frame)}
    except Exception as exc:
        return {
            "scopes": {
                "present": False,
                "isolated": False,
                "error": f"isolated render failed: {exc}",
            },
            "preview_jpeg": "",
            "local_frame": 0,
        }
    finally:
        if temp is not None:
            try:
                temp.Close()
            except Exception:
                pass


def _measure_clip_isolated_scopes(clip_data: dict, timeline_frame: int, fps_float: float) -> dict:
    """Back-compat: scopes only from an isolated clip render."""
    return _render_clip_isolated(
        clip_data, timeline_frame, fps_float, with_jpeg=False
    ).get("scopes") or {"present": False}


def _clip_duration_seconds(clip_data: dict) -> float:
    try:
        start = float(clip_data.get("start") or 0)
        end = float(clip_data.get("end") or 0)
        if end > start:
            return end - start
    except (TypeError, ValueError):
        pass
    try:
        reader = clip_data.get("reader") if isinstance(clip_data.get("reader"), dict) else {}
        dur = float(reader.get("duration") or 0)
        if dur > 0:
            return dur
    except (TypeError, ValueError):
        pass
    return 1.0


def _sample_local_frames(duration_s: float, fps_float: float, sample_count: int) -> list:
    """1-based local frame numbers evenly spaced across [0, duration)."""
    from classes import color_agent as ca

    n = max(1, int(sample_count or ca.sample_count_for_duration(duration_s)))
    fps = max(float(fps_float or 30.0), 1e-6)
    total_frames = max(1, int(round(max(duration_s, 1.0 / fps) * fps)))
    if n == 1:
        return [max(1, total_frames // 2)]
    frames = []
    for i in range(n):
        # Even spacing including near start and end.
        t = i / max(n - 1, 1)
        fr = 1 + int(round(t * (total_frames - 1)))
        frames.append(max(1, min(total_frames, fr)))
    # de-dupe preserving order
    out = []
    seen = set()
    for fr in frames:
        if fr not in seen:
            seen.add(fr)
            out.append(fr)
    return out


def _profile_clip_dense(
    clip_data: dict,
    fps_float: float,
    *,
    with_jpegs: bool = True,
    max_samples: int | None = None,
) -> dict:
    """Dense full-frame LookProfile across a timeline clip's duration."""
    from classes import color_agent as ca

    duration = _clip_duration_seconds(clip_data)
    n = ca.sample_count_for_duration(duration)
    if max_samples is not None:
        n = max(1, min(int(max_samples), n))
    local_frames = _sample_local_frames(duration, fps_float, n)
    scopes = []
    jpegs = []
    # Solo timeline uses position=0; keep original clip_data intact for callers.
    solo_data = dict(clip_data, position=0.0)
    for i, local_frame in enumerate(local_frames):
        want_jpeg = with_jpegs and (
            i == 0 or i == len(local_frames) // 2 or i == len(local_frames) - 1
        )
        rendered = _render_clip_isolated(
            solo_data, int(local_frame), fps_float, with_jpeg=want_jpeg
        )
        sc = rendered.get("scopes") or {"present": False}
        if sc.get("present"):
            scopes.append(sc)
        if want_jpeg and rendered.get("preview_jpeg"):
            jpegs.append(rendered["preview_jpeg"])
    profile = ca.build_look_profile(scopes)
    return {
        "profile": profile,
        "scopes_samples": len(scopes),
        "sample_frames": local_frames,
        "preview_jpegs": jpegs,
        "preview_jpeg": jpegs[len(jpegs) // 2] if jpegs else (jpegs[0] if jpegs else ""),
        "duration_s": duration,
    }


def _profile_media_path(
    path: str,
    *,
    with_jpegs: bool = True,
    max_samples: int | None = None,
) -> dict:
    """Dense LookProfile for a media-bin file or still image path (off-main)."""
    import openshot
    from classes import color_agent as ca

    path = str(path or "").strip()
    if not path or not os.path.exists(path):
        return {
            "profile": {"present": False, "error": f"file not found: {path}"},
            "scopes_samples": 0,
            "preview_jpegs": [],
            "preview_jpeg": "",
            "duration_s": 0.0,
        }

    app = _get_app()
    project = app.project
    try:
        width, height = _solo_render_size(
            int(project.get("width") or 1280), int(project.get("height") or 720)
        )
        sample_rate = int(project.get("sample_rate") or 48000)
        channels = int(project.get("channels") or 2)
        channel_layout = int(project.get("channel_layout") or 3)
        fps = project.get("fps") or {"num": 30, "den": 1}
        fps_num = int(fps.get("num", 30) or 30)
        fps_den = int(fps.get("den", 1) or 1)
        fps_float = float(fps_num) / float(fps_den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        width, height, sample_rate, channels, channel_layout = 640, 360, 48000, 2, 3
        fps_num, fps_den, fps_float = 30, 1, 30.0

    # Duration via ffprobe when available; stills → 0.
    duration = 0.0
    try:
        duration = float(_ffprobe_video_duration(path) or 0.0)
    except Exception:
        duration = 0.0
    still_ext = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
    if os.path.splitext(path)[1].lower() in still_ext:
        duration = 0.0

    n = 1 if duration <= 0.05 else ca.sample_count_for_duration(duration)
    if max_samples is not None:
        n = max(1, min(int(max_samples), n))
    local_frames = _sample_local_frames(max(duration, 1.0 / fps_float), fps_float, n)

    temp = None
    scopes = []
    jpegs = []
    try:
        temp = openshot.Timeline(
            width, height,
            openshot.Fraction(fps_num, fps_den),
            sample_rate, channels, channel_layout,
        )
        try:
            temp.SetMaxSize(int(width), int(height))
        except Exception:
            pass
        clip_os = openshot.Clip(path)
        # Place at 0 for simple local-frame indexing.
        try:
            clip_os.Position(0.0)
        except Exception:
            pass
        temp.AddClip(clip_os)
        temp.Open()
        for i, local_frame in enumerate(local_frames):
            frame = temp.GetFrame(int(local_frame))
            sc = _framescope_from_frame(frame)
            if sc.get("present"):
                scopes.append(sc)
            want_jpeg = with_jpegs and (
                i == 0 or i == len(local_frames) // 2 or i == len(local_frames) - 1
            )
            if want_jpeg:
                jpeg = _frame_to_jpeg_b64(frame)
                if jpeg:
                    jpegs.append(jpeg)
    except Exception as exc:
        return {
            "profile": {"present": False, "error": str(exc)},
            "scopes_samples": 0,
            "preview_jpegs": [],
            "preview_jpeg": "",
            "duration_s": duration,
        }
    finally:
        if temp is not None:
            try:
                temp.Close()
            except Exception:
                pass

    profile = ca.build_look_profile(scopes)
    return {
        "profile": profile,
        "scopes_samples": len(scopes),
        "sample_frames": local_frames,
        "preview_jpegs": jpegs,
        "preview_jpeg": jpegs[len(jpegs) // 2] if jpegs else "",
        "duration_s": duration,
        "path": path,
    }


def inspect_color(
    clipId="",
    timeline_clip_id="",
    clip_id="",
    atFrame="",
    at_frame="",
    reference="",
    referenceClipId="",
    include_preview="true",
    **_kw,
) -> str:
    """Measure graded look at a frame: isolated scopes + ColorGrade + preview JPEG.

    Renders the clip alone (track-agnostic). Optional reference adds gap hints and
    a second preview. preview_jpeg fields are base64 JPEGs for vision.
    """
    try:
        from classes import color_agent as ca
        from classes.query import Clip

        ids = _resolve_color_target_ids(
            clipIds=clipId or clip_id,
            timeline_clip_id=timeline_clip_id,
        )
        if not ids:
            return _color_target_error("inspect_color")
        cid = ids[0]
        want_jpeg = str(include_preview if include_preview not in (None, "") else "true").strip().lower() not in (
            "0", "false", "no", "off",
        )

        app = _get_app()
        fps = app.project.get("fps") or {"num": 30, "den": 1}
        try:
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            fps_float = 30.0

        def _read_subject():
            clip_obj = Clip.get(id=cid)
            if not clip_obj:
                return None, None, None, None, f"Error: clip {cid} not found."
            data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
            effects = data.get("effects")
            grade = ca.summarize_color_grade(ca.find_color_grade(effects))
            grain = ca.summarize_film_grain(ca.find_film_grain(effects))
            frame_arg = atFrame if atFrame not in (None, "") else at_frame
            if frame_arg not in (None, ""):
                try:
                    frame_number = max(1, int(float(frame_arg)))
                except (TypeError, ValueError):
                    return None, None, None, None, "Error: atFrame must be an integer frame number."
            else:
                try:
                    frame_number = int(getattr(app.window.preview_thread, "current_frame", 1) or 1)
                except Exception:
                    frame_number = 1
                try:
                    pos = float(data.get("position") or 0)
                    start = float(data.get("start") or 0)
                    end = float(data.get("end") or start)
                    duration = max(0.0, end - start)
                    playhead_sec = (frame_number - 1) / max(fps_float, 1e-6)
                    if playhead_sec < pos or playhead_sec > pos + duration:
                        frame_number = max(1, int(pos * fps_float) + 1)
                except (TypeError, ValueError):
                    pass
            return data, grade, grain, frame_number, None

        if QThread is not None and QThread.currentThread() is not app.thread():
            data, grade, grain, frame_number, err = _run_on_main_thread(_read_subject)
        else:
            data, grade, grain, frame_number, err = _read_subject()
        if err:
            return err

        rendered = _render_clip_isolated(
            data or {}, frame_number, fps_float, with_jpeg=want_jpeg
        )
        scopes = rendered.get("scopes") or {"present": False}
        result = {
            "ok": True,
            "timeline_clip_id": cid,
            "atFrame": frame_number,
            "color": grade,
            "film_grain": grain,
            "scopes": scopes,
            "preview_jpeg": rendered.get("preview_jpeg") or "",
        }

        ref_id = str(reference or referenceClipId or "").strip()
        if ref_id:
            def _read_ref():
                ref_clip = Clip.get(id=ref_id)
                if not ref_clip:
                    return None, None, None, None, f"reference clip {ref_id} not found"
                rdata = ref_clip.data if isinstance(ref_clip.data, dict) else {}
                reffects = rdata.get("effects")
                rgrade = ca.summarize_color_grade(ca.find_color_grade(reffects))
                rgrain = ca.summarize_film_grain(ca.find_film_grain(reffects))
                try:
                    pos = float(rdata.get("position") or 0)
                    start = float(rdata.get("start") or 0)
                    end = float(rdata.get("end") or start)
                    duration = max(0.0, end - start)
                    ref_frame = max(1, int((pos + duration * 0.5) * fps_float) + 1)
                except (TypeError, ValueError):
                    ref_frame = frame_number
                return rdata, rgrade, rgrain, ref_frame, None

            if QThread is not None and QThread.currentThread() is not app.thread():
                rdata, rgrade, rgrain, ref_frame, ref_err = _run_on_main_thread(_read_ref)
            else:
                rdata, rgrade, rgrain, ref_frame, ref_err = _read_ref()
            if ref_err:
                result["warnings"] = [ref_err]
            else:
                ref_rendered = _render_clip_isolated(
                    rdata or {}, ref_frame, fps_float, with_jpeg=want_jpeg
                )
                ref_scopes = ref_rendered.get("scopes") or {"present": False}
                result["reference"] = {
                    "timeline_clip_id": ref_id,
                    "atFrame": ref_frame,
                    "color": rgrade,
                    "film_grain": rgrain,
                    "scopes": ref_scopes,
                }
                result["reference_preview_jpeg"] = ref_rendered.get("preview_jpeg") or ""
                result.update(ca.reference_gap_hints(scopes, ref_scopes))
                result["grades_differ"] = ca.grades_meaningfully_differ(grade, rgrade)
                result["grain_differ"] = ca.grain_meaningfully_differs(grain, rgrain)

        return json.dumps(result)
    except Exception as e:
        return f"Error: {e}"


def list_looks(query="", vibe="", **_kw) -> str:
    """List ColorGrade presets, LUT packs, and Film Grain looks with vibe tags.

    Optional query/vibe filters the catalog (e.g. query='teal cinematic').
    """
    try:
        from classes import color_agent as ca

        q = " ".join(p for p in (str(query or ""), str(vibe or "")) if p).strip()
        return json.dumps(ca.list_looks_catalog(q))
    except Exception as e:
        return f"Error: {e}"


def _create_film_grain_effect_json(generate_id) -> dict:
    try:
        import openshot
        from classes.film_grain_presets import FILM_GRAIN_CLASS_NAME

        effect = openshot.EffectInfo().CreateEffect(FILM_GRAIN_CLASS_NAME)
        if effect is None:
            raise RuntimeError("CreateEffect returned None")
        effect_id = generate_id()
        effect.Id(effect_id)
        payload = json.loads(effect.Json())
        if not payload.get("id"):
            payload["id"] = effect_id
        return payload
    except Exception:
        from classes.film_grain_presets import FILM_GRAIN_CLASS_NAME

        return {"class_name": FILM_GRAIN_CLASS_NAME, "id": generate_id() if callable(generate_id) else ""}


def apply_look(
    clipIds="",
    clip_ids="",
    timeline_clip_id="",
    clipId="",
    lookId="",
    look_id="",
    lutPath="",
    lut_path="",
    lutIntensity="",
    lut_intensity="",
    mix="",
    stackGrain="",
    stack_grain="",
    grainPreset="",
    grain_preset="",
    all_clips="",
    **_kw,
) -> str:
    """Apply a Look preset, LUT, or Film Grain to clip(s); one undo.

    Pass lookId from list_looks_tool (warm_up, teal_cinema, grain:35mm_classic, …)
    or lutPath to a .cube. Optional mix / stackGrain / grainPreset. Soft presets
    replace ColorGrade knobs (same as the Look menu); LUT merges onto existing grade,
    and for a LUT, mix (or lutIntensity) is the LUT's strength.
    Target resolution: explicit ids → selection → playhead; clipIds=\"all\" / all_clips
    for every timeline clip.
    """
    try:
        from classes import color_agent as ca

        all_flag = _tool_flag(_kw.get("allClips"), all_clips, _kw.get("all_clips"), default=False)
        ids = _resolve_color_target_ids(
            clip_ids=clip_ids,
            clipIds=clipIds,
            timeline_clip_id=timeline_clip_id,
            clipId=clipId,
            all_clips=all_flag,
        )
        if not ids:
            return _color_target_error("apply_look")

        raw_look = str(lookId or look_id or "").strip()
        raw_lut = str(lutPath or lut_path or "").strip()
        if not raw_look and not raw_lut:
            return "Error: apply_look requires lookId or lutPath."

        resolved = ca.resolve_look_id(raw_look) if raw_look else {"kind": "lut", "lut_path": raw_lut, "id": raw_lut}
        if raw_lut and resolved.get("kind") != "lut":
            return "Error: pass either lookId or lutPath for a LUT, not both conflicting kinds."
        if raw_lut:
            resolved = {"kind": "lut", "lut_path": raw_lut, "id": raw_lut}

        intensity_arg = lutIntensity if lutIntensity not in (None, "") else lut_intensity
        mix_arg = mix
        grain_arg = (
            stackGrain if stackGrain not in (None, "")
            else stack_grain if stack_grain not in (None, "")
            else grainPreset if grainPreset not in (None, "")
            else grain_preset
        )

        lut_intensity_val = None
        if intensity_arg not in (None, ""):
            lut_intensity_val = ca._coerce_float(intensity_arg, "lutIntensity")
        mix_val = None
        if mix_arg not in (None, ""):
            mix_val = ca._coerce_float(mix_arg, "mix")

        grain_id = None
        if grain_arg not in (None, ""):
            g = str(grain_arg).strip()
            if g.lower() in ("1", "true", "yes", "on"):
                grain_id = "35mm_classic"
            else:
                grain_resolved = ca.resolve_look_id(
                    g if g.startswith("grain:") else f"grain:{g}"
                )
                grain_id = grain_resolved["grain_id"]
        elif resolved.get("kind") == "film_grain":
            grain_id = resolved["grain_id"]

        # Openshot-backed helpers only after args validate (keeps Error: clean).
        from classes.film_grain_presets import (
            FILM_GRAIN_PRESET_NONE,
            apply_film_grain_preset,
            is_film_grain_effect,
        )

        # Validate LUT path up front (no undo on bad id).
        abs_lut = ""
        if resolved.get("kind") == "lut":
            abs_lut = _resolve_lut_file(resolved["lut_path"])

        app = _get_app()
        receipts = []
        warnings = []
        error_box = [None]

        def _filter_ids():
            return _filter_unlocked_color_clip_ids(app, ids, warnings)

        if QThread is not None and QThread.currentThread() is not app.thread():
            ids = _run_on_main_thread(_filter_ids)
        else:
            ids = _filter_ids()
        if not ids:
            if warnings:
                return "Error: " + "; ".join(warnings)
            return _color_target_error("apply_look")

        def _do_look():
            from classes.query import Clip as _Clip

            changed = 0
            with _transaction(app):
                for cid in ids:
                    clip_obj = _Clip.get(id=cid)
                    if not clip_obj:
                        warnings.append(f"clip {cid} not found")
                        continue
                    data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
                    effects = list(data.get("effects") or [])
                    receipt = {"timeline_clip_id": cid, "look": resolved.get("id")}

                    if resolved.get("kind") == "color_preset":
                        preset_name = resolved["preset_name"]
                        if preset_name == "reset":
                            new_effects = [
                                e for e in effects if not ca.is_color_grade_effect(e)
                            ]
                            if len(new_effects) == len(effects) and not grain_id:
                                receipt["status"] = "noop"
                                receipts.append(receipt)
                                continue
                            effects = new_effects
                            receipt["status"] = "reset"
                        else:
                            existing = ca.find_color_grade(effects)
                            base = ca.create_color_grade_effect_json(app.project.generate_id)
                            if existing and existing.get("id"):
                                base["id"] = existing["id"]
                            if existing and "order" in existing:
                                base["order"] = existing["order"]
                            graded = ca.apply_soft_color_preset(base, preset_name)
                            if mix_val is not None:
                                ca.set_scalar(graded, "mix", mix_val)
                            # Replace first ColorGrade only.
                            replaced = False
                            new_effects = []
                            for effect in effects:
                                if ca.is_color_grade_effect(effect):
                                    if not replaced:
                                        new_effects.append(graded)
                                        replaced = True
                                else:
                                    new_effects.append(effect)
                            if not replaced:
                                new_effects.append(graded)
                            effects = new_effects
                            receipt["status"] = "preset"
                            receipt["color"] = ca.summarize_color_grade(graded)

                    elif resolved.get("kind") == "lut":
                        existing = ca.find_color_grade(effects)
                        if existing is None:
                            existing = ca.create_color_grade_effect_json(
                                app.project.generate_id
                            )
                            effects.append(existing)
                        patch = {"lut": {"path": abs_lut}}
                        # ColorGrade applies its LUT after the mix, so mix
                        # would only fade the clip's existing knobs (an earlier
                        # "warmer") and leave the LUT at full strength. For a
                        # LUT look, mix means how strong the look is.
                        strength = (
                            lut_intensity_val if lut_intensity_val is not None else mix_val
                        )
                        if strength is not None:
                            patch["lut"]["strength"] = strength
                        merged = ca.merge_color_grade(existing, patch)
                        replaced = False
                        new_effects = []
                        for effect in effects:
                            if ca.is_color_grade_effect(effect):
                                if not replaced:
                                    new_effects.append(merged)
                                    replaced = True
                            else:
                                new_effects.append(effect)
                        if not replaced:
                            new_effects.append(merged)
                        effects = new_effects
                        receipt["status"] = "lut"
                        receipt["lut_path"] = merged.get("lut_path")
                        receipt["color"] = ca.summarize_color_grade(merged)

                    # Optional / primary film grain
                    apply_grain = grain_id or (
                        resolved.get("kind") == "film_grain" and resolved.get("grain_id")
                    )
                    if apply_grain:
                        gid = grain_id or resolved.get("grain_id")
                        grain_indexes = [
                            i for i, e in enumerate(effects) if is_film_grain_effect(e)
                        ]
                        if gid == FILM_GRAIN_PRESET_NONE:
                            if grain_indexes:
                                effects = [
                                    e for e in effects if not is_film_grain_effect(e)
                                ]
                                receipt["grain"] = "none"
                            else:
                                # No FilmGrain present — skip write unless colour also changed.
                                receipt["grain"] = "none"
                                if receipt.get("status") is None:
                                    receipt["status"] = "noop"
                                    receipts.append(receipt)
                                    continue
                        else:
                            source = (
                                effects[grain_indexes[0]]
                                if grain_indexes
                                else _create_film_grain_effect_json(app.project.generate_id)
                            )
                            grain_effect = apply_film_grain_preset(source, gid)
                            if grain_indexes:
                                if effects[grain_indexes[0]].get("id"):
                                    grain_effect["id"] = effects[grain_indexes[0]]["id"]
                                for index in reversed(grain_indexes[1:]):
                                    del effects[index]
                                effects[grain_indexes[0]] = grain_effect
                            else:
                                effects.append(grain_effect)
                            receipt["grain"] = gid
                        if resolved.get("kind") == "film_grain":
                            receipt["status"] = receipt.get("status") or "grain"

                    if receipt.get("status") is None and not apply_grain:
                        receipt["status"] = "noop"
                        receipts.append(receipt)
                        continue

                    _write_clip_effects(clip_obj, effects)
                    changed += 1
                    receipts.append(receipt)
                if changed == 0 and not receipts:
                    error_box[0] = "Error: no matching timeline clips."
            try:
                app.window.refreshFrameSignal.emit()
            except Exception:
                pass

        if QThread is not None and QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do_look)
        else:
            _do_look()

        if error_box[0]:
            return error_box[0]
        if not receipts:
            return "Error: no clips were updated."
        result = {
            "ok": True,
            "clips": receipts,
            "warnings": warnings,
            "undo": "one step",
        }
        try:
            from classes.query import Clip as _Clip
            first_id = receipts[0].get("timeline_clip_id") or receipts[0].get("id")
            if QThread is not None and QThread.currentThread() is not app.thread():
                clip_obj = _run_on_main_thread(lambda: _Clip.get(id=first_id))
            else:
                clip_obj = _Clip.get(id=first_id)
            data = clip_obj.data if clip_obj and isinstance(clip_obj.data, dict) else {}
            fps = app.project.get("fps") or {"num": 30, "den": 1}
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
            after = _render_clip_isolated(data, 1, fps_float, with_jpeg=True)
            after_scopes = after.get("scopes") or {}
            result["after_preview_jpeg"] = after.get("preview_jpeg") or ""
            result["after_scopes"] = after_scopes
            hi = after_scopes.get("clipped_highlights")
            try:
                hi_f = float(hi) if hi is not None else 0.0
            except (TypeError, ValueError):
                hi_f = 0.0
            result["nuke_risk"] = bool(hi_f >= 0.12)
            # Attach synthetic vibe goal distance when lookId known.
            look_used = receipts[0].get("look")
            goal = ca.target_profile_for_look(str(look_used or ""))
            if goal and after_scopes.get("present"):
                after_prof = ca.build_look_profile(after_scopes)
                result["look_distance_to_vibe"] = ca.look_profile_distance(after_prof, goal)
                result["vibe_goal"] = str(look_used or "")
        except Exception as exc:
            result["after_preview_jpeg"] = ""
            result["nuke_risk"] = False
            result["warnings"] = list(warnings) + [f"after preview failed: {exc}"]
        return json.dumps(result)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def match_color_to_reference(
    clipId="",
    timeline_clip_id="",
    clip_id="",
    clipIds="",
    clip_ids="",
    reference="",
    referenceClipId="",
    referenceFileId="",
    referenceImagePath="",
    all_clips="",
    dry_run="",
    dryRun="",
    max_iterations="",
    **_kw,
) -> str:
    """Match SUBJECT look to REFERENCE via LookProfile + closed verify loop.

    Reference kinds (first non-empty wins):
      referenceClipId / reference — timeline clip
      referenceFileId — media-bin file
      referenceImagePath — absolute still/video path

    Subjects: clipId(s), or all_clips / clipIds='all' for the whole timeline.
    Dense-samples the reference across its full duration, solves ColorGrade,
    applies (one undo), re-profiles AFTER, recovers from nuke — up to 5 cycles.
    """
    try:
        from classes import color_agent as ca
        from classes.query import Clip

        try:
            from classes.query import File  # media-bin referenceFileId path
        except ImportError:  # headless stubs may omit File
            File = None

        all_flag = _tool_flag(_kw.get("allClips"), all_clips, _kw.get("all_clips"), default=False)
        ids = _resolve_color_target_ids(
            clip_ids=clip_ids or clip_id or clipId,
            clipIds=clipIds or clipId or clip_id,
            timeline_clip_id=timeline_clip_id,
            clipId=clipId or clip_id,
            all_clips=all_flag,
        )
        if not ids:
            return _color_target_error("match_color_to_reference")

        ref_clip_id = str(reference or referenceClipId or "").strip()
        ref_file_id = str(referenceFileId or _kw.get("reference_file_id", "") or "").strip()
        ref_image_path = str(
            referenceImagePath or _kw.get("reference_image_path", "") or ""
        ).strip()
        if not ref_clip_id and not ref_file_id and not ref_image_path:
            return (
                "Error: match_color_to_reference requires referenceClipId, "
                "referenceFileId, or referenceImagePath."
            )
        if ref_clip_id and ref_clip_id in ids:
            if len(ids) == 1:
                return "Error: subject and reference are the same clip."
            ids = [cid for cid in ids if cid != ref_clip_id]
        if not ids:
            return _color_target_error("match_color_to_reference")
        if ref_file_id and File is None:
            return "Error: media-bin File lookup unavailable in this environment."

        dry = _tool_flag(dryRun, dry_run, _kw.get("dry_run"), default=False)
        try:
            max_iters = int(max_iterations or _kw.get("maxIterations") or 5)
        except (TypeError, ValueError):
            max_iters = 5
        max_iters = max(1, min(5, max_iters))

        app = _get_app()
        try:
            fps = app.project.get("fps") or {"num": 30, "den": 1}
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            fps_float = 30.0

        match_warnings = []

        def _filter_ids():
            return _filter_unlocked_color_clip_ids(app, ids, match_warnings)

        if QThread is not None and QThread.currentThread() is not app.thread():
            ids = _run_on_main_thread(_filter_ids)
        else:
            ids = _filter_ids()
        if not ids:
            if match_warnings:
                return "Error: " + "; ".join(match_warnings)
            return _color_target_error("match_color_to_reference")

        def _load_clip(cid):
            clip_obj = Clip.get(id=cid)
            if not clip_obj:
                return None
            return clip_obj.data if isinstance(clip_obj.data, dict) else {}

        def _on_main(fn):
            if QThread is not None and QThread.currentThread() is not app.thread():
                return _run_on_main_thread(fn)
            return fn()

        # --- Reference LookProfile (dense) ---------------------------------
        ref_kind = "clip" if ref_clip_id else ("file" if ref_file_id else "path")
        ref_effect = None
        ref_grain = None
        if ref_clip_id:
            ref_data = _on_main(lambda: _load_clip(ref_clip_id))
            if not ref_data:
                return f"Error: reference clip {ref_clip_id} not found."
            ref_pack = _profile_clip_dense(ref_data, fps_float, with_jpegs=True)
            ref_effect, ref_grain = _on_main(
                lambda: (
                    ca.find_color_grade(ref_data.get("effects")),
                    ca.find_film_grain(ref_data.get("effects")),
                )
            )
        else:
            media_path = ref_image_path
            if ref_file_id:
                def _file_path():
                    fobj = File.get(id=ref_file_id)
                    if not fobj:
                        return ""
                    if hasattr(fobj, "absolute_path"):
                        return str(fobj.absolute_path() or "")
                    data = fobj.data if isinstance(getattr(fobj, "data", None), dict) else {}
                    return str((data.get("path") or data.get("reader", {}).get("path") or ""))
                media_path = _on_main(_file_path)
                if not media_path:
                    return f"Error: media-bin file {ref_file_id} not found."
            ref_pack = _profile_media_path(media_path, with_jpegs=True)

        ref_profile = ref_pack.get("profile") or {"present": False}
        if not ref_profile.get("present"):
            return json.dumps({
                "ok": False,
                "error": "Could not measure reference look",
                "reference_kind": ref_kind,
                "detail": ref_profile.get("error") or "no scopes",
            })

        # --- Subject profiles (first clip dense; others mid-frame for speed) -
        subject_data = {}
        for cid in ids:
            data = _on_main(lambda c=cid: _load_clip(c))
            if data:
                subject_data[cid] = data
        if not subject_data:
            return "Error: no matching timeline clips."

        primary_id = ids[0] if ids[0] in subject_data else next(iter(subject_data))
        before_pack = _profile_clip_dense(
            subject_data[primary_id], fps_float, with_jpegs=True
        )
        before_profile = before_pack.get("profile") or {"present": False}
        outdoor = ca.is_outdoor_bright_profile(before_profile)
        look_distance_before = ca.look_profile_distance(before_profile, ref_profile)

        sub_effect = ca.find_color_grade(subject_data[primary_id].get("effects"))
        sub_grain = ca.find_film_grain(subject_data[primary_id].get("effects"))
        grade_action = (
            ca.match_grade_action(sub_effect, ref_effect)
            if ref_clip_id else {"mode": "scopes"}
        )
        grain_action = (
            ca.match_grain_action(sub_grain, ref_grain)
            if ref_clip_id else {"mode": "noop"}
        )

        solved = ca.solve_grade_from_profiles(
            before_profile, ref_profile, scale=1.0
        )
        # Prefer paste when reference is a graded timeline clip.
        if grade_action.get("mode") in ("paste", "reset"):
            initial_color = grade_action
            match_mode = grade_action.get("mode")
        elif solved:
            initial_color = {"mode": "scopes", "patch": solved}
            match_mode = "look_profile"
        else:
            initial_color = {"mode": "noop"}
            match_mode = "vision"

        result = {
            "ok": True,
            "timeline_clip_ids": list(subject_data.keys()),
            "clips_graded": list(subject_data.keys()),
            "reference_kind": ref_kind,
            "reference": ref_clip_id or ref_file_id or ref_image_path,
            "look_distance_before": look_distance_before,
            "match_mode": match_mode,
            "grain_mode": grain_action.get("mode"),
            "samples_used": {
                "subject": before_pack.get("scopes_samples"),
                "reference": ref_pack.get("scopes_samples"),
            },
            "before_profile": before_profile,
            "reference_profile": ref_profile,
            "before_preview_jpeg": before_pack.get("preview_jpeg") or "",
            "reference_preview_jpeg": ref_pack.get("preview_jpeg") or "",
            "outdoor_bright": outdoor,
            "vision": (
                "Compare BEFORE vs REFERENCE vs AFTER. Match iterates until "
                "look_distance is close or max steps; nuke_risk means recovery ran."
            ),
        }

        if (
            initial_color.get("mode") == "noop"
            and grain_action.get("mode") == "noop"
            and (look_distance_before is None or look_distance_before < ca.LOOK_DISTANCE_MATCHED)
        ):
            result["applied"] = False
            result["status"] = "matched"
            result["message"] = "Already close to reference look."
            return json.dumps(result)

        if dry:
            result["applied"] = False
            result["proposed_patch"] = (
                initial_color.get("patch")
                or ({"color": "paste_from_reference"} if initial_color.get("mode") == "paste" else {})
            )
            result["proposed_grain"] = grain_action.get("mode")
            return json.dumps(result)

        # --- Apply + verify loop (one undo for whole batch) -----------------
        error_box = [None]
        iterations = []
        recovered = False
        final_status = "still_far"
        after_profile = before_profile
        after_jpeg = ""
        current_patch = dict(initial_color.get("patch") or {})
        paste_color = initial_color.get("color") if initial_color.get("mode") == "paste" else None
        do_reset = initial_color.get("mode") == "reset"
        grain_done = False

        def _apply_effects_batch(color_mode, patch, grain_act, *, first_pass):
            from classes.query import Clip as _Clip
            from classes.film_grain_presets import is_film_grain_effect as _is_grain

            for cid, data in list(subject_data.items()):
                    clip_obj = _Clip.get(id=cid)
                    if not clip_obj:
                        continue
                    effects = list(
                        (clip_obj.data if isinstance(clip_obj.data, dict) else {}).get("effects") or []
                    )
                    # Per-subject grade/grain on the first pass so batch match
                    # does not reuse the primary clip's decision for everyone.
                    cid_color_mode = color_mode
                    cid_paste = paste_color
                    cid_grain = grain_act
                    if first_pass and ref_clip_id:
                        sub_e = ca.find_color_grade(effects)
                        sub_g = ca.find_film_grain(effects)
                        g_act = ca.match_grade_action(sub_e, ref_effect)
                        cid_grain = ca.match_grain_action(sub_g, ref_grain)
                        if g_act.get("mode") == "reset":
                            cid_color_mode = "reset"
                            cid_paste = None
                        elif g_act.get("mode") == "paste":
                            cid_color_mode = "paste"
                            cid_paste = g_act.get("color")
                        elif color_mode == "patch":
                            cid_color_mode = "patch"
                        else:
                            cid_color_mode = "noop"

                    if cid_color_mode == "reset":
                        effects = [e for e in effects if not ca.is_color_grade_effect(e)]
                    elif cid_color_mode == "paste" and cid_paste:
                        existing = ca.find_color_grade(effects)
                        if existing is None:
                            effect = ca.create_color_grade_effect_json(app.project.generate_id)
                            effects.append(effect)
                            existing = effect
                        merged = ca.merge_color_grade(existing, {"color": cid_paste})
                        new_effects = []
                        replaced = False
                        for effect in effects:
                            if ca.is_color_grade_effect(effect):
                                if not replaced:
                                    new_effects.append(merged)
                                    replaced = True
                            else:
                                new_effects.append(effect)
                        if not replaced:
                            new_effects.append(merged)
                        effects = new_effects
                    elif cid_color_mode == "patch" and patch:
                        existing = ca.find_color_grade(effects)
                        if existing is None:
                            effect = ca.create_color_grade_effect_json(app.project.generate_id)
                            effects.append(effect)
                            existing = effect
                        merge_kwargs = {}
                        for k, v in patch.items():
                            if k not in ca.SCALAR_KEYS:
                                continue
                            dkey = f"{k}_delta"
                            if dkey in ca.DELTA_KEYS:
                                merge_kwargs[dkey] = v
                            else:
                                cur = ca.scalar_y(existing, k)
                                if cur is None:
                                    cur = ca.SCALAR_DEFAULTS.get(k, 0.0)
                                merge_kwargs[k] = float(cur) + float(v)
                        merged = ca.merge_color_grade(existing, merge_kwargs)
                        new_effects = []
                        replaced = False
                        for effect in effects:
                            if ca.is_color_grade_effect(effect):
                                if not replaced:
                                    new_effects.append(merged)
                                    replaced = True
                            else:
                                new_effects.append(effect)
                        if not replaced:
                            new_effects.append(merged)
                        effects = new_effects

                    if first_pass and cid_grain.get("mode") == "reset":
                        effects = [e for e in effects if not _is_grain(e)]
                    elif first_pass and cid_grain.get("mode") == "paste":
                        paste_g = cid_grain.get("grain") or {}
                        grain_indexes = [i for i, e in enumerate(effects) if _is_grain(e)]
                        new_g = copy.deepcopy(paste_g) if isinstance(paste_g, dict) else {}
                        new_g["class_name"] = "FilmGrain"
                        if grain_indexes:
                            if effects[grain_indexes[0]].get("id"):
                                new_g["id"] = effects[grain_indexes[0]]["id"]
                            elif not new_g.get("id"):
                                new_g["id"] = app.project.generate_id()
                            for index in reversed(grain_indexes[1:]):
                                del effects[index]
                            effects[grain_indexes[0]] = new_g
                        else:
                            if not new_g.get("id"):
                                new_g["id"] = app.project.generate_id()
                            effects.append(new_g)

                    _write_clip_effects(clip_obj, effects)
                    subject_data[cid] = (
                        clip_obj.data if isinstance(clip_obj.data, dict) else data
                    )
            try:
                app.window.refreshFrameSignal.emit()
            except Exception:
                pass

        # Open one transaction for the whole closed loop.
        with _transaction(app):
            for it in range(max_iters):
                color_mode = (
                    "reset" if do_reset and it == 0
                    else ("paste" if paste_color and it == 0 else "patch")
                )
                patch = {} if color_mode in ("reset", "paste") else current_patch
                if it == 0 and color_mode == "patch" and not patch and not paste_color and not do_reset:
                    if grain_action.get("mode") == "noop":
                        final_status = "still_far"
                        break
                _on_main(
                    lambda cm=color_mode, p=patch, g=grain_action, fp=(it == 0):
                    _apply_effects_batch(cm, p, g, first_pass=fp)
                )
                # Re-profile primary subject
                after_pack = _profile_clip_dense(
                    subject_data[primary_id], fps_float, with_jpegs=True,
                    max_samples=min(24, before_pack.get("scopes_samples") or 16),
                )
                after_profile = after_pack.get("profile") or {"present": False}
                after_jpeg = after_pack.get("preview_jpeg") or ""
                outcome = ca.assess_grade_outcome(
                    before_profile, after_profile, ref_profile, outdoor_bright=outdoor
                )
                iterations.append({
                    "iteration": it + 1,
                    "status": outcome.get("status"),
                    "look_distance": outcome.get("look_distance_after"),
                    "nuke_risk": outcome.get("nuke_risk"),
                    "reasons": outcome.get("reasons") or [],
                })
                if outcome.get("ok"):
                    final_status = "matched"
                    break
                if outcome.get("nuke_risk"):
                    recovered = True
                    current_patch = outcome.get("suggested_recovery_patch") or {}
                    paste_color = None
                    do_reset = False
                    final_status = "recovered_from_nuke"
                    if not current_patch:
                        break
                    continue
                # Still far / closer — smaller solve step
                current_patch = ca.solve_grade_from_profiles(
                    after_profile, ref_profile, scale=max(0.25, 0.7 ** (it + 1))
                )
                paste_color = None
                do_reset = False
                final_status = outcome.get("status") or "closer_not_exact"
                if not current_patch:
                    break
                before_profile = after_profile  # next assess uses latest as before

        if error_box[0]:
            return error_box[0]

        result["applied"] = True
        result["undo"] = "one step"
        result["iterations"] = iterations
        result["iteration_count"] = len(iterations)
        result["status"] = final_status
        result["recovered_from_nuke"] = recovered
        result["after_profile"] = after_profile
        result["after_preview_jpeg"] = after_jpeg
        result["look_distance_after"] = ca.look_profile_distance(after_profile, ref_profile)
        result["nuke_risk"] = any(i.get("nuke_risk") for i in iterations) and final_status != "matched"
        if final_status == "matched":
            result["message"] = (
                f"Matched look on {len(subject_data)} clip(s) "
                f"in {len(iterations)} iteration(s)."
            )
        elif recovered:
            result["message"] = (
                "Look was nuking highlights/shadows; recovered with a softer grade. "
                "Inspect AFTER vs REFERENCE — nudge if still off."
            )
        else:
            result["message"] = (
                f"Closer but not exact (status={final_status}). "
                "Look at AFTER vs REFERENCE and nudge, or re-run match."
            )
        return json.dumps(result)
    except Exception as e:
        return f"Error: {e}"



# Tools exposed to the main chat / video / transitions agents.
AGENT_TOOL_HANDLERS = {
    # Project
    "list_files_tool": list_files,
    "list_clips_tool": list_clips,
    "list_layers_tool": list_layers,
    # list_markers_tool: classes.editor_tools.tracks_nav
    # new/save/open_project_tool: classes.editor_tools.project_export
    # Playback
    "watch_clip_tool": watch_clip_and_play,
    "watch_clip_window_tool": watch_clip_window,
    # play_tool: classes.editor_tools.tracks_nav
    "go_to_start_tool": go_to_start,
    "go_to_end_tool": go_to_end,
    "undo_tool": undo,
    "redo_tool": redo,
    # Timeline
    # add_track_tool, add_marker_tool: classes.editor_tools.tracks_nav
    "delete_from_timeline_tool": delete_from_timeline,
    # Deprecated aliases -- kept dispatchable for stored plans and in-flight
    # sessions; the backend catalog exposes delete_from_timeline_tool only.
    "remove_clip_tool": remove_clip,
    "delete_clips_on_track_tool": delete_clips_on_track,
    # Audio mix / ducking
    "analyze_timeline_audio_tool": analyze_timeline_audio,
    "set_clip_volume_tool": set_clip_volume,
    "duck_under_speech_tool": duck_under_speech,
    "zoom_in_tool": zoom_in,
    "zoom_out_tool": zoom_out,
    "center_on_playhead_tool": center_on_playhead,
    "import_files_tool": import_files,
    "wait_until_project_indexed_tool": wait_until_project_indexed,
    # Export
    # Clips
    "get_file_info_tool": get_file_info,
    "split_file_add_clip_tool": split_file_add_clip,
    "add_clip_to_timeline_tool": add_clip_to_timeline,
    "import_video_url_and_add_to_timeline_tool": import_video_url_and_add_to_timeline,
    "ingest_web_video_tool": ingest_web_video,
    "slice_clip_at_playhead_tool": slice_clip_at_playhead,
    "reverse_clip_tool": reverse_clip,
    # Search / slice / modify (tag-query resolved)
    "search_clips_tool": search_clips,
    "search_clip_scenes_tool": search_clip_scenes,
    "get_project_catalog_tool": get_project_catalog,
    "slice_clip_at_best_match_tool": slice_clip_at_best_match,
    "suggest_motion_graphics_placements_tool": suggest_motion_graphics_placements,
    "propose_overlay_windows_tool": propose_overlay_windows,
    "place_motion_graphic_tool": place_motion_graphic,
    # Remotion / HyperFrames
    "fetch_motion_graphics_video_tool": fetch_motion_graphics_video,
    "fetch_remotion_video_from_supabase_tool": fetch_motion_graphics_video,
    # Video generation / AI edit
    "generate_video_and_add_to_timeline_tool": generate_video_and_add_to_timeline,
    "modify_clip_tool": modify_clip,
    "generate_transition_clip_tool": generate_transition_clip,
    # OpenShot transitions (mask/dissolve)
    "list_transitions_tool": list_transitions,
    "search_transitions_tool": search_transitions,
    "apply_transition_tool": apply_transition,
    # Colour grade (Phase 6 — ColorGrade / FrameScope)
    "apply_color_tool": apply_color,
    "inspect_color_tool": inspect_color,
    "list_looks_tool": list_looks,
    "apply_look_tool": apply_look,
    "match_color_to_reference_tool": match_color_to_reference,
    # generate_tts_and_add_to_timeline_tool: classes.editor_tools.ai_generation_tts
    # Stock / planning
    "import_stock_media_tool": import_stock_media,
    "resummarize_project_file_tool": resummarize_project_file,
    "reindex_project_file_tool": reindex_project_file,
    "get_clips_with_full_metadata_tool": get_clips_with_full_metadata,
    "get_timeline_placements_metadata_tool": get_timeline_placements_metadata,
    "get_timeline_state_tool": get_timeline_state,
}

# Editor tools declared with @editor_tool in classes/editor_tools/ (one module
# per workstream). Merged here so dispatch, chat labels, the undo grouping and
# the MCP listing treat them exactly like the handlers above.
from classes.editor_tools import REGISTRY as _EDITOR_TOOL_SPECS  # noqa: E402
from classes.agent_tools.schema import TOOL_SCHEMAS as _TOOL_SCHEMAS  # noqa: E402

# #183's add_effect / add_title / set_keyframes / set_project_setting and #220's
# add_captions are served by the editor tools of the same names, whose arguments
# are a superset of theirs.
for _phase_handlers in (PHASE3_HANDLERS, PHASE4_HANDLERS, PHASE5_HANDLERS):
    AGENT_TOOL_HANDLERS.update({name: func for name, func in _phase_handlers.items()
                                if name not in _EDITOR_TOOL_SPECS})
_editor_overlap = set(_EDITOR_TOOL_SPECS) & set(AGENT_TOOL_HANDLERS)
assert not _editor_overlap, f"editor_tools re-registers {sorted(_editor_overlap)}"
AGENT_TOOL_HANDLERS.update({name: spec.func for name, spec in _EDITOR_TOOL_SPECS.items()})
# One source of truth for an editor tool's arguments: its registry schema is the
# one execute_tool validates against and the MCP server advertises.
_TOOL_SCHEMAS.update({name: spec.schema for name, spec in _EDITOR_TOOL_SPECS.items()})

TOOL_HANDLERS = dict(AGENT_TOOL_HANDLERS)

# Humanized titles for chat tool-block headers (main agent tools only).
TOOL_DISPLAY_LABELS = {
    "list_files_tool": "List project media",
    "list_clips_tool": "List clips",
    "list_layers_tool": "List tracks",
    "watch_clip_tool": "Load and play clip",
    "watch_clip_window_tool": "Watch clip window",
    "go_to_start_tool": "Seek to start",
    "go_to_end_tool": "Seek to end",
    "undo_tool": "Undo",
    "redo_tool": "Redo",
    "delete_from_timeline_tool": "Delete from timeline",
    "remove_clip_tool": "Delete from timeline",
    "delete_clips_on_track_tool": "Delete from timeline",
    "analyze_timeline_audio_tool": "Analyze timeline audio",
    "set_clip_volume_tool": "Set clip volume",
    "duck_under_speech_tool": "Duck music under speech",
    "zoom_in_tool": "Zoom in",
    "zoom_out_tool": "Zoom out",
    "center_on_playhead_tool": "Center on playhead",
    "import_files_tool": "Import files from disk",
    "wait_until_project_indexed_tool": "Wait for indexing",
    "get_file_info_tool": "Read file info",
    "split_file_add_clip_tool": "Split clip and add to timeline",
    "add_clip_to_timeline_tool": "Add clip to timeline",
    "import_video_url_and_add_to_timeline_tool": "Import video to timeline",
    "ingest_web_video_tool": "Ingest web / YouTube video",
    "slice_clip_at_playhead_tool": "Slice clip at playhead",
    "reverse_clip_tool": "Reverse clip",
    "search_clips_tool": "Search project index",
    "search_clip_scenes_tool": "Search clip scenes",
    "get_project_catalog_tool": "Read project catalog",
    "slice_clip_at_best_match_tool": "Slice clip at best match",
    "suggest_motion_graphics_placements_tool": "Suggest MG placements (deprecated)",
    "propose_overlay_windows_tool": "Propose overlay windows",
    "place_motion_graphic_tool": "Place motion graphic",
    "fetch_motion_graphics_video_tool": "Fetch HyperFrames video",
    "fetch_remotion_video_from_supabase_tool": "Fetch HyperFrames video",
    "generate_video_and_add_to_timeline_tool": "Generate video",
    "modify_clip_tool": "AI edit clip",
    "generate_transition_clip_tool": "Bake A + morph + B",
    "list_transitions_tool": "List transitions",
    "search_transitions_tool": "Search transitions",
    "apply_transition_tool": "Apply transition",
    "apply_color_tool": "Apply color grade",
    "inspect_color_tool": "Inspect color",
    "list_looks_tool": "List looks",
    "apply_look_tool": "Apply look",
    "match_color_to_reference_tool": "Match color to reference",
    "import_stock_media_tool": "Import stock media",
    "resummarize_project_file_tool": "Resummarize file",
    "reindex_project_file_tool": "Reindex file",
    "get_clips_with_full_metadata_tool": "Read clips metadata",
    "get_timeline_placements_metadata_tool": "Read timeline placements",
    "get_timeline_state_tool": "Read timeline state",
}
TOOL_DISPLAY_LABELS.update(PHASE3_DISPLAY_LABELS)
TOOL_DISPLAY_LABELS.update(PHASE4_DISPLAY_LABELS)
TOOL_DISPLAY_LABELS.update(PHASE5_DISPLAY_LABELS)
TOOL_DISPLAY_LABELS.update({name: spec.label for name, spec in _EDITOR_TOOL_SPECS.items()})

assert set(TOOL_DISPLAY_LABELS) == set(AGENT_TOOL_HANDLERS), (
    "TOOL_DISPLAY_LABELS keys must match AGENT_TOOL_HANDLERS"
)

# Server-side / subagent names that never hit AGENT_TOOL_HANDLERS but still
# appear as tool_started titles over the WebSocket.
_EXTRA_TOOL_DISPLAY_LABELS = {
    "motion-graphics-agent": "Motion graphics",
    "publish_session_draft_tool": "Publish motion graphic",
    "lint_session_draft_tool": "Lint draft",
    "product_demo": "Product demo",
    "plan_product_demo_tool": "Plan product demo",
    "render_product_demo_tool": "Render product demo",
    "check_motion_graphics_health_tool": "Motion graphics health",
    "get_motion_graphics_job_status_tool": "Motion job status",
    # The assistant harness contributes its own tool names to the transcript.
    # `task` is the orchestrator handing work to a specialist; the file and
    # shell tools only ever run inside the motion-graphics sandbox, on
    # session/draft.html. Left to the generic fallback these read as "Task",
    # "Bash" and "Edit" -- a coding runtime showing through a video editor.
    "task": "Handing off to a specialist",
    "bash": "Building the motion graphic",
    "edit": "Editing the motion graphic",
    "write": "Writing the motion graphic",
    "read": "Reading the motion graphic",
    "glob": "Looking through motion graphic files",
    "grep": "Searching the motion graphic",
    "question": "Asking you a question",
    "todowrite": "Updating the task list",
    # Denied to the assistant, but a refused call still lands in the transcript.
    "webfetch": "Reading a web page",
    "websearch": "Searching the web",
    # Claude Code (a CLI chat backend) defers most tool schemas -- the Zenvi
    # editor tools among them -- and loads them with ToolSearch before a call.
    "toolsearch": "Looking up editor tools",
}


def humanize_tool_name(tool_name: str) -> str:
    """Return a short human-readable title for a tool name."""
    if tool_name in TOOL_DISPLAY_LABELS:
        return TOOL_DISPLAY_LABELS[tool_name]
    if tool_name in _EXTRA_TOOL_DISPLAY_LABELS:
        return _EXTRA_TOOL_DISPLAY_LABELS[tool_name]
    # The harness runtime's own names are all-lowercase keys here; match them
    # however they arrive cased ("TodoWrite", "WebFetch").
    if tool_name.lower() in _EXTRA_TOOL_DISPLAY_LABELS:
        return _EXTRA_TOOL_DISPLAY_LABELS[tool_name.lower()]
    base = tool_name[:-5] if tool_name.endswith("_tool") else tool_name
    # CLI runtimes name their tools in CamelCase ("NotebookEdit"); split the
    # words first, or capitalize() mashes them into "Notebookedit".
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)
    return base.replace("_", " ").strip().capitalize() or "Run tool"


# Tools that only READ project / timeline data and don't mutate Qt widgets.
# Safe to invoke from worker threads, which lets the agent run several of them
# concurrently without serializing through the Qt main-thread dispatcher.
READ_ONLY_TOOLS = frozenset({
    "list_files_tool",
    "list_clips_tool",
    "list_layers_tool",
    "get_timeline_state_tool",
    "get_file_info_tool",
    "list_transitions_tool",
    "search_transitions_tool",
    "get_clips_with_full_metadata_tool",
    "get_timeline_placements_metadata_tool",
    "propose_overlay_windows_tool",
    "analyze_timeline_audio_tool",
    "inspect_color_tool",
    "list_looks_tool",
    "inspect_timeline_tool",
    "inspect_media_tool",
    "get_transcript_tool",
    "transcribe_media_tool",
    "detect_beats_tool",
    "diarize_media_tool",
    "search_media_local_tool",
    "export_captions_tool",
}) | frozenset(name for name, spec in _EDITOR_TOOL_SPECS.items() if spec.read_only)

# Tools that perform long-running network/IO work and only briefly touch Qt
# state.  They marshal those brief reads onto the main thread internally, so
# the dispatcher must NOT wrap the entire call in ``_run_on_main_thread`` —
# doing so would block the GUI for the duration of the network call (up to
# 30 minutes for TwelveLabs indexing) and serialize parallel agent calls.
BACKGROUND_SAFE_TOOLS = frozenset({
    "inspect_color_tool",
    "match_color_to_reference_tool",
    "list_looks_tool",
    # LUT existence checks run in the handler; the ColorGrade write marshals.
    "apply_color_tool",
    "apply_look_tool",
    "reindex_project_file_tool",
    # Probing a folder of media can outlast the 30s dispatcher budget; the
    # add_files call marshals itself with its own, longer timeout.
    "import_files_tool",
    # Polls indexing state for minutes — must never occupy the GUI thread.
    "wait_until_project_indexed_tool",
    # Downloads + re-encodes off the GUI thread; its timeline mutations
    # marshal to the main thread internally.
    "import_video_url_and_add_to_timeline_tool",
    "resummarize_project_file_tool",
    "import_stock_media_tool",
    # Long-running Runware/ffmpeg work; Qt timeline touches are marshalled internally.
    "generate_video_and_add_to_timeline_tool",
    "modify_clip_tool",
    "generate_transition_clip_tool",
    # Network search against project TwelveLabs index (File reads are read-only).
    "search_clips_tool",
    "search_clip_scenes_tool",
    "watch_clip_window_tool",
    "get_project_catalog_tool",
    "slice_clip_at_best_match_tool",
    "split_file_add_clip_tool",
    "add_clip_to_timeline_tool",
    "propose_overlay_windows_tool",
    # HyperFrames download + alpha re-encode can take a while.
    "fetch_motion_graphics_video_tool",
    "fetch_remotion_video_from_supabase_tool",
    "ingest_web_video_tool",
    "inspect_timeline_tool",
    "inspect_media_tool",
    # Local ASR / VAD / beats / embeds can take minutes; Qt mutations marshal themselves.
    "get_transcript_tool",
    "transcribe_media_tool",
    "remove_words_tool",
    "remove_silence_tool",
    "add_captions_tool",
    "export_captions_tool",
    # Its no-cue fallback extracts audio and runs VAD; the writes marshal themselves.
    "duck_under_speech_tool",
    "detect_beats_tool",
    "diarize_media_tool",
    "search_media_local_tool",
}) | frozenset(name for name, spec in _EDITOR_TOOL_SPECS.items() if spec.background_safe)

# Tools whose main-thread work can legitimately run far longer than
# _run_on_main_thread's default 30s wait -- e.g. export_video_tool's encode
# loop runs synchronously on the GUI thread for the entire video (minutes to
# hours for a real project), and a 30s timeout raises TimeoutError to the
# caller while the export keeps running to completion in the background,
# which the agent (and user) sees as "Export failed" even though a file may
# still land later. Give these a generous ceiling instead of the default.
_MAIN_THREAD_TIMEOUTS = {
    # Fallback if export_video_tool is ever removed from BACKGROUND_SAFE_TOOLS.
    "export_video_tool": _EXPORT_MAIN_THREAD_TIMEOUT,
}


# Tools that execute_tool must NOT wrap in a transaction.
#
# Read-only tools make no mutations, so a transaction would be pure overhead.
# undo/redo are the sharp case: they walk the history stack, and opening a
# transaction around that would stamp the caller's id onto the reversal
# actions pushed into redoHistory, gluing separate steps together.
_UNGROUPED_TOOLS = READ_ONLY_TOOLS | frozenset({"undo_tool", "redo_tool"})


# Tools whose main-thread runtime scales with a `steps` argument.  A fixed 30s
# budget is fine for a single undo but can sever a steps=20 run mid-loop --
# _run_on_main_thread raises TimeoutError in the *caller* while the queued work
# keeps running, so the tool would report failure while undos kept applying.
_STEPPED_TOOLS = frozenset({"undo_tool", "redo_tool"})
_MAIN_THREAD_TIMEOUT_DEFAULT = 30
_MAIN_THREAD_TIMEOUT_PER_STEP = 8


def _main_thread_timeout(tool_name: str, tool_args: dict) -> int:
    if tool_name in _MAIN_THREAD_TIMEOUTS:
        return _MAIN_THREAD_TIMEOUTS[tool_name]
    if tool_name not in _STEPPED_TOOLS:
        return _MAIN_THREAD_TIMEOUT_DEFAULT
    n = _coerce_steps((tool_args or {}).get("steps"))
    return max(_MAIN_THREAD_TIMEOUT_DEFAULT, _MAIN_THREAD_TIMEOUT_PER_STEP * n)


def _bind_execute_runtime() -> None:
    from classes.agent_tools.execute import bind_runtime

    bind_runtime(
        handlers=TOOL_HANDLERS,
        read_only=READ_ONLY_TOOLS,
        background_safe=BACKGROUND_SAFE_TOOLS,
        ungrouped=_UNGROUPED_TOOLS,
        main_thread_timeouts=_MAIN_THREAD_TIMEOUTS,
        get_app=_get_app,
        run_on_main_thread=_run_on_main_thread,
        atomic=_atomic,
        coerce_steps=_coerce_steps,
        qthread=QThread,
        main_thread_timeout=_main_thread_timeout,
    )


def execute_tool(tool_name: str, tool_args: dict) -> str:
    """Execute a tool by name. Returns a contract-3 JSON receipt string."""
    return execute_tool_rich(tool_name, tool_args).receipt.to_json()


def execute_tool_rich(tool_name: str, tool_args: dict):
    """Execute a tool; return ToolOutput (receipt + optional images)."""
    from classes.agent_tools.execute import execute_tool_rich as _dispatch
    from classes.editor_tools import prepare_args

    _bind_execute_runtime()
    # Editor tools accept what models send ("1.5", "true"): coerce before the strict
    # validation, and refuse a value that cannot be in the registry's own words.
    args, problem = prepare_args(tool_name, tool_args or {})
    if problem:
        from classes.agent_tools.output import ToolOutput
        from classes.agent_tools.receipt import ToolReceipt
        return ToolOutput(receipt=ToolReceipt.refused(tool_name, problem))
    return _dispatch(tool_name, args)
