"""The edit tools on a real project: real store, real undo, real handlers, exactly as the chat calls them."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import media_index_tools as T, media_index_tools_edit as TE, media_index_tools_review as TR  # noqa: E402
from classes.media_index import library  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_search import build, profile, shot, w  # noqa: E402

SHA_V, SHA_M, SHA_S1, SHA_S2 = "a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32
PICTURE, MUSIC, VOICE = 1000000, 2000000, 3000000


@pytest.fixture
def world(editor, tmp_path, monkeypatch):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    for module in (T, TE, TR):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    for number, label in ((PICTURE, "Picture"), (MUSIC, "Music"), (VOICE, "Voice")):
        editor.add_track(number, label)
    return SimpleNamespace(editor=editor, shelf=shelf)


def add_music_index(shelf, sha=SHA_M, bpm=120.0, level=-20.0):
    beats = [round(1.0 + 0.5 * i, 3) for i in range(120)]
    shelf.set_source(sha, duration=76.4, media_type="audio")
    windows = [{"start": float(a), "end": float(a + 5), "rms_db": level} for a in range(0, 70, 5)]
    shelf.write_json(sha, "audio.json", {"tempo": {"bpm": bpm, "beats": beats}, "windows": windows, "music": {"downbeats": beats[::4], "arc": [0.5] * 10},
                                         "loudness": {"integrated_lufs": -14.0}})
    shelf.set_layer(sha, "audio", version=2, status="ready")


def picture_pair(world, a_end=6.9):
    """Two adjacent clips of one video (source 5.0-a_end, then 12.0-15.1) and a music bed over both."""
    ed = world.editor
    video = ed.add_file("video", duration=24.5, fingerprint={"sha256": SHA_V})
    music = ed.add_file("audio", fingerprint={"sha256": SHA_M}, ai_metadata={"analyzed": True})
    add_music_index(world.shelf)
    a = ed.add_clip(video, position=0.0, layer=PICTURE, start=5.0, end=a_end)
    b = ed.add_clip(video, position=a_end - 5.0, layer=PICTURE, start=12.0, end=15.1)
    bed = ed.add_clip(music, position=0.0, layer=MUSIC, start=0.0, end=40.0)
    ed.mark()
    return a, b, bed


def snapshot(ed, *ids):
    return {i: {k: ed.clip(i)[k] for k in ("position", "start", "end")} for i in ids}


# ============================ sync_cuts_to_beats_tool ============================
def test_the_cut_moves_onto_the_beat_in_one_undo_step_and_undo_puts_it_back(world):
    ed = world.editor
    a, b, bed = picture_pair(world)
    before = snapshot(ed, a, b, bed)
    receipt = ed.call_receipt("sync_cuts_to_beats_tool")
    assert receipt["status"] == "applied", receipt
    after = snapshot(ed, a, b, bed)
    assert after[a]["end"] == pytest.approx(7.0, abs=0.02) and after[a]["start"] == before[a]["start"] and after[a]["position"] == before[a]["position"]
    assert after[b]["position"] == pytest.approx(2.0, abs=0.02) and after[b]["start"] == pytest.approx(12.1, abs=0.02) and after[b]["end"] == before[b]["end"]
    assert after[bed] == before[bed], "the music is untouched"
    assert ed.undo_steps_since_mark() == 1, "one user intent, one undo step"
    ed.undo()
    assert snapshot(ed, a, b, bed) == before


def test_a_dry_run_changes_nothing_and_adds_no_undo_step(world):
    ed = world.editor
    a, b, bed = picture_pair(world)
    before = snapshot(ed, a, b, bed)
    receipt = ed.call_receipt("sync_cuts_to_beats_tool", dry_run=True)
    assert receipt["status"] in ("unchanged", "applied") and snapshot(ed, a, b, bed) == before and ed.undo_steps_since_mark() == 0
    assert receipt["data"]["dry_run"] is True and len(receipt["data"]["moves"]) == 1


def test_a_locked_track_is_refused_and_nothing_moves(world):
    ed = world.editor
    a, b, bed = picture_pair(world)
    ed.lock_track(PICTURE)
    before = snapshot(ed, a, b, bed)
    receipt = ed.call_receipt("sync_cuts_to_beats_tool")
    assert receipt["status"] in ("refused", "error") and "locked" in receipt["summary"]
    assert snapshot(ed, a, b, bed) == before and ed.undo_steps_since_mark() == 0


def test_a_cut_already_on_the_beat_is_unchanged_with_no_undo_step(world):
    ed = world.editor
    a, b, bed = picture_pair(world, a_end=7.0)                # the cut is at 2.0 s, on a beat
    receipt = ed.call_receipt("sync_cuts_to_beats_tool")
    assert receipt["status"] == "unchanged" and ed.undo_steps_since_mark() == 0


def test_without_music_the_tool_says_so_and_changes_nothing(world):
    ed = world.editor
    video = ed.add_file("video", duration=24.5, fingerprint={"sha256": SHA_V})
    ed.add_clip(video, position=0.0, layer=PICTURE, start=5.0, end=6.9)
    ed.mark()
    receipt = ed.call_receipt("sync_cuts_to_beats_tool")
    assert receipt["status"] in ("refused", "error") and "no music" in receipt["summary"] and ed.undo_steps_since_mark() == 0


# ============================ the edit brief ============================
def test_the_brief_is_saved_in_the_project_without_an_undo_step_and_read_back(world):
    ed = world.editor
    receipt = ed.call_receipt("set_edit_brief_tool", brief={"form": "YouTube vlog", "target_seconds": 60, "bans": ["no AI"]})
    assert receipt["status"] in ("applied", "unchanged"), receipt
    assert ed.get("edit_brief")["form"] == "YouTube vlog" and ed.undo_steps_since_mark() == 0
    got = ed.call_receipt("get_edit_brief_tool")
    assert got["data"]["brief"] == {"form": "YouTube vlog", "target_seconds": 60, "bans": ["no AI"]}
    ed.call_receipt("set_edit_brief_tool", brief={"form": None, "done": ["intro"]})
    assert ed.call_receipt("get_edit_brief_tool")["data"]["brief"] == {"target_seconds": 60, "bans": ["no AI"], "done": ["intro"]}
    assert ed.undo_steps_since_mark() == 0


# ============================ balance_mix_tool with the real volume and ducking handlers ============================
def add_voice(world, sha, level, position, seconds=6.0):
    ed = world.editor
    cues = [{"start": 0.5, "end": seconds - 0.5, "text": "hello there everyone"}]
    f = ed.add_file("audio", fingerprint={"sha256": sha}, duration=30.0, ai_metadata={"analyzed": True, "transcript_cues": cues, "has_speech": True})
    world.shelf.set_source(sha, duration=30.0, media_type="audio")
    world.shelf.write_json(sha, "audio.json", {"windows": [{"start": float(a), "end": float(a + 5), "rms_db": level} for a in range(0, 25, 5)], "tempo": None})
    world.shelf.set_layer(sha, "audio", version=2, status="ready")
    return ed.add_clip(f, position=position, layer=VOICE, start=0.0, end=seconds)


def volume_points(ed, clip_id):
    return list(((ed.clip(clip_id) or {}).get("volume") or {}).get("Points") or [])


@pytest.fixture
def mix_world(world, tmp_path, monkeypatch):
    """Two voices at different levels over a music bed, and a rendered-mix stand-in that measures like a real file."""
    ed = world.editor
    s1 = add_voice(world, SHA_S1, -18.0, 0.0)
    s2 = add_voice(world, SHA_S2, -26.0, 8.0)
    music = ed.add_file("audio", fingerprint={"sha256": SHA_M}, ai_metadata={"analyzed": True})
    add_music_index(world.shelf, level=-20.0)
    bed = ed.add_clip(music, position=0.0, layer=MUSIC, start=0.0, end=20.0)
    ed.mark()
    mixes = []

    def fake_render(start, end):
        folder = tmp_path / f"mix{len(mixes)}"
        folder.mkdir()
        path = folder / "mix.mp3"
        path.write_bytes(b"x")
        mixes.append((start, end))
        return str(path), ""

    measured = [{"integrated_lufs": -21.0, "true_peak_db": -9.0, "lra": 5.0}, {"integrated_lufs": -14.4, "true_peak_db": -3.0, "lra": 5.0}]
    monkeypatch.setattr(TE, "render_timeline_mix", fake_render)
    from classes.media_index import audio as au
    monkeypatch.setattr(au, "measure_loudness", lambda path: measured.pop(0))
    return SimpleNamespace(world=world, s1=s1, s2=s2, bed=bed, mixes=mixes)


def test_the_whole_mix_pass_is_one_undo_step_and_undo_restores_every_volume(mix_world):
    ed = mix_world.world.editor
    ids = (mix_world.s1, mix_world.s2, mix_world.bed)
    before = {i: volume_points(ed, i) for i in ids}
    receipt = ed.call_receipt("balance_mix_tool")
    assert receipt["status"] == "applied", receipt
    data = receipt["data"]
    assert {a["id"] for a in data["voices"]["adjust"]} == {mix_world.s1, mix_world.s2}
    assert data["loudness"]["before"]["integrated_lufs"] == -21.0 and data["loudness"]["after"]["integrated_lufs"] == -14.4
    assert abs(data["loudness"]["delta_db"] - 7.0) < 0.1 and mix_world.mixes == [(0.0, 20.0), (0.0, 20.0)]
    assert any(volume_points(ed, i) != before[i] for i in ids), "the volumes really changed"
    assert ed.undo_steps_since_mark() == 1, "voices, ducking and the master gain are one user intent"
    ed.undo()
    assert {i: volume_points(ed, i) for i in ids} == before


def test_a_dry_run_leaves_the_project_and_the_history_alone(mix_world):
    ed = mix_world.world.editor
    ids = (mix_world.s1, mix_world.s2, mix_world.bed)
    before = {i: volume_points(ed, i) for i in ids}
    receipt = ed.call_receipt("balance_mix_tool", dry_run=True)
    assert receipt["data"]["dry_run"] is True and {i: volume_points(ed, i) for i in ids} == before
    assert ed.undo_steps_since_mark() == 0 and mix_world.mixes == []


def test_the_quieter_voice_is_raised_and_the_louder_one_lowered_toward_the_median(mix_world):
    ed = mix_world.world.editor
    data = ed.call_receipt("balance_mix_tool", set_loudness=False, duck_music=False)["data"]
    by = {a["id"]: a["delta_db"] for a in data["voices"]["adjust"]}
    assert by[mix_world.s2] > 0 > by[mix_world.s1] and abs(by[mix_world.s2] + by[mix_world.s1] - 0.0) < 8.0
    assert data["voices"]["target_db"] == pytest.approx(-22.0, abs=0.1)


# ============================ the audit and the survey on a real project ============================
def test_the_audit_reads_the_real_timeline_and_leaves_no_trace(world):
    ed = world.editor
    a, b, bed = picture_pair(world)
    build(world.shelf, SHA_V, shots=[shot(0, 0, 12, "pan"), shot(1, 12, 25, "static")], duration=24.5,
          watch=[w(0, 0, 12, "A lake"), w(1, 12, 25, "A dog")], look={"profile": profile(0.5)})
    library.clear_cache()
    receipt = ed.call_receipt("review_edit_tool", form="YouTube vlog", measure_colour=False, render_mix=False)
    assert receipt["status"] in ("applied", "unchanged"), receipt
    data = receipt["data"]
    ids = {f["id"]: f for layer in data["layers"].values() for f in layer}
    assert ids["music"]["status"] == "ok" and ids["music"]["evidence"]["beds"] == 1, "the music bed on the timeline was found, with its file's index"
    assert ids["beat_sync"]["evidence"]["cuts"] == 1 if "beat_sync" in ids else True
    assert "loudness" in data["unknown"] and "consistency" in data["unknown"]
    assert data["measured"]["clips"] == 3 and data["measured"]["mix_rendered"] is False
    assert ed.undo_steps_since_mark() == 0, "an audit changes nothing"


def test_the_overview_counts_the_real_projects_files(world):
    ed = world.editor
    picture_pair(world)
    build(world.shelf, SHA_V, shots=[shot(0, 0, 12, "pan"), shot(1, 12, 25, "static")], duration=24.5,
          watch=[w(0, 0, 12, "A lake", interest=0.9, highlight_reason="calm and beautiful"), w(1, 12, 25, "A dog")], look={"profile": profile(0.5)})
    library.clear_cache()
    receipt = ed.call_receipt("get_project_overview_tool")
    data = receipt["data"]
    assert data["totals"]["videos"] == 1 and data["totals"]["audio"] == 1 and data["top_moments"][0]["why"] == "calm and beautiful"
    assert data["music"][0]["bpm"] == 120.0 and ed.undo_steps_since_mark() == 0


def test_after_the_real_ducking_pass_the_audit_no_longer_calls_the_music_too_loud(mix_world):
    ed = mix_world.world.editor

    def margin():
        data = ed.call_receipt("review_edit_tool", form="YouTube vlog", measure_colour=False, render_mix=False)["data"]
        found = {f["id"]: f for layer in data["layers"].values() for f in layer}
        return found["dialogue_margin"]

    first = margin()
    assert first["status"] == "needs" and first["evidence"]["worst_db"] < 8.0, "voices and music start at nearly the same level"
    ed.call_receipt("balance_mix_tool", set_loudness=False, even_out_voices=False)
    second = margin()
    assert second["status"] == "ok", second
    assert second["evidence"]["worst_db"] > first["evidence"]["worst_db"] + 6.0 and second["evidence"]["overlaps"][0]["automated_gain"] is True
