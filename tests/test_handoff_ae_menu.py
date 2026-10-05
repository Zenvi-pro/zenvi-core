"""File menu entries of the After Effects handoff (classes.handoff.after_effects), headless.

The dialogs and the job runner are replaced so the handlers' flow (snapshot
on the GUI thread, export off it, then the result / fallback dialogs) runs
without Qt. Registrations are removed after each test, so the shared
ui_registry looks the same to the other handoff tests.
"""

import importlib
import types

import pytest

from ae_project import ProjectBuilder
from classes.handoff import ui_registry


@pytest.fixture
def menu():
    import classes.handoff.after_effects as module
    module = importlib.reload(module)  # (re-)register for this test
    yield module
    ui_registry.unregister_action(module.ACTION_ID)


def test_export_and_send_entries_are_registered(menu):
    export = [a for a in ui_registry.export_actions() if a.id == "after_effects"]
    send = [a for a in ui_registry.send_actions() if a.id == "after_effects"]
    assert len(export) == 1 and export[0].label == "After Effects Script (.jsx)..." and export[0].order == 10
    assert len(send) == 1 and send[0].label == "After Effects" and send[0].host_app == "aftereffects"
    assert export[0].handler is menu.export_dialog and send[0].handler is menu.send


def test_the_plugin_loader_finds_the_package(monkeypatch, menu):
    from classes.handoff import plugins
    monkeypatch.setattr(plugins, "_done", False)
    monkeypatch.setattr(plugins, "_loaded", [])
    assert "classes.handoff.after_effects" in plugins.load_plugins()


class _Job:
    def __init__(self):
        self.reports = []
        self.result = None

    def report(self, progress=None, message=None):
        self.reports.append((progress, message))

    def should_cancel(self):
        return False


def _sync_runner(window, title, work, done):
    job = _Job()
    job.result = work(job)
    done(job)


def _snapshot(tmp_path):
    b = ProjectBuilder()
    path = tmp_path / "beach.mp4"
    path.write_bytes(b"beach")
    v = b.add_file("video", path=str(path))
    b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0)
    return b.snapshot(str(tmp_path / "Trip.zvn"))


def test_export_dialog_exports_off_the_gui_thread_then_shows_the_result(menu, monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(menu, "_snapshot", lambda window: _snapshot(tmp_path))
    monkeypatch.setattr(menu, "_ask_export_options", lambda window, default: (str(tmp_path / "out"), False, True))
    monkeypatch.setattr(menu, "_run_job", _sync_runner)
    monkeypatch.setattr(menu, "_ae_availability", lambda: (False, "/Applications/AE.app"))
    monkeypatch.setattr(menu, "_show_result", lambda window, result, connected, app: shown.append((result, connected, app)))
    window = types.SimpleNamespace(statusBar=lambda: types.SimpleNamespace(showMessage=lambda *a: None))
    menu.export_dialog(window)
    result, connected, app = shown[0]
    assert result.script_path == str(tmp_path / "out" / "Trip.jsx") and not result.collected
    assert (connected, app) == (False, "/Applications/AE.app")


def test_export_dialog_cancelled_does_nothing(menu, monkeypatch, tmp_path):
    monkeypatch.setattr(menu, "_snapshot", lambda window: _snapshot(tmp_path))
    monkeypatch.setattr(menu, "_ask_export_options", lambda window, default: None)
    monkeypatch.setattr(menu, "_run_job", lambda *a: pytest.fail("nothing should run"))
    menu.export_dialog(object())


def test_send_without_zenvi_link_offers_the_applescript_fallback(menu, monkeypatch, tmp_path):
    from classes import info
    from classes.handoff import after_effects_export as X
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(X, "find_after_effects_app", lambda: "/Applications/AE.app")
    offered = []
    monkeypatch.setattr(menu, "_snapshot", lambda window: _snapshot(tmp_path))
    monkeypatch.setattr(menu, "_run_job", _sync_runner)
    monkeypatch.setattr(menu, "_offer_fallback", lambda window, snap, message, app: offered.append((message, app)))
    menu.send(object())
    message, app = offered[0]
    assert "Zenvi Link" in message and app == "/Applications/AE.app"


def test_result_summary_counts_titles_missing_media_and_notes():
    result = types.SimpleNamespace(stats={"layers": 7, "footage": 4}, missing=["/x/a.mp4"], warnings=["w1", "w2"],
                                   titles=[{"mode": "native"}, {"mode": "png"}, {"mode": "native"}])
    from classes.handoff.after_effects import result_summary
    text = result_summary(result, _=lambda s: s)
    assert text == ("Exported 7 layer(s) and 4 footage item(s) for After Effects. 3 title(s): 2 as editable text, "
                    "1 as images. 1 media file(s) are missing and become placeholders. 2 note(s) below.")
