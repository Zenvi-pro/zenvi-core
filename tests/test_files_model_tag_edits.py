"""A programmatic Tags-cell refresh must never be saved into another file.

Regression: FilesModel.value_updated saved every Tags-cell change into the
*selected* file, including the model's own refresh after a tool (or undo)
changed another file's tags, and a nested refresh cleared ``ignore_updates``
while the outer one was still writing cells.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

# The files model subclasses QThread at import time, so this needs real Qt
# (ZENVI_REAL_QT=1); the stubbed suite auto-ignores importorskip files.
pytest.importorskip("PyQt5.QtWidgets")

from windows.models import files_model as fm  # noqa: E402
from windows.models.files_model import FilesModel  # noqa: E402


class _Item:
    def __init__(self, text, row=0, column=0):
        self._text, self._row, self._column = text, row, column

    def text(self):
        return self._text

    def data(self, _role=0):
        return self._text

    def row(self):
        return self._row

    def column(self):
        return self._column


class _File:
    saved = []

    def __init__(self, fid, tags=""):
        self.id = fid
        self.data = {"id": fid, "tags": tags}

    def save(self):
        _File.saved.append((self.id, self.data["tags"]))


def _model(ignore, rows):
    return SimpleNamespace(
        ignore_updates=ignore,
        model=SimpleNamespace(item=lambda row, col: _Item(rows[row]) if col == 5 else None),
        current_file=lambda: pytest.fail("must not fall back to the selected file"),
    )


def test_refresh_of_a_tags_cell_is_not_saved():
    _File.saved = []
    files = {"DRONE": _File("DRONE", "b-roll"), "LAKE": _File("LAKE", "")}
    with patch.object(fm.File, "get", side_effect=lambda id: files.get(id)):
        FilesModel.value_updated(_model(True, ["DRONE"]), _Item("b-roll, aerial", 0, 2))
    assert _File.saved == []


def test_user_tag_edit_lands_on_the_edited_row():
    _File.saved = []
    files = {"DRONE": _File("DRONE", "b-roll"), "LAKE": _File("LAKE", "")}
    with patch.object(fm.File, "get", side_effect=lambda id: files.get(id)):
        FilesModel.value_updated(_model(False, ["LAKE", "DRONE"]), _Item("aerial", 1, 2))
        FilesModel.value_updated(_model(False, ["LAKE", "DRONE"]), _Item("Name", 1, 1))
    assert _File.saved == [("DRONE", "aerial")]


def test_nested_refresh_restores_the_outer_ignore_flag():
    seen = []
    model = SimpleNamespace(ignore_updates=False)

    def inner(**_kw):
        model.ignore_updates = True
        if not seen:
            seen.append("outer")
            FilesModel.update_model(model, clear=False, update_file_id="B")
            seen.append(model.ignore_updates)   # still True for the rest of the outer refresh
        model.ignore_updates = False

    model._update_model = inner
    FilesModel.update_model(model, clear=False, update_file_id="A")
    assert seen == ["outer", True] and model.ignore_updates is False


def test_tree_view_repaint_of_a_row_is_not_a_save():
    """Regression: every optimize-progress tooltip repaint re-saved the file (one empty undo step each)."""
    from windows.views import files_treeview as tv

    saved, emitted = [], []

    class _F:
        def __init__(self):
            self.data = {"id": "V", "path": "/m/clip.mp4", "tags": "b-roll"}

        def save(self):
            saved.append(dict(self.data))

    f = _F()
    cells = {1: "clip.mp4", 2: "b-roll", 5: "V"}
    view = SimpleNamespace(
        files_model=SimpleNamespace(ignore_updates=False,
                                    model=SimpleNamespace(item=lambda row, col: _Item(cells[col]))),
        win=SimpleNamespace(FileUpdated=SimpleNamespace(emit=emitted.append)))
    app = patch.object(tv, "get_app", return_value=SimpleNamespace(_tr=lambda t: t))
    app.start()
    with patch.object(tv.File, "get", return_value=f):
        tv.FilesTreeView.value_updated(view, _Item("clip.mp4", 0, 0))
        assert saved == [] and emitted == []
        cells[1] = "Intro"
        tv.FilesTreeView.value_updated(view, _Item("Intro", 0, 1))
    assert saved[-1]["name"] == "Intro" and emitted == ["V"]
    with patch.object(tv.File, "get", return_value=None):
        tv.FilesTreeView.value_updated(view, _Item("x", 0, 1))   # placeholder row: no crash
    app.stop()
