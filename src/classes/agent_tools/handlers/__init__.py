"""Domain handler package for agent tools.

New filmmaker tools live here. Legacy handlers remain importable from
``classes.tool_handlers``; this package owns the registry composition so
``tool_handlers`` can stay a stable re-export surface.
"""

from classes.agent_tools.effects import add_effect
from classes.agent_tools.inspect import inspect_media, inspect_timeline
from classes.agent_tools.keyframes import set_keyframes
from classes.agent_tools.project_settings import set_project_setting
from classes.agent_tools.speech_extra import (
    add_captions,
    detect_beats_tool_handler,
    diarize_media,
    export_captions,
    remove_silence,
    search_media_local,
)
from classes.agent_tools.titles import add_title
from classes.agent_tools.transcript import get_transcript, remove_words, transcribe_media

PHASE3_HANDLERS = {
    "add_effect_tool": add_effect,
    "add_title_tool": add_title,
    "set_keyframes_tool": set_keyframes,
    "set_project_setting_tool": set_project_setting,
}

PHASE4_HANDLERS = {
    "inspect_timeline_tool": inspect_timeline,
    "inspect_media_tool": inspect_media,
}

def _audit_wrap(name, fn):
    def _wrapped(*args, **kwargs):
        from classes.speech.audit import audit, audit_receipt, enabled
        if enabled():
            audit("tool_call", tool=name, args={k: kwargs.get(k) for k in kwargs if k not in ("chat_session_id", "transaction_id")})
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:
            if enabled():
                audit("tool_exception", tool=name, error=str(exc))
            raise
        if isinstance(out, str):
            return audit_receipt(name, out)
        return out
    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    return _wrapped


PHASE5_HANDLERS = {
    "get_transcript_tool": _audit_wrap("get_transcript_tool", get_transcript),
    "remove_words_tool": _audit_wrap("remove_words_tool", remove_words),
    "transcribe_media_tool": _audit_wrap("transcribe_media_tool", transcribe_media),
    "remove_silence_tool": _audit_wrap("remove_silence_tool", remove_silence),
    "add_captions_tool": _audit_wrap("add_captions_tool", add_captions),
    "export_captions_tool": _audit_wrap("export_captions_tool", export_captions),
    "detect_beats_tool": _audit_wrap("detect_beats_tool", detect_beats_tool_handler),
    "diarize_media_tool": _audit_wrap("diarize_media_tool", diarize_media),
    "search_media_local_tool": _audit_wrap("search_media_local_tool", search_media_local),
}

PHASE3_DISPLAY_LABELS = {
    "add_effect_tool": "Add effect",
    "add_title_tool": "Add title",
    "set_keyframes_tool": "Set keyframes",
    "set_project_setting_tool": "Set project setting",
}

PHASE4_DISPLAY_LABELS = {
    "inspect_timeline_tool": "Inspect timeline",
    "inspect_media_tool": "Inspect media",
}

PHASE5_DISPLAY_LABELS = {
    "get_transcript_tool": "Get transcript",
    "remove_words_tool": "Remove words",
    "transcribe_media_tool": "Transcribe media",
    "remove_silence_tool": "Remove silence",
    "add_captions_tool": "Add captions",
    "export_captions_tool": "Export captions",
    "detect_beats_tool": "Detect beats",
    "diarize_media_tool": "Diarize speakers",
    "search_media_local_tool": "Search media (local)",
}
