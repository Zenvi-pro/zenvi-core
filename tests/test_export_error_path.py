"""Export failures stay in the dialog and restore auto-save."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("openshot")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default


_app = QApplication.instance() or QApplication([])
if not hasattr(_app, "get_settings"):
    _app.get_settings = lambda: _Settings()

from windows.export import (  # noqa: E402
    friendly_export_error,
    pause_window_auto_save,
    resume_window_auto_save,
)


def test_friendly_export_error_keeps_the_raw_message():
    text = friendly_export_error("Could not open video codec")
    assert "Could not open video codec" in text
    assert "try again" in text.lower() or "codec" in text.lower()


def test_friendly_export_error_hints_on_audio_codec():
    text = friendly_export_error("Could not open audio codec: aac")
    assert "audio codec" in text.lower()
    assert "Video Only" in text or "audio codec" in text.lower()


def test_pause_and_resume_auto_save(monkeypatch):
    timer = MagicMock()
    timer.isActive.return_value = True
    window = MagicMock()
    window.auto_save_timer = timer
    app = MagicMock()
    app.window = window
    monkeypatch.setattr("windows.export.get_app", lambda: app)

    assert pause_window_auto_save() is True
    timer.stop.assert_called_once()

    resume_window_auto_save(True)
    timer.start.assert_called_once()


def test_pause_auto_save_when_timer_is_idle(monkeypatch):
    timer = MagicMock()
    timer.isActive.return_value = False
    window = MagicMock()
    window.auto_save_timer = timer
    app = MagicMock()
    app.window = window
    monkeypatch.setattr("windows.export.get_app", lambda: app)

    assert pause_window_auto_save() is False
    timer.stop.assert_not_called()
    resume_window_auto_save(False)
    timer.start.assert_not_called()


def test_run_export_failure_re_enables_controls_and_does_not_accept(monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QDialog

    from windows.export import Export

    dlg = Export.__new__(Export)
    QDialog.__init__(dlg)
    dlg.exporting = True
    dlg._headless = False
    dlg.cancel_button = object()
    dlg.s = MagicMock()
    dlg.s.get.return_value = False
    dlg.timeline = MagicMock()
    dlg.timeline.info = MagicMock()
    dlg.project = MagicMock()
    dlg.cache_thread = MagicMock()
    dlg.old_cache_object = MagicMock()
    dlg.progressExportVideo = None
    cache = dlg.cache_thread
    presented = []
    enabled = []
    accepted = []

    dlg._present_export_error = presented.append
    dlg.enableControls = lambda: enabled.append(True)
    dlg._show_export_finished = lambda: accepted.append(True)

    monkeypatch.setattr("windows.export.pause_window_auto_save", lambda: True)
    monkeypatch.setattr("windows.export.resume_window_auto_save", lambda was_active: None)
    monkeypatch.setattr("windows.export.get_app", lambda: MagicMock(
        _tr=lambda s: s,
        project=MagicMock(get=lambda *a, **k: {"num": 30, "den": 1}),
        window=MagicMock(),
    ))
    monkeypatch.setattr("windows.export.openshot.CacheMemory", lambda *a, **k: MagicMock())
    monkeypatch.setattr("windows.export.openshot.FFmpegWriter", lambda path: (_ for _ in ()).throw(
        RuntimeError("Could not open video codec")))
    monkeypatch.setattr("windows.export.track_metric_error", lambda *a, **k: None)

    video_settings = {
        "start_frame": 1,
        "end_frame": 10,
        "width": 1920,
        "height": 1080,
        "fps": {"num": 30, "den": 1},
        "pixel_ratio": {"num": 1, "den": 1},
        "vcodec": "libx264",
        "video_bitrate": 5000000,
        "interlace": False,
        "topfirst": False,
        "spherical": False,
    }
    audio_settings = {
        "acodec": "aac",
        "sample_rate": 48000,
        "channels": 2,
        "channel_layout": 2,
        "audio_bitrate": 192000,
    }

    dlg.run_export("/tmp/out.mp4", video_settings, audio_settings, "Video & Audio")

    assert presented, "export error must be shown in the UI"
    assert enabled == [True]
    assert accepted == []
    assert dlg.exporting is False
    cache.StopThread.assert_called()
