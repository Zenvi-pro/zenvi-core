"""Remotion Studio for linked clips ("Open in Studio").

``open_studio`` starts the project's own ``remotion studio <entry> --port
<free> --no-open`` (one per project, reused while it runs; two requests at
once start one), waits until that Studio answers -- Remotion's own
``/__remotion_config`` names the project, so another server on the port is
never mistaken for it -- and opens ``http://localhost:<port>/<compositionId>``
in the browser. Studios Zenvi started are stopped when Zenvi quits
(``aboutToQuit`` and ``atexit``) and, through a preloaded watchdog
(``studio_watchdog.cjs``), when Zenvi dies without quitting; ``stop_studio`` /
``stop_all`` stop them on request.

Blocking (starting Node, waiting for the port): call off the GUI thread.
Studio output goes to ``~/.openshot_qt/cache/remotion/studio-<name>.log``.
"""

from __future__ import annotations

import atexit
import errno
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from classes.handoff import node_runtime
from classes.handoff.linked_media import LinkError
from classes.handoff.remotion import detect
from classes.logger import log

STARTUP_TIMEOUT = 120.0
FIRST_PORT = 3000
PORT_ATTEMPTS = 200
_HOSTS = ("127.0.0.1", "localhost", "::1")
WATCHDOG_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "studio_watchdog.cjs")


@dataclass
class StudioProcess:
    project_dir: str
    entry: str
    port: int
    proc: subprocess.Popen
    log_path: str
    started_at: float

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def url(self, composition: Optional[str] = None) -> str:
        base = f"http://localhost:{self.port}"
        return f"{base}/{quote(composition, safe='')}" if composition else base + "/"


_lock = threading.Lock()
_studios: Dict[str, StudioProcess] = {}
_starting: Dict[str, threading.Lock] = {}  # project -> held while a Studio for it starts
_hooks = {"atexit": False, "qt": False}


def _bindable(family: int, host: str, port: int) -> bool:
    """Could a server bind *host*:*port*? (No such stack or address here counts as free: nothing listens.)"""
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return True
    with sock:
        try:
            if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((host, port))
        except OSError as exc:
            return exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)
    return True


def port_is_free(port: int) -> bool:
    """Nothing listens on *port* on loopback or any interface (Remotion Studio binds 0.0.0.0 and ::)."""
    return all(_bindable(family, host, port) for family, host in (
        (socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::1"), (socket.AF_INET6, "::")))


def free_port(start: int = FIRST_PORT, attempts: int = PORT_ATTEMPTS) -> int:
    """A TCP port nothing listens on, on any interface (from *start* up, like Remotion's own choice)."""
    for port in range(start, start + attempts):
        if port_is_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


def studio_argv(runtime: node_runtime.NodeRuntime, project: detect.RemotionProject, port: int) -> List[str]:
    """The project's own Remotion CLI: ``node --require <watchdog> .../remotion-cli.js studio <entry> --port N --no-open``.

    Without a resolvable ``@remotion/cli`` script it falls back to npx (the
    watchdog then comes through ``NODE_OPTIONS``, see :func:`studio_env`).
    """
    cli_dir = detect.package_dir(project.root, "@remotion/cli")
    script = os.path.join(cli_dir, "remotion-cli.js") if cli_dir else ""
    args = ["studio", project.entry or "", "--port", str(int(port)), "--no-open"]
    if script and os.path.isfile(script):
        argv = runtime.node_argv(script, *args)
        if os.path.isfile(WATCHDOG_FILE) and argv and os.path.normcase(argv[0]) == os.path.normcase(runtime.node):
            argv = [argv[0], "--require", WATCHDOG_FILE] + argv[1:]
        return argv
    return runtime.npx_argv("remotion", *args)


def studio_env(runtime: node_runtime.NodeRuntime, argv: List[str], parent_pid: Optional[int] = None) -> dict:
    """The Studio's environment: no browser of its own, and the watchdog told which Zenvi to outlive."""
    env = runtime.env()
    env["BROWSER"] = "none"
    env["ZENVI_PARENT_PID"] = str(parent_pid or os.getpid())
    if WATCHDOG_FILE not in argv and os.path.isfile(WATCHDOG_FILE):  # npx: preload through NODE_OPTIONS
        quoted = '"%s"' % WATCHDOG_FILE.replace("\\", "\\\\").replace('"', '\\"')
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --require " + quoted).strip()
    return env


def _same_folder(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(p)).replace("\\", "/").rstrip("/")
    return norm(a) == norm(b)


def _answers(port: int, project_root: Optional[str] = None) -> bool:
    """True once the Remotion Studio of *project_root* answers on *port*.

    Remotion Studio says who it is at ``/__remotion_config``
    (``{"isRemotion": true, "cwd": <project>}``, what ``npx remotion studio``
    itself uses to find a running Studio); a Studio too old to have it must
    at least serve a page that mentions Remotion.
    """
    for host in _HOSTS:
        conn = http.client.HTTPConnection(host, port, timeout=1.0)
        try:
            conn.request("GET", "/__remotion_config")
            res = conn.getresponse()
            body = res.read(65536)
            if res.status == 200:
                try:
                    info = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    return False
                if not isinstance(info, dict) or info.get("isRemotion") is not True:
                    return False
                cwd = info.get("cwd")
                return project_root is None or not isinstance(cwd, str) or _same_folder(cwd, project_root)
            if res.status != 404:
                return False
            conn.close()
            conn = http.client.HTTPConnection(host, port, timeout=1.0)
            conn.request("GET", "/")
            page = conn.getresponse().read(65536)
            return b"remotion" in page.lower()
        except (OSError, http.client.HTTPException):
            continue
        finally:
            conn.close()
    return False


def _log_tail(path: str, lines: int = 12) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()[-20000:]
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return "\n".join(text.strip().splitlines()[-lines:])


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def _install_exit_hooks() -> None:
    if not _hooks["atexit"]:
        atexit.register(stop_all)
        _hooks["atexit"] = True
    if _hooks["qt"]:
        return
    try:
        from classes.qt_main_thread import invoke_on_gui

        def _connect():
            from qt_api import QCoreApplication
            app = QCoreApplication.instance()
            if app is not None and hasattr(app, "aboutToQuit"):
                app.aboutToQuit.connect(stop_all)
                _hooks["qt"] = True

        invoke_on_gui(_connect)
    except Exception:
        log.debug("could not hook Remotion Studio shutdown to aboutToQuit; atexit still stops it", exc_info=True)


def _open_url(url: str) -> None:
    try:
        from classes.qt_main_thread import call_on_gui

        def _open():
            from qt_api import QDesktopServices, QUrl
            return bool(QDesktopServices.openUrl(QUrl(url)))

        if call_on_gui(_open, timeout=15):
            return
    except Exception:
        log.debug("QDesktopServices could not open %s; trying webbrowser", url, exc_info=True)
    import webbrowser
    if not webbrowser.open(url):
        raise LinkError(f"could not open a browser; Remotion Studio is running at {url}")


def open_studio(project: detect.RemotionProject, composition: Optional[str] = None, *, open_browser: bool = True,
                timeout: float = STARTUP_TIMEOUT, should_cancel: Optional[Callable[[], bool]] = None,
                runtime: Optional[node_runtime.NodeRuntime] = None) -> dict:
    """Start (or reuse) Remotion Studio for *project* and open *composition* in the browser. Blocking."""
    if not project.entry:
        raise LinkError(f"{project.name} has no entry point, so Remotion Studio cannot start")
    key = os.path.abspath(project.root)
    with _lock:
        starting = _starting.setdefault(key, threading.Lock())
    with starting:  # a second request for the same project waits for the first Studio, then reuses it
        return _open_studio_locked(project, key, composition, open_browser=open_browser, timeout=timeout,
                                   should_cancel=should_cancel, runtime=runtime)


def _open_studio_locked(project: detect.RemotionProject, key: str, composition: Optional[str], *, open_browser: bool,
                        timeout: float, should_cancel: Optional[Callable[[], bool]],
                        runtime: Optional[node_runtime.NodeRuntime]) -> dict:
    with _lock:
        existing = _studios.get(key)
        if existing is not None and not existing.alive:
            _studios.pop(key, None)
            existing = None
    if existing is not None:
        url = existing.url(composition)
        if open_browser:
            _open_url(url)
        return {"url": url, "port": existing.port, "pid": existing.proc.pid, "reused": True}

    try:
        runtime = runtime or node_runtime.find_node(18)
    except node_runtime.NodeNotFound as exc:
        raise LinkError(f"Remotion Studio needs Node.js: {exc}") from None
    from classes.handoff.remotion.helper import cache_root
    os.makedirs(cache_root(), exist_ok=True)
    port = free_port()
    log_path = os.path.join(cache_root(), "studio-%s.log" % re.sub(r"[^A-Za-z0-9_-]+", "-", project.name)[:40])
    argv = studio_argv(runtime, project, port)
    env = studio_env(runtime, argv)
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(argv, cwd=project.root, env=env, stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, **_popen_kwargs())
    except OSError as exc:
        log_file.close()
        raise LinkError(f"could not start Remotion Studio: {exc}") from None
    finally:
        if not log_file.closed:
            log_file.close()  # the child keeps its own handle
    studio = StudioProcess(project_dir=key, entry=project.entry or "", port=port, proc=proc, log_path=log_path,
                           started_at=time.time())
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            raise LinkError(f"Remotion Studio stopped before it was ready (exit {proc.returncode}): "
                            f"{_log_tail(log_path) or 'no output'}")
        if _answers(port, project.root):
            break
        if should_cancel is not None and should_cancel():
            node_runtime.kill_process_tree(proc)
            from classes.handoff.jobs import JobCancelled
            raise JobCancelled("Remotion Studio start cancelled")
        if time.monotonic() > deadline:
            node_runtime.kill_process_tree(proc)
            raise LinkError(f"Remotion Studio did not answer on port {port} within {int(timeout)} s: "
                            f"{_log_tail(log_path) or 'no output'}")
        time.sleep(0.5)
    with _lock:
        _studios[key] = studio
    _install_exit_hooks()
    log.info("Remotion Studio for %s on port %d (pid %d)", project.root, port, proc.pid)
    url = studio.url(composition)
    if open_browser:
        _open_url(url)
    return {"url": url, "port": port, "pid": proc.pid, "reused": False}


def running_studios() -> List[dict]:
    with _lock:
        items = list(_studios.values())
    return [{"project_dir": s.project_dir, "port": s.port, "pid": s.proc.pid, "url": s.url(), "alive": s.alive}
            for s in items]


def stop_studio(project_dir: str) -> bool:
    with _lock:
        studio = _studios.pop(os.path.abspath(project_dir), None)
    if studio is None:
        return False
    node_runtime.kill_process_tree(studio.proc)
    return True


def stop_all() -> None:
    """Stop every Studio Zenvi started (app exit)."""
    with _lock:
        items = list(_studios.values())
        _studios.clear()
    for studio in items:
        try:
            node_runtime.kill_process_tree(studio.proc)
        except Exception:
            log.debug("stopping Remotion Studio pid %s failed", studio.proc.pid, exc_info=True)


__all__ = ["open_studio", "stop_studio", "stop_all", "running_studios", "studio_argv", "studio_env", "free_port",
           "port_is_free", "StudioProcess", "WATCHDOG_FILE"]
