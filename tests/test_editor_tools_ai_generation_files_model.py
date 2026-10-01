"""Project Files must not write a stale name back when a tool renames and tags a file.

Found live with generate_tts_and_add_to_timeline_tool: saving a new name + tags made
update_model refresh the Tags cell; that cell change saved the file again (FilesModel.
value_updated had no ignore guard), the nested update_model reset ignore_updates to False,
and the outer call's next cell change reached FilesTreeView.value_updated, which saved the
row's old Name text over the new name -- inside the same undo step. The real methods run
here on a small stand-in for QStandardItemModel.
"""

import ast
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _Item:
    def __init__(self, model, row, col, text=""):
        self._model, self._row, self._col, self._text = model, row, col, text

    def text(self):
        return self._text

    def data(self, role=0):
        return self._text

    def row(self):
        return self._row

    def column(self):
        return self._col

    def _changed(self):
        for callback in list(self._model.callbacks):
            callback(self)

    def setText(self, text):
        self._text = text
        self._changed()

    def setToolTip(self, tip):
        self._changed()

    def setAccessibleText(self, text):
        self._changed()


class _Model:
    def __init__(self):
        self.callbacks = []
        self.rows = []

    def item(self, row, col):
        return self.rows[row][col]

    def setHorizontalHeaderLabels(self, labels):
        pass


class _Index:
    def __init__(self, row, file_id):
        self._row, self._id = row, file_id

    def isValid(self):
        return True

    def data(self):
        return self._id

    def row(self):
        return self._row


_FILES_MODEL = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "models", "files_model.py")
_METHODS = ("changed", "update_model", "value_updated", "invalidate_indexing_status", "_tooltip_for_file")


def _files_model_methods(namespace):
    """FilesModel's own methods, compiled from source (the module needs a real QThread to import)."""
    with open(_FILES_MODEL, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _FILES_MODEL)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FilesModel")
    funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    exec(compile(ast.Module(body=[funcs[n] for n in _METHODS], type_ignores=[]), _FILES_MODEL, "exec"),
         namespace)
    return {n: namespace[n] for n in _METHODS}


@pytest.fixture
def project_files(editor):
    from classes.logger import log
    from classes.query import File
    import windows.views.files_treeview as ft

    fid = editor.add_file("audio", path="/media/generated_ab12.mp3", name="generated_ab12.mp3", tags="")
    model = _Model()
    model.rows.append([_Item(model, 0, c, t) for c, t in enumerate(
        ["generated_ab12.mp3", "generated_ab12.mp3", "", "audio", "/media", fid])])
    methods = _files_model_methods({"File": File, "get_app": lambda: editor.app, "log": log, "os": os})
    FilesModel = type("FilesModel", (), dict(methods))
    fm = FilesModel()
    fm.model, fm.model_ids, fm.ignore_updates = model, {fid: _Index(0, fid)}, False
    fm._status_cache, fm.generation_queue = {}, None
    fm._proxy_service = lambda: None
    fm.ModelRefreshed = MagicMock()
    fm._rebuild_generation_placeholders = lambda: None
    fm.current_file_id = lambda: fid               # add_files selects the new file
    fm.current_file = lambda: File.get(id=fid)
    view = SimpleNamespace(files_model=fm, win=MagicMock())
    model.callbacks += [fm.value_updated, lambda item: ft.FilesTreeView.value_updated(view, item)]
    editor.manager.add_listener(fm)
    editor.window.resize_contents = lambda: None
    yield SimpleNamespace(fid=fid, model=model, fm=fm, File=File)
    editor.manager.updateListeners.remove(fm)


def test_a_programmatic_rename_with_tags_is_not_reverted(editor, project_files):
    f = project_files.File.get(id=project_files.fid)
    f.data["name"] = "Narration - Welcome to Lisbon"
    f.data["tags"] = "narration, tts"
    f.save()
    stored = editor.file(project_files.fid)
    assert stored["name"] == "Narration - Welcome to Lisbon"
    assert stored["tags"] == "narration, tts"
    assert project_files.model.item(0, 1).text() == "Narration - Welcome to Lisbon"
    assert project_files.fm.ignore_updates is False


def test_a_user_edit_of_the_tags_cell_still_saves(editor, project_files):
    project_files.model.item(0, 2).setText("beach, waves")
    assert editor.file(project_files.fid)["tags"] == "beach, waves"
