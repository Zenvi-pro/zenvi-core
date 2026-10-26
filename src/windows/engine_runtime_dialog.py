"""Zenvi Engine Tools dialog: install or update the engine bridge runtime (classes/engine_runtime.py).

Opened from AI Tools > Zenvi Engine Tools... and from Preferences > AI. The
status check and the install run on a QThread of this dialog (both read the
disk, start Node and download): the GUI thread only shows their progress.
The dialog cannot be closed while an install runs; Cancel stops it and leaves
what was installed before.
"""

from __future__ import annotations

import threading
from typing import Optional

from qt_api import Qt, QThread, pyqtSignal
from qt_api import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from classes.app import get_app
from classes.logger import log


class _RuntimeWorker(QThread):
    """engine_runtime.status() or engine_runtime.install() off the GUI thread."""

    progressed = pyqtSignal(str, object, object)    # what, bytes done, bytes total (0: unknown)
    finished_ok = pyqtSignal(dict)
    finished_err = pyqtSignal(str, bool)            # message, cancelled

    def __init__(self, install: bool, parent=None):
        super().__init__(parent)
        self.install = install
        self.cancel = threading.Event()

    def run(self):
        from classes import engine_runtime
        try:
            if self.install:
                status = engine_runtime.install(progress=self.progressed.emit, cancel=self.cancel)
            else:
                status = engine_runtime.status()
            self.finished_ok.emit(status)
        except engine_runtime.InstallCancelled as exc:
            self.finished_err.emit(str(exc), True)
        except engine_runtime.RuntimeInstallError as exc:
            self.finished_err.emit(str(exc), False)
        except Exception as exc:
            log.error("Zenvi engine tools: install failed", exc_info=True)
            self.finished_err.emit(f"Unexpected error: {exc}", False)


class EngineRuntimeDialog(QDialog):
    """Shows whether the engine runtime is installed; installs or updates it with progress and cancel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _ = get_app()._tr
        self.setWindowTitle(_("Zenvi Engine Tools"))
        self.setMinimumWidth(520)
        self._worker: Optional[_RuntimeWorker] = None
        self._status: dict = {}

        root = QVBoxLayout(self)
        intro = QLabel(_("Zenvi engine tools (motion blocks, 3D scenes, FX, new edits) run in the Zenvi engine: "
                         "Node.js and the zenvi CLI, downloaded once into Zenvi's settings folder. "
                         "Nothing outside that folder is changed."))
        intro.setWordWrap(True)
        root.addWidget(intro)
        self._label = QLabel(_("Checking what is installed..."))
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self._label)
        self._progress_text = QLabel("")
        self._progress = QProgressBar()
        self._progress_text.hide()
        self._progress.hide()
        root.addWidget(self._progress_text)
        root.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._install_btn = QPushButton(_("Download and Install"))
        self._install_btn.setEnabled(False)
        self._install_btn.clicked.connect(self.start_install)
        self._cancel_btn = QPushButton(_("Cancel"))
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self.cancel_install)
        self._close_btn = QPushButton(_("Close"))
        self._close_btn.clicked.connect(self.reject)
        for button in (self._install_btn, self._cancel_btn, self._close_btn):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self._run(install=False)

    # -- worker ---------------------------------------------------------------
    def _run(self, install: bool) -> None:
        worker = _RuntimeWorker(install, self)
        worker.progressed.connect(self._on_progress)
        worker.finished_ok.connect(self._on_status if not install else self._on_installed)
        worker.finished_err.connect(self._on_error)
        worker.finished.connect(self._on_worker_done)
        self._worker = worker
        worker.start()

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._cancel_btn.hide()
        self._close_btn.setEnabled(True)

    # -- status ---------------------------------------------------------------
    def _on_status(self, status: dict) -> None:
        from classes import engine_bridge, engine_runtime
        _ = get_app()._tr
        self._status = status
        source = status.get("source") or ""
        if status.get("ready"):
            where = _("installed") if status.get("managed") else _("from %s") % status.get("cli")
            text = _("Ready: zenvi CLI %(version)s (%(where)s, bridge protocol %(protocol)s) on Node.js "
                     "%(node_version)s at %(node)s.") % {
                "version": status.get("cli_version") or "", "where": where, "protocol": status.get("protocol"),
                "node_version": status.get("node_version") or "", "node": status.get("node")}
            self._install_btn.setText(_("Update"))
        else:
            parts = []
            if status.get("node_needed"):
                parts.append(_("Node.js %(version)s (about %(mb)d MB, from nodejs.org)") % {
                    "version": engine_runtime.NODE_VERSION, "mb": engine_runtime.NODE_DOWNLOAD_MB})
            parts.append(_("the zenvi CLI from %s") % (source or _("(no source set)")))
            text = _("Not installed. Installing downloads %(what)s into %(home)s.") % {
                "what": _(" and ").join(parts), "home": status.get("home")}
            self._install_btn.setText(_("Download and Install"))
        if not source:
            text += "\n\n" + _("Set Preferences > AI > Zenvi Engine Tools Source to a release manifest URL or to "
                               "a zenvi CLI build folder (zenvi-web packages/cli/dist) to install or update.")
        if engine_bridge.bridge_setting() == "off":
            text += "\n\n" + _("Engine tools are turned off (Preferences > AI > Zenvi Engine Tools): installed "
                               "or not, none is listed until that is turned back on.")
        self._label.setText(text)
        self._install_btn.setEnabled(bool(source))

    # -- install --------------------------------------------------------------
    def start_install(self) -> None:
        if self._busy():
            return
        _ = get_app()._tr
        self._install_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.show()
        self._progress_text.setText(_("Starting..."))
        self._progress_text.show()
        self._progress.setRange(0, 0)
        self._progress.show()
        self._run(install=True)

    def cancel_install(self) -> None:
        if self._busy():
            self._worker.cancel.set()
            self._cancel_btn.setEnabled(False)
            self._progress_text.setText(get_app()._tr("Cancelling..."))

    def _on_progress(self, text: str, done, total) -> None:
        if total:
            self._progress.setRange(0, 1000)
            self._progress.setValue(int(1000 * min(done, total) / total))
            text = "%s: %.1f of %.1f MB" % (text, done / 1e6, total / 1e6)
        elif done:
            text = "%s: %.1f MB" % (text, done / 1e6)
        else:
            self._progress.setRange(0, 0)
        self._progress_text.setText(text)

    def _on_installed(self, status: dict) -> None:
        from classes import engine_bridge, tool_handlers
        _ = get_app()._tr
        self._progress.hide()
        if engine_bridge.bridge_setting() != "off":
            # List the engine tools now, on a worker thread; the Assistant and MCP see them as they arrive.
            tool_handlers.refresh_bridged_tools()
            self._progress_text.setText(_("Installed. Engine tools are now available to the Assistant."))
        else:
            self._progress_text.setText(_("Installed (engine tools stay off until Preferences turns them on)."))
        self._on_status(status)

    def _on_error(self, message: str, cancelled: bool) -> None:
        self._progress.hide()
        self._progress_text.setText(message)
        self._progress_text.show()
        self._install_btn.setEnabled(bool(self._status.get("source")) or not self._status)

    # -- closing --------------------------------------------------------------
    def reject(self) -> None:
        # The worker belongs to this dialog: it must finish (or be cancelled) before the dialog goes.
        if self._busy():
            if self._worker.install:
                self.cancel_install()
                return
            self._worker.wait()          # the status check: file checks and one ``node --version``
        super().reject()

    def closeEvent(self, event) -> None:
        if self._busy():
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)


def show_engine_runtime_dialog(parent=None) -> None:
    EngineRuntimeDialog(parent or get_app().window).exec_()
