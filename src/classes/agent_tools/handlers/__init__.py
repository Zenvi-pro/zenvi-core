"""Domain handler package for agent tools.

New filmmaker tools live here. Legacy handlers remain importable from
``classes.tool_handlers``; this package owns the registry composition so
``tool_handlers`` can stay a stable re-export surface.
"""

from classes.agent_tools.effects import add_effect
from classes.agent_tools.inspect import inspect_media, inspect_timeline
from classes.agent_tools.keyframes import set_keyframes
from classes.agent_tools.project_settings import set_project_setting
from classes.agent_tools.titles import add_title

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
