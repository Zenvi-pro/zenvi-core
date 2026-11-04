"""The HyperFrames import / export dialogs with real Qt.

Needs real Qt (the stubbed suite auto-ignores this file):

    QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \\
        .venv/bin/python -m pytest tests/test_handoff_hyperframes_ui_qt.py -q
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qt_api  # noqa: E402,F401  (pick the binding before the QApplication exists)
from PyQt5.QtCore import QThread  # noqa: E402
from PyQt5.QtWidgets import QApplication, QCheckBox, QLabel, QPlainTextEdit, QRadioButton  # noqa: E402

if QThread is None:  # the headless stub (file named explicitly without ZENVI_REAL_QT=1)
    pytest.skip("needs real Qt: set ZENVI_REAL_QT=1", allow_module_level=True)

from test_handoff_hyperframes_parser import root_div, write_project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _App:
    def __init__(self):
        self._tr = lambda text: text


@pytest.fixture
def dialogs(qapp, monkeypatch):
    from classes.handoff.hyperframes import dialogs as d
    from classes.handoff.hyperframes import importer
    monkeypatch.setattr(d, "get_app", lambda: _App())
    monkeypatch.setattr(importer, "ffprobe_media", lambda path: {"width": 1280, "height": 720, "duration": 10.0,
                                                                 "has_video": True, "has_audio": False})
    return d


def _inspection(tmp_path, script=""):
    from classes.handoff.hyperframes import importer
    root = write_project(tmp_path / "p", root_div(
        '<video id="v" class="clip" src="a.mp4" data-start="0" data-duration="4" muted></video>'
        '<h1 id="t" class="clip" data-start="0" data-duration="2">Hi</h1>', extra='data-duration="4"'),
        files={"a.mp4": b"v"}, script='const tl = gsap.timeline({paused:true});' + script +
        'window.__timelines["main"] = tl;')
    return importer.inspect_project(root, "auto", use_cli=False)


def test_import_dialog_shows_the_plan_and_returns_the_mode(dialogs, tmp_path):
    insp = _inspection(tmp_path)
    dlg = dialogs.HyperFramesImportDialog(insp)
    radios = {r.objectName(): r for r in dlg.findChildren(QRadioButton)}
    assert set(radios) == {"hyperframesMode_auto", "hyperframesMode_native", "hyperframesMode_flatten"}
    assert radios["hyperframesMode_auto"].isChecked() and insp.reason in radios["hyperframesMode_auto"].text()
    notes = dlg.findChild(QPlainTextEdit, "hyperframesImportNotes")
    assert notes is not None and "graphics" in notes.toPlainText()  # the title becomes a linked layer
    assert dlg.mode() == "auto"
    radios["hyperframesMode_flatten"].setChecked(True)
    assert dlg.mode() == "flatten"
    dlg.deleteLater()


def test_import_dialog_lists_what_cannot_be_rebuilt(dialogs, tmp_path):
    insp = _inspection(tmp_path, script='tl.to("#v", {x: 5, stagger: 0.1}, 0);')
    assert insp.mode == "flatten"
    dlg = dialogs.HyperFramesImportDialog(insp)
    text = dlg.findChild(QPlainTextEdit, "hyperframesImportNotes").toPlainText()
    assert "v: " in text and "stagger" in text
    dlg.deleteLater()


class _Job:
    def __init__(self):
        self.state, self.error, self.result = "done", None, None

    def report(self, *a, **k):
        pass

    def should_cancel(self):
        return False


class _FakeBox:
    def __init__(self, boxes):
        self.boxes = boxes

    def setWindowTitle(self, title):
        pass

    def setText(self, text):
        self.boxes.shown.append(text)

    def addButton(self, *a):
        return object()

    def exec_(self):
        return 0

    def clickedButton(self):
        return None


class _Boxes:
    Yes, No, ActionRole, Close = 1, 2, 3, 4

    def __init__(self, answer=2):
        self.shown, self.kinds, self.answer = [], [], answer

    def information(self, parent, title, text):
        self.shown.append(text)
        self.kinds.append("information")

    def warning(self, parent, title, text):
        self.shown.append(text)
        self.kinds.append("warning")

    def question(self, parent, title, text, *buttons):
        self.shown.append(text)
        self.kinds.append("question")
        return self.answer

    def __call__(self, parent=None):
        return _FakeBox(self)


def _run_now(monkeypatch):
    from classes.handoff import jobs

    def run_now(fn, *, on_done=None, **kw):
        job = _Job()
        try:
            job.result = fn(job)
        except Exception as exc:
            job.error, job.state = exc, "failed"
        on_done(job)
        return job
    monkeypatch.setattr(jobs, "submit_job", run_now)


@pytest.mark.parametrize("warnings, boxed", [
    (["already listed"], None),  # the dialog showed it: no second box, just the short message
    (["already listed", "the render is 2 frames short"], "the render is 2 frames short"),
])
def test_after_import_only_new_notes_get_a_box(dialogs, monkeypatch, warnings, boxed):
    from classes.handoff import jobs
    from classes.handoff.hyperframes import importer
    boxes, notes = _Boxes(), []
    monkeypatch.setattr(dialogs, "QMessageBox", boxes)
    monkeypatch.setattr(dialogs, "_notify", lambda window, text: notes.append(text))
    monkeypatch.setattr(importer, "inspect_project", lambda *a, **k: object())
    monkeypatch.setattr(importer, "run_import", lambda insp, **k: {"native": 2, "linked": 1, "warnings": warnings})

    def run_now(fn, *, on_done=None, **kw):
        job = _Job()
        job.result = fn(job)
        on_done(job)
        return job
    monkeypatch.setattr(jobs, "submit_job", run_now)
    dialogs._run_import(None, "/x/sample", "auto", 30.0, (1920, 1080), shown=["already listed"])
    assert notes == ["Imported sample: 2 native, 1 linked, 0 restored clip(s)"]
    if boxed is None:
        assert boxes.shown == []
    else:
        assert len(boxes.shown) == 1 and boxed in boxes.shown[0] and "already listed" not in boxes.shown[0]


def test_the_project_runs_nothing_before_the_trust_note(dialogs, monkeypatch, tmp_path):
    """Review C5-1 #8: picking the folder ran the project's own CLI (and npx with its .npmrc) first."""
    from classes.handoff.hyperframes import importer
    seen, shown = [], []
    folder = _inspection(tmp_path).project_dir
    real = importer.inspect_project
    monkeypatch.setattr(importer, "inspect_project", lambda *a, **k: seen.append(k.get("use_cli")) or real(*a, **k))
    monkeypatch.setattr(dialogs.QFileDialog, "getExistingDirectory", lambda *a, **k: folder)
    monkeypatch.setattr(dialogs.HyperFramesImportDialog, "exec_", lambda self: shown.append(self) or 0)
    monkeypatch.setattr(dialogs, "_project_settings", lambda: (30.0, (1920, 1080)))
    _run_now(monkeypatch)
    dialogs.import_hyperframes_project(None)
    assert seen == [False] and len(shown) == 1                      # Zenvi's own reading, then the dialog
    labels = " ".join(w.text() for w in shown[0].findChildren(QLabel))
    assert "once you import" in labels and "trust" in labels
    shown[0].deleteLater()


def test_a_busy_editor_is_not_reported_as_nothing_changed(dialogs, monkeypatch):
    """Review C5-1 #12: a commit timeout was shown as "nothing was changed" (a retry then imported twice)."""
    from classes.editor_tools.titles_text_common import CommitTimeout
    from classes.handoff.hyperframes import importer
    boxes = _Boxes()
    monkeypatch.setattr(dialogs, "QMessageBox", boxes)
    monkeypatch.setattr(importer, "inspect_project", lambda *a, **k: object())

    def busy(insp, **k):
        raise CommitTimeout("the editor was too busy")
    monkeypatch.setattr(importer, "run_import", busy)
    _run_now(monkeypatch)
    dialogs._run_import(None, "/x/sample", "auto", 30.0, (1920, 1080))
    assert boxes.kinds == ["information"] and "still being added" in boxes.shown[0]
    assert "nothing was changed" not in boxes.shown[0]


def test_export_asks_before_replacing_edits_made_in_hyperframes(dialogs, monkeypatch):
    from classes.handoff.hyperframes import cli as hf_cli
    from classes.handoff.hyperframes import exporter
    boxes = _Boxes(answer=_Boxes.Yes)
    monkeypatch.setattr(dialogs, "QMessageBox", boxes)
    calls = []

    def fake_export(snapshot, raw, folder, *, copy_media, overwrite_changes, **k):
        calls.append(overwrite_changes)
        if not overwrite_changes:
            raise exporter.ExportChanged("index.html changed", ["index.html"])
        return exporter.ExportResult(output_dir=folder, index=folder + "/index.html", files=[], assets={}, clips=2)
    monkeypatch.setattr(exporter, "export_project", fake_export)
    monkeypatch.setattr(hf_cli, "lint", lambda *a, **k: {"ok": True})
    _run_now(monkeypatch)
    dialogs._run_export(None, "/x/out", object(), {}, True, overwrite_changes=False)
    assert calls == [False, True] and boxes.kinds[0] == "question" and "index.html" in boxes.shown[0]
    assert any("Exported 2 clip(s)" in t for t in boxes.shown)
    boxes.answer, calls[:] = _Boxes.No, []
    dialogs._run_export(None, "/x/out", object(), {}, True, overwrite_changes=False)
    assert calls == [False]                                         # declined: nothing replaced


def test_export_dialog_copies_media_by_default(dialogs, tmp_path):
    dlg = dialogs.HyperFramesExportDialog(str(tmp_path))
    box = dlg.findChild(QCheckBox, "hyperframesCopyMedia")
    assert box is not None and box.isChecked()
    dlg.deleteLater()
