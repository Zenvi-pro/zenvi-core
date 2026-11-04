"""classes.handoff.adobe_link against a real loopback HTTP server (port 0) and a temp discovery dir."""

import os
import threading

import pytest

from classes.handoff import adobe_link as al
from handoff_fakes import FakeHost, write_discovery


@pytest.fixture
def host(tmp_path):
    fake = FakeHost()
    yield fake
    fake.stop()


def _discover(tmp_path, host, **kwargs):
    return write_discovery(str(tmp_path), host, **kwargs)


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


def _garbage_server():
    """A TCP listener that is not an HTTP server (answers every connection with junk)."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                conn.recv(4096)
                conn.sendall(b"\x00\x01 not http at all\r\n\r\n")
            finally:
                conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, stop


def test_one_broken_discovery_file_does_not_hide_the_other_app(tmp_path, host):
    srv, stop = _garbage_server()
    pr = FakeHost(app="premiere")
    try:
        base = _discover(tmp_path, host, url="http://127.0.0.1:%d/mcp" % srv.getsockname()[1])
        _discover(tmp_path, pr, app="premiere")
        ae, premiere = al.list_hosts(base)
        assert not ae.connected and "did not answer" in ae.reason
        assert premiere.connected and premiere.active
        _discover(tmp_path, host, url="http://127.0.0.1:99999/mcp")  # port out of range
        ae, premiere = al.list_hosts(base)
        assert not ae.connected and "loopback" in ae.reason and premiere.connected
    finally:
        stop.set()
        srv.close()
        pr.stop()


def test_redirects_are_refused_and_the_token_never_leaves(tmp_path, host):
    other = FakeHost(token="other")
    try:
        host.redirect_to = other.url
        base = _discover(tmp_path, host)
        assert not al.get_host("aftereffects", base).connected
        with pytest.raises(al.LinkHostError) as err:
            al.McpHttpClient(host.url, host.token).initialize()
        assert err.value.code == "FORBIDDEN" and "redirect" in str(err.value)
        assert other.headers == []  # the bearer token was never sent on
    finally:
        other.stop()


def test_host_calls_wait_as_long_as_the_tool_says(tmp_path, host):
    host.tools = [{"name": "ae_render", "timeoutMs": 900000}, {"name": "ae_get_state"}]
    base = _discover(tmp_path, host)
    assert al.tool_timeout("aftereffects", "ae_render", base) == pytest.approx(900 + al.TIMEOUT_SLACK)
    assert al.tool_timeout("aftereffects", "ae_get_state", base) == pytest.approx(al.DEFAULT_TIMEOUT + al.TIMEOUT_SLACK)
    assert al.tool_timeout("premiere", "premiere_x", base) == pytest.approx(al.DEFAULT_TIMEOUT + al.TIMEOUT_SLACK)


def test_event_stream_replies_skip_notifications_and_stop_at_the_reply():
    import io
    body = (b"event: message\ndata: {\"jsonrpc\": \"2.0\", \"method\": \"notifications/message\", "
            b"\"params\": {\"level\": \"info\"}}\n\n"
            b"data: {\"jsonrpc\": \"2.0\", \"id\": 9, \"result\": {\"other\": true}}\n\n"
            b"data: {\"jsonrpc\": \"2.0\", \"id\": 7, \"result\": {\"ok\": 1}}\n\n"
            b"data: never read\n\n")
    stream = io.BytesIO(body)
    assert al._read_sse_reply(stream, 7) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": 1}}
    assert stream.read() == b"data: never read\n\n"  # stopped at the reply
    with pytest.raises(al.LinkHostError, match="ended without a reply"):
        al._read_sse_reply(io.BytesIO(b"data: {\"jsonrpc\": \"2.0\", \"method\": \"ping\"}\n\n"), 1)
