"""Pluggable agent backends for the chat dock.

Each runner is a ``QObject`` worker (moved onto a ``QThread`` by
``AIChatWindow._make_worker``) that exposes the *same* six signals and the
``run_request(text, model_id)`` / ``clear_session()`` slots as the built-in
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
import shutil
import subprocess
import uuid

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


# Backend identifiers (kept in sync with ai_chat_ui constants).
BACKEND_CLAUDE = "claude_code"
BACKEND_CODEX = "codex"


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


class BaseAgentRunner(QObject):
    """Common signal surface + subprocess plumbing for CLI agent backends."""

    # Identical to AIChatWorker so the existing AIChatWindow slots connect 1:1.
    response_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    token_received = pyqtSignal(str)
    tool_started = pyqtSignal(str, str, str)   # call_id, tool_name, args_json
    tool_log = pyqtSignal(str, str)            # call_id, line
    tool_completed = pyqtSignal(str, bool, str)  # call_id, ok, result_text

    CLI_NAME = ""        # executable, e.g. "claude"
    DISPLAY_NAME = ""    # human label, e.g. "Claude Code"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session_id = ""          # set by AIChatWindow._make_worker
        self._backend_session_id = ""
        self._cli_session_id = ""      # CLI-side conversation id (for resume)
        self._cli_started = False
        self._stopping = False         # shutdown flag (mirrors AIChatWorker)
        self._proc = None
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
        """Terminate the running subprocess (called from the GUI thread)."""
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @pyqtSlot(str, str)
    def run_request(self, text: str, model_id: str):
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
                text=True, bufsize=1, env=self._build_env(), cwd=_project_cwd(),
            )
        except Exception as e:
            if not self._stopping:
                self._emit_error("Failed to launch %s: %s" % (self.DISPLAY_NAME, e))
            return

        try:
            for line in self._proc.stdout:
                if self._stopping:
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
            if not self._stopping:
                self._emit_error(str(e))
            return

        if self._stopping:
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
        if not self._stopping:
            self.response_ready.emit(text or "")

    def _emit_error(self, text: str):
        if not self._stopping:
            self.error_occurred.emit(text or "Unknown error.")

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
            "--permission-mode", "bypassPermissions",
        ]
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
