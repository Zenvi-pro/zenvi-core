"""Schema registry tests for agent tools (contract 3)."""

import pytest

from classes.agent_tools.schema import (
    PRIORITY_SCHEMAS,
    TOOL_SCHEMAS,
    UNSCHEMATIZED,
    get_schema,
    validate_args,
)
from classes import tool_handlers


def test_every_registered_tool_has_schema_or_allowlist():
    missing = []
    for name in tool_handlers.AGENT_TOOL_HANDLERS:
        if name not in TOOL_SCHEMAS and name not in UNSCHEMATIZED:
            missing.append(name)
    assert missing == [], f"tools without schema: {missing}"


def test_unschematized_allowlist_is_empty():
    assert UNSCHEMATIZED == frozenset()


def test_priority_schemas_reject_unknown_keys():
    for name in PRIORITY_SCHEMAS:
        schema = get_schema(name)
        assert schema is not None
        assert schema.get("additionalProperties") is False
        err = validate_args(name, {"__totally_unknown__": 1})
        assert err is not None
        assert err.startswith("Error:")


def test_add_clip_schema_accepts_core_args():
    assert validate_args("add_clip_to_timeline_tool", {
        "file_id": "abc",
        "position_seconds": 1.5,
        "track": "1",
    }) is None


def test_delete_scope_enum():
    assert validate_args("delete_from_timeline_tool", {"scope": "nope"}) is not None
    assert validate_args("delete_from_timeline_tool", {
        "timeline_clip_id": "c1",
        "scope": "clip",
    }) is None


def test_set_keyframes_requires_points():
    err = validate_args("set_keyframes_tool", {"property": "volume"})
    assert err is not None
    assert validate_args("set_keyframes_tool", {
        "property": "alpha",
        "points": [{"seconds": 0, "value": 1.0}, {"seconds": 1, "value": 0.0}],
        "timeline_clip_id": "c1",
    }) is None


def test_transaction_id_not_in_public_schema():
    schema = get_schema("add_clip_to_timeline_tool")
    assert "transaction_id" not in schema["properties"]


def test_mcp_iter_tool_defs_uses_strict_schemas():
    from classes.agent_mcp_server import iter_tool_defs
    defs = {d["name"]: d for d in iter_tool_defs()}
    for name in tool_handlers.AGENT_TOOL_HANDLERS:
        assert defs[name]["inputSchema"]["additionalProperties"] is False


def test_normalize_drops_empty_track_index_and_coerces_string_int():
    from classes.agent_tools.schema import normalize_args

    dropped = normalize_args("get_transcript_tool", {
        "clipId": "c1",
        "trackIndex": "",
        "force": "false",
    })
    assert "trackIndex" not in dropped
    assert dropped["force"] is False
    assert dropped["clipId"] == "c1"

    coerced = normalize_args("get_transcript_tool", {"trackIndex": "0", "force": "true"})
    assert coerced["trackIndex"] == 0
    assert coerced["force"] is True


def test_validate_accepts_assistant_stringly_transcript_args():
    assert validate_args("get_transcript_tool", {
        "clipId": "c1",
        "trackIndex": "",
        "force": "false",
    }) is None
    assert validate_args("get_transcript_tool", {
        "fileId": "f1",
        "trackIndex": "0",
    }) is None


def test_validate_accepts_timeline_detail_level():
    assert validate_args("get_timeline_state_tool", {"detail_level": "summary"}) is None
    assert validate_args("get_timeline_state_tool", {}) is None
    assert validate_args("list_clips_tool", {"detail_level": "full"}) is None
    assert validate_args("list_clips_tool", {}) is None


def test_validate_accepts_remove_words_matches():
    assert validate_args("remove_words_tool", {
        "clipId": "c1",
        "matches": ["FlowCut", "flocut"],
    }) is None
    assert validate_args("add_captions_tool", {
        "clipId": "c1",
        "engine": "auto",
    }) is None


def test_validate_accepts_transcript_aliases():
    assert validate_args("get_transcript_tool", {
        "timeline_clip_id": "PHM7T104LX",
    }) is None
    assert validate_args("get_transcript_tool", {
        "file_id": "FZOEBK3JJZ",
    }) is None
