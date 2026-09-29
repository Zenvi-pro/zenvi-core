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


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    "get_project_info_tool": _obj({}),
    "list_files_tool": _obj({}),
    "list_clips_tool": _obj({
        "layer": _str(description="Optional layer_number or UI track filter."),
    }),
    "list_layers_tool": _obj({}),
    "list_markers_tool": _obj({}),
    "new_project_tool": _obj({}),
    "save_project_tool": _obj({
        "path": _str(description="Optional destination path."),
    }),
    "open_project_tool": _obj({
        "path": _str(description="Project file path."),
    }, required=["path"]),
    "watch_clip_tool": _obj({
        "file_id": _str(),
        "timeline_clip_id": _str(),
        "query": _str(),
    }),
    "watch_clip_window_tool": _obj({
        "query": _str(),
        "start": _str_or_num(),
        "end": _str_or_num(),
        "timeline_clip_id": _str(),
        "file_id": _str(),
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
    "add_track_tool": _obj({
        "label": _str(),
        "name": _str(),
    }),
    "add_marker_tool": _obj({
        "position_seconds": _str_or_num(),
        "label": _str(),
        "text": _str(),
    }),
    "delete_from_timeline_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "scope": _str(enum=["auto", "clip", "track"]),
        "occurrence": _str(),
        "position_near": _str_or_num(),
        "include_transitions": _bool(),
    }),
    "remove_clip_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "scope": _str(enum=["auto", "clip", "track"]),
        "occurrence": _str(),
        "position_near": _str_or_num(),
        "include_transitions": _bool(),
    }),
    "delete_clips_on_track_tool": _obj({
        "track": _str(),
        "include_transitions": _bool(),
    }),
    "analyze_timeline_audio_tool": _obj({
        "track": _str(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
    }),
    "set_clip_volume_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "volume": _str_or_num(description="Linear gain 0..1 or dB string."),
        "gain": _str_or_num(),
        "db": _str_or_num(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
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
        "file_id": _str(),
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
        "query": _str(),
    }),
    "split_file_add_clip_tool": _obj({
        "file_id": _str(),
        "query": _str(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "position_seconds": _str_or_num(),
        "track": _str(),
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
        "url": _str(),
        "position_seconds": _str_or_num(),
        "track": _str(),
    }, required=["url"]),
    "slice_clip_at_playhead_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
    }),
    "search_clips_tool": _obj({
        "query": _str(),
        "limit": _str_or_num(),
        "file_id": _str(),
    }, required=["query"]),
    "search_clip_scenes_tool": _obj({
        "query": _str(),
        "file_id": _str(),
        "limit": _str_or_num(),
    }, required=["query"]),
    "get_project_catalog_tool": _obj({}),
    "slice_clip_at_best_match_tool": _obj({
        "query": _str(),
        "file_id": _str(),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
    }, required=["query"]),
    "suggest_motion_graphics_placements_tool": _obj({
        "query": _str(),
        "limit": _str_or_num(),
    }),
    "propose_overlay_windows_tool": _obj({
        "query": _str(),
        "limit": _str_or_num(),
    }),
    "place_motion_graphic_tool": _obj({
        "url": _str(),
        "file_id": _str(),
        "mode": _str(enum=["overlay", "gap", "cut_in"]),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "query": _str(),
    }),
    "fetch_motion_graphics_video_tool": _obj({
        "url": _str(),
        "session_id": _str(),
        "job_id": _str(),
    }),
    "fetch_remotion_video_from_supabase_tool": _obj({
        "url": _str(),
        "session_id": _str(),
        "job_id": _str(),
    }),
    "generate_video_and_add_to_timeline_tool": _obj({
        "prompt": _str(),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
        "model": _str(),
    }, required=["prompt"]),
    "modify_clip_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "file_id": _str(),
        "prompt": _str(),
        "query": _str(),
        "start_seconds": _str_or_num(),
        "end_seconds": _str_or_num(),
        "track": _str(),
    }),
    "generate_transition_clip_tool": _obj({
        "prompt": _str(),
        "from_clip_id": _str(),
        "to_clip_id": _str(),
        "duration_seconds": _str_or_num(),
    }),
    "list_transitions_tool": _obj({}),
    "search_transitions_tool": _obj({
        "query": _str(),
    }, required=["query"]),
    "apply_transition_tool": _obj({
        "name": _str(),
        "transition": _str(),
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "position_seconds": _str_or_num(),
        "duration_seconds": _str_or_num(),
        "track": _str(),
        "brightness": _str_or_num(),
        "contrast": _str_or_num(),
    }),
    "generate_tts_and_add_to_timeline_tool": _obj({
        "text": _str(),
        "voice": _str(),
        "position_seconds": _str_or_num(),
        "track": _str(),
    }, required=["text"]),
    "import_stock_media_tool": _obj({
        "query": _str(),
        "media_type": _str(enum=["video", "image", "audio", "any"]),
        "position_seconds": _str_or_num(),
        "track": _str(),
        "duration_seconds": _str_or_num(),
        "start_seconds": _str_or_num(),
        "limit": _str_or_num(),
    }, required=["query"]),
    "resummarize_project_file_tool": _obj({
        "file_id": _str(),
    }, required=["file_id"]),
    "reindex_project_file_tool": _obj({
        "file_id": _str(),
    }, required=["file_id"]),
    "get_clips_with_full_metadata_tool": _obj({
        "file_id": _str(),
        "limit": _str_or_num(),
        "detail_level": _str(description="summary (default) or full."),
    }),
    "get_timeline_placements_metadata_tool": _obj({
        "track": _str(),
        "detail_level": _str(description="summary (default) or full."),
    }),
    "get_timeline_state_tool": _obj({
        "detail_level": _str(
            description="summary (default) or full — accepted for Assistant/LLM compat.",
        ),
    }),
    # Phase 3.8 new tools
    "add_effect_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
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

    "inspect_timeline_tool": _obj({
        "startFrame": _int(minimum=0, description="0-index project frame."),
        "endFrame": _int(minimum=0, description="Exclusive end in 0-index frames."),
        "maxFrames": _int(minimum=1, maximum=12, description="Default 6, max 12."),
        "clipId": {"type": "string", "description": "From watchSuggested.clipId."},
        "start": _num(description="watchSuggested.start seconds"),
        "end": _num(description="watchSuggested.end seconds"),
        "overview": {"type": "boolean", "description": "One storyboard instead of N frames."},
    }),
    "inspect_media_tool": _obj({
        "fileId": {"type": "string", "description": "Zenvi file id from list_files."},
        "clipId": {"type": "string"},
        "start": _num(minimum=0),
        "end": _num(minimum=0),
        "maxFrames": _int(minimum=1, maximum=12),
        "overview": {"type": "boolean"},
    }, required=["fileId"]),

    "get_transcript_tool": _obj({
        "clipId": _str(description="Timeline clip id. Prefer this after edits."),
        "timeline_clip_id": _str(
            description="Alias for clipId (Assistant / list_clips naming).",
        ),
        "fileId": _str(description="Media-bin file id (source seconds at timeline 0)."),
        "file_id": _str(description="Alias for fileId."),
        "trackIndex": _int(minimum=0, description="UI track index; omit for all clips."),
        "language": _str(description="BCP-47 / whisper code, or 'auto'."),
        "modelId": _str(description="e.g. faster-whisper-base (whisper path only)."),
        "force": _bool(description="Ignore cache and re-run ASR."),
        "engine": _str(
            description="auto|apple|whisper. auto=Apple SpeechAnalyzer on macOS 26+ when helper is present, else Whisper. Windows always Whisper.",
        ),
        "detail_level": _str(
            description="Omit for a compact receipt (script + compactWords). Pass 'full' to also include the verbose words array.",
        ),
        "includeWords": _bool(
            description="If true, include the verbose per-word timing array (same as detail_level=full).",
        ),
    }),
    "transcribe_media_tool": _obj({
        "fileId": _str(description="Media-bin file id."),
        "file_id": _str(description="Alias for fileId."),
        "language": _str(description="BCP-47 / whisper code, or 'auto'."),
        "modelId": _str(description="e.g. faster-whisper-base"),
        "force": _bool(description="Ignore cache and re-run ASR."),
        "engine": _str(description="auto|apple|whisper"),
    }),
    "remove_words_tool": _obj({
        "clipId": _str(description="Timeline clip to cut."),
        "timeline_clip_id": _str(description="Alias for clipId."),
        "wordIndices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "description": "Word indices from get_transcript_tool for this clip.",
        },
        "fillerPreset": _str(
            description="Built-in preset: um_uh or english_fillers.",
        ),
        "transcriptGeneration": _int(
            minimum=1,
            description="Pin from get_transcript; refuse on mismatch.",
        ),
        "language": _str(),
        "modelId": _str(),
        "engine": _str(description="auto|apple|whisper"),
    }),

    "remove_silence_tool": _obj({
        "clipId": _str(description="Timeline clip to tighten."),
        "timeline_clip_id": _str(description="Alias for clipId."),
        "minPauseSec": _num(minimum=0.05, description="Minimum silence gap to remove."),
        "padSec": _num(minimum=0.0, description="Keep this much audio around speech."),
        "maxRemoveFraction": _num(
            minimum=0.1, maximum=0.99,
            description="Refuse if more than this fraction would be deleted.",
        ),
    }),

    "add_captions_tool": _obj({
        "clipId": _str(),
        "timeline_clip_id": _str(description="Alias for clipId."),
        "trackIndex": _int(minimum=0),
        "maxWords": _int(minimum=1, maximum=24),
        "maxChars": _int(minimum=8, maximum=120),
        "language": _str(),
        "modelId": _str(),
        "srtPath": _str(description="Import SRT/VTT instead of transcribing."),
    }),

    "export_captions_tool": _obj({
        "path": _str(description="Destination .srt or .vtt path."),
        "format": _str(description="srt or vtt."),
        "clipId": _str(),
        "timeline_clip_id": _str(description="Alias for clipId."),
        "trackIndex": _int(minimum=0),
        "language": _str(),
    }, required=["path"]),

    "detect_beats_tool": _obj({
        "fileId": _str(),
        "file_id": _str(description="Alias for fileId."),
        "clipId": _str(),
        "timeline_clip_id": _str(description="Alias for clipId."),
    }),

    "diarize_media_tool": _obj({
        "fileId": _str(),
        "file_id": _str(description="Alias for fileId."),
        "clipId": _str(),
        "timeline_clip_id": _str(description="Alias for clipId."),
        "maxSpeakers": _int(minimum=1, maximum=8),
        "language": _str(),
        "modelId": _str(),
    }),

    "search_media_local_tool": _obj({
        "query": _str(description="Visual / semantic query."),
        "top_k": _int(minimum=1, maximum=50),
        "provider": _str(description="local or auto."),
    }, required=["query"]),

}


def _prop_types(prop: dict) -> set[str]:
    raw = prop.get("type")
    if isinstance(raw, list):
        return {str(t) for t in raw}
    if raw is None:
        return set()
    return {str(raw)}


def normalize_args(tool_name: str, args: Optional[dict]) -> dict:
    """Coerce Assistant/LLM stringly-typed args into schema shapes.

    LangChain frontend stubs often advertise every field as ``str``, so models
    send ``trackIndex=""`` / ``trackIndex="0"`` / ``force="false"``. Drop empty
    optionals and coerce integers/booleans/arrays before jsonschema runs.
    """
    import json

    payload = dict(args or {})
    for hidden in HIDDEN_PARAMS:
        payload.pop(hidden, None)

    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return {k: v for k, v in payload.items() if v != ""}

    props = schema.get("properties") or {}
    out: dict[str, Any] = {}

    for key, value in payload.items():
        if key not in props:
            # Keep unexpected keys so jsonschema can refuse them when
            # additionalProperties is false. Only drop empty strings.
            if value != "":
                out[key] = value
            continue

        prop = props[key] if isinstance(props[key], dict) else {}
        types = _prop_types(prop)

        if value is None or value == "":
            # Optional empty → omit (required fields still fail validation).
            continue

        if "integer" in types and not isinstance(value, bool):
            if isinstance(value, int):
                out[key] = value
                continue
            if isinstance(value, float) and value.is_integer():
                out[key] = int(value)
                continue
            if isinstance(value, str):
                text = value.strip()
                if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
                    out[key] = int(text)
                    continue
            out[key] = value
            continue

        if "number" in types and not isinstance(value, bool):
            if isinstance(value, (int, float)):
                out[key] = float(value)
                continue
            if isinstance(value, str):
                try:
                    out[key] = float(value.strip())
                    continue
                except ValueError:
                    pass
            out[key] = value
            continue

        if "boolean" in types:
            if isinstance(value, bool):
                out[key] = value
                continue
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("1", "true", "yes", "on"):
                    out[key] = True
                    continue
                if low in ("0", "false", "no", "off"):
                    out[key] = False
                    continue
            out[key] = value
            continue

        if "array" in types and isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                out[key] = parsed
                continue
            except json.JSONDecodeError:
                if "," in text:
                    parts = [p.strip() for p in text.split(",") if p.strip()]
                    if parts and all(p.isdigit() for p in parts):
                        out[key] = [int(p) for p in parts]
                        continue
            out[key] = value
            continue

        out[key] = value

    return out


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
    payload = normalize_args(tool_name, args)
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
