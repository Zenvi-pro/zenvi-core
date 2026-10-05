"""File > Import / Export Project > Remotion Project... dialogs with real Qt.

Needs real Qt (the stubbed suite auto-ignores this file):

    QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \\
        .venv/bin/python -m pytest tests/test_handoff_remotion_ui_qt.py -q
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qt_api  # noqa: E402,F401  (pick the binding before the QApplication exists)
from PyQt5.QtCore import QThread, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow, QMenu  # noqa: E402

if QThread is None:  # the headless stub (file named explicitly without ZENVI_REAL_QT=1)
    pytest.skip("needs real Qt: set ZENVI_REAL_QT=1", allow_module_level=True)

from classes.handoff import ui_registry  # noqa: E402
from classes.handoff.remotion import detect, importer, sources  # noqa: E402
from remotion_fakes import make_project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _App:
    def __init__(self):
        self._tr = lambda text: text
        self.project = type("P", (), {"current_filepath": None, "needs_save": lambda self: False})()


@pytest.fixture
def dialogs(qapp, monkeypatch):
    from classes.handoff.remotion import dialogs as module
    from windows import linked_clip_dialog
    app = _App()
    monkeypatch.setattr(module, "get_app", lambda: app)
    monkeypatch.setattr(linked_clip_dialog, "get_app", lambda: app)
    return module


def _listing(root, *, zenvi=False, static=False):
    project = detect.inspect_project(root)
    if zenvi:
        os.makedirs(os.path.join(root, "src", "zenvi"), exist_ok=True)
        with open(os.path.join(root, "src", "zenvi", "timeline.json"), "w") as fh:
            fh.write('{"zenvi_timeline": 1, "source_project": "trip", "zenvi": {"project": {}}}')
        project = detect.inspect_project(root)
    scan = sources.scan_project(root, project.entry)
    comps = [importer.Composition("TitleCard", 1920, 1080, 30.0, 90, {"title": "Hello", "accent": "#FF5A36"},
                                  source=scan.get("TitleCard")),
             importer.Composition("Scene", 1280, 720, 30.0, 60, {}, source=scan.get("Scene"))]
    if static:
        comps = [importer.Composition(c.id, None, None, None, None, kind=c.kind, source=c.source) for c in comps]
    return importer.Listing(project, comps, [], static_only=static)


def test_import_dialog_lists_compositions_and_returns_the_choice(dialogs, tmp_path):
    listing = _listing(make_project(str(tmp_path / "p")))
    dialog = dialogs.RemotionImportDialog(listing)
    assert dialog.tree.topLevelItemCount() == 2 and dialog.open_radio is None
    assert dialog.tree.topLevelItem(0).text(1) == "1920x1080" and dialog.tree.topLevelItem(0).text(3) == "0:03.00"
    assert dialog.tree.topLevelItem(0).text(4) == "Graphics"
    assert [dialog.codec_combo.itemData(i) for i in range(dialog.codec_combo.count())] == \
        ["auto", "prores4444", "h264", "qtrle"]
    dialog.tree.topLevelItem(1).setCheckState(0, Qt.Unchecked)
    dialog.codec_combo.setCurrentIndex(2)
    dialog._accept()
    choice = dialog.choice()
    assert (choice.mode, choice.ids, choice.codec) == ("linked", ["TitleCard"], "h264")
    assert "remotion.dev/license" in dialog.findChild(type(dialog.error_label), "remotionLicence").text()


def test_edit_props_uses_the_linked_clip_props_editor(dialogs, tmp_path, monkeypatch):
    from windows.linked_clip_dialog import LinkedClipDialog
    listing = _listing(make_project(str(tmp_path / "p")))
    dialog = dialogs.RemotionImportDialog(listing)
    seen = {}

    def fake_exec(self):
        seen["button"] = self.apply_button.text()
        seen["props"] = dict(self._props)
        self._result = dict(self._props, title="Launch day")
        return True

    monkeypatch.setattr(LinkedClipDialog, "exec_", fake_exec)
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
    dialog._edit_props()
    assert seen["button"] == "Use These Props" and seen["props"] == {"title": "Hello", "accent": "#FF5A36"}
    dialog._accept()
    assert dialog.choice().props == {"TitleCard": {"title": "Launch day", "accent": "#FF5A36"}}


def test_a_zenvi_export_offers_the_native_restore_first(dialogs, tmp_path):
    listing = _listing(make_project(str(tmp_path / "p")), zenvi=True)
    dialog = dialogs.RemotionImportDialog(listing)
    assert dialog.open_radio.isChecked() and not dialog.tree.isEnabled()
    assert dialog.tree.topLevelItem(0).checkState(0) == Qt.Unchecked
    dialog.linked_radio.click()
    assert dialog.tree.isEnabled() and dialog.mode() == "linked"
    dialog.native_radio.click()
    dialog._accept()
    assert dialog.choice().mode == "native"


def test_an_uninstalled_project_offers_to_install(dialogs, tmp_path):
    listing = _listing(make_project(str(tmp_path / "p"), installed=False), static=True)
    dialog = dialogs.RemotionImportDialog(listing)
    assert dialog.install_button is not None and "npm install" in dialog.install_button.text()
    assert not dialog.ok_button.isEnabled() and not dialog.codec_combo.isEnabled()
    dialog._install()
    assert dialog.choice().mode == "install"


def test_export_dialog_defaults_and_values(dialogs, tmp_path):
    dialog = dialogs.RemotionExportDialog(str(tmp_path / "trip-remotion"))
    assert dialog.values() == {"output_dir": str(tmp_path / "trip-remotion"), "copy_media": True, "install": False}
    dialog.copy_check.setChecked(False)
    dialog.install_check.setChecked(True)
    assert dialog.values()["copy_media"] is False and dialog.values()["install"] is True


def test_menu_entries_appear_under_import_and_export_project(dialogs, qapp, monkeypatch):
    from windows import handoff_menus
    import classes.handoff.remotion  # noqa: F401  (registers the entries)
    monkeypatch.setattr(handoff_menus, "get_app", lambda: _App())
    window = QMainWindow()
    window.menuFile = QMenu("File", window)
    window.menuImport_Project = QMenu("Import Project", window.menuFile)
    window.menuExport = QMenu("Export Project", window.menuFile)
    window.menuFile.addMenu(window.menuImport_Project)
    window.menuFile.addMenu(window.menuExport)
    menus = handoff_menus.HandoffMenus(window)
    try:
        menus.rebuild()
        assert "Remotion Project..." in [a.text() for a in window.menuImport_Project.actions()]
        assert "Remotion Project..." in [a.text() for a in window.menuExport.actions()]
    finally:
        ui_registry.remove_listener(menus._registry_changed)
        window.deleteLater()
