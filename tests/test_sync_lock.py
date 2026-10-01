"""Track sync_locked defaults and ripple/extract behavior."""

from __future__ import annotations

from classes import edit_ops


def test_missing_sync_locked_defaults_true():
    assert edit_ops.layer_is_sync_locked([{"number": 1}], 1) is True


def test_ensure_layers_sync_defaults():
    out = edit_ops.ensure_layers_sync_defaults([{"number": 3, "lock": False}])
    assert out[0]["sync_locked"] is True


def test_ripple_trim_out_shifts_downstream_sync_locked():
    clips = [
        {"id": "v1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 2},
        {"id": "v2", "position": 2.0, "start": 0.0, "end": 2.0, "layer": 2},
        {"id": "m1", "position": 2.0, "start": 0.0, "end": 2.0, "layer": 1},
    ]
    layers = [
        {"number": 1, "lock": False, "sync_locked": True},
        {"number": 2, "lock": False, "sync_locked": True},
    ]
    r = edit_ops.ripple_trim(clips[0], clips, edge="out", delta_frames=-12, fps=24, layers=layers)
    assert r["ok"]
    # Shortening v1 by 12f should pull v2 and m1 earlier
    shifted = {p["id"]: p for p in r["patches"] if p["id"] in ("v2", "m1")}
    assert "v2" in shifted and "m1" in shifted


def test_ripple_trim_sync_off_leaves_music():
    clips = [
        {"id": "v1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 2},
        {"id": "v2", "position": 2.0, "start": 0.0, "end": 2.0, "layer": 2},
        {"id": "m1", "position": 2.0, "start": 0.0, "end": 2.0, "layer": 1},
    ]
    layers = [
        {"number": 1, "lock": False, "sync_locked": False},
        {"number": 2, "lock": False, "sync_locked": True},
    ]
    r = edit_ops.ripple_trim(clips[0], clips, edge="out", delta_frames=-12, fps=24, layers=layers)
    assert r["ok"]
    shifted_ids = set(r["shifted_ids"])
    assert "v2" in shifted_ids
    assert "m1" not in shifted_ids
