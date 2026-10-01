"""Headless edit_ops unit tests (no Qt)."""

from __future__ import annotations

import pytest

from classes import edit_ops
from classes import frame_time as ft


def _clip(cid, *, pos=0.0, start=0.0, end=2.0, layer=1, link=None):
    data = {
        "id": cid,
        "position": pos,
        "start": start,
        "end": end,
        "layer": layer,
    }
    if link is not None:
        data["link_group_id"] = link
    return data


def _layers(*nums, locked=(), sync_off=()):
    out = []
    for n in nums:
        out.append({
            "number": n,
            "id": f"L{n}",
            "lock": n in locked,
            "sync_locked": n not in sync_off,
        })
    return out


@pytest.mark.parametrize("fps", [24, 30, 30000 / 1001])
def test_slip_preserves_timeline_box(fps):
    c = _clip("v1", pos=1.0, start=0.5, end=2.5)
    r = edit_ops.slip(c, delta_frames=4, fps=fps)
    assert r["ok"] and r["status"] == "applied"
    patch = r["patches"][0]
    assert patch["position"] == ft.snap(1.0, fps)
    assert ft.duration_frames(patch["start"], patch["end"], fps) == ft.duration_frames(0.5, 2.5, fps)
    assert ft.to_frame(patch["start"], fps) == ft.to_frame(0.5, fps) + 4


def test_slip_linked_partners_same_delta():
    v = _clip("v1", pos=0.0, start=1.0, end=3.0, layer=2, link="g1")
    a = _clip("a1", pos=0.0, start=1.0, end=3.0, layer=1, link="g1")
    r = edit_ops.slip(v, delta_frames=6, fps=24, partners=[a])
    assert r["ok"]
    by_id = {p["id"]: p for p in r["patches"]}
    assert ft.to_frame(by_id["v1"]["start"], 24) == ft.to_frame(1.0, 24) + 6
    assert ft.to_frame(by_id["a1"]["start"], 24) == ft.to_frame(1.0, 24) + 6


def test_roll_keeps_sequence_length():
    left = _clip("a", pos=0.0, start=0.0, end=2.0, layer=1)
    right = _clip("b", pos=2.0, start=0.0, end=2.0, layer=1)
    seq_before = 4.0
    r = edit_ops.roll(left, right, delta_frames=8, fps=24)
    assert r["ok"]
    by_id = {p["id"]: p for p in r["patches"]}
    a_end = by_id["a"]["position"] + (by_id["a"]["end"] - by_id["a"]["start"])
    b_end = by_id["b"]["position"] + (by_id["b"]["end"] - by_id["b"]["start"])
    assert abs(a_end - by_id["b"]["position"]) < 1e-9
    assert abs(b_end - seq_before) < 1e-6


def test_roll_no_adjacent_empty_refused_via_empty_right():
    left = _clip("a", pos=0.0, start=0.0, end=0.1, layer=1)
    right = _clip("b", pos=0.1, start=0.0, end=0.1, layer=1)
    # Huge negative roll empties left
    r = edit_ops.roll(left, right, delta_frames=-100, fps=24)
    assert not r["ok"]
    assert r["status"] == "refused"
    assert r["undo"] == "none"


def test_roll_locked_track_refused():
    left = _clip("a", pos=0.0, start=0.0, end=2.0, layer=1)
    right = _clip("b", pos=2.0, start=0.0, end=2.0, layer=1)
    r = edit_ops.roll(left, right, delta_frames=4, fps=24, layers=_layers(1, locked=(1,)))
    assert not r["ok"]
    assert "locked" in r["error"]


def test_slide_neighbors_absorb():
    left = _clip("a", pos=0.0, start=0.0, end=2.0, layer=1)
    mid = _clip("b", pos=2.0, start=0.0, end=2.0, layer=1)
    right = _clip("c", pos=4.0, start=0.0, end=2.0, layer=1)
    r = edit_ops.slide(mid, [left, mid, right], delta_frames=6, fps=24)
    assert r["ok"]
    by_id = {p["id"]: p for p in r["patches"]}
    assert "a" in by_id and "b" in by_id and "c" in by_id
    # Sequence end still at 6s
    c = by_id["c"]
    assert abs(c["position"] + (c["end"] - c["start"]) - 6.0) < 1e-6


def test_slide_no_neighbors_refused():
    solo = _clip("solo", pos=1.0, start=0.0, end=2.0, layer=1)
    r = edit_ops.slide(solo, [solo], delta_frames=4, fps=24)
    assert not r["ok"]
    assert "adjacent" in r["error"]


def test_lift_leaves_delete_ids_no_shift():
    clips = [
        _clip("a", pos=0.0, end=2.0),
        _clip("b", pos=2.0, end=4.0),
        _clip("c", pos=4.0, end=6.0),
    ]
    r = edit_ops.lift(["b"], clips)
    assert r["ok"]
    assert r["delete_ids"] == ["b"]
    assert r["patches"] == []
    assert r["shifted_ids"] == []


def test_extract_closes_gap():
    clips = [
        _clip("a", pos=0.0, start=0.0, end=2.0, layer=1),
        _clip("b", pos=2.0, start=0.0, end=2.0, layer=1),
        _clip("c", pos=4.0, start=0.0, end=2.0, layer=1),
    ]
    r = edit_ops.extract(["b"], clips, fps=24, layers=_layers(1))
    assert r["ok"]
    assert r["delete_ids"] == ["b"]
    by_id = {p["id"]: p for p in r["patches"]}
    assert abs(by_id["c"]["position"] - 2.0) < 1e-6


def test_extract_respects_sync_lock_off():
    clips = [
        _clip("v1", pos=0.0, end=2.0, layer=2),
        _clip("v2", pos=2.0, end=4.0, layer=2),
        _clip("m1", pos=2.0, end=4.0, layer=1),  # music under v2
    ]
    # Extract v1; sync off on music → music should not shift
    r = edit_ops.extract(["v1"], clips, fps=24, layers=_layers(1, 2, sync_off=(1,)))
    assert r["ok"]
    shifted = {p["id"] for p in r["patches"]}
    assert "v2" in shifted
    assert "m1" not in shifted


def test_extract_sync_lock_on_shifts_music():
    clips = [
        _clip("v1", pos=0.0, end=2.0, layer=2),
        _clip("v2", pos=2.0, end=4.0, layer=2),
        _clip("m1", pos=2.0, end=4.0, layer=1),
    ]
    r = edit_ops.extract(["v1"], clips, fps=24, layers=_layers(1, 2))
    assert r["ok"]
    by_id = {p["id"]: p for p in r["patches"]}
    assert "m1" in by_id
    assert abs(by_id["m1"]["position"] - 0.0) < 1e-6


def test_locked_track_lift_refused():
    clips = [_clip("a", layer=1)]
    r = edit_ops.lift(["a"], clips, layers=_layers(1, locked=(1,)))
    assert not r["ok"]
    assert r["undo"] == "none"


def test_link_unlink_roundtrip():
    clips = [_clip("v"), _clip("a")]
    linked = edit_ops.link_clips(["v", "a"], clips)
    assert linked["ok"]
    gid = linked["link_group_id"]
    assert all(p["link_group_id"] == gid for p in linked["patches"])
    unlinked = edit_ops.unlink_clips(["v", "a"], linked["patches"])
    assert unlinked["ok"]
    assert all("link_group_id" not in p for p in unlinked["patches"])


def test_receipt_keys_stable_on_failure():
    r = edit_ops.slip(_clip("x"), delta_frames=0, fps=24)
    # zero delta is no_op success
    assert set(r) >= {
        "ok", "op", "version", "changed_ids", "shifted_ids",
        "before", "after", "no_op", "warnings", "undo", "status", "error",
    }
    bad = edit_ops.roll(_clip("a"), _clip("a"), delta_frames=1, fps=24)
    assert set(bad) >= {
        "ok", "op", "version", "changed_ids", "shifted_ids",
        "before", "after", "no_op", "warnings", "undo", "status", "error",
    }


def test_nudge_cap():
    assert abs(edit_ops.resolve_nudge_frames(100, soft=True)) == edit_ops.NUDGE_MAX_FRAMES
    assert edit_ops.resolve_nudge_frames(None, soft=True) == edit_ops.NUDGE_DEFAULT_FRAMES


def test_roll_invert_restores():
    left = _clip("a", pos=0.0, start=0.0, end=2.0, layer=1)
    right = _clip("b", pos=2.0, start=0.0, end=2.0, layer=1)
    r = edit_ops.roll(left, right, delta_frames=5, fps=24)
    assert r["ok"]
    inv = edit_ops.roll(r["patches"][0], r["patches"][1], delta_frames=-5, fps=24)
    assert inv["ok"]
    by_id = {p["id"]: p for p in inv["patches"]}
    assert abs(by_id["a"]["end"] - 2.0) < 1e-9
    assert abs(by_id["b"]["position"] - 2.0) < 1e-9


def test_apply_invert_100_times_roll():
    # Long media headroom so ±1f × 100 never empties either side.
    left = _clip("a", pos=0.0, start=0.0, end=10.0, layer=1)
    right = _clip("b", pos=10.0, start=0.0, end=10.0, layer=1)
    cur_l, cur_r = left, right
    for _ in range(100):
        r = edit_ops.roll(cur_l, cur_r, delta_frames=1, fps=24)
        assert r["ok"], r.get("error")
        by_id = {p["id"]: p for p in r["patches"]}
        cur_l, cur_r = by_id["a"], by_id["b"]
    for _ in range(100):
        r = edit_ops.roll(cur_l, cur_r, delta_frames=-1, fps=24)
        assert r["ok"], r.get("error")
        by_id = {p["id"]: p for p in r["patches"]}
        cur_l, cur_r = by_id["a"], by_id["b"]
    assert abs(cur_l["end"] - 10.0) < 1e-9
    assert abs(cur_r["position"] - 10.0) < 1e-9


def test_set_track_sync_lock():
    layers = [{"number": 1, "lock": False}, {"number": 2, "lock": False}]
    r = edit_ops.set_track_sync_lock(layers, track=1, sync_locked=False)
    assert r["ok"]
    by_num = {int(L["number"]): L for L in r["layers"]}
    assert by_num[1]["sync_locked"] is False
    assert by_num[2]["sync_locked"] is True  # default filled
