"""Model-facing text names only tools the server serves.

The model reads editor-tool descriptions and schemas, the editing playbook, the MCP
server instructions and the review findings, then calls the tools they name. A name
that is not served (retired, renamed or never registered) is a dead end, so every
``*_tool`` name in that text must be a tool the MCP server lists.
"""

import re
from pathlib import Path

from classes import tool_handlers  # noqa: F401  (merges the editor-tool registry into TOOL_SCHEMAS)
from classes.agent_mcp_server import SERVER_INSTRUCTIONS, iter_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS
from classes.edit_playbook import PLAYBOOK
from classes.editor_tools import REGISTRY

TOOL_NAME = re.compile(r"\b[a-z_]+_tool\b")
SRC_CLASSES = Path(__file__).resolve().parents[1] / "src" / "classes"
RAW_SOURCES = ("media_index/review.py", "editor_tools/coverage.py")
# Deprecated legacy handlers: the model must not be told to call them.
RETIRED = {
    "remove_clip_tool",
    "delete_clips_on_track_tool",
    "fetch_remotion_video_from_supabase_tool",
    "split_file_add_clip_tool",
}


def _schema_descriptions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _schema_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_descriptions(item)


def _model_facing_texts():
    for name, spec in REGISTRY.items():
        yield f"{name} ({spec.domain}) description", spec.description
    for name, schema in TOOL_SCHEMAS.items():
        for text in _schema_descriptions(schema):
            yield f"{name} schema", text
    yield "edit playbook", PLAYBOOK
    yield "MCP server instructions", SERVER_INSTRUCTIONS
    for path in RAW_SOURCES:
        lines = (SRC_CLASSES / path).read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            yield f"{path}:{lineno}", line


def test_model_facing_tool_names_are_served_over_mcp():
    served = {d["name"] for d in iter_tool_defs()} - RETIRED
    named = {}
    for where, text in _model_facing_texts():
        for name in TOOL_NAME.findall(text):
            named.setdefault(name, set()).add(where)
    assert named, "found no tool names in the model-facing text: the scan is broken"
    dangling = [f"{name}: {', '.join(sorted(where))}"
                for name, where in sorted(named.items()) if name not in served]
    assert dangling == [], "model-facing text names tools that are not served:\n" + "\n".join(dangling)
