"""Watch-window extract: padding, cues, dedupe, sparse warning."""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.watch_window import (
    DEFAULT_LONG_EDGE,
    TEXT_LONG_EDGE,
    dedupe_frame_records,
    extract_watch_window,
    is_onscreen_text_query,
    padded_window,
    plan_sample_times,
    snap_to_shot_boundaries,
)


def test_padded_window_clamps():
    s, e = padded_window(1.0, 3.0, duration=10.0, pad=2.0)
    assert abs(s - 0.0) < 1e-6
    assert abs(e - 5.0) < 1e-6


def test_held_slide_dedupes_to_few():
    same = b"\x10" * 256
    records = [
        {"timestamp": 10.0 + i * 0.5, "fingerprint": same, "must_keep": False, "path": f"f{i}"}
        for i in range(20)
    ]
    kept = dedupe_frame_records(records, max_frames=12)
    assert len(kept) == 1


def test_cues_in_range_are_sampled():
    times, warning, sparse = plan_sample_times(
        8.0, 12.0,
        cues=[{"start": 9.25, "end": 10.0, "text": "hello"}],
        max_frames=12,
    )
    assert any(abs(t - 9.25) < 0.02 for t in times)
    assert sparse is False
    assert warning == ""


def test_long_window_sparse_warning():
    times, warning, sparse = plan_sample_times(0.0, 90.0, max_frames=12)
    assert sparse is True
    assert "sparse" in warning.lower()
    assert len(times) <= 12


def test_onscreen_text_uses_1024():
    assert is_onscreen_text_query("read the terminal UI text")
    assert not is_onscreen_text_query("person jumps")
    assert TEXT_LONG_EDGE == 1024
    assert DEFAULT_LONG_EDGE == 512


def test_extract_watch_window_does_not_unbound_sparse(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"not-a-real-mp4")
    with patch("classes.watch_window._probe_duration", return_value=30.0):
        with patch("classes.watch_window._scene_times", return_value=[]):
            with patch("classes.watch_window._extract_one_jpeg", return_value=False):
                out = extract_watch_window(str(src), 1.0, 3.0, query="handshake")
    assert out["ok"] is True
    assert out["sparse"] is False


def test_wide_extract_uses_dense_second_pass(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"not-a-real-mp4")
    captured = {}

    def fake_plan(win_start, win_end, *a, **k):
        captured["span"] = float(win_end) - float(win_start)
        captured["start"] = float(win_start)
        captured["end"] = float(win_end)
        times, warning, sparse = plan_sample_times(win_start, win_end, max_frames=36)
        return times, warning, sparse

    with patch("classes.watch_window._probe_duration", return_value=80.0):
        with patch("classes.watch_window._scene_times", return_value=[10.0, 12.0, 14.0]):
            with patch("classes.watch_window._extract_one_jpeg", return_value=False):
                with patch("classes.watch_window.plan_sample_times", side_effect=fake_plan):
                    out = extract_watch_window(str(src), 0.0, 40.0, query="action", duration=80.0)
    assert captured["span"] <= 16.0 + 1e-6
    assert out["sparse"] is False
    times, _w, sparse = plan_sample_times(captured["start"], captured["end"])
    assert sparse is False
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert gaps
    assert max(gaps) <= 0.5 + 1e-6


def test_shot_boundary_snap():
    inn, out = snap_to_shot_boundaries(2.18, 9.0, 5.0, [2.0, 7.5], tolerance=0.4)
    assert abs(inn - 2.0) < 1e-9
    inn2, _out2 = snap_to_shot_boundaries(3.9, 9.0, 5.0, [2.0, 7.5], tolerance=0.4)
    assert abs(inn2 - 3.9) < 1e-9
    inn3, out3 = snap_to_shot_boundaries(4.9, 5.2, 5.0, [6.0], tolerance=0.4)
    assert inn3 <= 5.0 <= out3
    assert abs(inn3 - 4.9) < 1e-9

