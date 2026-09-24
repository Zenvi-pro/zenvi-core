"""Integrations dialog: bring your own provider keys (Higgsfield first)."""

import functools

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from classes import provider_keys
from classes.app import get_app


class IntegrationsDialog(QDialog):
    """One row per provider manifest entry: masked key, Test & Save, Remove, status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _ = get_app()._tr
        self.setWindowTitle(_("Integrations"))
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        intro = QLabel(_(
            "Use your own provider keys. Generation with your key is billed to your "
            "provider account instead of Zenvi credits. Remove the key to go back to "
            "Zenvi-managed generation."
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
            self._rows[pid] = (key_edit, status)
            self._show_stored(pid)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_stored(self, pid):
        _ = get_app()._tr
        key_edit, status = self._rows[pid]
        stored = provider_keys.get_key(pid)
        key_edit.clear()
        if stored:
            status.setText(_("Connected (%s)") % provider_keys.mask(stored))
        else:
            status.setText(_("Not configured. Zenvi-managed generation (credits) is used."))

    def _test_and_save(self, pid):
        from classes.api_client import get_backend_client

        key_edit, status = self._rows[pid]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, message = provider_keys.test_and_save(
                pid, key_edit.text(), get_backend_client().validate_provider_key,
            )
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            key_edit.clear()
        status.setText(message)

    def _remove(self, pid):
        provider_keys.clear_key(pid)
        self._show_stored(pid)
