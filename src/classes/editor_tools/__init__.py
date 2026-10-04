"""Editor tools: every capability of the editor, callable by the Zenvi Assistant.

One module per workstream registers its tools with ``@editor_tool`` (see
``_registry.py``); ``coverage.py`` is the checklist of editor capabilities
each tool claims with ``covers=``. ``classes.tool_handlers`` merges the
registry into ``AGENT_TOOL_HANDLERS``; the in-app MCP server and the backend
manifest (``scripts/export_editor_tool_manifest.py``) read the same schemas.
"""

from classes.editor_tools._registry import REGISTRY, ToolSpec, coerce_args, editor_tool, prepare_args  # noqa: F401

# Workstream modules, in listing order. Importing them registers their tools.
WORKSTREAM_MODULES = (
    "project_export",
    "timeline_edit",
    "clip_props",
    "effects_color",
    "titles_text",
    "tracks_nav",
    "media_files",
    "ai_generation",
    "media_index_tools",
)

WORKSTREAM_OF_MODULE = {
    "project_export": "project-export",
    "timeline_edit": "timeline-edit",
    "clip_props": "clip-props",
    "effects_color": "effects-color",
    "titles_text": "titles-text",
    "tracks_nav": "tracks-nav",
    "media_files": "media-files",
    "ai_generation": "ai-generation",
    "media_index_tools": "media-index",
}



def workstream_of(module_name: str):
    """Workstream owning a registry module: 'timeline_edit' or 'timeline_edit_speed' -> 'timeline-edit'."""
    for prefix in sorted(WORKSTREAM_OF_MODULE, key=len, reverse=True):
        if module_name == prefix or module_name.startswith(prefix + "_"):
            return WORKSTREAM_OF_MODULE[prefix]
    return None


from classes.editor_tools import (  # noqa: E402,F401
    project_export,
    timeline_edit,
    clip_props,
    effects_color,
    titles_text,
    tracks_nav,
    media_files,
    ai_generation,
    media_index_tools,
)
