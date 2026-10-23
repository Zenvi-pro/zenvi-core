"""Transcription dock: word rows grouped into readable lines, clip filter, live re-render."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from windows import index_panel


def _w(text, start, end, **extra):
    return {"text": text, "startFrame": round(start * 30), "timelineStartSec": start,
            "timelineEndSec": end, **extra}


TWO_CLIPS = {
    "transcriptionSource": "local-apple",
    "clips": [
        {"clipId": "C1", "title": "Intro", "words": [_w("Hello", 0.0, 0.4)]},
        {"clipId": "C2", "words": [_w("Bye", 5.0, 5.4, speakerId="S1")]},
    ],
}


class _Item:
    """QListWidgetItem stand-in that keeps the label and the seek payload."""

    def __init__(self, text=""):
        self.text = text
        self.data = None

    def setData(self, role, value):
        self.data = value

    def setFlags(self, flags):
        pass


def _new_panel(app=None, parent=None):
    """A real IndexPanel on the headless Qt stub. MagicMock builds child mocks by
    calling the class's base, which for an IndexPanel instance is IndexPanel
    itself, so the two Qt calls __init__ makes on self are stubbed here."""
    with patch("classes.app.get_app", return_value=app), \
            patch.object(index_panel.IndexPanel, "setObjectName", MagicMock()), \
            patch.object(index_panel.IndexPanel, "setWidget", MagicMock()):
        return index_panel.IndexPanel(parent)


def _render_rows(data, selected=()):
    rows, status = [], MagicMock()
    panel = SimpleNamespace(
        _data=data,
        _generation=2,
        _list=SimpleNamespace(clear=rows.clear, addItem=rows.append),
        _status=status,
        _app=SimpleNamespace(window=SimpleNamespace(selected_clips=list(selected))),
    )
    with patch.object(index_panel, "QListWidgetItem", _Item):
        index_panel.IndexPanel._render(panel)
    status.setText.assert_called_once()
    return rows, status.setText.call_args.args[0]


def test_lines_break_on_a_pause_of_seven_tenths_or_more():
    words = [_w("a", 0.0, 0.5), _w("b", 1.0, 1.2), _w("c", 1.9, 2.0)]  # gaps: 0.5 s, 0.7 s
    assert [ln["text"] for ln in index_panel.transcript_lines(words)] == ["a b", "c"]


@pytest.mark.parametrize("mark", [".", "?", "!"])
def test_lines_break_after_sentence_end(mark):
    words = [_w("Hello" + mark, 0.0, 0.4), _w("there,", 0.45, 0.8), _w("yes", 0.85, 1.1)]
    assert [ln["text"] for ln in index_panel.transcript_lines(words)] == ["Hello" + mark, "there, yes"]


def test_lines_wrap_at_twelve_words():
    words = [_w(f"w{i}", i * 0.2, i * 0.2 + 0.1) for i in range(30)]
    assert [len(ln["text"].split()) for ln in index_panel.transcript_lines(words)] == [12, 12, 6]


def test_line_carries_start_frame_speaker_and_clip_and_splits_on_speaker_change():
    words = [_w("Hi", 3.2, 3.6, speakerId="S1"), _w("there", 3.7, 4.0, speakerId="S1"),
             _w("Yes", 4.1, 4.3, speakerId="S2")]
    assert index_panel.transcript_lines(words, clip_id="C1") == [
        {"text": "Hi there", "startSec": 3.2, "startFrame": 96, "speakerId": "S1", "clipId": "C1"},
        {"text": "Yes", "startSec": 4.1, "startFrame": 123, "speakerId": "S2", "clipId": "C1"},
    ]


@pytest.mark.parametrize("sec, text", [(0.0, "0:00.0"), (3.2, "0:03.2"), (65.4, "1:05.4"), (59.96, "1:00.0")])
def test_timecode_is_minutes_seconds_tenths(sec, text):
    assert index_panel._timecode(sec) == text


def test_all_clips_show_under_named_headers_with_timecoded_lines():
    rows, status = _render_rows(TWO_CLIPS)
    assert [r.text for r in rows] == ["Intro", "0:00.0  Hello", "C2", "0:05.0  [S1] Bye"]
    assert rows[0].data is None and rows[2].data is None  # headers are not seek targets
    assert rows[3].data["startFrame"] == 150 and rows[3].data["startSec"] == 5.0
    assert status == "2 lines · 2 words · local-apple · gen 2"


def test_selected_clip_shows_only_its_lines_without_headers():
    rows, status = _render_rows(TWO_CLIPS, selected=["C2"])
    assert [r.text for r in rows] == ["0:05.0  [S1] Bye"]
    assert status == "1 line · 1 word · C2 · local-apple · gen 2"


def test_several_selected_clips_keep_their_headers():
    rows, _status = _render_rows(TWO_CLIPS, selected=["C1", "C2"])
    assert [r.text for r in rows] == ["Intro", "0:00.0  Hello", "C2", "0:05.0  [S1] Bye"]


def test_pause_is_measured_from_the_word_end_when_only_end_sec_is_known():
    words = [{"text": "long", "startSec": 0.0, "endSec": 1.0}, {"text": "word", "startSec": 1.1, "endSec": 1.3}]
    assert [ln["text"] for ln in index_panel.transcript_lines(words)] == ["long word"]


def test_selection_is_followed_through_the_main_window_it_is_built_in():
    """The dock is built inside MainWindow.__init__, before get_app().window is set."""
    parent = SimpleNamespace(SelectionChanged=MagicMock())
    _new_panel(SimpleNamespace(window=None), parent=parent)
    parent.SelectionChanged.connect.assert_called_once()


def test_selection_change_rerenders_from_last_receipt_without_reloading():
    window = SimpleNamespace(SelectionChanged=MagicMock(), selected_clips=[])
    panel = _new_panel(SimpleNamespace(window=window))
    connect = window.SelectionChanged.connect
    connect.assert_called_once()
    rows = []
    panel._list = SimpleNamespace(clear=rows.clear, addItem=rows.append)
    panel._status = MagicMock()
    panel._data = TWO_CLIPS
    window.selected_clips = ["C2"]
    with patch("classes.agent_tools.transcript.get_transcript") as get, \
            patch.object(index_panel, "QListWidgetItem", _Item):
        connect.call_args.args[0]()
    get.assert_not_called()
    assert [r.text for r in rows] == ["0:05.0  [S1] Bye"]


def test_dock_is_titled_transcription():
    titles = []

    def base_init(self, *args, **kwargs):
        titles.append(args[0])
        MagicMock.__init__(self)

    base = index_panel.IndexPanel.__mro__[1]  # the Qt dock base
    with patch.object(base, "__init__", base_init):
        _new_panel()
    assert titles == ["Transcription"]


def test_project_change_drops_the_last_receipt():
    project = SimpleNamespace(_data={})
    panel = SimpleNamespace(_app=SimpleNamespace(project=project), _loading=True, _data=TWO_CLIPS,
                            _list=MagicMock(), _status=MagicMock(), _generation=2)
    panel._project_token = lambda: index_panel.IndexPanel._project_token(panel)
    token = panel._project_token()
    project._data = {}  # File > Open replaced the project data
    index_panel.IndexPanel._populate(panel, None, token)
    assert panel._data is None
    panel._status.setText.assert_called_with("Project changed. Press Refresh.")
