"""Run the HyperFrames CLI for linked clips: timeline / lint JSON, renders, and the Studio preview.

Which CLI (first match) -- never one the project brings along: a project's
``node_modules`` is its code, and Zenvi runs a project's code only to render
it, after the import dialog's trust note (review C5-1 #8):

1. ``ZENVI_HYPERFRAMES_CLI`` -- a ``hyperframes`` executable, ``bin/hyperframes.mjs``
   or ``dist/cli.js`` the user chose (this is also how to use a project's own install);
2. the local HyperFrames setup's private install (``~/.openshot_qt/hyperframes/node_modules``,
   motion graphics), when it is the version the project pins, or the project pins none and it
   is at least :data:`PINNED_VERSION` (older CLIs may lack the flags Zenvi uses);
3. ``npx --yes hyperframes@<version>`` -- the version the project's package.json
   scripts pin (``npx hyperframes@0.8.126 render``), else :data:`PINNED_VERSION`.

Every command runs in a folder of Zenvi's own (:func:`work_dir`) with the
project folder as its argument, so a project's ``.npmrc`` or ``node_modules``
never applies -- except ``timeline``, which only reads the project from its
working folder (0.8.126 takes a folder argument for a sub-command name): it
runs in the project folder, but as a plain ``node <entry>`` (an npx CLI is
found in npm's cache), so nothing npm reads from there applies. Node.js 22+ comes from
:func:`classes.handoff.node_runtime.find_node`. Every command runs with
HyperFrames' telemetry, update check and self-update off (:data:`QUIET_ENV`):
Zenvi never opts a user in. Everything here blocks -- call it off the GUI
thread.

The Studio (``hyperframes preview``) runs as a child of Zenvi on a free
loopback port; :func:`open_studio` reuses a running one for the same
project, and every Studio Zenvi started is stopped when Zenvi quits.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from classes.handoff import node_runtime
from classes.handoff.linked_media import LinkError
from classes.logger import log

PINNED_VERSION = "0.8.126"
NODE_MIN_MAJOR = 22                 # the CLI's engines.node
QUIET_ENV = {
    "HYPERFRAMES_NO_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "HYPERFRAMES_NO_UPDATE_CHECK": "1",
    "HYPERFRAMES_NO_AUTO_INSTALL": "1",
    "HYPERFRAMES_SKIP_SKILLS": "1",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
}
CLI_ENV_OVERRIDE = "ZENVI_HYPERFRAMES_CLI"
WORKERS_ENV = "ZENVI_HYPERFRAMES_WORKERS"
JSON_TIMEOUT = 180.0                # a first npx run downloads the CLI
RENDER_TIMEOUT = 4 * 60 * 60
STUDIO_READY_TIMEOUT = 120.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PERCENT = re.compile(r"(\d{1,3})%\s+(.*)$")
_PIN = re.compile(r"hyperframes@(\d+\.\d+\.\d+(?:-[\w.]+)?)")


class CliError(LinkError):
    """The HyperFrames CLI could not run or failed; the message says what to do."""


@dataclass(frozen=True)
class Cli:
    """How to run HyperFrames: argv prefix, the version (when known) and where it came from."""

    argv: Tuple[str, ...]
    version: Optional[str]
    source: str                      # env | zenvi | npx | npx-cache (npx's download, run as plain node)
    runtime: node_runtime.NodeRuntime

    def command(self, *args: str) -> List[str]:
        return list(self.argv) + [str(a) for a in args]


def project_pin(project_dir: Optional[str]) -> Optional[str]:
    """The CLI version the project's package.json scripts run (``npx hyperframes@X.Y.Z``), if any."""
    if not project_dir:
        return None
    try:
        with open(os.path.join(project_dir, "package.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    for value in (scripts or {}).values() if isinstance(scripts, dict) else []:
        m = _PIN.search(str(value))
        if m:
            return m.group(1)
    return None


def _at_least(version: Optional[str], minimum: str) -> bool:
    """``version >= minimum`` for plain ``X.Y.Z`` releases (a pre-release or an unreadable version: no)."""
    def parts(text: Optional[str]) -> Optional[Tuple[int, ...]]:
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
        return tuple(int(g) for g in m.groups()) if m else None
    have, need = parts(version), parts(minimum)
    return have is not None and need is not None and have >= need


def _package_version(pkg_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "") or None
    except (OSError, ValueError, AttributeError):
        return None


def _entry_in(pkg_dir: str) -> Optional[str]:
    for rel in (("bin", "hyperframes.mjs"), ("dist", "cli.js")):
        path = os.path.join(pkg_dir, *rel)
        if os.path.isfile(path):
            return path
    return None


def private_install() -> str:
    """The local HyperFrames setup's package folder (motion graphics, ``~/.openshot_qt/hyperframes``)."""
    from classes import info
    return os.path.join(info.USER_PATH, "hyperframes", "node_modules", "hyperframes")


def resolve_cli(project_dir: Optional[str] = None, *, env: Optional[Dict[str, str]] = None) -> Cli:
    """The HyperFrames CLI to run for *project_dir* (see the module doc). Blocking (finds Node)."""
    environ = dict(os.environ if env is None else env)
    try:
        runtime = node_runtime.find_node(NODE_MIN_MAJOR, env=env)
    except node_runtime.NodeNotFound as exc:
        raise CliError(f"HyperFrames needs Node.js {NODE_MIN_MAJOR} or newer. {exc}") from None
    override = (environ.get(CLI_ENV_OVERRIDE) or "").strip()
    if override:
        path = os.path.expanduser(override)
        if os.path.isdir(path):
            entry = _entry_in(path) or _entry_in(os.path.join(path, "node_modules", "hyperframes"))
            if entry is None:
                raise CliError(f"{CLI_ENV_OVERRIDE}={override} holds no HyperFrames CLI")
            path = entry
        if not os.path.isfile(path):
            raise CliError(f"{CLI_ENV_OVERRIDE}={override} does not exist")
        argv = (runtime.node, path) if path.endswith((".js", ".mjs", ".cjs")) else (path,)
        pkg = os.path.dirname(os.path.dirname(os.path.realpath(path)))
        return Cli(argv, _package_version(pkg), "env", runtime)
    pin = project_pin(project_dir)
    private = private_install()
    entry = _entry_in(private)
    if entry:
        version = _package_version(private)
        if pin == version or (pin is None and _at_least(version, PINNED_VERSION)):
            return Cli((runtime.node, entry), version, "zenvi", runtime)
    version = pin or PINNED_VERSION
    return Cli(tuple(runtime.npx_argv("--yes", "hyperframes@%s" % version)), version, "npx", runtime)


def work_dir() -> str:
    """Zenvi's own folder the CLI runs in (not the project: its .npmrc / node_modules must not apply)."""
    from classes import info
    path = os.path.join(info.USER_PATH, "cache", "hyperframes-run")
    os.makedirs(path, exist_ok=True)
    return path


def cli_env(cli: Cli, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = cli.runtime.env()
    env.update(QUIET_ENV)
    if extra:
        env.update(extra)
    return env


def clean_line(line: str) -> str:
    """A CLI output line without ANSI codes and spinner redraws (the last redraw wins)."""
    parts = [p for p in re.split(r"\x1b\[1G|\x1b\[2K|\r", line) if p.strip()]
    text = parts[-1] if parts else line
    return _ANSI.sub("", text).strip()


def run(cli: Cli, args: Sequence[str], *, timeout: Optional[float] = None,
        on_line: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None, cwd: Optional[str] = None) -> Tuple[int, str]:
    """Run ``hyperframes <args>`` in :func:`work_dir` (or *cwd*): (exit code, output tail).

    CliError when it cannot start. Only a direct ``node <entry>`` CLI may run in a project folder.
    """
    if cwd is not None and cli.source == "npx":
        raise CliError("an npx HyperFrames CLI never runs inside a project folder (its .npmrc would apply)")
    try:
        return node_runtime.run_node(cli.command(*args), cwd or work_dir(), env=cli_env(cli), timeout=timeout,
                                     on_line=on_line, should_cancel=should_cancel, runtime=cli.runtime)
    except node_runtime.NodeTimeout:
        raise CliError(f"hyperframes {' '.join(args[:1])} did not finish within {int(timeout or 0)} s") from None
    except node_runtime.NodeCancelled:
        raise
    except node_runtime.NodeRunError as exc:
        raise CliError(f"could not run HyperFrames: {exc}") from None


def _json_from(text: str) -> Optional[dict]:
    decoder = json.JSONDecoder()
    best = None
    for m in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(value, dict) and (best is None or len(json.dumps(value)) > len(json.dumps(best))):
            best = value
    return best


def run_json(cli: Cli, args: Sequence[str], *, timeout: float = JSON_TIMEOUT, cwd: Optional[str] = None,
             should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """``hyperframes <args> --json``, parsed. CliError with the CLI's message on failure."""
    lines: List[str] = []
    code, tail = run(cli, list(args) + ["--json"], timeout=timeout, on_line=lines.append,
                     should_cancel=should_cancel, cwd=cwd)
    text = "\n".join(lines) or tail
    data = _json_from(text)
    if data is None:
        last = "\n".join(clean_line(x) for x in text.splitlines()[-6:])
        raise CliError(f"hyperframes {args[0]} printed no JSON (exit {code}): {last or 'no output'}")
    return data


def _npm_cache(cli: Cli) -> str:
    env = cli_env(cli)
    value = (env.get("npm_config_cache") or env.get("NPM_CONFIG_CACHE") or "").strip()
    if value:
        return os.path.expanduser(value)
    try:
        lines: List[str] = []
        code, tail = node_runtime.run_node(cli.runtime.npm_argv("config", "get", "cache"), work_dir(), env=env,
                                           timeout=60, on_line=lines.append, runtime=cli.runtime)
        text = next((clean_line(x) for x in reversed(lines or tail.splitlines()) if clean_line(x)), "")
        if code == 0 and text and os.path.isdir(text):
            return text
    except (node_runtime.NodeRunError, node_runtime.NodeTimeout, OSError):
        log.debug("npm config get cache failed", exc_info=True)
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "npm-cache")
    return os.path.expanduser("~/.npm")


def _npx_cached_entry(cli: Cli) -> Optional[str]:
    """The entry file of the CLI version *cli* names in npm's npx cache, if it was downloaded."""
    import glob
    for pkg in sorted(glob.glob(os.path.join(_npm_cache(cli), "_npx", "*", "node_modules", "hyperframes"))):
        if _package_version(pkg) == cli.version:
            entry = _entry_in(pkg)
            if entry:
                return entry
    return None


def direct(cli: Cli, *, should_cancel: Optional[Callable[[], bool]] = None) -> Cli:
    """*cli* as a plain ``node <entry>`` command (an npx CLI downloaded first, from :func:`work_dir`)."""
    if cli.source != "npx":
        return cli
    entry = _npx_cached_entry(cli)
    if entry is None:
        run(cli, ["--version"], timeout=JSON_TIMEOUT, should_cancel=should_cancel)  # npx downloads it
        entry = _npx_cached_entry(cli)
    if entry is None:
        raise CliError("could not find the HyperFrames CLI npx downloaded; set ZENVI_HYPERFRAMES_CLI to it")
    return Cli((cli.runtime.node, entry), cli.version, "npx-cache", cli.runtime)


def timeline(project_dir: str, cli: Optional[Cli] = None, **kwargs) -> dict:
    """``hyperframes timeline --json``: the resolved tracks and clips (HyperFrames' own timing).

    0.8.126 reads the project only from its working folder (a folder argument is taken for a
    sub-command), so it runs in *project_dir* -- as plain node, never through npm (:func:`direct`).
    """
    plain = direct(cli or resolve_cli(project_dir), should_cancel=kwargs.get("should_cancel"))
    return run_json(plain, ["timeline"], cwd=project_dir, **kwargs)


def lint(project_dir: str, cli: Optional[Cli] = None, **kwargs) -> dict:
    """``hyperframes lint <project> --json``: ``{ok, errorCount, warningCount, findings}``."""
    return run_json(cli or resolve_cli(project_dir), ["lint", project_dir], **kwargs)


def workers() -> Optional[str]:
    """``-w`` for renders: ``ZENVI_HYPERFRAMES_WORKERS`` when set, else HyperFrames' own choice."""
    value = (os.environ.get(WORKERS_ENV) or "").strip()
    return value or None


def render(project_dir: str, entry: str, output: str, *, fmt: str, cli: Optional[Cli] = None,
           variables: Optional[dict] = None, fps: Optional[str] = None, quality: Optional[str] = None,
           sdr: bool = True, on_progress: Optional[Callable[[Optional[float], str], None]] = None,
           should_cancel: Optional[Callable[[], bool]] = None, timeout: float = RENDER_TIMEOUT) -> str:
    """Render *entry* (project-relative html) of *project_dir* to *output* (mp4 / mov). Blocking.

    *sdr* passes ``--sdr`` (libopenshot has no HDR path, so linked renders are
    SDR even when the project has HDR sources). Progress lines (``NN%
    Streaming frame 12/78``) go to *on_progress*. Returns *output*; CliError
    with the last lines of the CLI's output when the render fails or writes
    nothing.
    """
    cli = cli or resolve_cli(project_dir)
    args = ["render", project_dir, "--composition", entry, "--format", fmt, "--output", output]
    if sdr:
        args.append("--sdr")
    if variables:
        var_file = output + ".variables.json"
        with open(var_file, "w", encoding="utf-8") as fh:
            json.dump(variables, fh, ensure_ascii=False)
        args += ["--variables-file", var_file]
    if fps:
        args += ["--fps", str(fps)]
    if quality:
        args += ["--quality", quality]
    w = workers()
    if w:
        args += ["--workers", w]
    progress = on_progress or (lambda _f, _m: None)

    def on_line(line: str) -> None:
        text = clean_line(line)
        m = _PERCENT.search(text)
        if m:
            progress(min(1.0, int(m.group(1)) / 100.0), m.group(2).strip() or "Rendering")

    code, tail = run(cli, args, timeout=timeout, on_line=on_line, should_cancel=should_cancel)
    if code != 0 or not os.path.isfile(output) or os.path.getsize(output) == 0:
        lines = [clean_line(x) for x in tail.splitlines() if clean_line(x)]
        useful = [x for x in lines if not x.startswith("[INFO]")] or lines
        raise CliError("the HyperFrames render failed (exit %s): %s" % (code, " | ".join(useful[-6:]) or "no output"))
    return output


# ---------------------------------------------------------------------------
# Studio (hyperframes preview)
# ---------------------------------------------------------------------------

@dataclass
class Studio:
    project_dir: str
    url: str
    port: int
    proc: subprocess.Popen


_studios: Dict[str, Studio] = {}
_studio_lock = threading.Lock()
_starting: Dict[str, threading.Lock] = {}   # one start at a time per project (two quick opens: one Studio)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def running_studio(project_dir: str) -> Optional[Studio]:
    key = os.path.realpath(project_dir)
    with _studio_lock:
        st = _studios.get(key)
        if st is not None and st.proc.poll() is not None:
            _studios.pop(key, None)
            st = None
    return st


def start_studio(project_dir: str, *, cli: Optional[Cli] = None, timeout: float = STUDIO_READY_TIMEOUT) -> Studio:
    """Start ``hyperframes preview`` for *project_dir* (or reuse Zenvi's running one); returns when it is ready.

    A second call while the first is still starting waits for it and gets the same Studio.
    """
    key = os.path.realpath(project_dir)
    with _studio_lock:
        starting = _starting.setdefault(key, threading.Lock())
    with starting:
        existing = running_studio(project_dir)
        if existing is not None:
            return existing
        return _launch_studio(project_dir, cli, timeout)


def _launch_studio(project_dir: str, cli: Optional[Cli], timeout: float) -> Studio:
    cli = cli or resolve_cli(project_dir)
    port = free_port()
    argv = cli.command("preview", project_dir, "--foreground", "--json", "--no-open", "--port", str(port))
    kwargs = node_runtime._popen_kwargs()  # its own process group (stopped as a tree), no console window
    try:
        proc = subprocess.Popen(argv, cwd=work_dir(), env=cli_env(cli), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **kwargs)
    except OSError as exc:
        raise CliError(f"could not start the HyperFrames Studio: {exc}") from None
    found: Dict[str, str] = {}
    tail: List[str] = []
    done = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if not done.is_set():
                tail.append(clean_line(line))
                data = _json_from(line) if "{" in line else None
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if isinstance(result, dict) and result.get("ready"):
                    found["url"] = str(result.get("studioUrl") or result.get("serverUrl") or "")
                    done.set()
        done.set()

    threading.Thread(target=reader, name="hyperframes-studio-output", daemon=True).start()
    done.wait(timeout)
    url = found.get("url")
    if not url:
        node_runtime.kill_process_tree(proc)
        detail = " | ".join(x for x in tail[-5:] if x) or "no output"
        raise CliError(f"the HyperFrames Studio did not start within {int(timeout)} s: {detail}")
    studio = Studio(project_dir, url, port, proc)
    with _studio_lock:
        _studios[os.path.realpath(project_dir)] = studio
    log.info("HyperFrames Studio for %s at %s (pid %s)", project_dir, url, proc.pid)
    return studio


def stop_studios() -> int:
    """Stop every Studio Zenvi started (at quit). Returns how many were running."""
    with _studio_lock:
        studios = list(_studios.values())
        _studios.clear()
    for st in studios:
        try:
            node_runtime.kill_process_tree(st.proc)
        except Exception:
            log.warning("could not stop the HyperFrames Studio %s", st.proc.pid, exc_info=True)
    return len(studios)


def open_url(url: str) -> None:
    """Open *url* in the user's browser (Qt on the GUI thread when there is an app, else ``webbrowser``)."""
    try:
        from classes.qt_main_thread import call_on_gui

        def _open():
            from qt_api import QDesktopServices, QUrl
            return bool(QDesktopServices.openUrl(QUrl(url)))
        if call_on_gui(_open, timeout=15):
            return
    except Exception:
        log.debug("QDesktopServices unavailable; using webbrowser", exc_info=True)
    import webbrowser
    if not webbrowser.open(url):
        raise CliError(f"could not open a browser; the Studio is at {url}")


def open_studio(project_dir: str) -> str:
    """Start (or reuse) the Studio for *project_dir* and open it in the browser; returns its URL. Blocking."""
    studio = start_studio(project_dir)
    open_url(studio.url)
    return studio.url


atexit.register(stop_studios)


__all__ = [
    "PINNED_VERSION", "NODE_MIN_MAJOR", "QUIET_ENV", "CliError", "Cli", "resolve_cli", "project_pin", "run",
    "run_json", "timeline", "lint", "render", "clean_line", "Studio", "start_studio", "running_studio",
    "stop_studios", "open_studio", "open_url", "free_port", "private_install", "workers", "work_dir", "direct",
]
