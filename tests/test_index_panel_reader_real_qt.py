"""The Index dock's reader under real Qt: words are links, playback lights one at a time, a click seeks."""

import json

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from types import SimpleNamespace  # noqa: E402

from PyQt5.QtCore import QObject, QUrl, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow  # noqa: E402

from windows import index_panel  # noqa: E402


class FakeWindow(QMainWindow):
    previewFrameSignal = pyqtSignal(int)
    SeekSignal = pyqtSignal(int)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def receipt():
    words = [{"text": "Hello", "startFrame": 10}, {"text": "there", "startFrame": 40}, {"text": "friend", "startFrame": 500}]
    return json.dumps({"contract": 3, "status": "applied", "tool": "get_transcript_tool", "summary": "ok",
                       "data": {"transcriptGeneration": 1, "transcriptionSource": "local-apple", "clips": [{"clipId": "C1", "words": words}]}})


def panel_with_words(qapp):
    win = FakeWindow()
    token = object()
    panel = index_panel.IndexPanel(win)
    panel._app = SimpleNamespace(window=win, project=SimpleNamespace(_data=token))
    panel._populate(receipt(), token)
    return win, panel


def lit(panel):
    return [s.cursor.selectedText() for s in panel._view.extraSelections()]


def test_words_are_laid_out_as_a_reading_and_each_has_a_place_in_it(qapp):
    _win, panel = panel_with_words(qapp)
    assert panel._view.toPlainText().split() == ["Hello", "there", "friend"]
    assert sorted(panel._word_spans) == [0, 1, 2]


def test_playback_lights_the_spoken_word_and_only_repaints_when_it_changes(qapp):
    win, panel = panel_with_words(qapp)
    win.previewFrameSignal.emit(5)
    assert lit(panel) == []
    win.previewFrameSignal.emit(12)
    assert lit(panel) == ["Hello"]
    win.previewFrameSignal.emit(41)
    assert lit(panel) == ["there"]
    win.previewFrameSignal.emit(2000)
    assert lit(panel) == [], "the last word does not stay lit for ever"


def test_clicking_a_word_seeks_the_playhead_to_it(qapp):
    win, panel = panel_with_words(qapp)
    seen = []
    win.SeekSignal.connect(seen.append)
    panel._on_link(QUrl("w:1"))
    assert seen == [41], "frame 40 plus the one-frame offset the rest of the editor uses"
    panel._on_link(QUrl("https://example.org"))
    assert seen == [41]


def test_loading_a_new_transcript_clears_the_highlight(qapp):
    win, panel = panel_with_words(qapp)
    win.previewFrameSignal.emit(12)
    panel._populate(receipt(), panel._app.project._data)
    assert lit(panel) == [] and panel._lit == -1
