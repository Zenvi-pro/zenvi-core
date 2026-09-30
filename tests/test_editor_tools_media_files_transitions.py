"""Timeline transition tools: list, update (length/mask/curves), reverse, remove, copy, and transitions
between every pair of clips (classes.editor_tools.media_files_transitions)."""

import json

import pytest

from classes import transition_ops as ops

L1, L2 = 1000000, 2000000


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


@pytest.fixture
def tl(editor):
    editor.window.timeline._get_transition_reader_json = lambda path: {
        "path": path, "has_single_image": True, "type": "QtImageReader"}
    return editor


def add_transition(editor, position, duration, layer=L1, mask="fade", reversed_=False, **extra):
    from classes.query import Transition
    entry, _ = ops.find_transition(mask)
    data = ops.new_mask_transition(None, {"path": entry["path"], "has_single_image": True},
                                   position=position, layer=layer, duration=duration, fps_float=30.0,
                                   title=entry["key"])
    data.pop("id")
    if reversed_:
        ops.reverse_transition_data(data)
    data.update(extra)
    t = Transition()
    t.data = data
    t.save()
    editor.mark()
    return t.id


def transition(editor, tid):
    for t in editor.get("effects") or []:
        if t.get("id") == tid:
            return t
    return None


def _last_x(kf):
    return max(p["co"]["X"] for p in kf["Points"])


def crossfaded(editor):
    """A [0,10) B [9,19) C [18,28) on track 1 with 1 s crossfades A|B and B|C."""
    f = editor.add_file("video", duration=30.0)
    a = editor.add_clip(f, position=0.0, end=10.0)
    b = editor.add_clip(f, position=9.0, end=10.0)
    c = editor.add_clip(f, position=18.0, end=10.0)
    t1 = add_transition(editor, 9.0, 1.0)
    t2 = add_transition(editor, 18.0, 1.0)
    return a, b, c, t1, t2


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_describes_pairs_edges_and_direction(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    fade_out = add_transition(tl, 27.0, 1.0, mask="circle_in_to_out", reversed_=True)
    rows = _receipt(tl.call("list_timeline_transitions_tool"))["transitions"]
    by_id = {r["transition_id"]: r for r in rows}
    assert by_id[t1]["between_clip_ids"] == [a, b] and by_id[t1]["clip_overlap"] == 1.0
    assert by_id[t1]["mask"] == "fade" and by_id[t1]["direction"] == "default" and by_id[t1]["track"] == 1
    assert by_id[fade_out]["on_clip_end"] == c and by_id[fade_out]["direction"] == "reversed"
    assert [r["transition_id"] for r in _receipt(tl.call("list_timeline_transitions_tool",
                                                         timeline_clip_id=a))["transitions"]] == [t1]
    assert _receipt(tl.call("list_timeline_transitions_tool", at_seconds=18.5))["transitions"][0][
        "transition_id"] == t2
    assert _receipt(tl.call("list_timeline_transitions_tool", track="2"))["transitions"] == []


# ---------------------------------------------------------------------------
# update: duration
# ---------------------------------------------------------------------------

def test_longer_crossfade_moves_the_later_clip_and_ripples(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    r = _receipt(tl.call("update_transition_tool", between_clip_ids=[a, b], duration_seconds=2.0))
    assert tl.clip(b)["position"] == pytest.approx(8.0) and tl.clip(c)["position"] == pytest.approx(17.0)
    t = transition(tl, t1)
    assert t["position"] == pytest.approx(8.0) and t["end"] == pytest.approx(2.0)
    assert _last_x(t["brightness"]) == 61
    assert transition(tl, t2)["position"] == pytest.approx(17.0)
    assert r["transitions"][0]["clip_overlap"] == pytest.approx(2.0)
    assert tl.undo_steps_since_mark() == 1
    tl.undo()
    assert tl.clip(b)["position"] == 9.0 and tl.clip(c)["position"] == 18.0
    assert transition(tl, t1)["end"] == 1.0 and transition(tl, t2)["position"] == 18.0
    tl.redo()
    assert tl.clip(c)["position"] == pytest.approx(17.0)


def test_every_crossfade_on_a_track_ripples_cumulatively(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    _receipt(tl.call("update_transition_tool", scope="track", track="1", duration_seconds=2.0))
    assert tl.clip(b)["position"] == pytest.approx(8.0) and tl.clip(c)["position"] == pytest.approx(16.0)
    assert transition(tl, t2)["position"] == pytest.approx(16.0) and transition(tl, t2)["end"] == pytest.approx(2.0)
    assert tl.undo_steps_since_mark() == 1


def test_shorter_crossfade_without_ripple_and_keep_overlap(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], duration_seconds=0.5, ripple=False))
    assert tl.clip(b)["position"] == pytest.approx(9.5) and tl.clip(c)["position"] == 18.0
    assert transition(tl, t1)["position"] == pytest.approx(9.5)
    tl.mark()
    _receipt(tl.call("update_transition_tool", transition_ids=[t2], duration_seconds=1.5, keep_overlap=True))
    assert tl.clip(c)["position"] == 18.0 and transition(tl, t2)["end"] == pytest.approx(1.5)


def test_fade_at_a_clip_end_keeps_the_end(tl):
    f = tl.add_file("video")
    tl.add_clip(f, position=0.0, end=10.0)
    t = add_transition(tl, 9.0, 1.0, reversed_=True)
    _receipt(tl.call("update_transition_tool", transition_ids=[t], duration_seconds=3.0))
    data = transition(tl, t)
    assert data["position"] == pytest.approx(7.0) and data["end"] == pytest.approx(3.0)
    assert ops.direction_of(data) == "reversed"


def test_update_refusals_are_not_undoable(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    for args in ({"transition_ids": [t1]},
                 {"transition_ids": [t1], "duration_seconds": 12.0},
                 {"transition_ids": ["NOPE"], "contrast": 5},
                 {"contrast": 5},
                 {"transition_ids": [t1, t2], "position_seconds": 3.0},
                 {"transition_ids": [t1], "mask": "not-a-real-transition-name"},
                 {"transition_ids": [t1], "mask": "fade", "mask_file_id": "X"},
                 {"between_clip_ids": [a], "contrast": 5},
                 {"between_clip_ids": [a, c], "contrast": 5}):
        out = tl.call("update_transition_tool", **args)
        assert out.startswith("Error"), (args, out)
    tl.lock_track(L1)
    assert "locked" in tl.call("update_transition_tool", transition_ids=[t1], contrast=5)
    assert tl.undo_steps_since_mark() == 0


def test_update_noop_adds_no_undo_step(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    r = _receipt(tl.call("update_transition_tool", transition_ids=[t1], duration_seconds=1.0))
    assert r["changed"] is False and tl.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# update: mask, curves, flags
# ---------------------------------------------------------------------------

def test_change_mask_keeps_direction_and_curve(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    tl.call("reverse_transition_tool", transition_ids=[t1])
    tl.mark()
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], mask="circle"))
    data = transition(tl, t1)
    assert data["reader"]["path"].endswith("circle_in_to_out.svg") and data["title"] == "circle_in_to_out"
    assert ops.direction_of(data) == "reversed" and tl.undo_steps_since_mark() == 1
    tl.undo()
    assert transition(tl, t1)["reader"]["path"].endswith("fade.svg")


def test_flags_contrast_curves_and_interpolation(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], invert_mask=True, contrast=12,
                     audio_crossfade=True, interpolation="linear"))
    data = transition(tl, t1)
    assert data["mask_invert"] is True and data["fade_audio_hint"] is True
    assert [p["co"]["Y"] for p in data["contrast"]["Points"]] == [12.0]
    assert {p["interpolation"] for p in data["brightness"]["Points"]} == {1}
    assert tl.undo_steps_since_mark() == 1
    tl.undo()
    data = transition(tl, t1)
    assert data["mask_invert"] is False and data["fade_audio_hint"] is False
    tl.mark()
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], brightness_keyframes=[
        {"seconds": 0, "value": 1}, {"seconds": 0.5, "value": 0, "interpolation": "linear"},
        {"seconds": 1, "value": -1}]))
    assert [(p["co"]["X"], p["co"]["Y"]) for p in transition(tl, t1)["brightness"]["Points"]] == [
        (1.0, 1.0), (16.0, 0.0), (31.0, -1.0)]
    assert tl.call("update_transition_tool", transition_ids=[t1], brightness_keyframes=[
        {"seconds": 5, "value": 1}]).startswith("Error")


def test_mask_from_a_project_image(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    img = tl.add_file("image", path="/media/gradient.png")
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], mask_file_id=img))
    assert transition(tl, t1)["reader"]["path"] == "/media/gradient.png"
    audio = tl.add_file("audio")
    assert tl.call("update_transition_tool", transition_ids=[t1], mask_file_id=audio).startswith("Error")


# ---------------------------------------------------------------------------
# reverse / remove / copy
# ---------------------------------------------------------------------------

def test_reverse_flips_and_undoes(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    r = _receipt(tl.call("reverse_transition_tool", scope="track", track="1"))
    assert [x["direction"] for x in r["transitions"]] == ["reversed", "reversed"]
    assert tl.undo_steps_since_mark() == 1
    tl.undo()
    assert ops.direction_of(transition(tl, t1)) == "default"
    tl.lock_track(L1)
    assert "locked" in tl.call("reverse_transition_tool", transition_ids=[t1])


def test_remove_keeps_clips(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    r = _receipt(tl.call("remove_transition_tool", at_seconds=9.5, track="1"))
    assert r["removed_transition_ids"] == [t1] and transition(tl, t1) is None
    assert tl.clip(a) and tl.clip(b)["position"] == 9.0 and transition(tl, t2)
    assert tl.undo_steps_since_mark() == 1
    tl.undo()
    assert transition(tl, t1)
    assert tl.call("remove_transition_tool", at_seconds=3.0).startswith("Error")


def test_ambiguous_target_is_refused(tl):
    crossfaded(tl)
    out = tl.call("remove_transition_tool")
    assert out.startswith("Error") and "2 transitions" in out


def test_copy_all_fits_each_targets_length(tl):
    a, b, c, t1, t2 = crossfaded(tl)
    tl.call("update_transition_tool", transition_ids=[t2], duration_seconds=2.0, keep_overlap=True)
    tl.call("update_transition_tool", transition_ids=[t1], mask="wipe_left_to_right", contrast=15)
    tl.call("reverse_transition_tool", transition_ids=[t1])
    tl.mark()
    r = _receipt(tl.call("copy_transition_tool", source_transition_id=t1, scope="track", track="1"))
    target = transition(tl, t2)
    assert [x["transition_id"] for x in r["transitions"]] == [t2]
    assert target["reader"]["path"].endswith("wipe_left_to_right.svg") and target["end"] == pytest.approx(2.0)
    assert ops.direction_of(target) == "reversed" and _last_x(target["brightness"]) == 61
    assert target["position"] == 18.0 and tl.undo_steps_since_mark() == 1
    tl.mark()
    tl.call("update_transition_tool", transition_ids=[t2], mask="circle")
    _receipt(tl.call("copy_transition_tool", source_transition_id=t1, transition_ids=[t2], what="mask"))
    assert transition(tl, t2)["reader"]["path"].endswith("wipe_left_to_right.svg")
    assert tl.call("copy_transition_tool", source_transition_id=t1, transition_ids=[t1]).startswith("Error")
    assert tl.call("copy_transition_tool", source_transition_id="NOPE", transition_ids=[t2]).startswith("Error")


# ---------------------------------------------------------------------------
# add_transitions_between_clips_tool
# ---------------------------------------------------------------------------

def butting(editor, n=3, length=5.0, start=0.0, layer=L1, file_duration=30.0):
    f = editor.add_file("video", duration=file_duration)
    return [editor.add_clip(f, position=i * length, start=start, end=start + length, layer=layer)
            for i in range(n)]


def test_crossfades_between_all_clips_ripple(tl):
    a, b, c = butting(tl)
    r = _receipt(tl.call("add_transitions_between_clips_tool", track="1", duration_seconds=1.0))
    assert [x["between_clip_ids"] for x in r["added"]] == [[a, b], [b, c]]
    assert tl.clip(b)["position"] == pytest.approx(4.0) and tl.clip(c)["position"] == pytest.approx(8.0)
    trans = sorted(tl.get("effects"), key=lambda t: t["position"])
    assert [t["position"] for t in trans] == [pytest.approx(4.0), pytest.approx(8.0)]
    assert all(t["fade_audio_hint"] and t["end"] == 1.0 and t["reader"]["path"].endswith("fade.svg") for t in trans)
    assert tl.undo_steps_since_mark() == 1
    tl.undo()
    assert tl.get("effects") == [] and tl.clip(c)["position"] == 10.0
    tl.redo()
    assert len(tl.get("effects")) == 2


def test_handles_keep_the_cuts_in_place(tl):
    a, b, c = butting(tl, start=2.0)
    r = _receipt(tl.call("add_transitions_between_clips_tool", track="1", duration_seconds=1.0, method="handles"))
    assert len(r["added"]) == 2
    assert tl.clip(a)["end"] == pytest.approx(7.5) and tl.clip(b)["position"] == pytest.approx(4.5)
    assert tl.clip(b)["start"] == pytest.approx(1.5) and tl.clip(c)["position"] == pytest.approx(9.5)
    assert sorted(t["position"] for t in tl.get("effects")) == [pytest.approx(4.5), pytest.approx(9.5)]
    no_handles = butting(tl, layer=L2, start=0.0, file_duration=5.0)
    out = tl.call("add_transitions_between_clips_tool", timeline_clip_ids=no_handles[:2], method="handles")
    assert out.startswith("Error") and "spare media" in out


def test_existing_overlaps_gaps_existing_transitions_and_clamping(tl):
    f = tl.add_file("video", duration=30.0)
    a = tl.add_clip(f, position=0.0, end=4.0)
    b = tl.add_clip(f, position=3.5, end=4.0)      # overlaps a by 0.5
    c = tl.add_clip(f, position=9.0, end=0.6)      # gap after b, very short
    d = tl.add_clip(f, position=9.6, end=4.0)      # butts c
    r = _receipt(tl.call("add_transitions_between_clips_tool", track="1", method="existing_overlaps"))
    assert [x["between_clip_ids"] for x in r["added"]] == [[a, b]] and r["added"][0]["duration"] == 0.5
    assert {s["reason"] for s in r["skipped"]} >= {"clips do not overlap"}
    tl.mark()
    r = _receipt(tl.call("add_transitions_between_clips_tool", track="1", duration_seconds=2.0))
    reasons = {s["join"]: s["reason"] for s in r["skipped"]}
    assert reasons[f"{a}->{b}"] == "already has a transition" and "gap" in reasons[f"{b}->{c}"]
    assert r["added"][0]["between_clip_ids"] == [c, d] and r["added"][0]["duration"] == pytest.approx(0.3)
    tl.mark()
    r = _receipt(tl.call("add_transitions_between_clips_tool", timeline_clip_ids=[a, b], replace_existing=True,
                         transition="circle"))
    assert len(r["replaced_transition_ids"]) == 1 and r["added"][0]["mask"] == "circle_in_to_out"


def test_auto_transition_refusals(tl):
    a, b = butting(tl, n=2)
    one = butting(tl, n=1, layer=L2)
    for args in ({}, {"track": "2"}, {"track": "1", "transition": "no-such-transition-zz"},
                 {"timeline_clip_ids": [a, one[0]]}, {"timeline_clip_ids": ["NOPE"]}):
        assert tl.call("add_transitions_between_clips_tool", **args).startswith("Error"), args
    tl.lock_track(L1)
    assert "locked" in tl.call("add_transitions_between_clips_tool", track="1")
    assert tl.undo_steps_since_mark() == 0


def test_random_transitions_and_default_length(tl):
    tl.settings["default-transition-length"] = 0.5
    a, b, c = butting(tl)
    r = _receipt(tl.call("add_transitions_between_clips_tool", track="1", transition="random", duration_seconds=0))
    assert len(r["added"]) == 2 and all(x["duration"] == 0.5 for x in r["added"])
    assert all(x["mask"] for x in r["added"])


def test_flip_wipe_side_keeps_the_clip_order(tl):
    """Live finding: invert or reverse alone shows the later clip first; both together flip the side."""
    a, b, c, t1, t2 = crossfaded(tl)
    rows = {r["transition_id"]: r for r in _receipt(tl.call("list_timeline_transitions_tool"))["transitions"]}
    assert rows[t1]["order"] == "earlier_to_later"
    _receipt(tl.call("update_transition_tool", transition_ids=[t1], mask="wipe_left_to_right",
                     flip_wipe_side=True))
    data = transition(tl, t1)
    assert data["mask_invert"] is True and ops.direction_of(data) == "reversed"
    row = _receipt(tl.call("list_timeline_transitions_tool", timeline_clip_id=a))["transitions"][0]
    assert row["order"] == "earlier_to_later"
    tl.call("reverse_transition_tool", transition_ids=[t1])
    row = _receipt(tl.call("list_timeline_transitions_tool", timeline_clip_id=a))["transitions"][0]
    assert row["order"] == "later_first"
    assert tl.call("update_transition_tool", transition_ids=[t1], flip_wipe_side=True,
                   invert_mask=False).startswith("Error")
