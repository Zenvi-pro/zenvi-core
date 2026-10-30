"""Domain handler package for agent tools.

New filmmaker tools live here. Legacy handlers remain importable from
``classes.tool_handlers``; this package owns the registry composition so
``tool_handlers`` can stay a stable re-export surface.
"""

from classes.agent_tools.inspect import inspect_timeline
from classes.agent_tools.speech_extra import (
    diarize_media,
    export_captions,
    remove_silence,
)
from classes.agent_tools.transcript import get_transcript, remove_words

PHASE4_HANDLERS = {
    "inspect_timeline_tool": inspect_timeline,
}

PHASE5_HANDLERS = {
    "get_transcript_tool": get_transcript,
    "remove_words_tool": remove_words,
    "remove_silence_tool": remove_silence,
    "export_captions_tool": export_captions,
    "diarize_media_tool": diarize_media,
}

PHASE4_DISPLAY_LABELS = {
    "inspect_timeline_tool": "Inspect timeline",
}

PHASE5_DISPLAY_LABELS = {
    "get_transcript_tool": "Get transcript",
    "remove_words_tool": "Remove words",
    "remove_silence_tool": "Remove silence",
    "export_captions_tool": "Export captions",
    "diarize_media_tool": "Diarize speakers",
}
