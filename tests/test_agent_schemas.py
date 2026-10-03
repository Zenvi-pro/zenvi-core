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
        "timeline_clip_id": "c1",
        "style": "highlight",
    }) is None


def test_validate_accepts_transcript_aliases():
    assert validate_args("get_transcript_tool", {
        "timeline_clip_id": "PHM7T104LX",
    }) is None
    assert validate_args("get_transcript_tool", {
        "file_id": "FZOEBK3JJZ",
    }) is None


# ---------------------------------------------------------------------------
# Schemas vs. the handlers that run them, and vs. the assistant backend.
#
# execute_tool refuses any argument outside TOOL_SCHEMAS before dispatch, so a
# schema that drifts from its handler silently breaks real calls: the backend's
# apply_transition_tool sends clip1_id/clip2_id/transition_name, and a schema
# listing name/transition/timeline_clip_id refused every one of them.
# ---------------------------------------------------------------------------

import inspect
import json
from pathlib import Path

from classes.agent_tools.schema import HIDDEN_PARAMS
from classes.editor_tools import coerce_args

# Properties a handler takes through **kwargs instead of by name: read with
# kwargs.get() or passed on to a callee that consumes them.
KWARG_PROPS = {
    # forwarded to add_clip_to_timeline(**_kw)
    "import_video_url_and_add_to_timeline_tool": {
        "query", "start_seconds", "end_seconds", "duration_seconds", "full_file",
    },
    # read by the shared timeline-clip resolver
    "modify_clip_tool": {"occurrence", "position_near", "track"},
    "search_clip_scenes_tool": {"occurrence", "position_near", "track"},
    "watch_clip_window_tool": {"occurrence", "position_near", "track"},
    "slice_clip_at_best_match_tool": {"track"},
    "split_file_add_clip_tool": {"require_visual_match"},
    "watch_clip_tool": {"query"},
    # Phase 5 speech tools read the snake_case aliases for clipId / fileId
    "transcribe_media_tool": {"file_id"},
    "get_transcript_tool": {"file_id", "timeline_clip_id"},
    "remove_words_tool": {"timeline_clip_id"},
    "remove_silence_tool": {"timeline_clip_id"},
    "export_captions_tool": {"timeline_clip_id"},
    "detect_beats_tool": {"file_id", "timeline_clip_id"},
    "diarize_media_tool": {"file_id", "timeline_clip_id"},
    # Phase 6 colour tools accept camelCase allClips via **kwargs / **_kw
    "apply_color_tool": {"allClips"},
    "apply_look_tool": {"allClips"},
    "match_color_to_reference_tool": {"allClips"},
}

# Sent by the assistant backend's stubs but unused by this handler. They stay in
# the schema so those calls validate; list them here so the drift is visible.
BACKEND_ONLY_PROPS = {
    "get_timeline_state_tool": {"detail_level"},
    "list_clips_tool": {"detail_level"},
}


def _handler_params(handler):
    sig = inspect.signature(handler)
    named, required, varkw = set(), set(), False
    for p in sig.parameters.values():
        if p.kind == p.VAR_KEYWORD:
            varkw = True
        elif p.kind != p.VAR_POSITIONAL and p.name not in HIDDEN_PARAMS:
            named.add(p.name)
            if p.default is inspect.Parameter.empty:
                required.add(p.name)
    return named, required, varkw


def test_schema_properties_match_handler_signatures():
    problems = []
    for name, schema in TOOL_SCHEMAS.items():
        handler = tool_handlers.AGENT_TOOL_HANDLERS.get(name)
        if handler is None:
            problems.append(f"{name}: schema without a registered handler")
            continue
        named, required, varkw = _handler_params(handler)
        props = set(schema.get("properties") or {})
        via_kw = KWARG_PROPS.get(name, set()) | BACKEND_ONLY_PROPS.get(name, set())
        if via_kw and not varkw:
            problems.append(f"{name}: allowlisted kwargs {sorted(via_kw)} but handler takes no **kwargs")
        if via_kw & named:
            problems.append(f"{name}: {sorted(via_kw & named)} are named params, drop them from the allowlist")
        if via_kw - props:
            problems.append(f"{name}: allowlisted {sorted(via_kw - props)} missing from the schema")
        unknown = props - named - via_kw
        if unknown:
            problems.append(f"{name}: schema accepts {sorted(unknown)} but the handler does not take them")
        unexposed = named - props
        if unexposed:
            problems.append(f"{name}: handler params {sorted(unexposed)} missing from the schema")
        not_required = required - set(schema.get("required") or [])
        if not_required:
            problems.append(f"{name}: handler requires {sorted(not_required)} but the schema does not")
    assert problems == [], "\n".join(problems)


_BACKEND_STUBS = Path(__file__).parent / "fixtures" / "backend_tool_stubs.json"


def _backend_value(annotation, prop_schema=None):
    enum = (prop_schema or {}).get("enum")
    if enum:
        return enum[0]
    ann = annotation.rstrip("!").replace(" ", "")
    if ann.startswith("Optional["):
        ann = ann[len("Optional["):-1]
    if ann == "float":
        return 1.5
    if ann == "int":
        return 2
    if ann == "bool":
        return True
    if ann.startswith(("list", "List")):
        return ["x"]
    return "1"


def test_backend_stub_calls_validate():
    """Every argument the backend's @tool stubs can send must pass the schema.

    The fixture snapshots zenvi-backend's stub signatures; refresh it when the
    backend adds or renames a desktop tool argument.
    """
    stubs = json.loads(_BACKEND_STUBS.read_text(encoding="utf-8"))["tools"]
    failures = []
    for name, params in sorted(stubs.items()):
        if name not in tool_handlers.AGENT_TOOL_HANDLERS:
            continue
        props = TOOL_SCHEMAS[name].get("properties") or {}
        payload = {p: _backend_value(a, props.get(p)) for p, a in params.items()}
        # execute_tool coerces an editor tool's arguments ("1" -> True, 2 -> "2") before validating.
        err = validate_args(name, coerce_args(name, payload))
        if err:
            failures.append(err)
        for p, a in params.items():
            if a.replace(" ", "").startswith("Optional["):
                err = validate_args(name, coerce_args(name, {**payload, p: None}))
                if err:
                    failures.append(err)
    assert failures == [], "\n".join(failures)


def test_backend_required_args_are_accepted_alone():
    stubs = json.loads(_BACKEND_STUBS.read_text(encoding="utf-8"))["tools"]
    for name, params in stubs.items():
        if name not in tool_handlers.AGENT_TOOL_HANDLERS:
            continue
        props = TOOL_SCHEMAS[name].get("properties") or {}
        minimal = {p: _backend_value(a, props.get(p)) for p, a in params.items() if a.endswith("!")}
        assert validate_args(name, minimal) is None, name


def test_apply_transition_accepts_the_backend_call():
    """The call that exposed the drift: the backend's between-clips transition."""
    assert validate_args("apply_transition_tool", {
        "clip1_id": "A1", "clip2_id": "B2", "transition_name": "fade",
        "duration": "1.0", "placement": "between",
    }) is None
    assert validate_args("apply_transition_tool", {"transition": "fade"}) is not None


def test_import_files_accepts_a_list_of_paths():
    """The handler takes a list; the schema refused it before the handler ran."""
    from classes.agent_tools.schema import normalize_args
    args = {"paths": ["/media/a.mp4", "/media/b.mp4"]}
    assert validate_args("import_files_tool", normalize_args("import_files_tool", args)) is None
    assert validate_args("import_files_tool", {"paths": "/media/a.mp4,/media/b.mp4"}) is None
