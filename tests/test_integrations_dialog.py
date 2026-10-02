"""Integrations dialog (#60): keychain and key-test work stays off the GUI thread.

Real Qt only (ZENVI_REAL_QT=1). The keychain and the backend are fakes; the
dialog, its worker threads and the queued signal back to the GUI are real.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from classes import provider_keys  # noqa: E402

KEY = "kid-123:secret-456"
SLOT = (provider_keys.KEYRING_SERVICE, "higgsfield")

_app = QApplication.instance() or QApplication([])


class _Keychain:
    def __init__(self):
        self.items = {}
        self.threads = set()
        self.fail_get = None

    def set_password(self, service, user, secret):
        self.threads.add(threading.current_thread().name)
        self.items[(service, user)] = secret

    def get_password(self, service, user):
        self.threads.add(threading.current_thread().name)
        if self.fail_get:
            raise self.fail_get
        return self.items.get((service, user))

    def delete_password(self, service, user):
        self.threads.add(threading.current_thread().name)
        del self.items[(service, user)]


class _Backend:
    def __init__(self):
        self.verdict = {"ok": False, "error": "Higgsfield API error 401: Invalid credentials"}
        self.gate = None
        self.threads = []

    def validate_provider_key(self, provider, key):
        self.threads.append(threading.current_thread().name)
        if self.gate is not None:
            self.gate.wait(5)
        return dict(self.verdict)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import windows.integrations as integ

    kc = _Keychain()
    backend = _Backend()
    monkeypatch.setattr(provider_keys, "_keyring", lambda: kc)
    monkeypatch.setattr(provider_keys, "_store_path", lambda: str(tmp_path / "provider_keys.json"))
    app = MagicMock()
    app._tr = lambda s: s
    monkeypatch.setattr(integ, "get_app", lambda: app)
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: backend)

    slot_threads = []
    original = integ.IntegrationsDialog._on_finished

    def on_finished(self, *args):
        slot_threads.append(threading.current_thread() is threading.main_thread())
        return original(self, *args)

    monkeypatch.setattr(integ.IntegrationsDialog, "_on_finished", on_finished)
    return integ, kc, backend, slot_threads


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_test_save_and_remove_run_off_the_gui_thread(env):
    integ, kc, backend, slot_threads = env
    dlg = integ.IntegrationsDialog()
    key_edit, status, (test_btn, remove_btn) = dlg._rows["higgsfield"]
    assert _wait(lambda: status.text().startswith("Not configured"))
    assert kc.threads == {"integrations-higgsfield"}  # the stored-key read on open

    # Rejected: busy while the worker runs, then the verdict; nothing stored.
    backend.gate = threading.Event()
    key_edit.setText("bogus:bogus")
    test_btn.click()
    assert status.text() == "Testing the key with Higgsfield..."
    assert not test_btn.isEnabled() and not remove_btn.isEnabled()
    backend.gate.set()
    assert _wait(test_btn.isEnabled)
    assert status.text() == "Higgsfield rejected the key: Higgsfield API error 401: Invalid credentials"
    assert key_edit.text() == "bogus:bogus"  # kept, so a typo can be fixed
    assert kc.items == {}
    assert backend.threads == ["integrations-higgsfield"]

    # Accepted: stored in the keychain, masked status, input cleared.
    backend.gate = None
    backend.verdict = {"ok": True, "error": None}
    key_edit.setText(KEY)
    test_btn.click()
    assert _wait(lambda: status.text().startswith("Connected"))
    assert status.text() == "Connected (••••-456)"
    assert key_edit.text() == ""
    assert kc.items == {SLOT: KEY}

    # Remove: back to Zenvi-managed, no restart.
    remove_btn.click()
    assert _wait(lambda: status.text().startswith("Not configured"))
    assert kc.items == {}
    assert remove_btn.isEnabled()

    assert kc.threads == {"integrations-higgsfield"}
    assert slot_threads and all(slot_threads)  # every result was applied on the GUI thread
    dlg.deleteLater()


def test_an_unreadable_stored_key_is_shown_as_such(env):
    integ, kc, _backend, _slots = env
    kc.items[SLOT] = KEY
    kc.fail_get = RuntimeError("keychain locked")
    dlg = integ.IntegrationsDialog()
    _key_edit, status, _buttons = dlg._rows["higgsfield"]
    assert _wait(lambda: "could not be read" in status.text())
    dlg.deleteLater()
