"""apply_clip_preset_tool: every clip-menu preset reaches the menu's own handler, in one undo step."""

import json
import os
import re

import pytest

from classes.editor_tools import clip_props_presets as presets

TIMELINE_PY = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "timeline.py")


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


def curve(y):
    return {"Points": [{"co": {"X": 1.0, "Y": float(y)}, "interpolation": 0}]}


class FakeTimeline:
    """Stands in for TimelineView: records calls and writes like the real handlers do.

    Fade/Animate/Volume take a transaction id and save each clip through
    update_clip_data, whose slot_transaction clears the id afterwards (so the
    ambient id is gone when they return). Rotate/Layout/Crop save without one.
    """

    def __init__(self, editor, write=True):
        self.editor = editor
        self.calls = []
        self.write = write

    def _save(self, cid, values, tid=None):
        if not self.write:
            return
        updates = self.editor.app.updates
        if tid:
            updates.transaction_id = tid
        updates.update(["clips", {"id": cid}], values)
        if tid:
            updates.transaction_id = None

    def Fade_Triggered(self, action, clip_ids, position="Entire Clip", transaction_id=None, fade_seconds=None):
        self.calls.append(("fade", action.name, list(clip_ids), position, fade_seconds, transaction_id))
        for cid in clip_ids:
            self._save(cid, {"alpha": curve(0.0)}, transaction_id or "fade-own")

    def Animate_Triggered(self, action, clip_ids, transaction_id=None, zone_seconds=None, emphasis_seconds=None):
        self.calls.append(("animate", action.name, list(clip_ids), zone_seconds, emphasis_seconds, transaction_id))
        for cid in clip_ids:
            self._save(cid, {"scale_x": curve(1.2), "effects": [{"id": "FX" + cid, "class_name": "Blur"}]},
                       transaction_id or "animate-own")

    def Volume_Triggered(self, action, clip_ids, position="Entire Clip", level=1.0, transaction_id=None,
                         fade_seconds=None):
        self.calls.append(("volume", action.name, list(clip_ids), position, level, fade_seconds, transaction_id))
        for cid in clip_ids:
            self._save(cid, {"volume": curve(level / 100.0 if action.name == "LEVEL" else 0.0)},
                       transaction_id or "volume-own")

    def Rotate_Triggered(self, action, clip_ids, position="Start of Clip"):
        self.calls.append(("rotate", action.name, list(clip_ids)))
        angle = {"NONE": 0.0, "RIGHT_90": 90.0, "LEFT_90": -90.0, "FLIP_180": 180.0}[action.name]
        for cid in clip_ids:
            self._save(cid, {"rotation": curve(angle)})

    def Layout_Triggered(self, action, clip_ids):
        self.calls.append(("layout", action.name, list(clip_ids)))
        for cid in clip_ids:
            self._save(cid, {"gravity": 2, "scale_x": curve(0.5)})

    def Crop_Triggered(self, clip_ids, mode):
        self.calls.append(("crop", mode, list(clip_ids)))
        for cid in clip_ids:
            self._save(cid, {"effects": [{"id": "CROP" + cid, "class_name": "Crop"}]})

    def No_Transform_Triggered(self, clip_ids):
        # The real one mints its own id and clears it (before this workstream's fix).
        self.calls.append(("no_transform", list(clip_ids)))
        updates = self.editor.app.updates
        updates.transaction_id = "no-transform-own"
        for cid in clip_ids:
            self._save(cid, {"rotation": curve(0.0), "gravity": 4})
        updates.transaction_id = None


@pytest.fixture
def timeline(editor):
    tl = FakeTimeline(editor)
    editor.window.timeline = tl
    editor.window.preview_thread.current_frame = 1
    return tl


# ---------------------------------------------------------------------------
# The preset table is the clip menu
# ---------------------------------------------------------------------------

def _menu_source():
    with open(TIMELINE_PY, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("    def ShowClipMenu(")
    return src[start:src.index("\n    def ", start + 10)]


def test_every_motion_preset_is_the_menu_item_it_names():
    src = _menu_source()
    items = {}
    for title, body in re.findall(r'_motion_sub\(_\("([^"]+)"\), \[(.*?)\]\)', src, re.S):
        for label, member in re.findall(r'\(_\("([^"]+)"\),\s*MenuAnimate\.(\w+)\)', body):
            items[(title, label)] = member
    for _menu, label, member in re.findall(r'_motion_act\((\w+), _\("([^"]+)"\),\s*MenuAnimate\.(\w+)\)', src):
        items[("", label)] = member
    items[("", "No Motion")] = "NONE"
    motion = [p for p in presets.PRESETS.values() if p.call == "animate"]
    assert len(motion) == len(items) == 87
    for p in motion:
        parts = p.menu.split(" > ")
        label = re.sub(r" \(.*\)$", "", parts[-1])
        title = parts[-2] if len(parts) > 2 and (parts[-2], label) in items else ""
        assert items.get((title, label)) == p.args[0], (p.name, p.menu)


def test_fade_volume_rotate_crop_layout_presets_match_the_menu():
    src = _menu_source()
    fades = set(re.findall(r'partial\(self\.Fade_Triggered, MenuFade\.(\w+), clip_ids(?:, "([^"]+)")?\)', src))
    volumes = set(re.findall(r'partial\(self\.Volume_Triggered, MenuVolume\.(\w+), clip_ids(?:, "([^"]+)")?', src))
    rotates = set(re.findall(r'self\.Rotate_Triggered, MenuRotate\.(\w+), clip_ids', src))
    crops = set(re.findall(r"self\.Crop_Triggered, clip_ids, '(\w+)'", src))
    layouts = set(re.findall(r'self\.Layout_Triggered, MenuLayout\.(\w+), clip_ids', src))
    table = presets.PRESETS.values()
    assert {(p.args[0], p.args[1] if p.args[1] != "Entire Clip" or p.args[0] != "NONE" else "")
            for p in table if p.call == "fade"} == fades
    assert {(p.args[0], p.args[1] if p.args[0] != "NONE" else "") for p in table if p.call == "volume"} == volumes
    assert {p.args[0] for p in table if p.call == "rotate"} == rotates
    assert {p.args[0] for p in table if p.call == "crop"} == crops
    assert {p.args[0] for p in table if p.call == "layout"} == layouts
    assert "No_Transform_Triggered" in src


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", presets.PRESET_NAMES)
def test_each_preset_reaches_its_handler_in_one_undo_step(editor, timeline, name):
    spec = presets.PRESETS[name]
    f = editor.add_file("video", has_audio=True)
    c = editor.add_clip(f, end=10.0)
    args = {"level_percent": 60} if name == "volume_level" else {}
    out = editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset=name, **args)
    if name in ("fade_none", "volume_reset"):
        assert "Nothing to change" in out and not timeline.calls  # already so: the handler is not even called
        assert editor.undo_steps_since_mark() == 0
        return
    r = receipt(out)
    if spec.call == "flip":  # mirrors in the tool itself; there is no menu handler
        assert not timeline.calls and r["changed"] is True and editor.undo_steps_since_mark() == 1
        return
    assert timeline.calls, name
    call = timeline.calls[0]
    assert call[0] == spec.call
    if spec.call in ("fade", "animate", "volume"):
        assert call[-1] is not None  # the tool's own transaction id is handed in
    assert editor.undo_steps_since_mark() == (1 if r.get("changed") else 0)


def test_fade_duration_and_emphasis_time_reach_the_handlers(editor, timeline):
    c = editor.add_clip(editor.add_file("video"), position=5.0, end=10.0)
    r = receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="fade_in_out",
                            duration_seconds=2))
    assert timeline.calls[-1][:5] == ("fade", "IN_OUT_FAST", [c], "Entire Clip", 2.0)
    assert r["fade_seconds"] == 2.0
    r = receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="tada", at_seconds=8.0,
                            duration_seconds=0.5))
    assert timeline.calls[-1][:5] == ("animate", "TADA", [c], 0.5, 8.0)
    assert r["emphasis_starts_at"] == {c: 8.0} and r["zone_seconds"] == 0.5


def test_volume_level_passes_the_percent(editor, timeline):
    c = editor.add_clip(editor.add_file("audio"))
    receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="volume_level", level_percent=60))
    assert timeline.calls[-1][:5] == ("volume", "LEVEL", [c], "Entire Clip", 60.0)
    assert editor.clip(c)["volume"]["Points"][0]["co"]["Y"] == pytest.approx(0.6)


@pytest.mark.parametrize("args,fragment", [
    ({"preset": "zoom_in", "duration_seconds": 3}, "whole clip"),
    ({"preset": "rotate_90_right", "duration_seconds": 3}, "no length"),
    ({"preset": "zoom_in", "at_seconds": 1}, "Emphasis"),
    ({"preset": "volume_level"}, "needs level_percent"),
    ({"preset": "fade_in", "level_percent": 50}, "only goes with"),
    ({"preset": "wobble", "at_seconds": 99}, "not over any"),
    ({"preset": "sparkle"}, "must be one of"),
])
def test_preset_argument_refusals(editor, timeline, args, fragment):
    c = editor.add_clip(editor.add_file("video"), end=10.0)
    out = editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], **args)
    assert out.startswith("Error") and fragment in out, out
    assert not timeline.calls and editor.undo_steps_since_mark() == 0


def test_presets_skip_clips_they_do_not_apply_to(editor, timeline):
    video = editor.add_clip(editor.add_file("video"), end=10.0)
    audio = editor.add_clip(editor.add_file("audio"), position=20.0)
    r = receipt(editor.call("apply_clip_preset_tool", scope="all", preset="zoom_in"))
    assert timeline.calls[-1][2] == [video]
    assert r["skipped"] == [{"timeline_clip_id": audio, "reason": "audio-only clip (no picture to move or transform)"}]
    r = receipt(editor.call("apply_clip_preset_tool", scope="all", preset="volume_fade_out"))
    assert timeline.calls[-1][2] == [audio]  # the silent video is skipped
    out = editor.call("apply_clip_preset_tool", timeline_clip_ids=[audio], preset="ken_burns_in")
    assert out.startswith("Error") and "does not apply" in out


def test_fades_apply_to_every_kind_of_clip(editor, timeline):
    a = editor.add_clip(editor.add_file("video"), end=10.0)
    b = editor.add_clip(editor.add_file("audio"), position=20.0)
    i = editor.add_clip(editor.add_file("image"), position=40.0)
    receipt(editor.call("apply_clip_preset_tool", scope="all", preset="fade_in"))
    assert sorted(timeline.calls[-1][2]) == sorted([a, b, i])


def test_locked_clips_refused_by_id_skipped_by_scope(editor, timeline):
    f = editor.add_file("video")
    editor.add_track(2000000, "Top")
    a = editor.add_clip(f, end=10.0)
    b = editor.add_clip(f, end=10.0, layer=2000000)
    editor.lock_track(2000000)
    out = editor.call("apply_clip_preset_tool", timeline_clip_ids=[b], preset="rotate_90_left")
    assert out.startswith("Error") and "locked" in out and not timeline.calls
    r = receipt(editor.call("apply_clip_preset_tool", scope="all", preset="rotate_90_left"))
    assert timeline.calls[-1] == ("rotate", "LEFT_90", [a]) and r["skipped"][0]["timeline_clip_id"] == b


def test_a_handler_that_clears_the_id_still_gives_one_undo_step(editor, timeline):
    """No Transform minted and cleared its own id: the tool call must still be ONE step, and the
    ambient transaction must survive for anything after it in the same group."""
    from classes import tool_handlers

    f = editor.add_file("video")
    a, b = editor.add_clip(f, end=10.0, rotation=curve(90.0)), editor.add_clip(f, position=20.0, end=10.0)
    with tool_handlers._transaction(editor.app) as outer:
        receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[a, b], preset="fade_in"))
        assert editor.app.updates.transaction_id == outer
        receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[a], preset="rotate_none"))
        assert editor.app.updates.transaction_id == outer
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(a)["alpha"]["Points"][0]["co"]["Y"] == 1.0
    assert editor.clip(a)["rotation"]["Points"][0]["co"]["Y"] == 90.0


def test_the_receipt_lists_changed_properties_and_new_effects(editor, timeline):
    c = editor.add_clip(editor.add_file("video"), end=10.0)
    r = receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="crop_resize"))
    assert timeline.calls[-1] == ("crop", "resize", [c])
    assert r["clips"][0]["effects_added"] == [{"id": "CROP" + c, "class_name": "Crop"}]


def test_a_preset_that_changes_nothing_leaves_no_undo_step_and_keeps_redo(editor, timeline):
    f = editor.add_file("video")
    c = editor.add_clip(f, end=10.0, rotation=curve(0.0))
    receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="rotate_90_right"))
    editor.undo()
    assert editor.manager.redoHistory
    editor.mark()
    r = receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="rotate_none"))
    assert r["changed"] is False and timeline.calls[-1] == ("rotate", "NONE", [c])
    assert editor.undo_steps_since_mark() == 0 and editor.manager.redoHistory  # redo still available
    editor.redo()
    assert editor.clip(c)["rotation"]["Points"][0]["co"]["Y"] == 90.0


def test_flip_negates_scale_and_keeps_animation(editor, timeline):
    c = editor.add_clip(editor.add_file("video"), end=10.0,
                        scale_x={"Points": [{"co": {"X": 1.0, "Y": 1.0}}, {"co": {"X": 301.0, "Y": 1.2}}]})
    receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="flip_horizontal"))
    assert [p["co"]["Y"] for p in editor.clip(c)["scale_x"]["Points"]] == [-1.0, -1.2]
    receipt(editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset="flip_horizontal"))
    assert [p["co"]["Y"] for p in editor.clip(c)["scale_x"]["Points"]] == [1.0, 1.2]
    assert editor.undo_steps_since_mark() == 2


@pytest.mark.parametrize("gravity,preset,allowed", [
    (2, "flip_horizontal", False), (2, "flip_vertical", False),   # Top Right: edge on both axes
    (5, "flip_horizontal", False), (5, "flip_vertical", True),    # Right: centred vertically
    (1, "flip_horizontal", True), (1, "flip_vertical", False),    # Top Center: centred horizontally
    (4, "flip_horizontal", True), (4, "flip_vertical", True),     # Center
])
def test_flip_only_mirrors_clips_centred_on_that_axis(editor, timeline, gravity, preset, allowed):
    """libopenshot 1.0 draws a mirrored clip outward from its anchor: a top-right PiP would vanish."""
    c = editor.add_clip(editor.add_file("video"), end=10.0, gravity=gravity,
                        scale_x={"Points": [{"co": {"X": 1.0, "Y": 0.5}}]},
                        scale_y={"Points": [{"co": {"X": 1.0, "Y": 0.5}}]})
    out = editor.call("apply_clip_preset_tool", timeline_clip_ids=[c], preset=preset)
    if allowed:
        receipt(out)
        key = "scale_x" if preset == "flip_horizontal" else "scale_y"
        assert editor.clip(c)[key]["Points"][0]["co"]["Y"] == -0.5
        assert editor.undo_steps_since_mark() == 1
    else:
        assert out.startswith("Error") and "off-screen" in out and "set gravity to" in out
        assert editor.undo_steps_since_mark() == 0
