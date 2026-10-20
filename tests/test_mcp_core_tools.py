"""The editor MCP lists a small core set; every other tool is found, then run.

External agents (Claude Code, Codex) picked wrong tools out of 200+ listed
side by side. They now see the core tools plus find_tools / run_tool.
"""

import json
import sys
import types

import pytest


@pytest.fixture
def tool_stub():
    th = types.ModuleType("classes.tool_handlers")

    def search_footage(query="", **_kw):
        """Search the footage by what happens, what is said and how it looks."""
        return "SEARCH query=%s" % query

    def reverse_clip(timeline_clip_id="", **_kw):
        """Reverse a clip so it plays backwards."""
        return "REVERSED %s" % timeline_clip_id

    th.AGENT_TOOL_HANDLERS = {"search_footage_tool": search_footage, "reverse_clip_tool": reverse_clip}
    th.humanize_tool_name = lambda n: n
    th.execute_tool_rich = lambda name, args: th.AGENT_TOOL_HANDLERS[name](**(args or {}))
    saved = sys.modules.get("classes.tool_handlers")
    sys.modules["classes.tool_handlers"] = th
    try:
        yield th
    finally:
        if saved is not None:
            sys.modules["classes.tool_handlers"] = saved
        else:
            sys.modules.pop("classes.tool_handlers", None)


def test_core_tools_are_real_handlers():
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES
    from classes.tool_handlers import AGENT_TOOL_HANDLERS

    assert set(CORE_TOOL_NAMES) <= set(AGENT_TOOL_HANDLERS)
    assert len(CORE_TOOL_NAMES) < 30


def test_the_mcp_lists_core_tools_and_search_only(tool_stub):
    from classes.agent_mcp_server import _extra_tools, listed_tool_defs

    names = {d["name"] for d in listed_tool_defs()}
    assert {"find_tools", "run_tool", "search_footage_tool"} <= names
    assert "reverse_clip_tool" not in names
    assert names - set(_extra_tools()) - {"find_tools", "run_tool"} <= {"search_footage_tool"}


def test_find_tools_returns_unlisted_tools_with_schemas(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    found = json.loads(call_meta_tool("find_tools", {"query": "play the clip backwards reverse"}))
    assert found[0]["name"] == "reverse_clip_tool"
    assert "inputSchema" in found[0]


def test_run_tool_runs_a_found_tool(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    out = call_meta_tool("run_tool", {"name": "reverse_clip_tool", "args": {"timeline_clip_id": "c1"}})
    assert "REVERSED c1" in str(out)


def test_run_tool_rejects_unknown_arguments(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    out = call_meta_tool("run_tool", {"name": "reverse_clip_tool", "args": {"bogus": 1}})
    assert str(out).startswith("Error")


def test_the_manifest_carries_the_core_set():
    sys.path.insert(0, "scripts")
    from export_editor_tool_manifest import build_manifest
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES

    assert build_manifest()["core_tools"] == list(CORE_TOOL_NAMES)


def test_run_tool_uses_the_mcp_chat_slot(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    seen = {}
    tool_stub.execute_tool_rich = lambda name, args: seen.update(args) or "ok"
    call_meta_tool("run_tool", {"name": "reverse_clip_tool", "args": {"timeline_clip_id": "c1"}})
    assert seen["chat_session_id"] == "mcp"


def test_find_tools_reports_a_bad_limit_instead_of_raising(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    assert call_meta_tool("find_tools", {"query": "reverse", "limit": "many"}).startswith("Error")
    assert json.loads(call_meta_tool("find_tools", {"query": "reverse", "limit": "1"}))
