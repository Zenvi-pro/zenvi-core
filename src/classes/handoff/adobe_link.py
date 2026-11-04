"""Find the Zenvi Link extension in After Effects / Premiere Pro and call its MCP tools.

Each Adobe app running the Zenvi Link CEP extension serves a loopback MCP
endpoint and advertises it in ``~/.openshot_qt/link/<app>.json`` (SPEC §3.1)::

    {"url": "http://127.0.0.1:53012/mcp", "token_file": ".../aftereffects.token",
     "pid": 12345, "project": "/x/promo.aep", "version": "1.0.0", "app": "aftereffects",
     "app_name": "Adobe After Effects 2026", "app_version": "26.3.0", "protocol": 1,
     "started_at": "...", "last_active_at": "..."}

A file is trusted only when its ``pid`` is alive, its URL is loopback, and
one ``initialize`` with the bearer token answers within 1.5 s. Other ports
are never probed and other processes' files are never deleted.

The client speaks the MCP subset of SPEC §3.2 over plain ``urllib``: JSON-RPC
POSTs with ``Accept: application/json, text/event-stream`` (a
``text/event-stream`` reply is read up to its first ``data:`` event).
Everything here blocks on the network: call it off the GUI thread
(``handoff.jobs`` or a background-safe tool).
"""

from __future__ import annotations

import base64
import copy
import datetime
import errno
import http.client
import itertools
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from classes.logger import log

APPS = ("aftereffects", "premiere")
APP_LABELS = {"aftereffects": "After Effects", "premiere": "Premiere Pro"}
HOST_IDS = {"aftereffects": "after-effects", "premiere": "premiere-pro"}
PROBE_TIMEOUT = 1.5
DEFAULT_TIMEOUT = 120.0
PROTOCOL_VERSION = "2025-06-18"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
INSTALL_HINT = "zenvi adobe install"
CLIENT_INFO = {"name": "zenvi-desktop", "title": "Zenvi", "version": "1.0"}

_ids = itertools.count(1)


class LinkHostError(RuntimeError):
    """A host call failed. ``code`` follows SPEC §3.3 (NOT_CONNECTED, TIMEOUT, HOST_ERROR, ...)."""

    def __init__(self, message: str, code: str = "HOST_ERROR"):
        super().__init__(message)
        self.code = code


class HostNotConnected(LinkHostError):
    def __init__(self, message: str):
        super().__init__(message, "NOT_CONNECTED")


def connect_hint(app: str) -> str:
    """How a user connects *app* (for refusals and disabled-menu tooltips)."""
    label = APP_LABELS.get(app, app)
    return (f"Open {label} with the Zenvi Link panel (Window > Extensions > Zenvi Link). If the panel is "
            f"missing, install it with `{INSTALL_HINT}` and restart {label}.")


def link_dir(base_dir: Optional[str] = None) -> str:
    """``<user dir>/link`` (``~/.openshot_qt/link``)."""
    if base_dir is None:
        from classes import info
        base_dir = info.USER_PATH
    return os.path.join(base_dir, "link")


def discovery_path(app: str, base_dir: Optional[str] = None) -> str:
    if app not in APPS:
        raise ValueError(f"unknown Adobe host {app!r}; expected one of {', '.join(APPS)}")
    return os.path.join(link_dir(base_dir), app + ".json")


def read_discovery(app: str, base_dir: Optional[str] = None) -> Optional[dict]:
    """The parsed discovery file of *app*, or None when missing or not a JSON object."""
    try:
        with open(discovery_path(app, base_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: Any) -> bool:
    """True when process *pid* exists (permission errors count as alive)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _loopback(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url))
        port = parsed.port  # ValueError when out of range (99999)
    except ValueError:
        return False
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS and bool(port)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the bearer token to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_token(token_file: Any) -> Optional[str]:
    try:
        with open(str(token_file), "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except (OSError, TypeError, ValueError):
        return None
    return token or None


def _parse_time(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class HostInfo:
    """One Adobe host as the desktop sees it (SPEC §3.8 host model)."""

    app: str
    connected: bool
    reason: str = ""                       # why not connected (shown to users / agents)
    url: Optional[str] = None
    pid: Optional[int] = None
    project: Optional[str] = None
    version: Optional[str] = None
    app_name: Optional[str] = None
    app_version: Optional[str] = None
    protocol: Optional[int] = None
    started_at: Optional[str] = None
    last_active_at: Optional[str] = None
    active: bool = False
    server_name: Optional[str] = None
    token_file: Optional[str] = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return HOST_IDS.get(self.app, self.app)

    @property
    def label(self) -> str:
        return APP_LABELS.get(self.app, self.app)

    def as_dict(self) -> dict:
        out = {"id": self.id, "kind": self.app, "label": self.label, "connected": self.connected,
               "active": self.active}
        if self.connected:
            out.update({"app_name": self.app_name, "app_version": self.app_version, "project": self.project,
                        "pid": self.pid, "url": self.url, "extension_version": self.version,
                        "last_active_at": self.last_active_at})
        else:
            out.update({"reason": self.reason, "install_hint": INSTALL_HINT, "how_to_connect": connect_hint(self.app)})
        return out


@dataclass
class HostImage:
    data: bytes
    mime_type: str = "image/png"


@dataclass
class HostResult:
    """A ``tools/call`` result: the host's contract-3 receipt, its text and images."""

    receipt: dict
    text: str
    images: List[HostImage]
    is_error: bool
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def summary(self) -> str:
        return str(self.receipt.get("summary") or self.text or "")


class McpHttpClient:
    """Minimal stateless MCP client over HTTP (JSON-RPC 2.0 POSTs, bearer token)."""

    def __init__(self, url: str, token: str):
        if not _loopback(url):
            raise LinkHostError(f"refusing a non-loopback host URL {url!r}", "INVALID_ARGUMENT")
        self.url = url
        self.token = token
        # loopback: never a proxy, never a redirect
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _post(self, payload: dict, timeout: float) -> Optional[dict]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        })
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status = resp.status
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype and status != 202:
                    # read event by event and stop at OUR reply: a stream may carry notifications
                    # first, and a server may keep it open after answering
                    return _read_sse_reply(resp, payload.get("id"))
                data = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise LinkHostError("the host answered with a redirect; refused (Zenvi Link never redirects)",
                                    "FORBIDDEN") from None
            if exc.code == 401:
                raise LinkHostError("the host rejected Zenvi's token (the extension restarted?); try again",
                                    "UNAUTHORIZED") from None
            if exc.code == 403:
                raise LinkHostError("the host refused the request (403)", "FORBIDDEN") from None
            raise LinkHostError(f"the host answered HTTP {exc.code}", "HOST_ERROR") from None
        except http.client.HTTPException as exc:  # BadStatusLine, IncompleteRead: not an MCP endpoint
            raise LinkHostError(f"the endpoint sent a broken HTTP reply ({type(exc).__name__})", "HOST_ERROR") from None
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LinkHostError(f"the host did not answer within {timeout:g} s", "TIMEOUT") from None
            raise LinkHostError(f"could not reach the host: {reason}", "NOT_CONNECTED") from None
        except ValueError as exc:  # a malformed URL or header
            raise LinkHostError(f"bad host endpoint: {exc}", "HOST_ERROR") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise LinkHostError("the host's answer is too large", "HOST_ERROR")
        if status == 202 or not data.strip():
            return None
        text = data.decode("utf-8", errors="replace")
        try:
            message = json.loads(text)
        except ValueError:
            raise LinkHostError("the host sent a reply that is not JSON", "HOST_ERROR") from None
        if not isinstance(message, dict):
            raise LinkHostError("the host sent an unexpected JSON-RPC reply", "HOST_ERROR")
        return message

    def request(self, method: str, params: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Send a JSON-RPC request and return its ``result`` (LinkHostError on a JSON-RPC error)."""
        rid = next(_ids)
        payload: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        message = self._post(payload, timeout)
        if message is None:
            raise LinkHostError(f"the host sent no reply to {method}", "HOST_ERROR")
        if "error" in message and message["error"] is not None:
            err = message["error"] if isinstance(message["error"], dict) else {"message": str(message["error"])}
            code = "UNSUPPORTED" if err.get("code") == -32601 else "HOST_ERROR"
            raise LinkHostError(f"{method} failed: {err.get('message') or err}", code)
        return message.get("result")

    def notify(self, method: str, params: Optional[dict] = None, timeout: float = PROBE_TIMEOUT) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, timeout)

    def initialize(self, timeout: float = PROBE_TIMEOUT) -> dict:
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": CLIENT_INFO}, timeout=timeout)
        return result if isinstance(result, dict) else {}


def _read_sse_reply(stream: Any, want_id: Any) -> Optional[dict]:
    """The JSON-RPC reply with id *want_id* from a ``text/event-stream`` response, read line by line.

    Notifications and other events before it are skipped; reading stops as
    soon as the reply arrives. LinkHostError when the stream ends first or
    grows past the size limit.
    """
    data_lines: List[str] = []
    total = 0

    def reply_in(lines: List[str]) -> Optional[dict]:
        try:
            message = json.loads("\n".join(lines))
        except ValueError:
            return None
        if not isinstance(message, dict) or ("result" not in message and "error" not in message):
            return None  # a notification or request from the server
        if want_id is not None and message.get("id") != want_id:
            return None
        return message

    while True:
        raw = stream.readline(MAX_RESPONSE_BYTES)
        if not raw:
            break
        total += len(raw)
        if total > MAX_RESPONSE_BYTES:
            raise LinkHostError("the host's answer is too large", "HOST_ERROR")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[6:] if line[5:6] == " " else line[5:])
        elif not line and data_lines:
            message = reply_in(data_lines)
            data_lines = []
            if message is not None:
                return message
    if data_lines:
        message = reply_in(data_lines)
        if message is not None:
            return message
    raise LinkHostError("the host's event stream ended without a reply", "HOST_ERROR")


def _first_sse_data(text: str) -> Optional[str]:
    """The data of the first event in a ``text/event-stream`` body (multi-line data joined)."""
    lines: List[str] = []
    for raw in text.splitlines():
        if raw.startswith("data:"):
            lines.append(raw[5:].lstrip(" ") if raw[5:6] == " " else raw[5:])
        elif not raw.strip() and lines:
            break
    return "\n".join(lines) if lines else None


def _client_for(data: dict) -> McpHttpClient:
    token = _read_token(data.get("token_file"))
    if not token:
        raise HostNotConnected("its token file is missing or empty")
    return McpHttpClient(str(data.get("url")), token)


def _inspect(app: str, base_dir: Optional[str], probe: bool, timeout: float) -> HostInfo:
    data = read_discovery(app, base_dir)
    if data is None:
        return HostInfo(app=app, connected=False, reason=f"{APP_LABELS[app]} is not running the Zenvi Link panel")
    info = HostInfo(app=app, connected=False, url=data.get("url"), project=data.get("project"),
                    version=data.get("version"), app_name=data.get("app_name"), app_version=data.get("app_version"),
                    started_at=data.get("started_at"), last_active_at=data.get("last_active_at"),
                    token_file=data.get("token_file"))
    try:
        info.pid = int(data.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        info.pid = None
    try:
        info.protocol = int(data.get("protocol"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        info.protocol = None
    if data.get("app") not in (None, app):
        info.reason = f"the discovery file names {data.get('app')!r}, not {app}"
        return info
    if not pid_alive(info.pid):
        info.reason = f"{APP_LABELS[app]} (pid {info.pid}) is no longer running"
        return info
    if not _loopback(str(info.url or "")):
        info.reason = "its endpoint is not a loopback URL; ignored"
        return info
    if not probe:
        info.connected = True
        return info
    try:
        result = _client_for(data).initialize(timeout=timeout)
    except LinkHostError as exc:
        info.reason = f"{APP_LABELS[app]} did not answer: {exc}"
        return info
    server = result.get("serverInfo")
    info.server_name = server.get("name") if isinstance(server, dict) else None
    info.connected = True
    return info


def _inspect_safely(app: str, base_dir: Optional[str], probe: bool, timeout: float) -> HostInfo:
    """``_inspect`` that never raises: one broken discovery file must not hide the other app."""
    try:
        return _inspect(app, base_dir, probe, timeout)
    except Exception as exc:
        log.warning("Zenvi Link discovery for %s failed", app, exc_info=True)
        return HostInfo(app=app, connected=False, reason=f"its discovery file could not be used: {exc}")


def list_hosts(base_dir: Optional[str] = None, *, probe: bool = True, timeout: float = PROBE_TIMEOUT) -> List[HostInfo]:
    """Both Adobe hosts, connected or not; ``active`` marks the most recently used connected one.

    *probe* sends one ``initialize`` per discovery file (up to *timeout*
    seconds each). Blocking: off the GUI thread.
    """
    hosts = [_inspect_safely(app, base_dir, probe, timeout) for app in APPS]
    live = [h for h in hosts if h.connected]
    if live:
        def _key(h: HostInfo):
            t = _parse_time(h.last_active_at) or _parse_time(h.started_at)
            return t.timestamp() if t else 0.0
        max(live, key=_key).active = True
    return hosts


def get_host(app: str, base_dir: Optional[str] = None, *, probe: bool = True) -> HostInfo:
    if app not in APPS:
        raise LinkHostError(f"unknown Adobe host {app!r}; expected one of {', '.join(APPS)}", "INVALID_ARGUMENT")
    return _inspect_safely(app, base_dir, probe, PROBE_TIMEOUT)


def _connected_client(app: str, base_dir: Optional[str]) -> McpHttpClient:
    if app not in APPS:
        raise LinkHostError(f"unknown Adobe host {app!r}; expected one of {', '.join(APPS)}", "INVALID_ARGUMENT")
    data = read_discovery(app, base_dir)
    host = _inspect_safely(app, base_dir, True, PROBE_TIMEOUT)
    if not host.connected or data is None:
        raise HostNotConnected(f"{APP_LABELS[app]} is not connected ({host.reason}). {connect_hint(app)}")
    return _client_for(data)


def list_host_tools(app: str, base_dir: Optional[str] = None, timeout: float = 10.0) -> List[dict]:
    """``tools/list`` of a connected host: ``[{name, title, description, inputSchema, annotations}]``."""
    result = _connected_client(app, base_dir).request("tools/list", {}, timeout=timeout)
    tools = result.get("tools") if isinstance(result, dict) else None
    return [t for t in (tools or []) if isinstance(t, dict)]


def _result_from(tool: str, app: str, result: Any) -> HostResult:
    if not isinstance(result, dict):
        raise LinkHostError(f"{tool} returned no result", "HOST_ERROR")
    content = result.get("content")
    if not isinstance(content, list):
        content = []
    texts, images = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "image" and block.get("data"):
            try:
                images.append(HostImage(base64.b64decode(block["data"]), str(block.get("mimeType") or "image/png")))
            except (ValueError, TypeError):
                log.warning("Adobe host %s sent an undecodable image from %s", app, tool)
    receipt = result.get("structuredContent")
    if not isinstance(receipt, dict):
        receipt = None
    if receipt is None and texts:
        try:
            parsed = json.loads(texts[0])
            receipt = parsed if isinstance(parsed, dict) else None
        except ValueError:
            receipt = None
    is_error = bool(result.get("isError"))
    if receipt is None:
        summary = texts[0] if texts else ("Error: " + tool + " failed" if is_error else tool + " finished")
        receipt = {"contract": 3, "status": "error" if is_error else "ok", "tool": tool, "host": app,
                   "summary": summary if not is_error or summary.startswith("Error") else "Error: " + summary,
                   "data": None, "warnings": [], "undoSteps": 0}
    if str(receipt.get("status")) in ("error", "refused"):
        is_error = True
    return HostResult(receipt=receipt, text="\n".join(texts), images=images, is_error=is_error, raw=result)


TIMEOUT_SLACK = 15.0
MAX_TIMEOUT = 3 * 60 * 60
# A host tool as an agent sees it: BRIEF to learn what an app can do (a whole catalog stays small),
# DETAIL to call it (no outputSchema, timeouts or image flags).
BRIEF_KEYS = ("name", "title")
DETAIL_KEYS = ("name", "title", "description", "inputSchema", "annotations")
_catalogs: Dict[Any, List[dict]] = {}
_catalogs_lock = threading.Lock()


def host_catalog(app: str, base_dir: Optional[str] = None, *, refresh: bool = False) -> List[dict]:
    """A connected host's ``tools/list`` rows, fetched once per host session (url + pid + started_at).

    A restarted extension has a new start time (and usually a new pid and
    port), so its catalog is fetched again. Raises HostNotConnected /
    LinkHostError like :func:`list_host_tools`. Returns copies: callers may
    change them.
    """
    data = read_discovery(app, base_dir) or {}
    key = (app, base_dir, str(data.get("url") or ""), data.get("pid"), str(data.get("started_at") or ""))
    if not refresh:
        with _catalogs_lock:
            cached = _catalogs.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    rows = list_host_tools(app, base_dir)
    with _catalogs_lock:
        for old in [k for k in _catalogs if k[0] == app and k[1] == base_dir]:
            _catalogs.pop(old, None)  # an older session of this app
        _catalogs[key] = copy.deepcopy(rows)
    return rows


def tool_brief(row: dict) -> dict:
    """A catalog row as ``{name, title}``: enough to know what the tool is for."""
    return {k: copy.deepcopy(row[k]) for k in BRIEF_KEYS if k in row}


def tool_details(row: dict) -> dict:
    """A catalog row as an agent needs it to call the tool: ``{name, title, description, inputSchema, annotations}``."""
    return {k: copy.deepcopy(row[k]) for k in DETAIL_KEYS if k in row}


def tool_timeout(app: str, tool: str, base_dir: Optional[str] = None) -> float:
    """Seconds to wait for *tool*: its catalog ``timeoutMs`` (:func:`host_catalog`) plus slack,
    else :data:`DEFAULT_TIMEOUT`. A long render keeps its own budget."""
    seconds = None
    try:
        for row in host_catalog(app, base_dir):
            if row.get("name") == tool:
                ms = row.get("timeoutMs")
                if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
                    seconds = float(ms) / 1000.0
                break
    except LinkHostError:
        seconds = None
    return min(MAX_TIMEOUT, (seconds or DEFAULT_TIMEOUT) + TIMEOUT_SLACK)


def call_host_tool(app: str, tool: str, args: Optional[dict] = None, timeout: Optional[float] = None,
                   base_dir: Optional[str] = None) -> HostResult:
    """Call host tool *tool* (``ae_*`` / ``premiere_*``) with *args* in the connected *app*.

    *timeout* defaults to the tool's own catalog ``timeoutMs`` (+ slack, see
    :func:`tool_timeout`), 120 s when the catalog gives none. Raises
    HostNotConnected (with how to connect) when the host is not reachable,
    LinkHostError(code=TIMEOUT) when the call outlives the timeout. A tool
    that ran and failed comes back as a HostResult with ``is_error=True`` and
    the host's receipt. Blocking.
    """
    if not str(tool or "").strip():
        raise LinkHostError("which host tool? pass its name (e.g. ae_get_state)", "INVALID_ARGUMENT")
    if args is not None and not isinstance(args, dict):
        raise LinkHostError("host tool arguments must be an object", "INVALID_ARGUMENT")
    client = _connected_client(app, base_dir)
    wait = float(timeout) if timeout is not None else tool_timeout(app, str(tool), base_dir)
    result = client.request("tools/call", {"name": str(tool), "arguments": dict(args or {})}, timeout=wait)
    return _result_from(str(tool), app, result)
