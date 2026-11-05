"""File → Zenvi Cloud with real Qt: the work runs on a QThread, results and
questions come back to the GUI thread.

Needs real Qt and libopenshot (the stubbed suite auto-ignores this file):

    PYTHONPATH=$HOME/zenvi-deps-1.0/python QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 \\
        .venv/bin/python -m pytest tests/test_cloud_sync_qt.py -q
"""

from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PyQt5.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qt_api  # noqa: E402,F401  (pick the binding before the QApplication exists)
from PyQt5.QtCore import QThread  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow, QMenu, QMessageBox  # noqa: E402

if QThread is None:  # the headless stub (file named explicitly without ZENVI_REAL_QT=1)
    pytest.skip("needs real Qt: set ZENVI_REAL_QT=1", allow_module_level=True)

from _cloud_fake import (  # noqa: E402
    TOKEN,
    FakeCloud,
    make_local_project,
    store_link,
)
from classes import cloud_sync  # noqa: E402
from classes.cloud_sync import StaticTokenSource  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def cloud():
    fake = FakeCloud()
    yield fake
    fake.close()


class _Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.menuFile = QMenu("File", self)
        self.menuBar().addMenu(self.menuFile)
        self.actionSave = self.menuFile.addAction("Save")
        self.actionSaveAs = self.menuFile.addAction("Save As...")
        self.menuFile.addSeparator()
        self.menuFile.addAction("Import Files...")
        self.saved = []
        self.opened = []

    def actionSave_trigger(self):
        raise AssertionError("a clean project must not be saved again")

    def save_project(self, path):
        self.saved.append((path, threading.current_thread()))

    def open_project(self, path):
        self.opened.append(path)


class _Project:
    def __init__(self, path, files):
        self.current_filepath = path
        self._data = {"files": files, "zenvi_cloud": {}}
        self.has_unsaved_changes = False

    def needs_save(self):
        return self.has_unsaved_changes


class _Updates:
    def __init__(self):
        self.untracked = []
        self.data_version = 1

    def update_untracked(self, key, values):
        self.untracked.append((key, values))


class _App:
    def __init__(self, project, cloud_url):
        self.project = project
        self.updates = _Updates()
        self._tr = lambda text: text
        self._cloud_url = cloud_url

    def get_settings(self):
        app = self

        class _Settings:
            def get(self, key):
                return app._cloud_url if key == "zenvi-cloud-url" else None

        return _Settings()


@pytest.fixture
def setup(qapp, cloud, tmp_path, monkeypatch):
    from windows import cloud_sync_ui

    monkeypatch.delenv("ZENVI_CLOUD_URL", raising=False)
    local = make_local_project(tmp_path)
    files = [{"id": file_id, "path": path} for file_id, path in local.paths.items()]
    app = _App(_Project(str(local.file), files), cloud.base_url)
    monkeypatch.setattr(cloud_sync_ui, "get_app", lambda: app)
    shown = []
    monkeypatch.setattr(cloud_sync_ui.PushResultDialog, "exec_", lambda self: shown.append(self) or 0)
    window = _Window()
    controller = cloud_sync_ui.install_cloud_menu(window)
    controller._tokens = StaticTokenSource(TOKEN)
    return cloud_sync_ui, controller, window, app, local, shown


def _wait_until_idle(qapp, controller, timeout=30.0):
    deadline = time.time() + timeout
    while controller.busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert not controller.busy, "the cloud job did not finish"


def test_menu_sits_under_save_as(setup):
    _ui, _controller, window, _app, _local, _shown = setup
    texts = [a.text() if a.menu() is None else f"[{a.menu().title()}]" for a in window.menuFile.actions()]
    assert texts[:4] == ["Save", "Save As...", "[Zenvi Cloud]", ""]
    assert [a.text() for a in window.menuZenviCloud.actions()] == [
        "Push to Zenvi Cloud", "Open in Web Editor", "", "Open from Zenvi Cloud..."]


def test_push_runs_on_a_qthread_and_reports_back_on_the_gui_thread(setup, qapp, cloud, monkeypatch):
    ui, controller, window, app, local, shown = setup
    gui_thread = qapp.thread()
    ran_on = []
    labels = []
    real_push = cloud_sync.push_project
    real_describe = ui.describe_progress

    def spy_push(*args, **kwargs):
        ran_on.append(QThread.currentThread())
        return real_push(*args, **kwargs)

    def spy_describe(progress, _=lambda s: s):
        labels.append((progress.phase, QThread.currentThread() is gui_thread))
        return real_describe(progress, _)

    monkeypatch.setattr(cloud_sync, "push_project", spy_push)
    monkeypatch.setattr(ui, "describe_progress", spy_describe)

    controller.push()
    assert controller.busy          # returned at once; results arrive through the event loop
    _wait_until_idle(qapp, controller)

    assert ran_on and ran_on[0] is not gui_thread
    assert labels and all(on_gui for _phase, on_gui in labels)
    assert {"hash", "upload"} <= {phase for phase, _ in labels}
    assert len(shown) == 1 and len(cloud.projects) == 1
    link = next(values for key, values in app.updates.untracked if key == ["zenvi_cloud"])
    assert link["project_id"] in cloud.projects
    deadline = time.time() + 5
    while not window.saved and time.time() < deadline:
        time.sleep(0.01)
    assert window.saved and window.saved[0][0] == str(local.file)
    assert window.saved[0][1] is not threading.main_thread()   # saved off the GUI thread, like autosave
    assert all(action.isEnabled() for action in window.menuZenviCloud.actions())


def test_conflict_question_is_asked_on_the_gui_thread(setup, qapp, cloud, monkeypatch):
    ui, controller, _window, app, local, shown = setup
    controller.push()
    _wait_until_idle(qapp, controller)
    first = next(values for key, values in app.updates.untracked if key == ["zenvi_cloud"])
    result = cloud_sync.PushResult(project_id=first["project_id"], revision=first["revision"], editor_url="",
                                   name="", created=False, overwritten=False, link=first, cloud_refs={})
    store_link(local.file, result)
    cloud.edit_in_web(first["project_id"])
    asked_on = []

    def ask(conflict):
        asked_on.append(QThread.currentThread() is qapp.thread())
        return True

    monkeypatch.setattr(controller, "_ask_overwrite", ask)

    controller.push()
    _wait_until_idle(qapp, controller)

    assert asked_on == [True]
    assert len(shown) == 2 and "web_note" not in cloud.projects[first["project_id"]]["project"]


def _click(button_of):
    """A QMessageBox.exec_ stand-in that clicks one of the box's own buttons."""
    def fake_exec(box):
        box.button(button_of).click()
        return 0
    return fake_exec


def test_overwrite_question_returns_the_clicked_choice(setup, monkeypatch):
    _ui, controller, _window, _app, _local, _shown = setup
    monkeypatch.setattr(QMessageBox, "exec_", _click(QMessageBox.Save))
    assert controller._ask_overwrite(None) is True
    monkeypatch.setattr(QMessageBox, "exec_", _click(QMessageBox.Cancel))
    assert controller._ask_overwrite(None) is False


def test_sign_in_offer_opens_the_login_window_and_retries(setup, qapp, monkeypatch):
    _ui, controller, _window, _app, _local, _shown = setup
    import windows.login_window as login_window

    class _Login:
        Accepted = 1

        def __init__(self, parent=None):
            self.parent = parent

        def exec_(self):
            return _Login.Accepted

    monkeypatch.setattr(login_window, "LoginWindow", _Login)
    monkeypatch.setattr(QMessageBox, "exec_", _click(QMessageBox.Ok))
    retried = []

    controller._offer_sign_in("Your Zenvi session has expired.", lambda: retried.append(True))
    for _ in range(20):
        qapp.processEvents()
        if retried:
            break
        time.sleep(0.01)

    assert retried == [True]


def test_a_pulled_project_reads_back_through_the_desktop_loader(qapp, cloud, tmp_path):
    """The file pull writes is what File → Open reads: media paths stay on the
    pulled copies and @transitions/ markers expand to the bundled assets."""
    from classes import info
    from classes.json_data import JsonDataStore

    local = make_local_project(tmp_path)
    client = cloud_sync.CloudClient(cloud.base_url, StaticTokenSource(TOKEN), retry_delay=0.0)
    pushed = cloud_sync.push_project(client, cloud_sync.PushRequest(str(local.file), dict(local.paths)),
                                     website="https://zenvi.test")
    pulled = cloud_sync.pull_project(client, pushed.project_id, dest_root=str(tmp_path / "Cloud"))

    data = JsonDataStore().read_from_file(pulled.project_file, path_mode="absolute")

    media = os.path.join(os.path.dirname(pulled.project_file), "My Film_assets", "media")
    assert {f["id"]: f["path"] for f in data["files"]} == {
        "F1": os.path.join(media, "a.mp4"), "F2": os.path.join(media, "b.wav")}
    assert data["clips"][0]["reader"]["path"] == os.path.join(media, "a.mp4")
    assert data["effects"][0]["reader"]["path"] == os.path.join(info.PATH, "transitions", "common", "fade.svg")
    assert data["zenvi_cloud"]["project_id"] == pushed.project_id


def test_cancel_from_the_progress_dialog_stops_the_push(setup, qapp, cloud, monkeypatch):
    _ui, controller, _window, app, _local, shown = setup
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a)))

    controller.push()
    controller._cancel_clicked()     # what the progress dialog's Cancel button does
    _wait_until_idle(qapp, controller)

    assert cloud.projects == {} and shown == [] and warnings == []
    assert app.updates.untracked == []
    assert all(action.isEnabled() for action in controller._actions)
