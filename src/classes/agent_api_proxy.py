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


# Tools the in-app MCP server exposes on top of the editor tools. These are for
# external agent CLIs only — the built-in assistant runs inside the backend
# this proxies to, so it has no use for them.
MCP_EXTRA_TOOLS = {
    "zenvi_api_request": zenvi_api_request,
}
