"""Unit tests for the pure minimap (overview/zoom slider) geometry."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from windows.views.minimap_geometry import compute_minimap_rects  # noqa: E402


def _clips():
    return [
        {"id": "a", "position": 5.0, "layer": 1, "start": 0.0, "end": 5.0},
        {"id": "b", "position": 30.0, "layer": 2, "start": 0.0, "end": 10.0},
    ]


# width=1000, duration=100 -> 10 px/second. Two layers -> row height = height/2.
_LAYER_INDEX = {2: 0, 1: 1}  # higher track number on top, matching reversed-sorted order


def test_clip_rect_positions():
    res = compute_minimap_rects(
        _clips(), [], [], selected_ids=set(), layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=100.0,
    )
    rects = sorted(res["clip_rects"], key=lambda r: r[0])
    # clip a: x = 5*10 = 50, width = 5*10 = 50, row 1 -> y = 1*20 = 20
    assert rects[0][0] == 50.0
    assert rects[0][2] == 50.0
    assert rects[0][1] == 20.0
    # clip b: x = 30*10 = 300, width = 10*10 = 100, row 0 -> y = 0
    assert rects[1][0] == 300.0
    assert rects[1][2] == 100.0
    assert rects[1][1] == 0.0


def test_drag_override_moves_only_target():
    overrides = {"a": {"position": 20.0}}
    res = compute_minimap_rects(
        _clips(), [], [], selected_ids=set(), layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=100.0, overrides=overrides,
    )
    rects = sorted(res["clip_rects"], key=lambda r: r[2])  # sort by width to identify
    # clip a (width 50) now at x = 20*10 = 200; clip b unchanged at x = 300
    moved = next(r for r in res["clip_rects"] if r[2] == 50.0)
    unmoved = next(r for r in res["clip_rects"] if r[2] == 100.0)
    assert moved[0] == 200.0
    assert unmoved[0] == 300.0


def test_override_layer_change_updates_row():
    overrides = {"a": {"layer": 2}}  # move clip a onto track row 0
    res = compute_minimap_rects(
        _clips(), [], [], selected_ids=set(), layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=100.0, overrides=overrides,
    )
    moved = next(r for r in res["clip_rects"] if r[2] == 50.0)
    assert moved[1] == 0.0  # row 0


def test_selected_clips_routed_separately():
    res = compute_minimap_rects(
        _clips(), [], [], selected_ids={"b"}, layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=100.0,
    )
    assert len(res["clip_rects"]) == 1
    assert len(res["clip_rects_selected"]) == 1
    assert res["clip_rects_selected"][0][2] == 100.0  # clip b


def test_markers_span_full_height():
    markers = [{"id": "m1", "position": 10.0}]
    res = compute_minimap_rects(
        _clips(), [], markers, selected_ids=set(), layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=100.0,
    )
    assert res["marker_rects"][0][0] == 100.0  # 10 * 10 px
    assert res["marker_rects"][0][3] == 40.0   # 2 layers * 20


def test_zero_duration_returns_empty():
    res = compute_minimap_rects(
        _clips(), [], [], selected_ids=set(), layer_index=_LAYER_INDEX,
        width=1000.0, height=40.0, duration=0.0,
    )
    assert res == {"clip_rects": [], "clip_rects_selected": [], "marker_rects": []}


if __name__ == "__main__":
    test_clip_rect_positions()
    test_drag_override_moves_only_target()
    test_override_layer_change_updates_row()
    test_selected_clips_routed_separately()
    test_markers_span_full_height()
    test_zero_duration_returns_empty()
    print("test_minimap_geometry: ok")
