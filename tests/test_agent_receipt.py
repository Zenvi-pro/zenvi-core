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
