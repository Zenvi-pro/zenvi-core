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
