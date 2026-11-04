"""Transcription dock: the reader shows the selected clips' words and follows the selection."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from windows import index_panel


def _w(text, start):
    return {"text": text, "startFrame": round(start * 30), "timelineStartSec": start}


TWO_CLIPS = {
    "transcript_generation": 2,
    "transcriptionSource": "local-apple",
    "clips": [
        {"clipId": "C1", "words": [_w("Hello", 0.0), _w("there", 0.5)]},
        {"clipId": "C2", "words": [_w("Bye", 5.0)]},
    ],
}


def _new_panel(app=None, parent=None):
    """A real IndexPanel on the headless Qt stub. MagicMock builds child mocks by
    calling the class's base, which for an IndexPanel instance is IndexPanel
    itself, so the two Qt calls __init__ makes on self are stubbed here."""
    with patch("classes.app.get_app", return_value=app), \
            patch.object(index_panel.IndexPanel, "setObjectName", MagicMock()), \
            patch.object(index_panel.IndexPanel, "setWidget", MagicMock()):
        return index_panel.IndexPanel(parent)


def _render(data, selected=()):
    shown, status = [], MagicMock()
    panel = SimpleNamespace(
        _data=data,
        _generation=2,
        _status=status,
        _fps=lambda: 30.0,
        _show_words=lambda model: shown.extend(w["text"] for w in model["words"]),
        _app=SimpleNamespace(window=SimpleNamespace(selected_clips=list(selected))),
    )
    index_panel.IndexPanel._render(panel)
    return shown, status.setText.call_args.args[0]


def test_nothing_selected_reads_every_clip():
    shown, status = _render(TWO_CLIPS)
    assert shown == ["Hello", "there", "Bye"]
    assert status == "3 words · local-apple · gen 2"


def test_a_selected_clip_reads_only_its_words():
    shown, status = _render(TWO_CLIPS, selected=["C2"])
    assert shown == ["Bye"]
    assert status == "1 word · local-apple · gen 2"


def test_a_selected_clip_without_words_says_so():
    shown, status = _render(TWO_CLIPS, selected=["C9"])
    assert shown == []
    assert status.startswith("Selected clip not transcribed")


def test_selection_is_followed_through_the_main_window_it_is_built_in():
    """The dock is built inside MainWindow.__init__, before get_app().window is set."""
    parent = SimpleNamespace(SelectionChanged=MagicMock(), previewFrameSignal=MagicMock())
    _new_panel(SimpleNamespace(window=None), parent=parent)
    parent.SelectionChanged.connect.assert_called_once()


def test_selection_change_rerenders_from_last_receipt_without_reloading():
    window = SimpleNamespace(SelectionChanged=MagicMock(), selected_clips=[])
    panel = _new_panel(SimpleNamespace(window=window))
    connect = window.SelectionChanged.connect
    connect.assert_called_once()
    shown = []
    panel._show_words = lambda model: (shown.clear(), shown.extend(w["text"] for w in model["words"]))
    panel._status = MagicMock()
    panel._fps = lambda: 30.0
    panel._data = TWO_CLIPS
    window.selected_clips = ["C2"]
    with patch("classes.agent_tools.transcript.get_transcript") as get:
        connect.call_args.args[0]()
    get.assert_not_called()
    assert shown == ["Bye"]


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
                            _show_words=MagicMock(), _status=MagicMock(), _generation=2)
    panel._project_token = lambda: index_panel.IndexPanel._project_token(panel)
    token = panel._project_token()
    project._data = {}  # File > Open replaced the project data
    index_panel.IndexPanel._populate(panel, None, token)
    assert panel._data is None
    panel._status.setText.assert_called_with("Project changed. Press Refresh.")
