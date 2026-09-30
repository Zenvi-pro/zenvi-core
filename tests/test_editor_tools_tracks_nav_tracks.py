"""Track tools: add_track_tool, update_track_tool, remove_track_tool (+ the shared track rules)."""

import json

import pytest

from classes import track_ops
from classes.track_display import normalize_track_or_layer_arg
from tracks_nav_fakes import FakeSelection, add_transition, set_layers


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


def numbers(editor):
    return sorted(int(t["number"]) for t in editor.get("layers"))


def layer_by_label(editor, label):
    return next(t for t in editor.get("layers") if t.get("label") == label)


# --- add_track_tool -----------------------------------------------------------

def test_add_track_goes_on_top_with_nothing_selected(editor):
    out = editor.call("add_track_tool", name="Music")
    data = receipt(out)
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000, 5000000, 6000000]
    assert layer_by_label(editor, "Music")["number"] == 6000000
    assert data["added"][0]["track"] == 6 and data["added"][0]["name"] == "Music"
    assert [r["track"] for r in data["tracks"]] == [1, 2, 3, 4, 5, 6]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000, 5000000]
    editor.redo()
    assert layer_by_label(editor, "Music")["number"] == 6000000


def test_add_track_at_the_bottom_renumbers_the_ui_tracks(editor):
    data = receipt(editor.call("add_track_tool", position="bottom", name="VO"))
    assert layer_by_label(editor, "VO")["number"] == 500000
    assert data["added"][0]["track"] == 1
    # The old bottom track is now track 2 in the receipt.
    assert next(r for r in data["tracks"] if r["layer"] == 1000000)["track"] == 2


def test_add_track_above_and_below_a_track(editor):
    receipt(editor.call("add_track_tool", position="above", relative_to="2", name="Above2"))
    assert layer_by_label(editor, "Above2")["number"] == 2500000
    receipt(editor.call("add_track_tool", position="below", relative_to="2", name="Below2"))
    assert layer_by_label(editor, "Below2")["number"] == 1500000
    # Relative to a track by name and by track id
    receipt(editor.call("add_track_tool", position="above", relative_to="Above2", name="X"))
    assert layer_by_label(editor, "X")["number"] == 2750000
    receipt(editor.call("add_track_tool", position="below", relative_to="L5", name="Y"))
    assert layer_by_label(editor, "Y")["number"] == 4500000


def test_add_several_tracks_is_one_step_and_names_run_bottom_to_top(editor):
    data = receipt(editor.call("add_track_tool", name="B-roll", count=3))
    assert [r["name"] for r in data["added"]] == ["B-roll 1", "B-roll 2", "B-roll 3"]
    assert [r["layer"] for r in data["added"]] == [6000000, 7000000, 8000000]
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    data = receipt(editor.call("add_track_tool", position="bottom", name="SFX", count=2))
    assert [r["name"] for r in data["added"]] == ["SFX 1", "SFX 2"]
    assert [r["track"] for r in data["added"]] == [1, 2]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert not any(t["label"].startswith("SFX") for t in editor.get("layers"))


def test_add_track_with_tight_numbers_renumbers_in_the_same_undo_step(editor):
    set_layers(editor, [1, 2, 3])
    f = editor.add_file("video")
    c = editor.add_clip(f, layer=2, position=1.0)
    editor.mark()
    data = receipt(editor.call("add_track_tool", position="above", relative_to="1", name="New"))
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000]
    assert layer_by_label(editor, "New")["number"] == 2000000
    assert editor.clip(c)["layer"] == 3000000          # the clip moved with its track
    assert data["added"][0]["track"] == 2
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert numbers(editor) == [1, 2, 3]
    assert editor.clip(c)["layer"] == 2
    editor.redo()
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000]
    assert editor.clip(c)["layer"] == 3000000


def test_add_track_to_a_project_without_tracks(editor):
    set_layers(editor, [])
    receipt(editor.call("add_track_tool"))
    assert numbers(editor) == [1000000]


def test_add_track_refusals_leave_history_alone(editor):
    for args, needle in (
        ({"position": "above"}, "needs relative_to"),
        ({"position": "top", "relative_to": "2"}, "only applies"),
        ({"position": "below", "relative_to": "Nope"}, "Nope"),
        ({"count": 0}, "count"),
        ({"position": "sideways"}, "position"),
        ({"label": "x"}, "unknown argument"),
    ):
        out = editor.call("add_track_tool", **args)
        assert out.startswith("Error") and needle in out, (args, out)
    assert editor.undo_steps_since_mark() == 0
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000, 5000000]


def test_add_track_warns_about_a_duplicate_name(editor):
    receipt(editor.call("add_track_tool", name="Music"))
    data = receipt(editor.call("add_track_tool", name="music"))
    assert "already has this name" in data["warning"]


# --- update_track_tool -----------------------------------------------------------

def test_rename_and_lock_a_track(editor):
    data = receipt(editor.call("update_track_tool", track="2", name="B-roll"))
    assert data["changed"] == ["name"] and data["track"]["name"] == "B-roll"
    assert next(t for t in editor.get("layers") if t["number"] == 2000000)["label"] == "B-roll"
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    data = receipt(editor.call("update_track_tool", track="B-roll", lock=True))
    assert data["track"]["locked"] is True
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert next(t for t in editor.get("layers") if t["number"] == 2000000)["lock"] is False


def test_rename_and_lock_together_is_one_step(editor):
    out = editor.call("update_track_tool", track="3", name="Titles", lock=True)
    data = receipt(out)
    assert data["changed"] == ["name", "lock"]
    assert "renamed to 'Titles' and locked" in out
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    t = next(t for t in editor.get("layers") if t["number"] == 3000000)
    assert t["label"] == "" and t["lock"] is False


def test_update_track_noop_and_clearing_a_name(editor):
    editor.lock_track(1000000)
    editor.mark()
    out = editor.call("update_track_tool", track="1", lock=True)
    assert receipt(out)["changed"] == [] and "already locked" in out
    assert editor.undo_steps_since_mark() == 0
    # Renaming a locked track is allowed (the Track menu allows it too).
    receipt(editor.call("update_track_tool", track="1", name="Base"))
    receipt(editor.call("update_track_tool", track="Base", name=""))
    assert next(t for t in editor.get("layers") if t["number"] == 1000000)["label"] == ""


def test_update_track_refusals(editor):
    assert "nothing to change" in editor.call("update_track_tool", track="1")
    assert editor.call("update_track_tool", track="9", name="x").startswith("Error")
    assert "missing required" in editor.call("update_track_tool", name="x")
    assert editor.undo_steps_since_mark() == 0


def test_a_locked_track_is_refused_by_other_tools(editor):
    f = editor.add_file("video")
    c = editor.add_clip(f, layer=1000000)
    receipt(editor.call("update_track_tool", track="1", lock=True))
    out = editor.call("delete_from_timeline_tool", timeline_clip_id=c)
    assert out.startswith("Error") and "locked" in out
    assert editor.clip(c) is not None


# --- remove_track_tool ------------------------------------------------------------

def test_remove_an_empty_track(editor):
    data = receipt(editor.call("remove_track_tool", track="3"))
    assert numbers(editor) == [1000000, 2000000, 4000000, 5000000]
    assert data["removed_track"]["layer"] == 3000000 and data["deleted_clip_ids"] == []
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert 3000000 in numbers(editor)


def test_remove_a_track_with_clips_needs_with_clips_and_undo_restores_them(editor):
    sel = FakeSelection(editor)
    f = editor.add_file("video")
    a = editor.add_clip(f, layer=2000000, position=0.0)
    b = editor.add_clip(f, layer=2000000, position=30.0)
    keep = editor.add_clip(f, layer=1000000, position=0.0)
    t = add_transition(editor, layer=2000000, position=29.0)
    sel.add(a, "clip")
    before = {cid: dict(editor.clip(cid)) for cid in (a, b)}
    editor.mark()

    out = editor.call("remove_track_tool", track="2")
    assert out.startswith("Error") and "2 clip(s) and 1 transition(s)" in out and "with_clips=true" in out
    assert editor.undo_steps_since_mark() == 0 and editor.clip(a) is not None

    data = receipt(editor.call("remove_track_tool", track="2", with_clips=True))
    assert sorted(data["deleted_clip_ids"]) == sorted([a, b])
    assert data["deleted_transition_ids"] == [t]
    assert editor.clip(a) is None and editor.clip(b) is None and editor.clip(keep) is not None
    assert 2000000 not in numbers(editor)
    removed_items = [call.args for call in editor.window.deselect_removed_item.call_args_list]
    assert (a, "clip") in removed_items and (t, "transition") in removed_items
    assert editor.undo_steps_since_mark() == 1

    editor.undo()
    assert 2000000 in numbers(editor)
    assert editor.clip(a) == before[a] and editor.clip(b) == before[b]
    assert any(e["id"] == t for e in editor.get("effects"))
    editor.redo()
    assert editor.clip(a) is None and 2000000 not in numbers(editor)


def test_remove_track_refusals(editor):
    editor.lock_track(2000000)
    editor.mark()
    assert "locked" in editor.call("remove_track_tool", track="2")
    assert editor.call("remove_track_tool", track="Nope").startswith("Error")
    set_layers(editor, [1000000])
    assert "at least one track" in editor.call("remove_track_tool", track="1")
    assert editor.undo_steps_since_mark() == 0


# --- shared rules ------------------------------------------------------------------

@pytest.mark.parametrize("numbers_, position, relative, expected", [
    ([], "top", None, ("number", 1000000)),
    ([1000000, 2000000], "top", None, ("number", 3000000)),
    ([1000000, 2000000], "bottom", None, ("number", 500000)),
    ([1000000, 2000000], "above", 1000000, ("number", 1500000)),
    ([1000000, 2000000], "above", 2000000, ("number", 3000000)),
    ([1000000, 2000000], "below", 2000000, ("number", 1500000)),
    ([1, 2, 3], "above", 1, ("renumber", 1)),
    ([1, 2, 3], "below", 2, ("renumber", 1)),
    ([2, 5], "bottom", None, ("renumber", 0)),
])
def test_plan_insert_follows_the_track_menu_rules(editor, numbers_, position, relative, expected):
    assert track_ops.plan_insert(numbers_, position, relative) == expected


def test_plan_insert_rejects_unknown_tracks_and_positions(editor):
    with pytest.raises(track_ops.TrackOpError):
        track_ops.plan_insert([1000000], "above", 7)
    with pytest.raises(track_ops.TrackOpError):
        track_ops.plan_insert([1000000], "middle")


def test_ui_track_numbers_never_substring_match_names():
    layers = [{"id": "L1", "number": 1000000, "label": ""},
              {"id": "L2", "number": 2000000, "label": "Music 1"},
              {"id": "L3", "number": 3000000, "label": "5"},
              {"id": "L4", "number": 4000000, "label": "Voiceover"}]
    # "1" is UI track 1, not the track whose name contains "1".
    assert normalize_track_or_layer_arg("1", layers) == (1000000, None)
    # "track 2" / "Track #2" name UI track 2.
    assert normalize_track_or_layer_arg("track 2", layers) == (2000000, None)
    assert normalize_track_or_layer_arg("Track #2", layers) == (2000000, None)
    # An exact numeric label still wins, and names still match by part.
    assert normalize_track_or_layer_arg("5", layers) == (3000000, None)
    assert normalize_track_or_layer_arg("voice", layers) == (4000000, None)
    assert normalize_track_or_layer_arg("Music 1", layers) == (2000000, None)
    layer, err = normalize_track_or_layer_arg("9", layers)
    assert layer is None and err.startswith("Error")
