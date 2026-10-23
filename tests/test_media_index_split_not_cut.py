"""A split is a cut the index made only so no shot is too long to keyframe. The edit makes no cut there.

The storyboard labels, the style numbers and the recreation plan count real cuts only: a split piece is folded into the take it continues.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import reference as mref, storyboard, style  # noqa: E402
from classes.media_index.library import FileIndex  # noqa: E402


def piece(i, a, b, kind="hard"):
    return {"id": i, "start": a, "end": b, "opens_with": ({"kind": kind} if kind else None)}


def test_a_split_piece_has_no_label_and_the_summary_counts_only_real_cuts():
    shots = [piece(0, 0.0, 4.0, None), piece(1, 4.0, 8.0), piece(2, 8.0, 12.0, "split"), piece(3, 12.0, 16.0, "fade")]
    fi = SimpleNamespace(shots=shots, sentences=[], audio={"tempo": {"bpm": 120.0, "beats": [4.0, 8.0, 12.0]}})
    assert storyboard.list_line(fi, shots[2]) == "S2 0:08.00-0:12.00 (4.0s)"
    assert storyboard.cut_summary(fi) == "CUTS 1 hard, 1 fade; 2 of 2 land on a beat (120 BPM)"


def test_the_rhythm_counts_a_take_once_however_many_pieces_it_has():
    shots = [piece(i, 2.0 * i, 2.0 * i + 2.0, None if i == 0 else "hard") for i in range(5)] + [piece(5, 10.0, 14.0), piece(6, 14.0, 18.0, "split")]
    line = storyboard.rhythm_line(SimpleNamespace(shots=shots))
    assert "average shot 3.0s" in line and "longest 8.0s" in line and "0 of 6 under a second" in line


def test_the_style_counts_cuts_and_shots_not_split_pieces():
    shots = [piece(0, 0.0, 4.0, None), piece(1, 4.0, 8.0), piece(2, 8.0, 12.0, "split"), piece(3, 12.0, 16.0, "dissolve"), piece(4, 16.0, 20.0)]
    out = style.edit_style(FileIndex(sha="a" * 64, duration=20.0, shots=shots, audio={"tempo": {"bpm": 120.0, "beats": [4.0, 12.0, 16.0]}}))
    assert out["shots"] == 4 and out["transitions"] == {"hard": 2, "dissolve": 1}
    assert out["pacing"]["average_shot"] == 5.0 and out["pacing"]["longest"] == 8.0 and out["pacing"]["cuts_per_minute"] == 9.0
    assert out["music"]["cut_on_beat"] == 1.0


def test_the_recreation_plan_and_the_footage_matcher_have_one_slot_per_real_cut():
    shots = [piece(0, 0.0, 4.0, None), piece(1, 4.0, 8.0), piece(2, 8.0, 12.0, "split"), piece(3, 12.0, 16.0), piece(4, 16.0, 20.0)]
    ref = FileIndex(sha="a" * 64, duration=20.0, shots=shots)
    out = mref.plan_recreation(ref, [])
    assert [r["reference_shot"] for r in out["shots"]] == [0, 1, 3, 4]
    assert (out["shots"][1]["start"], out["shots"][1]["end"]) == (4.0, 12.0)
    assert [r["reference_shot"] for r in mref.match_reference(ref, [])["shots"]] == [0, 1, 3, 4]


@pytest.fixture
def split_studio(editor, tmp_path, monkeypatch):
    from test_media_index_recreate_tools import make_studio
    studio = make_studio(editor, tmp_path, monkeypatch, opens={2: "split", 3: "fade"})
    editor.window.timeline._get_transition_reader_json = lambda path: {"path": path, "has_single_image": True, "type": "QtImageReader"}
    return studio


def test_a_take_with_a_split_is_one_recreated_shot_and_the_fade_after_it_is_still_built(split_studio):
    from test_media_index_scenario_trip_short import receipt_data
    ed, ref = split_studio.ed, split_studio.ref
    pid = receipt_data(ed, "plan_recreation_tool", reference_file_id=ref, stock="off")["plan_id"]
    out = receipt_data(ed, "build_recreation_tool", plan_id=pid)
    assert out["placed"] == 5 and out["transitions"] == 1 and out["transitions_skipped"] == 0
    assert receipt_data(ed, "check_recreation_tool", reference_file_id=ref, plan_id=pid)["areas"]["timing"] == 100.0
