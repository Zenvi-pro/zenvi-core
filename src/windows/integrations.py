"""Integrations dialog: bring your own provider keys (Higgsfield first)."""

import functools
import threading

from qt_api import (
    Qt, pyqtSignal, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from classes import provider_keys
from classes.app import get_app
from classes.logger import log


class IntegrationsDialog(QDialog):
    """One row per provider manifest entry: masked key, Test & Save, Remove, status.

    Every keychain read/write and the key test (a backend round trip to the
    provider) runs on a worker thread named ``integrations-<provider>``; the
    result comes back to the GUI thread through the queued ``_finished`` signal.
    """

    # provider id, ok, status message, keep what the user typed
    _finished = pyqtSignal(str, bool, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        _ = get_app()._tr
        self.setWindowTitle(_("Integrations"))
        self.setMinimumWidth(560)
        self._connected = _("Connected (%s)")
        self._not_configured = _("Not configured. Zenvi-managed generation (credits) is used.")
        self._unreadable = _("Your stored key could not be read. Re-enter it, or Remove it.")
        self._checking = _("Checking the stored key...")
        self._testing = _("Testing the key with %s...")
        self._removing = _("Removing the key...")
        self._finished.connect(self._on_finished)

        layout = QVBoxLayout(self)
        intro = QLabel(_(
            "Use your own provider keys. Text-to-video and transitions made with your key "
            "are billed to your provider account instead of Zenvi credits (video-to-video "
            "edits still use Zenvi credits). Remove the key to go back to Zenvi-managed generation."
        ))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        self._rows = {}
        for row, (pid, meta) in enumerate(provider_keys.PROVIDERS.items()):
            key_edit = QLineEdit()
            key_edit.setEchoMode(QLineEdit.Password)
            key_edit.setPlaceholderText(meta["key_hint"])
            status = QLabel()
            status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            test_btn = QPushButton(_("Test && Save"))
            remove_btn = QPushButton(_("Remove"))
            test_btn.clicked.connect(functools.partial(self._test_and_save, pid))
            remove_btn.clicked.connect(functools.partial(self._remove, pid))
            grid.addWidget(QLabel(meta["name"]), row * 2, 0)
            grid.addWidget(key_edit, row * 2, 1)
            grid.addWidget(test_btn, row * 2, 2)
            grid.addWidget(remove_btn, row * 2, 3)
            grid.addWidget(status, row * 2 + 1, 1, 1, 3)
            self._rows[pid] = (key_edit, status, (test_btn, remove_btn))
            self._show_stored(pid)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _run(self, pid, busy_text, work):
        """Run ``work() -> (ok, message, keep_input)`` off the GUI thread for ``pid``'s row."""
        _key_edit, status, buttons = self._rows[pid]
        for btn in buttons:
            btn.setEnabled(False)
        status.setText(busy_text)

        def run():
            try:
                ok, message, keep_input = work()
            except Exception as exc:
                log.warning("Integrations: %s key action failed: %s", pid, type(exc).__name__)
                ok, message, keep_input = False, f"Key store error ({type(exc).__name__}).", True
            try:
                self._finished.emit(pid, ok, message, keep_input)
            except RuntimeError:
                pass  # the dialog was destroyed first

        threading.Thread(target=run, daemon=True, name=f"integrations-{pid}").start()

    def _on_finished(self, pid, ok, message, keep_input):
        key_edit, status, buttons = self._rows[pid]
        if not keep_input:
            key_edit.clear()
        status.setText(message)
        for btn in buttons:
            btn.setEnabled(True)

    def _stored_status(self, pid):
        """Worker side: the row's status for whatever key is stored now."""
        try:
            stored = provider_keys.get_key(pid, strict=True)
        except provider_keys.KeyUnreadable:
            return False, self._unreadable, False
        if stored:
            return True, self._connected % provider_keys.mask(stored), False
        return False, self._not_configured, False

    def _show_stored(self, pid):
        self._run(pid, self._checking, functools.partial(self._stored_status, pid))

    def _test_and_save(self, pid):
        key_edit, _status, _buttons = self._rows[pid]
        key = key_edit.text()

        def work():
            from classes.api_client import get_backend_client
            ok, message = provider_keys.test_and_save(
                pid, key, get_backend_client().validate_provider_key,
            )
            return ok, message, not ok

        self._run(pid, self._testing % provider_keys.PROVIDERS[pid]["name"], work)

    def _remove(self, pid):
        def work():
            provider_keys.clear_key(pid)
            return self._stored_status(pid)

        self._run(pid, self._removing, work)
