import html
import json
import logging
import os
import re
import threading
import time

from PyQt5.QtCore import (
    Qt, QPropertyAnimation, QEasingCurve,
    QObject, QThread, pyqtSignal, pyqtSlot, QMetaObject, Q_ARG,
    QUrl, QFileInfo, QTimer,
)
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QComboBox, QMessageBox, QFrame,
    QGraphicsOpacityEffect, QScrollArea, QToolButton,
)
from PyQt5.QtGui import QColor, QTextCursor

from classes.logger import log
from classes.api_client import get_backend_client
from classes.tool_handlers import humanize_tool_name
from windows.embedded_web import web_embed_backend

# Theme colors for chat CEP UI (match theme QSS). Keys match ThemeName.value.
# Bloomberg Light: high information density, sharp edges, accent #6366F1.
CHAT_THEME_COLORS = {
    "Bloomberg Light": {
        "chat-bg": "#F8FAFC",
        "chat-preamble-bg": "#F1F5F9",
        "chat-text": "#0F172A",
        "chat-border": "#E2E8F0",
        "chat-input-bg": "#FFFFFF",
        "chat-button-bg": "#F1F5F9",
        "chat-button-hover-bg": "#6366F1",
        "chat-accent": "#6366F1",
        "chat-code-bg": "#E2E8F0",
        "chat-placeholder": "rgba(15, 23, 42, 0.5)",
    },
    "Humanity: Dark": {
        "chat-bg": "#191919",
        "chat-preamble-bg": "#252525",
        "chat-text": "#ffffff",
        "chat-border": "#404040",
        "chat-input-bg": "#252525",
        "chat-button-bg": "#353535",
        "chat-button-hover-bg": "#2a82da",
        "chat-accent": "#6366F1",
        "chat-placeholder": "rgba(255, 255, 255, 0.5)",
    },
    "Retro": {
        "chat-bg": "#f0f0f0",
        "chat-preamble-bg": "#e8e8e8",
        "chat-text": "#333333",
        "chat-border": "#ccc",
        "chat-input-bg": "#ffffff",
        "chat-button-bg": "#e8e8e8",
        "chat-button-hover-bg": "#217dd4",
        "chat-accent": "#217dd4",
        "chat-placeholder": "rgba(51, 51, 51, 0.5)",
    },
    "Cosmic Dusk": {
        "chat-bg":              "#161616",
        "chat-surface":         "#1e1e1e",
        "chat-text":            "#d4d4d4",
        "chat-muted":           "#6b7280",
        "chat-placeholder":     "#6b7280",
        "chat-border":          "#2a2a2a",
        "chat-input-bg":        "#1a1a1a",
        "chat-button-bg":       "#252525",
        "chat-button-hover-bg": "#2e2e2e",
        "chat-accent":          "#4d9cf6",
        "chat-code-bg":         "#252525",
    },
}


_LANGCHAIN_BLOCKS_RE = re.compile(
    r"^\s*\[\s*\{\s*['\"]\s*(?:text|type)\s*['\"]\s*:.+\}\s*\]\s*$",
    re.DOTALL,
)


def _join_content_blocks(blocks) -> str:
    """Concatenate the text of a list of LangChain content blocks."""
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            piece = block.get("text") or block.get("content") or ""
            if isinstance(piece, str) and piece:
                parts.append(piece)
    return "".join(parts).strip()


def _parse_content_blocks(text: str):
    """Parse a stringified list/dict of content blocks. Returns a list or None.

    Handles both JSON (double-quoted) and Python ``repr`` (single-quoted) forms.
    """
    import ast
    for parser in (json.loads, ast.literal_eval):
        try:
            obj = parser(text)
        except Exception:
            continue
        if isinstance(obj, dict):
            return [obj]
        if isinstance(obj, list):
            return obj
    return None


def _unwrap_langchain_content(value) -> str:
    """Normalize an assistant reply into a plain markdown string.

    Some providers (Anthropic, Gemini) return ``AIMessage.content`` as a list of
    typed blocks, e.g. ``[{'text': '...', 'type': 'text', 'index': 0}]``. That can
    reach us either as a real list/dict object or as a Python ``repr`` / JSON
    string. In every case we extract and join the ``text`` blocks, otherwise the
    chat renders the raw list literal as a single paragraph. We also decode literal
    ``\\n`` escape sequences that some payloads carry instead of real newlines,
    which would otherwise suppress markdown paragraphs, lists and headings.
    """
    # Real content-block objects that were never stringified.
    if isinstance(value, (list, tuple)):
        return _join_content_blocks(value)
    if isinstance(value, dict):
        return _join_content_blocks([value])
    if not isinstance(value, str):
        return "" if value is None else str(value)

    text = value
    stripped = text.strip()
    # Stringified content-block list/dict -> parse and join the text blocks.
    if stripped[:2] in ("[{", "{'", '{"') or _LANGCHAIN_BLOCKS_RE.match(text):
        parsed = _parse_content_blocks(stripped)
        if parsed is not None:
            joined = _join_content_blocks(parsed)
            if joined:
                text = joined

    # Decode literal escape sequences when the text carries no real newlines.
    if "\\n" in text and "\n" not in text:
        text = (text.replace("\\r\\n", "\n")
                    .replace("\\n", "\n")
                    .replace("\\t", "\t"))
    return text


def _markdown_to_html(text: str) -> str:
    """Convert markdown to HTML suitable for QTextEdit. Uses theme text color for body."""
    text = _unwrap_langchain_content(text)
    try:
        import markdown
        body = markdown.markdown(text, extensions=["extra"])
    except Exception as exc:
        log.warning("_markdown_to_html: markdown render failed: %s", exc)
        body = html.escape(text).replace("\n", "<br/>")
    # Wrap in a div and style code blocks so they don't override theme colors
    # Use 'currentColor' so code inherits the widget's text color
    style = (
        "pre, code { background: rgba(0,0,0,0.15); padding: 4px 6px; border-radius: 0; "
        "font-family: monospace; color: inherit; } "
        "pre { margin: 8px 0; overflow-x: auto; } "
        "pre code { padding: 0; background: transparent; } "
        "p { margin: 4px 0; } "
        "ul, ol { margin: 4px 0 4px 16px; } "
        "strong { font-weight: bold; } "
    )
    return f'<div style="{style}">{body}</div>'


def _plain_to_html(text: str) -> str:
    """Escape plain text for safe HTML display."""
    return "<p>" + html.escape(text).replace("\n", "<br/>") + "</p>"


# Wrapper blocks prepended before sending to the LLM (editor snapshot + legacy clip context).
_CONTEXT_BLOCK_RE = re.compile(
    r"\[(?:Editor snapshot|Selected timeline clip context)\].*?"
    r"\[/(?:Editor snapshot|Selected timeline clip context)\]\s*",
    re.DOTALL,
)


def _strip_context_blocks(text: str) -> str:
    """Remove [Editor snapshot] and legacy [Selected timeline clip context] blocks from text."""
    if not text:
        return text
    return _CONTEXT_BLOCK_RE.sub("", text).lstrip()


def _summarize_prompt(prompt: str, max_words: int = 6) -> str:
    """Ask the backend to summarize the user prompt in a few words. Returns empty on failure."""
    try:
        client = get_backend_client()
        system = (
            "Summarize the following user request in at most %d words. "
            "Reply with only the short phrase, no punctuation, no period."
        ) % max_words
        out = client.send_message_ws(
            message=f"[SYSTEM]{system}[/SYSTEM]\n{prompt}",
            auth_token=client.auth_token(),
        )
        out = (out or "").strip()
        return out[:80] if out else ""
    except Exception:
        return ""


REQUEST_TIMEOUT_SECONDS = 120

# Mirrors backend PLANNING_ALLOWLIST — tools safe to run in Plan mode.
_PLANNING_SAFE_TOOLS = frozenset({
    "get_project_info_tool", "list_files_tool", "list_clips_tool", "list_layers_tool",
    "get_timeline_state_tool", "list_markers_tool", "get_file_info_tool",
    "get_clips_with_full_metadata_tool", "get_timeline_placements_metadata_tool",
    "search_clip_scenes_tool", "search_clips_tool", "search_pexels_videos_tool",
    "search_freesound_music_tool", "list_transitions_tool", "search_transitions_tool",
    "save_edit_plan_tool", "save_planning_research_brief_tool",
    "update_edit_plan_step_tool", "finalize_edit_plan_tool",
    "present_planning_questions_tool",
    "save_edit_checkpoint_tool", "watch_clip_tool",
})


def _is_planning_tool_allowed(tool_name: str) -> bool:
    if not tool_name:
        return False
    if tool_name in _PLANNING_SAFE_TOOLS:
        return True
    if tool_name.startswith("research_") or tool_name.startswith("web_search"):
        return True
    return False


def _format_tool_command(tool_name: str, args: dict) -> str:
    """Build a `$`-style preview line summarising the tool invocation."""
    parts = [tool_name]
    if isinstance(args, dict):
        for k, v in args.items():
            try:
                if isinstance(v, str):
                    if len(v) > 80:
                        v_disp = v[:80] + "…"
                    else:
                        v_disp = v
                    parts.append('%s=%s' % (k, json.dumps(v_disp)))
                elif isinstance(v, (int, float, bool)) or v is None:
                    parts.append('%s=%s' % (k, json.dumps(v)))
                else:
                    s = json.dumps(v, default=str)
                    if len(s) > 80:
                        s = s[:80] + "…"
                    parts.append('%s=%s' % (k, s))
            except Exception:
                parts.append(str(k))
    return " ".join(parts)


class WidgetToolBlock(QFrame):
    """Collapsible tool-run block for native Qt chat (mirrors chat.js tool blocks)."""

    def __init__(self, call_id: str, title: str, cmd: str, parent=None):
        super().__init__(parent)
        self.call_id = call_id
        self._title = title
        self._cmd = cmd
        self._expanded = True
        self._running = True

        self.setObjectName("chatToolBlock")
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        self._header_btn = QToolButton()
        self._header_btn.setObjectName("chatToolHeader")
        self._header_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._header_btn.setAutoRaise(True)
        self._header_btn.clicked.connect(self._toggle_expanded)
        self._refresh_header()
        outer.addWidget(self._header_btn)

        self._body = QTextEdit()
        self._body.setObjectName("chatToolBody")
        self._body.setReadOnly(True)
        self._body.setMaximumHeight(180)
        self._body.setLineWrapMode(QTextEdit.NoWrap)
        mono = self._body.font()
        mono.setFamily("Consolas")
        mono.setPointSize(9)
        self._body.setFont(mono)
        self._body.setStyleSheet("background: #1a1a1a; color: #c8c8c8; border: none;")
        outer.addWidget(self._body)

    def _refresh_header(self):
        chevron = "▼" if self._expanded else "▶"
        prefix = "… " if self._running else ("✓ " if getattr(self, "_ok", True) else "✗ ")
        cmd_part = self._cmd
        if len(cmd_part) > 72:
            cmd_part = cmd_part[:72] + "…"
        self._header_btn.setText(f"{prefix}{chevron}  {self._title}  {cmd_part}")

    def _toggle_expanded(self):
        if self._running:
            return
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._refresh_header()

    def append_log(self, line: str):
        if line:
            self._body.append(line.rstrip("\n"))

    def complete(self, ok: bool, summary: str):
        self._running = False
        self._ok = ok
        if summary:
            self._cmd = summary
        self._expanded = False
        self._body.setVisible(False)
        self._refresh_header()


# Modules whose log records get attached to a running tool block. Anything
# outside this allow-list (and `ai_*`) is treated as unrelated background noise.
_TOOL_LOG_ALLOW_MODULES = frozenset({
    "zenvi_backend",
    "project_data",
    "main_window",
    "timeline",
    "tool_handlers",
    "track_display",
    "import_files",
    "preview_thread",
    "openshot_tools",
    "ai_openshot_tools",
    "ai_agent_runner",
    "ai_chat_ui",
})


class _ToolLogCapture:
    """Forwards relevant Python log records to one or more active tool calls.

    Multiple tool calls can run concurrently (the WS layer fans them out).
    A single shared ``_SharedToolHandler`` is attached to the OpenShot logger;
    each call that wants log output registers itself, and the handler
    dispatches every record to all currently-registered callbacks.
    """

    _shared_lock = threading.Lock()
    _shared_handler = None
    _active = {}  # call_id -> callback(call_id, line)

    def __init__(self, call_id: str, callback):
        self._call_id = call_id
        self._callback = callback

    @classmethod
    def _ensure_attached(cls):
        if cls._shared_handler is not None:
            return
        handler = _SharedToolHandler()
        handler.setLevel(logging.INFO)
        logging.getLogger("OpenShot").addHandler(handler)
        cls._shared_handler = handler

    @classmethod
    def _maybe_detach(cls):
        if cls._shared_handler is None:
            return
        if cls._active:
            return
        try:
            logging.getLogger("OpenShot").removeHandler(cls._shared_handler)
        except Exception:
            pass
        cls._shared_handler = None

    def install(self):
        with self._shared_lock:
            self._ensure_attached()
            self._active[self._call_id] = self._callback

    def uninstall(self):
        with self._shared_lock:
            self._active.pop(self._call_id, None)
            self._maybe_detach()


class _SharedToolHandler(logging.Handler):
    """Singleton handler that dispatches each filtered record to active calls."""

    def emit(self, record):
        try:
            module = getattr(record, "module", "") or ""
            if module not in _TOOL_LOG_ALLOW_MODULES and not module.startswith("ai_"):
                return
            try:
                msg = record.getMessage()
            except Exception:
                return
            if len(msg) > 500:
                msg = msg[:500] + "... [truncated]"
            line = "%s %s: %s" % (record.levelname, module, msg)
            with _ToolLogCapture._shared_lock:
                callbacks = list(_ToolLogCapture._active.items())
            for call_id, cb in callbacks:
                try:
                    cb(call_id, line)
                except Exception:
                    pass
        except Exception:
            pass


# Agent backends selectable from the top of the chat panel. "zenvi" is the
# built-in WebSocket assistant (unchanged); the others drive external agent CLIs.
BACKEND_ZENVI = "zenvi"
BACKEND_CLAUDE = "claude_code"
BACKEND_CODEX = "codex"
BACKENDS = [
    {"id": BACKEND_ZENVI, "name": "Zenvi Assistant"},
    {"id": BACKEND_CLAUDE, "name": "Claude Code"},
    {"id": BACKEND_CODEX, "name": "Codex"},
]
_VALID_BACKENDS = {b["id"] for b in BACKENDS}


def _coerce_backend(value) -> str:
    return value if value in _VALID_BACKENDS else BACKEND_ZENVI


class AIChatWorker(QObject):
    """Sends chat messages to the zenvi-backend API server in a background thread.

    Uses WebSocket for bidirectional communication: the backend can delegate
    tool calls (e.g. timeline operations) back to the frontend for execution.

    Emits *response_ready* with the assistant reply or *error_occurred* on failure.
    """

    response_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    token_received = pyqtSignal(str)
    tool_started = pyqtSignal(str, str, str)   # call_id, tool_name, args_json
    tool_log = pyqtSignal(str, str)            # call_id, line
    tool_completed = pyqtSignal(str, bool, str)  # call_id, ok, result_text
    plan_event = pyqtSignal(str, str)          # event_type, payload_json

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backend_session_id = None
        self._stopping = False  # Set to True during app shutdown to suppress fallback/emit

    @pyqtSlot(str, str, str, str, str)
    def run_request(self, text: str, model_id: str, agent_mode: str = "agent", action: str = "chat", plan_id: str = ""):
        """Send the user message to the backend via WebSocket (with tool delegation)."""
        self._agent_mode = agent_mode or "agent"
        try:
            from classes.tool_handlers import execute_tool

            client = get_backend_client()

            last_tool_result = None

            def on_tool_call(tool_name, tool_args, call_id):
                """Execute a tool locally and return the result."""
                nonlocal last_tool_result
                if getattr(self, "_agent_mode", "agent") == "planning" and not _is_planning_tool_allowed(tool_name):
                    return (
                        "Error: Planning mode — this tool is blocked. "
                        "Add it as a plan step instead."
                    )
                log.info("Tool delegated from backend: %s", tool_name)
                args = dict(tool_args or {})
                # Args displayed to the user shouldn't leak the chat session id.
                args_for_ui = {k: v for k, v in args.items() if k != "chat_session_id"}
                try:
                    self.tool_started.emit(call_id or "", tool_name or "", json.dumps(args_for_ui, default=str))
                except Exception:
                    pass

                # Ensure tool state that depends on the chat/request identity
                # (e.g. split→add_clip chains) is isolated per UI tab/session.
                if self._backend_session_id:
                    args["chat_session_id"] = self._backend_session_id

                capture = _ToolLogCapture(
                    call_id or tool_name or "tool",
                    lambda cid, line: self.tool_log.emit(cid, line),
                )
                capture.install()
                try:
                    result = execute_tool(tool_name, args)
                finally:
                    capture.uninstall()

                text = str(result) if result is not None else ""
                ok = bool(text) and not text.startswith("Error")
                try:
                    self.tool_completed.emit(call_id or "", ok, text)
                except Exception:
                    pass

                # Remember the last successful tool result so we can use it
                # if the WebSocket breaks after the tool already completed.
                if ok:
                    last_tool_result = result
                    if tool_name == "split_file_add_clip_tool":
                        QMetaObject.invokeMethod(
                            self,
                            "clear_session",
                            Qt.QueuedConnection,
                        )
                return result

            final_response = None
            final_error = None

            def on_response(response_text, session_id):
                nonlocal final_response
                final_response = response_text
                self._backend_session_id = session_id or self._backend_session_id

            def on_error(error_message):
                nonlocal final_error
                final_error = error_message

            def on_token(text):
                if text and not self._stopping:
                    self.token_received.emit(text)

            def on_tool_progress(kind, call_id, tool_name, payload):
                if self._stopping:
                    return
                if kind == "started":
                    args_json = json.dumps(payload or {}, default=str)
                    try:
                        self.tool_started.emit(call_id or "", tool_name or "", args_json)
                    except Exception:
                        pass
                    return
                if kind == "completed":
                    data = payload if isinstance(payload, dict) else {}
                    try:
                        self.tool_completed.emit(
                            call_id or "",
                            bool(data.get("ok")),
                            str(data.get("result", "")),
                        )
                    except Exception:
                        pass
                    return
                # progress: payload may be a plain line or {line, detail}
                line = ""
                detail = {}
                if isinstance(payload, dict):
                    line = str(payload.get("line") or "")
                    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
                else:
                    line = str(payload or "")
                if detail:
                    bits = []
                    if detail.get("phase"):
                        bits.append(str(detail["phase"]))
                    if detail.get("title"):
                        bits.append(str(detail["title"])[:40])
                    if detail.get("label"):
                        bits.append(str(detail["label"])[:40])
                    if detail.get("tool"):
                        bits.append(str(detail["tool"])[:40])
                    if detail.get("block_id"):
                        bits.append(str(detail["block_id"]))
                    if detail.get("query") and not detail.get("block_id"):
                        bits.append(str(detail["query"])[:40])
                    if detail.get("file_id"):
                        bits.append(f"file={detail['file_id']}")
                    if bits:
                        base = line or "beat"
                        line = f"{base} · " + " · ".join(bits)
                if line:
                    try:
                        self.tool_log.emit(call_id or "", line)
                    except Exception:
                        pass

            def on_plan_event(event_type, payload):
                if self._stopping:
                    return
                try:
                    self.plan_event.emit(event_type, json.dumps(payload or {}, default=str))
                except Exception:
                    pass

            result = client.send_message_ws(
                message=text,
                model_id=model_id or None,
                session_id=self._backend_session_id,
                on_tool_call=on_tool_call,
                on_response=on_response,
                on_error=on_error,
                on_token=on_token,
                on_tool_progress=on_tool_progress,
                auth_token=client.auth_token(),
                agent_mode=agent_mode or "agent",
                action=action or "chat",
                plan_id=plan_id or None,
                on_plan_event=on_plan_event,
            )

            if final_error:
                # App is shutting down — the WS was closed intentionally.
                # Don't fall back to REST or emit signals into a dying Qt stack.
                if self._stopping:
                    return

                # If a tool already executed successfully (e.g. v2v clip
                # was generated and imported), use that result instead of
                # falling back to REST which would re-run the entire agent.
                if last_tool_result:
                    log.info("WebSocket failed (%s) but tool already succeeded, using tool result", final_error)
                    self.response_ready.emit(last_tool_result)
                    return
                if final_response:
                    log.info("WebSocket failed (%s) but response already received", final_error)
                    self.response_ready.emit(final_response)
                    return
                self.error_occurred.emit(final_error or "Chat connection failed.")
                return

            if self._stopping:
                return
            self._log_tool_gap_if_any(text)
            if result is not None:
                self.response_ready.emit(result)
            elif final_response is not None:
                self.response_ready.emit(final_response)
            else:
                self.error_occurred.emit("No response from backend.")
        except Exception as e:
            if self._stopping:
                return  # Swallow exceptions during shutdown — Qt stack is going away
            log.error("AI chat error: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))

    def _log_tool_gap_if_any(self, request_text: str):
        """Silently record a missing-capability gap, if this request needed one.

        Runs on this worker's own thread (never the GUI thread), so it never
        blocks the UI. The chat response itself is unaffected either way.
        """
        try:
            from classes.agent_gap_log import classify_gap, append_gap
            gap = classify_gap(request_text)
            if gap:
                append_gap({
                    "request": request_text,
                    "missing_capability": gap,
                    "session_id": getattr(self, "_session_id", "") or "",
                })
        except Exception:
            log.debug("tool-gap logging failed", exc_info=True)

    @pyqtSlot()
    def clear_session(self):
        """Clear the backend chat session."""
        if self._backend_session_id:
            try:
                get_backend_client().clear_chat_session(self._backend_session_id)
            except Exception:
                pass
            # Keep the backend session_id stable for this UI tab/session.
            # Clearing only resets conversation state + Supabase memory rows.


class ChatBridge(QObject):
    """QWebChannel bridge: exposes sendMessage, cancelRequest, clearChat to the CEP chat UI."""

    def __init__(self, window=None, parent=None):
        super().__init__(parent)
        self.window = window

    @pyqtSlot(str, str, str)
    def sendMessage(self, text: str, model_id: str, agent_mode: str = ""):
        if self.window:
            mode = agent_mode if agent_mode in ("planning", "agent") else None
            self.window._handle_web_send_message(text.strip(), model_id or "", mode)

    @pyqtSlot(str, str)
    def executePlan(self, plan_id: str, model_id: str):
        if self.window:
            self.window._execute_plan(plan_id or "", model_id or "")

    @pyqtSlot()
    def executePlanNoArgs(self):
        if self.window:
            self.window._execute_plan("", "")

    @pyqtSlot()
    def editPlanInPlanningMode(self):
        if self.window:
            self.window._edit_plan_in_planning_mode()

    @pyqtSlot()
    def openPlanDock(self):
        if not self.window:
            return
        main_win = self.window.parent()
        dock = getattr(main_win, "dockPlan", None)
        if dock:
            sess = self.window._active_session()
            plan = (sess or {}).get("current_plan")
            if plan:
                try:
                    dock.load_plan(plan)
                except Exception:
                    pass
            dock.show()
            dock.raise_()

    @pyqtSlot()
    def openAgentTrace(self):
        if self.window:
            self.window.open_agent_trace()

    @pyqtSlot(str)
    def submitPlanAnswers(self, answers_json: str):
        if not self.window:
            return
        try:
            data = json.loads(answers_json) if answers_json else {}
        except Exception:
            data = {}
        if data.get("skip"):
            text = "[Plan answers] skip — use your best judgment from the clips."
        else:
            lines = ["[Plan answers]"]
            answers = data.get("answers") or {}
            if isinstance(answers, dict):
                for key, val in answers.items():
                    if val:
                        lines.append(f"- {key}: {val}")
            extra = str(data.get("notes") or "").strip()
            if extra:
                lines.append(f"- notes: {extra}")
            text = "\n".join(lines) if len(lines) > 1 else "[Plan answers] (no changes)"
        model_id = ""
        if hasattr(self.window, "model_combo") and self.window.model_combo:
            model_id = self.window.model_combo.currentData() or ""
        # Always treat as answering pending questions so the processing gate cannot block Skip/Submit.
        sess = self.window._active_session()
        if sess is not None:
            sess["pending_plan_questions"] = sess.get("pending_plan_questions") or [{"id": "_"}]
            sess["awaiting_plan_answers"] = False
        # If a prior planning turn is still winding down, force-clear processing so answers can send.
        if self.window.is_processing:
            self.window._set_processing_ui(False)
        self.window._dispatch_user_message(text, model_id, agent_mode="planning")

    @pyqtSlot(str)
    def setAgentMode(self, agent_mode: str):
        if self.window:
            self.window._set_agent_mode(agent_mode or "agent")

    @pyqtSlot()
    def cancelRequest(self):
        if self.window:
            self.window.cancel_request()

    @pyqtSlot()
    def clearChat(self):
        if self.window:
            self.window.clear_chat()

    @pyqtSlot()
    def ready(self):
        """Called from JS when QWebChannel is ready; push initial state."""
        if self.window and getattr(self.window, "_chat_web_ready", None):
            self.window._chat_web_ready()

    @pyqtSlot(str, str)
    def createSession(self, model_id: str, backend: str = ""):
        if self.window:
            self.window._create_session(model_id, backend or "zenvi")

    @pyqtSlot(str, str)
    def setBackend(self, session_id: str, backend: str):
        if self.window:
            self.window._set_session_backend(session_id, backend)

    @pyqtSlot(str)
    def switchSession(self, session_id: str):
        if self.window:
            self.window._switch_session(session_id)

    @pyqtSlot(str)
    def closeSession(self, session_id: str):
        if self.window:
            self.window._close_session(session_id)

    @pyqtSlot()
    def getGaps(self):
        if self.window:
            self.window._push_gap_list()

    @pyqtSlot(str)
    def resolveGap(self, entry_id: str):
        if self.window:
            self.window._resolve_gap(entry_id)

    @pyqtSlot(str)
    def deleteGap(self, entry_id: str):
        if self.window:
            self.window._delete_gap(entry_id)

    @pyqtSlot(str)
    def connectCli(self, backend_id: str):
        if self.window:
            self.window._connect_cli(backend_id)


class AIChatWindow(QDockWidget):
    """Zenvi Assistant chat dock. Supports markdown in assistant replies and matches app theme."""

    def __init__(self, parent=None):
        # Titled "Agents": the top selector chooses the backend (Zenvi Assistant,
        # Claude Code, Codex). objectName is kept stable so saved dock-state /
        # restoreState blobs continue to resolve this dock.
        super().__init__("Agents", parent)
        self.setObjectName("AIChatWindow")

        self.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )

        self.is_processing = False
        self._embed_backend = web_embed_backend()
        # Embedded HTML chat (WebEngine or WebKit); Qt widgets only when neither is available.
        self._use_web_ui = self._embed_backend in ("webengine", "webkit")
        self._chat_embed_backend = None  # set in _init_web_* ('webengine' | 'webkit')
        self._first_prompt_summary = None  # mirrors active session's first_prompt_summary
        self._chat_web_initial_sync_done = False
        self._user_cancelled = False
        self._token_buffer = []
        self._token_flush_scheduled = False

        # Per-session state: each entry holds {"worker", "thread", "title",
        # "messages", "processing", "first_prompt_summary"}.
        self._sessions: dict = {}
        self._active_sid: str = ""
        self._history_restore_started = False
        # Project path that the currently loaded sessions belong to.  Updated
        # by ``reload_for_project`` whenever the active project changes.
        self._current_project_path: str = ""
        try:
            from classes.app import get_app
            self._current_project_path = (
                getattr(get_app().project, "current_filepath", "") or ""
            )
        except Exception:
            self._current_project_path = ""

        # Bucket this project's transcripts live under in the chat-history
        # store.  Untitled projects get a throwaway draft key that is rekeyed
        # onto the real project the first time the user saves.
        self._draft_history_key: str = ""
        self._history_key: str = self._resolve_history_key(self._current_project_path)

        # Stop all threads on app quit (covers the shutdown path where
        # closeEvent is never called on dock widgets).
        from PyQt5.QtWidgets import QApplication
        app_instance = QApplication.instance()
        if app_instance:
            app_instance.aboutToQuit.connect(self._stop_all_threads)

        # Restore previously open chat sessions (if any) before building UI.
        store = self._load_chat_sessions_store(self._current_project_path)
        restored_sessions = self._restorable_sessions(store)
        if isinstance(restored_sessions, list) and restored_sessions:
            for entry in restored_sessions:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("session_id")
                title = entry.get("title") or "New Chat"
                if not sid or sid in self._sessions:
                    continue
                backend = _coerce_backend(entry.get("backend"))
                worker, thread = self._make_worker(sid, backend, restore=entry)
                self._sessions[sid] = {
                    "worker": worker,
                    "thread": thread,
                    "title": title,
                    "messages": [],
                    "processing": False,
                    "unread": False,
                    "first_prompt_summary": title,
                    "backend": backend,
                    "agent_mode": entry.get("agent_mode", "agent"),
                    "current_plan": None,
                }
                self._persist_session(sid)

            active_from_store = store.get("active_session_id") if isinstance(store, dict) else None
            if active_from_store in self._sessions:
                self._active_sid = active_from_store
            elif self._sessions:
                self._active_sid = next(iter(self._sessions))
            self._first_prompt_summary = self._sessions[self._active_sid].get("first_prompt_summary")
        if not self._sessions:
            # Create the initial session before building the UI widgets.
            self._create_initial_session()
            self._save_chat_sessions_store()

        if self._use_web_ui:
            if self._embed_backend == "webengine":
                log.info("Zenvi Assistant: embedded HTML UI (Qt WebEngine)")
                self._init_web_ui()
            else:
                log.info("Zenvi Assistant: embedded HTML UI (Qt WebKit + legacy-safe CSS)")
                self._init_webkit_ui()
        else:
            log.info(
                "Zenvi Assistant: native Qt chat (no Qt WebEngine/WebKit in this environment)"
            )
            self._init_widget_ui()

        self.setMinimumSize(400, 450)

        # Prefetch credits before the web UI finishes loading (avoids 0 → real flash).
        self._start_credits_refresh()
        # Detect claude/codex CLI availability for the agent selector's status dots.
        self._start_cli_detection_refresh()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _make_worker(self, session_id: str, backend: str = BACKEND_ZENVI, restore: dict = None):
        """Create and start a worker thread pair for *session_id* using *backend*.

        All backends expose the same signals/slots, so the connections and the
        ``_on_*`` handlers below are identical regardless of which one is chosen.

        *restore* is a stored session row; when present its CLI continuity
        fields are seeded onto the runner so a reopened tab resumes its agent
        conversation rather than starting a fresh one.
        """
        thread = QThread()
        if backend == BACKEND_CLAUDE:
            from windows.agent_runners import ClaudeCodeRunner
            worker = ClaudeCodeRunner()
        elif backend == BACKEND_CODEX:
            from windows.agent_runners import CodexRunner
            worker = CodexRunner()
        else:
            worker = AIChatWorker()
        worker._session_id = session_id   # used by signal handlers to route responses
        # Keep backend memory namespaced by the same session id as the UI tab.
        worker._backend_session_id = session_id
        worker.moveToThread(thread)
        worker.response_ready.connect(self._on_response_ready)
        worker.error_occurred.connect(self._on_error)
        worker.token_received.connect(self._on_token)
        worker.tool_started.connect(self._on_tool_started)
        worker.tool_log.connect(self._on_tool_log)
        worker.tool_completed.connect(self._on_tool_completed)
        worker.plan_event.connect(self._on_plan_event)
        # CLI backends only: lets us persist the conversation id they resume from.
        if hasattr(worker, "cli_session_changed"):
            worker.cli_session_changed.connect(self._on_cli_session_changed)
        if restore and hasattr(worker, "_cli_session_id"):
            cli_sid = restore.get("cli_session_id") or ""
            if cli_sid:
                worker._cli_session_id = cli_sid
                worker._cli_started = bool(restore.get("cli_started"))
                worker._cli_cwd = restore.get("cli_cwd") or ""
                # A stored id is always one the CLI itself reported or accepted.
                worker._cli_id_from_cli = True
        thread.start()
        return worker, thread

    @pyqtSlot(str, str, bool, str)
    def _on_cli_session_changed(self, session_id: str, cli_session_id: str,
                                cli_started: bool, cli_cwd: str):
        """Persist a CLI agent's conversation id so --resume survives a restart."""
        self._persist_session(
            session_id,
            cli_session_id=cli_session_id,
            cli_started=cli_started,
            cli_cwd=cli_cwd,
        )

    def _create_initial_session(self):
        import uuid
        sid = str(uuid.uuid4())
        worker, thread = self._make_worker(sid, BACKEND_ZENVI)
        self._sessions[sid] = {
            "worker": worker,
            "thread": thread,
            "title": "New Chat",
            "messages": [],
            "processing": False,
            "unread": False,
            "first_prompt_summary": None,
            "backend": BACKEND_ZENVI,
            "agent_mode": "agent",
            "current_plan": None,
        }
        self._active_sid = sid
        self._persist_session(sid)

    def _create_session(self, model_id: str = "", backend: str = BACKEND_ZENVI):
        """Create a new chat session and switch to it (called from the + tab button)."""
        import uuid
        backend = _coerce_backend(backend)
        sid = str(uuid.uuid4())
        worker, thread = self._make_worker(sid, backend)
        self._sessions[sid] = {
            "worker": worker,
            "thread": thread,
            "title": "New Chat",
            "messages": [],
            "processing": False,
            "unread": False,
            "first_prompt_summary": None,
            "backend": backend,
            "agent_mode": "agent",
            "current_plan": None,
        }
        self._active_sid = sid
        self._persist_session(sid)
        self._first_prompt_summary = None
        self.is_processing = False
        self._notify_agent_selector()
        if self._use_web_ui:
            self._push_models_for_backend(backend)
            self._run_js("clearMessages();")
            self._push_tabs_to_js()
            self._update_preamble()
            self._add_chrome_msg("New session started. Ask anything about your project.")
        else:
            self.chat_box.clear()
            self._add_chrome_msg("New session started. Ask anything about your project.")
            self._update_preamble()
            self._sync_widget_backend_combo()
            self._rebuild_widget_tabs()
        self._save_chat_sessions_store()

    # ------------------------------------------------------------------
    # Agent selector (lives in the main window toolbar, next to Save)
    # ------------------------------------------------------------------

    def active_backend(self) -> str:
        """Backend id of the chat tab the user is currently looking at."""
        sess = self._active_session() or {}
        return sess.get("backend", BACKEND_ZENVI)

    def cli_status(self) -> dict:
        """``{backend_id: {installed, version, registered}}`` as last detected."""
        try:
            return json.loads(getattr(self, "_cli_status", "") or "{}")
        except Exception:
            return {}

    def set_active_backend(self, backend: str):
        """Switch the active chat tab to *backend* (called by the toolbar)."""
        if self._active_sid:
            self._set_session_backend(self._active_sid, backend)

    def _notify_agent_selector(self):
        """Repaint the toolbar selector after the active backend changes.

        The button forwards this to the agent panel when one is open, so status
        landing mid-open repaints instead of going stale.
        """
        button = getattr(self.parent(), "agent_selector_button", None)
        if button is not None:
            button.sync_from_chat()

    def _notify_agent_connect_result(self, backend_id: str, ok: bool, message: str):
        """Let the toolbar agent panel report a Connect outcome inline."""
        button = getattr(self.parent(), "agent_selector_button", None)
        notify = getattr(button, "on_connect_result", None)
        if notify is None:
            return
        try:
            notify(backend_id, ok, message)
        except Exception:
            log.debug("agent panel connect-result notify failed", exc_info=True)

    def _set_session_backend(self, session_id: str, backend: str):
        """Switch the agent backend used by *session_id*.

        Recreating the worker is the minimal-risk approach: each runner stays
        unaware of any prior backend's state, and the new one is wired to the
        same ``_on_*`` slots.
        """
        backend = _coerce_backend(backend)
        sess = self._sessions.get(session_id)
        if not sess or sess.get("backend") == backend:
            return
        old_worker = sess.get("worker")
        if old_worker is not None:
            try:
                old_worker._stopping = True
            except Exception:
                pass
            if hasattr(old_worker, "cancel"):
                try:
                    old_worker.cancel()
                except Exception:
                    pass
        old_thread = sess.get("thread")
        if old_thread is not None and old_thread.isRunning():
            old_thread.quit()
            if not old_thread.wait(1500):
                try:
                    old_thread.terminate()
                    old_thread.wait(500)
                except Exception:
                    pass
        worker, thread = self._make_worker(session_id, backend)
        sess["worker"] = worker
        sess["thread"] = thread
        sess["backend"] = backend
        if backend != BACKEND_ZENVI:
            # See _resolve_agent_mode: CLI backends have no planning mode.
            sess["agent_mode"] = "agent"
            sess["current_plan"] = None
        if session_id == self._active_sid:
            self.is_processing = False
            self._set_processing_ui(False)
            self._push_models_for_backend(backend)
            if not self._use_web_ui:
                self._sync_widget_backend_combo()
        self._notify_agent_selector()
        self._persist_session(session_id, backend=backend)
        self._save_chat_sessions_store()

    def _switch_session(self, session_id: str):
        """Switch the displayed session to *session_id* (called when user clicks a tab)."""
        if session_id not in self._sessions or session_id == self._active_sid:
            return
        self._active_sid = session_id
        sess = self._sessions[session_id]
        self._first_prompt_summary = sess.get("first_prompt_summary")
        self.is_processing = sess.get("processing", False)
        if self._use_web_ui:
            self._run_js("clearMessages();")
            for role, html_body, is_assistant in sess.get("messages", []):
                self._run_js("appendMessage(%s, %s, %s);" % (
                    json.dumps(role),
                    json.dumps(html_body),
                    "true" if is_assistant else "false",
                ))
            self._push_tabs_to_js()
            self._run_js("setProcessing(%s);" % ("true" if self.is_processing else "false"))
            plan = sess.get("current_plan")
            if plan:
                self._run_js(
                    "if(window.setPlanChip) window.setPlanChip(%s);"
                    % json.dumps(plan)
                )
            else:
                self._run_js("if(window.setPlanChip) window.setPlanChip(null);")
            mode = sess.get("agent_mode", "agent")
            self._run_js("if(window.setAgentModeUI) window.setAgentModeUI(%s);" % json.dumps(mode))
            # Tabs can run different backends, and each has its own lineup.
            self._push_models_for_backend(sess.get("backend", BACKEND_ZENVI))
        self._notify_agent_selector()
        self._update_preamble()
        self._save_chat_sessions_store()
        if not self._use_web_ui:
            # Widget mode: render the stored messages for the newly active session.
            sess["unread"] = False
            self._render_active_session_widget()
            self._sync_widget_backend_combo()
            self._rebuild_widget_tabs()

    def _close_session(self, session_id: str):
        """Close a session and delete its Pinecone namespace (called from the × on a tab)."""
        if len(self._sessions) <= 1:
            return  # never close the last session
        if session_id not in self._sessions:
            return
        # Soft-delete first: the worker's clear_session below wipes the
        # backend's own copy, so this row can end up the only record left.
        from classes import chat_history
        chat_history.mark_session_closed(session_id)
        sess = self._sessions.pop(session_id)
        worker = sess.get("worker")
        thread = sess.get("thread")
        self._shutdown_worker(worker, thread, wait_ms=2000)
        # Clear backend session in background
        if worker:
            QMetaObject.invokeMethod(worker, "clear_session", Qt.QueuedConnection)
        # If we just closed the active session, switch to the first remaining one.
        # Let _switch_session assign self._active_sid itself — presetting it here
        # would trip that method's own "already active" early-return guard and
        # skip the tab/message/model-pill re-render entirely.
        if self._active_sid == session_id:
            self._switch_session(next(iter(self._sessions)))
        else:
            if self._use_web_ui:
                self._push_tabs_to_js()
            else:
                self._rebuild_widget_tabs()
        self._save_chat_sessions_store()

    def _push_tabs_to_js(self):
        """Push the current session list to the JS tab bar."""
        tabs = []
        for sid, sess in self._sessions.items():
            tabs.append({
                "id": sid,
                "title": sess.get("first_prompt_summary") or sess.get("title", "New Chat"),
                "active": sid == self._active_sid,
                "processing": bool(sess.get("processing", False)),
                "backend": sess.get("backend", BACKEND_ZENVI),
            })
        self._run_js("setTabs(%s);" % json.dumps(json.dumps(tabs)))

    @pyqtSlot(str)
    def reload_for_project(self, new_project_path: str):
        """Switch the chat dock to the per-project sessions for ``new_project_path``.

        Persists the currently open sessions under the previously active
        project's bucket, tears down their worker threads, then rebuilds the
        session list from the new project's store (creating a fresh session
        if none exist).  No backend memory is cleared — old conversations
        remain reachable when the original project is reopened.
        """
        try:
            from classes import chat_history

            new_project_path = (new_project_path or "").strip()
            prev_path = getattr(self, "_current_project_path", "") or ""
            prev_key = getattr(self, "_history_key", "") or ""
            if new_project_path and prev_path and (
                os.path.abspath(new_project_path) == os.path.abspath(prev_path)
            ):
                # Same saved project re-signaled — nothing to do.  An empty
                # ``new_project_path`` (New Project / untitled) is allowed to
                # fall through so the chat resets to a fresh session.
                return

            # Resolve the new bucket first: this is what forks a Save As and
            # adopts a project whose id churned underneath us.
            if not new_project_path:
                # New Project — a fresh throwaway bucket, not the last one.
                # Bin the outgoing draft if nothing was ever said in it.
                if prev_key.startswith("draft:"):
                    chat_history.discard_empty_bucket(prev_key)
                self._draft_history_key = ""
            new_key = self._resolve_history_key(new_project_path)
            if new_project_path and prev_key.startswith("draft:"):
                # First save of an untitled project: its chat comes along
                # rather than being thrown away.
                chat_history.rekey_project(prev_key, new_key, new_project_path)
                self._draft_history_key = ""

            live_sids = set(self._sessions.keys())
            stored_sids = {
                row.get("session_id") for row in chat_history.load_sessions(new_key)
            }
            if live_sids and (live_sids & stored_sids):
                # The store moved our live tabs into the new bucket (a draft
                # being saved, or a Save As fork), so the conversation simply
                # continues — no teardown, no reload.
                self._current_project_path = new_project_path
                self._history_key = new_key
                self._save_chat_sessions_store(new_project_path)
                return

            # 1. Persist current sessions to the previous project's store.
            try:
                self._save_chat_sessions_store(prev_path)
            except Exception:
                pass

            # 2. Tear down existing worker threads (do NOT clear backend
            #    memory — we want to be able to come back to these chats
            #    when the user reopens the previous project).
            try:
                from classes.api_client import get_backend_client
                get_backend_client().cancel_current_request()
            except Exception:
                pass
            for sess in list(self._sessions.values()):
                self._shutdown_worker(sess.get("worker"), sess.get("thread"), wait_ms=1500)
            self._sessions.clear()
            self._active_sid = ""
            self._first_prompt_summary = None
            self._history_restore_started = False
            self.is_processing = False

            # 3. Bind to the new project and load its store.
            self._current_project_path = new_project_path
            self._history_key = new_key
            store = self._load_chat_sessions_store(new_project_path)
            restored_sessions = self._restorable_sessions(store)
            if isinstance(restored_sessions, list) and restored_sessions:
                for entry in restored_sessions:
                    if not isinstance(entry, dict):
                        continue
                    sid = entry.get("session_id")
                    title = entry.get("title") or "New Chat"
                    if not sid or sid in self._sessions:
                        continue
                    backend = _coerce_backend(entry.get("backend"))
                    worker, thread = self._make_worker(sid, backend, restore=entry)
                    self._sessions[sid] = {
                        "worker": worker,
                        "thread": thread,
                        "title": title,
                        "messages": [],
                        "processing": False,
                        "unread": False,
                        "first_prompt_summary": title,
                        "backend": backend,
                        "agent_mode": entry.get("agent_mode", "agent"),
                        "current_plan": None,
                    }
                    self._persist_session(sid)
                active_from_store = (
                    store.get("active_session_id") if isinstance(store, dict) else None
                )
                if active_from_store in self._sessions:
                    self._active_sid = active_from_store
                else:
                    self._active_sid = next(iter(self._sessions))
                self._first_prompt_summary = self._sessions[self._active_sid].get(
                    "first_prompt_summary"
                )

            if not self._sessions:
                self._create_initial_session()

            # 4. Refresh the visible chat surface and tab bar.
            if self._use_web_ui:
                try:
                    self._run_js("clearMessages();")
                except Exception:
                    pass
                self._push_tabs_to_js()
                self._update_preamble()
            else:
                try:
                    if hasattr(self, "chat_box") and self.chat_box is not None:
                        self.chat_box.clear()
                except Exception:
                    pass
                self._update_preamble()
                self._render_active_session_widget()
                self._rebuild_widget_tabs()

            self._save_chat_sessions_store(new_project_path)

            # 5. Re-fetch chat history for restored sessions in the
            #    background (mirrors the post-init behaviour).
            try:
                self._start_restore_chat_histories_async()
            except Exception:
                pass
        except Exception as e:
            log.warning("AI chat reload_for_project failed: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Local persistence for open chat sessions (session ids + titles)
    # ------------------------------------------------------------------
    def _project_key(self, project_path: str = None) -> str:
        """Return a stable storage key for the given project file path.

        Saved projects get a sha1 of the absolute path; the unsaved
        ``Untitled Project`` (or any empty path) uses the legacy
        ``_default`` bucket so existing chats are preserved.
        """
        import hashlib
        if project_path is None:
            project_path = getattr(self, "_current_project_path", "") or ""
            if not project_path:
                try:
                    from classes.app import get_app
                    project_path = (
                        getattr(get_app().project, "current_filepath", "") or ""
                    )
                except Exception:
                    project_path = ""
        if not project_path:
            return "_default"
        try:
            abs_path = os.path.abspath(project_path)
        except Exception:
            abs_path = project_path
        return hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:16]

    def _project_id(self) -> str:
        """The id stored inside the project file — stable across rename/move."""
        try:
            from classes.app import get_app
            return str(get_app().project.get("id") or "")
        except Exception:
            return ""

    def _resolve_history_key(self, project_path: str) -> str:
        """Chat-history bucket for *project_path*, repairing the mapping if needed.

        Keyed on the project's own id rather than its path, so renaming a
        project keeps its chats.  ``resolve_project_key`` also handles the
        Save-As fork and the legacy-id churn cases — see that docstring.
        """
        from classes import chat_history

        path = project_path or ""
        if not path:
            # Untitled: one bucket per window, rekeyed on first save so an hour
            # of chatting before hitting Save isn't thrown away.
            if not self._draft_history_key:
                self._draft_history_key = chat_history.new_draft_key()
            return self._draft_history_key

        key = (
            chat_history.resolve_project_key(self._project_id(), path)
            or chat_history.path_key(path)
        )
        # First sight of this project: carry over the old metadata-only store
        # (a no-op once the bucket has rows of its own).
        legacy = self._load_chat_sessions_store(path)
        if isinstance(legacy, dict) and legacy.get("sessions"):
            chat_history.import_legacy_sessions(key, path, legacy.get("sessions"))
        return key

    def _restorable_sessions(self, legacy_store: dict) -> list:
        """Sessions to reopen for this project — local history first.

        The chat-history store is authoritative.  The legacy JSON store is
        still read as a fallback so a rollback loses nothing.
        """
        from classes import chat_history

        rows = chat_history.load_sessions(self._history_key)
        if rows:
            return rows
        legacy = legacy_store.get("sessions", []) if isinstance(legacy_store, dict) else []
        return [e for e in legacy if isinstance(e, dict) and e.get("session_id")]

    def _persist_session(self, session_id: str, **fields) -> None:
        """Register/refresh a session row in the chat-history store."""
        from classes import chat_history

        sess = self._sessions.get(session_id) or {}
        chat_history.upsert_session(
            session_id,
            self._history_key,
            project_path=self._current_project_path or None,
            title=fields.get("title") or sess.get("first_prompt_summary") or sess.get("title"),
            backend=fields.get("backend") or sess.get("backend"),
            agent_mode=fields.get("agent_mode") or sess.get("agent_mode"),
            cli_session_id=fields.get("cli_session_id"),
            cli_started=fields.get("cli_started"),
            cli_cwd=fields.get("cli_cwd"),
        )

    def _record_message(self, session_id: str, role: str, text: str) -> None:
        """Persist one final message. Never let a store failure break a turn."""
        if not session_id or not text:
            return
        from classes import chat_history
        chat_history.record_message(session_id, role, text)

    def _record_tool_started(self, session_id: str, call_id: str, tool_name: str) -> None:
        """Note that a tool ran, so a restored transcript can show the activity.

        Only the name and outcome are kept — args and results stay in the
        backend/CLI transcripts, which is what keeps this store small.
        """
        from classes import chat_history
        chat_history.record_tool_event(
            session_id, call_id, tool_name, humanize_tool_name(tool_name)
        )

    def _record_tool_completed(self, session_id: str, call_id: str, ok: bool) -> None:
        from classes import chat_history
        chat_history.complete_tool_event(session_id, call_id, bool(ok))

    def _chat_sessions_dir(self) -> str:
        from classes import info
        return os.path.join(info.USER_PATH, "chat_sessions")

    def _chat_sessions_store_path(self, project_path: str = None) -> str:
        key = self._project_key(project_path)
        return os.path.join(self._chat_sessions_dir(), f"{key}.json")

    def _migrate_legacy_chat_store(self) -> None:
        """One-time move of the old global store into the ``_default`` bucket."""
        try:
            from classes import info
            legacy_path = os.path.join(info.USER_PATH, "zenvi_chat_sessions.json")
            if not os.path.isfile(legacy_path):
                return
            new_path = os.path.join(self._chat_sessions_dir(), "_default.json")
            if os.path.isfile(new_path):
                return
            os.makedirs(self._chat_sessions_dir(), exist_ok=True)
            os.replace(legacy_path, new_path)
        except Exception:
            pass

    def _load_chat_sessions_store(self, project_path: str = None) -> dict:
        # Untitled / unsaved projects are ephemeral — never restore old chats
        # (this also ignores any stale or pre-existing ``_default.json``).
        if self._project_key(project_path) == "_default":
            return {}
        try:
            self._migrate_legacy_chat_store()
            path = self._chat_sessions_store_path(project_path)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
        except Exception:
            return {}

    def _save_chat_sessions_store(self, project_path: str = None) -> None:
        # Untitled / unsaved projects are ephemeral — don't persist their chats.
        if self._project_key(project_path) == "_default":
            return
        try:
            path = self._chat_sessions_store_path(project_path)
            os.makedirs(self._chat_sessions_dir(), exist_ok=True)

            sessions_payload = []
            for sid, sess in self._sessions.items():
                title = sess.get("first_prompt_summary") or sess.get("title", "New Chat")
                sessions_payload.append({
                    "session_id": sid,
                    "title": title,
                    "backend": sess.get("backend", BACKEND_ZENVI),
                    "agent_mode": sess.get("agent_mode", "agent"),
                })

            payload = {
                "version": 1,
                "active_session_id": self._active_sid,
                "sessions": sessions_payload,
            }

            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, path)
        except Exception:
            # Non-fatal: chat can still run without local persistence.
            pass

    # ------------------------------------------------------------------
    # Restoring a transcript: local history first, backend only for gaps
    # ------------------------------------------------------------------

    @staticmethod
    def _history_norm(role: str, content: str) -> str:
        """Comparable form of a message, so two sources can be lined up."""
        text = content or ""
        if role == "assistant":
            text = AIChatWindow._strip_thinking(text)
        return " ".join(text.split())

    def _render_item(self, role: str, content: str) -> dict:
        """Render one stored message into the shape the UI replays."""
        if role == "assistant":
            return {
                "role": role,
                "html_body": _markdown_to_html(content),
                "is_assistant": True,
                "content": content,
            }
        safe = html.escape(content or "").replace("\n", "<br/>")
        return {
            "role": role,
            "html_body": "<p>" + safe + "</p>",
            "is_assistant": False,
            "content": content or "",
        }

    def _local_history_items(self, session_id: str) -> list:
        from classes import chat_history
        return [
            self._render_item(m.get("role", ""), m.get("content", ""))
            for m in chat_history.load_messages(session_id)
        ]

    def _restore_local_histories(self) -> None:
        """Populate every open tab from the local store, then draw the active one.

        This runs before the network fetch so a reopened project shows its
        conversation immediately — and still shows it with the backend down.
        """
        for sid in list(self._sessions.keys()):
            items = self._local_history_items(sid)
            if not items:
                continue
            sess = self._sessions.get(sid)
            if sess is None:
                continue
            system_parts = [m for m in sess.get("messages", []) if m and m[0] == "system"]
            sess["messages"] = system_parts + [
                (it["role"], it["html_body"], it["is_assistant"]) for it in items
            ]
            sess["local_contents"] = [(it["role"], it["content"]) for it in items]
        if self._active_sid in self._sessions and self._sessions[self._active_sid].get("local_contents"):
            self._render_restored_active_session()

    def _render_restored_active_session(self) -> None:
        """Replay the active tab's stored transcript, tool activity included."""
        if not self._use_web_ui:
            self._render_active_session_widget()
            self._rebuild_widget_tabs()
            return

        from classes import chat_history
        messages = chat_history.load_messages(self._active_sid)
        by_turn = {}
        for ev in chat_history.load_tool_events(self._active_sid):
            by_turn.setdefault(ev.get("after_seq") or 0, []).append(ev)

        self._run_js("clearMessages();")
        for ev in by_turn.pop(0, []):
            self._replay_tool_block(ev)
        for msg in messages:
            item = self._render_item(msg.get("role", ""), msg.get("content", ""))
            self._run_js(
                "appendMessage(%s, %s, %s);" % (
                    json.dumps(item["role"]), json.dumps(item["html_body"]),
                    "true" if item["is_assistant"] else "false",
                )
            )
            # A turn's tool blocks sit between the message that triggered them
            # and the reply that followed, which is where they first appeared.
            for ev in by_turn.pop(msg.get("seq"), []):
                self._replay_tool_block(ev)
        for leftover in by_turn.values():
            for ev in leftover:
                self._replay_tool_block(ev)
        self._push_tabs_to_js()

    def _replay_tool_block(self, event: dict) -> None:
        """Redraw one finished tool block, already collapsed."""
        payload = {
            "call_id": event.get("call_id") or "",
            "title": event.get("title") or humanize_tool_name(event.get("tool_name") or ""),
            "cmd": "",
            "args_detail": "",
            "tool_name": event.get("tool_name") or "",
        }
        self._run_js(
            "if(window.replayToolBlock) window.replayToolBlock(%s, %s);"
            % (json.dumps(json.dumps(payload)),
               "true" if (event.get("status") != "error") else "false")
        )

    def _history_tail_beyond_local(self, local_pairs: list, backend_items: list) -> list:
        """Backend messages that extend what we already hold locally.

        Empty unless the backend transcript is strictly longer *and* everything
        we have locally lines up as its prefix — otherwise the two have
        diverged and the local copy is the one we trust.
        """
        if len(backend_items) <= len(local_pairs):
            return []
        for (l_role, l_content), item in zip(local_pairs, backend_items):
            if l_role != item.get("role"):
                return []
            if self._history_norm(l_role, l_content) != self._history_norm(
                item.get("role", ""), item.get("content", "")
            ):
                return []
        return backend_items[len(local_pairs):]

    @pyqtSlot(str, str)
    def _on_history_restored(self, session_id: str, messages_json: str):
        """Fold /chat/history into the tab — local history stays authoritative."""
        if session_id not in self._sessions:
            return
        try:
            restored_items = json.loads(messages_json) if messages_json else []
        except Exception:
            restored_items = []

        local_pairs = self._sessions[session_id].get("local_contents") or []
        if local_pairs:
            self._sessions[session_id]["unread"] = False
            self._sessions[session_id]["processing"] = False
            tail = self._history_tail_beyond_local(local_pairs, restored_items)
            if not tail:
                # Already rendered from the local store; nothing to add.
                if self._use_web_ui:
                    self._push_tabs_to_js()
                else:
                    self._rebuild_widget_tabs()
                return
            appended = [
                (it.get("role", ""), it.get("html_body", ""), bool(it.get("is_assistant")))
                for it in tail
            ]
            self._sessions[session_id]["messages"] = list(
                self._sessions[session_id].get("messages", [])
            ) + appended
            self._sessions[session_id]["local_contents"] = local_pairs + [
                (it.get("role", ""), it.get("content", "")) for it in tail
            ]
            if session_id == self._active_sid and self._use_web_ui:
                # Append only, so the replayed tool blocks stay put.
                for role, html_body, is_assistant in appended:
                    self._run_js(
                        "appendMessage(%s, %s, %s);" % (
                            json.dumps(role), json.dumps(html_body),
                            "true" if is_assistant else "false",
                        )
                    )
                self._push_tabs_to_js()
            elif session_id == self._active_sid:
                self._render_active_session_widget()
                self._rebuild_widget_tabs()
            else:
                if self._use_web_ui:
                    self._push_tabs_to_js()
                else:
                    self._rebuild_widget_tabs()
            return

        system_parts = [m for m in self._sessions[session_id].get("messages", []) if m and m[0] == "system"]
        restored_messages = [(m.get("role", ""), m.get("html_body", ""), bool(m.get("is_assistant", False))) for m in restored_items]
        merged = system_parts + restored_messages
        # Fresh chat with no backend history: match the UI shown after clicking "+" on the tab bar.
        if session_id == self._active_sid and not merged:
            welcome = "New session started. Ask anything about your project."
            safe = html.escape(welcome).replace("\n", "<br/>")
            merged = [("system", "<p>" + safe + "</p>", False)]
        self._sessions[session_id]["messages"] = merged
        self._sessions[session_id]["unread"] = False
        self._sessions[session_id]["processing"] = False

        if session_id != self._active_sid:
            # Inactive tabs only need data stored for the next switch.
            if self._use_web_ui:
                self._push_tabs_to_js()
            else:
                self._rebuild_widget_tabs()
            return

        if self._use_web_ui:
            self._run_js("clearMessages();")
            for role, html_body, is_assistant in self._sessions[session_id].get("messages", []):
                self._run_js(
                    "appendMessage(%s, %s, %s);" % (
                        json.dumps(role),
                        json.dumps(html_body),
                        "true" if is_assistant else "false",
                    )
                )
            self._push_tabs_to_js()
        else:
            self._render_active_session_widget()
            self._rebuild_widget_tabs()

    def _start_restore_chat_histories_async(self) -> None:
        """Fetch /chat/history/{session_id} for all open sessions."""
        if self._history_restore_started:
            return
        self._history_restore_started = True
        self._restore_local_histories()

        session_ids = list(self._sessions.keys())

        def _restore_all():
            try:
                client = get_backend_client()
            except Exception:
                client = None

            for sid in session_ids:
                restored_items = []
                messages = []
                if client is not None:
                    try:
                        resp = client.get_chat_history(sid)
                        messages = (resp or {}).get("messages", []) or []
                    except Exception:
                        messages = []

                    for m in messages:
                        role = m.get("role", "")
                        content = m.get("content", "") or ""
                        if role == "assistant":
                            html_body = _markdown_to_html(content)
                            restored_items.append({"role": role, "html_body": html_body, "is_assistant": True, "content": content})
                        else:
                            visible = _strip_context_blocks(content) if role == "user" else content
                            safe = html.escape(visible).replace("\n", "<br/>")
                            html_body = "<p>" + safe + "</p>"
                            restored_items.append({"role": role, "html_body": html_body, "is_assistant": False, "content": visible})

                try:
                    QMetaObject.invokeMethod(
                        self,
                        "_on_history_restored",
                        Qt.QueuedConnection,
                        Q_ARG(str, sid),
                        Q_ARG(str, json.dumps(restored_items)),
                    )
                except Exception:
                    pass

        threading.Thread(target=_restore_all, daemon=True).start()

    def _active_session(self) -> dict:
        return self._sessions.get(self._active_sid, {})

    # ------------------------------------------------------------------
    # Widget multi-chat tab bar + rendering helpers
    # ------------------------------------------------------------------
    def _display_stored_msg_widget(self, role: str, html_body: str, is_assistant: bool):
        """Render a stored (role, html_body) tuple into the widget chat box."""
        if not getattr(self, "chat_box", None):
            return

        cursor = self.chat_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_box.setTextCursor(cursor)

        role_display = "You" if role == "user" else ("Assistant" if role == "assistant" else role)
        if is_assistant:
            role_label = f'<span style="font-weight: bold;">{html.escape(role_display)}</span><br/>'
        else:
            role_style = "color: #3B82F6;" if role == "user" else ""
            role_label = (
                f'<span style="font-weight: bold; {role_style}">{html.escape(role_display)}</span><br/>'
            )

        self.chat_box.insertHtml(role_label + html_body + "<br/>")

        cursor = self.chat_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_box.setTextCursor(cursor)

    def _render_active_session_widget(self):
        """Clear and re-render stored messages for the active session (widget mode only)."""
        if self._use_web_ui:
            return
        if not getattr(self, "chat_box", None):
            return
        self.chat_box.clear()
        sess = self._active_session()
        for role, html_body, is_assistant in sess.get("messages", []):
            self._display_stored_msg_widget(role, html_body, is_assistant)

    def _on_widget_backend_changed(self, _idx):
        combo = getattr(self, "backend_combo", None)
        if combo is None or not self._active_sid:
            return
        self._set_session_backend(self._active_sid, combo.currentData() or BACKEND_ZENVI)

    def _sync_widget_backend_combo(self):
        """Reflect the active session's backend in the widget combo (no signal)."""
        combo = getattr(self, "backend_combo", None)
        if combo is None:
            return
        sess = self._active_session()
        backend = sess.get("backend", BACKEND_ZENVI) if sess else BACKEND_ZENVI
        idx = combo.findData(backend)
        if idx >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def _rebuild_widget_tabs(self):
        """Rebuild the widget fallback multi-chat tab bar."""
        if self._use_web_ui:
            return
        layout = getattr(self, "_widget_tabs_layout", None)
        if layout is None:
            return

        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for sid, sess in self._sessions.items():
            title = sess.get("first_prompt_summary") or sess.get("title", "New Chat")
            processing = bool(sess.get("processing", False))
            unread = bool(sess.get("unread", False))

            suffix = ""
            if processing:
                suffix += " [P]"
            if unread:
                suffix += " [U]"

            tab_btn = QPushButton(title + suffix)
            tab_btn.setObjectName("chatWidgetTabBtn")
            tab_btn.setFlat(True)
            tab_btn.setCheckable(False)
            tab_btn.setStyleSheet(
                "text-align: left; background: transparent; border: 1px solid transparent;"
                if sid != self._active_sid else
                "text-align: left; background: rgba(77,156,246,0.14); border: 1px solid rgba(77,156,246,0.35);"
            )
            tab_btn.clicked.connect(lambda _=False, s=sid: self._switch_session(s))
            layout.addWidget(tab_btn)

            if len(self._sessions) > 1:
                close_btn = QToolButton()
                close_btn.setObjectName("chatWidgetTabCloseBtn")
                close_btn.setAutoRaise(True)
                close_btn.setText("x")
                close_btn.setStyleSheet("border: none; color: #6b7280;")
                close_btn.clicked.connect(lambda _=False, s=sid: self._close_session(s))
                layout.addWidget(close_btn)

        add_btn = QPushButton("+")
        add_btn.setObjectName("chatWidgetTabAddBtn")
        add_btn.setFlat(True)
        add_btn.setStyleSheet("border: 1px solid rgba(255,255,255,0.08);")
        add_btn.clicked.connect(
            lambda _=False: self._create_session(
                self.model_combo.currentData() if getattr(self, "model_combo", None) else "",
                self.backend_combo.currentData() if getattr(self, "backend_combo", None) else BACKEND_ZENVI,
            )
        )
        layout.addWidget(add_btn)

    # ------------------------------------------------------------------
    # Widget UI (fallback when WebEngine is unavailable)
    # ------------------------------------------------------------------

    def _init_widget_ui(self):
        """Build classic Qt widget chat UI."""
        main = QWidget()
        main.setObjectName("AIChatWindowContents")
        layout = QVBoxLayout()
        main.setLayout(layout)
        self.setWidget(main)

        self._chat_opacity_effect = QGraphicsOpacityEffect(main)
        self._chat_opacity_effect.setOpacity(0.0)
        main.setGraphicsEffect(self._chat_opacity_effect)
        self._chat_fade_done = False
        self._chat_fade_anim = QPropertyAnimation(self._chat_opacity_effect, b"opacity")
        self._chat_fade_anim.setDuration(250)
        self._chat_fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._chat_fade_anim.setStartValue(0.0)
        self._chat_fade_anim.setEndValue(1.0)
        self._chat_fade_anim.finished.connect(self._on_chat_fade_finished)

        # ------------------------------------------------------------------
        # Agent backend selector (top of the panel)
        # ------------------------------------------------------------------
        backend_h = QHBoxLayout()
        backend_h.setContentsMargins(8, 6, 8, 0)
        backend_h.addWidget(QLabel("Agent:"))
        self.backend_combo = QComboBox()
        self.backend_combo.setObjectName("agentBackendCombo")
        for b in BACKENDS:
            self.backend_combo.addItem(b["name"], b["id"])
        self._sync_widget_backend_combo()
        self.backend_combo.currentIndexChanged.connect(self._on_widget_backend_changed)
        backend_h.addWidget(self.backend_combo)
        backend_h.addStretch()
        layout.addLayout(backend_h)

        # ------------------------------------------------------------------
        # Widget multi-chat tab bar (fallback mode)
        # ------------------------------------------------------------------
        self._widget_tab_scroll = QScrollArea()
        self._widget_tab_scroll.setWidgetResizable(True)
        self._widget_tab_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._widget_tab_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._widget_tab_scroll.setFrameShape(QFrame.NoFrame)
        self._widget_tabs_container = QWidget()
        self._widget_tabs_container.setObjectName("widgetChatTabContainer")
        self._widget_tabs_layout = QHBoxLayout()
        self._widget_tabs_layout.setContentsMargins(8, 4, 8, 4)
        self._widget_tabs_layout.setSpacing(6)
        self._widget_tabs_container.setLayout(self._widget_tabs_layout)
        self._widget_tab_scroll.setWidget(self._widget_tabs_container)
        self._widget_tab_scroll.setFixedHeight(38)
        layout.addWidget(self._widget_tab_scroll)

        self.preamble_frame = QFrame()
        self.preamble_frame.setObjectName("chatPreamble")
        preamble_layout = QVBoxLayout(self.preamble_frame)
        preamble_layout.setContentsMargins(8, 8, 8, 8)
        self.preamble_label = QLabel()
        self.preamble_label.setObjectName("chatPreambleLabel")
        self.preamble_label.setWordWrap(True)
        self.preamble_label.setTextFormat(Qt.RichText)
        preamble_layout.addWidget(self.preamble_label)
        layout.addWidget(self.preamble_frame)
        self._update_preamble()

        model_h = QHBoxLayout()
        model_h.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setObjectName("modelCombo")
        self._populate_models()
        model_h.addWidget(self.model_combo)
        model_h.addStretch()
        layout.addLayout(model_h)

        self._widget_tool_scroll = QScrollArea()
        self._widget_tool_scroll.setObjectName("widgetToolScroll")
        self._widget_tool_scroll.setWidgetResizable(True)
        self._widget_tool_scroll.setFrameShape(QFrame.NoFrame)
        self._widget_tool_scroll.setMaximumHeight(140)
        self._widget_tool_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._widget_tool_blocks_host = QWidget()
        self._widget_tool_container = QVBoxLayout(self._widget_tool_blocks_host)
        self._widget_tool_container.setContentsMargins(4, 2, 4, 2)
        self._widget_tool_container.setSpacing(4)
        self._widget_tool_container.addStretch()
        self._widget_tool_scroll.setWidget(self._widget_tool_blocks_host)
        self._widget_tool_blocks = {}
        layout.addWidget(self._widget_tool_scroll)

        self.chat_box = QTextEdit()
        self.chat_box.setObjectName("chatBox")
        self.chat_box.setReadOnly(True)
        self.chat_box.setAcceptRichText(True)
        self.chat_box.setPlaceholderText("")
        layout.addWidget(self.chat_box)

        input_h = QHBoxLayout()
        self.msg_input = QTextEdit()
        self.msg_input.setObjectName("msgInput")
        self.msg_input.setMaximumHeight(80)
        self.msg_input.setPlaceholderText("Type a message... (Enter to send, Shift+Enter for newline)")
        input_h.addWidget(self.msg_input)
        layout.addLayout(input_h)

        btn_h = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self.send_message)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self.cancel_request)
        self.cancel_btn.setVisible(False)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.clicked.connect(self.clear_chat)
        self.trace_btn = QPushButton("Trace")
        self.trace_btn.setObjectName("traceBtn")
        self.trace_btn.setToolTip("Inspect agent tool args/results for this session")
        self.trace_btn.clicked.connect(self.open_agent_trace)
        btn_h.addStretch()
        btn_h.addWidget(self.send_btn)
        btn_h.addWidget(self.cancel_btn)
        btn_h.addWidget(self.trace_btn)
        btn_h.addWidget(self.clear_btn)
        layout.addLayout(btn_h)

        self.msg_input.keyPressEvent = self._key_press
        self._add_chrome_msg("Chat started. Ask to list files, add tracks, export video, or describe your project.")
        self._rebuild_widget_tabs()
        self._start_restore_chat_histories_async()

    def open_agent_trace(self):
        """Open the Agent Trace dialog for the active session."""
        try:
            from windows.agent_trace_dialog import AgentTraceDialog
        except Exception:
            try:
                from src.windows.agent_trace_dialog import AgentTraceDialog
            except Exception as e:
                log.error("AgentTraceDialog import failed: %s", e)
                return
        sid = self._active_sid or ""
        dlg = AgentTraceDialog(sid, parent=self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _prepend_editor_snapshot(self, text: str) -> str:
        """Ground the model with a bounded timeline snapshot (main thread)."""
        try:
            from classes.tool_handlers import build_editor_snapshot_for_chat

            snap = build_editor_snapshot_for_chat()
            if snap:
                return f"{snap}\n{text}"
        except Exception:
            pass
        return text

    def _load_chat_html_for_embed(self, webkit=False):
        """Load chat_ui/index.html; inject WebKit companion stylesheet + flag when needed."""
        from classes import info
        chat_ui_dir = os.path.join(info.PATH, "chat_ui")
        index_path = os.path.join(chat_ui_dir, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        if webkit:
            if "<html " in html:
                html = html.replace("<html ", '<html data-zenvi-webkit="1" ', 1)
            else:
                html = html.replace("<html>", '<html data-zenvi-webkit="1">', 1)
            html = html.replace(
                "</head>",
                '\n    <link rel="stylesheet" href="chat-webkit.css">\n</head>',
                1,
            )
        return html

    def _init_web_ui(self):
        """Build embedded HTML chat UI (Qt WebEngine)."""
        from classes import info
        from PyQt5.QtWebEngineWidgets import QWebEngineView
        from PyQt5.QtWebChannel import QWebChannel

        self._chat_embed_backend = "webengine"
        self._chat_fade_done = True
        self._chat_web_ready = lambda: None
        self.preamble_frame = self.preamble_label = None
        self.model_combo = self.chat_box = self.msg_input = None
        self.send_btn = self.cancel_btn = self.clear_btn = None
        self._chat_opacity_effect = self._chat_fade_anim = None

        self._chat_bridge = ChatBridge(window=self, parent=self)
        self._chat_bridge.window = self

        chat_ui_dir = os.path.join(info.PATH, "chat_ui")
        index_path = os.path.join(chat_ui_dir, "index.html")
        base_url = QUrl.fromLocalFile(QFileInfo(index_path).absoluteFilePath())
        html = self._load_chat_html_for_embed(webkit=False)

        inject_js = (
            "var r = document.documentElement.style;"
            "r.setProperty('--chat-bg',       '#0d0d0d');"
            "r.setProperty('--chat-surface',  '#0d0d0d');"
            "r.setProperty('--chat-input-bg', '#171717');"
            "r.setProperty('--chat-border',   'transparent');"
            "document.body.style.background = '#0d0d0d';"
            "var msgs = document.getElementById('chat-messages');"
            "if (msgs) { msgs.style.background = '#0d0d0d'; msgs.style.border = 'none'; }"
            "var preamble = document.querySelector('.chat-container > div');"
            "if (preamble) { preamble.style.background = '#0d0d0d'; preamble.style.border = 'none'; }"
        )

        self._chat_view = QWebEngineView(self)
        self._chat_view.setObjectName("AIChatWindowContents")
        self._chat_view.page().setBackgroundColor(QColor(13, 13, 13))
        self.setWidget(self._chat_view)

        self._chat_channel = QWebChannel(self._chat_view.page())
        self._chat_channel.registerObject("zenviChatBridge", self._chat_bridge)
        self._chat_view.page().setWebChannel(self._chat_channel)
        self._chat_view.setHtml(html, base_url)

        def on_load_finished(ok):
            if ok:
                self._chat_view.page().runJavaScript(inject_js)
                self._chat_web_ready = self._inject_web_ready
                self._inject_web_ready()

        self._chat_view.loadFinished.connect(on_load_finished)

    def _init_webkit_ui(self):
        """Embedded HTML chat using Qt WebKit (MSYS2 / Windows WebKit builds)."""
        from classes import info
        from PyQt5.QtWebKitWidgets import QWebView
        from PyQt5.QtWebKit import QWebSettings

        from windows.embedded_web import attach_webkit_window_object, run_js as web_run_js

        self._chat_embed_backend = "webkit"
        self._chat_fade_done = True
        self._chat_web_ready = lambda: None
        self.preamble_frame = self.preamble_label = None
        self.model_combo = self.chat_box = self.msg_input = None
        self.send_btn = self.cancel_btn = self.clear_btn = None
        self._chat_opacity_effect = self._chat_fade_anim = None

        self._chat_bridge = ChatBridge(window=self, parent=self)
        self._chat_bridge.window = self

        chat_ui_dir = os.path.join(info.PATH, "chat_ui")
        index_path = os.path.join(chat_ui_dir, "index.html")
        base_url = QUrl.fromLocalFile(QFileInfo(index_path).absoluteFilePath())
        html = self._load_chat_html_for_embed(webkit=True)

        self._chat_view = QWebView(self)
        self._chat_view.setObjectName("AIChatWindowContents")
        pal = self._chat_view.palette()
        pal.setColor(self._chat_view.backgroundRole(), QColor(13, 13, 13))
        self._chat_view.setAutoFillBackground(True)
        self._chat_view.setPalette(pal)

        st = self._chat_view.settings()
        st.setAttribute(QWebSettings.LocalContentCanAccessFileUrls, True)
        st.setAttribute(QWebSettings.LocalContentCanAccessRemoteUrls, False)
        st.setAttribute(QWebSettings.PluginsEnabled, False)

        attach_webkit_window_object(self._chat_view, "zenviChatBridge", self._chat_bridge)
        self.setWidget(self._chat_view)
        self._chat_view.setHtml(html, base_url)

        inject_js = (
            "var r = document.documentElement.style;"
            "r.setProperty('--chat-bg',       '#0d0d0d');"
            "r.setProperty('--chat-surface',  '#0d0d0d');"
            "r.setProperty('--chat-input-bg', '#171717');"
            "r.setProperty('--chat-border',   'transparent');"
            "document.body.style.background = '#0d0d0d';"
            "var msgs = document.getElementById('chat-messages');"
            "if (msgs) { msgs.style.background = '#0d0d0d'; msgs.style.border = 'none'; }"
            "var preamble = document.querySelector('.chat-container > div');"
            "if (preamble) { preamble.style.background = '#0d0d0d'; preamble.style.border = 'none'; }"
        )

        def on_load_finished(ok):
            if ok:
                web_run_js(self._chat_view, "webkit", inject_js)
                self._chat_web_ready = self._inject_web_ready
                self._inject_web_ready()

        self._chat_view.loadFinished.connect(on_load_finished)

    def _run_js(self, code, callback=None):
        """Run JavaScript in the embedded WebEngine or WebKit chat page."""
        if not self._use_web_ui or not getattr(self, "_chat_view", None):
            return
        if getattr(self, "_chat_embed_backend", "webengine") == "webkit":
            from windows.embedded_web import run_js as web_run_js

            res = web_run_js(self._chat_view, "webkit", code)
            if callback:
                callback(res)
            return
        page = self._chat_view.page()
        if callback:
            page.runJavaScript(code, callback)
        else:
            page.runJavaScript(code)

    def _inject_web_ready(self):
        """Push theme colors, models, preamble and welcome message to the CEP UI."""
        if getattr(self, "_chat_web_initial_sync_done", False):
            return
        try:
            from classes.app import get_app
            app = get_app()
            theme = app.theme_manager.get_current_theme() if getattr(app, "theme_manager", None) else None
            name = getattr(theme, "name", "Humanity: Dark")
            colors = CHAT_THEME_COLORS.get(name)
            if colors is None:
                colors = CHAT_THEME_COLORS["Bloomberg Light"] if "Light" in name or "Retro" in name else CHAT_THEME_COLORS["Humanity: Dark"]
            self._run_js("setThemeColors(%s);" % json.dumps(json.dumps(colors)))
        except Exception:
            colors = CHAT_THEME_COLORS["Bloomberg Light"]
            self._run_js("setThemeColors(%s);" % json.dumps(json.dumps(colors)))

        self._push_models_for_backend()

        preamble = self._get_preamble_html()
        self._run_js("setPreamble(%s);" % json.dumps(preamble))

        self._run_js("clearMessages();")
        self._push_tabs_to_js()
        self._start_restore_chat_histories_async()
        self._chat_web_initial_sync_done = True

        # Push already-detected CLI status (detection started in __init__, before
        # this page finished loading) so the dropdown doesn't wait another 60s.
        cached_cli_status = getattr(self, "_cli_status", None)
        if cached_cli_status:
            self._run_js(
                "if(window.setCliStatus) setCliStatus(%s);" % json.dumps(cached_cli_status)
            )

        # Push prefetched balance (or loading placeholder) when the web UI is ready.
        try:
            from classes.credits_client import credits as _creds
            cached = _creds.cached_balance()
            if cached is not None:
                self._on_credits_balance(cached)
            elif self._use_web_ui:
                self._run_js(
                    "if(window.updateCreditsBalance) updateCreditsBalance(-1);"
                )
        except Exception:
            pass

    def _start_credits_refresh(self):
        """Fetch credits balance once and start a 60-second refresh timer."""
        self._fetch_credits_balance()
        if not getattr(self, "_credits_timer", None):
            self._credits_timer = QTimer(self)
            self._credits_timer.timeout.connect(self._fetch_credits_balance)
            self._credits_timer.start(60_000)   # refresh every 60 seconds

    def _fetch_credits_balance(self):
        """Fetch balance in a background thread; push result to JS on main thread."""
        def run():
            try:
                from classes.credits_client import credits as _creds
                authed, balance = _creds.balance()
                if not authed:
                    return
                QMetaObject.invokeMethod(
                    self,
                    "_on_credits_balance",
                    Qt.QueuedConnection,
                    Q_ARG(int, balance),
                )
            except Exception as exc:
                log.debug("credits refresh failed: %s", exc)

        threading.Thread(target=run, daemon=True, name="credits-ui-refresh").start()

    @pyqtSlot(int)
    def _on_credits_balance(self, balance: int):
        """Push updated balance to the JS badge (called on main thread)."""
        self._run_js(
            "if(window.updateCreditsBalance) updateCreditsBalance(%s);"
            % json.dumps(balance)
        )

    def _zenvi_models(self):
        """Model-picker entries served by the Zenvi backend."""
        models = []
        try:
            client = get_backend_client()
            api_models = client.list_models()
            default_id = client.get_default_model_id()
            for m in api_models:
                mid = m.get("model_id", "")
                # Pass the picker metadata straight through. The JS side
                # defaults anything missing, so an older backend still works.
                models.append({
                    "id": mid,
                    "name": m.get("display_name", mid),
                    "default": mid == default_id,
                    "provider": m.get("provider", ""),
                    "featured": m.get("featured", True),
                    "rank": m.get("rank", 500),
                    "tags": m.get("tags", []),
                    "available": m.get("available", True),
                })
        except Exception:
            log.debug("Zenvi Assistant: model list unavailable; using empty list")
        return models

    def _models_for_backend(self, backend: str = None):
        """Model-picker entries for *backend* (defaults to the active session's).

        Each backend owns its own lineup — the Zenvi backend serves one from the
        API, Claude Code has a fixed catalogue of Claude models, and a backend
        with no list at all (Codex) leaves the CLI's own default in charge.
        """
        if backend is None:
            sess = self._active_session() or {}
            backend = sess.get("backend", BACKEND_ZENVI)
        if backend == BACKEND_ZENVI:
            return self._zenvi_models()
        from windows.agent_runners import models_for_backend
        return models_for_backend(backend)

    def _push_models_for_backend(self, backend: str = None):
        """Repopulate the model picker for *backend*, and (re)send the backend
        list the web UI needs to label its tabs."""
        if not self._use_web_ui:
            return
        models = self._models_for_backend(backend)
        self._run_js("setModels(%s);" % json.dumps(json.dumps(models)))
        self._run_js(
            "if(window.setBackends) setBackends(%s);" % json.dumps(json.dumps(BACKENDS))
        )

    def _start_cli_detection_refresh(self):
        """Detect claude/codex CLI availability once, then refresh every 60s."""
        self._detect_clis()
        if not getattr(self, "_cli_detect_timer", None):
            self._cli_detect_timer = QTimer(self)
            self._cli_detect_timer.timeout.connect(self._detect_clis)
            self._cli_detect_timer.start(60_000)   # refresh every 60 seconds

    def _detect_clis(self):
        """Check claude/codex CLI availability in a background thread; push to JS."""
        def run():
            try:
                from windows.agent_runners import detect_cli
                status = {
                    BACKEND_CLAUDE: detect_cli("claude"),
                    BACKEND_CODEX: detect_cli("codex"),
                }
                QMetaObject.invokeMethod(
                    self,
                    "_on_cli_status",
                    Qt.QueuedConnection,
                    Q_ARG(str, json.dumps(status)),
                )
            except Exception as exc:
                log.debug("CLI detection failed: %s", exc)

        threading.Thread(target=run, daemon=True, name="cli-detect").start()

    @pyqtSlot(str)
    def _on_cli_status(self, status_json: str):
        """Push CLI availability to the Agent dropdown (called on main thread)."""
        self._cli_status = status_json
        self._notify_agent_selector()
        if self._use_web_ui:
            self._run_js(
                "if(window.setCliStatus) setCliStatus(%s);" % json.dumps(status_json)
            )

    def _push_gap_list(self):
        """Push the current tool-gap log to the JS viewer (called on open)."""
        try:
            from classes.agent_gap_log import read_gaps
            entries = read_gaps()
        except Exception:
            log.debug("read_gaps failed", exc_info=True)
            entries = []
        if self._use_web_ui:
            self._run_js("if(window.setGapList) setGapList(%s);" % json.dumps(json.dumps(entries)))

    def _resolve_gap(self, entry_id: str):
        try:
            from classes.agent_gap_log import mark_resolved
            mark_resolved(entry_id)
        except Exception:
            log.debug("mark_resolved failed", exc_info=True)
        self._push_gap_list()

    def _delete_gap(self, entry_id: str):
        try:
            from classes.agent_gap_log import delete_gap
            delete_gap(entry_id)
        except Exception:
            log.debug("delete_gap failed", exc_info=True)
        self._push_gap_list()

    def _connect_cli(self, backend_id: str):
        """Register Zenvi's MCP server with claude/codex (Connect button in
        the empty state) so an external terminal session can reach it."""
        def run():
            ok, message = False, "Unknown backend."
            try:
                from classes.agent_mcp_server import get_mcp_server
                srv = get_mcp_server().start()
                if backend_id == BACKEND_CLAUDE:
                    from windows.agent_runners import register_claude
                    ok, message = register_claude(srv.port, srv.token)
                elif backend_id == BACKEND_CODEX:
                    from windows.agent_runners import register_codex
                    ok, message = register_codex(srv.port, srv.token)
            except Exception as e:
                log.debug("connect_cli failed: %s", e, exc_info=True)
                ok, message = False, str(e)
            QMetaObject.invokeMethod(
                self, "_on_connect_result", Qt.QueuedConnection,
                Q_ARG(str, backend_id), Q_ARG(bool, ok), Q_ARG(str, message),
            )

        threading.Thread(target=run, daemon=True, name="cli-connect").start()

    @pyqtSlot(str, bool, str)
    def _on_connect_result(self, backend_id: str, ok: bool, message: str):
        """Called on the main thread once register_claude/register_codex finishes."""
        if self._use_web_ui:
            self._run_js(
                "if(window.onConnectResult) onConnectResult(%s, %s, %s);"
                % (json.dumps(backend_id), json.dumps(ok), json.dumps(message))
            )
        self._notify_agent_connect_result(backend_id, ok, message)
        # Status must always be real, never stale — re-check right away so the
        # panel/empty-state flips live instead of waiting for the 60s timer.
        self._detect_clis()

    def _get_preamble_html(self):
        """Return preamble as HTML: AI summary as heading when set, else 'Zenvi Assistant'."""
        if self._first_prompt_summary:
            return '<span class="preamble-title">%s</span>' % html.escape(self._first_prompt_summary.strip())
        return '<span class="preamble-title">Zenvi Assistant</span>'

    def _request_preamble_summary(self, prompt: str):
        """Start a background thread to summarize the first user prompt and update preamble."""
        sess = self._active_session()
        if not sess or sess.get("first_prompt_summary") or not prompt or not prompt.strip():
            return
        active_sid = self._active_sid  # capture for closure

        def run():
            summary = _summarize_prompt(prompt.strip())
            if summary:
                QMetaObject.invokeMethod(
                    self,
                    "_on_preamble_summary",
                    Qt.QueuedConnection,
                    Q_ARG(str, active_sid),
                    Q_ARG(str, summary),
                )

        t = threading.Thread(target=run, daemon=True)
        t.start()

    @pyqtSlot(str, str)
    def _on_preamble_summary(self, session_id: str, text: str):
        """Called on main thread when first-prompt summary is ready."""
        if session_id in self._sessions and not self._sessions[session_id].get("first_prompt_summary") and text:
            self._sessions[session_id]["first_prompt_summary"] = text
            if session_id == self._active_sid:
                self._first_prompt_summary = text
                self._update_preamble()
            self._push_tabs_to_js()
            self._persist_session(session_id, title=text)
            self._save_chat_sessions_store()

    def _clear_widget_tool_blocks(self):
        """Remove live tool blocks (widget mode) at the start of a new request."""
        if self._use_web_ui or not getattr(self, "_widget_tool_container", None):
            return
        while self._widget_tool_container.count() > 1:
            item = self._widget_tool_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._widget_tool_blocks.clear()
        if getattr(self, "_widget_tool_scroll", None):
            self._widget_tool_scroll.setVisible(False)

    def _resolve_agent_mode(self, agent_mode: str = None) -> str:
        sess = self._active_session() or {}
        # Planning mode is a Zenvi-backend feature (the plan events come over
        # the WebSocket). CLI agents plan internally and never emit them, so
        # honouring a stale "planning" here would only mislabel the turn.
        if sess.get("backend", BACKEND_ZENVI) != BACKEND_ZENVI:
            return "agent"
        if agent_mode in ("planning", "agent"):
            return agent_mode
        return sess.get("agent_mode", "agent")

    def _dispatch_user_message(self, text: str, model_id: str, agent_mode: str = None, action: str = "chat", plan_id: str = ""):
        """Shared send pipeline for web and widget chat UIs."""
        self._user_cancelled = False
        sess = self._active_session()
        worker = sess.get("worker")
        if worker is None:
            return
        if self.is_processing and action == "chat" and not sess.get("pending_plan_questions"):
            if text:
                self._run_js("alert('Processing previous message...');")
            return
        mode = self._resolve_agent_mode(agent_mode)
        sess["agent_mode"] = mode
        if sess.get("pending_plan_questions") and action == "chat" and text:
            sess.pop("pending_plan_questions", None)
            if self._use_web_ui:
                self._run_js("if(window.clearPlanQuestions) window.clearPlanQuestions();")
        self._clear_widget_tool_blocks()
        if action == "chat" and text:
            self._add_user_msg(text)
        if action == "chat" and text and self._try_local_command(text):
            return
        if action == "chat" and text:
            self._request_preamble_summary(text)
        augmented_text = self._prepend_editor_snapshot(text) if text else text
        self._set_processing_ui(True)
        QMetaObject.invokeMethod(
            worker,
            "run_request",
            Qt.QueuedConnection,
            Q_ARG(str, augmented_text or ""),
            Q_ARG(str, model_id or ""),
            Q_ARG(str, mode),
            Q_ARG(str, action or "chat"),
            Q_ARG(str, plan_id or ""),
        )
        self._save_chat_sessions_store()

    def _handle_web_send_message(self, text: str, model_id: str, agent_mode: str = None):
        """Handle send from CEP UI (same logic as send_message but with args)."""
        if self.is_processing:
            self._run_js("alert('Processing previous message...');")
            return
        if not text:
            return
        self._dispatch_user_message(text, model_id, agent_mode=agent_mode)

    def _set_agent_mode(self, agent_mode: str):
        sess = self._active_session()
        sess["agent_mode"] = agent_mode if agent_mode in ("planning", "agent") else "agent"
        if self._use_web_ui:
            self._run_js("if(window.setAgentModeUI) window.setAgentModeUI(%s);" % json.dumps(sess["agent_mode"]))
        self._save_chat_sessions_store()

    def _execute_plan(self, plan_id: str, model_id: str):
        """Run deterministic plan executor via backend."""
        if self.is_processing:
            self._run_js("alert('Processing previous message...');")
            return
        sess = self._active_session()
        worker = sess.get("worker")
        if worker is None or not getattr(worker, "_backend_session_id", None):
            self._run_js("alert('Start planning in this chat tab first so the plan is linked to a session.');")
            return
        plan = sess.get("current_plan") or {}
        status = (plan.get("status") or "").lower()
        if status not in ("ready", "blocked", "completed", "executing"):
            self._run_js("alert('Plan is not ready to execute yet.');")
            return
        if status in ("blocked", "completed", "executing"):
            for step in plan.get("steps") or []:
                st = (step.get("status") or "").lower()
                if st in ("failed", "blocked") or (st == "skipped" and step.get("last_error")):
                    step["status"] = "pending"
                    step["last_error"] = ""
            plan["status"] = "ready"
            sess["current_plan"] = plan
            if self._use_web_ui:
                self._run_js("if(window.setPlanChip) window.setPlanChip(%s);" % json.dumps(plan))
            main_win = self.parent()
            if hasattr(main_win, "dockPlan") and main_win.dockPlan:
                try:
                    main_win.dockPlan.load_plan(plan)
                except Exception:
                    pass
        self._add_assistant_msg("Executing plan…")
        sess["agent_mode"] = "agent"
        if self._use_web_ui:
            self._run_js("if(window.setAgentModeUI) window.setAgentModeUI('agent');")
        self._dispatch_user_message(
            "",
            model_id,
            agent_mode="agent",
            action="execute_plan",
            plan_id=plan_id or plan.get("plan_id", ""),
        )

    def _edit_plan_in_planning_mode(self):
        """Switch to Plan mode and prefill chat to revise a failed step."""
        sess = self._active_session()
        plan = (sess or {}).get("current_plan") or {}
        failed_id = ""
        err = ""
        for step in plan.get("steps") or []:
            if (step.get("status") or "").lower() == "failed":
                failed_id = step.get("step_id") or ""
                err = (step.get("last_error") or "")[:240]
                break
        if not failed_id:
            failed_id = plan.get("current_step_id") or ""
        text = f"Revise step {failed_id}"
        if err:
            text += f": execution failed with — {err}"
        self._set_agent_mode("planning")
        if self._use_web_ui:
            self._run_js("if(window.setChatInput) window.setChatInput(%s);" % json.dumps(text))
        else:
            self._add_user_msg(text)

    def _on_plan_event(self, event_type: str, payload_json: str):
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except Exception:
            payload = {}
        if sid in self._sessions:
            if event_type == "plan_ready":
                self._sessions[sid]["current_plan"] = payload
            elif event_type == "plan_updated":
                self._sessions[sid]["current_plan"] = payload
                if sid == self._active_sid:
                    main_win = self.parent()
                    if hasattr(main_win, "dockPlan") and main_win.dockPlan:
                        try:
                            main_win.dockPlan.load_plan(payload)
                        except Exception:
                            pass
                    if self._use_web_ui:
                        self._run_js(
                            "if(window.setPlanChip) window.setPlanChip(%s);"
                            % json.dumps(payload)
                        )
            elif event_type == "plan_questions":
                self._sessions[sid]["pending_plan_questions"] = payload.get("questions") or []
                self._sessions[sid]["awaiting_plan_answers"] = True
            elif event_type == "plan_step_status":
                plan = self._sessions[sid].get("current_plan") or {}
                step_id = payload.get("step_id", "")
                for step in plan.get("steps") or []:
                    if step.get("step_id") == step_id:
                        step["status"] = payload.get("status", step.get("status"))
                        if payload.get("error"):
                            step["last_error"] = payload.get("error")
                        break
                self._sessions[sid]["current_plan"] = plan
            if event_type == "plan_execution_done":
                if payload.get("plan"):
                    self._sessions[sid]["current_plan"] = payload["plan"]
                else:
                    plan = self._sessions[sid].get("current_plan") or {}
                    if plan:
                        plan["status"] = payload.get("status", plan.get("status"))
                        self._sessions[sid]["current_plan"] = plan
        if sid != self._active_sid:
            return
        main_win = self.parent()
        if event_type == "plan_ready":
            steps = payload.get("steps") or []
            if payload.get("status", "").lower() != "ready" or not steps:
                return
            if hasattr(main_win, "dockPlan") and main_win.dockPlan:
                try:
                    main_win.dockPlan.load_plan(payload)
                except Exception as e:
                    log.warning("Failed to show plan dock: %s", e)
            if self._use_web_ui:
                self._run_js(
                    "if(window.setPlanChip) window.setPlanChip(%s);"
                    % json.dumps(payload)
                )
                self._run_js("if(window.clearPlanQuestions) window.clearPlanQuestions();")
        elif event_type == "plan_questions":
            if sid in self._sessions:
                self._sessions[sid]["pending_plan_questions"] = payload.get("questions") or []
                self._sessions[sid]["awaiting_plan_answers"] = True
            self._set_processing_ui(False)
            if self._use_web_ui:
                self._run_js("if(window.setProcessing) window.setProcessing(false);")
                self._run_js(
                    "if(window.setPlanQuestions) window.setPlanQuestions(%s);"
                    % json.dumps(payload.get("questions") or [])
                )
                # Stop showing further streamed prose for this turn (questions live in the panel).
                self._run_js("if(window.suppressStreamingMessage) window.suppressStreamingMessage();")
                self._token_buffer.clear()
                self._token_flush_scheduled = False
        if event_type == "plan_step_status":
            if hasattr(main_win, "dockPlan") and main_win.dockPlan:
                try:
                    main_win.dockPlan.show()
                    main_win.dockPlan.update_step_status(
                        payload.get("step_id", ""),
                        payload.get("status", ""),
                        payload.get("error", ""),
                    )
                except Exception as e:
                    log.debug("plan dock step update: %s", e)
            if self._use_web_ui:
                self._run_js(
                    "if(window.updatePlanChipProgress) window.updatePlanChipProgress(%s, %s, %s);"
                    % (
                        json.dumps(payload.get("step_id", "")),
                        json.dumps(payload.get("status", "")),
                        json.dumps(payload.get("error", "")),
                    )
                )
        if event_type == "plan_execution_done":
            plan = self._sessions.get(sid, {}).get("current_plan")
            if plan and hasattr(main_win, "dockPlan") and main_win.dockPlan:
                try:
                    main_win.dockPlan.load_plan(plan)
                except Exception:
                    pass
            if self._use_web_ui:
                plan = self._sessions.get(sid, {}).get("current_plan")
                if plan:
                    self._run_js(
                        "if(window.setPlanChip) window.setPlanChip(%s);"
                        % json.dumps(plan)
                    )
                else:
                    self._run_js("if(window.setPlanChip) window.setPlanChip(null);")
            if sid == self._active_sid:
                completed = payload.get("completed", 0)
                skipped = payload.get("skipped", 0)
                repaired = payload.get("repaired", 0)
                if skipped:
                    self._add_assistant_msg(
                        f"Plan finished — {completed} step(s) done"
                        + (f", {repaired} auto-repaired" if repaired else "")
                        + f", {skipped} auto-skipped after self-heal."
                    )
                elif repaired:
                    self._add_assistant_msg(
                        f"Plan completed — {completed} step(s), {repaired} auto-repaired in-run."
                    )
        if event_type == "mode_changed":
            mode = payload.get("agent_mode")
            if mode in ("planning", "agent"):
                self._set_agent_mode(mode)

    def _shutdown_worker(self, worker, thread, wait_ms=3000):
        """Stop one chat worker thread (cancel WS/CLI, quit, wait, terminate)."""
        if worker:
            # Mark the worker as stopping FIRST so that when
            # cancel_current_request() closes the WebSocket, the worker's
            # run_request slot sees _stopping=True and returns silently
            # instead of falling back to REST or emitting signals into a Qt
            # stack that is already being torn down.
            worker._stopping = True
            if hasattr(worker, "cancel"):
                try:
                    worker.cancel()  # terminate any CLI subprocess
                except Exception:
                    pass
        try:
            from classes.api_client import get_backend_client
            get_backend_client().cancel_current_request()
        except Exception:
            pass
        if thread and thread.isRunning():
            thread.quit()
            if not thread.wait(wait_ms):
                log.warning("AI chat thread did not stop within %d ms; terminating", wait_ms)
                thread.terminate()
                thread.wait(1000)

    def _stop_all_threads(self):
        """Cleanly stop all session worker threads. Safe to call more than once."""
        try:
            from classes.api_client import get_backend_client
            get_backend_client().cancel_current_request()
        except Exception:
            pass
        if getattr(self, "_credits_timer", None):
            try:
                self._credits_timer.stop()
            except Exception:
                pass
        try:
            from windows.agent_runners import cleanup_agent_mcp_configs
            cleanup_agent_mcp_configs()
        except Exception:
            pass
        # Neither shutdown path saved the tab list before this.
        self._save_chat_sessions_store()
        for sess in list(self._sessions.values()):
            self._shutdown_worker(sess.get("worker"), sess.get("thread"))
        try:
            from classes import chat_history
            # An untitled project that was never chatted in leaves nothing behind.
            key = getattr(self, "_history_key", "") or ""
            if key.startswith("draft:"):
                chat_history.discard_empty_bucket(key)
            chat_history.close()
        except Exception:
            pass

    def closeEvent(self, event):
        """Stop all AI worker threads when the dock is explicitly closed."""
        self._stop_all_threads()
        super().closeEvent(event)

    def showEvent(self, event):
        """Run fade-in animation the first time the dock is shown (widget UI only)."""
        super().showEvent(event)
        if not self._use_web_ui and not self._chat_fade_done and self._chat_opacity_effect and self._chat_fade_anim:
            self._chat_opacity_effect.setOpacity(0.0)
            self._chat_fade_anim.stop()
            self._chat_fade_anim.start()

    def _on_chat_fade_finished(self):
        self._chat_fade_done = True
        self._chat_opacity_effect.setOpacity(1.0)

    def _update_preamble(self):
        """Update preamble text with current context (project name, tips)."""
        text = self._get_preamble_html()
        if self._use_web_ui:
            self._run_js("setPreamble(%s);" % json.dumps(text))
        elif self.preamble_label:
            self.preamble_label.setText(text)

    def _populate_models(self):
        """Populate model combo from the backend API."""
        models = []
        default_id = ""
        try:
            client = get_backend_client()
            api_models = client.list_models()
            default_id = client.get_default_model_id()
            for m in api_models:
                mid = m.get("model_id", "")
                models.append((
                    mid,
                    m.get("display_name", mid),
                    m.get("provider", "") or (mid.split("/", 1)[0] if "/" in mid else ""),
                ))
        except Exception:
            log.debug(
                "Zenvi Assistant: model list unavailable for widget UI; combo left empty until backend is up"
            )
        if not models:
            self.model_combo.addItem("No AI providers loaded", "")
            return

        # This is only the no-web-view fallback, so keep it simple: group by
        # provider with a disabled separator row so a long catalog stays
        # navigable in a plain combo box.
        provider_labels = {
            "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
            "xai": "xAI", "ollama": "Ollama",
        }
        current_provider = None
        for model_id, display_name, provider in models:
            if provider != current_provider:
                current_provider = provider
                label = provider_labels.get(provider, provider or "Other")
                self.model_combo.addItem("── %s ──" % label, "")
                sep_idx = self.model_combo.count() - 1
                # Separator rows must not be selectable.
                self.model_combo.model().item(sep_idx).setEnabled(False)
            self.model_combo.addItem(display_name, model_id)
        idx = self.model_combo.findData(default_id)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)

    def _key_press(self, event):
        if event.key() == Qt.Key_Return and event.modifiers() != Qt.ShiftModifier:
            self.send_message()
        else:
            QTextEdit.keyPressEvent(self.msg_input, event)

    _WATCH_CLIP_PATTERNS = [
        "watch clip", "view clip", "show clip", "play clip",
        "play the feral", "show the feral", "watch the feral",
        "play the trailer", "show me the clip", "show me the trailer",
        "load the feral", "open the feral",
    ]

    def _try_local_command(self, text: str) -> bool:
        """Execute certain commands locally without sending to the backend.
        Returns True if the command was handled and no backend call is needed.
        """
        lower = text.lower().strip()
        if any(pat in lower for pat in self._WATCH_CLIP_PATTERNS):
            self._add_system_msg("Loading Feral trailer and starting playback...")
            try:
                from classes.tool_handlers import execute_tool
                result = execute_tool("watch_clip_tool", {})
                self._add_assistant_msg(result)
            except Exception as exc:
                self._add_assistant_msg(f"Error: {exc}")
            self._set_processing_ui(False)
            return True
        return False

    def send_message(self):
        if self.is_processing:
            QMessageBox.warning(self, "Wait", "Processing previous message...")
            return
        text = self.msg_input.toPlainText().strip()
        if not text:
            return
        self.msg_input.clear()
        model_id = self.model_combo.currentData()
        if not model_id and self.model_combo.count():
            model_id = self.model_combo.currentText()
        model_id_str = model_id if model_id else ""
        self._dispatch_user_message(text, model_id_str)
        self.msg_input.setFocus()

    def _set_processing_ui(self, processing: bool):
        """Update Send/Cancel visibility and enabled state."""
        self.is_processing = processing
        sess = self._active_session()
        if sess:
            sess["processing"] = processing
        if self._use_web_ui:
            self._run_js("setProcessing(%s);" % ("true" if processing else "false"))
            # Refresh tab bar so per-session processing indicators stay in sync.
            self._push_tabs_to_js()
            return
        # Widget mode: rebuild tabs so the active processing indicator updates.
        if getattr(self, "_widget_tabs_layout", None):
            self._rebuild_widget_tabs()
        if self.send_btn:
            self.send_btn.setEnabled(not processing)
            self.send_btn.setText("Processing..." if processing else "Send")
        if self.cancel_btn:
            self.cancel_btn.setVisible(processing)
        if not processing and self.msg_input:
            self.msg_input.setFocus()

    def cancel_request(self):
        """Stop the in-flight request and reset the chat UI."""
        self._user_cancelled = True
        self._token_buffer.clear()
        self._token_flush_scheduled = False
        try:
            from classes.api_client import get_backend_client
            get_backend_client().cancel_current_request()
        except Exception:
            pass
        # CLI backends: terminate the running subprocess for the active session.
        sess = self._active_session()
        worker = sess.get("worker") if sess else None
        if worker is not None and hasattr(worker, "cancel"):
            try:
                worker.cancel()
            except Exception:
                pass
        if self._use_web_ui:
            self._run_js("if(window.resetStreamingMessage) window.resetStreamingMessage();")
        self._set_processing_ui(False)

    def _schedule_token_flush(self):
        if self._token_flush_scheduled:
            return
        self._token_flush_scheduled = True
        QTimer.singleShot(24, self._flush_token_buffer)

    def _flush_token_buffer(self):
        self._token_flush_scheduled = False
        if not self._token_buffer:
            return
        chunk = "".join(self._token_buffer)
        self._token_buffer.clear()
        if not chunk or self._user_cancelled:
            return
        self._run_js(
            "if(window.appendOrUpdateStreamingMessage) window.appendOrUpdateStreamingMessage(%s);"
            % json.dumps(chunk)
        )

    @pyqtSlot(str)
    def _on_token(self, text: str):
        """Forward a streamed LLM token chunk to the active chat view."""
        if not text or self._user_cancelled:
            return
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        if sid != self._active_sid:
            return
        if (self._sessions.get(sid) or {}).get("awaiting_plan_answers"):
            return
        if self._use_web_ui:
            self._token_buffer.append(text)
            self._schedule_token_flush()

    def _tool_result_summary(self, result: str) -> str:
        if not result:
            return ""
        first_line = result.strip().splitlines()[0] if result.strip() else ""
        if len(first_line) > 140:
            return first_line[:140] + "…"
        return first_line

    @pyqtSlot(str, str, str)
    def _on_tool_started(self, call_id: str, tool_name: str, args_json: str):
        """Render a Cursor-style collapsible terminal block for a tool call."""
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        # Recorded for every tab, not just the visible one — a background tab's
        # activity should still be there when the user switches to it.
        self._record_tool_started(sid, call_id, tool_name)
        if sid != self._active_sid:
            return
        # Drop any pre-tool "thinking" that already streamed into the answer bubble.
        self._token_buffer.clear()
        self._token_flush_scheduled = False
        if self._use_web_ui:
            self._run_js(
                "if(window.resetStreamingMessage) window.resetStreamingMessage();"
                "if(window.reopenThinkingForTools) window.reopenThinkingForTools();"
            )
        try:
            args = json.loads(args_json) if args_json else {}
        except Exception:
            args = {}
        title = humanize_tool_name(tool_name)
        cmd = _format_tool_command(tool_name, args)
        # Full args for expand/inspect (bounded)
        try:
            args_pretty = json.dumps(args, indent=2, ensure_ascii=False, default=str)
        except Exception:
            args_pretty = str(args)
        if len(args_pretty) > 4000:
            args_pretty = args_pretty[:4000] + "\n…"
        if self._use_web_ui:
            payload = {
                "call_id": call_id,
                "title": title,
                "cmd": cmd,
                "args_detail": args_pretty,
                "tool_name": tool_name or "",
            }
            self._run_js("if(window.addToolBlock) window.addToolBlock(%s);"
                         % json.dumps(json.dumps(payload)))
            return
        if not getattr(self, "_widget_tool_container", None):
            return
        block_id = call_id or tool_name or ("tool_%s" % time.time())
        block = WidgetToolBlock(block_id, title, cmd, parent=self._widget_tool_blocks_host)
        if args_pretty:
            block.append_log("ARGS:\n" + args_pretty)
        insert_at = max(0, self._widget_tool_container.count() - 1)
        self._widget_tool_container.insertWidget(insert_at, block)
        self._widget_tool_blocks[block_id] = block
        self._widget_tool_scroll.setVisible(True)

    @pyqtSlot(str, str)
    def _on_tool_log(self, call_id: str, line: str):
        """Append one log line to a running tool block."""
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        if sid != self._active_sid or not line:
            return
        if self._use_web_ui:
            self._run_js("if(window.appendToolLog) window.appendToolLog(%s, %s);"
                         % (json.dumps(call_id), json.dumps(line)))
            return
        block = self._widget_tool_blocks.get(call_id)
        if block:
            block.append_log(line)

    @pyqtSlot(str, bool, str)
    def _on_tool_completed(self, call_id: str, ok: bool, result: str):
        """Mark a tool block as done/error and keep result text for inspection."""
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        self._record_tool_completed(sid, call_id, ok)
        if sid != self._active_sid:
            return
        summary = self._tool_result_summary(result)
        detail = (result or "").strip()
        if len(detail) > 6000:
            detail = detail[:6000] + "\n…"
        if self._use_web_ui:
            if detail:
                self._run_js(
                    "if(window.appendToolLog) window.appendToolLog(%s, %s);"
                    % (json.dumps(call_id), json.dumps("RESULT:\n" + detail))
                )
            self._run_js(
                "if(window.completeToolBlock) window.completeToolBlock(%s, %s, %s);"
                % (json.dumps(call_id), "true" if ok else "false", json.dumps(summary))
            )
            return
        block = self._widget_tool_blocks.get(call_id)
        if block:
            if detail:
                block.append_log("RESULT:\n" + detail)
            block.complete(ok, summary)

    @pyqtSlot(str)
    def _on_response_ready(self, text: str):
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        if sid in self._sessions:
            self._sessions[sid]["processing"] = False
            if sid == self._active_sid:
                self._sessions[sid]["unread"] = False
            else:
                # Widget mode uses an in-Python unread flag; WebEngine unread is
                # tracked inside chat.js.
                if not self._use_web_ui:
                    self._sessions[sid]["unread"] = True
        if sid == self._active_sid:
            if self._user_cancelled:
                self._user_cancelled = False
                self._token_buffer.clear()
                self._token_flush_scheduled = False
                if self._use_web_ui:
                    self._run_js("if(window.resetStreamingMessage) window.resetStreamingMessage();")
                self._set_processing_ui(False)
                return
            self._flush_token_buffer()
            # If we streamed tokens, replace the streaming bubble with the
            # finalised markdown-rendered message instead of appending a new one.
            if self._use_web_ui:
                self._run_js("if(window.finalizeStreamingMessage) window.finalizeStreamingMessage();")
            awaiting = bool((self._sessions.get(sid) or {}).get("awaiting_plan_answers"))
            pending_q = (self._sessions.get(sid) or {}).get("pending_plan_questions")
            if awaiting and pending_q:
                # Only suppress prose while the question panel is still active.
                self._sessions[sid]["awaiting_plan_answers"] = False
                short = (text or "").strip()
                if short and "question" in short.lower() and len(short) < 400:
                    self._add_assistant_msg(short)
                else:
                    self._add_assistant_msg(
                        "Answer the questions in the panel above (or Skip) so I can finalize the plan."
                    )
            else:
                if sid in self._sessions:
                    self._sessions[sid]["awaiting_plan_answers"] = False
                body = (text or "").strip()
                if not body or body == "Done.":
                    body = (
                        "Still working on the plan — say \"continue the plan\" "
                        "if nothing appears in the Plan dock."
                    )
                self._add_assistant_msg(body)
            self._set_processing_ui(False)
        else:
            # Background session — store message and notify JS for unread badge
            if sid in self._sessions:
                # Same normalisation the active path applies, so what we persist
                # doesn't depend on which tab happened to be in front.
                text = self._strip_thinking(text)
                html_body = _markdown_to_html(text)
                self._sessions[sid]["messages"].append(("assistant", html_body, True))
                self._record_message(sid, "assistant", text)
                self._run_js(
                    "if(window.onBackgroundResponse) window.onBackgroundResponse(%s, %s);"
                    % (json.dumps(sid), json.dumps(html_body))
                )
            if self._use_web_ui:
                self._push_tabs_to_js()
            else:
                self._rebuild_widget_tabs()

    @pyqtSlot(str)
    def _on_error(self, text: str):
        sid = getattr(self.sender(), "_session_id", self._active_sid)
        if sid in self._sessions:
            self._sessions[sid]["processing"] = False
        if sid == self._active_sid:
            if self._user_cancelled:
                self._user_cancelled = False
                self._token_buffer.clear()
                self._token_flush_scheduled = False
                if self._use_web_ui:
                    self._run_js("if(window.resetStreamingMessage) window.resetStreamingMessage();")
                return
            self._token_buffer.clear()
            self._token_flush_scheduled = False
            log.debug("ai_chat_ui _on_error: %s", text[:80] if text else "")
            if self._use_web_ui:
                self._run_js("if(window.resetStreamingMessage) window.resetStreamingMessage();")
            self._add_system_msg("Error: %s" % text)
            self._set_processing_ui(False)

    def clear_chat(self):
        reply = QMessageBox.question(
            self, "Clear", "Clear chat?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._first_prompt_summary = None
            sess = self._active_session()
            if sess:
                sess["messages"] = []
                sess["first_prompt_summary"] = None
                sess["unread"] = False
                from classes import chat_history
                chat_history.clear_session_messages(self._active_sid)
                # The worker forgets its CLI conversation too, so drop the
                # stored resume state or we'd try to rejoin a dead thread.
                chat_history.update_session(
                    self._active_sid, cli_session_id="", cli_started=False
                )
                worker = sess.get("worker")
                if worker:
                    QMetaObject.invokeMethod(worker, "clear_session", Qt.QueuedConnection)
            if self._use_web_ui:
                self._run_js("clearMessages();")
                self._push_tabs_to_js()
            else:
                self._clear_widget_tool_blocks()
                self.chat_box.clear()
                self._rebuild_widget_tabs()
            self._update_preamble()
            self._add_chrome_msg("Chat cleared. Ask anything about your project or editing.")
            # The cleared title otherwise sat unpersisted until some later save.
            self._save_chat_sessions_store()

    def _add_user_msg(self, text):
        self._add_msg(text, "user", is_assistant=False, is_system=False)

    def _add_chrome_msg(self, text):
        """A UI banner (welcome, "chat cleared") — shown but never persisted."""
        self._add_msg(text, "system", is_assistant=False, is_system=True, ephemeral=True)

    @staticmethod
    def _strip_thinking(text):
        """Drop leaked thinking headers that sometimes prefix a final reply."""
        text = re.sub(
            r"(?im)^\s*Thought for\s+(?:<)?\d+(?:\.\d+)?(?:s| sec| seconds)?\.?\s*\n+",
            "",
            text or "",
        )
        text = re.sub(r"(?im)^\s*Thinking(?:…|\.\.\.)?\s*\n+", "", text).strip()
        return re.sub(
            r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    def _add_assistant_msg(self, text):
        self._add_msg(
            self._strip_thinking(text), "assistant", is_assistant=True, is_system=False
        )

    def _add_system_msg(self, text):
        self._add_msg(text, "system", is_assistant=False, is_system=True)

    def _add_msg(self, text, role, is_assistant=False, is_system=False, ephemeral=False):
        # Every backend funnels its final messages through here with the raw
        # text still in hand, which is why this is the one persistence hook.
        if not ephemeral:
            self._record_message(self._active_sid, role, text)
        if self._use_web_ui:
            if is_assistant:
                html_body = _markdown_to_html(text)
            else:
                safe = html.escape(text).replace("\n", "<br/>")
                html_body = "<p>" + safe + "</p>"
            # Store for replay when the user switches back to this tab
            sess = self._active_session()
            if sess is not None:
                sess["messages"].append((role, html_body, is_assistant))
            self._run_js("appendMessage(%s, %s, %s);" % (
                json.dumps(role),
                json.dumps(html_body),
                "true" if is_assistant else "false",
            ))
            return
        if is_assistant:
            html_body = _markdown_to_html(text)
        else:
            safe = html.escape(text).replace("\n", "<br/>")
            html_body = "<p>" + safe + "</p>"

        # Store for replay when switching sessions (widget mode).
        sess = self._active_session()
        if sess is not None:
            sess["messages"].append((role, html_body, is_assistant))

        self._display_stored_msg_widget(role, html_body, is_assistant)
