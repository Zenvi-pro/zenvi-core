"""Tests for project-wide TwelveLabs search_clips handler."""

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


def test_search_clips_uses_project_index_and_maps_file_id():
    info = {
        "index_id": "idx-project-1",
        "index_name": "zenvi-proj-abc",
        "indexed_count": 1,
        "video_map": {
            "vid-dog": {"file_id": "file-dog", "name": "dog.mp4"},
        },
    }
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.search.return_value = {
        "results": [
            {
                "video_id": "vid-dog",
                "start": 12.0,
                "end": 15.0,
                "rank": 1,
                "filename": "dog.mp4",
            }
        ]
    }

    with patch(
        "classes.project_tl_index.collect_project_twelvelabs_index",
        return_value=info,
    ), patch(
        "classes.api_client.get_backend_client",
        return_value=client,
    ):
        out = tool_handlers.search_clips(query="dog jumps", top_k="5")

    client.search.assert_called_once()
    kwargs = client.search.call_args
    # positional: query, then kwargs or mixed
    assert kwargs[0][0] == "dog jumps" or kwargs.kwargs.get("query") == "dog jumps"
    called_index = kwargs.kwargs.get("index_id")
    if called_index is None and len(kwargs[0]) > 1:
        # unlikely; prefer kwargs
        pass
    assert kwargs.kwargs.get("index_id") == "idx-project-1"
    assert "media_bin_file_id=file-dog" in out
    assert "start_seconds=12.000" in out
    assert "zenvi-proj-abc" in out or "idx-project-1" in out


def test_search_clips_errors_without_project_index():
    with patch(
        "classes.project_tl_index.collect_project_twelvelabs_index",
        return_value={
            "index_id": "",
            "index_name": "zenvi-proj",
            "video_map": {},
            "indexed_count": 0,
            "error": "No TwelveLabs-indexed videos in this project yet.",
        },
    ):
        out = tool_handlers.search_clips(query="hello")
    assert out.startswith("Error:")
    assert "Index" in out or "index" in out


def test_display_labels_include_search_clips():
    assert "search_clips_tool" in tool_handlers.AGENT_TOOL_HANDLERS
    assert "search_clips_tool" in tool_handlers.TOOL_DISPLAY_LABELS
    assert set(tool_handlers.TOOL_DISPLAY_LABELS) == set(
        tool_handlers.AGENT_TOOL_HANDLERS
    )
