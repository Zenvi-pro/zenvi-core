"""search_clips renders raw seconds (3dp) and degraded hits without keep windows."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes import tool_handlers  # noqa: E402


def _run_search(results):
    info = {
        "index_id": "idx-1",
        "index_name": "zenvi-proj",
        "indexed_count": 1,
        "video_map": {"vid-a": {"file_id": "file-a", "name": "a.mp4"}},
    }
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.search.return_value = {"results": results}
    with patch(
        "classes.project_tl_index.collect_project_twelvelabs_index",
        return_value=info,
    ), patch(
        "classes.api_client.get_backend_client",
        return_value=client,
    ):
        return tool_handlers.search_clips(query="dog jumps", top_k="5")


def test_search_clips_emits_raw_seconds_three_dp():
    out = _run_search([{
        "video_id": "vid-a",
        "start": 83.4,
        "end": 91.2,
        "peak": 87.1,
        "rank": 1,
        "filename": "a.mp4",
        "role": "action",
        "degraded": False,
    }])
    assert "start_seconds=83.400" in out
    assert "end_seconds=91.200" in out
    assert "peak_seconds=87.100" in out
    assert "keep window 1:23-1:31" not in out
    assert "83.400" in out


def test_search_clips_round_trip_seconds():
    start, end, peak = 83.4, 91.2, 87.1
    out = _run_search([{
        "video_id": "vid-a",
        "start": start,
        "end": end,
        "peak": peak,
        "rank": 1,
        "filename": "a.mp4",
    }])
    import re
    m = re.search(
        r"start_seconds=([0-9.]+) end_seconds=([0-9.]+) peak_seconds=([0-9.]+)",
        out,
    )
    assert m
    assert abs(float(m.group(1)) - start) < 1e-3
    assert abs(float(m.group(2)) - end) < 1e-3
    assert abs(float(m.group(3)) - peak) < 1e-3


def test_degraded_hit_is_not_keep_window():
    out = _run_search([{
        "video_id": "vid-a",
        "start": 0.0,
        "end": 40.0,
        "rank": 1,
        "filename": "a.mp4",
        "role": "orientation",
        "degraded": True,
    }])
    assert "keep window" not in out
    assert "chapter-level match only" in out
    hit_line = [ln for ln in out.splitlines() if "media_bin_file_id=file-a" in ln][0]
    assert "start_seconds=" not in hit_line
