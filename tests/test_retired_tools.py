"""Duplicate editor tools are retired; one tool per job.

A 2026-10-24 audit found ~30 tools doing the same job as another under a
different name (three colour-grade paths, four timeline listings, legacy
navigation verbs). Agents picked between them at random. Each retired name
maps to the tool that now does its job, and neither the handler table, the
schemas nor the MCP listing may offer the old one.
"""

import re
from pathlib import Path

from classes import tool_handlers
from classes.agent_mcp_server import iter_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS
from classes.editor_tools import REGISTRY

RETIRED = {
    # colour
    "apply_color_tool": "color_grade_clip_tool",
    "apply_look_tool": "color_grade_clip_tool",
    "list_looks_tool": "list_luts_tool",
    "inspect_color_tool": "analyze_frame_colors_tool",
    "match_color_to_reference_tool": "harmonize_look_tool",
    # listings
    "list_clips_tool": "get_timeline_state_tool",
    "list_layers_tool": "get_timeline_state_tool",
    "get_timeline_placements_metadata_tool": "get_timeline_state_tool",
    "list_files_tool": "list_project_files_tool",
    "get_file_info_tool": "list_project_files_tool",
    "get_clips_with_full_metadata_tool": "list_project_files_tool",
    # transitions catalogue
    "search_transitions_tool": "list_transitions_tool",
    # legacy navigation
    "go_to_start_tool": "seek_playhead_tool",
    "go_to_end_tool": "seek_playhead_tool",
    "zoom_in_tool": "set_timeline_view_tool",
    "zoom_out_tool": "set_timeline_view_tool",
    "center_on_playhead_tool": "set_timeline_view_tool",
    "slice_clip_at_playhead_tool": "slice_clips_tool",
    "reverse_clip_tool": "set_clip_speed_tool",
    # media
    "import_video_url_and_add_to_timeline_tool": "ingest_web_video_tool",
    "detect_beats_tool": "analyze_music_tool",
    "inspect_media_tool": "view_frames_tool",
    "transcribe_media_tool": "get_transcript_tool",
    # recreation / project
    "match_reference_tool": "plan_recreation_tool",
    "compare_recreation_tool": "check_recreation_tool",
    "set_project_profile_tool": "set_project_setting_tool",
}


def _served():
    return {d["name"] for d in iter_tool_defs()}


def _props(name):
    for d in iter_tool_defs():
        if d["name"] == name:
            schema = d.get("inputSchema") or d.get("input_schema") or {}
            return schema.get("properties") or {}
    raise AssertionError(f"{name} is not served")


# Shipped backends still call this one. It stays callable and out of the prompt.
_SHIPPED_BACKEND = {"get_clips_with_full_metadata_tool"}


def test_retired_tools_are_gone_everywhere():
    served = _served()
    for name in set(RETIRED) - _SHIPPED_BACKEND:
        assert name not in tool_handlers.AGENT_TOOL_HANDLERS, name
        assert name not in TOOL_SCHEMAS, name
        assert name not in REGISTRY, name
        assert name not in served, name
    for name in _SHIPPED_BACKEND:
        assert name in tool_handlers.AGENT_TOOL_HANDLERS
        assert name in TOOL_SCHEMAS
        assert name in served


def test_every_retired_job_has_a_served_replacement():
    served = _served()
    missing = sorted({new for new in RETIRED.values() if new not in served})
    assert not missing


def test_merged_tools_took_over_the_retired_arguments():
    # match_color_to_reference picked clips; harmonize now can too.
    assert "timeline_clip_ids" in _props("harmonize_look_tool")
    # search_transitions_tool's query moved onto the catalogue lister.
    assert "query" in _props("list_transitions_tool")
    # set_project_profile_tool's "match this clip" moved onto set_project_setting_tool;
    # `preset` was only an alias of `profile`.
    props = _props("set_project_setting_tool")
    assert "from_file_id" in props and "preset" not in props
    # detect_beats_tool took timeline clips; analyze_music_tool now does.
    assert "timeline_clip_id" in _props("analyze_music_tool")


def test_no_source_names_a_retired_tool():
    src = Path(__file__).resolve().parents[1] / "src"
    pattern = re.compile(r"\b(%s)\b" % "|".join(sorted(RETIRED)))
    hits = []
    for path in src.rglob("*"):
        if path.suffix not in (".py", ".js", ".html", ".md", ".json"):
            continue
        if path.name == "legacy_desktop.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(src)}:{lineno}")
    assert not hits, hits[:40]


def test_arguments_are_snake_case_only():
    camel = {
        d["name"]: sorted(p for p in (d.get("inputSchema") or d.get("input_schema") or {}).get("properties", {})
                          if re.search(r"[A-Z]", p))
        for d in iter_tool_defs()
    }
    assert {n: p for n, p in camel.items() if p} == {}
