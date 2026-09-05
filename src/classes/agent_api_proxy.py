"""
 @file
 @brief Lets an external agent CLI call the Zenvi backend API as the signed-in
        user, without ever handling the user's token.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""

import json as _json
import os
import logging
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
REQUEST_TIMEOUT = 30          # seconds
MAX_RESPONSE_CHARS = 12_000   # keep a huge payload from swamping the agent's context


def _backend_url() -> str:
    """The Zenvi backend the app itself is configured to talk to."""
    from classes.api_client import ZenviBackendClient
    return ZenviBackendClient._get_backend_url().rstrip("/")


def _access_token():
    """A currently-valid access token, refreshed by AuthManager if it had expired."""
    from classes.auth_manager import AuthManager
    return AuthManager.instance().get_access_token()


def zenvi_api_request(method: str = "GET", path: str = "", body: str = "",
                      query: str = "") -> str:
    """Call the Zenvi backend API (api.zenvi.pro) as the signed-in Zenvi user.

    Use this for anything served by the Zenvi backend — models, chat history,
    credits, projects, account. Zenvi attaches the user's authentication itself,
    so no token is needed here and none is ever exposed to the agent.

    ``method`` is one of GET, POST, PUT, PATCH, DELETE. ``path`` is a path on the
    backend such as ``/api/v1/models`` — not a full URL; requests to any other
    host are refused. ``body`` is a JSON string sent as the request body, and
    ``query`` is a JSON object of query-string parameters. Returns the HTTP
    status followed by the response body.
    """
    import requests

    verb = (method or "GET").strip().upper()
    if verb not in ALLOWED_METHODS:
        return "Error: unsupported method %r. Use one of: %s" % (
            method, ", ".join(ALLOWED_METHODS))

    target = (path or "").strip()
    if not target:
        return "Error: 'path' is required, e.g. /api/v1/models"

    # Only ever the configured Zenvi backend: this call carries the user's
    # credentials, so it must not be steerable into a request to another host.
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        base_host = urlsplit(_backend_url()).netloc
        if parts.netloc != base_host:
            return ("Error: 'path' must be a path on the Zenvi backend (%s), "
                    "not a URL to another host." % base_host)
        target = parts.path + (("?" + parts.query) if parts.query else "")
    if not target.startswith("/"):
        target = "/" + target

    params = None
    if query:
        try:
            params = _json.loads(query) if isinstance(query, str) else dict(query)
        except Exception:
            return "Error: 'query' must be a JSON object, e.g. {\"limit\": 10}"
        if not isinstance(params, dict):
            return "Error: 'query' must be a JSON object, e.g. {\"limit\": 10}"

    payload = None
    if body:
        try:
            payload = _json.loads(body) if isinstance(body, str) else body
        except Exception:
            return "Error: 'body' must be a JSON string, e.g. {\"name\": \"demo\"}"

    token = _access_token()
    if not token:
        return ("Error: not signed in to Zenvi. Ask the user to sign in from the "
                "app, then try again.")

    url = _backend_url() + target
    headers = {"Authorization": "Bearer %s" % token,
               "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            verb, url, headers=headers, params=params, json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        # Never let the token reach the log or the agent.
        log.warning("zenvi_api_request %s %s failed: %s", verb, target, exc)
        return "Error: request to %s failed: %s" % (target, exc)

    text = response.text or ""
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + "\n… (truncated)"
    return "HTTP %s %s %s\n%s" % (response.status_code, verb, target, text)


# ---------------------------------------------------------------------------
# Harness tools (#150)
#
# A scheduled routine cannot click: no file picker, no chat dock, no Cancel
# button. These three tools give such a run the pieces it is otherwise missing —
# a liveness probe, a way to prompt the native assistant, and log pointers for
# when something hangs. They live here rather than in AGENT_TOOL_HANDLERS
# because the MCP server calls extras straight on a worker thread instead of
# marshalling them to the Qt GUI thread, which is exactly what a probe for a
# wedged GUI thread needs.
# ---------------------------------------------------------------------------

ASSISTANT_TURN_TIMEOUT = 900   # seconds; matches the backend's tool-call budget


def _core_log_paths() -> list:
    """(label, path) for every log an unattended run may need to read back."""
    from classes import info

    user = info.USER_PATH
    return [
        ("core+assistant", os.path.join(user, "openshot-qt.log")),
        ("assistant gaps", os.path.join(user, "agent_tool_gaps.jsonl")),
        ("crash", os.path.join(user, "crash.log")),
        ("faulthandler", os.path.join(user, "faulthandler.log")),
        ("libopenshot", os.path.join(user, "libopenshot.log")),
    ]


def get_log_paths_tool() -> str:
    """Report where Zenvi's logs live, for diagnosing a hung or failed harness run.

    Lists the editor log files with their size and last-modified time. The native
    assistant has no separate log — its chat and tool activity is written into
    openshot-qt.log. The backend logs to stdout only, so its output lives in the
    terminal that started it; per-session agent traces are on disk under the
    backend's logs/agent_traces/<session_id>.jsonl.
    """
    import time

    lines = []
    for label, path in _core_log_paths():
        try:
            st = os.stat(path)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            lines.append("%-16s %s (%d bytes, modified %s)"
                         % (label, path, st.st_size, when))
        except OSError:
            lines.append("%-16s %s (not present)" % (label, path))

    try:
        backend = _backend_url()
    except Exception:
        backend = "(unknown)"
    lines.append("%-16s %s - logs to stdout only; agent traces at "
                 "logs/agent_traces/<session_id>.jsonl, also readable via "
                 "GET /api/v1/chat/sessions/{session_id}/trace"
                 % ("backend", backend))
    return "\n".join(lines)


def mcp_health_tool(main_thread_timeout_seconds: int = 5) -> str:
    """Check that the editor is up AND its Qt main thread is still alive.

    Read-only tools keep answering from a worker thread even when the GUI thread
    is wedged, so "MCP responds" is not proof the editor works. This runs a no-op
    on the main thread and reports whether it came back. If main_thread_alive is
    false, mutating tools (import, export, timeline edits) will time out — stop
    and collect the logs from get_log_paths_tool rather than retrying.
    """
    import time

    try:
        budget = max(1, int(main_thread_timeout_seconds))
    except Exception:
        budget = 5

    parts = []
    try:
        from classes.agent_mcp_server import get_mcp_server
        srv = get_mcp_server()
        parts.append("mcp_port=%s" % (srv.port if srv.port else "not-started"))
    except Exception as exc:
        parts.append("mcp_port=error(%s)" % exc)

    try:
        from classes.agent_mcp_server import iter_tool_defs
        parts.append("tools=%d" % len(iter_tool_defs()))
    except Exception as exc:
        parts.append("tools=error(%s)" % exc)

    try:
        from classes.tool_handlers import _run_on_main_thread
        started = time.time()
        _run_on_main_thread(lambda: True, timeout=budget)
        parts.append("main_thread_alive=true")
        parts.append("main_thread_latency_ms=%d" % ((time.time() - started) * 1000))
    except Exception as exc:
        parts.append("main_thread_alive=false")
        parts.append("main_thread_error=%s" % exc)

    return " ".join(parts)


def send_assistant_prompt_tool(message: str = "", session_id: str = "",
                               timeout_seconds: int = ASSISTANT_TURN_TIMEOUT) -> str:
    """Send a prompt to Zenvi's own assistant and wait for the whole turn to finish.

    This is how an unattended run reaches the assistant that would normally be
    driven from the chat dock: it edits the timeline through the same editor
    tools. Returns the assistant's final reply and the session_id, so a follow-up
    prompt can continue the same conversation. If the turn is still running after
    timeout_seconds (default 900) it is cancelled and reported as status=timeout
    rather than left running.
    """
    import threading
    import uuid

    text = (message or "").strip()
    if not text:
        return "Error: 'message' is required — the prompt to send to the assistant."

    try:
        budget = max(1, int(timeout_seconds))
    except Exception:
        budget = ASSISTANT_TURN_TIMEOUT

    token = _access_token()
    if not token:
        return ("Error: not signed in to Zenvi. Ask the user to sign in from the "
                "app, then try again.")

    sid = (session_id or "").strip() or str(uuid.uuid4())

    # A private client: cancel_current_request() closes every socket on the
    # client it is called on, so sharing the app's singleton would tear down a
    # live chat turn in the dock whenever a harness run timed out.
    from classes.api_client import ZenviBackendClient
    client = ZenviBackendClient()

    final = {"text": None, "session_id": sid, "error": None}

    def _on_response(response_text, response_session_id=""):
        final["text"] = response_text
        if response_session_id:
            final["session_id"] = response_session_id

    def _on_error(error_message):
        final["error"] = error_message

    def _on_tool_call(tool_name, tool_args, call_id=""):
        # Without this the client acks every tool call with "no tool handler"
        # and the assistant cannot touch the timeline at all.
        from classes.tool_handlers import execute_tool
        args = dict(tool_args or {})
        args["chat_session_id"] = final["session_id"]
        try:
            return execute_tool(tool_name, args)
        except Exception as exc:
            log.warning("assistant tool %s failed: %s", tool_name, exc)
            return "Error: %s" % exc

    def _run():
        try:
            result = client.send_message_ws(
                message=text,
                session_id=sid,
                auth_token=token,
                agent_mode="agent",
                action="chat",
                on_response=_on_response,
                on_error=_on_error,
                on_tool_call=_on_tool_call,
            )
            # send_message_ws can return None even after a clean 'done', so the
            # on_response text is the authoritative answer.
            if final["text"] is None and result:
                final["text"] = result
        except Exception as exc:
            final["error"] = str(exc)

    worker = threading.Thread(target=_run, name="zenvi-mcp-assistant", daemon=True)
    worker.start()
    worker.join(budget)

    if worker.is_alive():
        try:
            client.cancel_current_request()
        except Exception as exc:
            log.warning("cancelling assistant turn failed: %s", exc)
        return ("status=timeout session_id=%s\nThe assistant turn did not finish "
                "within %ss and was cancelled. Logs:\n%s"
                % (final["session_id"], budget, get_log_paths_tool()))

    if final["error"] and final["text"] is None:
        return "status=error session_id=%s\n%s" % (final["session_id"], final["error"])

    return "status=complete session_id=%s\n\n%s" % (
        final["session_id"], final["text"] or "(the assistant returned no text)")


# Tools the in-app MCP server exposes on top of the editor tools. These are for
# external agent CLIs only — the built-in assistant runs inside the backend
# this proxies to, so it has no use for them (and must not be able to prompt
# itself, which is why send_assistant_prompt_tool is here and not in
# AGENT_TOOL_HANDLERS).
MCP_EXTRA_TOOLS = {
    "zenvi_api_request": zenvi_api_request,
    "send_assistant_prompt_tool": send_assistant_prompt_tool,
    "mcp_health_tool": mcp_health_tool,
    "get_log_paths_tool": get_log_paths_tool,
}
