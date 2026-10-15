"""Unit tests for contract-3 receipts and timeline snapshots (no Qt)."""

from fractions import Fraction

from classes.agent_tools.receipt import (
    ToolReceipt,
    from_handler_str,
    is_error_result,
    parse_receipt,
)
from classes.agent_tools.snapshot import mutation_result, requested_vs_landed, timeline_snapshot
from classes.frame_time import to_frame


def test_receipt_error_summary_prefix():
    r = ToolReceipt.error("t", "boom")
    assert r.summary.startswith("Error:")
    assert r.status == "error"
    assert r.undoSteps == 0
    assert is_error_result(r.to_json())


def test_receipt_refused_prefix():
    r = ToolReceipt.refused("t", "nope")
    assert r.summary.startswith("Error:")
    assert parse_receipt(r.to_json())["status"] == "refused"


def test_from_handler_str_error():
    r = from_handler_str("add_clip_to_timeline_tool", "Error: File not found")
    assert r.status == "error"
    assert is_error_result(r.to_json())


def test_from_handler_str_success_unmutated():
    r = from_handler_str("list_files_tool", "3 files", mutated=False)
    assert r.undoSteps == 0


def test_snapshot_insert_and_delete():
    fps = Fraction(30, 1)
    before = timeline_snapshot([], fps=fps)
    after = timeline_snapshot([
        {"id": "c1", "layer": 1, "_track_index": 1, "position": 1.5, "start": 0, "end": 2},
    ], fps=fps)
    receipt = mutation_result(before, after, tool="add", summary="added")
    assert receipt.status == "applied"
    assert len(receipt.clips) == 1
    assert receipt.clips[0]["id"] == "c1"
    assert receipt.clips[0]["landedFrame"] == to_frame(1.5, fps)

    deleted = mutation_result(after, before, tool="del", summary="removed")
    assert deleted.removedClipIds == ["c1"]


def test_requested_1_501s_lands_frame_45_at_30fps():
    fps = Fraction(30, 1)
    info = requested_vs_landed(1.501, 1.5, fps)
    assert info["requestedFrame"] == 45
    assert info["landedFrame"] == 45


def test_shift_compression_at_three():
    fps = Fraction(30, 1)
    before_clips = [
        {"id": f"c{i}", "layer": 0, "_track_index": 0, "position": float(i), "start": 0, "end": 1}
        for i in range(5)
    ]
    after_clips = [
        {"id": f"c{i}", "layer": 0, "_track_index": 0, "position": float(i) + 2.0, "start": 0, "end": 1}
        for i in range(5)
    ]
    before = timeline_snapshot(before_clips, fps=fps)
    after = timeline_snapshot(after_clips, fps=fps)
    receipt = mutation_result(before, after, tool="ripple", summary="shifted")
    assert receipt.shifted
    assert receipt.shifted[0]["count"] == 5
    assert receipt.shifted[0]["by"] == to_frame(2.0, fps)


def test_noop_mutation():
    fps = Fraction(30, 1)
    clips = [{"id": "c1", "layer": 0, "_track_index": 0, "position": 0, "start": 0, "end": 1}]
    snap = timeline_snapshot(clips, fps=fps)
    receipt = mutation_result(snap, snap, tool="t", summary="same", status="unchanged", undo_steps=1)
    assert receipt.undoSteps == 0
    assert receipt.clips == []


# --- review follow-ups (PR #216): receipts the agent updates its state from ---

import pytest  # noqa: E402


class _Project:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def _run(monkeypatch, tool_name, data, history, edit):
    from types import SimpleNamespace

    from classes.agent_tools import execute

    app = SimpleNamespace(project=_Project(data), updates=SimpleNamespace(actionHistory=history))

    def handler(**_kw):
        edit(data, history)
        return "Done."

    monkeypatch.setattr(execute, "_HANDLERS", {tool_name: handler})
    monkeypatch.setattr(execute, "_GET_APP", lambda: app)
    monkeypatch.setattr(execute, "_QTHREAD", None)
    monkeypatch.setattr(execute, "_UNGROUPED", frozenset({tool_name}))
    monkeypatch.setattr(execute, "_READ_ONLY", frozenset())
    return execute.execute_tool_rich(tool_name, {}).receipt


def _clip(cid, position=0.0, start=0.0, end=2.0, layer=1):
    return {"id": cid, "position": position, "start": start, "end": end, "layer": layer}


def test_an_undo_receipt_reports_what_the_undo_changed(monkeypatch):
    data = {"fps": {"num": 30, "den": 1}, "clips": [_clip("a"), _clip("b", position=2.0)]}

    def undo(d, history):
        history.pop()
        d["clips"] = [c for c in d["clips"] if c["id"] != "b"]

    receipt = _run(monkeypatch, "undo_tool", data, ["add b"], undo)
    assert receipt.status == "applied"
    assert receipt.removedClipIds == ["b"]
    assert receipt.undoSteps == 0  # an undo adds nothing to undo


def test_a_slipped_clip_is_reported_as_changed(monkeypatch):
    data = {"fps": {"num": 30, "den": 1}, "clips": [_clip("a", start=0.0, end=2.0)]}

    def slip(d, history):
        history.append("slip")
        d["clips"][0].update(start=1.0, end=3.0)  # same place and length, new in-point

    receipt = _run(monkeypatch, "fake_slip_tool", data, [], slip)
    assert [c["id"] for c in receipt.clips] == ["a"]
    assert receipt.clips[0]["start"] == 1.0


def test_new_tracks_and_markers_are_in_the_receipt(monkeypatch):
    data = {"fps": {"num": 30, "den": 1}, "clips": [], "layers": [{"id": "L1", "number": 1}],
            "markers": []}

    def add(d, history):
        history.append("add")
        d["layers"].append({"id": "L2", "number": 2})
        d["markers"].append({"id": "M1", "position": 1.5})

    receipt = _run(monkeypatch, "fake_track_tool", data, [], add)
    assert [t["id"] for t in receipt.createdTracks] == ["L2"]
    assert [m["id"] for m in receipt.markers] == ["M1"]
