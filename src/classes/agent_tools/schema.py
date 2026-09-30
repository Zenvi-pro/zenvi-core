"""Explicit JSON Schema registry for agent tools (contract 3).

``TOOL_SCHEMAS`` is the source of truth for MCP ``inputSchema`` and for
runtime validation in ``execute_tool``. Introspection is only a CI fallback
that must fail once a tool is registered without an entry here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

import jsonschema
from jsonschema import Draft202012Validator

# Params never exposed to the model / stripped before validation.
HIDDEN_PARAMS = frozenset({"self", "chat_session_id", "transaction_id"})

# First wave (plan 3.3) — kept as a named set for tests / docs.
PRIORITY_SCHEMAS = frozenset({
    "add_clip_to_timeline_tool",
    "delete_from_timeline_tool",
    "slice_clip_at_playhead_tool",
    "slice_clip_at_best_match_tool",
    "place_motion_graphic_tool",
    "apply_transition_tool",
    "set_clip_volume_tool",
    "duck_under_speech_tool",
    "import_files_tool",
    "split_file_add_clip_tool",
    "add_track_tool",
    "add_marker_tool",
    "modify_clip_tool",
    "undo_tool",
    "redo_tool",
    "export_video_tool",
})

# Empty until every registered tool has a schema. CI fails if a handler is
# registered but missing from TOOL_SCHEMAS and not listed here.
UNSCHEMATIZED: frozenset[str] = frozenset()


def _obj(properties: dict, required: Optional[list] = None, **extra) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    schema.update(extra)
    return schema


def _str(**kw) -> dict:
    return {"type": "string", **kw}


def _bool(**kw) -> dict:
    return {"type": "boolean", **kw}


def _num(minimum=None, maximum=None, **kw) -> dict:
    out: dict[str, Any] = {"type": "number", **kw}
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


def _int(minimum=None, maximum=None, **kw) -> dict:
    out: dict[str, Any] = {"type": "integer", **kw}
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


def _str_or_num(**kw) -> dict:
    """LLM args often arrive as strings; accept either."""
    return {"oneOf": [{"type": "string"}, {"type": "number"}], **kw}


def _opt_str_or_num(**kw) -> dict:
    """Optional numeric hint (position_near): string, number, or null."""
    return {"oneOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}], **kw}


def _str_or_bool(**kw) -> dict:
    """Flags the handlers parse from 'true'/'false' strings; accept a real bool too."""
    return {"oneOf": [{"type": "string"}, {"type": "boolean"}], **kw}


def _url_list(**kw) -> dict:
    """A list of URLs, one URL string, or null (the backend's Optional[list[str]])."""
    return {"oneOf": [{"type": "array", "items": {"type": "string"}},
                      {"type": "string"}, {"type": "null"}], **kw}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    "get_project_info_tool": _obj({}),
    "list_files_tool": _obj({}),
    "list_clips_tool": _obj({
        "layer": _str(description="Optional layer_number or UI track filter."),
        "detail_level": _str(description="Sent by the assistant backend; ignored here."),
    }),
    "list_layers_tool": _obj({}),
    "list_markers_tool": _obj({}),
    "new_project_tool": _obj({}),
    "save_project_tool": _obj({
        "file_path": _str(description="Optional destination path; empty saves in place."),
    }),
    "open_project_tool": _obj({
        "file_path": _str(description="Project file path."),
    }, required=["file_path"]),
    "watch_clip_tool": _obj({
        "file_path": _str(description="Media path to import, place and play."),
        "query": _str(),
    }),
    "watch_clip_window_tool": _obj({
        "query": _str(),
        "start": _str_or_num(),
        "end": _str_or_num(),
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
    }),
    "play_tool": _obj({}),
    "go_to_start_tool": _obj({}),
    "go_to_end_tool": _obj({}),
    "undo_tool": _obj({
        "steps": _str_or_num(description="Number of undo steps."),
    }),
    "redo_tool": _obj({
        "steps": _str_or_num(),
    }),
    "add_track_tool": _obj({}),
    "add_marker_tool": _obj({}),
    "delete_from_timeline_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "scope": _str(enum=["auto", "clip", "track"]),
        "occurrence": _str(),
        "position_near": _opt_str_or_num(),
        "include_transitions": _bool(),
    }),
    "remove_clip_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
    }),
    "delete_clips_on_track_tool": _obj({
        "track": _str(),
        "include_transitions": _bool(),
    }),
    "analyze_timeline_audio_tool": _obj({
        "track": _str(),
        "timeline_clip_id": _str(),
        "detail": _str(description="'summary' (default) or 'windows'."),
    }),
    "set_clip_volume_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "level_db": _str_or_num(description="Gain in dB (e.g. -6)."),
        "level": _str_or_num(description="Linear gain 0..1."),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "fade_ms": _str_or_num(),
        "mode": _str(description="'replace' (default) or another handler mode."),
    }),
    "duck_under_speech_tool": _obj({
        "bed_clip_ids": _str(),
        "bed_query": _str(),
        "bed_track": _str(),
        "speech_clip_ids": _str(),
        "duck_db": _str_or_num(),
        "attack_ms": _str_or_num(),
        "release_ms": _str_or_num(),
        "pad_before_ms": _str_or_num(),
        "pad_after_ms": _str_or_num(),
        "boost_speech_db": _str_or_num(),
        "dry_run": _str(description="true/false — preview without writing."),
    }),
    "zoom_in_tool": _obj({}),
    "zoom_out_tool": _obj({}),
    "center_on_playhead_tool": _obj({}),
    "import_files_tool": _obj({
        "paths": _str(description="Comma-separated paths or JSON list."),
        "path": _str(),
        "folder": _str(),
        "skip_indexing": _str(),
    }),
    "wait_until_project_indexed_tool": _obj({
        "timeout_seconds": _str_or_num(),
    }),
    "export_video_tool": _obj({
        "show_dialog": _str(),
        "output_path": _str(),
    }),
    "get_export_settings_tool": _obj({}),
    "set_export_setting_tool": _obj({
        "key": _str(),
        "value": {"type": ["string", "number", "boolean", "null"]},
    }, required=["key"]),
    "get_file_info_tool": _obj({
        "file_id": _str(),
    }, required=["file_id"]),
    "split_file_add_clip_tool": _obj({
        "file_id": _str(),
        "query": _str(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "start_frame": _str_or_num(),
        "end_frame": _str_or_num(),
        "name": _str(),
        "require_visual_match": _str_or_bool(),
    }),
    "add_clip_to_timeline_tool": _obj({
        "file_id": _str(description="Media-bin file id."),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "query": _str(),
        "full_file": _str(),
    }),
    "import_video_url_and_add_to_timeline_tool": _obj({
        "video_url": _str(),
        "position_seconds": _str_or_num(),
        "track": _str_or_num(),
        "query": _str(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "duration_seconds": _str_or_num(),
        "full_file": _str_or_bool(),
    }),
    "slice_clip_at_playhead_tool": _obj({}),
    "search_clips_tool": _obj({
        "query": _str(),
        "top_k": _str_or_num(),
        "look_for": _str(description="Sent by the assistant backend; ignored here."),
    }, required=["query"]),
    "search_clip_scenes_tool": _obj({
        "query": _str(),
        "top_k": _str_or_num(),
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
    }, required=["query"]),
    "get_project_catalog_tool": _obj({}),
    "slice_clip_at_best_match_tool": _obj({
        "query": _str(),
        "occurrence": _str_or_num(),
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "start_seconds": _str_or_num(description="Sent by the assistant backend; ignored here."),
        "end_seconds": _str_or_num(description="Sent by the assistant backend; ignored here."),
    }, required=["query"]),
    "suggest_motion_graphics_placements_tool": _obj({
        "brief": _str(),
        "beat_count": _str_or_num(),
    }),
    "propose_overlay_windows_tool": _obj({
        "beat_count": _str_or_num(),
        "prefer_transparent": _str_or_bool(),
    }),
    "place_motion_graphic_tool": _obj({
        "file_id": _str(),
        "mode": _str(description="'overlay' (default), 'gap' or 'cut_in'."),
        "position_seconds": _str_or_num(),
        "duration_seconds": _str_or_num(),
        "track": _str(),
        "layout_region": _str(),
        "query": _str(),
    }),
    "fetch_motion_graphics_video_tool": _obj({
        "segment_urls": _url_list(),
        "supabase_url": _str(),
        "supabase_path": _str(),
        "render_job_id": _str(),
        "label": _str(),
    }),
    "fetch_remotion_video_from_supabase_tool": _obj({
        "segment_urls": _url_list(),
        "supabase_url": _str(),
        "supabase_path": _str(),
        "render_job_id": _str(),
        "label": _str(),
    }),
    "generate_video_and_add_to_timeline_tool": _obj({
        "prompt": _str(),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
    }, required=["prompt"]),
    "modify_clip_tool": _obj({
        "mode": _str(description="'replace' (default) or another handler mode."),
        "description": _str(),
        "query": _str(),
        "fade_ms": _str_or_num(),
        "duration_seconds": _str_or_num(),
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
    }),
    "generate_transition_clip_tool": _obj({
        "clip_a_id": _str(),
        "clip_b_id": _str(),
        "clip_a_query": _str(),
        "clip_b_query": _str(),
        "prompt_hint": _str(),
    }),
    "list_transitions_tool": _obj({
        "category": _str(description="'all' (default), 'common' or 'extra'."),
    }),
    "search_transitions_tool": _obj({
        "query": _str(),
    }, required=["query"]),
    "apply_transition_tool": _obj({
        "clip1_id": _str(),
        "clip2_id": _str(description="Required when placement is 'between'."),
        "transition_name": _str(),
        "duration": _str_or_num(description="Seconds."),
        "placement": _str(description="'between' (default), 'start' or 'end'."),
    }, required=["clip1_id"]),
    "generate_tts_and_add_to_timeline_tool": _obj({
        "text": _str(),
        "voice": _str(),
        "model": _str(),
        "speed": _str_or_num(),
        "track": _str_or_num(),
        "position": _str_or_num(description="Timeline position in seconds."),
    }, required=["text"]),
    "import_stock_media_tool": _obj({
        "source": _str(description="'pexels' or 'freesound'; empty with local_path."),
        "video_id": _str_or_num(),
        "link": _str(),
        "sound_id": _str_or_num(),
        "preview_url": _str(),
        "filename": _str(),
        "local_path": _str(),
    }),
    "resummarize_project_file_tool": _obj({
        "file_id": _str(),
    }, required=["file_id"]),
    "reindex_project_file_tool": _obj({
        "file_id": _str(),
        "force": _str_or_bool(),
    }, required=["file_id"]),
    "get_clips_with_full_metadata_tool": _obj({
        "detail_level": _str(),
    }),
    "get_timeline_placements_metadata_tool": _obj({
        "detail_level": _str(),
    }),
    "get_timeline_state_tool": _obj({
        "detail_level": _str(description="Sent by the assistant backend; ignored here."),
    }),
    # Phase 3.8 new tools
    "add_effect_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
        "effect": _str(description="libopenshot EffectInfo class_name."),
        "effect_name": _str(),
    }),
    "add_title_tool": _obj({
        "text": _str(description="Title / lower-third text."),
        "template": _str(description="Bundled SVG template basename without .svg."),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
        "file_name": _str(),
    }, required=["text"]),
    "set_keyframes_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "position_near": _opt_str_or_num(),
        "property": _str(enum=[
            "volume", "alpha", "location_x", "location_y",
            "scale_x", "scale_y", "rotation",
        ]),
        "points": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "frame": _int(minimum=1),
                    "seconds": _num(minimum=0),
                    "value": _num(),
                },
                "additionalProperties": False,
                "required": ["value"],
            },
        },
    }, required=["property", "points"]),
    "set_project_setting_tool": _obj({
        "fps": _str_or_num(),
        "fps_num": _int(minimum=1),
        "fps_den": _int(minimum=1),
        "width": _int(minimum=16, maximum=8192),
        "height": _int(minimum=16, maximum=8192),
        "sample_rate": _int(minimum=8000, maximum=192000),
    }),
}


def get_schema(tool_name: str) -> Optional[dict]:
    schema = TOOL_SCHEMAS.get(tool_name)
    return deepcopy(schema) if schema is not None else None


def validate_args(tool_name: str, args: Optional[dict]) -> Optional[str]:
    """Return an Error: message if *args* fail the tool schema, else None.

    Tools without a ``TOOL_SCHEMAS`` entry are allowed through (tests and
    dynamic handlers). CI asserts every ``AGENT_TOOL_HANDLERS`` name has a
    schema so production tools cannot skip validation silently.
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return None
    payload = dict(args or {})
    for hidden in HIDDEN_PARAMS:
        payload.pop(hidden, None)
    try:
        Draft202012Validator(schema).validate(payload)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"Error: Invalid arguments for {tool_name}: {exc.message} (at {path})."
    return None


def strip_hidden(args: Optional[dict]) -> dict:
    out = dict(args or {})
    for hidden in HIDDEN_PARAMS:
        out.pop(hidden, None)
    return out
