"""classes.handoff.adobe_link against a real loopback HTTP server (port 0) and a temp discovery dir."""

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from classes.handoff import adobe_link as al

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class FakeHost:
    """A Zenvi Link MCP endpoint: SPEC 3.2 subset, configurable per test."""

    def __init__(self, app="aftereffects", token="t0k3n"):
        self.app = app
        self.token = token
        self.sse = False
        self.calls = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer " + host.token:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", "Bearer")
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                host.calls.append(body)
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                reply = {"jsonrpc": "2.0", "id": body["id"]}
                method = body.get("method")
                if method == "initialize":
                    reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                       "serverInfo": {"name": "zenvi-link-" + host.app, "version": "1.0.0"}}
                elif method == "tools/list":
                    reply["result"] = {"tools": [{"name": "ae_get_state", "inputSchema": {"type": "object"}}]}
                elif method == "tools/call":
                    reply["result"] = host.call(body["params"]["name"], body["params"].get("arguments") or {})
                else:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
                data = json.dumps(reply).encode()
                self.send_response(200)
                if host.sse:
                    data = b"event: message\ndata: " + data + b"\n\n"
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]

    def call(self, name, args):
        if name == "slow":
            time.sleep(float(args.get("seconds") or 2))
        if name == "ae_fail":
            receipt = {"contract": 3, "status": "refused", "tool": name, "host": self.app,
                       "summary": "Error: no active comp", "error": {"code": "NO_ACTIVE_COMP", "message": "x"}}
            return {"content": [{"type": "text", "text": json.dumps(receipt)}], "structuredContent": receipt,
                    "isError": True}
        receipt = {"contract": 3, "status": "ok", "tool": name, "host": self.app, "summary": "State read",
                   "data": {"args": args}, "warnings": [], "undoSteps": 0}
        content = [{"type": "text", "text": json.dumps(receipt)}]
        if name == "ae_capture_frame":
            content.append({"type": "image", "data": PNG, "mimeType": "image/png"})
        return {"content": content, "structuredContent": receipt, "isError": False}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def host(tmp_path):
    fake = FakeHost()
    yield fake
    fake.stop()


def _discover(tmp_path, host, *, app="aftereffects", pid=None, url=None, token=None, last_active="2026-10-04T21:05:12Z"):
    link = tmp_path / "link"
    link.mkdir(exist_ok=True)
    token_file = link / (app + ".token")
    token_file.write_text(token if token is not None else host.token)
    os.chmod(token_file, 0o600)
    data = {"url": url or host.url, "token_file": str(token_file), "pid": pid or os.getpid(), "project": "/x/p.aep",
            "version": "1.0.0", "app": app, "app_name": "Adobe After Effects 2026", "app_version": "26.3.0",
            "protocol": 1, "started_at": "2026-10-04T21:00:00Z", "last_active_at": last_active}
    (link / (app + ".json")).write_text(json.dumps(data))
    return str(tmp_path)


def test_no_discovery_files_means_nothing_connected(tmp_path):
    hosts = al.list_hosts(str(tmp_path))
    assert [h.app for h in hosts] == ["aftereffects", "premiere"]
    assert not any(h.connected or h.active for h in hosts)
    row = hosts[0].as_dict()
    assert row["id"] == "after-effects" and row["install_hint"] == "zenvi adobe install"
    assert "Zenvi Link" in row["how_to_connect"]


def test_a_live_host_is_connected_and_active(tmp_path, host):
    base = _discover(tmp_path, host)
    ae, pr = al.list_hosts(base)
    assert ae.connected and ae.active and ae.server_name == "zenvi-link-aftereffects"
    assert ae.app_version == "26.3.0" and ae.project == "/x/p.aep" and not pr.connected
    assert host.calls[0]["method"] == "initialize"


def test_the_most_recently_active_host_wins(tmp_path, host):
    other = FakeHost(app="premiere")
    try:
        base = _discover(tmp_path, host, last_active="2026-10-04T21:00:00Z")
        _discover(tmp_path, other, app="premiere", last_active="2026-10-04T22:00:00Z")
        ae, pr = al.list_hosts(base)
        assert ae.connected and pr.connected and pr.active and not ae.active
    finally:
        other.stop()


@pytest.mark.parametrize("kwargs, words", [
    ({"pid": 999999}, "no longer running"),
    ({"url": "http://10.0.0.5:53012/mcp"}, "not a loopback"),
    ({"token": "wrong"}, "token"),
])
def test_untrusted_discovery_files_are_ignored_and_kept(tmp_path, host, kwargs, words):
    base = _discover(tmp_path, host, **kwargs)
    ae = al.get_host("aftereffects", base)
    assert not ae.connected and words in ae.reason
    assert os.path.isfile(os.path.join(base, "link", "aftereffects.json"))  # never deleted


def test_call_host_tool_returns_receipt_text_and_images(tmp_path, host):
    base = _discover(tmp_path, host)
    res = al.call_host_tool("aftereffects", "ae_capture_frame", {"time": 1.5}, base_dir=base)
    assert not res.is_error and res.receipt["data"] == {"args": {"time": 1.5}}
    assert len(res.images) == 1 and res.images[0].data.startswith(b"\x89PNG")
    assert res.summary == "State read"


def test_event_stream_replies_are_read(tmp_path, host):
    host.sse = True
    base = _discover(tmp_path, host)
    res = al.call_host_tool("aftereffects", "ae_get_state", {}, base_dir=base)
    assert res.receipt["status"] == "ok"
    assert al.list_host_tools("aftereffects", base)[0]["name"] == "ae_get_state"


def test_host_side_failures_come_back_as_error_results(tmp_path, host):
    base = _discover(tmp_path, host)
    res = al.call_host_tool("aftereffects", "ae_fail", {}, base_dir=base)
    assert res.is_error and res.receipt["error"]["code"] == "NO_ACTIVE_COMP"


def test_not_connected_and_timeouts_raise_with_codes(tmp_path, host):
    with pytest.raises(al.HostNotConnected) as err:
        al.call_host_tool("premiere", "premiere_get_state", {}, base_dir=str(tmp_path))
    assert err.value.code == "NOT_CONNECTED" and "Zenvi Link" in str(err.value)
    base = _discover(tmp_path, host)
    with pytest.raises(al.LinkHostError) as err:
        al.call_host_tool("aftereffects", "slow", {"seconds": 3}, timeout=0.5, base_dir=base)
    assert err.value.code == "TIMEOUT"
    with pytest.raises(al.LinkHostError) as err:
        al.call_host_tool("photoshop", "x", {}, base_dir=base)
    assert err.value.code == "INVALID_ARGUMENT"


def test_client_refuses_non_loopback_urls():
    with pytest.raises(al.LinkHostError):
        al.McpHttpClient("http://example.com:80/mcp", "t")


def test_first_sse_event_parsing():
    assert al._first_sse_data("event: message\ndata: {\"a\":\ndata: 1}\n\ndata: {}\n\n") == "{\"a\":\n1}"
    assert al._first_sse_data(": ping\n\n") is None
