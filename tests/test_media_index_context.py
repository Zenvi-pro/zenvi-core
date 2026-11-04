"""What is under, above and around a clip, and what an edit to it would move."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import context as C  # noqa: E402
from classes.media_index.review import TimelineClip  # noqa: E402


def clip(cid, layer, start, end, kind="video", role=None, gain=None, name=None, audio=None):
    return TimelineClip(id=cid, name=name or cid, layer=layer, kind=kind, start=start, end=end, role=role, gain_db=gain,
                        has_audio=(kind == "audio" or role is not None) if audio is None else audio)


def ids(rows):
    return [r["id"] for r in rows]


# ============================ the surroundings ============================
def test_clips_above_and_below_are_listed_with_their_overlap_top_of_the_stack_first():
    clips = [clip("hero", 2, 10, 20), clip("top", 4, 12, 16), clip("mid", 3, 8, 14), clip("under", 1, 0, 30), clip("far", 1, 40, 50)]
    ctx = C.clip_context("hero", clips)
    assert ids(ctx["above"]) == ["top", "mid"] and ctx["above"][0]["overlap"] == [12.0, 16.0] and ctx["above"][1]["overlap"] == [10.0, 14.0]
    assert ids(ctx["below"]) == ["under"] and ctx["below"][0]["overlap"] == [10.0, 20.0]
    assert "far" not in ids(ctx["above"] + ctx["below"]), "no overlap in time, not around it"


def test_a_clip_on_its_own_track_is_not_above_or_below():
    ctx = C.clip_context("a", [clip("a", 2, 0, 10), clip("b", 2, 10, 20)])
    assert ctx["above"] == [] and ctx["below"] == []


def test_only_picture_clips_stack_over_a_picture_and_sound_is_listed_separately():
    clips = [clip("pic", 2, 0, 10), clip("song", 3, 0, 10, kind="audio", role="music", gain=-18.0), clip("title", 4, 2, 5, kind="title", audio=False)]
    ctx = C.clip_context("pic", clips)
    assert ids(ctx["above"]) == ["title"] and ctx["above"][0]["hides"] is True
    assert ids(ctx["sound_with"]) == ["song"] and ctx["sound_with"][0]["role"] == "music" and ctx["sound_with"][0]["level_db"] == -18.0


def test_an_opaque_clip_above_hides_what_is_under_it_and_a_faded_one_does_not():
    clips = [clip("bottom", 1, 0, 20), clip("cover", 2, 5, 12), clip("ghost", 3, 15, 20)]
    ctx = C.clip_context("bottom", clips, opacity={"cover": 1.0, "ghost": 0.4})
    assert ctx["visible"]["hidden_by_clips_above"] == [[5.0, 12.0]] and ctx["visible"]["hidden_seconds"] == 7.0 and ctx["visible"]["fully_hidden"] is False
    ghost = next(r for r in ctx["above"] if r["id"] == "ghost")
    assert ghost["hides"] is False and ghost["opacity"] == 0.4


def test_overlapping_covers_are_counted_once_and_a_full_cover_means_fully_hidden():
    clips = [clip("bottom", 1, 0, 10), clip("a", 2, 0, 6), clip("b", 3, 4, 10)]
    ctx = C.clip_context("bottom", clips)
    assert ctx["visible"]["hidden_by_clips_above"] == [[0.0, 10.0]] and ctx["visible"]["hidden_seconds"] == 10.0 and ctx["visible"]["fully_hidden"] is True


def test_a_caption_above_does_not_hide_the_picture_and_audio_has_no_visibility():
    ctx = C.clip_context("pic", [clip("pic", 1, 0, 10), clip("cap", 2, 0, 10, kind="caption", audio=False)])
    assert ctx["visible"]["fully_hidden"] is False and ctx["above"][0]["hides"] is False
    assert C.clip_context("song", [clip("song", 1, 0, 10, kind="audio", role="music")])["visible"] is None


# ============================ neighbours and seams ============================
def test_the_neighbours_on_the_track_and_the_seam_between_them():
    clips = [clip("a", 1, 0, 5), clip("b", 1, 5, 10), clip("c", 1, 10.5, 15), clip("d", 2, 0, 15)]
    ctx = C.clip_context("b", clips)
    assert ctx["before"]["id"] == "a" and ctx["before"]["kind"] == "hard cut" and ctx["before"]["gap_seconds"] == 0.0
    assert ctx["after"]["id"] == "c" and ctx["after"]["kind"] == "gap" and ctx["after"]["gap_seconds"] == 0.5


def test_an_overlap_and_a_transition_are_told_apart_from_a_hard_cut():
    clips = [clip("a", 1, 0, 5.5), clip("b", 1, 5, 10), clip("c", 1, 10, 15)]
    assert C.clip_context("b", clips)["before"]["kind"] == "overlap"
    tr = [C.Transition(id="t1", layer=1, start=9.5, end=10.5, title="Fade")]
    after = C.clip_context("b", clips, transitions=tr)["after"]
    assert after["kind"] == "transition: Fade"
    elsewhere = [C.Transition(id="t2", layer=2, start=9.5, end=10.5, title="Fade")]
    assert C.clip_context("b", clips, transitions=elsewhere)["after"]["kind"] == "hard cut"


def test_the_first_and_last_clip_have_one_neighbour_missing():
    clips = [clip("a", 1, 0, 5), clip("b", 1, 5, 10)]
    assert C.clip_context("a", clips)["before"] is None and C.clip_context("b", clips)["after"] is None


# ============================ links, tracks, sound ============================
def test_links_are_reported_as_unavailable_when_the_project_has_none_and_listed_when_it_does():
    clips = [clip("v", 2, 0, 10), clip("a", 1, 0, 10, kind="audio", role="speech")]
    off = C.clip_context("v", clips)
    assert off["links"]["available"] is False and "pro editing tools" in off["links"]["note"]
    on = C.clip_context("v", clips, links={"v": "g1", "a": "g1"})
    assert on["links"] == {"available": True, "group": "g1", "partners": [{"id": "a", "name": "a", "layer": 1, "kind": "audio", "start": 0.0, "end": 10.0}]}
    alone = C.clip_context("v", clips, links={"v": "", "a": "g2"})
    assert alone["links"]["group"] is None and alone["links"]["partners"] == []


def test_the_track_facts_and_automated_volume_are_shown():
    tracks = {2: C.Track(layer=2, label="B-roll", locked=True, sync_locked=False)}
    clips = [clip("v", 2, 0, 10), clip("m", 1, 0, 10, kind="audio", role="music", gain=None)]
    ctx = C.clip_context("v", clips, tracks)
    assert ctx["track"] == {"layer": 2, "label": "B-roll", "locked": True, "sync_locked": False}
    assert ctx["sound_with"][0]["automated_volume"] is True and ctx["sound_with"][0]["level_db"] is None


def test_an_unknown_clip_is_an_error():
    with pytest.raises(KeyError):
        C.clip_context("nope", [clip("a", 1, 0, 5)])


# ============================ what an edit would move ============================
def timeline():
    return [clip("a", 1, 0, 5), clip("b", 1, 5, 10), clip("c", 1, 10, 15), clip("d", 1, 15, 20), clip("cover", 2, 6, 9), clip("song", 3, 0, 20, kind="audio", role="music")]


def moved(pred):
    return {s["id"]: (s["from"], s["to"]) for s in pred["shifted"]}


def test_deleting_a_clip_closes_the_gap_on_its_own_track_only():
    pred = C.predict_edit("b", "delete", 0.0, timeline())
    assert moved(pred) == {"c": (10.0, 5.0), "d": (15.0, 10.0)} and pred["shift"] == -5.0 and pred["shifted_count"] == 2
    assert ids(pred["stay_put"]) == ["cover", "song"] and any("keep their place" in n for n in pred["notes"])
    assert pred["sync_lock_known"] is False and pred["tracks_that_move"] == [1] and any("no sync-lock setting" in n for n in pred["notes"])


@pytest.mark.parametrize("op,seconds,shift", [("trim_end", 1.5, -1.5), ("trim_start", 2.0, -2.0), ("lengthen", 3.0, 3.0)])
def test_trimming_or_lengthening_shifts_the_later_clips_by_that_much(op, seconds, shift):
    pred = C.predict_edit("b", op, seconds, timeline())
    assert pred["shift"] == shift and moved(pred)["c"] == (10.0, 10.0 + shift) and "a" not in moved(pred)


def test_sync_locked_tracks_move_too_and_locked_tracks_never_do():
    clips = timeline() + [clip("x", 4, 12, 18), clip("y", 5, 12, 18), clip("e", 6, 1, 4)]
    tracks = {1: C.Track(1, sync_locked=True), 3: C.Track(3, sync_locked=True), 4: C.Track(4, sync_locked=True), 5: C.Track(5, sync_locked=False),
              6: C.Track(6, sync_locked=True, locked=True)}
    pred = C.predict_edit("b", "delete", 0.0, clips, tracks)
    assert {"c", "d", "x"} <= set(moved(pred)) and "y" not in moved(pred), "track 5 is not sync-locked"
    assert "e" not in moved(pred) and pred["sync_lock_known"] is True and pred["tracks_that_move"] == [1, 3, 4]
    assert ids(pred["stay_put"]) == ["cover"], "track 2 is not sync-locked and sits over the clip: it keeps its place (track 5 does not overlap it, so it is not listed)"


def test_a_track_with_no_value_counts_as_locked_once_the_project_has_the_setting():
    tracks = {1: C.Track(1, sync_locked=True), 2: C.Track(2, sync_locked=None)}
    layers, known = C.sync_locked_layers(tracks, 1)
    assert layers == [1, 2] and known is True
    assert C.sync_locked_layers({1: C.Track(1), 2: C.Track(2)}, 1) == ([1], False)


def test_linked_clips_left_behind_are_flagged():
    clips = [clip("v1", 2, 0, 5), clip("v2", 2, 5, 10), clip("v3", 2, 10, 15), clip("a3", 1, 10, 15, kind="audio", role="speech")]
    links = {"v3": "g", "a3": "g"}
    pred = C.predict_edit("v2", "delete", 0.0, clips, {}, links)
    assert pred["split_links"] == [{"clip": "v3", "partner": "a3", "partner_name": "a3"}] and any("out of sync" in n for n in pred["notes"])
    assert C.predict_edit("v2", "delete", 0.0, clips, {}, None)["split_links"] == []


def test_deleting_one_of_a_linked_pair_leaves_the_other():
    clips = [clip("v", 2, 0, 5), clip("a", 1, 0, 5, kind="audio", role="speech")]
    pred = C.predict_edit("v", "delete", 0.0, clips, {}, {"v": "g", "a": "g"})
    assert pred["split_links"] == [{"clip": "v", "partner": "a", "partner_name": "a"}]


def test_cuts_that_land_on_a_beat_now_but_not_after_are_counted():
    beats = [i * 0.5 for i in range(0, 41)]
    pred = C.predict_edit("b", "trim_end", 0.25, timeline(), beats=beats)
    assert pred["cuts_off_the_beat"] == 2 and any("no longer land on one" in n for n in pred["notes"])
    assert C.predict_edit("b", "trim_end", 0.5, timeline(), beats=beats)["cuts_off_the_beat"] == 0, "half a beat keeps them on the grid"
    assert C.predict_edit("b", "trim_end", 0.25, timeline())["cuts_off_the_beat"] == 0, "no music, no beat to lose"


def test_cuts_that_would_start_landing_inside_speech_are_counted():
    pred = C.predict_edit("a", "lengthen", 2.0, [clip("a", 1, 0, 5), clip("b", 1, 5, 10)], speech=[(6.0, 9.0)])
    assert pred["cuts_into_speech"] == 1 and any("middle of speech" in n for n in pred["notes"])
    assert C.predict_edit("a", "lengthen", 2.0, [clip("a", 1, 0, 5), clip("b", 1, 5, 10)], speech=[(0.0, 4.0)])["cuts_into_speech"] == 0


@pytest.mark.parametrize("op,seconds,message", [("explode", 1.0, "unknown edit"), ("trim_end", 0.0, "how many seconds"), ("trim_end", 5.0, "remove the whole clip"),
                                                 ("lengthen", -1.0, "how many seconds")])
def test_a_bad_request_is_refused(op, seconds, message):
    with pytest.raises(ValueError, match=message):
        C.predict_edit("a", op, seconds, [clip("a", 1, 0, 5)])


# ============================ the tool ============================
def call(**kw):
    out = REGISTRY["get_clip_context_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def facts(monkeypatch):
    from classes.editor_tools import media_index_tools_precision as P
    clips = timeline()
    monkeypatch.setattr(P, "_timeline_facts", lambda: (clips, {1: C.Track(1, sync_locked=True)}, [], {"cover": 1.0}, None))
    monkeypatch.setattr(P, "_timeline_beats", lambda c: ([i * 0.5 for i in range(41)], []))


def test_the_tool_describes_a_clip_and_changes_nothing(facts):
    head, r = call(timeline_clip_id="b")
    assert r["changed"] is False and r["prediction"] is None and ids(r["context"]["above"]) == ["cover"]
    assert r["context"]["before"]["id"] == "a" and r["context"]["after"]["id"] == "c" and "1 above" in head and "playing sound" in head


def test_the_tool_predicts_an_edit_when_asked(facts):
    head, r = call(timeline_clip_id="b", if_edit="delete")
    assert r["prediction"]["shifted_count"] == 2 and r["prediction"]["shift"] == -5.0 and "2 clip(s) would shift by -5 s" in head
    _, trimmed = call(timeline_clip_id="b", if_edit="trim_end", seconds=0.25)
    assert trimmed["prediction"]["cuts_off_the_beat"] == 2


def test_the_tool_refuses_what_it_cannot_do(facts):
    assert REGISTRY["get_clip_context_tool"].func(timeline_clip_id="zzz").startswith("Error")
    assert "how many seconds" in REGISTRY["get_clip_context_tool"].func(timeline_clip_id="b", if_edit="lengthen")


def test_opacity_comes_from_the_alpha_keyframes():
    from classes.editor_tools.media_index_tools_precision import _opacity_of
    assert _opacity_of(None) == 1.0 and _opacity_of({}) == 1.0 and _opacity_of({"Points": []}) == 1.0
    assert _opacity_of({"Points": [{"co": {"X": 1, "Y": 0.0}}, {"co": {"X": 30, "Y": 1.0}}]}) == 0.5
    assert _opacity_of({"Points": [{"co": {"X": 1, "Y": 3.0}}]}) == 1.0 and _opacity_of({"Points": [{"co": {"X": 1, "Y": "x"}}]}) == 1.0


def test_a_clip_that_starts_inside_the_edited_one_is_not_one_of_the_clips_after_it():
    pred = C.predict_edit("b", "delete", 0.0, [clip("a", 1, 0, 5), clip("b", 1, 5, 10), clip("x", 1, 9, 12), clip("c", 1, 10, 15)])
    assert moved(pred) == {"c": (10.0, 5.0)}, "x overlaps b (a transition-style overlap) and is not shifted by this rule"


def test_a_later_clip_on_a_locked_track_stays_where_it_is():
    clips = [clip("a", 1, 0, 5), clip("b", 1, 5, 10), clip("lockedlater", 6, 12, 18), clip("c", 1, 10, 15)]
    tracks = {1: C.Track(1, sync_locked=True), 6: C.Track(6, sync_locked=True, locked=True)}
    assert set(moved(C.predict_edit("b", "delete", 0.0, clips, tracks))) == {"c"}


def test_a_linked_pair_that_moves_together_is_not_split():
    clips = [clip("v1", 2, 0, 5), clip("v2", 2, 5, 10), clip("v3", 2, 10, 15), clip("a3", 1, 10, 15, kind="audio", role="speech")]
    tracks = {1: C.Track(1, sync_locked=True), 2: C.Track(2, sync_locked=True)}
    pred = C.predict_edit("v2", "delete", 0.0, clips, tracks, {"v3": "g", "a3": "g"})
    assert set(moved(pred)) == {"v3", "a3"} and pred["split_links"] == []


# ============================ gathering the timeline for the tool ============================
class Obj:
    def __init__(self, data):
        self.data = data


def test_the_tool_reads_tracks_transitions_opacity_and_links_from_the_project(monkeypatch):
    from types import SimpleNamespace
    from classes.editor_tools import media_index_tools_precision as P
    import classes.editor_tools.media_index_tools_review as review_tools
    objs = {"a": Obj({"alpha": {"Points": [{"co": {"X": 1, "Y": 0.25}}]}, "link_group_id": "g1"}), "b": Obj({"link_group_id": "g1"}), "c": Obj({})}
    project = {"layers": [{"number": 1, "label": "V1", "lock": True, "sync_locked": False}, {"number": 2, "name": "A1"}, {"number": "bad"}],
               "effects": [{"id": "t1", "layer": 1, "position": 4.0, "end": 5.0, "title": "Fade"}, "junk"]}
    monkeypatch.setattr(review_tools, "build_timeline", lambda: ([clip("a", 1, 0, 5)], None, objs))
    monkeypatch.setattr(P, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda key, default=None: project.get(key, default))))
    clips, tracks, transitions, opacity, links = P._timeline_facts()
    assert tracks[1] == C.Track(1, "V1", True, False) and tracks[2] == C.Track(2, "A1", False, None) and set(tracks) == {1, 2}
    assert transitions == [C.Transition("t1", 1, 4.0, 5.0, "Fade")] and opacity == {"a": 0.25, "b": 1.0, "c": 1.0}
    assert links == {"a": "g1", "b": "g1", "c": ""}


def test_a_project_with_no_link_groups_has_no_links(monkeypatch):
    from types import SimpleNamespace
    from classes.editor_tools import media_index_tools_precision as P
    import classes.editor_tools.media_index_tools_review as review_tools
    monkeypatch.setattr(review_tools, "build_timeline", lambda: ([], None, {"a": Obj({}), "b": Obj({"link_group_id": ""})}))
    monkeypatch.setattr(P, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda key, default=None: [])))
    assert P._timeline_facts()[4] is None


def test_an_edit_to_a_clip_on_a_locked_track_would_be_refused_and_moves_nothing():
    clips = [clip("a", 1, 0, 5), clip("b", 1, 5, 10), clip("c", 1, 10, 15)]
    pred = C.predict_edit("b", "delete", 0.0, clips, {1: C.Track(1, locked=True)})
    assert pred["refused_locked_track"] is True and pred["shifted"] == [] and pred["tracks_that_move"] == [] and any("locked" in n for n in pred["notes"])
    assert C.predict_edit("b", "delete", 0.0, clips, {1: C.Track(1)})["refused_locked_track"] is False
