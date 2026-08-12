"""Unit tests for classes.mg_placement (pure helpers, no Qt)."""

from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.mg_placement import (  # noqa: E402
    apply_embedding_time_boosts,
    file_looks_transparent,
    layout_region_for,
    primary_track_overlaps,
    ripple_positions,
    score_scene_blob,
)


def test_score_scene_blob_avoid_and_prefer():
    avoid_score, avoid, prefer = score_scene_blob("close-up face talking head interview")
    assert avoid is True
    assert avoid_score < 0

    prefer_score, avoid2, prefer2 = score_scene_blob(
        "wide establishing b-roll landscape exterior drone aerial"
    )
    assert prefer2 is True
    assert prefer_score > avoid_score
    assert avoid2 is False or prefer_score > 0


def test_layout_region_mapping():
    assert layout_region_for(avoid=True, prefer=False, is_gap=False) == "lower_third"
    assert layout_region_for(avoid=True, prefer=True, is_gap=False) == "corner_br"
    assert layout_region_for(avoid=False, prefer=True, score=3, is_gap=False) == "full_frame"
    assert layout_region_for(avoid=False, prefer=True, score=1, is_gap=False) == "corner_tr"
    assert layout_region_for(avoid=False, prefer=False, is_gap=True) == "mid_plate"
    assert layout_region_for(avoid=False, prefer=False, is_gap=False) == "lower_third"


def test_primary_track_overlaps_and_ripple():
    clips = [
        {"id": "a", "layer": 1, "position": 0.0, "start": 0.0, "end": 5.0},
        {"id": "b", "layer": 1, "position": 5.0, "start": 0.0, "end": 4.0},
        {"id": "c", "layer": 2, "position": 0.0, "start": 0.0, "end": 10.0},
    ]
    assert primary_track_overlaps(clips, layer=1, t0=2.0, t1=3.0) is True
    assert primary_track_overlaps(clips, layer=1, t0=9.0, t1=10.0) is False
    assert primary_track_overlaps(clips, layer=1, t0=4.5, t1=5.5) is True

    shifts = ripple_positions(clips, layer=1, t=5.0, delta=2.0)
    assert shifts == [("b", 7.0)]
    shifts2 = ripple_positions(clips, layer=1, t=0.0, delta=1.5)
    assert {cid for cid, _ in shifts2} == {"a", "b"}
    by_id = dict(shifts2)
    assert by_id["a"] == 1.5
    assert by_id["b"] == 6.5


def test_file_looks_transparent():
    assert file_looks_transparent({"ai_metadata": {"transparent": True}}) is True
    assert file_looks_transparent({"ai_metadata": {"transparent": "true"}}) is True
    assert file_looks_transparent({"tags": ["transparent_overlay"]}) is True
    assert file_looks_transparent({"path": "/tmp/out.webm"}) is True
    assert file_looks_transparent({"path": "/tmp/out.mp4", "ai_metadata": {}}) is False


def test_apply_embedding_time_boosts():
    windows = [
        {"t": 10.0, "score": 0, "avoid": False, "prefer": False},
        {"t": 20.0, "score": 1, "avoid": False, "prefer": False},
    ]
    apply_embedding_time_boosts(
        windows,
        [{"t": 10.2, "score_delta": -3, "avoid": True}, {"t": 50.0, "score_delta": 5}],
        radius_s=1.5,
    )
    assert windows[0]["score"] == -3
    assert windows[0]["avoid"] is True
    assert windows[1]["score"] == 1
