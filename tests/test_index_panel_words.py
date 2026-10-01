"""The Index dock lists transcript words, so it must ask get_transcript for them.

get_transcript's default receipt is compact (data.script only, no words), and
the dock showed "0 words" for a transcribed clip.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from windows import index_panel


def _receipt(words):
    return json.dumps({
        "contract": 3,
        "status": "applied",
        "tool": "get_transcript_tool",
        "summary": "ok",
        "data": {
            "transcriptGeneration": 2,
            "transcriptionSource": "local-apple",
            "clips": [{"clipId": "C1", "words": words}],
        },
    })


def _panel(added):
    return SimpleNamespace(
        _app=MagicMock(),
        _list=SimpleNamespace(clear=lambda: added.clear(), addItem=added.append),
        _status=MagicMock(),
        _generation=None,
    )


class _InlineThread:
    """threading.Thread stand-in that records the target and runs it on start()."""

    started = []

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target
        self.name = name

    def start(self):
        _InlineThread.started.append(self.name)
        self._target()


def test_refresh_requests_words_and_lists_them():
    words = [
        {"text": "Welcome", "startFrame": 3, "timelineStartSec": 0.1},
        {"text": "basically", "startFrame": 160, "timelineStartSec": 5.33},
    ]
    added = []
    panel = _panel(added)
    panel._load_transcript = lambda: index_panel.IndexPanel._load_transcript(panel)
    panel._populate = lambda raw: index_panel.IndexPanel._populate(panel, raw)
    _InlineThread.started.clear()
    with patch("classes.agent_tools.transcript.get_transcript", return_value=_receipt(words)) as get, \
            patch.object(index_panel.threading, "Thread", _InlineThread), \
            patch("classes.qt_main_thread.invoke_on_gui", side_effect=lambda f, *a: f(*a)) as gui:
        index_panel.IndexPanel.refresh(panel)

    # Transcription can take seconds on a cache miss: it runs on a worker,
    # and only the list update is handed back to the GUI thread.
    assert _InlineThread.started == ["index_panel_transcript"]
    gui.assert_called_once()
    get.assert_called_once()
    assert get.call_args.kwargs.get("includeWords") is True
    assert len(added) == 2
    panel._status.setText.assert_called_with("2 words · local-apple · gen 2")
    assert panel._loading is False


def test_refresh_ignores_clicks_while_loading():
    panel = _panel([])
    panel._loading = True
    with patch.object(index_panel.threading, "Thread") as thread:
        index_panel.IndexPanel.refresh(panel)
    thread.assert_not_called()


def test_failed_load_reports_and_unlocks():
    added = ["stale"]
    panel = _panel(added)
    panel._loading = True
    index_panel.IndexPanel._populate(panel, RuntimeError("asr helper missing"))
    assert added == []
    assert panel._loading is False
    panel._status.setText.assert_called_with("Refresh failed: asr helper missing")
