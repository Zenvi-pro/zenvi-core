"""One transition tool per job: reverse is an update_transition_tool argument, add_transitions_between_clips_tool
places what apply_transition_tool placed, and apply_transition_tool stays callable for shipped backends only."""

import copy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from classes import tool_handlers as th
from classes import transition_ops as ops
from classes.agent_mcp_server import SERVER_INSTRUCTIONS, iter_tool_defs, listed_tool_defs
from classes.agent_tools.schema import TOOL_SCHEMAS
from classes.edit_playbook import PLAYBOOK
from classes.editor_tools import REGISTRY, engine_bridge

L1, L2 = 1000000, 2000000
COVERAGE_SRC = Path(__file__).resolve().parents[1] / "src" / "classes" / "editor_tools" / "coverage.py"


@pytest.fixture
def tl(editor):
    editor.window.timeline._get_transition_reader_json = lambda path: {
        "path": path, "has_single_image": True, "type": "QtImageReader"}
    return editor


@pytest.fixture
def openshot(monkeypatch):
    """apply_transition_tool builds its keyframes with libopenshot; the fake records them."""
    fake = MagicMock()
    fake.Keyframe.return_value.Json.return_value = "{}"
    fake.QtImageReader.return_value.Json.return_value = "{}"
    monkeypatch.setitem(sys.modules, "openshot", fake)
    return fake


def _apply(**args):
    """apply_transition_tool's handler; no Qt thread hop in tests."""
    with patch.object(th, "QThread", None):
        return th.AGENT_TOOL_HANDLERS["apply_transition_tool"](**args)


def _mask(editor, position, duration, layer=L1):
    """A fade transition built the way the timeline builds one, stored in the project."""
    from classes.query import Transition
    entry, _ = ops.find_transition("fade")
    data = ops.new_mask_transition(None, {"path": entry["path"], "has_single_image": True},
                                   position=position, layer=layer, duration=duration, fps_float=30.0,
                                   title=entry["key"])
    data.pop("id")
    t = Transition()
    t.data = data
    t.save()
    editor.mark()
    return t.id


def _stored(editor, tid):
    return next(t for t in editor.get("effects") or [] if t.get("id") == tid)


def _added_on(editor, layer):
    return [t for t in editor.get("effects") or [] if t["layer"] == layer]


# ---------------------------------------------------------------------------
# reverse_transition_tool -> update_transition_tool(reverse=true)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("crossfade", [False, True], ids=["fade_on_one_clip", "crossfade_between_two"])
def test_update_reverse_writes_what_reverse_transition_tool_wrote(tl, crossfade):
    f = tl.add_file("video", duration=30.0)
    tl.add_clip(f, position=0.0, end=10.0)
    if crossfade:
        tl.add_clip(f, position=9.0, end=10.0)
    tid = _mask(tl, 9.0 if crossfade else 0.0, 1.0)
    # reverse_transition_tool saved exactly this: the timeline's reverse helper applied to the stored transition.
    expected = ops.reverse_transition_data(copy.deepcopy(_stored(tl, tid)))
    receipt = tl.call_receipt("update_transition_tool", transition_ids=[tid], reverse=True)
    assert receipt["status"] == "applied", receipt
    assert _stored(tl, tid) == expected
    assert ops.direction_of(_stored(tl, tid)) == "reversed"
    assert tl.undo_steps_since_mark() == 1


def test_update_reverse_cannot_be_combined_with_flip_wipe_side(tl):
    f = tl.add_file("video", duration=30.0)
    tl.add_clip(f, position=0.0, end=10.0)
    tid = _mask(tl, 0.0, 1.0)
    out = tl.call("update_transition_tool", transition_ids=[tid], reverse=True, flip_wipe_side=True)
    assert out.startswith("Error") and "not both" in out, out


def test_reverse_transition_tool_is_retired():
    name = "reverse_transition_tool"
    assert name not in th.AGENT_TOOL_HANDLERS
    assert name not in TOOL_SCHEMAS
    assert name not in REGISTRY
    assert name not in {d["name"] for d in iter_tool_defs()}


# ---------------------------------------------------------------------------
# apply_transition_tool -> add_transitions_between_clips_tool
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placement", ["start", "end"])
def test_add_transitions_places_an_edge_fade_like_apply_transition(tl, openshot, placement):
    f = tl.add_file("video", duration=30.0)
    old = tl.add_clip(f, position=2.0, layer=L1, end=5.0)
    new = tl.add_clip(f, position=2.0, layer=L2, end=5.0)
    assert not _apply(clip1_id=old, transition_name="fade", duration="0.5", placement=placement).startswith("Error")
    applied = tl.window.timeline.update_transition_data.call_args.args[0]
    applied_first_y = openshot.Keyframe.return_value.AddPoint.call_args_list[0].args[1]

    receipt = tl.call_receipt("add_transitions_between_clips_tool", timeline_clip_ids=[new], placement=placement,
                              transition="fade", duration_seconds=0.5)
    assert receipt["status"] == "applied", receipt
    (added,) = _added_on(tl, L2)
    assert (added["position"], added["end"], added["title"]) == (
        applied["position"], applied["end"], applied["title"])
    first = min(added["brightness"]["Points"], key=lambda p: p["co"]["X"])
    assert first["co"]["Y"] == applied_first_y  # 1.0 = a fade in, -1.0 = a fade out


def test_add_transitions_joins_an_explicit_pair_like_apply_transition(tl, openshot):
    f = tl.add_file("video", duration=30.0)
    a1 = tl.add_clip(f, position=0.0, layer=L1, end=5.0)
    b1 = tl.add_clip(f, position=5.0, layer=L1, end=5.0)
    a2 = tl.add_clip(f, position=0.0, layer=L2, end=5.0)
    b2 = tl.add_clip(f, position=5.0, layer=L2, end=5.0)
    reply = _apply(clip1_id=a1, clip2_id=b1, transition_name="fade", duration="0.5")
    assert not reply.startswith("Error"), reply
    applied = tl.window.timeline.update_transition_data.call_args.args[0]

    receipt = tl.call_receipt("add_transitions_between_clips_tool", timeline_clip_ids=[a2, b2], transition="fade",
                              duration_seconds=0.5)
    assert receipt["status"] == "applied", receipt
    (added,) = _added_on(tl, L2)
    assert tl.clip(b1)["position"] == pytest.approx(4.5) == tl.clip(b2)["position"]
    assert (added["position"], added["end"], added["title"]) == (
        applied["position"], applied["end"], applied["title"])


def test_apply_transition_tool_stays_callable_and_is_not_offered_to_models():
    name = "apply_transition_tool"
    assert name in th.AGENT_TOOL_HANDLERS and name in TOOL_SCHEMAS
    assert name in {d["name"] for d in iter_tool_defs()}  # shipped backends call it over MCP
    assert name not in {d["name"] for d in listed_tool_defs()}
    texts = [(tool, spec.description) for tool, spec in REGISTRY.items()]
    texts += [("edit playbook", PLAYBOOK), ("MCP server instructions", SERVER_INSTRUCTIONS),
              ("editor_tools/coverage.py", COVERAGE_SRC.read_text(encoding="utf-8"))]
    assert [where for where, text in texts if name in text] == []


def test_the_engine_twin_of_add_transition_is_the_listed_add_tool():
    assert engine_bridge.NATIVE_TWINS["add_transition_tool"] == "add_transitions_between_clips_tool"
