"""Clip link_group_id helpers."""

from __future__ import annotations

from classes import edit_ops


def test_auto_style_link_and_list():
    clips = [
        {"id": "v1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 2},
        {"id": "a1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 1},
        {"id": "m1", "position": 0.0, "start": 0.0, "end": 5.0, "layer": 0},
    ]
    linked = edit_ops.link_clips(["v1", "a1"], clips)
    assert linked["ok"]
    gid = linked["link_group_id"]
    listed = edit_ops.list_link_groups(linked["patches"] + [clips[2]])
    assert listed["groups"][gid] == ["v1", "a1"] or set(listed["groups"][gid]) == {"v1", "a1"}


def test_partners_for():
    clips = [
        {"id": "v1", "position": 0.0, "start": 0.0, "end": 1.0, "layer": 2, "link_group_id": "g"},
        {"id": "a1", "position": 0.0, "start": 0.0, "end": 1.0, "layer": 1, "link_group_id": "g"},
        {"id": "x", "position": 0.0, "start": 0.0, "end": 1.0, "layer": 3},
    ]
    partners = edit_ops.partners_for("v1", clips)
    assert [p["id"] for p in partners] == ["a1"]


def test_unlink_then_independent_slip():
    clips = [
        {"id": "v1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 2, "link_group_id": "g"},
        {"id": "a1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 1, "link_group_id": "g"},
    ]
    un = edit_ops.unlink_clips(["v1"], clips)
    v = next(p for p in un["patches"] if p["id"] == "v1")
    # audio still linked in original list sense; partners_for on mixed state
    mixed = [v, clips[1]]
    assert edit_ops.partners_for("v1", mixed) == []
    r = edit_ops.slip(v, delta_frames=3, fps=24, partners=[])
    assert r["ok"]
    assert len(r["patches"]) == 1
