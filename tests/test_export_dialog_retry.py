"""After a failed export the dialog stays open, and trying again must work.

run_export()'s finally block used to tear everything down after any attempt:
it closed the export timeline and dropped the cache thread. The dialog kept
offering "Export Video", and the next click failed with
``'NoneType' object has no attribute 'Reader'``. A failed attempt now only
stops its cache thread; the full teardown waits for a finished export or for
the dialog to close.

These drive the real Export.run_export on a plain object carrying the dialog's
state, so they run under the headless stub.
"""

from __future__ import annotations

import copy
import sys
from unittest.mock import MagicMock

import pytest


def _import_real_export_module(monkeypatch):
    for name in ("openshot", "classes.openshot_rc", "classes.ui_util",
                 "classes.metrics", "classes.app", "classes.query",
                 "PyQt5.QtGui", "PyQt5"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    import windows.export as export_mod
    return export_mod


class _Project:
    """Just enough of ProjectDataStore for run_export's fps rescale."""

    def __init__(self, fps=30):
        self._data = {"fps": {"num": fps, "den": 1}, "keyframes_scaled_by": 1.0,
                      "profile": "none", "clips": []}

    def get(self, key):
        return copy.deepcopy(self._data.get(key))

    def rescale_keyframes(self, factor):
        self._data["keyframes_scaled_by"] *= factor


@pytest.fixture
def export_mod(monkeypatch):
    mod = _import_real_export_module(monkeypatch)
    live_project = _Project()
    monkeypatch.setattr(mod, "get_app", lambda: MagicMock(
        _tr=lambda s: s, project=live_project, window=MagicMock()))
    monkeypatch.setattr(mod, "pause_window_auto_save", lambda: False)
    monkeypatch.setattr(mod, "resume_window_auto_save", lambda was_active: None)
    monkeypatch.setattr(mod, "track_metric_error", lambda *a, **k: None)
    monkeypatch.setattr(mod, "run_pipelined_export", None)
    monkeypatch.setattr(mod, "try_smart_render_export", None)
    return mod


def _writers(monkeypatch, export_mod, *outcomes):
    """FFmpegWriter that fails or works per attempt, in order."""
    fake_openshot = MagicMock()
    made = []

    def make_writer(path):
        outcome = outcomes[len(made)]
        made.append(path)
        if isinstance(outcome, Exception):
            raise outcome
        return MagicMock()

    fake_openshot.FFmpegWriter.side_effect = make_writer
    fake_openshot.FFmpegWriter.IsValidCodec.return_value = True
    monkeypatch.setattr(export_mod, "openshot", fake_openshot)
    return made


def _dialog(export_mod):
    Export = export_mod.Export

    class _Dialog:
        run_export = Export.run_export
        _cleanup_export_resources = Export._cleanup_export_resources
        _end_export_attempt = Export._end_export_attempt
        _reset_for_retry = Export._reset_for_retry
        _complete_export_success = Export._complete_export_success

    dlg = _Dialog()
    dlg._headless = False
    dlg.exporting = True
    dlg.s = None
    dlg.cancel_button = object()
    dlg.timeline = MagicMock()
    dlg.cache_thread = MagicMock()
    dlg.old_cache_object = MagicMock()
    dlg.project = _Project()
    dlg.ExportStarted, dlg.ExportFrame, dlg.ExportEnded = MagicMock(), MagicMock(), MagicMock()
    dlg.errors, dlg.finished = [], []
    dlg._present_export_error = dlg.errors.append
    dlg.enableControls = lambda: None
    dlg._show_export_finished = lambda: dlg.finished.append(True)
    return dlg


def _video(**overrides):
    settings = {"vformat": "mp4", "vcodec": "libx264", "fps": {"num": 30, "den": 1},
                "width": 640, "height": 360, "pixel_ratio": {"num": 1, "den": 1},
                "video_bitrate": 2_000_000, "start_frame": 1, "end_frame": 3,
                "interlace": False, "topfirst": False, "spherical": False}
    settings.update(overrides)
    return settings


_AUDIO = {"acodec": "aac", "sample_rate": 48000, "channels": 2,
          "channel_layout": 3, "audio_bitrate": 192000}


def test_a_failed_export_keeps_the_dialog_able_to_export(monkeypatch, export_mod):
    made = _writers(monkeypatch, export_mod, RuntimeError("Could not open video codec"), "ok")
    dlg = _dialog(export_mod)
    cache_thread, timeline = dlg.cache_thread, dlg.timeline

    dlg.run_export("/tmp/out.mp4", _video(), _AUDIO, "Video Only")

    assert dlg.errors, "the failure is shown in the dialog"
    assert dlg.cache_thread is cache_thread, "the cache thread survives a failed attempt"
    cache_thread.StopThread.assert_called()
    timeline.Close.assert_not_called()

    # The user fixes the settings and clicks Export Video again.
    dlg.exporting = True
    dlg.run_export("/tmp/out.mp4", _video(), _AUDIO, "Video Only")

    assert len(dlg.errors) == 1, "the second attempt must not fail on a torn-down dialog"
    assert len(made) == 2
    assert cache_thread.StartThread.call_count == 2
    cache_thread.Reader.assert_any_call(timeline)
    dlg.ExportEnded.emit.assert_called_once_with("/tmp/out.mp4")
    assert dlg.finished == [True]
    # Everything is torn down exactly once, after the export that worked.
    assert timeline.Close.call_count == 1
    assert dlg.cache_thread is None


def test_a_retry_drops_frames_cached_by_the_failed_attempt(monkeypatch, export_mod):
    _writers(monkeypatch, export_mod, RuntimeError("Could not open video codec"), "ok")
    dlg = _dialog(export_mod)
    dlg.run_export("/tmp/out.mp4", _video(), _AUDIO, "Video Only")
    dlg.timeline.ClearAllCache.assert_not_called()

    dlg.exporting = True
    dlg.run_export("/tmp/out.mp4", _video(width=1280, height=720), _AUDIO, "Video Only")
    # Once for the retry, once in the final teardown.
    assert dlg.timeline.ClearAllCache.call_count == 2


def test_a_retry_at_another_fps_rescales_the_original_keyframes(monkeypatch, export_mod):
    """The failed attempt left self.project scaled for 60 fps; rescaling that
    again for the retry used to scale the keyframes twice."""
    _writers(monkeypatch, export_mod, RuntimeError("Could not open video codec"), "ok")
    dlg = _dialog(export_mod)

    dlg.run_export("/tmp/out.mp4", _video(fps={"num": 60, "den": 1}), _AUDIO, "Video Only")
    assert dlg.project._data["keyframes_scaled_by"] == 2.0

    dlg.exporting = True
    dlg.run_export("/tmp/out.mp4", _video(fps={"num": 60, "den": 1}), _AUDIO, "Video Only")
    assert dlg.project._data["keyframes_scaled_by"] == 2.0, "scaled once, not 2 x 2"


def test_a_successful_export_still_cleans_up_once(monkeypatch, export_mod):
    _writers(monkeypatch, export_mod, "ok")
    dlg = _dialog(export_mod)
    dlg.run_export("/tmp/out.mp4", _video(), _AUDIO, "Video Only")
    assert dlg.timeline.Close.call_count == 1
    dlg._cleanup_export_resources()  # e.g. reject() when the user closes the dialog
    assert dlg.timeline.Close.call_count == 1


def test_a_failed_headless_export_tears_down(monkeypatch, export_mod):
    """Headless exports throw their Export object away, so nothing is kept."""
    _writers(monkeypatch, export_mod, RuntimeError("Could not open video codec"))
    dlg = _dialog(export_mod)
    dlg._headless = True
    with pytest.raises(RuntimeError):
        dlg.run_export("/tmp/out.mp4", _video(), _AUDIO, "Video Only")
    assert dlg.timeline.Close.call_count == 1
    assert dlg.cache_thread is None
