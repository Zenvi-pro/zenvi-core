"""timeline-edit tools: add many, move/nudge/reorder, trim, slice, gaps, duplicate, align.

Every mutating call is exactly one undo step; every refusal adds none.
"""

import json

import pytest

from classes.query import Clip, Transition
from timeline_edit_fakes import FakeTimeline, fake_openshot

T1, T2, T3 = 1000000, 2000000, 3000000  # UI tracks 1 (bottom), 2, 3


@pytest.fixture
def ed(editor, monkeypatch):
    from classes import clip_utils, timeline_ops
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    editor.window.selected_tracks = []
    return editor


def receipt(out):
    assert not out.startswith("Error"), out
    head, _, body = out.partition("\n")
    return json.loads(body) if body else {}


def refused(editor, out, *fragments):
    assert out.startswith("Error"), out
    for frag in fragments:
        assert frag in out, out
    assert editor.undo_steps_since_mark() == 0


def one_step(editor):
    assert editor.undo_steps_since_mark() == 1


def pos(editor, cid):
    return round(editor.clip(cid)["position"], 3)


# ---------------------------------------------------------------------------
# add_clips_to_timeline_tool
# ---------------------------------------------------------------------------

def test_add_clips_back_to_back_on_empty_track(ed):
    files = [ed.add_file("video", duration=d) for d in (4.0, 6.0, 5.0)]
    out = ed.call("add_clips_to_timeline_tool", file_ids=files, track="1")
    r = receipt(out)
    assert [c["position"] for c in r["clips"]] == [0.0, 4.0, 10.0]
    assert r["total_seconds"] == 15.0 and r["track"] == 1
    one_step(ed)
    ed.undo()
    assert Clip.filter() == []
    ed.redo()
    assert len(Clip.filter()) == 3


def test_add_clips_appends_after_the_track_and_defaults_to_the_bottom_track(ed):
    f = ed.add_file("video", duration=5.0)
    ed.add_clip(f, position=0.0, end=3.0, layer=T1)
    r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=[f]))
    assert r["clips"][0]["position"] == 3.0 and r["clips"][0]["layer"] == T1


def test_add_clips_fit_to_total_with_crossfades(ed):
    files = [ed.add_file("video", duration=10.0) for _ in range(6)]
    r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=files, fit_total_seconds=30,
                        transition="fade", transition_seconds=0.5))
    assert abs(r["total_seconds"] - 30.0) <= 1 / 30 + 1e-6
    assert len(r["transition_ids"]) == 5
    lengths = [round(c["duration"], 3) for c in r["clips"]]
    assert max(lengths) - min(lengths) <= 2 / 30 + 1e-6  # evenly trimmed
    # the middle of each clip is kept
    c0 = r["clips"][0]
    assert abs(c0["source_in"] - (10.0 - c0["duration"]) / 2) <= 1 / 30 + 1e-6
    reader = Transition.get(id=r["transition_ids"][0]).data["reader"]
    assert reader["path"].endswith("fade.svg")
    one_step(ed)


def test_add_clips_fit_keeps_short_and_explicit_clips(ed):
    long_ = ed.add_file("video", duration=30.0)
    short = ed.add_file("video", duration=2.0)
    r = receipt(ed.call("add_clips_to_timeline_tool", items=[
        {"file_id": long_}, {"file_id": short}, {"file_id": long_, "source_start": 5, "source_end": 13}],
        fit_total_seconds=20))
    d = [c["duration"] for c in r["clips"]]
    assert d[1] == 2.0 and d[2] == 8.0 and abs(sum(d) - 20.0) < 1e-6


def test_add_clips_fit_refuses_an_impossible_target(ed):
    files = [ed.add_file("video", duration=10.0) for _ in range(8)]
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=files, fit_total_seconds=5),
            "cannot fit 8 clips", "8.0 s")


def test_add_clips_images_and_image_length(ed):
    img = ed.add_file("image")
    r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=[img, img], image_seconds=3))
    assert [(c["position"], c["duration"]) for c in r["clips"]] == [(0.0, 3.0), (3.0, 3.0)]


def test_add_clips_refusals(ed):
    f = ed.add_file("video", duration=5.0)
    refused(ed, ed.call("add_clips_to_timeline_tool"), "file_ids or items")
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[f], items=[{"file_id": f}]), "not both")
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=["nope"]), "no project file")
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[f], transition="sparkle_swirl"), "unknown transition")
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[f], transition="fade", fade="in"), "not both")
    refused(ed, ed.call("add_clips_to_timeline_tool", items=[{"file_id": f, "source_start": 3, "source_end": 9}]),
            "outside the file")
    refused(ed, ed.call("add_clips_to_timeline_tool", items=[{"file_id": f, "bogus": 1}]), "unknown key")
    ed.lock_track(T1)
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[f], track="1"), "locked")


def test_add_clips_refuses_to_cover_existing_clips_unless_allowed(ed):
    f = ed.add_file("video", duration=5.0)
    ed.add_clip(f, position=2.0, end=5.0, layer=T1)
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[f], start_seconds=0, track="1"),
            "would overlap", "allow_overlap")
    r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=[f], start_seconds=0, track="1", allow_overlap=True))
    assert r["clips"][0]["position"] == 0.0


def test_add_clips_transition_longer_than_a_clip_is_refused(ed):
    a = ed.add_file("video", duration=5.0)
    b = ed.add_file("video", duration=0.5)
    refused(ed, ed.call("add_clips_to_timeline_tool", file_ids=[a, b, a], transition="fade",
                        transition_seconds=1.0), "too short")


def test_add_clips_random_transition_and_order(ed):
    files = [ed.add_file("video", duration=4.0, name=n) for n in ("c.mp4", "a.mp4", "b.mp4")]
    r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=files, order="name", transition="random"))
    assert [c["file_id"] for c in r["clips"]] == [files[1], files[2], files[0]]
    assert len(r["transition_ids"]) == 2


def test_add_clips_snaps_to_the_24fps_grid():
    from editor_tools_harness import make_editor
    ed = make_editor(fps=(24, 1))
    try:
        from unittest.mock import patch
        from classes import clip_utils, timeline_ops
        with patch.object(timeline_ops, "openshot", fake_openshot()), \
                patch.object(clip_utils, "get_app", return_value=ed.app):
            ed.window.timeline = FakeTimeline(ed)
            f = ed.add_file("video", duration=10.0)
            r = receipt(ed.call("add_clips_to_timeline_tool", file_ids=[f, f, f], fit_total_seconds=17.3))
        for c in r["clips"]:
            data = ed.clip(c["timeline_clip_id"])
            for key in ("position", "start", "end"):
                assert abs(data[key] * 24 - round(data[key] * 24)) < 1e-6, (key, data[key])
    finally:
        ed.stop()


# ---------------------------------------------------------------------------
# move_clips_tool
# ---------------------------------------------------------------------------

def _seq(ed, n=3, d=4.0, layer=T1):
    f = ed.add_file("video", duration=10.0)
    return [ed.add_clip(f, position=i * d, end=d, layer=layer) for i in range(n)]


def test_move_to_time_and_undo(ed):
    a, = _seq(ed, 1)
    r = receipt(ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=12.5))
    assert pos(ed, a) == 12.5 and r["moved"][0]["to"]["position"] == 12.5
    one_step(ed)
    ed.undo()
    assert pos(ed, a) == 0.0
    ed.redo()
    assert pos(ed, a) == 12.5


def test_nudge_by_frames(ed):
    a, = _seq(ed, 1)
    ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=1.0)
    ed.mark()
    receipt(ed.call("move_clips_tool", timeline_clip_ids=[a], by_frames=-3))
    assert abs(ed.clip(a)["position"] - (1.0 - 3 / 30)) < 1e-9
    one_step(ed)


def test_move_to_another_track_keeps_time(ed):
    a, = _seq(ed, 1)
    receipt(ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="3"))
    assert ed.clip(a)["layer"] == T3 and pos(ed, a) == 0.0


def test_move_refuses_overlap_unless_allowed(ed):
    a, b, _c = _seq(ed)
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=5.0), "would overlap",
            "ripple=true")
    r = receipt(ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=5.0, allow_overlap=True))
    assert {h["overlaps"] for h in r["overlaps"]} >= {b}


def test_move_refusals(ed):
    a, b, _c = _seq(ed)
    refused(ed, ed.call("move_clips_tool", position_seconds=1), "say which clips")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a]), "nothing to do")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=1, by_frames=2), "only one")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[b], by_seconds=-10), "before the timeline start")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], after_clip_id=a), "anchor clip is one")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=["nope"], by_seconds=1), "no timeline clip")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="9"))
    ed.lock_track(T2)
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="2"), "track 2 is locked")
    ed.lock_track(T2, False)
    ed.lock_track(T1)
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="2"), "track 1 is locked")


def test_ripple_move_intro_to_the_end(ed):
    a, b, c = _seq(ed)
    r = receipt(ed.call("move_clips_tool", timeline_clip_ids=[a], after_clip_id=c, ripple=True))
    assert [pos(ed, x) for x in (b, c, a)] == [0.0, 4.0, 8.0]
    assert set(r["shifted"]) == {b, c}
    one_step(ed)
    ed.undo()
    assert [pos(ed, x) for x in (a, b, c)] == [0.0, 4.0, 8.0]


def test_ripple_move_before_a_clip_and_by_position(ed):
    a, b, c = _seq(ed)
    receipt(ed.call("move_clips_tool", timeline_clip_ids=[c], before_clip_id=a, ripple=True))
    assert [pos(ed, x) for x in (c, a, b)] == [0.0, 4.0, 8.0]
    ed.mark()
    # position_seconds counts in the timeline as it was before the move: 12 = the end
    receipt(ed.call("move_clips_tool", timeline_clip_ids=[c], position_seconds=12.0, ripple=True))
    assert [pos(ed, x) for x in (a, b, c)] == [0.0, 4.0, 8.0]


def test_ripple_move_refuses_non_contiguous_runs_and_mid_clip_inserts(ed):
    a, b, c = _seq(ed)
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a, c], position_seconds=20, ripple=True),
            "contiguous")
    refused(ed, ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=6.0, ripple=True),
            "falls inside")


def test_move_several_clips_keeps_spacing_and_relative_tracks(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=1.0, end=2.0, layer=T1)
    b = ed.add_clip(f, position=2.0, end=2.0, layer=T2)
    receipt(ed.call("move_clips_tool", timeline_clip_ids=[a, b], position_seconds=10.0, to_track="3"))
    assert (ed.clip(a)["layer"], pos(ed, a)) == (T2, 10.0)
    assert (ed.clip(b)["layer"], pos(ed, b)) == (T3, 11.0)


# ---------------------------------------------------------------------------
# trim_clips_tool
# ---------------------------------------------------------------------------

def test_trim_start_keeps_picture_in_place(ed):
    a, b = _seq(ed, 2)
    r = receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_start_seconds=2))
    assert (ed.clip(a)["start"], pos(ed, a), ed.clip(a)["end"]) == (2.0, 2.0, 4.0)
    assert r["clips"][0]["after"]["duration"] == 2.0 and pos(ed, b) == 4.0
    one_step(ed)
    ed.undo()
    assert (ed.clip(a)["start"], pos(ed, a)) == (0.0, 0.0)


def test_ripple_trim_pulls_later_clips(ed):
    a, b, c = _seq(ed)
    r = receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_start_seconds=1.5, ripple=True))
    assert (pos(ed, a), ed.clip(a)["start"]) == (0.0, 1.5)
    assert [pos(ed, b), pos(ed, c)] == [2.5, 6.5]
    assert set(r["shifted"]) == {b, c}
    one_step(ed)


def test_trim_limits_are_reported(ed):
    a, b = _seq(ed, 2)
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[b], source_end=12), "at most 10.00 s")
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[b], trim_start_seconds=-1), "at most 0.00 s")
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_end_seconds=-2), "would overlap")
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_start_seconds=4), "one frame")
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[a]), "nothing to trim")
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_end_seconds=1, duration_seconds=2), "only one")
    ed.lock_track(T1)
    refused(ed, ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_end_seconds=1), "locked")


def test_trim_extends_a_trimmed_clip_back_into_its_source(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=5.0, start=3.0, end=6.0)
    receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_start_seconds=-1, duration_seconds=5))
    assert (ed.clip(a)["start"], ed.clip(a)["end"], pos(ed, a)) == (2.0, 7.0, 4.0)


def test_trim_extend_end_with_ripple_pushes_next_clip(ed):
    a, b = _seq(ed, 2)
    receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_end_seconds=-2, ripple=True))
    assert ed.clip(a)["end"] == 6.0 and pos(ed, b) == 6.0


def test_trim_image_can_grow_past_its_default_length(ed):
    img = ed.add_file("image")
    a = ed.add_clip(img, position=0.0)
    receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], duration_seconds=25))
    assert ed.clip(a)["end"] == 25.0


def test_trim_no_change_adds_no_history(ed):
    a, = _seq(ed, 1)
    r = receipt(ed.call("trim_clips_tool", timeline_clip_ids=[a], source_start=0))
    assert r["changed"] is False
    assert ed.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# slice_clips_tool
# ---------------------------------------------------------------------------

def test_slice_one_clip_keep_both(ed):
    a, b = _seq(ed, 2)
    r = receipt(ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=1.5))
    call = ed.fake.calls[-1]
    assert call[:4] == ("slice", "KEEP_BOTH", [a], []) and call[4] == 1.5
    right = r["pieces"][0]["right"]
    assert right and pos(ed, right) == 1.5 and ed.clip(a)["end"] == 1.5
    one_step(ed)
    ed.undo()
    assert ed.clip(right) is None and ed.clip(a)["end"] == 4.0


def test_slice_at_playhead_and_keep_right_ripple(ed):
    a, b = _seq(ed, 2)
    ed.window.preview_thread.player.Position.return_value = 31  # frame 31 = 1.0 s
    receipt(ed.call("slice_clips_tool", timeline_clip_ids=[a], keep="right", ripple=True))
    assert ed.fake.calls[-1][1] == "KEEP_RIGHT" and ed.fake.calls[-1][5] is True
    assert (pos(ed, a), ed.clip(a)["start"], pos(ed, b)) == (0.0, 1.0, 3.0)


def test_slice_scope_all_skips_locked_tracks(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=0.0, end=4.0, layer=T1)
    b = ed.add_clip(f, position=0.0, end=4.0, layer=T2)
    ed.lock_track(T2)
    r = receipt(ed.call("slice_clips_tool", scope="all", at_seconds=2.0))
    assert ed.fake.calls[-1][2] == [a]
    assert [s["timeline_clip_id"] for s in r["skipped_locked"]] == [b]


def test_slice_refusals(ed):
    a, _b = _seq(ed, 2)
    refused(ed, ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=6.0), "not under")
    refused(ed, ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=0.0), "not under")
    refused(ed, ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=1, ripple=True), "one side is kept")
    refused(ed, ed.call("slice_clips_tool", at_seconds=1), "say what to slice")
    refused(ed, ed.call("slice_clips_tool", scope="track", track="3", at_seconds=1), "nothing to slice")
    ed.lock_track(T1)
    refused(ed, ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=1), "locked")
    refused(ed, ed.call("slice_clips_tool", scope="track", track="1", at_seconds=1), "locked")
    assert ed.fake.calls == []


# ---------------------------------------------------------------------------
# remove_gaps_tool
# ---------------------------------------------------------------------------

def test_remove_all_gaps_on_a_track(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=1.0, end=2.0)
    b = ed.add_clip(f, position=5.0, end=2.0)
    r = receipt(ed.call("remove_gaps_tool", track="1"))
    assert [pos(ed, a), pos(ed, b)] == [0.0, 2.0]
    assert r["closed"][0]["gaps"] == [[0.0, 1.0], [3.0, 5.0]]
    one_step(ed)
    ed.undo()
    assert [pos(ed, a), pos(ed, b)] == [1.0, 5.0]


def test_remove_first_gap_after_a_time_closes_a_deleted_clips_hole(ed):
    a, b, c = _seq(ed)
    ed.app.updates.delete(["clips", {"id": b}])  # delete_from_timeline_tool leaves a hole at 4-8
    ed.mark()
    receipt(ed.call("remove_gaps_tool", track="1", from_seconds=4.0, only_first=True))
    assert [pos(ed, a), pos(ed, c)] == [0.0, 4.0]
    one_step(ed)


def test_remove_gaps_nothing_to_do_and_refusals(ed):
    f = ed.add_file("video", duration=10.0)
    ed.add_clip(f, position=0.0, end=2.0, layer=T1)
    r = receipt(ed.call("remove_gaps_tool", track="1"))
    assert r["changed"] is False and ed.undo_steps_since_mark() == 0
    ed.add_clip(f, position=4.0, end=2.0, layer=T1)
    ed.add_clip(f, position=3.0, end=2.0, layer=T2)
    refused(ed, ed.call("remove_gaps_tool"), "say which track")
    ed.lock_track(T1)
    refused(ed, ed.call("remove_gaps_tool", track="1"), "locked")


def test_remove_gaps_picks_the_only_track_with_gaps(ed):
    f = ed.add_file("video", duration=10.0)
    ed.add_clip(f, position=0.0, end=2.0, layer=T1)
    b = ed.add_clip(f, position=4.0, end=2.0, layer=T1)
    ed.add_clip(f, position=0.0, end=9.0, layer=T2)
    receipt(ed.call("remove_gaps_tool"))
    assert pos(ed, b) == 2.0


# ---------------------------------------------------------------------------
# duplicate_clips_tool
# ---------------------------------------------------------------------------

def test_duplicate_after_the_original_with_new_effect_ids(ed):
    a, = _seq(ed, 1)
    eid = ed.add_effect(a, "Blur")
    r = receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a]))
    new = r["copies"][0][0]["timeline_clip_id"]
    assert pos(ed, new) == 4.0 and new != a
    assert ed.clip(new)["effects"][0]["id"] != eid
    one_step(ed)
    ed.undo()
    assert ed.clip(new) is None


def test_duplicate_three_copies_with_ripple_insert(ed):
    a, b = _seq(ed, 2)
    r = receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a], copies=3, ripple=True))
    starts = [c[0]["position"] for c in r["copies"]]
    assert starts == [4.0, 8.0, 12.0] and pos(ed, b) == 16.0
    one_step(ed)


def test_duplicate_refuses_to_cover_and_copies_inner_transitions(ed):
    a, b = _seq(ed, 2)
    refused(ed, ed.call("duplicate_clips_tool", timeline_clip_ids=[a]), "would overlap")
    ed.call("trim_clips_tool", timeline_clip_ids=[a], trim_end_seconds=-0.5, allow_overlap=True)  # a 0-4.5
    t = Transition()
    t.data = {"layer": T1, "position": 4.0, "start": 0.0, "end": 0.5, "title": "Transition", "type": "Mask"}
    t.save()
    ed.mark()
    r = receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a, b], to_track="2"))
    assert len(r["transition_ids"]) == 1
    assert Transition.get(id=r["transition_ids"][0]).data["position"] == 12.0
    assert {c["track"] for c in r["copies"][0]} == {2}
    ed.mark()
    ed.lock_track(T3)
    refused(ed, ed.call("duplicate_clips_tool", timeline_clip_ids=[a], to_track="3"), "locked")


# ---------------------------------------------------------------------------
# align_clips_tool
# ---------------------------------------------------------------------------

def test_align_starts_and_ends(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=2.0, end=4.0, layer=T1)
    b = ed.add_clip(f, position=5.0, end=2.0, layer=T2)
    receipt(ed.call("align_clips_tool", timeline_clip_ids=[a, b]))
    assert [pos(ed, a), pos(ed, b)] == [2.0, 2.0]
    one_step(ed)
    ed.mark()
    receipt(ed.call("align_clips_tool", timeline_clip_ids=[a, b], edge="end"))
    assert [pos(ed, a), pos(ed, b)] == [2.0, 4.0]
    ed.mark()
    receipt(ed.call("align_clips_tool", timeline_clip_ids=[a, b], to_seconds=10))
    assert [pos(ed, a), pos(ed, b)] == [10.0, 10.0]


def test_align_refusals_and_no_op(ed):
    a, b = _seq(ed, 2)
    refused(ed, ed.call("align_clips_tool", timeline_clip_ids=[a]), "at least two")
    refused(ed, ed.call("align_clips_tool", timeline_clip_ids=[a, b]), "would overlap")
    f = ed.add_file("video", duration=10.0)
    c = ed.add_clip(f, position=0.0, end=2.0, layer=T2)
    r = receipt(ed.call("align_clips_tool", timeline_clip_ids=[a, c]))
    assert r["changed"] is False and ed.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# slice_clip_at_playhead_tool (existing tool, fixed)
# ---------------------------------------------------------------------------

def test_slice_at_playhead_skips_locked_and_edge_items(ed):
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=0.0, end=4.0, layer=T1)
    ed.add_clip(f, position=0.0, end=4.0, layer=T2)            # locked
    ed.add_clip(f, position=2.0, end=3.0, layer=T3)            # starts at the playhead
    ed.lock_track(T2)
    ed.window.preview_thread.current_frame = 61                # 2.0 s
    out = ed.call("slice_clip_at_playhead_tool")
    assert out.startswith("Sliced 1 item(s)"), out
    assert ed.fake.calls[-1][:3] == ("slice", "KEEP_BOTH", [a])


def test_slice_at_playhead_with_nothing_sliceable_is_an_error(ed):
    f = ed.add_file("video", duration=10.0)
    ed.add_clip(f, position=0.0, end=4.0, layer=T2)
    ed.lock_track(T2)
    ed.window.preview_thread.current_frame = 31
    out = ed.call("slice_clip_at_playhead_tool")
    assert out.startswith("Error:") and ed.fake.calls == []


def _crossfaded(ed):
    """A 0-4, B 3.5-7.5, C 7-11 on track 1 with 0.5 s transitions at 3.5 and 7."""
    f = ed.add_file("video", duration=10.0)
    a = ed.add_clip(f, position=0.0, end=4.0)
    b = ed.add_clip(f, position=3.5, end=4.0)
    c = ed.add_clip(f, position=7.0, end=4.0)
    trans = []
    for at in (3.5, 7.0):
        t = Transition()
        t.data = {"layer": T1, "position": at, "start": 0.0, "end": 0.5, "title": "Transition", "type": "Mask"}
        t.save()
        trans.append(t.id)
    ed.mark()
    return a, b, c, trans


def test_ripple_trim_keeps_the_next_crossfade(ed):
    """Regression: ripple only moved items starting after the old end, so the
    clip overlapping the tail (crossfade partner) and its transition stayed put."""
    a, b, c, (t_ab, t_bc) = _crossfaded(ed)
    receipt(ed.call("trim_clips_tool", timeline_clip_ids=[b], trim_end_seconds=1, ripple=True))
    assert pos(ed, c) == 6.0 and Transition.get(id=t_bc).data["position"] == 6.0
    assert Transition.get(id=t_ab).data["position"] == 3.5 and pos(ed, b) == 3.5
    one_step(ed)


def test_add_clips_paints_stills_and_transitions_on_the_gui_thread(ed, monkeypatch):
    """Qt painting off a plain thread deadlocks (QFontCache vs GIL): the background-safe tool
    sends image/SVG reads and transition images to on_main and reads video directly."""
    from classes.editor_tools import timeline_edit
    hopped = []
    real_on_main = timeline_edit.on_main

    def recording_on_main(func, *args, **kw):
        hopped.append((getattr(func, "__name__", ""), args[0] if args and isinstance(args[0], str) else None))
        return real_on_main(func, *args, **kw)

    monkeypatch.setattr(timeline_edit, "on_main", recording_on_main)
    video = ed.add_file("video", duration=5.0)
    img = ed.add_file("image")
    receipt(ed.call("add_clips_to_timeline_tool", file_ids=[video, img], transition="fade", image_seconds=3))
    painted = [(name, arg) for name, arg in hopped if name in ("clip_json", "transition_reader_json")]
    assert ("clip_json", "/media/sample_image.jpg") in painted
    assert any(name == "transition_reader_json" and arg.endswith("fade.svg") for name, arg in painted)
    assert ("clip_json", "/media/sample_video.mp4") not in painted


def test_background_tools_wait_long_enough_for_each_hop(ed, monkeypatch):
    """A hop that outlives its wait still runs later, so giving up after 30 s left
    half an edit live (seen: duplicate opened the room but inserted no copies)."""
    from classes.editor_tools import timeline_edit
    timeouts = []
    real_on_main = timeline_edit.on_main

    def recording_on_main(func, *args, timeout=None):
        timeouts.append(timeout)
        return real_on_main(func, *args, timeout=timeout)

    monkeypatch.setattr(timeline_edit, "on_main", recording_on_main)
    a, b = _seq(ed, 2)
    receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a], copies=2, ripple=True))
    f = ed.add_file("video", duration=3.0)
    receipt(ed.call("add_clips_to_timeline_tool", file_ids=[f], track="2"))
    assert timeouts and all(t == timeline_edit.HOP_TIMEOUT for t in timeouts)


def test_duplicate_leaves_a_clips_incoming_crossfade_behind(ed):
    """Copying one clip of a crossfaded sequence must not copy its neighbour's transition
    (seen live: every copy faded in from black)."""
    a, b, c, (t_ab, t_bc) = _crossfaded(ed)
    r = receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[c]))
    assert r["transition_ids"] == []
    r = receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a, b], position_seconds=20))
    assert len(r["transition_ids"]) == 1
    assert Transition.get(id=r["transition_ids"][0]).data["position"] == 23.5
