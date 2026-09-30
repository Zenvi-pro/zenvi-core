"""Discovery files (gui_mcp.json / headless_mcp.json): format and lifecycle.

The file is the contract external CLIs (zenvi-web's `zenvi`, zenvi-39's MCP
helper) use to find a running editor, so its shape is pinned here, and so is
the rule that a server nobody opted in (a test's) never writes one.
"""

import json
import os
import stat
import sys
import time
import types

import pytest

from classes import mcp_discovery


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- the file itself ---------------------------------------------------------

def test_discovery_paths_are_fixed_names_in_the_profile(tmp_path):
    assert mcp_discovery.discovery_path(str(tmp_path), "gui") == str(tmp_path / "gui_mcp.json")
    assert mcp_discovery.discovery_path(str(tmp_path), "headless") == str(tmp_path / "headless_mcp.json")
    with pytest.raises(ValueError):
        mcp_discovery.discovery_path(str(tmp_path), "other")


def test_payload_has_exactly_the_contract_keys(tmp_path):
    payload = mcp_discovery.build_payload(
        "http://127.0.0.1:7434/mcp", str(tmp_path / "mcp_token"), 42, str(tmp_path / "cut.zvn"), "1.0.188")
    assert payload == {
        "url": "http://127.0.0.1:7434/mcp",
        "token_file": str(tmp_path / "mcp_token"),
        "pid": 42,
        "project": str(tmp_path / "cut.zvn"),
        "version": "1.0.188",
    }
    untitled = mcp_discovery.build_payload("u", "t", 1, None, "v")
    assert untitled["project"] is None
    assert os.path.isabs(untitled["token_file"])


def test_write_replaces_the_file_privately_and_leaves_no_temp_files(tmp_path):
    path = str(tmp_path / "gui_mcp.json")
    mcp_discovery.write(path, {"pid": 1, "url": "a"})
    mcp_discovery.write(path, {"pid": 2, "url": "b"})

    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == {"pid": 2, "url": "b"}
    assert os.listdir(tmp_path) == ["gui_mcp.json"]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_creates_a_missing_profile_directory(tmp_path):
    path = str(tmp_path / "nested" / "headless_mcp.json")
    mcp_discovery.write(path, {"pid": 3})
    assert mcp_discovery.read(path) == {"pid": 3}


def test_failed_write_keeps_the_old_file_and_cleans_up(tmp_path, monkeypatch):
    path = str(tmp_path / "gui_mcp.json")
    mcp_discovery.write(path, {"pid": 1})

    def refuse(src, dst, attempts=5):
        raise PermissionError("locked")

    monkeypatch.setattr(mcp_discovery, "_replace", refuse)
    with pytest.raises(PermissionError):
        mcp_discovery.write(path, {"pid": 2})
    assert mcp_discovery.read(path) == {"pid": 1}
    assert os.listdir(tmp_path) == ["gui_mcp.json"]


def test_remove_only_deletes_this_process_file(tmp_path):
    path = str(tmp_path / "gui_mcp.json")
    assert mcp_discovery.remove(path, 7) is False  # nothing there

    mcp_discovery.write(path, {"pid": 8})
    assert mcp_discovery.remove(path, 7) is False  # a newer instance's file
    assert os.path.exists(path)

    assert mcp_discovery.remove(path, 8) is True
    assert not os.path.exists(path)


def test_read_tolerates_garbage(tmp_path):
    path = tmp_path / "gui_mcp.json"
    path.write_text("{not json", encoding="utf-8")
    assert mcp_discovery.read(str(path)) is None
    assert mcp_discovery.remove(str(path), os.getpid()) is False
    path.write_text("[1, 2]", encoding="utf-8")
    assert mcp_discovery.read(str(path)) is None


# --- the server publishes and withdraws it --------------------------------------

@pytest.fixture
def tool_stub():
    th = types.ModuleType("classes.tool_handlers")

    def list_files(**_kw):
        """List the media files in the current project bin."""
        return "FILES"

    th.AGENT_TOOL_HANDLERS = {"list_files_tool": list_files}
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


@pytest.fixture
def server(tool_stub, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import classes.agent_mcp_server as srv_mod

    monkeypatch.setattr(srv_mod, "_token_path", lambda: str(tmp_path / "mcp_token"))
    project = {"path": str(tmp_path / "cut.zvn")}
    monkeypatch.setattr(srv_mod, "_current_project_path", lambda: project["path"])
    srv = srv_mod.ZenviMcpServer()
    srv.project = project  # test handle on the "open project"
    # _watch_project reaches for the real app; there is none here.
    monkeypatch.setattr(srv, "_watch_project", lambda: None)
    yield srv
    srv.stop()


def test_server_writes_the_file_while_listening_and_removes_it_on_stop(server, tmp_path):
    from classes import info

    path = str(tmp_path / "gui_mcp.json")
    server.set_discovery_file(path)
    server.start()
    assert _wait_for(lambda: os.path.exists(path))

    data = mcp_discovery.read(path)
    assert set(data) == {"url", "token_file", "pid", "project", "version"}
    assert data["url"] == server.url() == "http://127.0.0.1:%d/mcp" % server.port
    assert data["token_file"] == str(tmp_path / "mcp_token")
    with open(data["token_file"], encoding="utf-8") as fh:
        assert fh.read().strip() == server.token
    assert data["pid"] == os.getpid()
    assert data["project"] == str(tmp_path / "cut.zvn")
    assert data["version"] == info.VERSION

    server.stop()
    assert not os.path.exists(path)


def test_server_follows_the_open_project(server, tmp_path):
    path = str(tmp_path / "headless_mcp.json")
    server.set_discovery_file(path)
    server.start()
    assert _wait_for(lambda: (mcp_discovery.read(path) or {}).get("project") == str(tmp_path / "cut.zvn"))

    server.project["path"] = str(tmp_path / "other.zvn")
    server.refresh_discovery()
    assert _wait_for(lambda: (mcp_discovery.read(path) or {}).get("project") == str(tmp_path / "other.zvn"))

    server.project["path"] = None  # File > New
    server.refresh_discovery()
    assert _wait_for(lambda: os.path.exists(path) and mcp_discovery.read(path)["project"] is None)


def test_enabling_discovery_on_a_running_server_writes_at_once(server, tmp_path):
    server.start()
    path = str(tmp_path / "headless_mcp.json")
    assert not os.path.exists(path)
    server.set_discovery_file(path)
    assert _wait_for(lambda: os.path.exists(path))


def test_a_server_nobody_opted_in_writes_no_file(server, tmp_path, monkeypatch):
    from classes import info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    server.start()
    time.sleep(0.3)
    assert not [n for n in os.listdir(tmp_path) if n.endswith("_mcp.json")]


def test_a_write_that_loses_the_race_with_stop_leaves_nothing(server, tmp_path):
    path = str(tmp_path / "gui_mcp.json")
    server.set_discovery_file(path)
    server.start()
    assert _wait_for(lambda: os.path.exists(path))
    server.stop()
    # A publish queued just before stop() runs after it: it must not recreate the file.
    server._write_discovery()
    assert not os.path.exists(path)


def test_project_load_watcher_only_reacts_to_loads():
    from classes.agent_mcp_server import _ProjectLoadWatcher

    calls = []
    watcher = _ProjectLoadWatcher(lambda: calls.append(1))
    watcher.changed(types.SimpleNamespace(type="update"))
    watcher.changed(types.SimpleNamespace(type="insert"))
    assert calls == []
    watcher.changed(types.SimpleNamespace(type="load"))
    assert calls == [1]

    # A failing refresh must not break UpdateManager's listener loop.
    def boom():
        raise RuntimeError("disk gone")

    _ProjectLoadWatcher(boom).changed(types.SimpleNamespace(type="load"))


def test_enable_discovery_points_the_singleton_at_the_profile(monkeypatch, tmp_path):
    import classes.agent_mcp_server as srv_mod
    from classes import info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    fake = srv_mod.ZenviMcpServer()
    monkeypatch.setattr(srv_mod, "_server", fake)
    assert srv_mod.enable_discovery("gui") == str(tmp_path / "gui_mcp.json")
    assert fake._discovery_file == str(tmp_path / "gui_mcp.json")


def test_registered_extra_tools_are_advertised(tool_stub, monkeypatch):
    import classes.agent_mcp_server as srv_mod

    def shutdown_headless_tool(save: bool = False, file_path: str = "") -> str:
        """Shut down this headless Zenvi session."""
        return "{}"

    monkeypatch.setattr(srv_mod, "_REGISTERED_EXTRA_TOOLS", {})
    assert "shutdown_headless_tool" not in srv_mod._extra_tools()
    srv_mod.register_extra_tool("shutdown_headless_tool", shutdown_headless_tool)

    defs = {d["name"]: d for d in srv_mod.iter_tool_defs()}
    assert srv_mod._extra_tools()["shutdown_headless_tool"] is shutdown_headless_tool
    schema = defs["shutdown_headless_tool"]["inputSchema"]
    assert schema["properties"] == {"save": {"type": "boolean"}, "file_path": {"type": "string"}}
    assert "required" not in schema
    # The static extras are still there.
    assert "mcp_health_tool" in defs
