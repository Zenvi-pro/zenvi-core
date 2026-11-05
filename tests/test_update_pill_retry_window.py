"""The update pill and panel around an in-app retry, with the real widgets.

Review of #365: the window flipped the pill to "Downloading update…" before it
knew a download would start, so a declined retry could leave the pill there with
the panel's buttons disabled.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

# Real Qt and libopenshot (the main window): conftest keeps this file out of
# the stubbed suite; ci.yml runs it in the pytest-suite job.
pytest.importorskip("openshot")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QAction, QApplication, QMainWindow  # noqa: E402

if not hasattr(QApplication, "instance"):  # conftest's Qt stub, not real Qt
    pytest.skip("real Qt required (run with ZENVI_REAL_QT=1)", allow_module_level=True)

_APP = QApplication.instance() or QApplication([])


class _Updater:
    def __init__(self, starts):
        self.starts = starts
        self.calls = 0

    def retry(self):
        self.calls += 1
        return self.starts


@pytest.fixture
def ui(monkeypatch):
    if not hasattr(_APP, "get_settings"):
        _APP.get_settings = lambda: MagicMock(get=lambda key, default=None: default)
    if not hasattr(_APP, "_tr"):
        _APP._tr = lambda s: s
    if "windows.views.timeline" not in sys.modules:
        stub = types.ModuleType("windows.views.timeline")
        stub.TimelineView = type("TimelineView", (), {})
        sys.modules["windows.views.timeline"] = stub
    import windows.main_window as main_window
    from windows import update_panel
    from windows.update_status_button import UpdateStatusButton

    host = QMainWindow()
    host.actionUpdate = QAction("Update", host)
    button = UpdateStatusButton(host)
    panel = update_panel.UpdatePanel(host)
    opened = []
    monkeypatch.setattr(update_panel.webbrowser, "open", lambda url, new=0: opened.append(url))

    win = types.SimpleNamespace(update_status_button=button, _update_panel=panel,
                                _auto_updater=None)
    win._ensure_update_button = lambda: None
    win._sync_update_panel = lambda: panel.sync_from_button(button)
    for name in ("retry_auto_update", "updateFailed", "updateDownloadProgress"):
        setattr(win, name, types.MethodType(getattr(main_window.MainWindow, name), win))
    panel.window = win
    yield types.SimpleNamespace(win=win, button=button, panel=panel, opened=opened, host=host)
    panel.deleteLater()
    host.deleteLater()


def test_a_declined_retry_keeps_the_failed_pill_and_its_buttons(ui):
    ui.button.set_failed("9.9.9", "Connection reset")
    ui.win._auto_updater = _Updater(starts=False)
    ui.win.retry_auto_update()
    assert ui.win._auto_updater.calls == 1
    assert ui.button.state == "failed"
    ui.panel.sync_from_button(ui.button)
    assert ui.panel.primary.isEnabled()
    assert ui.panel.primary.text() == "Try again"


def test_try_again_downloads_in_app_then_a_failure_offers_both_ways_out(ui):
    ui.button.set_failed("9.9.9", "Connection reset")
    ui.panel.sync_from_button(ui.button)
    assert ui.panel.later.text() == "Open download page"
    ui.win._auto_updater = _Updater(starts=True)

    ui.panel.primary.click()                  # Try again
    assert ui.win._auto_updater.calls == 1
    assert ui.opened == []                    # the website is not the primary path
    assert ui.button.state == "downloading"

    # The check could not finish (e.g. GitHub unreachable): the updater sends a failure.
    ui.win.updateFailed("", "Couldn't reach GitHub to check for the update")
    assert ui.button.state == "failed"
    assert ui.button.version == "9.9.9"
    ui.panel.sync_from_button(ui.button)
    assert ui.panel.primary.isEnabled() and ui.panel.primary.text() == "Try again"
    assert "GitHub" in ui.panel.detail.text()

    ui.panel.later.click()                    # Open download page
    assert ui.opened == ["https://zenvi.pro/download"]


def test_download_update_from_the_available_pill_starts_in_app(ui):
    ui.button.set_available("9.9.9")
    ui.panel.sync_from_button(ui.button)
    assert ui.panel.primary.text() == "Download update"
    assert ui.panel.later.text() == "Later"
    ui.win._auto_updater = _Updater(starts=True)
    ui.panel.primary.click()
    assert ui.button.state == "downloading"
    ui.panel.later.click()                    # Later only dismisses
    assert ui.opened == []


def test_no_updater_leaves_the_pill_alone(ui):
    # Source runs have no AutoUpdater; nothing should pretend to download.
    ui.button.set_available("9.9.9")
    ui.win.retry_auto_update()
    assert ui.button.state == "available"
