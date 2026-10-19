"""The v1 cloud search, watch, catalog and summarize tools are gone: nothing registers, schemas, labels or lists them.

Moment finding goes through search_footage_tool (the local shelf), so the agent is never offered a
tool that no longer exists.
"""

from classes import tool_handlers as th
from classes.agent_mcp_server import SERVER_INSTRUCTIONS, iter_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS

REMOVED = ("search_clips_tool", "search_clip_scenes_tool", "slice_clip_at_best_match_tool", "watch_clip_window_tool",
           "get_project_catalog_tool", "resummarize_project_file_tool")


def test_removed_tools_are_not_registered_anywhere():
    exposed = {d["name"] for d in iter_tool_defs()}
    for name in REMOVED:
        assert name not in th.AGENT_TOOL_HANDLERS, name
        assert name not in th.TOOL_DISPLAY_LABELS, name
        assert name not in th.BACKGROUND_SAFE_TOOLS, name
        assert name not in th.READ_ONLY_TOOLS, name
        assert name not in TOOL_SCHEMAS, name
        assert name not in exposed, name


def test_the_local_moment_search_is_what_the_agent_is_pointed_at():
    assert "search_footage_tool" in th.AGENT_TOOL_HANDLERS
    assert "search_footage_tool" in TOOL_SCHEMAS
    assert "search_footage_tool" in SERVER_INSTRUCTIONS
    assert not [name for name in REMOVED if name in SERVER_INSTRUCTIONS]
