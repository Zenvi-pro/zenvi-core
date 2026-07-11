"""Tests for the in-app MCP tool server (classes.agent_mcp_server).

Covers schema derivation, tool listing, and an end-to-end check that an MCP
client can list + call tools over the localhost streamable-HTTP transport with
the per-launch bearer token. The optional ``claude`` CLI smoke is gated behind
ZENVI_RUN_CLI_SMOKE=1 so normal runs don't spend model tokens.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

import pytest

from classes.agent_mcp_server import _build_input_schema


# --- schema derivation (no server / no stubs needed) -----------------------

def test_schema_is_permissive_for_kwargs_only():
    def handler(**kwargs):
        """List the media files in the current project bin."""

    schema = _build_input_schema(handler)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is True
    assert "properties" not in schema


def test_schema_extracts_typed_params_and_required():
    def handler(name, label="x", count=3, **kwargs):
        """Do a thing."""

    schema = _build_input_schema(handler)
    assert set(schema["properties"]) == {"name", "label", "count"}
    assert schema["required"] == ["name"]
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["additionalProperties"] is True  # has **kwargs


# --- a stubbed tool layer so we don't need Qt/libopenshot ------------------

@pytest.fixture
def tool_stub():
    th = types.ModuleType("classes.tool_handlers")

    def list_files(**_kw):
        """List the media files in the current project bin."""
        return "FIXTURE_FILES: a.mp4, b.wav"

    def add_track(label="", **_kw):
        """Add a new track to the timeline."""
        return "added track %s" % label

    th.AGENT_TOOL_HANDLERS = {"list_files_tool": list_files, "add_track_tool": add_track}
    th.humanize_tool_name = lambda n: n
    th.execute_tool = lambda name, args: th.AGENT_TOOL_HANDLERS[name](**(args or {}))

    saved = sys.modules.get("classes.tool_handlers")
    sys.modules["classes.tool_handlers"] = th
    try:
        yield th
    finally:
        if saved is not None:
            sys.modules["classes.tool_handlers"] = saved
        else:
            sys.modules.pop("classes.tool_handlers", None)


def test_iter_tool_defs(tool_stub):
    from classes.agent_mcp_server import iter_tool_defs
    defs = {d["name"]: d for d in iter_tool_defs()}
    assert set(defs) == {"list_files_tool", "add_track_tool"}
    assert defs["add_track_tool"]["inputSchema"]["properties"]["label"]["type"] == "string"
    assert "media files" in defs["list_files_tool"]["description"]


# --- transport: an MCP client can list + call tools ------------------------

def test_server_lists_and_calls_tools(tool_stub):
    pytest.importorskip("mcp")
    from classes.agent_mcp_server import ZenviMcpServer

    srv = ZenviMcpServer().start()
    time.sleep(1.0)
    try:
        async def run():
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            headers = {"Authorization": "Bearer %s" % srv.token}
            async with httpx.AsyncClient(headers=headers) as http_client:
                async with streamable_http_client(srv.url(), http_client=http_client) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool("list_files_tool", {})
                        return [t.name for t in tools.tools], result.content[0].text

        names, text = asyncio.run(run())
        assert "list_files_tool" in names
        assert "FIXTURE_FILES" in text
    finally:
        srv.stop()


def test_call_tool_broadcasts_started_and_completed(tool_stub):
    """Phase 9: every MCP tool call (Zenvi-driven or a genuine external
    terminal session — this layer can't tell those apart) must broadcast a
    matched started/completed pair via get_tool_call_broadcaster(), which is
    what lets AIChatWindow render a read-only "Live from terminal" view."""
    pytest.importorskip("mcp")
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtWidgets import QApplication
    from classes.agent_mcp_server import ZenviMcpServer, get_tool_call_broadcaster

    app = QApplication.instance() or QApplication([])
    broadcaster = get_tool_call_broadcaster()
    started, completed = [], []
    broadcaster.tool_call_started.connect(lambda cid, name, args: started.append((cid, name, args)))
    broadcaster.tool_call_completed.connect(lambda cid, ok, text: completed.append((cid, ok, text)))

    srv = ZenviMcpServer().start()
    time.sleep(1.0)
    try:
        async def run():
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            headers = {"Authorization": "Bearer %s" % srv.token}
            async with httpx.AsyncClient(headers=headers) as http_client:
                async with streamable_http_client(srv.url(), http_client=http_client) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        await session.call_tool("list_files_tool", {})

        asyncio.run(run())

        # The HTTP round-trip only completes after both signals were already
        # emitted server-side; pump the (not-otherwise-running) Qt event loop
        # briefly so the queued cross-thread deliveries land before asserting.
        deadline = time.time() + 2.0
        while (not started or not completed) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
    finally:
        srv.stop()

    assert len(started) == 1
    call_id, tool_name, args_json = started[0]
    assert tool_name == "list_files_tool"
    assert call_id  # non-empty

    assert len(completed) == 1
    completed_call_id, ok, text = completed[0]
    assert completed_call_id == call_id  # started/completed pair up by call_id
    assert ok is True
    assert "FIXTURE_FILES" in text


def test_server_requires_bearer_token(tool_stub):
    from classes.agent_mcp_server import ZenviMcpServer

    srv = ZenviMcpServer().start()
    time.sleep(1.0)
    try:
        req = urllib.request.Request(
            srv.url(), method="POST", data=b"{}",
            headers={"content-type": "application/json",
                     "accept": "application/json, text/event-stream"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 401
    finally:
        srv.stop()


# --- fixed port + persisted token (Phase 7) ---------------------------------

def test_bind_port_prefers_stable_port_when_free():
    import socket as socket_mod
    from classes.agent_mcp_server import _bind_port

    # Use a high, unlikely-to-collide port rather than the real PREFERRED_PORT
    # (7434) so this test never fights a real running app instance for it.
    s = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()

    assert _bind_port("127.0.0.1", free_port) == free_port


def test_bind_port_falls_back_when_taken():
    import socket as socket_mod
    from classes.agent_mcp_server import _bind_port

    holder = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken_port = holder.getsockname()[1]
    try:
        result = _bind_port("127.0.0.1", taken_port)
        assert result != taken_port
        assert 0 < result < 65536
    finally:
        holder.close()


def test_load_or_create_token_persists_across_calls(monkeypatch, tmp_path):
    import classes.agent_mcp_server as srv_mod

    token_file = str(tmp_path / "mcp_token")
    monkeypatch.setattr(srv_mod, "_token_path", lambda: token_file)

    first = srv_mod._load_or_create_token()
    assert first
    assert os.path.exists(token_file)

    second = srv_mod._load_or_create_token()
    assert second == first


def test_load_or_create_token_generates_when_missing(monkeypatch, tmp_path):
    import classes.agent_mcp_server as srv_mod

    monkeypatch.setattr(srv_mod, "_token_path", lambda: str(tmp_path / "nested" / "mcp_token"))
    token = srv_mod._load_or_create_token()
    assert token
    assert os.path.exists(tmp_path / "nested" / "mcp_token")


# --- optional: drive the real claude CLI against the server ----------------

_RUN_CLI = bool(shutil.which("claude")) and os.environ.get("ZENVI_RUN_CLI_SMOKE") == "1"


@pytest.mark.skipif(not _RUN_CLI, reason="set ZENVI_RUN_CLI_SMOKE=1 (and have claude on PATH) to run")
def test_claude_cli_invokes_mcp_tool(tool_stub):
    from classes.agent_mcp_server import ZenviMcpServer

    srv = ZenviMcpServer().start()
    time.sleep(1.0)
    cfg = {"mcpServers": {"zenvi-editor": {"type": "http", "url": srv.url(),
            "headers": {"Authorization": "Bearer %s" % srv.token}}}}
    cfg_path = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False).name
    json.dump(cfg, open(cfg_path, "w"))
    saw_tool_use = saw_fixture = False
    try:
        proc = subprocess.Popen(
            ["claude", "-p", "Call the list_files_tool tool with no arguments and report the result.",
             "--output-format", "stream-json", "--verbose",
             "--mcp-config", cfg_path, "--strict-mcp-config",
             "--permission-mode", "bypassPermissions",
             "--allowedTools", "mcp__zenvi-editor__list_files_tool"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            try:
                ev = json.loads(line.strip())
            except Exception:
                continue
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use" and "list_files_tool" in (b.get("name") or ""):
                        saw_tool_use = True
            if "FIXTURE_FILES" in json.dumps(ev):
                saw_fixture = True
        proc.wait(timeout=120)
    finally:
        srv.stop()
        os.unlink(cfg_path)
    assert saw_tool_use and saw_fixture
