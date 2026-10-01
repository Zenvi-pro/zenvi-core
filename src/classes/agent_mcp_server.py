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

While it listens, the app's server also advertises itself in a discovery file
(``gui_mcp.json`` / ``headless_mcp.json``, see :mod:`classes.mcp_discovery`).
"""

from __future__ import annotations

import inspect
import logging
import os
import secrets
import socket
import threading
import time

from classes import mcp_discovery

log = logging.getLogger(__name__)

# Name the external CLIs see; their tools are namespaced as ``mcp__zenvi-editor__<tool>``.
SERVER_NAME = "zenvi-editor"

# Returned in the MCP ``initialize`` response, so every current and future
# runner (Claude Code, Codex, ...) inherits it without a per-CLI prompt flag.
# The built-in Zenvi Assistant bakes watch into its place/slice workflows; CLI
# harnesses do not run those, so they must watch their own edits explicitly.
SERVER_INSTRUCTIONS = (
    "These tools drive a live video editor. Every tool returns a JSON receipt "
    "with contract=3: status (applied|unchanged|refused|error), summary, clips, "
    "shifted, removedClipIds, createdTracks, watchSuggested, undoSteps. After a "
    "successful mutation (status=applied), patch your timeline state from the "
    "receipt — do not re-call get_timeline_state_tool unless notes say track "
    "indexes shifted. If watchSuggested is non-null, call inspect_timeline_tool "
    "with that object (clipId/start/end) — or with startFrame/endFrame — before "
    "claiming the edit is correct; you will receive composited JPEG frames with "
    "a 0–1 top-left coordinate grid. Use inspect_media_tool (prefer overview=true "
    "first on long files) before describing footage; never guess from filenames. "
    "Do not shell ffmpeg for frames. Do not use watch_clip_window_tool for "
    "verification — that path is Assistant place/slice backend confirm and does "
    "not return images to you. To cut at a known source time, pass start_seconds "
    "(and end_seconds for a range) to slice_clip_at_best_match_tool. After "
    "delete_from_timeline_tool, check the receipt removedClipIds. If inspect shows "
    "the edit is wrong, undo_tool and retry. "
    "SPEECH WORKFLOW: You HAVE on-device transcription. For any dialogue, "
    "transcript, filler-word, or caption request call get_transcript_tool "
    "(never suggest Whisper/Rev/Otter/external ASR). It returns spoken words "
    "in project frames (never put ASR inside inspect_*). On macOS, engine=auto "
    "prefers Apple SpeechAnalyzer when the helper is present (macOS 26+); "
    "Windows and older Macs use faster-whisper. Pass engine=whisper or "
    "engine=apple to pin. Tighten pacing with remove_silence_tool "
    "first, then remove_words_tool (pass transcriptGeneration; fillerPreset "
    "um_uh is allowed). After cuts, call get_transcript_tool again — stale "
    "indices are refused. add_captions_tool burns timed dialogue; "
    "export_captions_tool writes SRT/VTT. diarize_media_tool labels speakers "
    "offline. detect_beats_tool finds music beats. search_media_local_tool is "
    "on-device visual search; search_clips_tool remains the cloud TwelveLabs tier."
)

# Preferred port: stable across restarts so a CLI registered once (e.g.
# ``claude mcp add zenvi --transport http http://127.0.0.1:7434/mcp``) keeps
# working. Falls back to an ephemeral port if taken — only the terminal-attach
# path degrades in that case (it needs a known port to register against);
# Zenvi-driven runners still work since they read the live port from this
# same server instance.
PREFERRED_PORT = 7434

# Handler params that should never be exposed to the agent.
_HIDDEN_PARAMS = {"self", "chat_session_id", "transaction_id"}


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
    """Fallback introspection schema (CI only). Prefer TOOL_SCHEMAS."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {"type": "object", "additionalProperties": False}

    props: dict = {}
    required: list = []
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if name in _HIDDEN_PARAMS:
            continue
        props[name] = {"type": _param_json_type(param)}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _first_doc_paragraph(func) -> str:
    doc = inspect.getdoc(func) or ""
    para = doc.split("\n\n", 1)[0].strip()
    return " ".join(para.split())


def iter_tool_defs() -> list:
    """Build ``{name, description, inputSchema}`` for every tool we expose.

    ``TOOL_SCHEMAS`` is the source of truth. Introspection is only used when a
    tool is listed in ``UNSCHEMATIZED`` (should be empty in production).
    """
    from classes.tool_handlers import AGENT_TOOL_HANDLERS, humanize_tool_name
    from classes.agent_tools.schema import UNSCHEMATIZED, get_schema

    defs = []
    for name, func in list(AGENT_TOOL_HANDLERS.items()) + list(_extra_tools().items()):
        description = _first_doc_paragraph(func) or humanize_tool_name(name)
        schema = get_schema(name)
        if schema is None:
            if name in UNSCHEMATIZED or name not in AGENT_TOOL_HANDLERS:
                # MCP extras may not be in TOOL_SCHEMAS yet — introspect.
                schema = _build_input_schema(func)
            else:
                raise RuntimeError(
                    f"Tool {name!r} is registered but has no TOOL_SCHEMAS entry"
                )
        defs.append({
            "name": name,
            "description": description,
            "inputSchema": schema,
        })
    return defs


# Tools a launch mode adds on top of MCP_EXTRA_TOOLS -- the headless session's
# shutdown_headless_tool. The advertised tool list is built once in start(), so
# register before the server starts.
_REGISTERED_EXTRA_TOOLS: dict = {}


def register_extra_tool(name: str, func) -> None:
    """Expose *func* as MCP tool *name*, called like the other extras (on a
    worker thread, never through execute_tool)."""
    _REGISTERED_EXTRA_TOOLS[name] = func


def _extra_tools() -> dict:
    """Non-editor tools exposed only over MCP, keyed by tool name."""
    tools = {}
    try:
        from classes.agent_api_proxy import MCP_EXTRA_TOOLS
        tools.update(MCP_EXTRA_TOOLS)
    except Exception:
        log.debug("MCP extra tools unavailable", exc_info=True)
    tools.update(_REGISTERED_EXTRA_TOOLS)
    return tools


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


class _ProjectLoadWatcher:
    """UpdateManager listener: a ``load`` action means the project was replaced
    (File > Open, new_project_tool, ...), so the advertised path may be stale."""

    def __init__(self, on_load):
        self._on_load = on_load

    def changed(self, action):
        # UpdateManager stops notifying the remaining listeners when one
        # raises, so this one never does.
        try:
            if getattr(action, "type", None) == "load":
                self._on_load()
        except Exception:
            log.debug("Could not refresh the MCP discovery file", exc_info=True)


def _current_project_path() -> str | None:
    try:
        from classes.app import get_app
        path = getattr(getattr(get_app(), "project", None), "current_filepath", None)
    except Exception:
        log.warning("Could not read the open project's path for the MCP discovery file",
                    exc_info=True)
        return None
    return path if isinstance(path, str) and path else None


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
        # Off unless the app opts in (enable_discovery): a server started by a
        # test must never overwrite the discovery file of a real running app.
        self._discovery_file: str | None = None
        self._discovery_lock = threading.Lock()
        self._published_project: str | None = None
        self._watching_project = False

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
        if self._discovery_file:
            self._watch_project()
            self._publish_discovery(_current_project_path())
        return self

    def _connect_shutdown_hook(self):
        try:
            from qt_api import QApplication
            qapp = QApplication.instance()
            if qapp is not None:
                qapp.aboutToQuit.connect(self.stop)
        except Exception:
            pass

    # -- discovery file ----------------------------------------------------
    def set_discovery_file(self, path: str | None, on_written=None) -> None:
        """Advertise this server in *path* while it listens (None: don't).

        Set before start() it takes effect when the server comes up; set on a
        running server it is written right away, and *on_written* is called
        (on the writer thread) once the file is on disk.
        """
        self._discovery_file = path
        if path and self._started:
            self._watch_project()
            self._publish_discovery(_current_project_path(), on_written)

    def refresh_discovery(self) -> None:
        """Rewrite the discovery file if the open project changed.

        Cheap enough for the GUI thread: it compares one path and hands the
        write to a worker thread.
        """
        if not self._discovery_file or not self._started:
            return
        project = _current_project_path()
        if project != self._published_project:
            self._publish_discovery(project)

    def _publish_discovery(self, project: str | None, on_written=None) -> None:
        self._published_project = project
        # File I/O stays off the GUI thread; the file is tiny, but every
        # volume counts as slow (AGENTS.md).
        threading.Thread(target=self._write_discovery, args=(on_written,),
                         name="zenvi-mcp-discovery", daemon=True).start()

    def _write_discovery(self, on_written=None) -> None:
        # Serialized with _remove_discovery: a write that loses the race with
        # stop() finds the server stopped and does nothing, so no file is left
        # behind pointing at a closed port. The payload is read here, not when
        # the write was queued, so the last write always carries the latest path.
        with self._discovery_lock:
            path = self._discovery_file
            if not path or not self._started:
                return
            from classes import info
            payload = mcp_discovery.build_payload(
                self.url(), _token_path(), os.getpid(), _current_project_path(), info.VERSION)
            try:
                mcp_discovery.write(path, payload)
            except OSError:
                log.warning("Could not write the MCP discovery file %s", path, exc_info=True)
                return
        if on_written is not None:
            on_written(path)

    def _remove_discovery(self) -> None:
        with self._discovery_lock:
            path = self._discovery_file
            if not path:
                return
            try:
                mcp_discovery.remove(path, os.getpid())
            except OSError:
                log.warning("Could not remove the MCP discovery file %s", path, exc_info=True)

    def _watch_project(self) -> None:
        """Keep the file's ``project`` current: a load replaces the project,
        a Save As renames it (projectChanged)."""
        if self._watching_project:
            return
        self._watching_project = True
        try:
            from classes.app import get_app
            app = get_app()
            app.updates.add_listener(_ProjectLoadWatcher(self.refresh_discovery))
            window = getattr(app, "window", None)
            if window is not None:
                window.projectChanged.connect(lambda _path: self.refresh_discovery())
        except Exception:
            log.debug("MCP discovery file will not follow project changes", exc_info=True)

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
            from classes.agent_tools.output import ToolOutput, mcp_content, wrap_str_result
            from classes.tool_handlers import execute_tool_rich
            args = dict(arguments or {})

            extra = _extra_tools().get(name)
            if extra is not None:
                # Called directly on the worker thread rather than through
                # execute_tool, which marshals to the GUI thread — these tools
                # do network I/O and would freeze the UI for their duration.
                result = await anyio.to_thread.run_sync(lambda: extra(**args))
                if isinstance(result, ToolOutput):
                    return mcp_content(result)
                text = "" if result is None else str(result)
                return mcp_content(wrap_str_result(name, text))

            output = await anyio.to_thread.run_sync(
                lambda: execute_tool_rich(name, args)
            )
            if isinstance(output, ToolOutput):
                return mcp_content(output)
            text = "" if output is None else str(output)
            return mcp_content(wrap_str_result(name, text))

        app = fm.streamable_http_app()
        app.add_middleware(_BearerAuthMiddleware, token=self.token)
        return app

    def stop(self):
        thread = None
        with self._lock:
            if not self._started:
                return
            self._started = False
            thread = self._thread
        # Withdraw the advertisement before the port closes, so no CLI is
        # handed a URL that is about to stop answering.
        self._remove_discovery()
        try:
            if self._uvicorn is not None:
                self._uvicorn.should_exit = True
        except Exception:
            pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None
            self._uvicorn = None

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


def enable_discovery(kind: str, on_written=None) -> str:
    """Have the process-wide server advertise itself in
    ``~/.openshot_qt/<kind>_mcp.json`` while it listens; returns that path.

    launch.py calls this for the desktop window before the server starts; a
    headless session calls it once its project is open, with *on_written*
    to hear when the file is on disk.
    """
    from classes import info
    path = mcp_discovery.discovery_path(info.USER_PATH, kind)
    get_mcp_server().set_discovery_file(path, on_written)
    return path
