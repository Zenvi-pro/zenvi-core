"""Client for the engine bridge: Zenvi engine tools run by the bundled ``zenvi`` CLI.

The TypeScript engine (zenvi-web ``@zenvi/engine``) is where new editing
tools land. Instead of a Python port of each, the desktop runs the CLI on a
snapshot of the open project and applies the change it answers -- desktop
UpdateManager actions -- as one undo step (``editor_tools/engine_bridge.py``).
Design, protocol and measurements: zenvi-web ``docs/ENGINE_BRIDGE.md``.

On by default once its runtime is installed (``classes.engine_runtime``:
Node and the CLI, downloaded on demand into ``~/.openshot_qt/engine``);
``ZENVI_ENGINE_BRIDGE`` or the ``engine-bridge`` setting choose otherwise:

* ``auto`` (default) -- ``server`` when the CLI and a Node are found, else off.
* ``1`` / ``server`` -- one long-lived ``zenvi bridge`` process, JSON-RPC lines
  on its stdin/stdout (Node starts once per session).
* ``oneshot`` -- ``zenvi tool-run`` per call (a Node start each time).
* ``off`` -- the kill switch: no engine tools, whatever is installed.

Where the CLI comes from (:func:`find_cli`, first hit wins):
``ZENVI_ENGINE_CLI``, the ``engine-cli-path`` setting, the copy a build bundles
at ``<app>/engine/zenvi.mjs``, then the installed runtime's
``~/.openshot_qt/engine/cli/zenvi.mjs`` (only while its recorded bridge
protocol is this desktop's). Node (:func:`find_node`, 22 or newer):
``ZENVI_NODE``, the ``engine-node-path`` setting, the runtime's own
``~/.openshot_qt/engine/node``, the private Node the local HyperFrames setup
installs under ``~/.openshot_qt/hyperframes/node``, then ``PATH`` and the
folders installers use that a GUI app's PATH misses. Neither found: every
bridged call is refused with what was searched and how to install it.

Every method blocks (file checks, ``node --version``, the CLI itself): call
them off the GUI thread.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Optional

from classes.logger import log

# The bridge protocol this desktop speaks (zenvi-web packages/cli/src/bridge/run.ts BRIDGE_PROTOCOL).
PROTOCOL = 2
DEFAULT_TIMEOUT_SECONDS = 120.0
CATALOG_TIMEOUT_SECONDS = 30.0
# zenvi-web package.json "engines".
MIN_NODE_MAJOR = 22
NODE_VERSION_TIMEOUT = 10.0
CLI_NAME = "zenvi.mjs"
# Under info.USER_PATH: the runtime engine_runtime installs (cli/, node/, runtime.json).
RUNTIME_DIR = "engine"
INSTALL_HINT = "AI Tools > Zenvi Engine Tools... installs them (a one-time download)"


class BridgeUnavailable(Exception):
    """The CLI cannot be started, crashed, timed out or speaks another protocol."""


def _setting(name: str) -> str:
    """A desktop setting as text ('' without an app, e.g. in the headless test suite)."""
    try:
        from classes.app import get_app
        value = get_app().get_settings().get(name)
    except Exception:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _env(env):
    return env if env is not None else os.environ


def bridge_setting(env=None) -> str:
    """'auto', 'server', 'oneshot' or 'off', from ``ZENVI_ENGINE_BRIDGE`` or the ``engine-bridge`` setting.

    Reads no files: safe on the GUI thread.
    """
    value = str(_env(env).get("ZENVI_ENGINE_BRIDGE", "")).strip().lower()
    if not value and env is None:
        value = _setting("engine-bridge").lower()
    if value in ("1", "true", "yes", "on", "server"):
        return "server"
    if value == "oneshot":
        return "oneshot"
    if value in ("0", "false", "no", "off"):
        return "off"
    return "auto"


def bridge_mode(env=None) -> str:
    """'' (off), 'server' or 'oneshot'. ``auto`` looks for the runtime: blocks, off the GUI thread."""
    setting = bridge_setting(env)
    if setting == "off":
        return ""
    if setting == "auto":
        return "server" if runtime_ready(env) else ""
    return setting


def runtime_ready(env=None) -> bool:
    """Whether the CLI and a Node it runs on are found (blocks: file checks, ``node --version``)."""
    try:
        locate_cli(env)
    except BridgeUnavailable:
        return False
    return True


def managed_cli_dir() -> str:
    from classes import info
    return os.path.join(info.USER_PATH, RUNTIME_DIR, "cli")


def managed_cli() -> str:
    return os.path.join(managed_cli_dir(), CLI_NAME)


def managed_node_dir() -> str:
    from classes import info
    return os.path.join(info.USER_PATH, RUNTIME_DIR, "node")


def managed_node() -> str:
    if sys.platform == "win32":
        return os.path.join(managed_node_dir(), "node.exe")
    return os.path.join(managed_node_dir(), "bin", "node")


def installed_runtime() -> dict:
    """The installed runtime's runtime.json ({} when there is none or it is unreadable)."""
    from classes import info
    try:
        with open(os.path.join(info.USER_PATH, RUNTIME_DIR, "runtime.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cli_candidates(env=None) -> list:
    """Where the CLI bundle may be, in the order :func:`find_cli` tries them."""
    from classes import info
    env = _env(env)
    named = [env.get("ZENVI_ENGINE_CLI", "")]
    if env is os.environ:
        named.append(_setting("engine-cli-path"))
    return [p for p in named if p] + [os.path.join(info.PATH, "engine", CLI_NAME), managed_cli()]


def find_cli(env=None) -> str:
    """The zenvi CLI bundle (``zenvi.mjs``), absolute; BridgeUnavailable with what was searched."""
    env = _env(env)
    candidates = cli_candidates(env)
    explicit = env.get("ZENVI_ENGINE_CLI", "") or (_setting("engine-cli-path") if env is os.environ else "")
    if explicit:
        # A path the user gave is the one they mean: no quiet fallback to another build.
        if os.path.isfile(explicit):
            return os.path.abspath(explicit)
        raise BridgeUnavailable(f"the zenvi CLI is not at {explicit} (ZENVI_ENGINE_CLI / the engine-cli-path setting)")
    outdated = ""
    for path in candidates:
        if not os.path.isfile(path):
            continue
        if path == managed_cli():
            spoken = (installed_runtime().get("cli") or {}).get("protocol")
            if spoken != PROTOCOL:
                # An older install (or one cut short): never run a CLI this desktop cannot talk to.
                outdated = (f" The installed zenvi CLI speaks bridge protocol {spoken}, this Zenvi {PROTOCOL}: "
                            "update it (AI Tools > Zenvi Engine Tools...).")
                continue
        return os.path.abspath(path)
    raise BridgeUnavailable(
        f"Zenvi engine tools are not installed: {INSTALL_HINT}, or set ZENVI_ENGINE_CLI / Preferences > AI > "
        f"Zenvi CLI for Engine Tools to a built zenvi.mjs (zenvi-web: npm run build -w @zenvi/cli).{outdated} "
        "Looked in: " + ", ".join(candidates))


def node_candidates(env=None) -> list:
    """Node executables to try, in order (a folder named by ZENVI_NODE means the node inside it)."""
    from classes import info
    env = _env(env)
    exe = "node.exe" if sys.platform == "win32" else "node"
    explicit = _explicit_node(env)
    if explicit:
        # A Node the user names is the one they mean: no quiet fallback to another install.
        return [explicit]
    out = [managed_node()]
    private = os.path.join(info.USER_PATH, "hyperframes", "node")
    out.append(os.path.join(private, exe) if sys.platform == "win32" else os.path.join(private, "bin", exe))
    on_path = shutil.which("node", path=env.get("PATH"))
    if on_path:
        out.append(on_path)
    if sys.platform == "win32":
        out.append(os.path.join(env.get("ProgramFiles", r"C:\Program Files"), "nodejs", exe))
    else:
        out += ["/opt/homebrew/bin/node", "/usr/local/bin/node", os.path.expanduser("~/.volta/bin/node")]
    seen, unique = set(), []
    for path in out:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _explicit_node(env) -> str:
    """ZENVI_NODE or the engine-node-path setting: a node executable, the folder holding it, or a name on PATH."""
    named = env.get("ZENVI_NODE", "") or (_setting("engine-node-path") if env is os.environ else "")
    if not named:
        return ""
    if os.path.isdir(named):
        return os.path.join(named, "node.exe" if sys.platform == "win32" else "node")
    if os.path.dirname(named):
        return named
    return shutil.which(named, path=env.get("PATH")) or named


_node_majors: dict = {}


def forget_node_versions() -> None:
    """Ask every Node its version again (after an install put a new one in place)."""
    _node_majors.clear()


def node_major(node: str) -> Optional[int]:
    """``node --version``'s major number, or None when it does not run (cached per path)."""
    if node in _node_majors:
        return _node_majors[node]
    try:
        done = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=NODE_VERSION_TIMEOUT,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None  # a slow first start (antivirus): not remembered, the next call asks again
    except (OSError, subprocess.SubprocessError):
        done = None
    found = re.match(r"v?(\d+)\.", (done.stdout or "").strip()) if done is not None else None
    major = int(found.group(1)) if found else None
    _node_majors[node] = major
    return major


def find_node(env=None) -> str:
    """A Node.js new enough for the CLI; BridgeUnavailable with what was searched."""
    tried = []
    explicit = _explicit_node(_env(env))
    for path in node_candidates(env):
        if not os.path.isfile(path):
            if path == explicit:
                raise BridgeUnavailable(f"Node.js is not at {path} (ZENVI_NODE / the engine-node-path setting)")
            continue
        major = node_major(path)
        if major is not None and major >= MIN_NODE_MAJOR:
            return path
        tried.append(f"{path} ({'v%d' % major if major is not None else 'does not run'})")
    found = f" Found only: {', '.join(tried)}." if tried else ""
    raise BridgeUnavailable(
        f"Zenvi engine tools need Node.js {MIN_NODE_MAJOR} or newer.{found} {INSTALL_HINT} with a private "
        "copy; or install it (https://nodejs.org), or set ZENVI_NODE / Preferences > AI > Node.js for Engine Tools.")


def locate_cli(env=None) -> tuple:
    """(node, zenvi.mjs); BridgeUnavailable when either is missing."""
    cli = find_cli(env)
    return find_node(env), cli


def _timeout(env=None) -> float:
    try:
        return float(_env(env).get("ZENVI_ENGINE_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


class EngineBridgeClient:
    """Runs engine tools through the CLI. Thread-safe; calls are serialized."""

    def __init__(self, node: str, cli: str, mode: str = "server", timeout: Optional[float] = None):
        self.node = node
        self.cli = cli
        self.mode = mode
        self.timeout = timeout if timeout is not None else _timeout()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self.engine_version = ""

    # -- public ---------------------------------------------------------------
    def tools(self) -> list:
        """The engine catalog as the bridge runs it (``zenvi bridge --tools``)."""
        out = self._run_once(["bridge", "--tools"], CATALOG_TIMEOUT_SECONDS)
        if out.get("protocol") != PROTOCOL:
            raise BridgeUnavailable(
                f"the zenvi CLI speaks bridge protocol {out.get('protocol')}, this desktop {PROTOCOL}")
        tools = out.get("tools")
        if not isinstance(tools, list):
            raise BridgeUnavailable("the CLI listed no tools")
        self.engine_version = str(out.get("engine_version") or "")
        return tools

    def run(self, tool: str, args: dict, project_json: str, project_file: str,
            backend_token: Optional[str] = None) -> dict:
        """Run *tool* on the project (its JSON text as the desktop holds it). Returns the bridge answer."""
        with self._lock:
            if self.mode == "oneshot":
                return self._run_tool_once(tool, args, project_json, project_file, backend_token)
            params = {"tool": tool, "args": args or {}, "project_file": project_file}
            if backend_token:
                params["backend_token"] = backend_token
            # Splice the project text in as is: no second encode of a large project.
            head = json.dumps(params)[:-1]
            return self._request("run", head + ', "project": ' + project_json + "}")

    def close(self) -> None:
        with self._lock:
            self._stop()

    # -- server mode ----------------------------------------------------------
    def _start(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                [self.node, self.cli, "bridge"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise BridgeUnavailable(f"could not start the zenvi CLI: {exc}") from exc
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True,
                         name="engine-bridge-stderr").start()
        hello = self._exchange(self._proc, "hello", None)
        if hello.get("protocol") != PROTOCOL:
            self._stop()
            raise BridgeUnavailable(
                f"the zenvi CLI speaks bridge protocol {hello.get('protocol')}, this desktop {PROTOCOL}")
        self.engine_version = str(hello.get("engine_version") or "")
        return self._proc

    @staticmethod
    def _drain_stderr(proc) -> None:
        for line in iter(proc.stderr.readline, b""):
            log.debug("zenvi bridge: %s", line.decode("utf-8", "replace").rstrip())

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            log.debug("zenvi bridge did not stop cleanly", exc_info=True)

    def _request(self, method: str, params_json: Optional[str]) -> dict:
        proc = self._start()
        try:
            return self._exchange(proc, method, params_json)
        except BridgeUnavailable:
            # A crashed or hung process is not reused; the next call starts a new one.
            self._stop()
            raise

    def _exchange(self, proc, method: str, params_json: Optional[str]) -> dict:
        self._next_id += 1
        rid = self._next_id
        line = '{"jsonrpc": "2.0", "id": %d, "method": %s' % (rid, json.dumps(method))
        line += (', "params": ' + params_json + "}\n") if params_json else "}\n"
        # A hung call is killed: readline then returns b"" and the call fails.
        watchdog = threading.Timer(self.timeout, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            proc.stdin.write(line.encode("utf-8"))
            proc.stdin.flush()
            raw = proc.stdout.readline()
        except OSError as exc:
            raise BridgeUnavailable(f"the zenvi CLI stopped: {exc}") from exc
        finally:
            watchdog.cancel()
        if not raw:
            raise BridgeUnavailable("the zenvi CLI stopped or timed out")
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise BridgeUnavailable(f"the zenvi CLI answered something that is not JSON: {raw[:200]!r}") from exc
        if message.get("id") != rid:
            raise BridgeUnavailable("the zenvi CLI answered out of order")
        if "error" in message:
            raise BridgeUnavailable(str((message.get("error") or {}).get("message") or "bridge error"))
        return message.get("result") or {}

    # -- one-shot mode --------------------------------------------------------
    def _run_once(self, argv: list, timeout: float, env=None) -> dict:
        try:
            done = subprocess.run([self.node, self.cli, *argv], capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeUnavailable(f"the zenvi CLI did not answer: {exc}") from exc
        try:
            return json.loads(done.stdout)
        except ValueError as exc:
            err = done.stderr.decode("utf-8", "replace").strip()[-300:]
            raise BridgeUnavailable(f"the zenvi CLI failed (exit {done.returncode}): {err}") from exc

    def _run_tool_once(self, tool, args, project_json, project_file, backend_token) -> dict:
        folder = tempfile.mkdtemp(prefix="zenvi-bridge-")
        try:
            snapshot = os.path.join(folder, "project.json")
            with open(snapshot, "w", encoding="utf-8") as fh:
                fh.write(project_json)
            env = dict(os.environ)
            if backend_token:
                env["ZENVI_BACKEND_TOKEN"] = backend_token
            return self._run_once(
                ["tool-run", tool, "--args", json.dumps(args or {}), "--snapshot", snapshot,
                 "--project-file", project_file], self.timeout, env=env)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


_client: Optional[EngineBridgeClient] = None
_client_lock = threading.Lock()


def get_client() -> EngineBridgeClient:
    """The session's client (BridgeUnavailable when the bridge is off or the CLI is missing)."""
    global _client
    with _client_lock:
        if _client is None:
            setting = bridge_setting()
            if setting == "off":
                raise BridgeUnavailable("Zenvi engine tools are turned off (Preferences > AI > Zenvi Engine Tools)")
            node, cli = locate_cli()
            mode = "server" if setting == "auto" else setting
            _client = EngineBridgeClient(node, cli, mode)
        return _client


def set_client(client: Optional[EngineBridgeClient]) -> None:
    """Use *client* for this session (tests), closing the previous one."""
    global _client
    with _client_lock:
        if _client is not None and _client is not client:
            _client.close()
        _client = client
