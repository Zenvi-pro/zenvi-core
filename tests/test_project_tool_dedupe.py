"""One tool per job, for the three duplicate pairs the family audit flagged.

import_timeline_xml_tool -> import_project_file_tool: one XML import path; sequence and placement are optional there.
list_recent_projects_tool -> open_project_tool with neither file_path nor recent_index: lists the recent projects.
rerender_linked_clip_tool -> update_linked_clip_tool: re-renders Zenvi Motion / 3D / FX links as well.

A retired name is gone from the registry, the handler table, the schemas, the MCP listing and the engine bridge.
Its replacement does the retired tool's job for the same input.
"""

import pathlib
import re

import pytest

from classes import tool_handlers
from classes.agent_mcp_server import iter_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS
from classes.editor_tools import REGISTRY, engine_links
from classes.handoff import linked_media as lm
from handoff_fakes import FakeProvider, linked, remotion_link, tt  # noqa: F401  (fixtures)
from test_editor_tools_handoff_premiere import ppro, speed_xml  # noqa: F401  (fixtures: Premiere-ready editor, 3-clip XML)
from test_editor_tools_project_export import settings  # noqa: F401  (fixture: the recent-projects settings)
from test_engine_bridge import _calls, bridge  # noqa: F401  (fixture: the fake engine CLI as the bridge)
from test_engine_bridge_tools import _link

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "premiere"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "classes"
RETIRED_TO_REPLACEMENT = {
    "import_timeline_xml_tool": "import_project_file_tool",
    "list_recent_projects_tool": "open_project_tool",
    "rerender_linked_clip_tool": "update_linked_clip_tool",
}
# The modules this change edits; none may name a retired tool any more.
EDITED = (
    "editor_tools/project_export.py",
    "editor_tools/handoff_premiere.py",
    "editor_tools/handoff.py",
    "editor_tools/engine_links.py",
)


@pytest.fixture
def provider(linked):  # noqa: F811
    p = FakeProvider(probe=linked.probe)
    lm.register_provider(p)
    return p


def _two_sequences(xml_path, tmp_path):
    """The speed sequence twice in one XML: the first is "Speeds", the second "Director's cut"."""
    text = pathlib.Path(xml_path).read_text()
    head, body = text.split('<xmeml version="4">', 1)[0], text.split('<xmeml version="4">', 1)[1]
    body = body.rsplit("</xmeml>", 1)[0]
    second = body.replace("<name>Speeds</name>", "<name>Director's cut</name>", 1).replace('id="', 'id="dc-')
    two = tmp_path / "Two.xml"
    two.write_text(head + '<xmeml version="4"><project><children>' + body + second + "</children></project></xmeml>")
    return str(two)


# --- one tool per job -------------------------------------------------------------------------------------------

def test_retired_names_are_gone_from_every_surface():
    served = {d["name"] for d in iter_tool_defs()}
    for old in RETIRED_TO_REPLACEMENT:
        assert old not in REGISTRY and old not in tool_handlers.AGENT_TOOL_HANDLERS, old
        assert old not in TOOL_SCHEMAS and old not in served, old
    assert set(RETIRED_TO_REPLACEMENT.values()) <= served


def test_the_engine_bridge_does_not_offer_the_retired_rerender():
    engine_tools = {"update_motion_block_tool", "rerender_3d_scene_tool", "update_fx_tool"}
    offered = {tool["name"] for tool, _run in engine_links.desktop_tools(engine_tools, set())}
    assert "rerender_linked_clip_tool" not in offered


def test_edited_modules_name_no_retired_tool():
    for rel in EDITED:
        text = (SRC / rel).read_text(encoding="utf-8")
        hits = [old for old in RETIRED_TO_REPLACEMENT if re.search(r"\b%s\b" % old, text)]
        assert not hits, (rel, hits)


# --- import_timeline_xml_tool -> import_project_file_tool -----------------------------------------------------

def test_import_project_file_imports_a_premiere_sequence_as_one_undo_step(ppro, speed_xml):  # noqa: F811
    ppro.mark()
    r = ppro.call_receipt("import_project_file_tool", file_path=speed_xml)
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    data = r["data"]
    assert len(data["timeline_clip_ids"]) == 3 and len(data["layers"]) == 1 and data["missing_media"] == []
    assert data["sequence_name"] == "Speeds" and "Imported 3 clip(s)" in r["summary"]
    assert ppro.undo_steps_since_mark() == 1
    ppro.undo()
    assert not [c for c in ppro.clips() if c["id"] in data["timeline_clip_ids"]]


def test_import_project_file_takes_placement_and_sequence(ppro, speed_xml, tmp_path):  # noqa: F811
    ppro.window.preview_thread.player.Position.return_value = 301      # 10 s at 30 fps
    r = ppro.call_receipt("import_project_file_tool", file_path=speed_xml, placement="at_playhead")
    assert r["status"] == "applied" and r["data"]["offset"] == pytest.approx(10.0), r["summary"]
    first = min(float(c["position"]) for c in ppro.clips() if c["id"] in r["data"]["timeline_clip_ids"])
    assert first == pytest.approx(10.0)

    two = _two_sequences(speed_xml, tmp_path)
    r = ppro.call_receipt("import_project_file_tool", file_path=two)
    assert r["status"] == "applied" and r["data"]["sequence_name"] == "Speeds", r["summary"]
    assert any("Director's cut" in w for w in r["data"]["warnings"])
    r = ppro.call_receipt("import_project_file_tool", file_path=two, sequence="director's cut")
    assert r["status"] == "applied" and r["data"]["sequence_name"] == "Director's cut", r["summary"]
    r = ppro.call_receipt("import_project_file_tool", file_path=two, sequence="Nope")
    assert r["status"] == "error" and r["undoSteps"] == 0 and "no sequence named 'Nope'" in r["summary"]


def test_import_project_file_refusals_leave_history_alone(ppro, tmp_path):  # noqa: F811
    ppro.mark()
    r = ppro.call_receipt("import_project_file_tool", file_path=str(tmp_path / "nope.xml"))
    assert r["status"] == "error" and "file not found" in r["summary"]
    junk = tmp_path / "junk.xml"
    junk.write_text("<fcpxml version='1.9'/>")
    r = ppro.call_receipt("import_project_file_tool", file_path=str(junk))
    assert r["status"] == "error" and "not a Final Cut Pro 7" in r["summary"]
    lost = tmp_path / "lost.xml"
    lost.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/", "/nowhere/at/all/"))
    r = ppro.call_receipt("import_project_file_tool", file_path=str(lost))
    assert r["status"] == "error" and "missing media clip.mp4" in r["summary"]
    r = ppro.call_receipt("import_project_file_tool", file_path=str(junk), placement="sideways")
    assert r["status"] in ("error", "refused") and "must be one of" in r["summary"]
    edl = tmp_path / "cut.edl"
    edl.write_text("TITLE: cut\n")
    r = ppro.call_receipt("import_project_file_tool", file_path=str(edl), placement="at_playhead")
    assert r["status"] in ("error", "refused") and "XML only" in r["summary"]
    assert ppro.undo_steps_since_mark() == 0


# --- list_recent_projects_tool -> open_project_tool with no arguments ------------------------------------------

def test_open_project_without_arguments_lists_the_recent_projects(editor, settings, tmp_path):  # noqa: F811
    kept = tmp_path / "kept.zvn"
    kept.write_text("{}")
    gone = str(tmp_path / "gone.zvn")
    settings.values["recent_projects"] = [gone, str(kept)]    # stored oldest first; listed most recent first
    editor.store.has_unsaved_changes = True                    # listing asks nothing about unsaved work
    r = editor.call_receipt("open_project_tool")
    assert r["status"] == "applied" and r["undoSteps"] == 0, r["summary"]
    projects = r["data"]["projects"]
    assert [(p["index"], p["name"], p["exists"]) for p in projects] == [(1, "kept", True), (2, "gone", False)]
    assert projects[0]["path"] == str(kept) and projects[0]["modified"] and projects[1]["modified"] is None
    assert r["summary"].startswith("2 recent project(s); most recent: kept.")
    editor.window.open_project.assert_not_called()


def test_open_project_listing_says_when_nothing_is_recent(editor, settings):  # noqa: F811
    settings.values["recent_projects"] = []
    r = editor.call_receipt("open_project_tool")
    assert r["status"] == "applied" and r["data"]["projects"] == [] and r["summary"] == "No recent projects."


# --- rerender_linked_clip_tool -> update_linked_clip_tool -----------------------------------------------------

def test_update_linked_clip_re_renders_a_motion_block_through_its_engine_tool(bridge, tmp_path):  # noqa: F811
    f = bridge.add_file("video", path=str(tmp_path / "counter.mov"), zenvi_link=_link("zenvi-motion"))
    bridge.add_clip(f)
    r = bridge.call_receipt("update_linked_clip_tool", file_id=f)
    assert r["status"] == "applied", r["summary"]
    assert bridge.file(f)["path"].endswith("counter.mov.v2.mov")
    assert _calls(bridge.log)[-1]["tool"] == "update_motion_block_tool"
    assert _calls(bridge.log)[-1]["args"] == {"file_id": f}
    assert bridge.undo_steps_since_mark() == 1


def test_update_linked_clip_re_renders_an_fx_clip_with_its_whole_stack(bridge, tmp_path):  # noqa: F811
    effects = [{"effect": "radial_blur", "start": 0, "end": 0.3}, {"effect": "bloom"}]
    f = bridge.add_file("video", path=str(tmp_path / "fx.mov"), zenvi_link=_link("zenvi-fx", props={"effects": effects}))
    c = bridge.add_clip(f)
    assert bridge.call_receipt("update_linked_clip_tool", file_id=f)["status"] == "applied"
    assert _calls(bridge.log)[-1]["args"] == {"clip_id": c, "effects": effects}
    assert bridge.undo_steps_since_mark() == 1


def test_update_linked_clip_refuses_engine_props_and_no_op_rerenders(bridge, tmp_path):  # noqa: F811
    block = bridge.add_file("video", path=str(tmp_path / "counter.mov"), zenvi_link=_link("zenvi-motion"))
    r = bridge.call_receipt("update_linked_clip_tool", file_id=block, props={"title": "x"})
    assert r["status"] in ("error", "refused") and "update_motion_block_tool" in r["summary"]
    r = bridge.call_receipt("update_linked_clip_tool", file_id=block, rerender=False)
    assert r["status"] in ("error", "refused") and "nothing to change" in r["summary"]
    assert bridge.undo_steps_since_mark() == 0


def test_update_linked_clip_re_renders_a_remotion_clip_as_the_retired_tool_did(linked, provider):  # noqa: F811
    out = lm.import_linked(dict(remotion_link()), position=0.0)
    linked.mark()
    provider.version = 2                                      # the source changed: the clip is stale
    r = linked.call_receipt("update_linked_clip_tool", file_id=out["file_id"])
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    assert len(provider.renders) == 2 and provider.renders[-1]["props"] == {"title": "Hello"}
    assert lm.link_state(linked.file(out["file_id"])) == "fresh"
