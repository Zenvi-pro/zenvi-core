"""Smoke tests for timeline search tool handler outputs."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless CI).
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


def _clip_row(clip_id, *, position=0.0, start=0.0, end=30.0, layer=1000000):
    c = MagicMock()
    c.id = clip_id
    c.data = {
        "file_id": "file-1",
        "position": position,
        "start": start,
        "end": end,
        "layer": layer,
        "title": "Tennis",
    }
    return c


def test_list_clips_includes_source_window_and_summary_preview():
    file_data = {
        "name": "match.mp4",
        "path": "/tmp/match.mp4",
        "duration": 120.0,
        "ai_metadata": {
            "analyzed": True,
            "tags": {"objects": ["tennis"], "scenes": ["court"], "activities": []},
            "scene_descriptions": [{"time": 5.0, "description": "tennis serve"}],
        },
    }
    clip = _clip_row("clip-1", start=0.0, end=30.0)
    mock_file = MagicMock()
    mock_file.get.return_value = MagicMock(data=file_data)
    mock_clip = MagicMock()
    mock_clip.filter.return_value = [clip]
    mock_app = MagicMock()
    mock_app.project.get.return_value = [{"number": 1000000, "id": "t1"}]

    with patch.object(tool_handlers, "_get_app", return_value=mock_app):
        with patch.dict(sys.modules, {"classes.query": MagicMock(Clip=mock_clip, File=mock_file)}):
            out = tool_handlers.list_clips()
    assert "source_start=" in out
    assert "source_end=" in out
    assert "summary_preview=" in out


def test_get_timeline_placements_metadata_rows():
    ctx = MagicMock()
    ctx.timeline_clip_id = "c1"
    ctx.file_id = "f1"
    ctx.parent_file_id = "f1"
    ctx.title = "Tennis"
    ctx.layer = 1000000
    ctx.ui_track = 1
    ctx.timeline_position = 0.0
    ctx.timeline_end = 30.0
    ctx.source_start = 0.0
    ctx.source_end = 30.0
    ctx.index_status = "ready"
    ctx.summary_preview = "tennis, court"
    ctx.effective_metadata = {"scene_descriptions": [{"time": 5.0, "description": "serve"}]}

    with patch(
        "classes.timeline_clip_context.enumerate_timeline_contexts",
        return_value=[ctx],
    ):
        out = tool_handlers.get_timeline_placements_metadata()
    assert "timeline_clip_id=c1" in out
    assert "source_window=0.00-30.00s" in out
    assert "tennis" in out


if __name__ == "__main__":
    test_list_clips_includes_source_window_and_summary_preview()
    test_get_timeline_placements_metadata_rows()
    print("test_tool_handlers_timeline_search: ok")
