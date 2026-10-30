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

    def set_clip_speed(timeline_clip_ids=None, reverse=False, **_kw):
        """Speed clips up or slow them down, or play them backwards (reverse)."""
        return "REVERSED %s" % ",".join(timeline_clip_ids or [])

    th.AGENT_TOOL_HANDLERS = {"search_footage_tool": search_footage, "set_clip_speed_tool": set_clip_speed}
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


def test_multi_clip_assembly_is_core():
    """Placing clips one by one, agents got crossfade overlaps wrong (1 s or none for a
    0.5 s fade); the batch tools do the overlap maths, so they must not hide behind find_tools."""
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES

    assert {"add_clips_to_timeline_tool", "add_transitions_between_clips_tool"} <= set(CORE_TOOL_NAMES)
    # Their one-at-a-time twins duplicate them; they stay findable, not listed.
    assert not {"add_clip_to_timeline_tool", "apply_transition_tool"} & set(CORE_TOOL_NAMES)


def test_the_survey_and_check_steps_are_core():
    """Instructions say storyboard_tool first and every receipt's watchSuggested asks for inspect_timeline_tool;
    storyboard_tool also describes every shot, so describe_shots_tool is its duplicate."""
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES

    assert {"storyboard_tool", "inspect_timeline_tool"} <= set(CORE_TOOL_NAMES)
    assert "describe_shots_tool" not in CORE_TOOL_NAMES


def test_the_mcp_lists_core_tools_and_search_only(tool_stub):
    from classes.agent_mcp_server import _extra_tools, listed_tool_defs

    names = {d["name"] for d in listed_tool_defs()}
    assert {"find_tools", "run_tool", "search_footage_tool"} <= names
    assert "set_clip_speed_tool" not in names
    assert names - set(_extra_tools()) - {"find_tools", "run_tool"} <= {"search_footage_tool"}


PLAYBOOK_ACTIONS = {
    "add a title or end card": "add_title_tool",
    "balance the mix: even voices, music under them, loudness": "balance_mix_tool",
    "duck music under speech": "duck_under_speech_tool",
    "remove silent pauses": "remove_silence_tool",
    "set the project profile: resolution, aspect ratio, frame rate": "set_project_setting_tool",
    "even out the look so clips match each other": "harmonize_look_tool",
    "sync cuts to the music beats": "sync_cuts_to_beats_tool",
    "remove filler words like um and uh": "remove_words_tool",
    "record the edit brief: goal, form, length": "set_edit_brief_tool",
}


def test_playbook_actions_find_their_tool_in_the_top_three():
    """The playbook and instructions describe unlisted tools in words; find_tools must map the words back."""
    from classes import tool_handlers  # noqa: F401  (merges the editor-tool registry)
    from classes.agent_mcp_server import call_meta_tool

    missed = {}
    for query, want in PLAYBOOK_ACTIONS.items():
        names = [t["name"] for t in json.loads(call_meta_tool("find_tools", {"query": query, "limit": 3}))]
        if want not in names:
            missed[query] = names
    assert missed == {}


def test_find_tools_says_each_result_runs_through_run_tool():
    from classes import tool_handlers  # noqa: F401
    from classes.agent_mcp_server import call_meta_tool

    found = json.loads(call_meta_tool("find_tools", {"query": "add a title"}))
    assert found and all('run_tool(name="%s"' % t["name"] in t["call"] for t in found)


def test_find_tools_returns_unlisted_tools_with_schemas(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    found = json.loads(call_meta_tool("find_tools", {"query": "play the clip backwards reverse"}))
    assert found[0]["name"] == "set_clip_speed_tool"
    assert "inputSchema" in found[0]


def test_run_tool_runs_a_found_tool(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    out = call_meta_tool("run_tool", {"name": "set_clip_speed_tool", "args": {"timeline_clip_ids": ["c1"], "reverse": True}})
    assert "REVERSED c1" in str(out)


def test_run_tool_rejects_unknown_arguments(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    out = call_meta_tool("run_tool", {"name": "set_clip_speed_tool", "args": {"bogus": 1}})
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
    call_meta_tool("run_tool", {"name": "set_clip_speed_tool", "args": {"timeline_clip_ids": ["c1"], "reverse": True}})
    assert seen["chat_session_id"] == "mcp"


def test_find_tools_reports_a_bad_limit_instead_of_raising(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    assert call_meta_tool("find_tools", {"query": "reverse", "limit": "many"}).startswith("Error")
    assert json.loads(call_meta_tool("find_tools", {"query": "reverse", "limit": "1"}))


def test_find_tools_only_returns_tools_that_are_not_listed(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    found = json.loads(call_meta_tool("find_tools", {"query": "search footage reverse clip"}))
    assert [t["name"] for t in found] == ["set_clip_speed_tool"]


def test_run_tool_rejects_args_that_are_not_an_object(tool_stub):
    from classes.agent_mcp_server import call_meta_tool

    for bad in ([1], "x"):
        assert str(call_meta_tool("run_tool", {"name": "set_clip_speed_tool", "args": bad})).startswith("Error")
