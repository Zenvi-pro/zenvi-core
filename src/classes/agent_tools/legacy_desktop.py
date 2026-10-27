"""Shipped backends still call get_clips_with_full_metadata_tool.

This editor answers from the local index. New chats do not advertise the name.
"""

from __future__ import annotations


def get_clips_with_full_metadata(detail_level="summary", **kwargs) -> str:
    """Project files and what the local index saved about them."""
    del detail_level, kwargs
    from classes.editor_tools.media_index_tools_review import get_project_overview

    try:
        return get_project_overview()
    except Exception as exc:
        return f"Error: {exc}"


LEGACY_HANDLERS = {
    "get_clips_with_full_metadata_tool": get_clips_with_full_metadata,
}

LEGACY_LABELS = {
    "get_clips_with_full_metadata_tool": "Read clips metadata",
}

LEGACY_SCHEMAS = {
    "get_clips_with_full_metadata_tool": {
        "type": "object",
        "properties": {
            "detail_level": {"type": "string", "enum": ["summary", "full"]},
        },
        "additionalProperties": False,
    },
}
