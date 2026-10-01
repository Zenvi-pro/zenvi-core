"""The headless dialog guard against real Qt dialogs (offscreen platform).

Run with ZENVI_REAL_QT=1 QT_QPA_PLATFORM=offscreen (auto-skipped under the
headless Qt stub). Each prompt must come back with the answer Escape would
give -- in particular never NoButton, which the unsaved-changes prompts read
as "Don't Save".
"""

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from classes import headless  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def guard(qapp, monkeypatch):
    reported = []
    monkeypatch.setattr(headless, "report", reported.append)
    g = headless.DialogGuard()
    qapp.installEventFilter(g)
    yield reported
    qapp.removeEventFilter(g)


def test_save_changes_prompt_is_cancelled(guard):
    answer = QMessageBox.question(
        None, "Unsaved Changes", "Save changes to project first?",
        QMessageBox.Cancel | QMessageBox.No | QMessageBox.Yes)
    assert answer == QMessageBox.Cancel
    assert len(guard) == 1
    assert guard[0].startswith("dismissed a dialog nobody can answer here: ")
    # (macOS message boxes drop their window title, so only the text is certain.)
    assert guard[0].endswith("Save changes to project first?")


def test_replace_file_prompt_answers_no(guard):
    answer = QMessageBox.question(None, "Export Video", "x.mp4 already exists.",
                                  QMessageBox.No | QMessageBox.Yes)
    assert answer == QMessageBox.No


def test_warning_is_acknowledged(guard):
    assert QMessageBox.warning(None, "Error Saving Project", "disk full") == QMessageBox.Ok
    assert guard[0].endswith("disk full")


def test_custom_buttons_without_a_safe_choice_close_unanswered(guard):
    box = QMessageBox()
    box.setWindowTitle("Missing project files")
    skip = box.addButton("Skip all and open project", QMessageBox.AcceptRole)
    box.addButton("Locate folder...", QMessageBox.ActionRole)
    box.exec_()
    assert box.clickedButton() is not skip


def test_file_dialog_is_cancelled(guard):
    path, _filter = QFileDialog.getOpenFileName(
        None, "Open Project...", "", "Zenvi Project (*.zvn)",
        options=QFileDialog.DontUseNativeDialog)
    assert path == ""
