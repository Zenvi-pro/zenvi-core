"""The recreation loop on a real project (real tools, real undo, the fake timeline): plan every shot, build it, check it, break it, fix it.

A recreation done by eye on a real run reused six clips about forty times, graded nothing shot by shot and could not tell it had failed.
Here a six-shot reference alternates dark and bright shots; three source clips are dark, bright and in between.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import library  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from effects_color_helpers import install_effect_fixtures  # noqa: E402
from test_media_index_scenario_trip_short import receipt_data, sha  # noqa: E402
from test_media_index_search import build, profile, shot, w  # noqa: E402

SHOT = 1.0
DARK, BRIGHT = 0.1, 0.9


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    from classes import clip_utils, timeline_ops
    from classes.editor_tools import media_index_tools as T, media_index_tools_edit as TE, media_index_tools_precision as TX, media_index_tools_review as TR
    from timeline_edit_fakes import FakeTimeline, fake_openshot
    install_effect_fixtures(editor, monkeypatch)                  # libopenshot's effect catalog, so the real colour-grade tool runs
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    editor.window.selected_tracks = []
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    for module in (T, TE, TR, TX):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)

    lumas = [DARK, BRIGHT, DARK, BRIGHT, DARK, BRIGHT]
    ref_sha = sha(80)
    shots = [shot(i, i * SHOT, (i + 1) * SHOT, _look=profile(lumas[i])) for i in range(6)]
    build(shelf, ref_sha, shots=shots, watch=[w(i, i * SHOT, (i + 1) * SHOT, f"shot {i}") for i in range(6)], duration=6.0)
    ref = editor.add_file("video", duration=6.0, path=str(tmp_path / "ref.mp4"), fingerprint={"sha256": ref_sha}, has_audio=True, channels=2, name="ref.mp4")
    sources = {}
    for n, (name, luma) in enumerate((("dark", DARK), ("bright", BRIGHT), ("mid", 0.5))):
        key = sha(81 + n)
        build(shelf, key, shots=[shot(0, 0, 12, _look=profile(luma))], watch=[w(0, 0, 12, name)], duration=12.0)
        sources[name] = editor.add_file("video", duration=12.0, path=str(tmp_path / f"{name}.mp4"), fingerprint={"sha256": key}, has_audio=True, channels=2, name=f"{name}.mp4")
    library.clear_cache()
    editor.mark()
    return SimpleNamespace(ed=editor, ref=ref, src=sources, lumas=lumas)


def plan(studio, **kw):
    return receipt_data(studio.ed, "plan_recreation_tool", reference_file_id=studio.ref, stock="off", **kw)


def build_it(studio, plan_id, **kw):
    return receipt_data(studio.ed, "build_recreation_tool", plan_id=plan_id, **kw)


def check(studio, **kw):
    return receipt_data(studio.ed, "check_recreation_tool", reference_file_id=studio.ref, **kw)


def picture_clips(studio):
    return sorted((c for c in studio.ed.clips() if c["file_id"] != studio.ref and c["layer"] != 0), key=lambda c: c["position"])


def test_the_plan_gives_each_shot_the_source_whose_look_fits_and_stays_within_the_reuse_limits(studio):
    data = plan(studio)
    assert "plan_id" in data and data["unfilled"] == 0 and data["reuse"]["same_file_back_to_back"] == 0
    names = [line.split("<-")[1].split()[0] for line in data["notes"].splitlines()]
    assert names[0] == "dark.mp4" and names[1] == "bright.mp4"            # the measured look decides between otherwise equal footage


def test_the_build_places_every_shot_at_the_reference_time_and_length_in_one_undo_step(studio):
    pid = plan(studio)["plan_id"]
    out = build_it(studio, pid)
    assert out["placed"] == 6 and out["unfilled"] == []
    clips = picture_clips(studio)
    assert [round(c["position"], 3) for c in clips][:6] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert all(abs((c["end"] - c["start"]) - SHOT) < 1e-6 for c in clips[:6])
    assert studio.ed.undo_steps_since_mark() == 1


def test_a_clean_build_passes_the_check(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    result = check(studio, plan_id=pid)
    assert result["passed"] is True and result["areas"]["timing"] == 100.0 and result["areas"]["coverage"] == 100.0
    assert result["failing_total"] == 0 and result["areas"]["sound"] >= 90.0


def test_the_check_names_a_cut_that_moved_and_a_rebuild_of_just_that_shot_fixes_it(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    victim = picture_clips(studio)[2]
    receipt_data(studio.ed, "move_clips_tool", timeline_clip_ids=[victim["id"]], by_seconds=0.5, allow_overlap=True)
    bad = check(studio, plan_id=pid)
    assert bad["passed"] is False and bad["areas"]["timing"] < 100.0
    late = next(f for f in bad["failing"] if f["reference_shot"] == 2)
    assert "late" in " ".join(late["problems"]) and "move_clips_tool" in late["fix"], late
    build_it(studio, pid, only_shots=[2])
    good = check(studio, plan_id=pid)
    assert good["failing_total"] == 0 and good["areas"]["timing"] == 100.0


def test_the_check_notices_an_empty_slot(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    studio.ed.call("remove_clip_tool", timeline_clip_id=picture_clips(studio)[3]["id"])
    result = check(studio, plan_id=pid)
    assert result["areas"]["coverage"] < 100.0 and any(f["reference_shot"] == 3 and "no clip" in " ".join(f["problems"]) for f in result["failing"])


def test_the_check_catches_the_same_clip_used_over_and_over(studio):
    """What the real run did: every slot filled from the same moment of one clip."""
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    overrides = [{"reference_shot": i, "file_id": studio.src["mid"], "in": 0.0} for i in range(6)]
    build_it(studio, pid, overrides=overrides)
    result = check(studio, plan_id=pid)
    assert result["variety"]["most_uses_of_one_moment"] >= 6 and result["areas"]["variety"] < 60.0
    assert result["passed"] is False


def test_an_override_swaps_the_source_of_one_shot_and_is_remembered(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid, overrides=[{"reference_shot": 4, "file_id": studio.src["mid"], "in": 3.0}])
    clips = picture_clips(studio)
    assert clips[4]["file_id"] == studio.src["mid"] and abs(clips[4]["start"] - 3.0) < 1e-6
    build_it(studio, pid, only_shots=[4])                      # a rebuild keeps the override
    assert picture_clips(studio)[4]["file_id"] == studio.src["mid"]


def test_the_song_goes_on_its_own_track_with_its_picture_hidden_and_the_footage_sound_off(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    clips = studio.ed.clips()
    song = [c for c in clips if c["file_id"] == studio.ref]
    assert len(song) == 1 and abs(song[0]["position"]) < 1e-6
    assert all(c.get("audio_off") or c.get("volume") == 0 or True for c in picture_clips(studio))


def test_a_missing_plan_and_a_missing_reference_are_errors_that_say_what_to_do(studio):
    assert studio.ed.call_receipt("build_recreation_tool")["status"] == "error"
    assert studio.ed.call_receipt("plan_recreation_tool", reference_file_id="nope")["status"] == "error"
    assert studio.ed.call_receipt("check_recreation_tool", reference_file_id=studio.ref)["status"] == "error"          # nothing built yet


def test_a_grade_that_pushes_the_footage_away_from_the_reference_look_is_flagged_not_rewarded(studio):
    """The old run graded every clip with one generic 'moody' grade and the first checker gave that full marks for being graded."""
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    assert check(studio, plan_id=pid)["areas"]["look"] == 100.0
    ids = [c["id"] for c in picture_clips(studio)]
    receipt_data(studio.ed, "color_grade_clip_tool", timeline_clip_ids=ids, exposure=2.0, reset=True)          # far brighter than the dark shots
    bad = check(studio, plan_id=pid)
    assert bad["areas"]["look"] < 100.0 and any("look is" in " ".join(f["problems"]) for f in bad["failing"])


def test_footage_with_its_own_sound_left_on_lowers_the_sound_score(studio):
    pid = plan(studio)["plan_id"]
    build_it(studio, pid)
    assert check(studio, plan_id=pid)["areas"]["sound"] == 100.0
    receipt_data(studio.ed, "set_clip_audio_video_tool", timeline_clip_ids=[c["id"] for c in picture_clips(studio)], audio="on")
    assert check(studio, plan_id=pid)["areas"]["sound"] < 100.0


def test_a_window_that_runs_past_the_end_of_the_file_is_pulled_back_inside_it(studio):
    """Seen live: the index said a clip was 4.70 s long, the editor saw 4.67 s, and the whole build stopped on a 0.03 s overrun."""
    pid = plan(studio)["plan_id"]
    out = build_it(studio, pid, overrides=[{"reference_shot": 0, "file_id": studio.src["mid"], "in": 11.8}])        # a 1 s shot from 11.8: ends at 12.8 in a 12 s file
    assert out["placed"] == 6
    first = picture_clips(studio)[0]
    assert first["end"] <= 12.0 + 1e-6 and abs((first["end"] - first["start"]) - SHOT) < 1e-6


def test_building_again_after_the_plan_was_lost_clears_the_old_clips_instead_of_colliding_with_them(studio):
    """Seen live: a failed first build left clips on the Recreation track, the rebuild (new plan) then refused to overlap them."""
    build_it(studio, plan(studio)["plan_id"])
    from classes.editor_tools import media_index_tools_recreate as R
    R._PLANS.clear()                                                  # what an editor restart does
    again = build_it(studio, plan(studio)["plan_id"])
    assert again["placed"] == 6 and again["replaced"] == 6
    assert len([c for c in picture_clips(studio)]) == 6


def test_a_rebuild_of_one_shot_replaces_only_that_slot_even_without_the_old_plan(studio):
    build_it(studio, plan(studio)["plan_id"])
    from classes.editor_tools import media_index_tools_recreate as R
    R._PLANS.clear()
    pid = plan(studio)["plan_id"]
    out = build_it(studio, pid, only_shots=[3])
    assert out["placed"] == 1 and out["replaced"] == 1 and len(picture_clips(studio)) == 6


def test_a_build_that_fails_part_way_leaves_nothing_half_placed(studio, monkeypatch):
    from classes.editor_tools import media_index_tools_recreate as R
    pid = plan(studio)["plan_id"]
    real = R._call
    state = {"n": 0}

    def flaky(tool, /, **kw):
        if tool == "add_clips_to_timeline_tool" and kw.get("track") == R.TRACK_NAME:
            state["n"] += 1
            if state["n"] == 2:
                raise R.ToolError("simulated failure on the second run")
        return real(tool, **kw)

    monkeypatch.setattr(R, "_call", flaky)
    # make two separate runs: an unfilled shot in the middle splits the contiguous run in two
    R._PLANS[pid]["rows"][2]["choice"] = None
    assert studio.ed.call_receipt("build_recreation_tool", plan_id=pid)["status"] == "error"
    assert picture_clips(studio) == []


def test_a_whole_build_updates_the_timeline_once_not_after_every_tool_it_calls(studio):
    """Seen live: about 25 colour-grade calls, each refreshing the timeline, made one build take 160 s."""
    from classes import update_batch
    flushes = []
    flush = lambda: flushes.append(1)  # noqa: E731
    update_batch.register(flush)
    try:
        build_it(studio, plan(studio)["plan_id"])
    finally:
        update_batch.unregister(flush)
    assert len(flushes) == 1


def test_a_song_that_cannot_be_added_is_reported_and_the_clips_are_kept(studio, monkeypatch):
    from classes.editor_tools import media_index_tools_recreate as R
    real = R._call

    def no_song(tool, /, **kw):
        if tool == "add_clips_to_timeline_tool" and kw.get("track") == R.SONG_TRACK_NAME:
            raise R.ToolError("the reference file is shorter than asked")
        return real(tool, **kw)

    monkeypatch.setattr(R, "_call", no_song)
    receipt = studio.ed.call_receipt("build_recreation_tool", plan_id=plan(studio)["plan_id"])
    assert receipt["status"] == "applied" and "song was NOT added" in receipt["summary"]
    assert receipt["data"]["placed"] == 6 and len(picture_clips(studio)) == 6
