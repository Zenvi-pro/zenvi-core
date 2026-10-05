"""Handoff menus, the linked-clips status bar and the props dialog with real Qt.

Needs real Qt (the stubbed suite auto-ignores this file):

    QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \\
        .venv/bin/python -m pytest tests/test_handoff_ui_qt.py -q
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
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QLineEdit, QMainWindow, QMenu, QPlainTextEdit, QPushButton, QSpinBox,
    QStatusBar, QToolBar,
)

if QThread is None:  # the headless stub (file named explicitly without ZENVI_REAL_QT=1)
    pytest.skip("needs real Qt: set ZENVI_REAL_QT=1", allow_module_level=True)

from classes.handoff import jobs, ui_registry  # noqa: E402
from handoff_fakes import FakeHost, write_discovery  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _App:
    def __init__(self, qapp):
        self._tr = lambda text: text
        self.applicationStateChanged = qapp.applicationStateChanged
        self.aboutToQuit = _Signal()


class _Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.menuFile = QMenu("File", self)
        self.menuBar().addMenu(self.menuFile)
        self.menuImport_Project = QMenu("Import Project", self.menuFile)
        self.menuImport_Project.addAction("EDL...")
        self.menuExport = QMenu("Export Project", self.menuFile)
        self.menuExport.addAction("Export Video...")
        self.menuFile.addAction("Save")
        self.menuFile.addSeparator()
        self.menuFile.addMenu(self.menuImport_Project)
        self.menuFile.addMenu(self.menuExport)
        self.menuFile.addSeparator()
        self.menuFile.addAction("Quit")
        self.statusBar = QStatusBar(self)
        self.setStatusBar(self.statusBar)
        self.statusBar.hide()  # every theme hides it; the pill lives on the main toolbar
        self.toolBar = QToolBar(self)
        self.addToolBar(self.toolBar)


def _pump(qapp, until, timeout=10.0):
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return until()


@pytest.fixture
def ui(qapp, tmp_path, monkeypatch):
    from classes import info
    from windows import handoff_menus, linked_clip_dialog, linked_source_menu
    app = _App(qapp)
    window_app = app
    for mod in (handoff_menus, linked_clip_dialog, linked_source_menu):
        monkeypatch.setattr(mod, "get_app", lambda: app)
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    saved = {k: dict(v) for k, v in ui_registry._actions.items()}
    calls = []
    ui_registry.register_export_action("probe_export", "Probe Export...", calls.append, order=5)
    ui_registry.register_import_action("probe_import", "Probe Import...", calls.append, order=5)
    ui_registry.register_send_action("probe_ae", "After Effects", "aftereffects", calls.append, order=5)
    window = _Window()
    monkeypatch.setattr(handoff_menus.LinkedClipsStatus, "_linked_files", lambda self: [])
    handoff_menus.install_handoff_menus(window)
    # what a theme does when it builds the main toolbar
    window.toolBar.addWidget(window.handoff_status).setVisible(window.handoff_status.is_active)
    window.test_app = window_app
    yield window, calls, tmp_path
    window.handoff_status.timer.stop()
    jobs.remove_listener(window.handoff_status._on_job)
    ui_registry.remove_listener(window.handoff_menus._registry_changed)
    for k, v in saved.items():
        ui_registry._actions[k].clear()
        ui_registry._actions[k].update(v)
    window.deleteLater()


def test_export_and_import_entries_follow_a_separator(ui, qapp):
    window, calls, _ = ui
    export_texts = [a.text() for a in window.menuExport.actions()]
    assert export_texts[:3] == ["Export Video...", "", "Probe Export..."]
    import_texts = [a.text() for a in window.menuImport_Project.actions()]
    assert import_texts[:3] == ["EDL...", "", "Probe Import..."]
    window.menuExport.actions()[2].trigger()
    assert calls == [window]


def test_send_to_sits_after_export_and_follows_the_host(ui, qapp):
    window, calls, tmp_path = ui
    texts = [a.menu().title() if a.menu() else a.text() for a in window.menuFile.actions()]
    assert texts.index("Send To") == texts.index("Export Project") + 1
    send = window.menuSendTo.actions()[0]
    assert send.text() == "After Effects" and not send.isEnabled()
    window.handoff_menus.refresh_hosts(force=True)
    assert _pump(qapp, lambda: not window.handoff_menus._refreshing)
    send = window.menuSendTo.actions()[0]
    assert not send.isEnabled() and "Zenvi Link" in send.toolTip()

    host = FakeHost()
    try:
        write_discovery(str(tmp_path / "user"), host)
        window.handoff_menus.refresh_hosts(force=True)
        assert _pump(qapp, lambda: window.menuSendTo.actions()[0].isEnabled())
        window.menuSendTo.actions()[0].trigger()
        assert calls == [window]
    finally:
        host.stop()


def test_toolbar_pill_shows_running_jobs_and_cancels_them(ui, qapp):
    window, _calls, _ = ui
    status = window.handoff_status
    assert not status.isVisible()
    window.show()
    release = threading.Event()

    def work(job):
        job.report(0.5, "Rendering Intro")
        while not job.should_cancel() and not release.wait(0.01):
            pass
        job.raise_if_cancelled()

    slot = next(a for a in window.toolBar.actions() if window.toolBar.widgetForAction(a) is status)
    assert not slot.isVisible()
    job = jobs.submit_job(work, label="Rendering Intro", key="F1")
    assert _pump(qapp, lambda: status.isVisible() and status.label.text() == "Rendering Intro")
    assert slot.isVisible() and status.is_active
    assert status.cancel_button.isVisible() and status.progress.value() == 500
    status.cancel_button.click()
    assert _pump(qapp, lambda: job.finished)
    assert job.state == jobs.CANCELLED
    assert _pump(qapp, lambda: not status.isVisible())
    assert not slot.isVisible()
    status.show_message("Re-rendered 1 linked clip(s)", ms=200)
    assert status.isVisible() and status.label.text() == "Re-rendered 1 linked clip(s)"
    assert _pump(qapp, lambda: not status.isVisible())
    window.hide()


def test_stale_notice_offers_re_render(ui, qapp, monkeypatch):
    from classes.handoff.linked_media import LinkCheck
    from windows import handoff_menus
    window, _calls, _ = ui
    status = window.handoff_status
    monkeypatch.setattr(handoff_menus.LinkedClipsStatus, "_linked_files", lambda self: [{"id": "F1"}])
    from classes.handoff import linked_media
    monkeypatch.setattr(linked_media, "check_link", lambda data, compute=True: LinkCheck("F1", "remotion", "stale"))
    rerendered = []
    monkeypatch.setattr(handoff_menus, "rerender_files", lambda win, ids, **kw: rerendered.append(ids))
    window.show()
    status.check_soon(force=True)
    assert _pump(qapp, lambda: status.label.text() == "1 linked clip changed")
    assert status.rerender_button.isVisible()
    status.rerender_button.click()
    assert rerendered == [["F1"]]
    window.hide()


def test_props_dialog_builds_editors_and_returns_typed_props(qapp, monkeypatch):
    from windows import linked_clip_dialog
    monkeypatch.setattr(linked_clip_dialog, "get_app", lambda: _App(qapp))
    link = {"kind": "remotion", "source": {"composition": "Intro", "project_dir": "/p"},
            "props": {"title": "Hello", "accent": "#FF5A36", "count": 3, "speed": 1.5, "loop": False,
                      "items": [1, 2]}}
    d = linked_clip_dialog.LinkedClipDialog(link, check={"state": "stale", "detail": "code changed"})
    assert isinstance(d.findChild(QLineEdit, "prop_title"), QLineEdit)
    assert isinstance(d.findChild(QPushButton, "prop_accent"), QPushButton)
    assert isinstance(d.findChild(QSpinBox, "prop_count"), QSpinBox)
    assert isinstance(d.findChild(QLineEdit, "prop_speed"), QLineEdit)  # floats are text: no rounding
    assert isinstance(d.findChild(QCheckBox, "prop_loop"), QCheckBox)
    assert isinstance(d.findChild(QPlainTextEdit, "prop_items"), QPlainTextEdit)
    assert "Source changed" in d.state_label.text()
    d.findChild(QLineEdit, "prop_title").setText("Launch day")
    d.findChild(QLineEdit, "prop_title").textEdited.emit("Launch day")  # what typing does
    d.findChild(QSpinBox, "prop_count").setValue(5)
    d.findChild(QCheckBox, "prop_loop").setChecked(True)
    d._apply()
    assert d.props() == {"title": "Launch day", "accent": "#FF5A36", "count": 5, "speed": 1.5, "loop": True,
                         "items": [1, 2]}


def test_props_dialog_raw_json_round_trip_and_errors(qapp, monkeypatch):
    from windows import linked_clip_dialog
    monkeypatch.setattr(linked_clip_dialog, "get_app", lambda: _App(qapp))
    d = linked_clip_dialog.LinkedClipDialog({"kind": "hyperframes", "props": {"title": "A"}}, can_render=False)
    assert not d.apply_button.isEnabled() and "cannot render" in d.error_label.text()
    d.tabs.setCurrentIndex(1)
    assert '"title": "A"' in d.json_edit.toPlainText()
    d.json_edit.setPlainText('{"title": "B", "extra": true}')
    d.tabs.setCurrentIndex(0)
    assert d.findChild(QCheckBox, "prop_extra").isChecked()
    d.tabs.setCurrentIndex(1)
    d.json_edit.setPlainText("[1]")
    d._apply()
    assert d.props() is None and "JSON object" in d.error_label.text()


def test_quit_stops_handoff_jobs(ui):
    window, _calls, _ = ui
    assert jobs.shutdown in window.test_app.aboutToQuit.slots
    assert window.handoff_status.timer.stop in window.test_app.aboutToQuit.slots


def test_props_dialog_keeps_untouched_values_exact(qapp, monkeypatch):
    from windows import linked_clip_dialog
    monkeypatch.setattr(linked_clip_dialog, "get_app", lambda: _App(qapp))
    props = {"ratio": 0.123456789, "stamp": 1759622400000, "label": "x", "nested": {"a": [1, 2.5]}}
    d = linked_clip_dialog.LinkedClipDialog({"kind": "remotion", "props": dict(props)})
    d._apply()
    assert d.props() == props  # nothing touched, nothing changed (no spin-box rounding or clamping)
    ratio = d.findChild(QLineEdit, "prop_ratio")
    ratio.setText("0.25")
    ratio.textEdited.emit("0.25")
    d._apply()
    assert d.props()["ratio"] == 0.25 and d.props()["stamp"] == 1759622400000


def test_page_switch_labels_are_never_clipped_and_switch_pages(qapp, monkeypatch):
    from windows import linked_clip_dialog
    monkeypatch.setattr(linked_clip_dialog, "get_app", lambda: _App(qapp))
    d = linked_clip_dialog.LinkedClipDialog({"kind": "remotion", "props": {"title": "A"}})
    d.show()
    qapp.processEvents()
    assert d.tabs.tabBar().isHidden()  # the theme's icon-only tab styling cannot clip anything
    for index, text in enumerate(("Props", "Raw JSON")):
        button = d.findChild(QPushButton, "propsPage%d" % index)
        assert button.text() == text
        assert button.width() >= button.fontMetrics().horizontalAdvance(text) + 8
    d.findChild(QPushButton, "propsPage1").click()
    assert d.tabs.currentIndex() == 1 and '"title": "A"' in d.json_edit.toPlainText()
    d.findChild(QPushButton, "propsPage0").click()
    assert d.tabs.currentIndex() == 0 and d.findChild(QPushButton, "propsPage0").isChecked()
    d.hide()


def test_edit_props_shows_the_providers_editable_props(qapp, monkeypatch):
    from classes.handoff import linked_media
    from windows import linked_clip_dialog, linked_source_menu
    app = _App(qapp)
    for mod in (linked_clip_dialog, linked_source_menu):
        monkeypatch.setattr(mod, "get_app", lambda: app)

    class _Provider:
        kind = "remotion"

        def editable_props(self, link):
            return dict({"size": 96}, **link["props"])

    link = {"kind": "remotion", "source": {"composition": "Intro"}, "props": {"title": "Hi", "internal": 3}}

    class _File:
        id = "F1"
        data = {"name": "Intro"}

    monkeypatch.setattr(linked_source_menu, "_file_and_link", lambda fid: (_File(), dict(link)))
    monkeypatch.setattr(linked_media, "provider_for", lambda kind: _Provider())
    shown, rerendered = {}, []

    class _Dialog:
        def __init__(self, link, **kwargs):
            shown.update(kwargs)

        def exec_(self):
            return 1

        def props(self):
            return dict(shown["props"], title="Bye")

    monkeypatch.setattr(linked_clip_dialog, "LinkedClipDialog", _Dialog)
    from windows import handoff_menus
    monkeypatch.setattr(handoff_menus, "rerender_files", lambda win, ids, **kw: rerendered.append((ids, kw)))
    window = _Window()
    job = linked_source_menu.edit_props(window, "F1")
    assert _pump(qapp, lambda: bool(rerendered))
    assert job.error is None and shown["props"] == {"size": 96, "title": "Hi", "internal": 3}
    ids, kw = rerendered[0]
    assert ids == ["F1"] and kw["replace_props"] is True
    assert kw["props"] == {"title": "Bye", "internal": 3, "size": 96}
    window.deleteLater()
