"""current_file_id() must not let a stale current index override the selection."""

from types import SimpleNamespace

import pytest

# The files model subclasses QThread at import time, so this needs real Qt
# (ZENVI_REAL_QT=1); the stubbed suite auto-ignores importorskip files.
pytest.importorskip("PyQt5.QtWidgets")

from windows.models.files_model import FilesModel  # noqa: E402


def _index(file_id, valid=True):
    index = SimpleNamespace()
    index.row = lambda: 0
    index.isValid = lambda: valid
    index.data = lambda *_args: file_id
    index.sibling = lambda _row, _col: index
    return index


def _model(selected, current):
    return SimpleNamespace(
        selection_model=SimpleNamespace(
            selectedRows=lambda _col: [_index(file_id) for file_id in selected],
            currentIndex=lambda: current,
        )
    )


def test_current_file_id_prefers_current_when_it_is_selected():
    model = _model(["F1", "F2"], _index("F2"))
    assert FilesModel.current_file_id(model) == "F2"


def test_current_file_id_ignores_stale_current_outside_selection():
    # Switching details/list views can leave currentIndex on a row that is no
    # longer selected; a tag edit must then land on the selected file.
    model = _model(["F2"], _index("F1"))
    assert FilesModel.current_file_id(model) == "F2"


def test_current_file_id_falls_back_to_current_without_selection():
    model = _model([], _index("F1"))
    assert FilesModel.current_file_id(model) == "F1"


def test_current_file_id_none_without_selection_or_current():
    model = _model([], _index(None, valid=False))
    assert FilesModel.current_file_id(model) is None
