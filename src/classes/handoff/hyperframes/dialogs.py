"""Qt side of File > Import Project / Export Project > HyperFrames Project... (GUI thread only).

The handlers ask for a folder and the choices with dialogs, then hand the
work to ``handoff.jobs`` (the toolbar pill shows progress with Cancel):
reading the project (``hyperframes timeline --json``), rendering its
compositions, exporting. Results come back on the GUI thread as a short
message (``handoff_menus.notify``) or a message box with what to do next.
One import = one undo step; a failed or cancelled one changes nothing.
"""

from __future__ import annotations

import os
from typing import List, Optional

from qt_api import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel, QMessageBox,
    QPlainTextEdit, QRadioButton, QVBoxLayout,
)

from classes.app import get_app
from classes.logger import log

TRUST_NOTE = ("Linked parts render with the HyperFrames CLI (Node.js 22 or newer), which runs the project's own "
              "HTML and scripts in a headless browser, like `npx hyperframes render`: import projects you trust.")


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


def _start_dir() -> str:
    try:
        path = getattr(get_app().project, "current_filepath", None)
        if path:
            return os.path.dirname(path)
    except Exception:
        log.debug("no project folder", exc_info=True)
    return os.path.expanduser("~")


def _project_settings():
    p = get_app().project
    fps = p.get("fps") or {"num": 30, "den": 1}
    try:
        rate = float(fps.get("num") or 30) / float(fps.get("den") or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        rate = 30.0
    return rate, (int(p.get("width") or 1920), int(p.get("height") or 1080))


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return "%d:%05.2f" % (int(value // 60), value % 60)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class HyperFramesImportDialog(QDialog):
    """What a HyperFrames project brings in, and how (auto / native / flatten)."""

    def __init__(self, inspection, parent=None):
        super().__init__(parent)
        _ = _tr
        self.inspection = inspection
        s = inspection.summary()
        self.setObjectName("HyperFramesImportDialog")
        self.setWindowTitle(_("Import HyperFrames Project"))
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(_("Project:"), QLabel(inspection.project_dir))
        form.addRow(_("Composition:"), QLabel("%s — %dx%d, %s" % (s["composition"], s["width"], s["height"],
                                                                   _fmt_seconds(s["duration"]))))
        timing = _("HyperFrames CLI %s") % (s["hyperframes_cli"] or "") if s["timing_from"] == "hyperframes" \
            else _("read by Zenvi (HyperFrames CLI not available)")
        form.addRow(_("Timing:"), QLabel(timing))
        if s["zenvi_export"]:
            form.addRow(_("Zenvi export:"), QLabel(_("%d clip(s) restore natively") % s["restored_clips"]))
        form.addRow(_("Media clips:"), QLabel(str(s["media_clips"])))
        form.addRow(_("Compositions / graphics:"), QLabel(_("%d composition(s), %d graphics layer(s)") % (
            s["compositions"], s["layers"])))
        layout.addLayout(form)

        self.group = QButtonGroup(self)
        self.radios = {}
        labels = (
            ("auto", _("Automatic (recommended): %s") % inspection.reason),
            ("native", _("Editable clips: media as native clips, compositions and graphics as linked clips")),
            ("flatten", _("One linked clip of the whole project (exactly as HyperFrames renders it)")),
        )
        for i, (mode, text) in enumerate(labels):
            radio = QRadioButton(text)
            radio.setObjectName("hyperframesMode_" + mode)
            self.group.addButton(radio, i)
            self.radios[mode] = radio
            layout.addWidget(radio)
        self.radios[inspection.requested if inspection.requested in self.radios else "auto"].setChecked(True)

        notes: List[str] = []
        for label, probs in (inspection.problems or {}).items():
            notes.append("%s: %s" % (label, "; ".join(probs[:3])))
        notes.extend(inspection.warnings or [])
        if notes:
            box = QPlainTextEdit("\n".join("• " + n for n in notes[:40]))
            box.setObjectName("hyperframesImportNotes")
            box.setReadOnly(True)
            box.setMaximumHeight(140)
            layout.addWidget(QLabel(_("Notes:")))
            layout.addWidget(box)
        trust = QLabel(_(TRUST_NOTE))
        trust.setWordWrap(True)
        layout.addWidget(trust)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("Import"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mode(self) -> str:
        for mode, radio in self.radios.items():
            if radio.isChecked():
                return mode
        return "auto"


def import_hyperframes_project(window) -> None:
    """File > Import Project > HyperFrames Project...: folder, summary + mode, then render and add (a job)."""
    from classes.handoff import jobs
    _ = _tr
    folder = QFileDialog.getExistingDirectory(window, _("Choose a HyperFrames project folder"), _start_dir())
    if not folder:
        return
    fps, canvas = _project_settings()

    def read(job):
        from classes.handoff.hyperframes import importer
        return importer.inspect_project(folder, "auto", fps=fps, canvas=canvas, should_cancel=job.should_cancel)

    def read_done(job):
        if job.state == jobs.CANCELLED:
            return
        if job.error is not None:
            QMessageBox.warning(window, _("Import HyperFrames Project"),
                                _("Could not read %s:\n%s") % (folder, job.error))
            return
        dialog = HyperFramesImportDialog(job.result, window)
        if dialog.exec_() != QDialog.Accepted:
            return
        _run_import(window, folder, dialog.mode(), fps, canvas)

    jobs.submit_job(read, label=_("Reading HyperFrames project"), kind="hyperframes", on_done=read_done)


def _run_import(window, folder: str, mode: str, fps: float, canvas) -> None:
    from classes.handoff import jobs
    _ = _tr

    def work(job):
        from classes.handoff.hyperframes import importer
        insp = importer.inspect_project(folder, mode, fps=fps, canvas=canvas, should_cancel=job.should_cancel)
        return importer.run_import(insp, on_progress=job.report, should_cancel=job.should_cancel)

    def done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _("HyperFrames import cancelled; nothing changed"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _("Import HyperFrames Project"),
                                _("The import failed and nothing was changed:\n%s") % job.error)
            return
        r = job.result or {}
        text = _("Imported %(name)s: %(native)d native, %(linked)d linked, %(restored)d restored clip(s)") % {
            "name": os.path.basename(folder), "native": r.get("native", 0), "linked": r.get("linked", 0),
            "restored": r.get("restored", 0)}
        _notify(window, text)
        warnings = r.get("warnings") or []
        if warnings:
            QMessageBox.information(window, _("Import HyperFrames Project"), text + "\n\n" + "\n".join(
                "• " + w for w in warnings[:10]))
        status = getattr(window, "handoff_status", None)
        if status is not None and hasattr(status, "check_soon"):
            status.check_soon(force=True)

    jobs.submit_job(work, label=_("Importing HyperFrames project"), kind="hyperframes", on_done=done)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class HyperFramesExportDialog(QDialog):
    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        _ = _tr
        self.setObjectName("HyperFramesExportDialog")
        self.setWindowTitle(_("Export HyperFrames Project"))
        layout = QVBoxLayout(self)
        label = QLabel(_("Write the timeline as a HyperFrames project in:\n%s") % folder)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.copy_media = QCheckBox(_("Copy the media into the project (recommended; uncheck to link to the "
                                      "originals and save disk)"))
        self.copy_media.setObjectName("hyperframesCopyMedia")
        self.copy_media.setChecked(True)
        layout.addWidget(self.copy_media)
        note = QLabel(_("The Zenvi timeline is embedded in index.html, so importing the folder back restores your "
                        "clips. GSAP loads from its CDN when the project plays."))
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("Export"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def export_hyperframes_project(window) -> None:
    """File > Export Project > HyperFrames Project...: folder + options, snapshot here, export in a job."""
    from classes.handoff import jobs
    from classes.handoff.hyperframes import exporter
    from classes.handoff.timeline_view import TimelineSnapshot
    _ = _tr
    if not (get_app().project.get("clips") or []):
        QMessageBox.information(window, _("Export HyperFrames Project"), _("The timeline has no clips to export."))
        return
    folder = QFileDialog.getExistingDirectory(window, _("Choose a new or empty folder for the HyperFrames project"),
                                              _start_dir())
    if not folder:
        return
    dialog = HyperFramesExportDialog(folder, window)
    if dialog.exec_() != QDialog.Accepted:
        return
    copy_media = dialog.copy_media.isChecked()
    snapshot = TimelineSnapshot.from_app()
    raw = exporter.raw_project(get_app().project._data)

    def work(job):
        from classes.handoff.hyperframes import cli as hf_cli
        result = exporter.export_project(snapshot, raw, folder, copy_media=copy_media, on_progress=job.report,
                                         should_cancel=job.should_cancel)
        try:
            lint = hf_cli.lint(result.output_dir, timeout=60.0, should_cancel=job.should_cancel)
        except (hf_cli.CliError, OSError) as exc:
            lint = {"ok": None, "skipped": str(exc)}
        return result, lint

    def done(job):
        if job.state == jobs.CANCELLED:
            _notify(window, _("HyperFrames export cancelled"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _("Export HyperFrames Project"), _("The export failed:\n%s") % job.error)
            return
        result, lint = job.result
        lines = [_("Exported %d clip(s) to %s.") % (result.clips, result.output_dir)]
        if lint.get("ok") is True:
            lines.append(_("HyperFrames lint: clean."))
        elif lint.get("ok") is False:
            lines.append(_("HyperFrames lint: %s error(s), %s warning(s).") % (lint.get("errorCount"),
                                                                              lint.get("warningCount")))
        if result.warnings:
            lines.append("")
            lines.extend("• " + w for w in result.warnings[:10])
        box = QMessageBox(window)
        box.setWindowTitle(_("Export HyperFrames Project"))
        box.setText("\n".join(lines))
        open_folder = box.addButton(_("Open Folder"), QMessageBox.ActionRole)
        studio = box.addButton(_("Open in HyperFrames Studio"), QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec_()
        if box.clickedButton() is open_folder:
            from qt_api import QDesktopServices, QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(result.output_dir))
        elif box.clickedButton() is studio:
            open_studio(window, result.output_dir)

    jobs.submit_job(work, label=_("Exporting to HyperFrames"), kind="hyperframes", on_done=done)


def open_studio(window, project_dir: str) -> None:
    """Start (or reuse) the HyperFrames Studio for *project_dir* in a job and open it in the browser."""
    from classes.handoff import jobs
    _ = _tr

    def work(job):
        from classes.handoff.hyperframes import cli as hf_cli
        return hf_cli.open_studio(project_dir)

    def done(job):
        if job.error is not None:
            QMessageBox.warning(window, _("HyperFrames Studio"), _("The Studio did not start:\n%s") % job.error)
        elif job.state != jobs.CANCELLED:
            _notify(window, _("HyperFrames Studio: %s") % job.result)

    jobs.submit_job(work, label=_("Starting HyperFrames Studio"), kind="hyperframes", on_done=done)


__all__ = ["HyperFramesImportDialog", "HyperFramesExportDialog", "import_hyperframes_project",
           "export_hyperframes_project", "open_studio"]
