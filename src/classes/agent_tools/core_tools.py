"""The editor tools every agent sees directly; the rest are found with find_tools.

Listing 200+ tools side by side made agents pick wrong or duplicate ones and
paid for every schema on every call. The MCP server lists these, and the
backend reads them from the editor tool manifest for the built-in assistant.
"""

CORE_TOOL_NAMES = (
    "get_project_overview_tool",
    "get_timeline_state_tool",
    "list_project_files_tool",
    "search_footage_tool",
    "storyboard_tool",
    "get_transcript_tool",
    "import_files_tool",
    "add_clips_to_timeline_tool",
    "trim_clips_tool",
    "slice_clips_tool",
    "delete_from_timeline_tool",
    "remove_gaps_tool",
    "add_transitions_between_clips_tool",
    "color_grade_clip_tool",
    "add_captions_tool",
    "export_video_tool",
    "review_edit_tool",
    "inspect_timeline_tool",
    "undo_tool",
)
