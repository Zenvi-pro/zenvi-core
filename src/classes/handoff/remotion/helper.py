"""Run ``helper.mjs`` -- the project's own Remotion, driven from Zenvi -- and read what it reports.

``helper.mjs`` ships next to this module (package data; ``setup.py`` and
``freeze.py`` copy every file under ``src/``). It prints one JSON object
per line prefixed with ``@@zenvi `` (``hello``, ``progress``, ``warning``,
``result``, ``error``); everything else on its output is Remotion's own
logging, kept as the tail for error reports.

:func:`run_helper` blocks until the command ends: call it off the GUI
thread (a ``handoff.jobs`` job or a background-safe tool). It maps the
helper's stages onto one 0..1 progress, honours ``should_cancel`` (the
process tree is killed) and turns failures into :class:`HelperError`
(a ``LinkError``) whose message says what to do.

Bundles are cached in ``~/.openshot_qt/cache/remotion/bundles/<key>``
(``key`` = a fingerprint of the project's sources, entry and Remotion
version), so the second render of an unchanged project skips webpack.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from classes.handoff import node_runtime
from classes.handoff.linked_media import LinkError, fingerprint_value
from classes.logger import log

# A real path: helper.mjs runs main() only when Node's argv[1] is the file itself (it compares real
# paths too, but resolving here keeps the argv honest for logs and error reports).
HELPER_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "helper.mjs")
HELPER_VERSION = 1
EVENT_PREFIX = "@@zenvi "
MIN_NODE_MAJOR = 18
COMMANDS = ("probe", "compositions", "render", "still")
TIMEOUTS = {"probe": 120.0, "compositions": 1200.0, "still": 1200.0, "render": 6 * 3600.0}
# Each bundle is ~50 MB of webpack output; on Windows Remotion cannot symlink public/ into it, so
# every cached bundle also holds a copy of public/ -- keep fewer there.
BUNDLE_CACHE_KEEP = 2 if sys.platform == "win32" else 6
BUNDLE_MIN_IDLE_SECONDS = 3600.0
MESSAGE_LIMIT = 700

# Where each helper stage sits in the command's overall progress.
STAGES: Dict[str, Dict[str, Tuple[float, float]]] = {
    "probe": {"config": (0.0, 1.0)},
    "compositions": {"config": (0.0, 0.03), "bundling": (0.03, 0.75), "browser": (0.75, 0.82),
                     "compositions": (0.82, 1.0)},
    "still": {"config": (0.0, 0.02), "bundling": (0.02, 0.6), "browser": (0.6, 0.68), "stills": (0.68, 1.0)},
    "render": {"config": (0.0, 0.01), "bundling": (0.01, 0.2), "browser": (0.2, 0.24), "rendering": (0.24, 0.97),
               "encoding": (0.97, 1.0)},
}

ProgressFn = Callable[[Optional[float], str], None]
CancelFn = Callable[[], bool]


class HelperError(LinkError):
    """The helper failed; ``code`` is its error code, ``tail`` Remotion's last output."""

    def __init__(self, message: str, code: str = "FAILED", tail: str = ""):
        super().__init__(message)
        self.code = code
        self.tail = tail


@dataclass
class HelperRun:
    """A finished helper command."""

    result: dict
    warnings: List[str] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    tail: str = ""
    seconds: float = 0.0
    remotion_version: Optional[str] = None


def helper_path() -> str:
    """helper.mjs as a real path (symlinked install folders resolved)."""
    path = os.path.realpath(HELPER_FILE)
    if not os.path.isfile(path):
        raise LinkError(f"this Zenvi build is missing {HELPER_FILE}; reinstall Zenvi")
    return path


def cache_root() -> str:
    """``~/.openshot_qt/cache/remotion`` (bundles, studio logs)."""
    from classes import info
    return os.path.join(info.USER_PATH, "cache", "remotion")


def bundle_dir(project_root: str, entry: str, sources_fingerprint: str, remotion_version: Optional[str]) -> str:
    """The cache folder for a bundle of these sources (it may not exist yet)."""
    key = fingerprint_value({"root": os.path.abspath(project_root), "entry": entry, "sources": sources_fingerprint,
                             "remotion": remotion_version, "helper": HELPER_VERSION})
    return os.path.join(cache_root(), "bundles", key.split(":", 1)[-1][:16])


def prune_bundles(keep: int = BUNDLE_CACHE_KEEP, *, now: Optional[float] = None,
                  min_idle: float = BUNDLE_MIN_IDLE_SECONDS, root: Optional[str] = None) -> List[str]:
    """Delete cached bundles beyond the *keep* most recently used ones (never one used in the last hour).

    Bundles hold ~50 MB of webpack output each. Leftover ``.partial-*``
    folders of a crashed bundling are removed once idle. Returns the removed
    folders. Blocking disk work.
    """
    folder = os.path.join(root or cache_root(), "bundles")
    if not os.path.isdir(folder):
        return []
    now = time.time() if now is None else now
    entries = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        entries.append((mtime, name, path))
    entries.sort(reverse=True)
    removed = []
    kept = 0
    for mtime, name, path in entries:
        idle = now - mtime >= min_idle
        if ".partial-" in name:
            if idle:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
            continue
        kept += 1
        if kept > keep and idle:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


# What Remotion starts and may leave behind when Node dies first: its headless Chrome (in the
# project's node_modules/.remotion browser cache) and its compositor binary. Never Node itself --
# that could be the user's own `npx remotion studio`.
_ORPHAN_MARKERS = (os.path.join(".remotion", "chrome-headless-shell"), os.path.join(".remotion", "chrome-for-testing"),
                   os.path.join("@remotion", "compositor-"))
# Processes that adopt orphans: init / launchd (pid 1) and subreapers such as `systemd --user`.
_REAPERS = ("init", "launchd", "systemd")


def orphan_pids(project_root: str, ps_output: Optional[str] = None) -> List[int]:
    """Orphaned processes started from *project_root*'s Remotion: headless Chrome, the compositor.

    Remotion starts Chrome in its own process group and kills it from
    Node's SIGTERM/exit handlers; if Node is SIGKILLed first (a stuck
    cancel), Chrome survives, re-parented to init/launchd or a subreaper
    (``systemd --user``). Matched by the executable living in the project's
    ``node_modules/.remotion`` browser cache or being the ``@remotion``
    compositor. POSIX only.
    """
    roots = {os.path.join(os.path.abspath(project_root), "node_modules")}
    roots.add(os.path.realpath(next(iter(roots))))
    markers = [os.path.join(r, m) for r in sorted(roots) for m in _ORPHAN_MARKERS]
    if ps_output is None:
        if sys.platform == "win32":
            return []
        try:
            ps_output = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                                       timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    rows = []
    commands: Dict[int, str] = {}
    for line in (ps_output or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        rows.append((pid, ppid, parts[2]))
        commands[pid] = parts[2]

    def _reaper(ppid: int) -> bool:
        if ppid == 1:
            return True
        exe = os.path.basename(commands.get(ppid, "").split(" ", 1)[0]).lstrip("-")
        return exe in _REAPERS

    return [pid for pid, ppid, command in rows if _reaper(ppid) and any(command.startswith(m) for m in markers)]


def reap_orphans(project_root: str) -> List[int]:
    """Kill orphaned Remotion Chrome/compositor processes of *project_root* (after a cancel or failure)."""
    killed = []
    for pid in orphan_pids(project_root):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            killed.append(pid)
        except OSError:
            continue
    if killed:
        log.warning("Killed %d orphaned Remotion browser process(es) of %s: %s", len(killed), project_root, killed)
    return killed


def default_concurrency() -> int:
    """Render concurrency: ``ZENVI_REMOTION_CONCURRENCY`` or half the cores (1-4), so editing stays smooth."""
    raw = os.environ.get("ZENVI_REMOTION_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def build_argv(node: str, command: str, *, project_dir: str, entry: Optional[str] = None,
               composition: Optional[str] = None, codec: Optional[str] = None, output: Optional[str] = None,
               out_dir: Optional[str] = None, props_file: Optional[str] = None, frames: Optional[str] = None,
               concurrency: Optional[int] = None, bundle: Optional[str] = None) -> List[str]:
    """``[node, helper.mjs, command, --project, ..]`` (values as separate argv items: no shell, no quoting)."""
    if command not in COMMANDS:
        raise ValueError(f"unknown helper command {command!r}")
    argv = [node, helper_path(), command, "--project", os.path.abspath(project_dir)]
    pairs = (("entry", entry), ("composition", composition), ("codec", codec), ("output", output),
             ("out-dir", out_dir), ("props", props_file), ("frames", frames),
             ("concurrency", str(int(concurrency)) if concurrency else None), ("bundle-dir", bundle))
    for key, value in pairs:
        if value is None or value == "":
            continue
        text = str(value)
        if text.startswith("--"):
            raise ValueError(f"--{key} value must not start with '--': {text!r}")
        argv += ["--" + key, text]
    return argv


def parse_event(line: str) -> Optional[dict]:
    """The JSON event on a ``@@zenvi `` line, else None (Remotion's own output).

    The event may follow other output on the same line: something in the
    project that writes to stdout without a newline must not swallow it.
    """
    at = line.find(EVENT_PREFIX)
    if at < 0:
        return None
    try:
        event = json.loads(line[at + len(EVENT_PREFIX):])
    except ValueError:
        return None
    return event if isinstance(event, dict) and isinstance(event.get("event"), str) else None


def overall_progress(command: str, stage: str, fraction: Optional[float]) -> Optional[float]:
    """A stage's fraction mapped into the command's 0..1 progress (None stays None for unknown stages)."""
    span = STAGES.get(command, {}).get(stage)
    if span is None:
        return None
    lo, hi = span
    if fraction is None:
        return lo
    return lo + (hi - lo) * max(0.0, min(1.0, float(fraction)))


def _first_lines(text: str, count: int = 3) -> str:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return " ".join(lines[:count])[:MESSAGE_LIMIT]


def error_message(event: Optional[dict], tail: str, command: str, code: int) -> Tuple[str, str]:
    """(message for the user, error code) from the helper's error event or its output tail."""
    if event:
        err_code = str(event.get("code") or "FAILED")
        message = _first_lines(event.get("message") or "", 4)
        logs = [str(x) for x in (event.get("browserLogs") or []) if x]
        if logs and err_code not in ("MISSING_DEPENDENCY", "INVALID_ARGUMENT", "NOT_FOUND", "NO_ENTRY"):
            message += " (browser console: " + _first_lines(logs[0], 1)[:200] + ")"
        return message or f"Remotion {command} failed (exit {code})", err_code
    last = _first_lines("\n".join(str(tail or "").splitlines()[-6:]), 6)
    return (f"Remotion {command} failed (exit {code})" + (f": {last}" if last else "")), "FAILED"


def run_helper(command: str, *, project_dir: str, entry: Optional[str] = None, props: Optional[dict] = None,
               options: Optional[Dict[str, Any]] = None, on_progress: Optional[ProgressFn] = None,
               should_cancel: Optional[CancelFn] = None, timeout: Optional[float] = None,
               runtime: Optional[node_runtime.NodeRuntime] = None) -> HelperRun:
    """Run one helper command to completion and return its ``result`` event.

    *props* are written to a temporary JSON file (``--props``); *options*
    are the other helper flags (``composition``, ``codec``, ``output``,
    ``out_dir``, ``frames``, ``concurrency``, ``bundle``). Raises HelperError
    (LinkError), ``jobs.JobCancelled`` when cancelled, and LinkError with
    install guidance when Node.js is missing. Blocking.
    """
    from classes.handoff.jobs import JobCancelled
    try:
        runtime = runtime or node_runtime.find_node(MIN_NODE_MAJOR)
    except node_runtime.NodeNotFound as exc:
        raise LinkError(f"Remotion needs Node.js: {exc}") from None
    opts = dict(options or {})
    tmp = tempfile.mkdtemp(prefix="zenvi-remotion-")
    started = time.monotonic()
    events: List[dict] = []
    warnings: List[str] = []
    state: Dict[str, Any] = {"result": None, "error": None, "remotion": None}
    report = on_progress or (lambda _f, _m: None)

    def _on_line(line: str) -> None:
        event = parse_event(line)
        if event is None:
            return
        kind = event.get("event")
        if kind == "progress":
            value = event.get("progress")
            fraction = overall_progress(command, str(event.get("stage") or ""),
                                        float(value) if isinstance(value, (int, float)) else None)
            report(fraction, str(event.get("message") or ""))
        elif kind == "warning":
            warnings.append(str(event.get("message") or ""))
        elif kind == "hello":
            state["remotion"] = event.get("remotion")
        elif kind == "result":
            state["result"] = event
        elif kind == "error":
            state["error"] = event
        if kind != "progress":
            events.append(event)

    try:
        props_file = None
        if props is not None:
            props_file = os.path.join(tmp, "props.json")
            with open(props_file, "w", encoding="utf-8") as fh:
                json.dump(props, fh, ensure_ascii=False)
        argv = build_argv(runtime.node, command, project_dir=project_dir, entry=entry, props_file=props_file,
                          composition=opts.get("composition"), codec=opts.get("codec"), output=opts.get("output"),
                          out_dir=opts.get("out_dir"), frames=opts.get("frames"),
                          concurrency=opts.get("concurrency"), bundle=opts.get("bundle"))
        env = runtime.env()
        env.setdefault("BROWSER", "none")
        try:
            code, tail = node_runtime.run_node(argv, cwd=project_dir, env=env, on_line=_on_line,
                                               should_cancel=should_cancel,
                                               timeout=timeout if timeout is not None else TIMEOUTS.get(command),
                                               runtime=runtime)
        except node_runtime.NodeCancelled:
            reap_orphans(project_dir)
            raise JobCancelled(f"Remotion {command} cancelled") from None
        except node_runtime.NodeTimeout as exc:
            reap_orphans(project_dir)
            raise HelperError(f"Remotion {command} did not finish in time ({exc}); try again, or render fewer "
                              "frames", "TIMEOUT", exc.tail) from None
        except node_runtime.NodeRunError as exc:
            if should_cancel is not None and should_cancel():
                raise JobCancelled(f"Remotion {command} cancelled") from None
            raise HelperError(f"could not start Node.js for Remotion: {exc}", "FAILED", exc.tail) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    cancelled = should_cancel is not None and should_cancel()
    if code == 130 or cancelled or (state["error"] or {}).get("code") == "CANCELLED":
        if code != 0:
            reap_orphans(project_dir)
        raise JobCancelled(f"Remotion {command} cancelled")
    if code != 0 or state["result"] is None:
        reap_orphans(project_dir)
        message, err_code = error_message(state["error"], tail, command, code)
        if code == 0 and state["error"] is None and not events:
            message = (f"Zenvi's Remotion helper ended without reporting anything (exit 0) for {command}; "
                       f"reinstall Zenvi if this keeps happening ({HELPER_FILE})")
            err_code = "NO_OUTPUT"
        log.warning("Remotion helper %s failed (exit %s, %s): %s\n%s", command, code, err_code, message,
                    "\n".join(tail.splitlines()[-40:]))
        raise HelperError(message, err_code, tail)
    result = dict(state["result"])
    result.pop("event", None)
    warnings += [str(w) for w in (result.get("warnings") or []) if w]
    return HelperRun(result=result, warnings=warnings, events=events, tail=tail,
                     seconds=round(time.monotonic() - started, 2), remotion_version=state["remotion"])


__all__ = ["HelperError", "HelperRun", "run_helper", "build_argv", "parse_event", "overall_progress",
           "error_message", "bundle_dir", "prune_bundles", "cache_root", "helper_path", "default_concurrency",
           "orphan_pids", "reap_orphans",
           "HELPER_FILE", "HELPER_VERSION", "EVENT_PREFIX"]
