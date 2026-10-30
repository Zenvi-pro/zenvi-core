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


# ---------------------------------------------------------------------------
# Review round 1: trust before any Node, open-as-editable, export over edits
# ---------------------------------------------------------------------------

class _SyncJob:
    def __init__(self):
        self.state, self.result, self.error = "done", None, None

    def report(self, *_a):
        pass

    def should_cancel(self):
        return False


@pytest.fixture
def sync_jobs(monkeypatch):
    """handoff.jobs.submit_job, run inline (on_done right after the work)."""
    from classes.handoff import jobs
    submitted = []

    def submit(fn, *, label, on_done=None, **_kw):
        submitted.append(label)
        job = _SyncJob()
        try:
            job.result = fn(job)
        except jobs.JobCancelled:
            job.state = jobs.CANCELLED
        except Exception as exc:  # noqa: BLE001  (the real executor reports it the same way)
            job.state, job.error = jobs.FAILED, exc
        if on_done is not None:
            on_done(job)
        return job

    monkeypatch.setattr(jobs, "submit_job", submit)
    return submitted


def test_a_projects_code_runs_only_after_the_user_trusts_it(dialogs, sync_jobs, tmp_path, monkeypatch):
    from classes.handoff.remotion import helper
    from remotion_fakes import FakeHelper
    fake = FakeHelper()
    monkeypatch.setattr(helper, "run_helper", fake)
    from classes.handoff.remotion import trust
    trust.reset()
    root = make_project(str(tmp_path / "promo"))
    asked, shown = [], []
    answers = iter([None, "run"])

    def ask(window, project, install=False):
        asked.append((project.name, install, list(fake.commands())))
        return next(answers)

    monkeypatch.setattr(dialogs, "ask_trust", ask)
    monkeypatch.setattr(dialogs, "_show_import_dialog", lambda window, listing, key: shown.append(listing))
    dialogs.read_project(None, root)
    assert asked == [("fake-remotion", False, [])] and fake.calls == [] and shown == []  # declined: nothing ran
    dialogs.read_project(None, root)
    assert len(asked) == 2 and asked[1][2] == []                    # asked before anything ran
    assert fake.commands() == ["compositions"] and not shown[0].static_only
    dialogs.read_project(None, os.path.join(root, "src"))          # the same project: remembered for the session
    assert len(asked) == 2 and fake.commands() == ["compositions", "compositions"]


def test_installing_dependencies_asks_first_too(dialogs, sync_jobs, tmp_path, monkeypatch):
    from classes.handoff.remotion import trust
    trust.reset()
    root = make_project(str(tmp_path / "bare"), installed=False)
    asked, installs, shown = [], [], []
    monkeypatch.setattr(dialogs, "ask_trust", lambda w, p, install=False: asked.append(install) or None)
    monkeypatch.setattr(dialogs, "_install", lambda window, folder: installs.append(folder))
    monkeypatch.setattr(dialogs, "_show_import_dialog", lambda window, listing, key: shown.append((listing, key)))
    dialogs.read_project(None, root)                                # not installed: the dialog, no question yet
    assert asked == [] and shown and shown[0][0].static_only
    listing, key = shown[0]
    dialogs._start_import(None, listing, dialogs.ImportChoice(mode="install"), key)
    assert asked == [True] and installs == []                       # declined: npm never ran


class _Window:
    def __init__(self):
        self.opened, self.emitted, self.saved = [], [], 0
        outer = self

        class _Signal:
            def emit(self, path):
                outer.emitted.append(path)

        self.OpenProjectSignal = _Signal()

    def open_project(self, path, interactive=True):
        self.opened.append((path, interactive))
        return True

    def actionSave_trigger(self):
        self.saved += 1


def _zenvi_export(_unused, tmp_path):
    """A real Zenvi export (exporter on a small project) next to trip.zvn, like the default export folder."""
    from classes.handoff.remotion import exporter
    from classes.handoff.timeline_view import TimelineSnapshot
    media = tmp_path / "beach.mp4"
    media.write_bytes(b"media")
    data = {"id": "TRIP000001", "fps": {"num": 30, "den": 1}, "width": 1920, "height": 1080,
            "layers": [{"id": "L1", "number": 1000000, "label": "", "lock": False, "y": 0}],
            "files": [{"id": "F1", "path": str(media), "media_type": "video", "duration": 5.0, "width": 1920,
                       "height": 1080, "has_audio": False, "has_video": True, "fps": {"num": 30, "den": 1},
                       "video_length": "150", "name": "beach.mp4"}],
            "clips": [{"id": "C1", "file_id": "F1", "layer": 1000000, "position": 0.0, "start": 0.0, "end": 5.0,
                       "title": "beach", "reader": {"path": str(media), "media_type": "video", "duration": 5.0,
                                                    "width": 1920, "height": 1080}}],
            "effects": [], "markers": []}
    out = tmp_path / "trip-remotion"
    snapshot = TimelineSnapshot.from_project(exporter.project_copy(data), str(tmp_path / "trip.zvn"))
    exporter.export_project(snapshot, exporter.project_copy(data), str(out), generator="Zenvi test")
    return detect.inspect_project(str(out))


def test_open_as_editable_settles_unsaved_changes_first_and_never_writes_the_open_project(
        dialogs, sync_jobs, tmp_path, monkeypatch):
    from classes.handoff.remotion import restore
    from PyQt5.QtWidgets import QMessageBox
    trip = tmp_path / "trip.zvn"
    trip.write_text('{"the": "open project, unsaved changes in memory"}')
    project = _zenvi_export(None, tmp_path)
    app = dialogs.get_app()
    app.project = type("P", (), {"current_filepath": str(trip), "needs_save": lambda self: True})()
    events = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **k: events.append("asked: save first?") or QMessageBox.No))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda w, title, text: warnings.append(text)))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    real_write = restore.write_project_file

    def write(*a, **k):
        events.append("written")
        return real_write(*a, **k)

    monkeypatch.setattr(restore, "write_project_file", write)
    defaults = []
    # 1) the user types the open project's own name: refused, the open project is untouched
    monkeypatch.setattr(dialogs, "ask_restored_path", lambda w, default: defaults.append(default) or str(trip))
    window = _Window()
    dialogs._open_restored(window, project)
    assert events[0] == "asked: save first?" and "is the project open in Zenvi" in warnings[-1]
    assert trip.read_text() == '{"the": "open project, unsaved changes in memory"}'
    assert defaults[0] == str(tmp_path / "trip (from Remotion).zvn")  # a new name, never trip.zvn
    assert window.opened == [] and window.emitted == []
    # 2) the proposed name: written after the question, opened once -- no second "save changes?" prompt
    events.clear()
    monkeypatch.setattr(dialogs, "ask_restored_path", lambda w, default: default)
    dialogs._open_restored(window, project)
    assert events == ["asked: save first?", "written"]
    assert window.opened == [(str(tmp_path / "trip (from Remotion).zvn"), False)]
    assert json_load(tmp_path / "trip (from Remotion).zvn")["clips"][0]["id"] == "C1"


def json_load(path):
    import json
    with open(str(path)) as fh:
        return json.load(fh)


def test_open_as_editable_stops_when_the_save_is_cancelled(dialogs, sync_jobs, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    project = _zenvi_export(None, tmp_path)
    app = dialogs.get_app()
    app.project = type("P", (), {"current_filepath": "", "needs_save": lambda self: True})()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    asked = []
    monkeypatch.setattr(dialogs, "ask_restored_path", lambda w, default: asked.append(default) or default)
    window = _Window()  # actionSave_trigger "cancels": the project still needs saving
    dialogs._open_restored(window, project)
    assert window.saved == 1 and asked == [] and window.opened == [] and window.emitted == []


def test_the_restored_project_save_dialog_adds_zvn_itself(dialogs, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog
    seen = {}

    def fake_exec(self):
        seen.update(suffix=self.defaultSuffix(), mode=self.acceptMode(), files=self.selectedFiles())
        return True

    monkeypatch.setattr(QFileDialog, "exec_", fake_exec)
    path = dialogs.ask_restored_path(None, str(tmp_path / "trip (from Remotion).zvn"))
    assert seen["suffix"] == "zvn" and seen["mode"] == QFileDialog.AcceptSave
    assert path.endswith("trip (from Remotion).zvn")


def test_exporting_over_remotion_side_edits_asks_before_replacing_them(dialogs, sync_jobs, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from classes.handoff.remotion import exporter
    calls = []

    def export(snapshot, data, output_dir, **kw):
        calls.append(kw["replace_edits"])
        if not kw["replace_edits"]:
            raise exporter.ExportHasEdits("has edits", ["changes to src/zenvi/ZenviClip.tsx"])
        return {"clips": 1, "output_dir": output_dir, "next_steps": []}

    monkeypatch.setattr(exporter, "export_project", export)
    monkeypatch.setattr(exporter, "snapshot_from_app", lambda: ("snap", {}))
    texts = []

    def exec_(self):
        texts.append(self.text() + "\n" + self.informativeText())
        for button in self.buttons():
            if button.text() in ("Replace Them", "OK"):
                self._chosen = button
        return 0

    monkeypatch.setattr(QMessageBox, "exec_", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: getattr(self, "_chosen", None))
    monkeypatch.setattr(dialogs.RemotionExportDialog, "exec_", lambda self: True)
    dialogs.export_remotion_project(None)
    assert calls == [False, True]
    assert "changes to src/zenvi/ZenviClip.tsx" in texts[0] and "Import the folder first" in texts[0]
    assert dialogs.default_export_dir(str(tmp_path / "trip.zvn")) == str(tmp_path / "trip-remotion")
    (tmp_path / "trip-remotion").mkdir()
    assert dialogs.default_export_dir(str(tmp_path / "trip.zvn")) == str(tmp_path / "trip-remotion-2")


# ---------------------------------------------------------------------------
# Verification round: dynamic compositions, the trust gate beyond File > Import
# ---------------------------------------------------------------------------

def _dynamic_project(root, *, installed=True):
    from remotion_fakes import DYNAMIC_ROOT_TSX
    make_project(root, installed=installed)
    with open(os.path.join(root, "src", "Root.tsx"), "w") as fh:
        fh.write(DYNAMIC_ROOT_TSX)  # templates.map(t => <Composition id={t.id} ... />)
    return root


def test_compositions_registered_dynamically_are_read_with_node_after_the_trust_question(
        dialogs, sync_jobs, tmp_path, monkeypatch):
    """The static scan finds none: an installed project must still be asked about and listed by Remotion."""
    from classes.handoff.remotion import helper, sources, trust
    from remotion_fakes import DYNAMIC_COMPOSITIONS, FakeHelper
    trust.reset()
    fake = FakeHelper(DYNAMIC_COMPOSITIONS)
    monkeypatch.setattr(helper, "run_helper", fake)
    monkeypatch.setattr(helper, "prune_bundles", lambda *a, **k: [])
    root = _dynamic_project(str(tmp_path / "templates"))
    assert sources.scan_project(root, "src/index.ts") == {}
    asked, shown, infos = [], [], []
    monkeypatch.setattr(dialogs, "ask_trust", lambda w, p, install=False: asked.append(p.name) or "run")
    monkeypatch.setattr(dialogs, "_show_import_dialog", lambda window, listing, key: shown.append(listing))
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    dialogs.read_project(None, root)
    assert infos == [] and asked == ["fake-remotion"] and fake.commands() == ["compositions"]
    assert [c.id for c in shown[0].compositions] == ["IntroCard", "OutroCard"] and not shown[0].static_only


def test_an_uninstalled_dynamic_project_offers_install_instead_of_saying_it_has_none(
        dialogs, sync_jobs, tmp_path, monkeypatch):
    from classes.handoff.remotion import trust
    trust.reset()
    root = _dynamic_project(str(tmp_path / "templates"), installed=False)
    shown, infos = [], []
    monkeypatch.setattr(dialogs, "_show_import_dialog", lambda window, listing, key: shown.append(listing))
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    dialogs.read_project(None, root)
    assert infos == [] and shown and shown[0].static_only and shown[0].compositions == []
    dialog = dialogs.RemotionImportDialog(shown[0])
    assert dialog.install_button is not None  # Install, then Remotion lists them


def test_the_trust_question_for_a_re_render_or_studio_names_what_runs(dialogs, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    seen = []

    def exec_(self):
        seen.append(self.text())
        self._chosen = [b for b in self.buttons() if b.text() == "Run Its Code"][0]
        return 0

    monkeypatch.setattr(QMessageBox, "exec_", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: getattr(self, "_chosen", None))
    assert dialogs.ask_trust_for("/p/promo", "promo", "render") is True
    assert dialogs.ask_trust_for("/p/promo", "promo", "studio") is True
    assert seen[0].startswith("Re-rendering promo runs its code") and "Remotion Studio" in seen[1]
