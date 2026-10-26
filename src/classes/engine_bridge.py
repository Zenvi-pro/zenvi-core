"""Client for the engine bridge: Zenvi engine tools run by the bundled ``zenvi`` CLI.

The TypeScript engine (zenvi-web ``@zenvi/engine``) is where new editing
tools land. Instead of a Python port of each, the desktop runs the CLI on a
snapshot of the open project and applies the change it answers -- desktop
UpdateManager actions -- as one undo step (``editor_tools/engine_bridge.py``).
Design, protocol and measurements: zenvi-web ``docs/ENGINE_BRIDGE.md``.

Off unless ``ZENVI_ENGINE_BRIDGE`` is set:

* ``1`` / ``server`` -- one long-lived ``zenvi bridge`` process, JSON-RPC lines
  on its stdin/stdout (Node starts once per session).
* ``oneshot`` -- ``zenvi tool-run`` per call (a Node start each time).

``ZENVI_ENGINE_CLI`` is the CLI bundle (``zenvi.mjs``); ``ZENVI_NODE`` the
Node binary (default: ``node`` on PATH). Every method blocks: call them off
the GUI thread.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Optional

from classes.logger import log

# The bridge protocol this desktop speaks (zenvi-web packages/cli/src/bridge/run.ts BRIDGE_PROTOCOL).
PROTOCOL = 1
DEFAULT_TIMEOUT_SECONDS = 120.0
CATALOG_TIMEOUT_SECONDS = 30.0


class BridgeUnavailable(Exception):
    """The CLI cannot be started, crashed, timed out or speaks another protocol."""


def bridge_mode(env=None) -> str:
    """'' (off), 'server' or 'oneshot', from ``ZENVI_ENGINE_BRIDGE``."""
    value = str((env if env is not None else os.environ).get("ZENVI_ENGINE_BRIDGE", "")).strip().lower()
    if value in ("1", "true", "yes", "on", "server"):
        return "server"
    if value == "oneshot":
        return "oneshot"
    return ""


def locate_cli(env=None) -> tuple:
    """(node, zenvi.mjs) from the environment; BridgeUnavailable when either is missing."""
    env = env if env is not None else os.environ
    cli = env.get("ZENVI_ENGINE_CLI", "")
    if not cli or not os.path.isfile(cli):
        raise BridgeUnavailable("set ZENVI_ENGINE_CLI to the zenvi CLI bundle (zenvi.mjs)")
    node = env.get("ZENVI_NODE") or shutil.which("node")
    if not node:
        raise BridgeUnavailable("Node.js was not found (set ZENVI_NODE)")
    return node, os.path.abspath(cli)


def _timeout(env=None) -> float:
    try:
        return float((env if env is not None else os.environ).get("ZENVI_ENGINE_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
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
            mode = bridge_mode()
            if not mode:
                raise BridgeUnavailable("the engine bridge is off (set ZENVI_ENGINE_BRIDGE=1)")
            node, cli = locate_cli()
            _client = EngineBridgeClient(node, cli, mode)
        return _client


def set_client(client: Optional[EngineBridgeClient]) -> None:
    """Use *client* for this session (tests), closing the previous one."""
    global _client
    with _client_lock:
        if _client is not None and _client is not client:
            _client.close()
        _client = client
