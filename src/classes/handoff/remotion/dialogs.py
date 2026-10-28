"""Qt side of File > Import Project / Export Project > Remotion Project... (GUI thread only).

The handlers ask for folders and choices with dialogs, then hand the work to
``handoff.jobs`` (the toolbar pill shows progress with Cancel): reading the
compositions, installing dependencies, rendering, exporting. Results come
back on the GUI thread as a short message (``handoff_menus.notify``) or a
message box with what to do next. One import = one undo step.

Nothing of a picked project runs before the user trusts it: the first look
is the static scan (no Node); reading sizes and props, rendering and
installing run its code (``remotion.config.*``, its components, npm
scripts), so Zenvi asks first and remembers the answer for that folder until
it quits (:mod:`classes.handoff.remotion.trust`, which also gates re-renders
and Open in Studio). Disk work (existence checks, names, timeline.json)
stays in jobs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from qt_api import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, Qt,
)

from classes.app import get_app
from classes.handoff.remotion import trust
from classes.logger import log

CODEC_LABELS = (
    ("auto", "Automatic (ProRes 4444 when transparent, else H.264)"),
    ("prores4444", "ProRes 4444 (keeps transparency)"),
    ("h264", "H.264 (opaque, smaller)"),
    ("qtrle", "QuickTime Animation (transparency, large files)"),
)
LICENCE_NOTE = ("Zenvi renders with this project's own Remotion, which runs the project's code (like npx remotion "
                "render): import projects you trust. Zenvi does not include Remotion; it is free for individuals and "
                "companies of up to 3 people, larger companies need a company licence (remotion.dev/license).")
TRUST_TEXT = ("Reading %s runs its code: Zenvi uses the project's own Remotion, which evaluates its remotion.config "
              "and compositions with Node.js and a headless browser, like npx remotion render. Only continue with "
              "projects you trust.")
INSTALL_TRUST_TEXT = ("Installing the dependencies of %s runs code from its packages (npm install scripts). Only "
                      "continue with projects you trust.")
ACTION_TRUST_TEXTS = {
    "render": ("Re-rendering %s runs its code: Zenvi uses the project's own Remotion, which evaluates its "
               "remotion.config and compositions with Node.js and a headless browser, like npx remotion render. "
               "Only continue with projects you trust."),
    "studio": ("Opening %s in Remotion Studio runs its code (its remotion.config and compositions, with Node.js), "
               "like npx remotion studio. Only continue with projects you trust."),
    "read": TRUST_TEXT,
}


def _tr(text: str) -> str:
    try:
        return get_app()._tr(text)
    except Exception:
        return text


def _notify(window, text: str) -> None:
    try:
        from windows.handoff_menus import notify
        notify(window, text)
    except Exception:
        log.debug("handoff notify unavailable", exc_info=True)


def _start_dir(window) -> str:
    try:
        path = getattr(get_app().project, "current_filepath", None)
        if path:
            return os.path.dirname(path)
    except Exception:
        pass
    return os.path.expanduser("~")


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    return "%d:%05.2f" % (int(seconds // 60), seconds % 60)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@dataclass
class ImportChoice:
    mode: str                                  # open | native | linked | install
    ids: List[str] = field(default_factory=list)
    props: Dict[str, dict] = field(default_factory=dict)
    codec: str = "auto"


class RemotionImportDialog(QDialog):
    """Pick what to bring in from a Remotion project (compositions, props, codec, or the native restore)."""

    def __init__(self, listing, parent=None):
        super().__init__(parent)
        _ = _tr
        self.listing = listing
        self.project = listing.project
        self._props: Dict[str, dict] = {}
        self._choice: Optional[ImportChoice] = None
        self.setObjectName("RemotionImportDialog")
        self.setWindowTitle(_("Import Remotion Project"))
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)

        info = QFormLayout()
        info.addRow(_("Project:"), QLabel("%s — %s" % (self.project.name, self.project.root)))
        info.addRow(_("Remotion:"), QLabel(self.project.version or _("not found")))
        info.addRow(_("Entry:"), QLabel(self.project.entry or _("not found")))
        layout.addLayout(info)

        self.mode_group = QButtonGroup(self)
        self.open_radio = self.native_radio = self.linked_radio = None
        if self.project.is_zenvi_generated:
            note = QLabel(_("Zenvi exported this project. Bring its timeline back as native, editable clips, or "
                            "render its compositions as linked clips."))
            note.setWordWrap(True)
            layout.addWidget(note)
            self.open_radio = QRadioButton(_("Open as an editable Zenvi project (recommended)"))
            self.native_radio = QRadioButton(_("Add its timeline to this project as native clips"))
            self.linked_radio = QRadioButton(_("Import compositions as linked clips"))
            for i, radio in enumerate((self.open_radio, self.native_radio, self.linked_radio)):
                self.mode_group.addButton(radio, i)
                layout.addWidget(radio)
            self.open_radio.setChecked(True)
            self.mode_group.buttonClicked.connect(lambda _b: self._sync())

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("remotionCompositions")
        self.tree.setHeaderLabels([_("Composition"), _("Size"), _("FPS"), _("Duration"), _("Folder")])
        self.tree.setRootIsDecorated(False)
        for comp in listing.compositions:
            size = "%sx%s" % (comp.width, comp.height) if comp.width else "?"
            fps = ("%g" % comp.fps) if comp.fps else "?"
            name = comp.id + (" " + _("(still)") if comp.kind == "still" else "")
            item = QTreeWidgetItem([name, size, fps, _fmt_duration(comp.seconds), comp.source.folder
                                    if comp.source is not None and comp.source.folder else ""])
            item.setData(0, Qt.UserRole, comp.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            default_on = not self.project.is_zenvi_generated
            item.setCheckState(0, Qt.Checked if default_on else Qt.Unchecked)
            self.tree.addTopLevelItem(item)
        for col in range(5):
            self.tree.resizeColumnToContents(col)
        layout.addWidget(self.tree)

        row = QHBoxLayout()
        self.props_button = QPushButton(_("Edit Props..."))
        self.props_button.clicked.connect(self._edit_props)
        row.addWidget(self.props_button)
        row.addStretch(1)
        row.addWidget(QLabel(_("Render as:")))
        self.codec_combo = QComboBox()
        for value, label in CODEC_LABELS:
            self.codec_combo.addItem(_(label), value)
        row.addWidget(self.codec_combo)
        layout.addLayout(row)

        self.install_button = None
        if listing.static_only and not self.project.installed:
            warn = QLabel(_("This project is not installed (node_modules is missing), so Zenvi cannot read sizes, "
                            "durations or props, or render it yet."))
            warn.setWordWrap(True)
            layout.addWidget(warn)
            self.install_button = QPushButton(_("Install Dependencies (%s)") % self.project.install_command())
            self.install_button.clicked.connect(self._install)
            layout.addWidget(self.install_button)
        elif listing.static_only:
            warn = QLabel(_("Zenvi did not run this project's code, so it cannot read sizes, durations or props, or "
                            "render its compositions; its Zenvi timeline can still be restored."))
            warn.setWordWrap(True)
            warn.setObjectName("remotionNotRun")
            layout.addWidget(warn)

        licence = QLabel(_(LICENCE_NOTE))
        licence.setWordWrap(True)
        licence.setObjectName("remotionLicence")
        layout.addWidget(licence)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e5534b;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(self)
        self.ok_button = buttons.addButton(_("Import"), QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync()

    def mode(self) -> str:
        if self.open_radio is not None and self.open_radio.isChecked():
            return "open"
        if self.native_radio is not None and self.native_radio.isChecked():
            return "native"
        return "linked"

    def _sync(self) -> None:
        linked = self.mode() == "linked"
        can_render = not self.listing.static_only
        self.tree.setEnabled(linked)
        self.props_button.setEnabled(linked and can_render)
        self.codec_combo.setEnabled(linked and can_render)
        self.ok_button.setEnabled(not linked or can_render)

    def checked_ids(self) -> List[str]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                out.append(str(item.data(0, Qt.UserRole)))
        return out

    def _edit_props(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            self.error_label.setText(_tr("Select a composition to edit its props."))
            return
        comp = self.listing.by_id().get(str(item.data(0, Qt.UserRole)))
        if comp is None:
            return
        from windows.linked_clip_dialog import LinkedClipDialog
        current = dict(comp.default_props)
        current.update(self._props.get(comp.id) or {})
        source = comp.source
        link = {"kind": "remotion", "props": current, "render": {},
                "source": {"project_dir": self.project.root, "composition": comp.id,
                           "file": source.best_file if source else None, "line": source.best_line if source else None}}
        dialog = LinkedClipDialog(link, name=comp.id, can_render=True, parent=self)
        dialog.apply_button.setText(_tr("Use These Props"))
        dialog.state_label.setText(_tr("Not imported yet"))
        if dialog.exec_() and dialog.props() is not None:
            self._props[comp.id] = dict(dialog.props() or {})
            item.setCheckState(0, Qt.Checked)

    def _install(self) -> None:
        self._choice = ImportChoice(mode="install")
        self.accept()

    def _accept(self) -> None:
        mode = self.mode()
        if mode == "linked":
            ids = self.checked_ids()
            if not ids:
                self.error_label.setText(_tr("Tick at least one composition to import."))
                return
            props = {k: v for k, v in self._props.items() if k in ids}
            self._choice = ImportChoice(mode="linked", ids=ids, props=props,
                                        codec=str(self.codec_combo.currentData() or "auto"))
        else:
            self._choice = ImportChoice(mode=mode)
        self.accept()

    def choice(self) -> Optional[ImportChoice]:
        return self._choice


def import_remotion_project(window) -> None:
    """File > Import Project > Remotion Project...: pick the folder, then read it off the GUI thread."""
    folder = QFileDialog.getExistingDirectory(window, _tr("Import Remotion Project"), _start_dir(window))
    if folder:
        read_project(window, folder)


def ask_trust(window, project, *, install: bool = False) -> Optional[str]:
    """"run" when the user lets Zenvi run *project*'s code, "restore" to bring a Zenvi export back without
    running anything, None to stop."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(_tr("Run This Project's Code?"))
    box.setText((_tr(INSTALL_TRUST_TEXT) if install else _tr(TRUST_TEXT)) % project.name)
    box.setInformativeText(project.root)
    run_button = box.addButton(_tr("Install") if install else _tr("Run Its Code"), QMessageBox.AcceptRole)
    restore_button = None
    if project.is_zenvi_generated and not install:
        restore_button = box.addButton(_tr("Restore Without Running It"), QMessageBox.ActionRole)
    box.addButton(QMessageBox.Cancel)
    box.exec_()
    clicked = box.clickedButton()
    if clicked is run_button:
        return "run"
    if restore_button is not None and clicked is restore_button:
        return "restore"
    return None


def ask_trust_for(root: str, name: str, action: str) -> bool:
    """The trust question for a re-render or Open in Studio of a linked clip (``trust.require``; GUI thread)."""
    window = getattr(get_app(), "window", None)
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(_tr("Run This Project's Code?"))
    box.setText(_tr(ACTION_TRUST_TEXTS.get(action, TRUST_TEXT)) % name)
    box.setInformativeText(root)
    run_button = box.addButton(_tr("Run Its Code"), QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.exec_()
    return box.clickedButton() is run_button


def read_project(window, folder: str) -> None:
    """Look at *folder* without running it, ask whether to trust it, then read it with its own Remotion."""
    from classes.handoff import jobs
    from classes.handoff.remotion import detect, importer

    def look(job):  # the static scan: nothing of the project runs
        listing = importer.list_compositions(folder, static=True)
        return listing, trust.trust_key(listing.project.root)

    def looked(job):
        if job.error is not None:
            QMessageBox.warning(window, _tr("Import Remotion Project"), str(job.error))
            return
        listing, key = job.result
        project = listing.project
        if not project.entry:  # nothing can ever render
            if not listing.compositions and not project.is_zenvi_generated:
                try:
                    detect.require_ready(project)
                except Exception as exc:
                    QMessageBox.information(window, _tr("Import Remotion Project"), str(exc))
                    return
            _show_import_dialog(window, _not_installed(listing), key)
            return
        if not project.installed:  # nothing can run yet; Install asks first, then the compositions are read
            _show_import_dialog(window, _not_installed(listing), key)
            return
        # Installed: the compositions come from the project's own bundle -- also when the code registers them
        # dynamically (templates.map(t => <Composition id={t.id} ... />)) and the static scan found none.
        if not trust.is_trusted(key):
            answer = ask_trust(window, project)
            if answer is None:
                return
            if answer == "restore":
                _show_import_dialog(window, listing, key)
                return
            trust.remember(key)
        _read_compositions(window, folder, key)

    jobs.submit_job(look, label=_tr("Reading Remotion project"), kind="remotion", interactive=True, on_done=looked)


def _not_installed(listing):
    """The static listing with the "not installed" note the full listing would carry."""
    project = listing.project
    why = (f"run `{project.install_command()}` in {project.root} to read sizes, durations and props"
           if project.entry else "the project has no entry point")
    listing.warnings[:] = [f"read from the code only: {why}"]
    return listing


def _read_compositions(window, folder: str, key: str) -> None:
    from classes.handoff import jobs
    from classes.handoff.remotion import importer

    def work(job):
        return importer.list_compositions(folder, on_progress=job.report, should_cancel=job.should_cancel,
                                          allow_static=True)

    def on_done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _tr("Reading the Remotion project was cancelled"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _tr("Import Remotion Project"), str(job.error))
            return
        listing = job.result
        if not listing.compositions and not listing.project.is_zenvi_generated:
            QMessageBox.information(window, _tr("Import Remotion Project"),
                                    _tr("%s has no compositions.") % listing.project.name)
            return
        _show_import_dialog(window, listing, key)

    jobs.submit_job(work, label=_tr("Reading Remotion project"), kind="remotion", on_done=on_done)


def _show_import_dialog(window, listing, key: str) -> None:
    dialog = RemotionImportDialog(listing, parent=window)
    if not dialog.exec_():
        return
    choice = dialog.choice()
    if choice is not None:
        _start_import(window, listing, choice, key)


def _start_import(window, listing, choice: ImportChoice, key: str = "") -> None:
    from classes.handoff import jobs
    from classes.handoff.remotion import detect, importer
    project = listing.project
    if choice.mode == "install":
        if not (key and trust.is_trusted(key)):  # (the key comes from the job that looked at the project)
            if ask_trust(window, project, install=True) != "run":
                return
            if key:
                trust.remember(key)
        _install(window, project.root)
        return
    if choice.mode == "open":
        _open_restored(window, project)
        return
    if choice.mode == "native":
        kwargs: Dict[str, Any] = {"compositions": [detect.ZENVI_COMPOSITION], "restore_native": True}
        label = _tr("Restoring the Zenvi timeline")
    else:
        kwargs = {"compositions": choice.ids, "props": choice.props, "codec": choice.codec, "restore_native": False,
                  "listing": listing}
        label = _tr("Importing %d Remotion composition(s)") % len(choice.ids)

    def work(job):
        return importer.import_project(project.root, on_progress=job.report, should_cancel=job.should_cancel,
                                       **kwargs)

    def on_done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _tr("Remotion import cancelled; nothing changed"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _tr("Import Remotion Project"), _tr("The import failed: %s") % job.error)
            return
        receipt = job.result or {}
        count = len(receipt.get("linked") or [])
        native = receipt.get("native") or {}
        if native:
            edited = len(native.get("edits") or [])
            text = _tr("Restored %d clip(s) from %s") % (len(native.get("clips") or []), project.name)
            if edited:
                text += " " + _tr("(%d edit(s) made in the Remotion project)") % edited
            _notify(window, text)
        if count:
            _notify(window, _tr("Imported %d Remotion clip(s)") % count)
        if receipt.get("warnings"):
            QMessageBox.information(window, _tr("Import Remotion Project"),
                                    "\n".join("• %s" % w for w in receipt["warnings"][:8]))

    jobs.submit_job(work, label=label, kind="remotion", on_done=on_done)


def _install(window, folder: str) -> None:
    from classes.handoff import jobs
    from classes.handoff.remotion import install

    def work(job):
        return install.install_dependencies(folder, on_progress=job.report, should_cancel=job.should_cancel)

    def on_done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _tr("Installing dependencies was cancelled"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _tr("Install Dependencies"), str(job.error))
            return
        _notify(window, _tr("Installed Remotion %s") % (job.result or {}).get("remotion_version", ""))
        read_project(window, folder)

    jobs.submit_job(work, label=_tr("Installing Remotion dependencies"), kind="remotion", on_done=on_done)


def settle_unsaved(window) -> Optional[str]:
    """Before opening another project: "clean", "saved" (saved it), "discard" (the user said No), None (stop)."""
    app = get_app()
    try:
        needs_save = app.project.needs_save()
    except Exception:
        needs_save = False
    if not needs_save:
        return "clean"
    ret = QMessageBox.question(window, _tr("Unsaved Changes"), _tr("Save changes to project first?"),
                               QMessageBox.Cancel | QMessageBox.No | QMessageBox.Yes)
    if ret == QMessageBox.Yes:
        window.actionSave_trigger()
        try:
            still = app.project.needs_save()
        except Exception:
            still = False
        return None if still else "saved"  # the save was cancelled or failed: keep everything as it is
    return "discard" if ret == QMessageBox.No else None


def ask_restored_path(window, default: str) -> str:
    """The save dialog for the restored project: ``.zvn`` added by the dialog itself, so it asks about overwriting
    the real file name."""
    dialog = QFileDialog(window, _tr("Save the Restored Zenvi Project"))
    dialog.setAcceptMode(QFileDialog.AcceptSave)
    dialog.setFileMode(QFileDialog.AnyFile)
    dialog.setNameFilter(_tr("Zenvi Project (*.zvn)"))
    dialog.setDefaultSuffix("zvn")
    dialog.setDirectory(os.path.dirname(default))
    dialog.selectFile(os.path.basename(default))
    if not dialog.exec_():
        return ""
    files = dialog.selectedFiles()
    return str(files[0]) if files else ""


def _open_restored(window, project) -> None:
    """"Open as an editable Zenvi project": settle unsaved changes, pick a new file, write it off the GUI thread,
    open it. The open project's own file is never the target."""
    from classes.handoff import jobs
    from classes.handoff.remotion import detect, restore
    decision = settle_unsaved(window)
    if decision is None:
        return
    open_path = str(getattr(get_app().project, "current_filepath", "") or "")
    timeline_file = project.zenvi_timeline or restore.timeline_path(project.root)

    def plan(job):  # off the GUI thread: the project's name from the file's head, a name no file has yet
        name = detect.zenvi_source_project(project.root) or project.name
        return restore.unique_project_path(os.path.dirname(project.root), name)

    def planned(job):
        if job.error is not None:
            QMessageBox.warning(window, _tr("Import Remotion Project"), str(job.error))
            return
        path = ask_restored_path(window, str(job.result))
        if not path:
            return

        def write(write_job):
            write_job.report(None, _tr("Writing the Zenvi project"))
            return restore.write_project_file(restore.load_timeline(timeline_file), project.root, path, replace=True,
                                              open_project=open_path or None)

        def written(write_job):
            if write_job.error is not None:
                QMessageBox.warning(window, _tr("Import Remotion Project"), str(write_job.error))
                return
            restored, warnings, applied = write_job.result
            lines = []
            if applied:
                lines.append(_tr("Brought back %d edit(s) made in the Remotion project.") % len(applied))
            lines += ["• %s" % w for w in warnings[:8]]
            if lines:
                QMessageBox.information(window, _tr("Import Remotion Project"), "\n".join(lines))
            _open_written(window, restored, decision)

        jobs.submit_job(write, label=_tr("Restoring the Zenvi project"), kind="remotion", on_done=written)

    jobs.submit_job(plan, label=_tr("Restoring the Zenvi project"), kind="remotion", interactive=True,
                    on_done=planned)


def _open_written(window, path: str, decision: str) -> None:
    """Open the restored project; unsaved changes were settled before it was written (asked again only if the
    project changed since)."""
    app = get_app()
    try:
        dirty = app.project.needs_save()
    except Exception:
        dirty = False
    if dirty and decision != "discard":
        decision = settle_unsaved(window) or ""
        if not decision:
            _notify(window, _tr("The restored project is saved as %s") % path)
            return
        try:
            dirty = app.project.needs_save()
        except Exception:
            dirty = False
    if not dirty:
        window.OpenProjectSignal.emit(path)  # the normal open (missing-media prompts included)
        return
    try:  # the user chose not to save: open without asking a second time
        if not window.open_project(path, interactive=False):
            QMessageBox.warning(window, _tr("Import Remotion Project"), _tr("Could not open %s") % path)
    except Exception as exc:
        QMessageBox.warning(window, _tr("Import Remotion Project"), _tr("Could not open %s: %s") % (path, exc))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class RemotionExportDialog(QDialog):
    """Where to write the Remotion project, and whether to copy media / install."""

    def __init__(self, default_dir: str, parent=None):
        super().__init__(parent)
        _ = _tr
        self.setObjectName("RemotionExportDialog")
        self.setWindowTitle(_("Export to Remotion"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        intro = QLabel(_("Zenvi writes a Remotion project that plays this timeline (one composition, "
                         "ZenviTimeline) and comes back to Zenvi losslessly with File > Import Project."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(default_dir)
        self.folder_edit.setObjectName("remotionExportFolder")
        browse = QPushButton(_("Browse..."))
        browse.clicked.connect(self._browse)
        row.addWidget(QLabel(_("Folder:")))
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.copy_check = QCheckBox(_("Copy the media into the project (works on any computer)"))
        self.copy_check.setChecked(True)
        layout.addWidget(self.copy_check)
        self.install_check = QCheckBox(_("Install dependencies now (npm install; needs Node.js and internet)"))
        layout.addWidget(self.install_check)
        licence = QLabel(_("The project depends on Remotion from npm. Remotion is free for individuals and "
                           "companies of up to 3 people; larger companies need a company licence "
                           "(remotion.dev/license)."))
        licence.setWordWrap(True)
        layout.addWidget(licence)
        buttons = QDialogButtonBox(self)
        buttons.addButton(_("Export"), QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        current = self.folder_edit.text().strip()
        parent = os.path.dirname(current) if current else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, _tr("Choose Where to Create the Remotion Project"), parent)
        if chosen:
            name = os.path.basename(current) or "zenvi-remotion"
            self.folder_edit.setText(os.path.join(chosen, name))

    def values(self) -> dict:
        return {"output_dir": self.folder_edit.text().strip(), "copy_media": self.copy_check.isChecked(),
                "install": self.install_check.isChecked()}


def default_export_dir(project_path: Optional[str]) -> str:
    """``<project>-remotion`` next to the project (numbered when taken). Blocking (existence checks)."""
    name = os.path.splitext(os.path.basename(project_path))[0] if project_path else "Untitled"
    base = os.path.dirname(project_path) if project_path else os.path.expanduser("~")
    candidate = os.path.join(base, f"{name}-remotion")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(base, f"{name}-remotion-{n}")
        n += 1
    return candidate


def export_remotion_project(window) -> None:
    """File > Export Project > Remotion Project...: choose the folder, snapshot, export off the GUI thread."""
    from classes.handoff import jobs
    project_path = getattr(get_app().project, "current_filepath", None)

    def plan(job):
        return default_export_dir(project_path)

    def planned(job):
        default = str(job.result) if job.error is None and job.result else os.path.join(os.path.expanduser("~"),
                                                                                        "Untitled-remotion")
        dialog = RemotionExportDialog(default, parent=window)
        if not dialog.exec_():
            return
        values = dialog.values()
        if not values["output_dir"]:
            QMessageBox.warning(window, _tr("Export to Remotion"), _tr("Choose a folder for the Remotion project."))
            return
        from classes.handoff.remotion import exporter
        snapshot, data = exporter.snapshot_from_app()  # the only GUI-thread work: a copy of the project
        _run_export(window, snapshot, data, values, replace_edits=False)

    jobs.submit_job(plan, label=_tr("Exporting to Remotion"), kind="remotion", interactive=True, on_done=planned)


def _run_export(window, snapshot, data, values: dict, *, replace_edits: bool) -> None:
    from classes.handoff import jobs
    from classes.handoff.remotion import exporter

    def work(job):
        return exporter.export_project(snapshot, data, values["output_dir"], copy_media=values["copy_media"],
                                       install=values["install"], on_progress=job.report,
                                       should_cancel=job.should_cancel, replace_edits=replace_edits)

    def on_done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _tr("Export to Remotion cancelled; nothing in the folder changed"))
            return
        if isinstance(job.error, exporter.ExportHasEdits):
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle(_tr("Export to Remotion"))
            box.setText(_tr("%s is an earlier export with changes made in Remotion that exporting again would "
                            "replace:") % values["output_dir"])
            box.setInformativeText("\n".join("• %s" % e for e in job.error.edits[:8]) + "\n\n" + _tr(
                "Import the folder first (File > Import Project > Remotion Project...) to keep them."))
            replace = box.addButton(_tr("Replace Them"), QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec_()
            if box.clickedButton() is replace:
                _run_export(window, snapshot, data, values, replace_edits=True)
            return
        if job.error is not None:
            QMessageBox.warning(window, _tr("Export to Remotion"), str(job.error))
            return
        receipt = job.result or {}
        steps = "\n".join(receipt.get("next_steps") or [])
        text = _tr("Exported %(clips)d clip(s) to %(dir)s.\n\nNext:\n%(steps)s") % {
            "clips": receipt.get("clips", 0), "dir": receipt.get("output_dir", ""), "steps": steps}
        if receipt.get("warnings") or receipt.get("notes"):
            items = (receipt.get("warnings") or []) + (receipt.get("notes") or [])
            text += "\n\n" + _tr("Notes:") + "\n" + "\n".join("• %s" % w for w in items[:8])
        box = QMessageBox(window)
        box.setWindowTitle(_tr("Export to Remotion"))
        box.setText(text)
        open_button = box.addButton(_tr("Open Folder"), QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec_()
        if box.clickedButton() is open_button:
            from qt_api import QDesktopServices, QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(receipt.get("output_dir", "")))

    jobs.submit_job(work, label=_tr("Exporting to Remotion"), kind="remotion", on_done=on_done)


__all__ = ["import_remotion_project", "export_remotion_project", "read_project", "RemotionImportDialog",
           "RemotionExportDialog", "ImportChoice", "ask_trust", "ask_trust_for", "settle_unsaved", "ask_restored_path",
           "default_export_dir"]
