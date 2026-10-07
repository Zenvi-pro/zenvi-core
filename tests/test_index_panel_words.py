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
    panel = SimpleNamespace(
        _app=SimpleNamespace(project=SimpleNamespace(_data={})),
        _list=SimpleNamespace(clear=lambda: added.clear(), addItem=added.append),
        _status=MagicMock(),
        _generation=None,
    )
    panel._project_token = lambda: index_panel.IndexPanel._project_token(panel)
    panel._load_transcript = lambda token=None: index_panel.IndexPanel._load_transcript(panel, token)
    panel._populate = lambda raw, token=None: index_panel.IndexPanel._populate(panel, raw, token)
    return panel


class _InlineThread:
    """threading.Thread stand-in that records the target and runs it on start()."""

    started = []

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target = target
        self._args = args
        self.name = name

    def start(self):
        _InlineThread.started.append(self.name)
        self._target(*self._args)


def test_refresh_requests_words_and_lists_them():
    words = [
        {"text": "Welcome", "startFrame": 3, "timelineStartSec": 0.1},
        {"text": "basically", "startFrame": 160, "timelineStartSec": 5.33},
    ]
    added = []
    panel = _panel(added)
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


def test_result_for_a_closed_project_is_dropped():
    """Opening another project mid-transcription must not list the old words."""
    added = []
    panel = _panel(added)
    token = index_panel.IndexPanel._project_token(panel)
    panel._app.project._data = {}  # File > Open replaced the project data
    words = [{"text": "Welcome", "startFrame": 3, "timelineStartSec": 0.1}]
    index_panel.IndexPanel._populate(panel, _receipt(words), token)
    assert added == []
    assert panel._loading is False
    panel._status.setText.assert_called_with("Project changed. Press Refresh.")


def test_delete_people_data_asks_first_and_deletes_off_the_gui_thread():
    import threading
    from classes.media_index import people
    seen = {}
    status = MagicMock()
    panel = SimpleNamespace(_status=status, _confirm_delete_people=lambda: False)
    panel._delete_people_worker = lambda: index_panel.IndexPanel._delete_people_worker(panel)
    with patch.object(people, "delete_all", side_effect=lambda **k: seen.setdefault("n", 1) and {"scans": 2, "registry": True, "models": 0}):
        index_panel.IndexPanel.delete_people_data(panel)
        assert "n" not in seen and not status.setText.called, "declining deletes nothing"
        panel._confirm_delete_people = lambda: True
        started = []
        real = threading.Thread

        def capture(*a, **k):
            t = real(*a, **k)
            started.append(t)
            return t

        with patch.object(index_panel.threading, "Thread", capture), patch("classes.qt_main_thread.invoke_on_gui", lambda f, *a, **k: f(*a, **k)):
            index_panel.IndexPanel.delete_people_data(panel)
            started[0].join(5)
    assert seen == {"n": 1} and started[0].name == "index_panel_delete_people"
    assert status.setText.call_args_list[-1].args[0] == "Deleted 2 face scan(s) and the people list."


def test_a_failing_delete_is_reported_not_raised():
    status = MagicMock()
    panel = SimpleNamespace(_status=status)
    with patch("classes.media_index.people.delete_all", side_effect=OSError("disk busy")), patch("classes.qt_main_thread.invoke_on_gui", lambda f, *a, **k: f(*a, **k)):
        index_panel.IndexPanel._delete_people_worker(panel)
    assert "disk busy" in status.setText.call_args.args[0]
