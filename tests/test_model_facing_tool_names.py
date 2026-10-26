"""Model-facing text names only tools the server serves.

The model reads editor-tool descriptions and schemas, the editing playbook, the MCP
server instructions and the review findings, then calls the tools they name. A name
that is not served (retired, renamed or never registered) is a dead end, so every
``*_tool`` name in that text must be a tool the MCP server lists.
"""

import re
from pathlib import Path

from classes import tool_handlers  # noqa: F401  (merges the editor-tool registry into TOOL_SCHEMAS)
from classes.agent_mcp_server import SERVER_INSTRUCTIONS, iter_tool_defs, listed_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS
from classes.edit_playbook import PLAYBOOK
from classes.editor_tools import REGISTRY

TOOL_NAME = re.compile(r"\b[a-z_]+_tool\b")
RUN_TOOL_CALL = re.compile(r"""run_tool\(name=["']?[a-z_]+_tool""")
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
    served = {d["name"] for d in listed_tool_defs() + iter_tool_defs()} - RETIRED
    named = {}
    for where, text in _model_facing_texts():
        for name in TOOL_NAME.findall(text):
            named.setdefault(name, set()).add(where)
    assert named, "found no tool names in the model-facing text: the scan is broken"
    dangling = [f"{name}: {', '.join(sorted(where))}"
                for name, where in sorted(named.items()) if name not in served]
    assert dangling == [], "model-facing text names tools that are not served:\n" + "\n".join(dangling)


def test_text_sent_with_every_call_names_only_listed_tools():
    """The instructions, playbook and listed tools' descriptions reach the model on every call, next to the
    callable names. An unlisted name there reads as callable and fails (add_title_tool looped that way);
    such tools are described in words and reached with find_tools / run_tool."""
    listed_defs = listed_tool_defs()
    listed = {d["name"] for d in listed_defs}
    texts = [("edit playbook", PLAYBOOK), ("MCP server instructions", SERVER_INSTRUCTIONS)]
    texts += [(f"{d['name']} description", d["description"]) for d in listed_defs]
    texts += [(f"{d['name']} schema", t) for d in listed_defs for t in _schema_descriptions(d["inputSchema"])]
    # run_tool(name="x_tool") is how an unlisted tool is written; the bare name is what reads as callable.
    texts = [(where, RUN_TOOL_CALL.sub("", text)) for where, text in texts]
    stray = sorted({f"{name}: {where}" for where, text in texts for name in TOOL_NAME.findall(text) if name not in listed})
    assert stray == [], "unlisted tools named in per-call text:\n" + "\n".join(stray)


def test_the_playbook_says_how_to_reach_unlisted_tools():
    assert "find_tools" in PLAYBOOK and "run_tool" in PLAYBOOK
    assert "color_grade_clip_tool" in PLAYBOOK  # the one creative-grade path


def test_model_facing_text_never_names_the_backend_only_stock_music_workflow():
    """stock_music is a Zenvi Assistant workflow; Claude Code and Codex over MCP have no such tool.
    Stock music is imported with import_stock_media_tool and placed with add_clips_to_timeline_tool."""
    texts = list(_model_facing_texts())
    texts.append(("tool_handlers.py", (SRC_CLASSES / "tool_handlers.py").read_text(encoding="utf-8")))
    found = sorted({where for where, text in texts if re.search(r"\bstock_music\b", text)})
    assert found == [], "stock_music is named in: " + ", ".join(found)
    # The call the model copies must run: JSON args with the source, and the phrase point as the in-point.
    audition = REGISTRY["audition_music_tool"].description
    for where, text in (("edit playbook", PLAYBOOK), ("audition_music_tool", audition)):
        assert 'run_tool(name="import_stock_media_tool", args={"source": "freesound", "sound_id"' in text, where
        assert "add_clips_to_timeline_tool" in text and "source_start" in text, where


def test_a_stock_import_receipt_points_at_the_listed_placement_tool():
    text = (SRC_CLASSES / "tool_handlers.py").read_text(encoding="utf-8")
    assert "Call add_clip_to_timeline_tool with file_id=" not in text


def test_retired_handlers_are_not_served_over_mcp():
    from classes.tool_handlers import AGENT_TOOL_HANDLERS
    served = {d["name"] for d in iter_tool_defs()}
    assert not RETIRED & set(AGENT_TOOL_HANDLERS), sorted(RETIRED & set(AGENT_TOOL_HANDLERS))
    assert not RETIRED & served, sorted(RETIRED & served)
