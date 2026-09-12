"""In-app MCP server exposing Zenvi's editing tools to external agent CLIs.

Runs a localhost streamable-HTTP MCP (Model Context Protocol) server *inside* the
running Qt process so external agent CLIs (Claude Code, Codex) can drive the editor
using the very same tools the built-in Zenvi Assistant uses.

Tool definitions are auto-generated from ``AGENT_TOOL_HANDLERS`` (name -> python
function) by introspecting each handler's signature + docstring — there is no separate
schema registry to maintain. Tool calls are dispatched through
:func:`classes.tool_handlers.execute_tool`, which already marshals mutating operations
onto the Qt main thread, so no new thread-safety machinery is required here.

Security: the server binds to ``127.0.0.1`` and requires a bearer token (persisted
across restarts, see ``_load_or_create_token``), so only the CLIs we configure
(with that token) can reach it.
"""

from __future__ import annotations

import inspect
import logging
import os
import secrets
import socket
import threading
import time

log = logging.getLogger(__name__)

# Name the external CLIs see; their tools are namespaced as ``mcp__zenvi-editor__<tool>``.
SERVER_NAME = "zenvi-editor"

# Returned in the MCP ``initialize`` response, so every current and future
# runner (Claude Code, Codex, ...) inherits it without a per-CLI prompt flag.
# The built-in Zenvi Assistant bakes watch into its place/slice workflows; CLI
# harnesses do not run those, so they must watch their own edits explicitly.
SERVER_INSTRUCTIONS = (
    "These tools drive a live video editor. After any edit that changes what is "
    "on the timeline (add_clip_to_timeline_tool, slice_clip_at_best_match_tool, "
    "slice_clip_at_playhead_tool, modify_clip_tool, place_motion_graphic_tool, "
    "apply_transition_tool, remove_clip_tool), call watch_clip_window_tool on the "
    "affected clip to confirm the result with vision - is the intended moment on "
    "screen, did the cut land cleanly, is album art covering video. Feed the "
    "in/out it returns back into a slice/placement call to tighten a bad cut, or "
    "undo_tool if the edit is wrong. A watch that cannot run degrades to 'no "
    "match' and never blocks you."
)

# Preferred port: stable across restarts so a CLI registered once (e.g.
# ``claude mcp add zenvi --transport http http://127.0.0.1:7434/mcp``) keeps
# working. Falls back to an ephemeral port if taken — only the terminal-attach
# path degrades in that case (it needs a known port to register against);
# Zenvi-driven runners still work since they read the live port from this
# same server instance.
PREFERRED_PORT = 7434

# Handler params that should never be exposed to the agent.
_HIDDEN_PARAMS = {"self", "chat_session_id"}


def _param_json_type(param) -> str:
    """Best-effort JSON-schema type for a python parameter (annotation, then default)."""
    ann = param.annotation
    mapping = {str: "string", bool: "boolean", int: "integer", float: "number",
               list: "array", dict: "object"}
    if ann is not inspect.Parameter.empty and ann in mapping:
        return mapping[ann]
    default = param.default
    if default not in (inspect.Parameter.empty, None):
        return mapping.get(type(default), "string")
    return "string"


def _build_input_schema(func) -> dict:
    """Derive a JSON schema for a tool handler from its signature.

    Handlers that only accept ``**kwargs`` (the common case) get a permissive
    object schema so the agent can pass whatever the tool documents.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {"type": "object", "additionalProperties": True}

    props: dict = {}
    required: list = []
    has_var_keyword = False
    for name, param in sig.parameters.items():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if param.kind == inspect.Parameter.VAR_POSITIONAL or name in _HIDDEN_PARAMS:
            continue
        props[name] = {"type": _param_json_type(param)}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    if not props:
        return {"type": "object", "additionalProperties": True}

    schema = {"type": "object", "properties": props,
              "additionalProperties": has_var_keyword}
    if required:
        schema["required"] = required
    return schema


def _first_doc_paragraph(func) -> str:
    doc = inspect.getdoc(func) or ""
    para = doc.split("\n\n", 1)[0].strip()
    return " ".join(para.split())


def iter_tool_defs() -> list:
    """Build ``{name, description, inputSchema}`` for every tool we expose.

    The editor tools come straight from ``AGENT_TOOL_HANDLERS``; the extras are
    tools that only make sense for an external agent CLI (see MCP_EXTRA_TOOLS).
    """
    from classes.tool_handlers import AGENT_TOOL_HANDLERS, humanize_tool_name

    defs = []
    for name, func in list(AGENT_TOOL_HANDLERS.items()) + list(_extra_tools().items()):
        description = _first_doc_paragraph(func) or humanize_tool_name(name)
        defs.append({
            "name": name,
            "description": description,
            "inputSchema": _build_input_schema(func),
        })
    return defs


def _extra_tools() -> dict:
    """Non-editor tools exposed only over MCP, keyed by tool name."""
    try:
        from classes.agent_api_proxy import MCP_EXTRA_TOOLS
        return MCP_EXTRA_TOOLS
    except Exception:
        log.debug("MCP extra tools unavailable", exc_info=True)
        return {}


def _free_port(host: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _bind_port(host: str, preferred: int) -> int:
    """Prefer *preferred* (stable across restarts); fall back to an ephemeral
    port if it's already taken."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, preferred))
        return preferred
    except OSError:
        log.warning(
            "Port %d unavailable, falling back to an ephemeral port "
            "(the terminal-attach path will need reconnecting; Zenvi-driven "
            "agents still work)", preferred,
        )
        return _free_port(host)
    finally:
        s.close()


def _token_path() -> str:
    from classes import info
    return os.path.join(info.USER_PATH, "mcp_token")


def _load_or_create_token() -> str:
    """Persist the bearer token across restarts so a CLI registered once
    keeps working after Zenvi restarts, instead of needing to re-register
    every launch."""
    path = _token_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except Exception:
        pass
    token = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Created 0600 in one step rather than chmod'ed afterwards: a plain
        # open() applies the umask first (usually 0644), leaving the bearer
        # token world-readable for the window in between.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
    except Exception:
        log.debug("Failed to persist MCP token", exc_info=True)
    return token


class _BearerAuthMiddleware:
    """Reject any HTTP request lacking ``Authorization: Bearer <token>``."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode("latin-1")
        if provided != f"Bearer {self.token}":
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.app(scope, receive, send)


class ZenviMcpServer:
    """Lazily-started localhost MCP server backed by ``execute_tool``."""

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port: int | None = None
        self.token: str | None = None
        self._started = False
        self._lock = threading.Lock()
        self._uvicorn = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "ZenviMcpServer":
        """Start the server once (idempotent). Safe to call from any thread."""
        with self._lock:
            if self._started:
                return self
            self.port = _bind_port(self.host, PREFERRED_PORT)
            self.token = _load_or_create_token()
            app = self._build_app()

            import uvicorn
            # log_config=None: don't let uvicorn reconfigure global logging —
            # the app already has its own setup, and reconfiguring collides
            # with it under some startup timings (e.g. "Unable to configure
            # formatter 'default'"). Standard guidance for embedding uvicorn
            # inside a larger app.
            config = uvicorn.Config(app, host=self.host, port=self.port,
                                    log_level="warning", loop="asyncio",
                                    log_config=None)
            self._uvicorn = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._uvicorn.run,
                                            name="zenvi-mcp", daemon=True)
            self._thread.start()

            # uvicorn binds inside the worker thread, and _bind_port only
            # probed the port -- another process can have taken it in between.
            # Wait for the real bind so a lost race raises here instead of
            # handing the CLI a dead URL and a generic connection error.
            deadline = time.monotonic() + 5.0
            while not self._uvicorn.started and self._thread.is_alive():
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            if not self._uvicorn.started:
                self._uvicorn.should_exit = True
                self._uvicorn = None
                self._thread = None
                raise RuntimeError(
                    "MCP server failed to bind %s:%s" % (self.host, self.port))

            self._started = True
            self._connect_shutdown_hook()
            log.info("Zenvi MCP server listening on %s (%d tools)",
                     self.url(), len(iter_tool_defs()))
            return self

    def _connect_shutdown_hook(self):
        try:
            from PyQt5.QtWidgets import QApplication
            qapp = QApplication.instance()
            if qapp is not None:
                qapp.aboutToQuit.connect(self.stop)
        except Exception:
            pass

    def _build_app(self):
        import anyio
        import mcp.types as types
        from mcp.server.fastmcp import FastMCP

        fm = FastMCP(SERVER_NAME, host=self.host, port=self.port,
                     stateless_http=True, json_response=True,
                     instructions=SERVER_INSTRUCTIONS)

        tools = [types.Tool(name=d["name"], description=d["description"],
                            inputSchema=d["inputSchema"]) for d in iter_tool_defs()]

        @fm._mcp_server.list_tools()
        async def _list_tools():
            return tools

        # validate_input=False: schemas are intentionally permissive; let the tool
        # itself report bad args (matching the WebSocket tool path's behaviour).
        @fm._mcp_server.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict):
            from classes.tool_handlers import execute_tool
            args = dict(arguments or {})

            extra = _extra_tools().get(name)
            if extra is not None:
                # Called directly on the worker thread rather than through
                # execute_tool, which marshals to the GUI thread — these tools
                # do network I/O and would freeze the UI for their duration.
                result = await anyio.to_thread.run_sync(lambda: extra(**args))
            else:
                result = await anyio.to_thread.run_sync(lambda: execute_tool(name, args))
            text = "" if result is None else str(result)
            return [types.TextContent(type="text", text=text)]

        app = fm.streamable_http_app()
        app.add_middleware(_BearerAuthMiddleware, token=self.token)
        return app

    def stop(self):
        with self._lock:
            if not self._started:
                return
            self._started = False
        try:
            if self._uvicorn is not None:
                self._uvicorn.should_exit = True
        except Exception:
            pass

    # -- accessors ---------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started

    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"


_server: ZenviMcpServer | None = None
_server_lock = threading.Lock()


def get_mcp_server() -> ZenviMcpServer:
    """Return the process-wide MCP server singleton (not yet started)."""
    global _server
    with _server_lock:
        if _server is None:
            _server = ZenviMcpServer()
        return _server
