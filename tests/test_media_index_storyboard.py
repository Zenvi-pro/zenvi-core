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
    fi = SimpleNamespace(sentences=[{"start": 5.0, "end": 6.0, "text": "hello"}])
    shot = {"id": 3, "start": 5.5, "end": 5.9, "motion": {"class": "handheld"},
            "watch": {"shot_type": "close", "description": "A girl looks down. She is sad and the room is dark " + "x" * 200}}
    row = storyboard.list_line(fi, shot)
    assert row.startswith("S3 0:05.50-0:05.90 (0.4s) close, handheld: A girl looks down") and "sad" not in row and row.endswith('says "hello"')
    long = storyboard.list_line(SimpleNamespace(sentences=[]), {"id": 1, "start": 0, "end": 2, "watch": {"description": "y" * 300}})
    assert len(long) < 160 and "…" in long
