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
        "detail_level": _str(
            description="summary (default) or full — accepted for Assistant/LLM compat; listing is always complete.",
        ),
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
        "ripple": _str_or_bool(),
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
    "reverse_clip_tool": _obj({
        "timeline_clip_id": _str(),
        "clip_query": _str(),
        "track": _str(),
        "occurrence": _str_or_num(),
        "mode": _str(description="'reverse' (default) or 'reset'."),
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
        "dry_run": _str_or_bool(description="true/false — preview without writing."),
    }),
    "zoom_in_tool": _obj({}),
    "zoom_out_tool": _obj({}),
    "center_on_playhead_tool": _obj({}),
    "import_files_tool": _obj({
        "paths": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                  "description": "List of paths, or one comma-separated string."},
        "files": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                  "description": "Alias of paths."},
        "path": _str(),
        "folder": _str(),
        "skip_indexing": _str_or_bool(),
        "dry_run": _str_or_bool(description="true previews what would be imported; nothing changes."),
        "media_types": _str(description="all (default), video, audio, image, or a comma list."),
    }),
    "wait_until_project_indexed_tool": _obj({
        "timeout_seconds": _str_or_num(),
    }),
    "export_video_tool": _obj({
        "show_dialog": _str_or_bool(),
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
        "full_file": _str_or_bool(),
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
    "ingest_web_video_tool": _obj({
        "url": _str(description="YouTube / web page URL (preferred)."),
        "video_url": _str(description="Alias for url."),
        "intent": _str(
            description="'reference' (default, media bin), 'timeline' (place), or 'recreate'.",
        ),
        "place": _str_or_bool(description="Place on timeline; ignored for recreate."),
        "track": _str_or_num(),
        "position_seconds": _str_or_num(),
        "write_subs": _str_or_bool(),
        "job_id": _str(description="Long-poll id from a prior status=running reply."),
    }),
    "apply_color_tool": _obj({
        "clipIds": _str(description="JSON list, comma-separated ids, or 'all'."),
        "clip_ids": _str(),
        "timeline_clip_id": _str(),
        "clipId": _str(),
        "all_clips": _str_or_bool(),
        "allClips": _str_or_bool(),
        "reset": _str_or_bool(description="true removes ColorGrade."),
        "color": _str(description="Full grade object JSON to paste."),
        "exposure": _str_or_num(),
        "contrast": _str_or_num(),
        "saturation": _str_or_num(),
        "vibrance": _str_or_num(),
        "temperature": _str_or_num(),
        "tint": _str_or_num(),
        "temperature_delta": _str_or_num(),
        "tint_delta": _str_or_num(),
        "exposure_delta": _str_or_num(),
        "vibrance_delta": _str_or_num(),
        "highlights": _str_or_num(),
        "shadows": _str_or_num(),
        "mix": _str_or_num(),
        "lut_path": _str(),
        "lut_intensity": _str_or_num(),
        "lut": _str(),
        "wheels": _str(description="Wheels JSON."),
        "curve_all": _str(),
        "curve_red": _str(),
        "curve_green": _str(),
        "curve_blue": _str(),
        "masterCurve": _str(),
        "redCurve": _str(),
        "greenCurve": _str(),
        "blueCurve": _str(),
    }),
    "inspect_color_tool": _obj({
        "clipId": _str(),
        "timeline_clip_id": _str(),
        "clip_id": _str(),
        "atFrame": _str_or_num(),
        "at_frame": _str_or_num(),
        "reference": _str(),
        "referenceClipId": _str(),
        "include_preview": _str_or_bool(),
    }),
    "list_looks_tool": _obj({
        "query": _str(),
        "vibe": _str(),
    }),
    "apply_look_tool": _obj({
        "clipIds": _str(description="JSON list, comma-separated ids, or 'all'."),
        "clip_ids": _str(),
        "timeline_clip_id": _str(),
        "clipId": _str(),
        "lookId": _str(description="Id from list_looks_tool."),
        "look_id": _str(),
        "lutPath": _str(),
        "lut_path": _str(),
        "lutIntensity": _str_or_num(),
        "lut_intensity": _str_or_num(),
        "mix": _str_or_num(),
        "stackGrain": _str_or_bool(),
        "stack_grain": _str_or_bool(),
        "grainPreset": _str(),
        "grain_preset": _str(),
        "all_clips": _str_or_bool(),
        "allClips": _str_or_bool(),
    }),
    "match_color_to_reference_tool": _obj({
        "clipId": _str(),
        "timeline_clip_id": _str(),
        "clip_id": _str(),
        "clipIds": _str(),
        "clip_ids": _str(),
        "reference": _str(),
        "referenceClipId": _str(),
        "referenceFileId": _str(),
        "referenceImagePath": _str(),
        "all_clips": _str_or_bool(),
        "allClips": _str_or_bool(),
        "dry_run": _str_or_bool(),
        "dryRun": _str_or_bool(),
        "max_iterations": _str_or_num(),
    }),
    "slice_clip_at_playhead_tool": _obj({}),
    "search_clips_tool": _obj({
        "query": _str(),
        "top_k": _str_or_num(),
        "look_for": _str(description="'on_screen' (what is visible), 'spoken' (what is said); omit for both."),
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
        "start_seconds": _str_or_num(description="Known cut time in source seconds."),
        "end_seconds": _str_or_num(description="End of a known range in source seconds."),
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
        "detail_level": _str(description="summary (default) or full."),
    }),
    "get_timeline_placements_metadata_tool": _obj({
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
        "modelId": _str(description="faster-whisper model, e.g. faster-whisper-base; ignored by the bundled whisper.cpp engine (one model)."),
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
        "modelId": _str(description="faster-whisper model, e.g. faster-whisper-base; ignored by the bundled whisper.cpp engine."),
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
        "matches": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exact spoken tokens to cut (case/punct insensitive), e.g. "
                "[\"FlowCut\", \"flocut\", \"um\"]. Prefer this when removing a "
                "brand/name/phrase by text. Do NOT delete the clip from the timeline."
            ),
        },
        "fillerPreset": _str(
            description="Built-in preset: um_uh or english_fillers.",
        ),
        "transcriptGeneration": _int(
            minimum=1,
            description=(
                "Pin from get_transcript when using wordIndices. Optional for "
                "matches/fillerPreset cuts (stale pins are ignored)."
            ),
        ),
        "language": _str(
            description="Prefer 'auto'. Do not pass detected language from a prior receipt.",
        ),
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
        "engine": _str(
            description="auto|apple|whisper — same on-device ASR as get_transcript_tool (never cloud).",
        ),
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
