"""Pluggable agent backends for the chat dock.

Each runner is a ``QObject`` worker (moved onto a ``QThread`` by
``AIChatWindow._make_worker``) that exposes the *same* six signals and the
``run_request(...)`` / ``clear_session()`` slots as the built-in
``AIChatWorker`` — so the existing chat rendering works unchanged regardless of
which backend produced the events.

The CLI runners (Claude Code, Codex) spawn the agent CLI as a headless
subprocess in streaming-JSON mode and point it at the in-app MCP server
(:mod:`classes.agent_mcp_server`) so the agent can drive the editor using the
same tools the built-in assistant uses. They keep their own file/shell/web
tools too (full agent abilities).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import uuid

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


# Backend identifiers (kept in sync with ai_chat_ui constants).
BACKEND_CLAUDE = "claude_code"
BACKEND_CODEX = "codex"


# Models offered in the chat model picker per backend, in menu order. ``id`` is
# passed straight through to the CLI's ``--model`` flag; Claude Code accepts a
# full model name or a latest-alias ("opus", "sonnet"), and we use full names so
# the picker keeps meaning the same model after a new release ships.
#
# A backend with an empty list hides the model pill and lets the CLI use
# whatever its own config selects — that is the case for Codex, whose model
# lineup we do not track here.
def models_for_backend(backend: str) -> list:
    """Model-picker entries for *backend* (see ``setModels`` in chat.js)."""
    if backend == BACKEND_CLAUDE:
        return [dict(m) for m in ClaudeCodeRunner.MODELS]
    if backend == BACKEND_CODEX:
        return [dict(m) for m in CodexRunner.MODELS]
    return []


def _agent_mcp_dir() -> str:
    from classes import info
    path = os.path.join(info.USER_PATH, "agent_mcp")
    os.makedirs(path, exist_ok=True)
    return path


def _project_cwd() -> str:
    """Working directory for the agent — the current project's folder if any."""
    try:
        from classes.app import get_app
        fp = getattr(get_app().project, "current_filepath", "") or ""
        if fp and os.path.isdir(os.path.dirname(fp)):
            return os.path.dirname(fp)
    except Exception:
        pass
    return os.path.expanduser("~")


def _strip_mcp_prefix(name: str) -> str:
    """``mcp__zenvi-editor__add_track_tool`` -> ``add_track_tool`` (native tools unchanged)."""
    if name and name.startswith("mcp__"):
        return name.split("__")[-1]
    return name or ""


def detect_cli(binary_name: str) -> dict:
    """Check whether *binary_name* (``claude`` or ``codex``) is on PATH, and
    (if installed) whether Zenvi's MCP server is already registered with it.

    Runs synchronously with short timeouts; callers on the GUI thread must
    offload this to a background thread (see ``AIChatWindow``'s detection
    worker) rather than call it directly.
    """
    if not shutil.which(binary_name):
        return {"installed": False, "version": None, "registered": False}
    version = None
    try:
        result = subprocess.run(
            [binary_name, "--version"], capture_output=True, text=True, timeout=3
        )
        version = (result.stdout or result.stderr or "").strip() or None
    except Exception:
        version = None
    return {"installed": True, "version": version, "registered": _is_registered(binary_name)}


def _is_registered(binary_name: str) -> bool:
    if binary_name == "claude":
        return _claude_is_registered()
    if binary_name == "codex":
        return _codex_is_registered()
    return False


def _claude_config_path() -> str:
    return os.path.expanduser("~/.claude.json")


def _claude_is_registered() -> bool:
    """Check the ``mcpServers`` table in ``~/.claude.json`` directly.

    ``claude mcp list`` also reports this, but it live health-checks every
    configured server (including ones needing OAuth) before printing
    anything — slow and network-dependent, and observed to occasionally
    exceed a reasonable subprocess timeout right after a fresh registration,
    which would misreport a real registration as absent. Reading the config
    file is instant and has no such race.
    """
    try:
        with open(_claude_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "zenvi" in (data.get("mcpServers") or {}):
            return True
    except Exception:
        pass
    return _claude_is_registered_via_cli()


def _claude_is_registered_via_cli() -> bool:
    """Fallback for when the config file can't be read directly: ask the CLI.
    ``claude mcp list`` prints one ``<name>: <url> (<transport>) - <status>``
    line per server; anchor on the colon so we don't false-match some other
    server whose URL/args happen to contain the substring "zenvi"."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10
        )
        return "zenvi:" in (result.stdout or "")
    except Exception:
        return False


def _codex_config_path() -> str:
    return os.path.expanduser("~/.codex/config.toml")


def _codex_is_registered() -> bool:
    path = _codex_config_path()
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return "zenvi_editor" in (data.get("mcp_servers") or {})
    except Exception:
        return False


def register_claude(port: int, token: str):
    """Register Zenvi's MCP server with the ``claude`` CLI (user scope).

    Idempotent: removes any prior ``zenvi`` registration first (ignoring
    failure — it's fine if none existed) so re-running this after the port
    changed (e.g. a fallback-port restart) cleanly replaces the old entry
    rather than erroring on a duplicate name.

    Returns ``(ok, message)``.
    """
    try:
        subprocess.run(
            ["claude", "mcp", "remove", "-s", "user", "zenvi"],
            capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            ["claude", "mcp", "add", "--transport", "http", "zenvi",
             "http://127.0.0.1:%d/mcp" % port,
             "--header", "Authorization: Bearer %s" % token,
             "--scope", "user"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, "Connected. Run `claude` in your terminal to use it."
        return False, (result.stderr or result.stdout or "claude mcp add failed").strip()
    except Exception as e:
        return False, str(e)


_CODEX_SECTION_HEADER = "[mcp_servers.zenvi_editor]"


def _codex_desired_section(port: int) -> str:
    return (
        '%s\n'
        'url = "http://127.0.0.1:%d/mcp"\n'
        'bearer_token_env_var = "ZENVI_MCP_TOKEN"\n'
    ) % (_CODEX_SECTION_HEADER, port)


def register_codex(port: int, token: str):
    """Write/update the ``[mcp_servers.zenvi_editor]`` table in
    ``~/.codex/config.toml`` (Codex has no CLI command for registering an
    HTTP-transport MCP server — only stdio servers via ``codex mcp add``;
    confirmed against the current Codex CLI docs).

    Validates the file both before and after editing, and writes a
    ``.zenvi-backup`` copy first — this mutates a config file we don't fully
    control the rest of the schema/contents of, so failing safe matters more
    than convenience here.

    The token itself is never written to the file (Codex reads it from the
    ``ZENVI_MCP_TOKEN`` env var at runtime via ``bearer_token_env_var``), so
    the returned message tells the caller to export it before running codex.

    Returns ``(ok, message)``.
    """
    path = _codex_config_path()
    try:
        import tomllib
    except ImportError:
        return False, "Python 3.11+ (tomllib) is required to edit config.toml."

    original = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except Exception as e:
            return False, "Failed to read ~/.codex/config.toml: %s" % e
        try:
            tomllib.loads(original)
        except Exception as e:
            return False, "~/.codex/config.toml has invalid TOML, not touching it: %s" % e

    new_section = _codex_desired_section(port)
    if _CODEX_SECTION_HEADER in original:
        pattern = re.compile(re.escape(_CODEX_SECTION_HEADER) + r".*?(?=\n\[|\Z)", re.DOTALL)
        updated = pattern.sub(new_section.rstrip("\n"), original, count=1)
    else:
        if original and not original.endswith("\n"):
            original += "\n"
        sep = "\n" if original else ""
        updated = original + sep + new_section

    try:
        tomllib.loads(updated)
    except Exception as e:
        return False, "Generated config would be invalid TOML, aborting: %s" % e

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if original:
            with open(path + ".zenvi-backup", "w", encoding="utf-8") as fh:
                fh.write(original)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except Exception as e:
        return False, "Failed to write ~/.codex/config.toml: %s" % e

    return True, (
        "Updated ~/.codex/config.toml. Before running codex, run:\n"
        "export ZENVI_MCP_TOKEN=%s"
    ) % token


class BaseAgentRunner(QObject):
    """Common signal surface + subprocess plumbing for CLI agent backends."""

    # Identical to AIChatWorker so the existing AIChatWindow slots connect 1:1.
    response_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    token_received = pyqtSignal(str)
    tool_started = pyqtSignal(str, str, str)   # call_id, tool_name, args_json
    tool_log = pyqtSignal(str, str)            # call_id, line
    tool_completed = pyqtSignal(str, bool, str)  # call_id, ok, result_text
    # Declared for signature parity with AIChatWorker so AIChatWindow can
    # connect the same slots to every backend. CLI backends never emit it —
    # planning mode is a Zenvi-backend feature, and these agents do their own
    # planning internally.
    plan_event = pyqtSignal(str, str)          # event_type, payload_json

    CLI_NAME = ""        # executable, e.g. "claude"
    DISPLAY_NAME = ""    # human label, e.g. "Claude Code"
    MODELS: list = []    # model-picker entries; empty = use the CLI's own default

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session_id = ""          # set by AIChatWindow._make_worker
        self._backend_session_id = ""
        self._cli_session_id = ""      # CLI-side conversation id (for resume)
        self._cli_started = False
        self._stopping = False         # shutdown flag (mirrors AIChatWorker)
        # User pressed Stop. Distinct from _stopping, which means the whole app
        # (or this tab) is going away and must stay latched: a cancelled tab has
        # to accept the next message, so this one is cleared by run_request.
        self._cancelled = False
        self._proc = None
        self._model_id = ""
        self._server = None
        self._responded = False
        self._final_text = ""
        self._last_error = ""
        self._stderr_tail: list = []

    # -- slots -------------------------------------------------------------
    @pyqtSlot()
    def clear_session(self):
        """Forget CLI continuity so the next message starts a fresh conversation."""
        self._cli_started = False
        self._cli_session_id = ""

    def cancel(self):
        """Terminate the running subprocess (called from the GUI thread).

        Non-blocking: the worker thread's read loop hits EOF and its final
        ``wait()`` reaps the process, so we don't stall the UI here.
        """
        self._cancelled = True
        proc = self._proc
        if proc and proc.poll() is None:
            # Signal the whole process group, not just the CLI: these agents
            # spawn their own children (shells, language servers, MCP clients),
            # and terminating the parent alone leaves those running — they keep
            # driving the editor through the MCP server after the user pressed
            # Stop. run_request starts the child in its own session so this
            # group id is ours to kill.
            for send in (
                lambda: os.killpg(os.getpgid(proc.pid), signal.SIGTERM),
                proc.terminate,
                proc.kill,
            ):
                try:
                    send()
                    return
                except Exception:
                    continue

    @property
    def _aborted(self) -> bool:
        """True when nothing more should be emitted for the current request."""
        return self._stopping or self._cancelled

    # Signature must match AIChatWorker.run_request exactly: AIChatWindow
    # dispatches through QMetaObject.invokeMethod with five Q_ARG(str, ...),
    # and Qt resolves the slot by its registered signature — a shorter one is
    # simply never found and the request silently does nothing.
    @pyqtSlot(str, str, str, str, str)
    def run_request(self, text: str, model_id: str, agent_mode: str = "agent",
                    action: str = "chat", plan_id: str = ""):
        # ``agent_mode``/``action``/``plan_id`` drive the Zenvi backend's
        # planning flow only; CLI agents plan internally, so they are accepted
        # for signature parity and otherwise ignored.
        self._cancelled = False
        self._model_id = self._coerce_model(model_id)
        self._responded = False
        self._final_text = ""
        self._last_error = ""
        self._stderr_tail = []
        if not self._cli_session_id:
            self._cli_session_id = self._session_id or str(uuid.uuid4())

        try:
            from classes.agent_mcp_server import get_mcp_server
            self._server = get_mcp_server().start()
        except Exception as e:
            log.error("MCP server start failed: %s", e, exc_info=True)
            self._emit_error("Could not start the editor tool server: %s" % e)
            return

        if shutil.which(self.CLI_NAME) is None:
            self._emit_error(
                "%s CLI not found. Install it and make sure '%s' is on your PATH, "
                "then try again." % (self.DISPLAY_NAME, self.CLI_NAME)
            )
            return

        ready_err = self._ensure_ready()
        if ready_err:
            self._emit_error(ready_err)
            return

        try:
            argv = self._build_argv(text)
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Explicit UTF-8, not text=True's locale-dependent default: a
                # GUI-launched app's environment often lacks LANG/LC_ALL, which
                # can silently resolve to ASCII and crash on the CLI's normal
                # non-ASCII output (em dashes, arrows, checkmarks, etc.).
                # errors="replace" so a genuinely malformed byte degrades to
                # U+FFFD instead of killing the whole read loop.
                encoding="utf-8", errors="replace",
                bufsize=1, env=self._build_env(), cwd=_project_cwd(),
                # Own process group so cancel() can signal the CLI *and* every
                # child it spawned (see cancel()).
                start_new_session=True,
            )
        except Exception as e:
            if not self._aborted:
                self._emit_error("Failed to launch %s: %s" % (self.DISPLAY_NAME, e))
            return

        try:
            for line in self._proc.stdout:
                if self._aborted:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    # Non-JSON log noise (CLI diagnostics) — keep a short tail
                    # so we can surface something useful if the run fails.
                    self._stderr_tail.append(line)
                    self._stderr_tail = self._stderr_tail[-8:]
                    continue
                try:
                    self._handle_event(ev)
                except Exception:
                    log.debug("agent event handling failed", exc_info=True)
            self._proc.wait(timeout=5)
        except Exception as e:
            if not self._aborted:
                self._emit_error(str(e))
            return

        if self._aborted:
            return
        self._cli_started = True
        if self._responded:
            return
        if self._final_text:
            self._emit_response(self._final_text)
        elif self._last_error:
            self._emit_error(self._last_error)
        elif self._proc.returncode not in (0, None):
            tail = "\n".join(self._stderr_tail[-4:])
            self._emit_error(
                "%s exited with code %s.%s"
                % (self.DISPLAY_NAME, self._proc.returncode,
                   ("\n" + tail) if tail else "")
            )
        else:
            self._emit_response("(No response.)")

    # -- emit helpers (respect shutdown) -----------------------------------
    def _emit_response(self, text: str):
        self._responded = True
        if not self._aborted:
            self.response_ready.emit(text or "")

    def _emit_error(self, text: str):
        if not self._aborted:
            self.error_occurred.emit(text or "Unknown error.")

    # -- model selection ---------------------------------------------------
    def _coerce_model(self, model_id: str) -> str:
        """Keep *model_id* only if it is one this backend actually offers.

        Tabs remember the model the picker last had, and that picker is shared
        with the other backends — so a tab switched from Zenvi to Claude Code
        can arrive holding a Zenvi model id, which the CLI would reject.
        """
        if not model_id or not self.MODELS:
            return ""
        return model_id if any(m["id"] == model_id for m in self.MODELS) else ""

    # -- subclass hooks ----------------------------------------------------
    def _ensure_ready(self):
        """Return an error string if the backend can't run, else None."""
        return None

    def _build_env(self):
        return None

    def _build_argv(self, text: str):
        raise NotImplementedError

    def _handle_event(self, ev: dict):
        raise NotImplementedError


class ClaudeCodeRunner(BaseAgentRunner):
    """Drives the Claude Code CLI (`claude -p --output-format stream-json`)."""

    CLI_NAME = "claude"
    DISPLAY_NAME = "Claude Code"

    # ``rank`` orders the picker, ``featured`` decides whether an entry shows
    # before the menu's "show all" toggle — same contract as the Zenvi model
    # list the backend serves (see setModels in chat.js).
    MODELS = [
        {"id": "claude-opus-5",   "name": "Opus 5",   "provider": "anthropic",
         "rank": 10, "featured": True, "default": True,
         "tags": ["Most capable"]},
        {"id": "claude-sonnet-5", "name": "Sonnet 5", "provider": "anthropic",
         "rank": 20, "featured": True, "tags": ["Balanced"]},
        {"id": "claude-haiku-4-5", "name": "Haiku 4.5", "provider": "anthropic",
         "rank": 30, "featured": True, "tags": ["Fastest"]},
        {"id": "claude-fable-5",  "name": "Fable 5",  "provider": "anthropic",
         "rank": 40, "featured": False, "tags": ["Frontier"]},
        {"id": "claude-opus-4-8", "name": "Opus 4.8", "provider": "anthropic",
         "rank": 50, "featured": False},
        {"id": "claude-opus-4-7", "name": "Opus 4.7", "provider": "anthropic",
         "rank": 60, "featured": False},
        {"id": "claude-opus-4-6", "name": "Opus 4.6", "provider": "anthropic",
         "rank": 70, "featured": False},
        {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6", "provider": "anthropic",
         "rank": 80, "featured": False},
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._open_blocks: dict = {}   # stream_event block index -> {"kind","call_id"}
        self._think_seq = 0

    def _build_argv(self, text: str):
        cfg = _write_claude_mcp_config(self._server)
        argv = [
            self.CLI_NAME, "-p", text,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", cfg, "--strict-mcp-config",
            # The agent is driving the editor on the user's behalf from inside
            # the app — there is no terminal to answer a permission prompt, so
            # a prompt would just hang the turn until it times out.
            "--dangerously-skip-permissions",
        ]
        if self._model_id:
            argv += ["--model", self._model_id]
        if self._cli_started and self._cli_session_id:
            argv += ["--resume", self._cli_session_id]
        else:
            argv += ["--session-id", self._cli_session_id]
        return argv

    def _handle_event(self, ev: dict):
        etype = ev.get("type")
        if etype == "stream_event":
            self._handle_stream_event(ev.get("event") or {})
            return
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name") or "tool"
                    self.tool_started.emit(
                        block.get("id") or "", _strip_mcp_prefix(name),
                        json.dumps(block.get("input") or {}, default=str))
            return
        if etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    text = _content_to_text(block.get("content"))
                    ok = not block.get("is_error")
                    self.tool_completed.emit(block.get("tool_use_id") or "", ok, text)
            return
        if etype == "result":
            if ev.get("is_error"):
                self._last_error = ev.get("result") or "The agent reported an error."
            else:
                self._emit_response(ev.get("result") or self._final_text)
            return

    def _handle_stream_event(self, event: dict):
        etype = event.get("type")
        if etype == "content_block_start":
            idx = event.get("index")
            cb = event.get("content_block") or {}
            if cb.get("type") == "thinking":
                self._think_seq += 1
                call_id = "think_%d" % self._think_seq
                self._open_blocks[idx] = {"kind": "thinking", "call_id": call_id}
                self.tool_started.emit(call_id, "thinking", "{}")
            else:
                self._open_blocks[idx] = {"kind": cb.get("type")}
            return
        if etype == "content_block_delta":
            idx = event.get("index")
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                txt = delta.get("text") or ""
                self._final_text += txt
                self.token_received.emit(txt)
            elif dtype == "thinking_delta":
                blk = self._open_blocks.get(idx)
                if blk and blk.get("kind") == "thinking":
                    self.tool_log.emit(blk["call_id"], delta.get("thinking") or "")
            return
        if etype == "content_block_stop":
            blk = self._open_blocks.pop(event.get("index"), None)
            if blk and blk.get("kind") == "thinking":
                self.tool_completed.emit(blk["call_id"], True, "")
            return
        if etype == "message_start":
            # A fresh assistant turn streams into the same bubble; drop stale
            # block bookkeeping but keep accumulated final text.
            self._open_blocks.clear()


class CodexRunner(BaseAgentRunner):
    """Drives the Codex CLI (`codex exec --json`)."""

    CLI_NAME = "codex"
    DISPLAY_NAME = "Codex"
    # Left empty on purpose: we do not track the Codex model lineup, so the
    # picker stays hidden and the CLI uses whatever its own config selects.
    MODELS: list = []

    _TOOL_ITEM_TYPES = {
        "command_execution", "mcp_tool_call", "tool_call", "function_call",
        "local_shell_call", "web_search",
    }
    _MSG_ITEM_TYPES = {"assistant_message", "agent_message", "message"}

    def _build_env(self):
        env = dict(os.environ)
        if self._server is not None and self._server.token:
            env["ZENVI_MCP_TOKEN"] = self._server.token
        return env

    def _build_argv(self, text: str):
        url = self._server.url() if self._server else ""
        common = [
            "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "-c", 'mcp_servers.zenvi_editor.url="%s"' % url,
            "-c", 'mcp_servers.zenvi_editor.bearer_token_env_var="ZENVI_MCP_TOKEN"',
        ]
        if self._model_id:
            common += ["--model", self._model_id]
        if self._cli_started and self._cli_session_id:
            return [self.CLI_NAME, "exec", "resume", self._cli_session_id, *common, text]
        return [self.CLI_NAME, "exec", *common, text]

    def _handle_event(self, ev: dict):
        etype = ev.get("type")
        if etype == "thread.started":
            self._cli_session_id = ev.get("thread_id") or self._cli_session_id
            return
        if etype in ("item.started", "item.updated", "item.completed"):
            self._handle_item(etype, ev.get("item") or {})
            return
        if etype == "turn.completed":
            self._emit_response(self._final_text)
            return
        if etype == "turn.failed":
            self._last_error = (ev.get("error") or {}).get("message") or "The agent reported an error."
            return
        if etype == "error":
            # Many of these are transient "Reconnecting..." notices; remember the
            # latest as a fallback only.
            self._last_error = ev.get("message") or self._last_error

    def _handle_item(self, etype: str, item: dict):
        itype = item.get("type")
        call_id = item.get("id") or ""
        if itype in self._TOOL_ITEM_TYPES:
            if etype == "item.started":
                name = item.get("tool") or item.get("name") or item.get("command") or itype
                args = item.get("arguments") or item.get("input") or {}
                self.tool_started.emit(call_id, _strip_mcp_prefix(str(name)),
                                       json.dumps(args, default=str) if isinstance(args, dict) else "{}")
            elif etype == "item.completed":
                out = item.get("output") or item.get("result") or item.get("text") or ""
                ok = (item.get("status") or "completed") not in ("failed", "error")
                self.tool_completed.emit(call_id, ok, str(out))
            return
        if itype in self._MSG_ITEM_TYPES:
            if etype == "item.completed":
                txt = item.get("text") or item.get("message") or ""
                if txt:
                    self._final_text = txt
                    self.token_received.emit(txt)
            return
        if itype == "reasoning" and etype == "item.completed":
            txt = item.get("text") or ""
            if txt:
                rid = "think_%s" % (call_id or "0")
                self.tool_started.emit(rid, "thinking", "{}")
                self.tool_log.emit(rid, txt)
                self.tool_completed.emit(rid, True, "")


# ---------------------------------------------------------------------------
# MCP config helpers
# ---------------------------------------------------------------------------

def _write_claude_mcp_config(server) -> str:
    """Write the Claude `--mcp-config` JSON pointing at the in-app MCP server."""
    cfg = {
        "mcpServers": {
            "zenvi-editor": {
                "type": "http",
                "url": server.url(),
                "headers": {"Authorization": "Bearer %s" % server.token},
            }
        }
    }
    path = os.path.join(_agent_mcp_dir(), "claude_mcp.json")
    with open(path, "w") as fh:
        json.dump(cfg, fh)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _content_to_text(content) -> str:
    """Flatten an MCP/Claude tool_result `content` field into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
            elif isinstance(blk, dict) and blk.get("text"):
                parts.append(blk.get("text"))
        return "\n".join(p for p in parts if p)
    return str(content)


def cleanup_agent_mcp_configs():
    """Remove written MCP config files (called on shutdown)."""
    try:
        import glob
        for p in glob.glob(os.path.join(_agent_mcp_dir(), "*.json")):
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
