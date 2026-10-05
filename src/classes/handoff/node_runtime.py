"""Find Node.js for the code-video handoffs (Remotion, HyperFrames) and run it safely.

Zenvi does not ship Node. :func:`find_node` looks, in order, at

1. ``ZENVI_NODE`` (a node executable, or the folder holding it),
2. Zenvi's private copy that the local HyperFrames setup installs
   (``~/.openshot_qt/hyperframes/node``),
3. ``PATH``,
4. the places installers put node that a GUI app's minimal PATH misses:
   ``/opt/homebrew/bin``, ``/usr/local/bin``, ``~/.volta/bin``, the newest
   ``~/.nvm/versions/node/*/bin``, fnm's default alias, ``%APPDATA%\\npm``
   and ``%ProgramFiles%\\nodejs``,

and takes the first one whose ``node --version`` is at least *min_major*.
npm and npx are returned as argv prefixes (``[node, .../npm-cli.js]`` when
the JS entry sits next to node, so Windows never needs a ``.cmd`` shim or a
shell).

:func:`run_node` runs a command to completion, streaming its output lines,
and kills the whole process tree on cancel or timeout. It blocks: call it
only off the GUI thread (``handoff.jobs``). No console window on Windows.
"""

from __future__ import annotations

import collections
import glob
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from classes.handoff.jobs import JobCancelled
from classes.logger import log

DEFAULT_MIN_MAJOR = 18
VERSION_TIMEOUT = 10.0
TAIL_LINES = 200
KILL_GRACE_SECONDS = 3.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

INSTALL_GUIDANCE = (
    "Install Node.js {major} or newer (https://nodejs.org, or `brew install node` on macOS), then try again. "
    "If it is installed somewhere Zenvi does not look, set the ZENVI_NODE environment variable to the node "
    "executable and restart Zenvi."
)


class NodeNotFound(RuntimeError):
    """No usable Node.js; the message says how to install one."""

    def __init__(self, message: str, searched: Sequence[str] = ()):
        super().__init__(message)
        self.searched = list(searched)


class NodeRunError(RuntimeError):
    """A node command did not finish normally. ``tail`` holds its last output lines."""

    def __init__(self, message: str, tail: str = ""):
        super().__init__(message)
        self.tail = tail


class NodeTimeout(NodeRunError):
    pass


class NodeCancelled(NodeRunError, JobCancelled):
    """The caller cancelled; also a ``jobs.JobCancelled``, so callers treat it as a cancel, not a failure."""


@dataclass(frozen=True)
class NodeRuntime:
    """A usable Node.js: the executable, npm/npx argv prefixes and the version (``"22.11.0"``)."""

    node: str
    npm: Tuple[str, ...]
    npx: Tuple[str, ...]
    version: str

    @property
    def major(self) -> int:
        m = _VERSION_RE.match(self.version)
        return int(m.group(1)) if m else 0

    @property
    def bin_dir(self) -> str:
        return os.path.dirname(self.node)

    def env(self, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """*base* (default: this process's environment) with node's folder first on PATH."""
        env = dict(os.environ if base is None else base)
        path = env.get("PATH", "")
        parts = path.split(os.pathsep) if path else []
        if self.bin_dir and self.bin_dir not in parts:
            env["PATH"] = os.pathsep.join([self.bin_dir] + parts)
        return env

    def npm_argv(self, *args: str) -> List[str]:
        return list(self.npm) + list(args)

    def npx_argv(self, *args: str) -> List[str]:
        return list(self.npx) + list(args)

    def node_argv(self, *args: str) -> List[str]:
        return [self.node] + list(args)


def _exe(name: str, platform: str) -> str:
    return name + ".exe" if platform == "win32" else name


def _home(env: Mapping[str, str]) -> str:
    return env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")


def private_node_path(platform: Optional[str] = None, user_path: Optional[str] = None) -> str:
    """Where Zenvi's local HyperFrames setup keeps its portable Node.js."""
    platform = platform or sys.platform
    if user_path is None:
        from classes import info
        user_path = info.USER_PATH
    base = os.path.join(user_path, "hyperframes", "node")
    return os.path.join(base, "node.exe") if platform == "win32" else os.path.join(base, "bin", "node")


def _version_key(path: str) -> Tuple[int, int, int]:
    m = _VERSION_RE.search(os.path.basename(path.rstrip("/\\")))
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)  # type: ignore[return-value]


def candidate_paths(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
                    user_path: Optional[str] = None) -> List[str]:
    """Node executables to try, in search order (existence not checked)."""
    env = dict(os.environ if env is None else env)
    platform = platform or sys.platform
    node = _exe("node", platform)
    home = _home(env)
    out: List[str] = []

    override = (env.get("ZENVI_NODE") or "").strip()
    if override:
        override = os.path.expanduser(override)
        out.append(os.path.join(override, node) if os.path.isdir(override) else override)
    out.append(private_node_path(platform, user_path))
    # every node on PATH, in PATH order (a too-old one first must not hide a newer one later)
    for folder in (env.get("PATH") or "").split(os.pathsep):
        if folder.strip():
            out.append(os.path.join(os.path.expanduser(folder.strip().strip('"')), node))

    if platform == "win32":
        appdata = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        out.append(os.path.join(appdata, "npm", node))
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = env.get(key)
            if root:
                out.append(os.path.join(root, "nodejs", node))
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        out.append(os.path.join(local, "Volta", "bin", node))
        out.append(os.path.join(local, "fnm_multishells", node))
        nvm_home = env.get("NVM_SYMLINK")
        if nvm_home:
            out.append(os.path.join(nvm_home, node))
    else:
        out += ["/opt/homebrew/bin/node", "/usr/local/bin/node", os.path.join(home, ".volta", "bin", "node")]
        nvm_dir = env.get("NVM_DIR") or os.path.join(home, ".nvm")
        versions = sorted(glob.glob(os.path.join(nvm_dir, "versions", "node", "v*")), key=_version_key,
                          reverse=True)
        out += [os.path.join(v, "bin", "node") for v in versions]
        fnm_dirs = [env.get("FNM_DIR") or "", os.path.join(home, ".fnm"),
                    os.path.join(home, "Library", "Application Support", "fnm"),
                    os.path.join(home, ".local", "share", "fnm")]
        for d in fnm_dirs:
            if d:
                out.append(os.path.join(d, "aliases", "default", "bin", "node"))
        out += ["/usr/bin/node", "/snap/bin/node"]
    seen, unique = set(), []
    for p in out:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _popen_kwargs(platform: Optional[str] = None) -> dict:
    """A new process group (so a cancel can kill the tree) and no console window on Windows."""
    if (platform or sys.platform) == "win32":
        kwargs: dict = {"creationflags": _NO_WINDOW | _NEW_GROUP}
        startupinfo = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo is not None:
            si = startupinfo()
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            si.wShowWindow = 0
            kwargs["startupinfo"] = si
        return kwargs
    return {"start_new_session": True}


def node_version(node: str, timeout: float = VERSION_TIMEOUT) -> Optional[str]:
    """``node --version`` without the ``v`` ("22.11.0"), or None when it does not run."""
    try:
        proc = subprocess.run([node, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, **_popen_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not m:
        return None
    return "%s.%s.%s" % m.groups()


def _npm_tool(node: str, name: str, platform: str) -> Tuple[str, ...]:
    base = os.path.dirname(node)
    for root in (os.path.join(base, "node_modules", "npm", "bin"),
                 os.path.join(base, "..", "lib", "node_modules", "npm", "bin")):
        script = os.path.join(root, "%s-cli.js" % name)
        if os.path.isfile(script):
            return (node, os.path.normpath(script))
    if platform != "win32":
        sibling = os.path.join(base, name)
        if os.path.isfile(sibling):
            return (sibling,)
    found = shutil.which(name)
    return (found,) if found else (name,)


_CACHE: Dict[int, NodeRuntime] = {}
_CACHE_LOCK = threading.Lock()


def find_node(min_major: int = DEFAULT_MIN_MAJOR, *, env: Optional[Mapping[str, str]] = None,
              refresh: bool = False, platform: Optional[str] = None,
              user_path: Optional[str] = None) -> NodeRuntime:
    """The first Node.js >= *min_major* in search order; NodeNotFound with install guidance otherwise.

    Runs ``node --version`` for each candidate: call off the GUI thread. The
    answer is cached per *min_major* for the session; *refresh* re-scans
    (after the user installed Node).
    """
    platform = platform or sys.platform
    cacheable = env is None and user_path is None and platform == sys.platform
    if cacheable and not refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(min_major)
        if hit and os.path.isfile(hit.node):
            return hit
    searched, too_old = [], []
    for path in candidate_paths(env, platform, user_path):
        if not os.path.isfile(path):
            continue
        searched.append(path)
        version = node_version(path)
        if not version:
            continue
        major = int(version.split(".", 1)[0])
        if major < min_major:
            too_old.append("%s (%s)" % (path, version))
            continue
        runtime = NodeRuntime(node=os.path.abspath(path), npm=_npm_tool(path, "npm", platform),
                              npx=_npm_tool(path, "npx", platform), version=version)
        if cacheable:
            with _CACHE_LOCK:
                _CACHE[min_major] = runtime
        log.info("Handoff: using Node.js %s at %s", version, runtime.node)
        return runtime
    message = "Node.js %d or newer was not found." % min_major
    if too_old:
        message += " Too old: %s." % ", ".join(too_old)
    raise NodeNotFound(message + " " + INSTALL_GUIDANCE.format(major=min_major), searched)


def kill_process_tree(proc: "subprocess.Popen", platform: Optional[str] = None) -> None:
    """Stop *proc* and everything it started (its process group / Windows job tree)."""
    if proc.poll() is not None:
        return
    platform = platform or sys.platform
    try:
        if platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=15, creationflags=_NO_WINDOW)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + KILL_GRACE_SECONDS
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except (OSError, subprocess.SubprocessError):
        log.debug("process tree kill failed; killing the parent only", exc_info=True)
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        log.warning("node process %s did not exit after kill", proc.pid)


def run_node(argv: Sequence[str], cwd: Optional[str], env: Optional[Mapping[str, str]] = None,
             timeout: Optional[float] = None, on_line: Optional[Callable[[str], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None, *,
             runtime: Optional[NodeRuntime] = None) -> Tuple[int, str]:
    """Run *argv* (e.g. ``runtime.npx_argv("remotion", "render", ...)``) to completion.

    Returns ``(exit code, tail)`` -- the last output lines (stdout and stderr
    merged). *on_line* gets every line as it arrives (on this worker thread).
    Raises NodeTimeout / NodeCancelled after killing the process tree, and
    NodeRunError when the command cannot start. The child's environment is
    *env* (default: this process's) with the node folder first on PATH.
    Blocking: never call on the GUI thread.
    """
    if runtime is None and argv and os.path.isabs(str(argv[0])):
        # [node, script] / an absolute npx: the program's own folder goes first on PATH
        runtime = NodeRuntime(node=str(argv[0]), npm=(), npx=(), version="")
    child_env = runtime.env(env) if runtime is not None else dict(os.environ if env is None else env)
    tail: collections.deque = collections.deque(maxlen=TAIL_LINES)
    try:
        proc = subprocess.Popen(list(argv), cwd=cwd or None, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **_popen_kwargs())
    except OSError as exc:
        raise NodeRunError("could not start %s: %s" % (argv[0] if argv else "?", exc)) from None

    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.put(raw.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    threading.Thread(target=_reader, name="handoff-node-output", daemon=True).start()
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    finished_reading = False
    try:
        while True:
            if should_cancel is not None and should_cancel():
                kill_process_tree(proc)
                raise NodeCancelled("cancelled", "\n".join(tail))
            if deadline is not None and time.monotonic() > deadline:
                kill_process_tree(proc)
                raise NodeTimeout("did not finish within %d s" % int(timeout or 0), "\n".join(tail))
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if finished_reading and proc.poll() is not None:
                    break
                continue
            if line is None:
                finished_reading = True
                if proc.poll() is not None:
                    break
                continue
            tail.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    log.debug("on_line callback failed", exc_info=True)
        code = proc.wait()
        return code, "\n".join(tail)
    finally:
        if proc.poll() is None:
            kill_process_tree(proc)
