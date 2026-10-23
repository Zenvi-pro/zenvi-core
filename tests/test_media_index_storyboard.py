"""storyboard_tool: a video as its shots, in one call, with exact cuts and one keyframe per shot.

Pure paging/time rules first, then the real tool on generated footage with six known hard cuts (real structure analysis, real frames).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import facts, library, storyboard  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from eval import corpus  # noqa: E402
from test_media_index_scenario_trip_short import receipt_data, sha  # noqa: E402


def shots_of(*bounds):
    return [{"id": i, "start": a, "end": b} for i, (a, b) in enumerate(bounds)]


def test_pages_hold_at_most_the_limit_and_say_where_the_next_begins():
    shots = shots_of(*[(i, i + 1) for i in range(100)])
    here, nxt = storyboard.page(shots, 0, per_page=48)
    assert len(here) == 48 and nxt == 48.0
    last, nxt = storyboard.page(shots, nxt, per_page=48)
    assert [s["id"] for s in last][0] == 48 and len(last) == 48 and nxt == 96.0
    tail, nxt = storyboard.page(shots, nxt, per_page=48)
    assert len(tail) == 4 and nxt is None


def test_a_page_starting_inside_a_shot_includes_that_shot():
    here, _ = storyboard.page(shots_of((0, 4), (4, 8)), 2.0)
    assert [s["id"] for s in here] == [0, 1]


def test_the_keyframe_is_the_middle_of_the_shot_and_never_past_its_last_frame():
    assert storyboard.keyframe_time({"start": 0.0, "end": 4.0}) == 2.0
    assert storyboard.keyframe_time({"start": 1.0, "end": 1.03}, frame=1 / 24) == 1.0


@pytest.fixture
def footage(editor, tmp_path, monkeypatch):
    from classes.editor_tools import media_index_tools as T, media_index_tools_precision as TX
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    for module in (T, TX):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    video, truth = corpus.hard_cuts(seconds=2.0)
    key = sha(70)
    done = facts.compute_facts(str(video), fingerprint={"sha256": key}, shelf=shelf, media_type="video", run_speech=False)
    assert done["layers"].get("structure") == "ready", done
    fid = editor.add_file("video", duration=12.0, path=str(video), fingerprint={"sha256": key})
    library.clear_cache()
    return SimpleNamespace(ed=editor, fid=fid, truth=truth, shelf=shelf)


def test_one_call_gives_every_cut_exactly_and_one_keyframe_per_shot(footage):
    data = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid])
    assert data["total_shots"] == data["shots_shown"] == 6 and data["next_start"] is None
    starts = [float(line.split("[")[1].split("-")[0].split(":")[1]) for line in data["notes"].splitlines() if line.startswith("[")]
    cuts = footage.truth["hard"]
    assert len(starts) == 6
    assert all(abs(a - b) < 0.1 for a, b in zip(starts[1:], cuts)), (starts, cuts)
    assert len(data["tiles"]) == 6 and [t["shot"] for t in data["tiles"]] == list(range(6))
    assert Path(data["image_path"]).is_file()


def test_asking_again_is_served_from_the_shelf(footage):
    first = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid])
    again = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid])
    assert first["cached"] is False and again["cached"] is True and first["image_path"] == again["image_path"]


def test_a_file_without_a_shot_list_is_told_to_wait_not_to_sample(editor, tmp_path, monkeypatch):
    from classes.editor_tools import media_index_tools as T, media_index_tools_precision as TX
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "empty"))
    for module in (T, TX):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    fid = editor.add_file("video", duration=10.0, path=str(tmp_path / "x.mp4"), fingerprint={"sha256": sha(71)})
    r = editor.call_receipt("storyboard_tool", file_ids=[fid])
    assert r["status"] == "error" and "wait" in r["summary"] and "sample" in r["summary"]


def test_list_gives_a_row_per_shot_each_with_its_own_time_in_one_call(footage):
    data = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid], detail="list")
    rows = [line for line in data["notes"].splitlines() if line.startswith("S")]
    assert len(rows) == data["shots_shown"] == data["total_shots"] == 6 and data["next_start"] is None
    assert all("(" in r and "s)" in r for r in rows) and rows[0].startswith("S0 0:00.00-")
    assert len(data["sheets"]) == 1 and data["sheets"][0]["from_shot"] == 0 and data["sheets"][0]["to_shot"] == 5


def test_pictures_can_be_left_out(footage):
    data = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid], detail="list", pictures=False)
    assert data["sheets"] == [] and data["image_path"] is None and data["shots_shown"] == 6


def test_a_row_is_short_keeps_its_time_and_the_first_sentence_only():
    fi = SimpleNamespace(sentences=[{"start": 5.6, "end": 8.0, "text": "hello"}])
    shot = {"id": 3, "start": 5.5, "end": 5.9, "motion": {"class": "handheld"},
            "watch": {"shot_type": "close", "description": "A girl looks down. She is sad and the room is dark " + "x" * 200}}
    row = storyboard.list_line(fi, shot)
    assert row.startswith("S3 0:05.50-0:05.90 (0.4s) close, handheld: A girl looks down") and "sad" not in row and row.endswith('says "hello"')
    long = storyboard.list_line(SimpleNamespace(sentences=[]), {"id": 1, "start": 0, "end": 2, "watch": {"description": "y" * 300}})
    assert len(long) < 160 and "…" in long
    # a sung line is printed once, at the shot it starts in
    song = SimpleNamespace(sentences=[{"start": 5.6, "end": 9.0, "text": "la la"}])
    later = {"id": 4, "start": 6.0, "end": 6.5, "watch": {"description": "x"}}
    assert 'says "la la"' in storyboard.list_line(song, shot) and "says" not in storyboard.list_line(song, later)
    # a shot the description pass left out says so
    assert storyboard.list_line(SimpleNamespace(sentences=[]), {"id": 5, "start": 0, "end": 1, "watch": {"description": "", "missing": True}}).endswith("[no description]")
    assert "[no description]" not in storyboard.list_line(SimpleNamespace(sentences=[]), {"id": 5, "start": 0, "end": 1})


def test_the_keyframe_sheets_come_back_as_pictures_in_the_result(footage):
    from classes import tool_handlers
    out = tool_handlers.execute_tool_rich("storyboard_tool", {"file_ids": [footage.fid], "detail": "list"})
    assert len(out.images) == 1 and out.images[0].data[:2] == b"\xff\xd8"          # one JPEG, the sheet of six keyframes
    quiet = tool_handlers.execute_tool_rich("storyboard_tool", {"file_ids": [footage.fid], "pictures": False})
    assert quiet.images == []
    other = tool_handlers.execute_tool_rich("list_files_tool", {})
    assert other.images == []                                                       # nothing leaks into the next call


def test_a_row_names_a_transition_that_is_not_a_cut_and_the_summary_counts_how_shots_join():
    shots = [{"id": 0, "start": 0.0, "end": 2.0, "opens_with": None},
             {"id": 1, "start": 2.0, "end": 4.0, "opens_with": {"kind": "hard"}},
             {"id": 2, "start": 4.0, "end": 6.0, "opens_with": {"kind": "fade"}},
             {"id": 3, "start": 6.0, "end": 8.0, "opens_with": {"kind": "dissolve"}}]
    fi = SimpleNamespace(shots=shots, sentences=[], audio={"tempo": {"bpm": 120.0, "beats": [2.02, 5.0, 6.01]}})
    assert "[fade in]" in storyboard.list_line(fi, shots[2]) and "in]" not in storyboard.list_line(fi, shots[1])
    assert storyboard.cut_summary(fi) == "CUTS 1 hard, 1 fade, 1 dissolve; 2 of 3 land on a beat (120 BPM)"
    assert storyboard.cut_summary(SimpleNamespace(shots=shots[:1], audio={})) == ""


def test_camera_moves_carry_their_speed_only_when_measured_with_confidence():
    pan = {"motion": {"class": "pan", "direction": "left", "pan_speed": 0.2574, "confidence": 0.67}}
    assert storyboard.camera(pan) == "pan left 0.26/s"
    assert storyboard.camera({"motion": {"class": "tilt", "direction": "down", "tilt_speed": -0.1571, "confidence": 0.57}}) == "tilt down 0.16/s"
    assert storyboard.camera({"motion": {"class": "zoom_out", "zoom_rate": -0.524, "confidence": 0.53}}) == "zoom_out 0.52/s"
    assert storyboard.camera({"motion": {"class": "pan", "direction": "right", "pan_speed": 0.05, "confidence": 0.1}}) == "pan"
    assert storyboard.camera({"motion": {"class": "handheld", "confidence": 0.9}}) == "handheld"
    assert storyboard.camera({}) == ""


def test_a_hedging_description_is_marked_unsure_and_a_plain_one_is_not():
    assert storyboard.unsure({"description": "An abstract glowing shape resembling a mouth"}) == " [unsure]"
    assert storyboard.unsure({"description": "A package or object with red hearts"}) == " [unsure]"
    assert storyboard.unsure({"description": "Two stylized fish swim on white"}) == ""


def test_look_is_brightness_and_the_three_main_colours():
    shot = {"look": {"present": True, "avg_luma": 0.2987}, "look_extras": {"palette": [{"hex": "#0b1116"}, {"hex": "#ede9e9"}, {"hex": "#413e3b"}, {"hex": "#000000"}]}}
    assert storyboard.look_tag(shot) == " | luma 0.30 #0b1116 #ede9e9 #413e3b"
    assert storyboard.look_tag({}) == ""


def _vector(*parts):
    import numpy as np
    return np.array(parts, dtype=np.float32)


def test_near_identical_pictures_far_apart_are_reported_and_neighbours_and_different_ones_are_not():
    import numpy as np
    same, other = _vector(1, 0, 0, 0), _vector(0, 1, 0, 0)
    shots = [{"id": i, "start": float(i), "end": float(i + 1)} for i in range(6)]
    vectors = {0: same, 1: other, 2: _vector(0, 0, 1, 0), 3: _vector(0, 0, 0, 1), 4: other + _vector(0.05, 0, 0, 0), 5: same * 0.999 + _vector(0, 0.01, 0, 0)}
    rows = [{"shot": i} for i in vectors]
    fi = SimpleNamespace(shots=shots, image_rows=rows, image_matrix=np.stack([vectors[i] for i in vectors]))
    line = storyboard.repeats_line(fi)
    assert line.startswith("REPEATS") and "S0~S5" in line and "S1~S4" in line and "S2" not in line
    assert "S0~S1" not in line                                                       # neighbours are left out
    assert storyboard.repeats_line(SimpleNamespace(shots=shots, image_rows=[], image_matrix=None)) == ""


def test_music_sections_name_the_shots_they_cover_and_the_rhythm_says_how_it_speeds_up():
    shots = [{"id": i, "start": float(i * 2), "end": float(i * 2 + 2)} for i in range(6)] + [{"id": 6 + i, "start": 12.0 + i, "end": 12.5 + i} for i in range(6)]
    fi = SimpleNamespace(shots=shots, audio={"music": {"sections": [{"start": 0.0, "end": 12.0, "label": "steady"}, {"start": 12.0, "end": 18.0, "label": "peak"}]}})
    assert storyboard.music_line(fi) == "MUSIC 0:00.0-0:12.0 steady S0-S5; 0:12.0-0:18.0 peak S6-S11"
    rhythm = storyboard.rhythm_line(fi)
    assert "6 of 12 under a second" in rhythm and "first, middle, last third average 2.0s," in rhythm and rhythm.endswith("0.5s")
    assert storyboard.rhythm_line(SimpleNamespace(shots=shots[:3])) == ""


def test_lyrics_are_transcribed_in_the_named_language_kept_and_shown_against_the_shots(editor, tmp_path, monkeypatch):
    import subprocess
    from classes.editor_tools import media_index_tools as T, media_index_tools_precision as TX
    from classes.speech.cache import TranscriptRecord, Word, file_identity
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    for module in (T, TX):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    silent, _truth = corpus.hard_cuts(seconds=2.0)
    video = tmp_path / "song.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(silent), "-f", "lavfi", "-i", "sine=d=12", "-shortest", "-c:v", "copy", "-c:a", "aac", str(video)], check=True)
    key = sha(72)
    done = facts.compute_facts(str(video), fingerprint={"sha256": key}, shelf=shelf, media_type="video", run_speech=False)
    assert done["layers"].get("structure") == "ready", done
    fid = editor.add_file("video", duration=12.0, path=str(video), fingerprint={"sha256": key}, has_audio=True)
    library.clear_cache()
    calls = []

    def fake(path, **kw):
        calls.append(kw.get("language"))
        abspath, size, mtime = file_identity(path)
        words = [Word(c, 2.3 + i * 0.3, 2.55 + i * 0.3, 0.8) for i, c in enumerate("心のなかで")]
        return TranscriptRecord(path=abspath, size=size, mtimeNs=mtime, modelId="apple-speech-analyzer", language=kw.get("language") or "en-US",
                                words=words, generation=1, transcriptionSource="local-apple")

    monkeypatch.setattr("classes.speech.asr.transcribe_file", fake)
    data = receipt_data(editor, "get_lyrics_tool", file_ids=[fid], language="ja-JP")
    assert calls == ["ja-JP"] and data["words"] == 5 and data["replaced"] is True and data["lyrics"][0].endswith("心のなかで")
    board = receipt_data(editor, "storyboard_tool", file_ids=[fid], detail="list", pictures=False)["notes"]
    assert "SPEECH ja-JP: 5 words" in board and 'says "心のなかで"' in board
    quiet = editor.call_receipt("get_lyrics_tool", file_ids=[fid])
    assert quiet["status"] == "error" and "language" in quiet["summary"]


def test_several_files_are_all_listed_not_just_the_first(footage, tmp_path):
    from classes.media_index import facts as F
    other = sha(73)
    video, _truth = corpus.hard_cuts(seconds=2.0)
    F.compute_facts(str(video), fingerprint={"sha256": other}, shelf=footage.shelf, media_type="video", run_speech=False)
    fid2 = footage.ed.add_file("video", duration=12.0, path=str(video), fingerprint={"sha256": other}, name="second.mp4")
    library.clear_cache()
    data = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid, fid2], detail="list")
    assert [f["file_id"] for f in data["files"]] == [footage.fid, fid2] and all(f["shots"] == 6 for f in data["files"])
    assert data["shots_shown"] == 12 and data["notes"].count("\nS0 ") + data["notes"].startswith("S0 ") >= 2 or data["notes"].count("S5 ") == 2
    assert data["sheets"] == [] and "ask for that one file" in data["notes"]


def test_a_file_in_the_bin_with_no_shot_list_is_named_not_skipped_silently(footage):
    ghost = footage.ed.add_file("video", duration=5.0, path="/media/ghost.mp4", fingerprint={"sha256": sha(74)})
    data = receipt_data(footage.ed, "storyboard_tool", file_ids=[footage.fid, ghost])
    assert "no shot list yet" in data["notes"] and [f["file_id"] for f in data["files"]] == [footage.fid]
