"""Tools that take tens of seconds run in the background, so a chat turn answers with a note instead of waiting."""

import json
import threading
import time

# Tools that take tens of seconds and place their own result, so the turn need not wait for them,
# with the words the next turn's note uses: the agent reaches most of them through another tool's name.
BACKGROUND_TOOLS = {
    "generate_video_and_add_to_timeline_tool": "AI video",
    "generate_image_and_add_to_timeline_tool": "AI image",
    "export_video_tool": "export",
    "hyperframes_setup_tool": "HyperFrames setup",
    "add_animated_title_tool": "animated title",
}
GRACE_SECONDS = 8.0
BACKGROUND_PREFIX = "Running in the background"
_RESULT_CHARS = 4000  # a whole receipt; the next turn needs its ids and paths

_lock = threading.Lock()
_jobs = {}  # (session, name, args json) -> _Job


class _Job:
    def __init__(self, session, name, args):
        self.session = session
        self.name = name
        self.args = args
        self.started = time.monotonic()
        self.result = None
        self.detached = False
        self.done = threading.Event()


def _work(job, call):
    try:
        job.result = call()
    except Exception as exc:  # noqa: BLE001
        from classes.logger import log

        log.error("Background tool %s failed: %s", job.name, exc)
        job.result = f"Error: tool execution failed: {exc}"
    job.done.set()


def run(name, args, call, session=""):
    """Run *call*; a tool in BACKGROUND_TOOLS answers with a note after GRACE_SECONDS and keeps going."""
    if name not in BACKGROUND_TOOLS:
        return call()
    key = (session or "", name, json.dumps(args, sort_keys=True, default=str))
    with _lock:
        job = _jobs.get(key)
        fresh = job is None
        if fresh:
            job = _jobs[key] = _Job(session or "", name, args)
    if fresh:
        # ponytail: detached jobs are uncapped (the four-slot limit no longer covers them); cap them if many pile up.
        threading.Thread(target=_work, args=(job, call), daemon=True, name="zenvi-background-job").start()
    # A repeat call of a running job waits for it: that is how the model asks for the result.
    job.done.wait(GRACE_SECONDS if fresh else None)
    with _lock:
        if job.done.is_set():
            # A detached job stays until turn_notes() reports it: this answer may never reach the
            # model (Stop, a dropped socket), and the result must not be lost with it.
            if not job.detached and _jobs.get(key) is job:
                del _jobs[key]
            return job.result
        job.detached = True
    return (
        f"{BACKGROUND_PREFIX}: this call has not finished after {GRACE_SECONDS:g} s and keeps going on its own; "
        "its result is placed in the project when it is done. Carry on with anything that does not need it "
        "and tell the user it is still running. If your next step needs its result, make the same call again "
        "with the same arguments to wait for it."
    )


def turn_notes(session=""):
    """What *session*'s background calls did since its last message: a block for the next one, or ''."""
    lines = []
    with _lock:
        for key, job in list(_jobs.items()):
            if not job.detached or job.session != (session or ""):
                continue
            head = " ".join(part for part in (BACKGROUND_TOOLS[job.name], _args_summary(job.args)) if part)
            if job.done.is_set():
                lines.append(f"- {head}: finished: {_one_line(str(job.result or '')[:_RESULT_CHARS])}")
                del _jobs[key]
            else:
                lines.append(f"- {head}: still running ({int(time.monotonic() - job.started)} s)")
    if not lines:
        return ""
    head = "[Background jobs] Calls from earlier turns. A finished one is done: use its result, do not make the call again."
    return "\n".join([head, *lines, "[/Background jobs]"])


def reset():
    with _lock:
        _jobs.clear()


def _args_summary(args):
    if isinstance(args, dict):
        for key in ("prompt", "text", "file_name", "preset", "project"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return '"' + _one_line(value[:80]) + '"'
    return ""


def _one_line(text):
    return " ".join(text.splitlines())
