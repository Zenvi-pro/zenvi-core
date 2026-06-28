"""In-app MCP server exposing Zenvi's editing tools to external agent CLIs.

Runs a localhost streamable-HTTP MCP (Model Context Protocol) server *inside* the
running Qt process so external agent CLIs (Claude Code, Codex) can drive the editor
using the very same tools the built-in Zenvi Assistant uses.

Tool definitions are auto-generated from ``AGENT_TOOL_HANDLERS`` (name -> python
function) by introspecting each handler's signature + docstring — there is no separate
schema registry to maintain. Tool calls are dispatched through
:func:`classes.tool_handlers.execute_tool`, which already marshals mutating operations
onto the Qt main thread, so no new thread-safety machinery is required here.

Security: the server binds to ``127.0.0.1`` on an ephemeral port and requires a
per-launch bearer token, so only the CLIs we configure (with that token) can reach it.
"""

from __future__ import annotations

import inspect
import logging
import secrets
import socket
import threading

log = logging.getLogger(__name__)

# Name the external CLIs see; their tools are namespaced as ``mcp__zenvi-editor__<tool>``.
SERVER_NAME = "zenvi-editor"

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
    """Build ``{name, description, inputSchema}`` for every agent tool handler."""
    from classes.tool_handlers import AGENT_TOOL_HANDLERS, humanize_tool_name

    defs = []
    for name, func in AGENT_TOOL_HANDLERS.items():
        description = _first_doc_paragraph(func) or humanize_tool_name(name)
        defs.append({
            "name": name,
            "description": description,
            "inputSchema": _build_input_schema(func),
        })
    return defs


def _free_port(host: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


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
            self.port = _free_port(self.host)
            self.token = secrets.token_urlsafe(24)
            app = self._build_app()

            import uvicorn
            config = uvicorn.Config(app, host=self.host, port=self.port,
                                    log_level="warning", loop="asyncio")
            self._uvicorn = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._uvicorn.run,
                                            name="zenvi-mcp", daemon=True)
            self._thread.start()
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
                     stateless_http=True, json_response=True)

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
            result = await anyio.to_thread.run_sync(lambda: execute_tool(name, args))
            return [types.TextContent(type="text",
                                      text="" if result is None else str(result))]

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
