"""Opening Export drops the decoded frames held by the preview timeline's clips and readers."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest


class _Stop(Exception):
    """Ends Export.__init__ right after the preview-cache block."""


class _Timeline:
    """Stands in for the preview timeline: records ClearAllCache calls, raises `error` if set."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def SetCache(self, cache):
        pass

    def ClearAllCache(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error


@pytest.fixture
def export_mod(monkeypatch):
    if os.environ.get("ZENVI_REAL_QT") == "1":
        pytest.skip("drives Export.__init__ on the headless Qt stub")
    # export.py imports classes.metrics, which calls get_app() at import time.
    monkeypatch.setitem(sys.modules, "classes.metrics", MagicMock())
    import windows.export as mod

    return mod


def _open_export(export_mod, monkeypatch, timeline):
    """Run Export.__init__ up to its VideoCacheThread() call, with the Qt widgets mocked."""
    app = MagicMock()
    app._tr = lambda s: s
    app.project = {}
    app.window.timeline_sync.timeline = timeline
    monkeypatch.setattr(export_mod, "get_app", lambda: app)
    monkeypatch.setattr(export_mod, "ui_util", MagicMock())
    monkeypatch.setattr(export_mod, "track_metric_screen", lambda *a, **k: None)
    fake_openshot = MagicMock()
    fake_openshot.VideoCacheThread.side_effect = _Stop
    monkeypatch.setattr(export_mod, "openshot", fake_openshot)

    class _Dialog(export_mod.Export):
        def __getattr__(self, name):  # undeclared widgets become plain mocks
            return MagicMock()

    dlg = _Dialog.__new__(_Dialog)
    export_mod.QDialog.__init__(dlg)
    dlg._setup_toolbox_tab_order = lambda: None
    with pytest.raises(_Stop):
        export_mod.Export.__init__(dlg)


def test_init_deep_clears_the_preview_timeline(export_mod, monkeypatch):
    timeline = _Timeline()

    _open_export(export_mod, monkeypatch, timeline)

    assert timeline.calls == [(True,)]


def test_init_logs_and_survives_a_failed_deep_clear(export_mod, monkeypatch):
    fake_log = MagicMock()
    monkeypatch.setattr(export_mod, "log", fake_log)
    timeline = _Timeline(error=RuntimeError("cache busy"))

    _open_export(export_mod, monkeypatch, timeline)  # the RuntimeError must not escape __init__

    assert timeline.calls == [(True,)]
    fake_log.warning.assert_called_once()
    assert fake_log.warning.call_args.kwargs.get("exc_info") is True
