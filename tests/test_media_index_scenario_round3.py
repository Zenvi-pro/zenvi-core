"""Two scripted sessions from real testing, on a real project (real tools, real undo, real audio analysis of generated sound).

1. "Where does the speaker start? Cut right there." The transcript said the first word was at 0.033 s (one frame) while the voice
   really starts at 1.0 s. The cut at the transcript time left a 33 ms sliver. Now that cut is refused with the tool to use instead,
   and the voice edge the sound itself gives is a clean cut.
2. "Cut this clip to the beat of its own music." One long clip whose sound has a steady beat is cut into beat-length pieces, in one
   undo step, on the beats the real analysis found.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import facts, library  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from eval import corpus  # noqa: E402
from test_media_index_scenario_trip_short import PICTURE, receipt_data, sha  # noqa: E402
from test_media_index_search import build, profile, shot, w  # noqa: E402
from test_media_index_voice import s  # noqa: E402
from test_media_index_voice import w as word  # noqa: E402


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    from classes import clip_utils, timeline_ops
    from classes.editor_tools import media_index_tools as T, media_index_tools_edit as TE, media_index_tools_precision as TX, media_index_tools_review as TR
    from timeline_edit_fakes import FakeTimeline, fake_openshot
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
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
    editor.add_track(PICTURE, "Picture")
    editor.add_track(PICTURE + 1, "Music")

    # a talking clip: the voice starts at 1.0 s, but the transcript puts its first word at 0.033 s
    voice, _ = corpus.voice_bursts(spans=((1.0, 3.0), (4.5, 6.0)), seconds=7.0)
    talk_sha = sha(60)
    build(shelf, talk_sha, shots=[shot(0, 0, 7, "static")], watch=[w(0, 0, 7, "A chef talks", shot_type="medium")], duration=7.0, look={"profile": profile(0.5)})
    shelf.write_json(talk_sha, "speech.json", {"words": [word(0.033, 0.6, "So"), word(1.1, 1.5, "today")], "sentences": [s(0.033, 3.2, "So today")]})
    shelf.set_layer(talk_sha, "speech", version=1, status="ready")
    shelf.set_source(talk_sha, duration=7.0, media_type="video", has_audio=True)
    talk = editor.add_file("video", duration=7.0, path=str(voice), fingerprint={"sha256": talk_sha})

    # a clip with a steady beat: analysed by the real local audio analysis
    click, truth = corpus.click_track(bpm=120.0, seconds=20.0, sections=((0.0, 20.0, 0.5),))
    beat_sha = sha(61)
    build(shelf, beat_sha, shots=[shot(0, 0, 20, "static")], duration=20.0, look={"profile": profile(0.5)})
    done = facts.compute_facts(str(click), fingerprint={"sha256": beat_sha}, shelf=shelf, media_type="audio", run_speech=False)
    assert done["layers"].get("audio") == "ready", done
    trailer = editor.add_file("video", duration=20.0, path=str(click), fingerprint={"sha256": beat_sha}, has_audio=True, channels=2)
    editor.mark()
    library.clear_cache()
    return SimpleNamespace(ed=editor, talk=talk, trailer=trailer, shelf=shelf, truth=truth)


def pieces(ed, layer=PICTURE):
    return sorted((round(c["position"], 3), round(c["position"] + c["end"] - c["start"], 3)) for c in ed.clips() if c["layer"] == layer)


def test_a_cut_at_the_transcript_time_is_refused_and_the_voice_edge_gives_a_clean_cut(studio):
    ed = studio.ed
    receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": studio.talk}], track=str(PICTURE), start_seconds=0)
    clip_id = next(c["id"] for c in ed.clips() if c["layer"] == PICTURE)
    ed.mark()

    # what happened in testing: the Assistant read 0.033 s from the transcript and cut there
    out = ed.call("slice_clips_tool", timeline_clip_ids=[clip_id], at_seconds=0.033)
    assert out.startswith("Error") and "under two frames" in out and "get_voice_edges_tool" in out
    assert pieces(ed) == [(0.0, 7.0)] and ed.undo_steps_since_mark() == 0, "nothing was cut, no undo step"

    # what it should do: ask the sound
    edges = receipt_data(ed, "get_voice_edges_tool", file_ids=[studio.talk], start_seconds=0.0, end_seconds=3.5)["voice"]
    assert edges["found"] and edges["start"] == pytest.approx(1.0, abs=0.08), "the voice starts at 1.0 s, not at the transcript's 0.033 s"
    cut = receipt_data(ed, "slice_clips_tool", timeline_clip_ids=[clip_id], at_seconds=edges["start"] - 0.1)
    assert cut["pieces"][0]["changed"] is True
    left, right = pieces(ed)
    assert left[1] == pytest.approx(0.9, abs=0.05) and right[0] == left[1] and right[1] == 7.0
    assert min(b - a for a, b in pieces(ed)) > 0.5, "no sliver"
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert pieces(ed) == [(0.0, 7.0)]


def test_one_clip_is_cut_to_the_beat_of_its_own_music_on_the_beats_the_analysis_found(studio):
    ed = studio.ed
    receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": studio.trailer}], track=str(PICTURE), start_seconds=0)
    clip_id = next(c["id"] for c in ed.clips() if c["layer"] == PICTURE)
    ed.mark()
    truth = studio.truth["beats"]

    profile_out = receipt_data(ed, "analyze_music_tool", file_id=studio.trailer)
    assert profile_out["profile"]["bpm"] == pytest.approx(120.0, abs=2.0), "the real analysis reads the tempo"

    plan = receipt_data(ed, "slice_on_beats_tool", timeline_clip_id=clip_id, dry_run=True)
    assert plan["dry_run"] is True and ed.undo_steps_since_mark() == 0 and pieces(ed) == [(0.0, 20.0)]
    assert len(plan["cuts"]) == pytest.approx(len(truth), abs=3)

    done = receipt_data(ed, "slice_on_beats_tool", timeline_clip_id=clip_id)
    cuts = [a for a, _ in pieces(ed)][1:]
    assert len(cuts) == len(done["cuts"]) and done["pieces"] == len(cuts) + 1
    nearest = [min(abs(c - t) for t in truth) for c in cuts]
    assert max(nearest) < 0.06, "every cut sits on a beat, within a couple of frames"
    assert min(b - a for a, b in pieces(ed)) > 0.4, "pieces run from one beat to the next, no slivers"
    assert ed.undo_steps_since_mark() == 1, f"{len(cuts)} cuts, one undo step"
    ed.undo()
    assert pieces(ed) == [(0.0, 20.0)]

    bars = receipt_data(ed, "slice_on_beats_tool", timeline_clip_id=clip_id, every=4)
    assert len(bars["cuts"]) == pytest.approx(len(truth) / 4, abs=2)
