"""An agent is not offered tools whose runtime this computer lacks and Zenvi cannot install.

People identity runs on onnxruntime, which release builds do not ship (it is in
requirements-speech.txt only). Without it the people tools are left out of MCP
find_tools and listed in the chat's ``unavailable_tools``, which zenvi-backend
(core/chat/client_tools.py) drops from the session. With it they are offered.
Runtimes Zenvi installs itself (HyperFrames, Manim, music, the engine) are not
hidden: their tools or the engine bridge say what to install.
"""

import json
import sys
import types

import pytest

from classes.editor_tools import REGISTRY
from classes.media_index import people_models as PM
from test_api_client_engine_tools import _sent

PEOPLE = {"scan_people_tool", "who_is_this_tool", "name_person_tool", "setup_people_tool", "people_status_tool"}


@pytest.fixture
def handlers(monkeypatch):
    """tool_handlers holding the people tools and one tool every build runs."""
    th = types.ModuleType("classes.tool_handlers")
    th.AGENT_TOOL_HANDLERS = {n: REGISTRY[n].func for n in sorted(PEOPLE) + ["generate_music_tool"]}
    th.humanize_tool_name = lambda n: n
    th.execute_tool_rich = lambda name, args: "ran %s" % name
    monkeypatch.setitem(sys.modules, "classes.tool_handlers", th)


def _found(query):
    from classes.agent_mcp_server import call_meta_tool

    return {t["name"] for t in json.loads(call_meta_tool("find_tools", {"query": query, "limit": 10}))}


QUERY = "recognise people: scan faces, who is this person, name them, set up people identity status"


def test_without_onnxruntime_find_tools_offers_no_people_tool(handlers, monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: False)
    assert not _found(QUERY) & PEOPLE
    assert "generate_music_tool" in _found("generate music")


def test_with_onnxruntime_find_tools_offers_the_people_tools(handlers, monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: True)
    assert PEOPLE <= _found(QUERY)


def test_without_onnxruntime_the_chat_tells_the_backend_the_people_tools_cannot_run(monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: False)
    assert PEOPLE <= set(_sent(monkeypatch, [])["unavailable_tools"])


def test_with_onnxruntime_the_chat_withholds_no_tool(monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: True)
    assert "unavailable_tools" not in _sent(monkeypatch, [])


# --- the engine bridge: why no engine tool is listed is in the log -------------

def test_auto_without_a_runtime_logs_why_no_engine_tool_is_listed(monkeypatch):
    from classes import engine_bridge

    def missing(env=None):
        raise engine_bridge.BridgeUnavailable("Node.js is not at /nowhere/node (ZENVI_NODE / the engine-node-path setting)")
    monkeypatch.setattr(engine_bridge, "locate_cli", missing)
    logged = []
    for level in ("info", "warning"):
        monkeypatch.setattr(engine_bridge.log, level, lambda msg, *a: logged.append(msg % a))
    assert engine_bridge.bridge_mode({"ZENVI_ENGINE_BRIDGE": "auto"}) == ""
    assert any("/nowhere/node" in line for line in logged)


def test_a_bad_cli_path_is_logged_with_the_path_and_reported_as_named(tmp_path, monkeypatch):
    from classes import engine_bridge, engine_runtime, info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(info, "PATH", str(tmp_path / "app"))
    bad = str(tmp_path / "typo" / "zenvi.mjs")
    logged = []
    monkeypatch.setattr(engine_runtime.log, "warning", lambda msg, *a: logged.append(msg % a))
    monkeypatch.setattr(engine_bridge, "find_node", lambda env=None: "/usr/bin/node")
    monkeypatch.setattr(engine_bridge, "node_major", lambda node: 24)
    status = engine_runtime.status({"ZENVI_ENGINE_CLI": bad})
    assert not status["ready"] and status["named"] and bad in status["problem"]
    assert any(bad in line for line in logged)
    # Nothing named, nothing installed: not a fault worth a warning.
    logged.clear()
    status = engine_runtime.status({})
    assert not status["named"] and not logged
