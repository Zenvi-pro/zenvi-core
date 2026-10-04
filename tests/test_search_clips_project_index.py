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


# ============================ with the local index on, footage it does not cover is still searchable ============================
def _original_index():
    info = {"index_id": "idx-1", "index_name": "zenvi-proj", "indexed_count": 2,
            "video_map": {"vid-old": {"file_id": "file-old", "name": "old.mp4"}, "vid-new": {"file_id": "file-new", "name": "new.mp4"}}}
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.search.return_value = {"results": [
        {"video_id": "vid-new", "start": 1.0, "end": 3.0, "rank": 1, "filename": "new.mp4"},
        {"video_id": "vid-old", "start": 20.0, "end": 24.0, "rank": 2, "filename": "old.mp4"}]}
    return info, client


def test_local_answer_is_topped_up_with_files_only_the_original_index_knows():
    info, client = _original_index()
    local = "Found 1 match(es) across 1 project media item(s) (local index):\n  • new.mp4 media_bin_file_id=file-new media_type=video — start_seconds=1.000 end_seconds=3.000"
    with patch("classes.editor_tools.media_index_tools.legacy_search_clips", return_value=local), \
            patch("classes.editor_tools.media_index_tools.v1_only_file_ids", return_value={"file-old"}), \
            patch("classes.project_tl_index.collect_project_twelvelabs_index", return_value=info), \
            patch("classes.api_client.get_backend_client", return_value=client):
        out = tool_handlers.search_clips(query="dog", top_k="5")
    assert out.startswith(local) and "old.mp4" in out and "media_bin_file_id=file-old" in out
    assert out.count("new.mp4") == 1, "a file the local index already answered for is not listed twice"
    client.search.assert_called_once()


def test_when_the_local_index_finds_nothing_the_original_index_answers_alone():
    info, client = _original_index()
    with patch("classes.editor_tools.media_index_tools.legacy_search_clips", return_value="No index matches for 'dog' in this project's index (1 indexed media item(s))."), \
            patch("classes.editor_tools.media_index_tools.v1_only_file_ids", return_value={"file-old"}), \
            patch("classes.project_tl_index.collect_project_twelvelabs_index", return_value=info), \
            patch("classes.api_client.get_backend_client", return_value=client):
        out = tool_handlers.search_clips(query="dog", top_k="5")
    assert out.startswith("Found 1 match(es)") and "old.mp4" in out and "new.mp4" not in out


def test_nothing_is_added_when_every_file_is_in_the_local_index():
    local = "Found 1 match(es) across 1 project media item(s) (local index):\n  • a.mp4"
    with patch("classes.editor_tools.media_index_tools.legacy_search_clips", return_value=local), \
            patch("classes.editor_tools.media_index_tools.v1_only_file_ids", return_value=set()), \
            patch("classes.api_client.get_backend_client") as backend:
        assert tool_handlers.search_clips(query="dog", top_k="5") == local
    backend.assert_not_called()


def test_an_error_from_the_original_index_never_spoils_the_local_answer():
    local = "Found 1 match(es) across 1 project media item(s) (local index):\n  • a.mp4"
    with patch("classes.editor_tools.media_index_tools.legacy_search_clips", return_value=local), \
            patch("classes.editor_tools.media_index_tools.v1_only_file_ids", return_value={"file-old"}), \
            patch("classes.project_tl_index.collect_project_twelvelabs_index", return_value={"error": "no index", "video_map": {}}):
        assert tool_handlers.search_clips(query="dog", top_k="5") == local


def test_the_second_pass_ignores_hits_for_files_it_was_not_asked_about():
    info, client = _original_index()
    with patch("classes.project_tl_index.collect_project_twelvelabs_index", return_value=info), patch("classes.api_client.get_backend_client", return_value=client):
        out = tool_handlers.search_clips(query="dog", top_k="5", _only_files={"file-missing"})
    assert out.startswith("No index matches") and "old.mp4" not in out and "new.mp4" not in out
