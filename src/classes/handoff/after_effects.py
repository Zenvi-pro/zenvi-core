"""Zenvi -> After Effects in the File menu (C2 handoff package).

Importing this module (``classes.handoff.plugins.load_plugins``) registers:

* File > Export Project > **After Effects Script (.jsx)...**: a small dialog
  (folder, copy media into ``media/``, include audio), the export on the
  handoff executor with a progress dialog and Cancel, then a result dialog
  with the warnings, **Reveal** and **Run in After Effects now** (through
  Zenvi Link when After Effects is connected; on macOS through AppleScript
  when After Effects is installed but not connected; otherwise how to
  install Zenvi Link).
* File > Send To > **After Effects** (enabled while After Effects is
  connected): exports into the project's assets folder and builds the comp
  in After Effects, then reports After Effects' summary.

Handlers run on the GUI thread and only show dialogs; every file, network
and subprocess step runs in ``classes.handoff.jobs`` (see
``after_effects_export``). The export writes nothing into the Zenvi
project, so there is no undo step on the Zenvi side.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from classes.handoff import ui_registry
from classes.logger import log

ACTION_ID = "after_effects"
EXPORT_LABEL = "After Effects Script (.jsx)..."
SEND_LABEL = "After Effects"


def _tr(text: str) -> str:
    try:
        from classes.app import get_app
        return get_app()._tr(text)
    except Exception:
        return text


def _error_text(error: Optional[BaseException]) -> str:
    from classes.handoff.after_effects_export import AeHandoffError
    from classes.handoff.adobe_link import LinkHostError
    if isinstance(error, (AeHandoffError, LinkHostError)):
        return str(error)
    return _tr("Unexpected error: %s") % error


def _status(window, text: str) -> None:
    """A short result message in the toolbar pill (the themes hide the status bar)."""
    try:
        from windows.handoff_menus import notify
        notify(window, text)
    except Exception:
        log.debug("handoff notice unavailable", exc_info=True)


def _snapshot(window):
    """The open project as a TimelineSnapshot (GUI thread), or None after telling the user why not."""
    from classes.handoff.timeline_view import TimelineSnapshot
    snapshot = TimelineSnapshot.from_app()
    if not snapshot.clips:
        from qt_api import QMessageBox
        QMessageBox.information(window, _tr("Export to After Effects"),
                                _tr("The timeline has no clips to export."))
        return None
    return snapshot


class _Progress:
    """A window-modal progress dialog with Cancel, fed by a handoff Job."""

    def __init__(self, window, title: str):
        from qt_api import QProgressDialog, Qt
        self.job: Any = None
        self.dialog = QProgressDialog(_tr("Starting..."), _tr("Cancel"), 0, 1000, window)
        self.dialog.setWindowTitle(title)
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(0)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.setMinimumWidth(460)
        self.dialog.canceled.connect(self._cancel)
        self.dialog.show()

    def _cancel(self) -> None:
        if self.job is not None:
            self.job.cancel()
            self.dialog.setLabelText(_tr("Cancelling..."))

    def update(self, job) -> None:
        if job.message:
            self.dialog.setLabelText(_tr(job.message))
        if job.progress is None:
            self.dialog.setRange(0, 0)
        else:
            self.dialog.setRange(0, 1000)
            self.dialog.setValue(int(job.progress * 1000))

    def close(self) -> None:
        self.dialog.close()
        self.dialog.deleteLater()


def _run_job(window, title: str, work: Callable[[Any], Any], done: Callable[[Any], None]) -> None:
    """Run ``work(job)`` on the handoff executor with a progress dialog; ``done(job)`` on the GUI thread.

    The normal (registered) lane, not ``interactive``: an export can copy
    gigabytes and a send waits on After Effects, and only registered jobs are
    listed as running and cancelled by ``jobs.shutdown()`` when Zenvi quits.
    """
    from classes.handoff import jobs
    progress = _Progress(window, title)

    def on_done(job) -> None:
        progress.close()
        if job.state == jobs.CANCELLED:
            _status(window, _tr("%s cancelled") % title)
            return
        if job.state == jobs.FAILED:
            from qt_api import QMessageBox
            QMessageBox.warning(window, title, _error_text(job.error))
            return
        done(job)

    progress.job = jobs.submit_job(work, label=title, kind="after-effects", on_progress=progress.update,
                                   on_done=on_done)


def _cancel_hook(job):
    def should_cancel() -> bool:
        return job.should_cancel()
    return should_cancel


def _progress_hook(job):
    def report(fraction, message) -> None:
        job.report(fraction, _tr(message) if message else message)
    return report


def _ae_availability():
    """(connected through Zenvi Link, installed After Effects app on macOS). Blocking (probe <= 1.5 s)."""
    from classes.handoff import adobe_link
    from classes.handoff.after_effects_export import find_after_effects_app
    try:
        connected = adobe_link.get_host("aftereffects").connected
    except Exception:
        log.debug("After Effects host probe failed", exc_info=True)
        connected = False
    return connected, (None if connected else find_after_effects_app())


# ---------------------------------------------------------------------------
# File > Export Project > After Effects Script (.jsx)...
# ---------------------------------------------------------------------------

def export_dialog(window) -> None:
    """Menu handler: ask where to export, then export off the GUI thread."""
    from classes.handoff.after_effects_export import default_output_dir
    snapshot = _snapshot(window)
    if snapshot is None:
        return
    options = _ask_export_options(window, default_output_dir(snapshot.project_path, snapshot.name))
    if options is None:
        return
    folder, collect, include_audio = options
    title = _tr("Export to After Effects")

    def work(job):
        from classes.handoff.after_effects_export import export_after_effects
        result = export_after_effects(snapshot, folder, collect_media=collect, include_audio=include_audio,
                                      interactive=True, progress=_progress_hook(job),
                                      should_cancel=_cancel_hook(job))
        connected, app_path = _ae_availability()
        return result, connected, app_path

    def done(job):
        result, connected, app_path = job.result
        _status(window, _tr("Exported %s") % result.script_path)
        _show_result(window, result, connected, app_path)

    _run_job(window, title, work, done)


def _ask_export_options(window, default_folder: str):
    """(folder, collect media, include audio) from a small dialog, or None when cancelled."""
    from qt_api import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                        QPushButton, QVBoxLayout)
    dialog = QDialog(window)
    dialog.setWindowTitle(_tr("Export to After Effects"))
    dialog.setMinimumWidth(560)
    layout = QVBoxLayout(dialog)
    intro = QLabel(_tr("Zenvi writes an After Effects script (.jsx) that rebuilds this timeline as a composition: "
                       "footage, layers, timing, keyframes, titles, effects, transitions and markers."))
    intro.setWordWrap(True)
    layout.addWidget(intro)
    row = QHBoxLayout()
    row.addWidget(QLabel(_tr("Folder:")))
    folder_edit = QLineEdit(default_folder)
    row.addWidget(folder_edit, 1)
    browse = QPushButton(_tr("Browse..."))
    row.addWidget(browse)
    layout.addLayout(row)
    collect = QCheckBox(_tr("Copy media into a media/ folder next to the script (to move it to another computer)"))
    collect.setChecked(True)
    layout.addWidget(collect)
    audio = QCheckBox(_tr("Include audio"))
    audio.setChecked(True)
    layout.addWidget(audio)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText(_tr("Export"))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    def pick() -> None:
        start = folder_edit.text().strip() or default_folder
        chosen = QFileDialog.getExistingDirectory(dialog, _tr("Export to After Effects"),
                                                  os.path.dirname(start) or start)
        if chosen:
            folder_edit.setText(os.path.join(chosen, os.path.basename(start.rstrip("/\\")) or "AfterEffects"))

    browse.clicked.connect(pick)
    if dialog.exec_() != QDialog.Accepted:
        return None
    folder = folder_edit.text().strip() or default_folder
    return os.path.abspath(os.path.expanduser(folder)), collect.isChecked(), audio.isChecked()


def result_summary(result, _=_tr) -> str:
    """One paragraph about an export (pure; tested headlessly)."""
    stats = result.stats or {}
    native = sum(1 for t in result.titles if t.get("mode") == "native")
    text = _("Exported %(layers)s layer(s) and %(footage)s footage item(s) for After Effects.") % {
        "layers": stats.get("layers", 0), "footage": stats.get("footage", 0)}
    if result.titles:
        text += " " + _("%(total)s title(s): %(native)s as editable text, %(png)s as images.") % {
            "total": len(result.titles), "native": native, "png": len(result.titles) - native}
    if result.missing:
        text += " " + _("%s media file(s) are missing and become placeholders.") % len(result.missing)
    if result.warnings:
        text += " " + _("%s note(s) below.") % len(result.warnings)
    return text


def _show_result(window, result, connected: bool, app_path: Optional[str]) -> None:
    from qt_api import QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout
    from classes.handoff.after_effects_export import reveal
    dialog = QDialog(window)
    dialog.setWindowTitle(_tr("Exported to After Effects"))
    dialog.setMinimumWidth(560)
    layout = QVBoxLayout(dialog)
    summary = QLabel(result_summary(result))
    summary.setWordWrap(True)
    layout.addWidget(summary)
    path = QLineEdit(result.script_path)
    path.setReadOnly(True)
    layout.addWidget(path)
    details = []
    for t in result.titles:
        details.append(_tr("Title %(title)s: %(mode)s - %(detail)s") % {
            "title": t.get("title"), "mode": _tr("editable text") if t.get("mode") == "native" else _tr("image"),
            "detail": t.get("detail")})
    details += [_tr("Missing: %s") % m for m in result.missing]
    details += result.warnings
    if details:
        notes = QPlainTextEdit("\n".join(details))
        notes.setReadOnly(True)
        notes.setMinimumHeight(140)
        layout.addWidget(notes)
    row = QHBoxLayout()
    reveal_button = QPushButton(_tr("Reveal"))
    reveal_button.clicked.connect(lambda: _reveal(window, result.script_path, reveal))
    row.addWidget(reveal_button)
    if connected:
        run = QPushButton(_tr("Run in After Effects now"))
        run.clicked.connect(lambda: (dialog.accept(), _run_now(window, result.script_path, None)))
        row.addWidget(run)
    elif app_path:
        run = QPushButton(_tr("Run in After Effects now"))
        run.setToolTip(_tr("Zenvi Link is not connected, so Zenvi asks After Effects through AppleScript. "
                           "macOS asks once whether Zenvi may control After Effects."))
        run.clicked.connect(lambda: (dialog.accept(), _run_now(window, result.script_path, app_path)))
        row.addWidget(run)
    else:
        from classes.handoff.adobe_link import connect_hint
        hint = QLabel(_tr("To run it from Zenvi: %s") % connect_hint("aftereffects"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
    row.addStretch(1)
    close = QPushButton(_tr("Close"))
    close.setDefault(True)
    close.clicked.connect(dialog.accept)
    row.addWidget(close)
    layout.addLayout(row)
    dialog.exec_()


def _reveal(window, path: str, reveal) -> None:
    try:
        reveal(path)
    except Exception as exc:
        from qt_api import QMessageBox
        QMessageBox.warning(window, _tr("Reveal"), str(exc))


def _run_now(window, script_path: str, app_path: Optional[str]) -> None:
    """Run an exported script in After Effects (Zenvi Link, or AppleScript when *app_path* is given)."""
    title = _tr("Run in After Effects")

    def work(job):
        from classes.handoff.after_effects_export import run_in_after_effects, run_with_applescript
        job.report(None, _tr("Building the comp in After Effects"))
        if app_path:
            return run_with_applescript(script_path, app_path)
        summary, _receipt = run_in_after_effects(script_path)
        return summary

    def done(job):
        _show_ae_summary(window, title, job.result)

    _run_job(window, title, work, done)


def _show_ae_summary(window, title: str, summary: Optional[dict]) -> None:
    from qt_api import QMessageBox
    box = QMessageBox(window)
    box.setWindowTitle(title)
    if summary:
        box.setText(str(summary.get("summary") or _tr("After Effects built the comp.")))
        warnings = summary.get("warnings") or []
        if warnings:
            box.setDetailedText("\n".join(str(w) for w in warnings))
    else:
        box.setText(_tr("After Effects ran the script. Check the comp in After Effects."))
    box.exec_()


# ---------------------------------------------------------------------------
# File > Send To > After Effects
# ---------------------------------------------------------------------------

def send(window) -> None:
    """Menu handler: export into the project's assets folder and build the comp in the connected After Effects."""
    snapshot = _snapshot(window)
    if snapshot is None:
        return
    title = _tr("Send to After Effects")

    def work(job):
        from classes.handoff import adobe_link
        from classes.handoff.after_effects_export import find_after_effects_app, send_to_after_effects
        try:
            return "sent", send_to_after_effects(snapshot, progress=_progress_hook(job),
                                                 should_cancel=_cancel_hook(job))
        except adobe_link.HostNotConnected as exc:
            return "not_connected", (str(exc), find_after_effects_app())

    def done(job):
        kind, value = job.result
        if kind == "sent":
            _status(window, value.message)
            _show_ae_summary(window, title, value.summary or {"summary": value.message})
            return
        message, app_path = value
        _offer_fallback(window, snapshot, message, app_path)

    _run_job(window, title, work, done)


def _offer_fallback(window, snapshot, message: str, app_path: Optional[str]) -> None:
    from qt_api import QMessageBox
    title = _tr("Send to After Effects")
    if not app_path:
        QMessageBox.information(window, title, message)
        return
    answer = QMessageBox.question(
        window, title,
        message + "\n\n" + _tr("After Effects is installed on this Mac. Build the comp through AppleScript instead? "
                               "macOS asks once whether Zenvi may control After Effects."))
    if answer != QMessageBox.Yes:
        return

    def work(job):
        from classes.handoff.after_effects_export import export_after_effects, run_with_applescript, send_folder
        result = export_after_effects(snapshot, send_folder(snapshot.project_path, snapshot.name),
                                      collect_media=False, interactive=False, progress=_progress_hook(job),
                                      should_cancel=_cancel_hook(job))
        job.report(None, _tr("Building the comp in After Effects"))
        return run_with_applescript(result.script_path, app_path)

    def done(job):
        _show_ae_summary(window, title, job.result)

    _run_job(window, title, work, done)


ui_registry.register_export_action(ACTION_ID, EXPORT_LABEL, export_dialog, order=10,
                                   tooltip="Write an After Effects script that rebuilds this timeline as a comp")
ui_registry.register_send_action(ACTION_ID, SEND_LABEL, "aftereffects", send, order=10,
                                 tooltip="Build this timeline as a comp in the After Effects that runs Zenvi Link")

__all__ = ["export_dialog", "send", "result_summary", "ACTION_ID", "EXPORT_LABEL", "SEND_LABEL"]
