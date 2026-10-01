"""A hardware encoder must pass a trial encode in a child process before export uses it.

h264_videotoolbox on libopenshot 1.0 passes FFmpegWriter.IsValidCodec and then
aborts the whole process on its first frame
(``Assertion frame->format == AV_PIX_FMT_VIDEOTOOLBOX failed``). A Python
try/except cannot catch that, so export tries the encoder in a copy of the app
(``launch.py --zenvi-trial-encode <codec>``) and falls back to software.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from classes import encoder_trial
from classes.export_acceleration import hw_encode

SRC = Path(__file__).resolve().parents[1] / "src"

# A stand-in libopenshot for the child process. FAKE_OPENSHOT_MODE picks how
# the "encoder" behaves; FAKE_OPENSHOT_MARKER records which Qt modules the
# child had imported by the time it closed the writer.
_FAKE_OPENSHOT = textwrap.dedent('''
    import os, sys
    MODE = os.environ.get("FAKE_OPENSHOT_MODE", "ok")
    LAYOUT_STEREO = 3

    class Fraction:
        def __init__(self, num, den):
            pass

    class Timeline:
        def __init__(self, *args):
            pass
        def Open(self):
            pass
        def Close(self):
            pass
        def GetFrame(self, number):
            return number

    class FFmpegWriter:
        def __init__(self, path):
            self.path = path
        def SetVideoOptions(self, *args):
            if MODE == "raise":
                raise RuntimeError("Could not open video codec")
        def PrepareStreams(self):
            pass
        def Open(self):
            open(self.path, "wb").close()
        def WriteFrame(self, frame):
            if MODE == "abort":
                os.abort()
            with open(self.path, "ab") as fh:
                fh.write(b"frame")
        def Close(self):
            marker = os.environ.get("FAKE_OPENSHOT_MARKER")
            if marker:
                with open(marker, "w") as fh:
                    fh.write(",".join(sorted(
                        m for m in sys.modules if m.startswith(("PyQt", "qt_api")))))
''')


@pytest.fixture(autouse=True)
def _fresh_trial_cache():
    hw_encode.clear_encoder_probe_cache()
    yield
    hw_encode.clear_encoder_probe_cache()


@pytest.fixture
def fake_openshot(tmp_path, monkeypatch):
    """Put a fake `openshot` first on the child's import path."""
    lib = tmp_path / "fake_openshot"
    lib.mkdir()
    (lib / "openshot.py").write_text(_FAKE_OPENSHOT, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(lib))
    marker = tmp_path / "qt_modules.txt"
    monkeypatch.setenv("FAKE_OPENSHOT_MARKER", str(marker))
    return marker


def _launch_trial(codec):
    return subprocess.run(
        [sys.executable, str(SRC / "launch.py"), encoder_trial.TRIAL_FLAG, codec],
        capture_output=True, text=True, timeout=120,
    )


# --- the child: launch.py hands over before Qt ------------------------------

def test_trial_mode_encodes_and_exits_without_starting_qt(fake_openshot, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHOT_MODE", "ok")
    result = _launch_trial("h264_videotoolbox")
    assert result.returncode == 0, result.stderr
    assert fake_openshot.read_text() == "", "the trial child must not import Qt"


def test_trial_mode_reports_an_encoder_that_will_not_open(fake_openshot, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHOT_MODE", "raise")
    result = _launch_trial("h264_nvenc")
    assert result.returncode == 1
    assert "Could not open video codec" in result.stderr


def test_trial_main_needs_a_codec():
    assert encoder_trial.main([]) == 2


# --- the parent: an abort in the child costs a log line, not the app --------

def test_encoder_that_aborts_fails_the_trial_and_the_parent_lives(fake_openshot, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHOT_MODE", "abort")
    assert hw_encode._run_trial("h264_videotoolbox") is False


def test_encoder_that_works_passes_the_trial(fake_openshot, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHOT_MODE", "ok")
    assert hw_encode._run_trial("h264_videotoolbox") is True


def test_a_hung_trial_is_killed_and_fails(monkeypatch):
    monkeypatch.setattr(hw_encode, "TRIAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(hw_encode, "_trial_command",
                        lambda codec: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    assert hw_encode._run_trial("h264_nvenc") is False
    assert time.monotonic() - started < 30


def test_failure_detail_keeps_the_assertion_not_the_stack_dump():
    stderr = "\n".join([
        "Encoding Device Nr: 0",
        "Assertion frame->format == AV_PIX_FMT_VIDEOTOOLBOX failed at libavcodec/videotoolboxenc.c:2382",
        "Caught signal 6 (SIGABRT)",
    ] + ["  %d  Python  0x0000000102dd7354 frame" % i for i in range(20)])
    detail = hw_encode._failure_detail(stderr)
    assert "AV_PIX_FMT_VIDEOTOOLBOX" in detail
    assert "0x0000000102dd7354" not in detail


# --- selection -------------------------------------------------------------

def _count_trials(monkeypatch, passes):
    calls = []

    def fake_run_trial(codec):
        calls.append(codec)
        return passes

    monkeypatch.setattr(hw_encode, "_run_trial", fake_run_trial)
    return calls


def _codecs_exist(monkeypatch):
    fake = types.SimpleNamespace(
        FFmpegWriter=types.SimpleNamespace(IsValidCodec=lambda codec: True))
    monkeypatch.setitem(sys.modules, "openshot", fake)


def test_trial_result_is_cached_for_the_session(monkeypatch):
    calls = _count_trials(monkeypatch, passes=False)
    assert hw_encode.encoder_passes_trial("h264_videotoolbox") is False
    assert hw_encode.encoder_passes_trial("h264_videotoolbox") is False
    assert calls == ["h264_videotoolbox"]


def test_preferred_encoder_skips_hardware_that_fails_its_trial(monkeypatch):
    _codecs_exist(monkeypatch)
    monkeypatch.setattr(hw_encode, "platform_encoder_candidates",
                        lambda family="h264": ["h264_videotoolbox", "libx264"])
    calls = _count_trials(monkeypatch, passes=False)
    assert hw_encode.get_preferred_video_encoder(prefer_hardware=True) == "libx264"
    assert calls == ["h264_videotoolbox"]


def test_preferred_encoder_keeps_hardware_that_passes_its_trial(monkeypatch):
    _codecs_exist(monkeypatch)
    monkeypatch.setattr(hw_encode, "platform_encoder_candidates",
                        lambda family="h264": ["h264_nvenc", "libx264"])
    _count_trials(monkeypatch, passes=True)
    assert hw_encode.get_preferred_video_encoder(prefer_hardware=True) == "h264_nvenc"


def test_headless_default_settings_do_not_pick_a_failing_encoder(monkeypatch):
    _codecs_exist(monkeypatch)
    monkeypatch.setattr(hw_encode, "platform_encoder_candidates",
                        lambda family="h264": ["h264_videotoolbox", "libx264"])
    _count_trials(monkeypatch, passes=False)
    out = hw_encode.maybe_apply_hardware_bitrate(
        {"vcodec": "libx264", "video_bitrate": 2_000_000}, prefer_hardware=True)
    assert out == {"vcodec": "libx264", "video_bitrate": 2_000_000}


@pytest.mark.parametrize("codec, software", [
    ("h264_videotoolbox", "libx264"),
    ("hevc_vaapi", "libx265"),
    ("vp9_vaapi", "libvpx-vp9"),
    ("h264_nvenc", "libx264"),
])
def test_safe_encoder_swaps_a_failing_hardware_encoder_for_software(monkeypatch, codec, software):
    _count_trials(monkeypatch, passes=False)
    assert hw_encode.safe_video_encoder(codec) == software


def test_safe_encoder_says_so_when_the_software_fallback_is_missing(monkeypatch):
    _count_trials(monkeypatch, passes=False)
    fake = types.SimpleNamespace(
        FFmpegWriter=types.SimpleNamespace(IsValidCodec=lambda codec: codec != "libx265"))
    monkeypatch.setitem(sys.modules, "openshot", fake)
    with pytest.raises(RuntimeError, match="libx265"):
        hw_encode.safe_video_encoder("hevc_vaapi")


def test_safe_encoder_leaves_software_encoders_untried(monkeypatch):
    calls = _count_trials(monkeypatch, passes=False)
    assert hw_encode.safe_video_encoder("libx264") == "libx264"
    assert hw_encode.safe_video_encoder("libvpx-vp9") == "libvpx-vp9"
    assert calls == []


def test_poll_keeps_running_while_the_trial_is_pending(monkeypatch):
    release = threading.Event()

    def slow_trial(codec):
        release.wait(5)
        return True

    monkeypatch.setattr(hw_encode, "_run_trial", slow_trial)
    polls = []

    def poll():
        polls.append(1)
        if len(polls) >= 3:
            release.set()

    assert hw_encode.encoder_passes_trial("h264_videotoolbox", poll=poll) is True
    assert len(polls) >= 3


# --- export uses the safe encoder --------------------------------------------

def _import_real_export_module(monkeypatch):
    for name in ("openshot", "classes.openshot_rc", "classes.ui_util",
                 "classes.metrics", "classes.app", "classes.query",
                 "PyQt5.QtGui", "PyQt5"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    import windows.export as export_mod
    return export_mod


def _run_export_with(monkeypatch, vcodec, headless, cancel_during_trial=False):
    """Run the real Export.run_export on a plain object; return (writer, notices)."""
    export_mod = _import_real_export_module(monkeypatch)
    writer = MagicMock()
    fake_openshot = MagicMock()
    fake_openshot.FFmpegWriter.return_value = writer
    fake_openshot.FFmpegWriter.IsValidCodec.return_value = True
    monkeypatch.setattr(export_mod, "openshot", fake_openshot)

    def safe_encoder(codec, poll=None):
        if cancel_during_trial:
            job.exporting = False
        return "libx264" if "videotoolbox" in codec else codec

    monkeypatch.setattr(export_mod, "safe_video_encoder", safe_encoder)
    monkeypatch.setattr(export_mod, "get_app", lambda: MagicMock(
        _tr=lambda s: s, project=MagicMock(get=lambda *a, **k: {"num": 30, "den": 1})))
    monkeypatch.setattr(export_mod, "pause_window_auto_save", lambda: False)
    monkeypatch.setattr(export_mod, "resume_window_auto_save", lambda was_active: None)
    monkeypatch.setattr(export_mod, "run_pipelined_export", None)
    monkeypatch.setattr(export_mod, "try_smart_render_export", None)

    notices = []
    job = types.SimpleNamespace(
        _headless=headless, exporting=True, s=None,
        timeline=MagicMock(), project=MagicMock(), cache_thread=MagicMock(),
        ExportStarted=MagicMock(), ExportFrame=MagicMock(), ExportEnded=MagicMock(),
        _cleanup_export_resources=lambda: None, _show_export_finished=lambda: None,
        _complete_export_success=writer.export_completed, enableControls=lambda: None,
        _present_encoder_fallback=lambda hw, sw: notices.append((hw, sw)),
    )
    video_settings = {
        "vformat": "mp4", "vcodec": vcodec, "fps": {"num": 30, "den": 1},
        "width": 640, "height": 360, "pixel_ratio": {"num": 1, "den": 1},
        "video_bitrate": 4_000_000, "start_frame": 1, "end_frame": 3,
        "interlace": False, "topfirst": False, "spherical": False,
    }
    audio_settings = {"acodec": "aac", "sample_rate": 48000, "channels": 2,
                      "channel_layout": 3, "audio_bitrate": 192000}
    export_mod.Export.run_export(job, "/tmp/out.mp4", video_settings, audio_settings, "Video Only")
    return writer, notices


def test_dialog_export_with_a_failing_hardware_preset_uses_software_and_says_so(monkeypatch):
    writer, notices = _run_export_with(monkeypatch, "h264_videotoolbox", headless=False)
    assert writer.SetVideoOptions.call_args[0][1] == "libx264"
    assert notices == [("h264_videotoolbox", "libx264")]
    # The software GOP / B-frame tuning follows the codec actually used.
    options = [c[0][1:] for c in writer.SetOption.call_args_list]
    assert ("g", "48") in options


def test_cancel_during_the_trial_stops_before_anything_is_written(monkeypatch):
    writer, _ = _run_export_with(
        monkeypatch, "h264_videotoolbox", headless=False, cancel_during_trial=True)
    writer.Open.assert_not_called()
    writer.WriteFrame.assert_not_called()
    writer.export_completed.assert_not_called()


def test_export_keeps_a_working_codec(monkeypatch):
    writer, notices = _run_export_with(monkeypatch, "libx264", headless=True)
    assert writer.SetVideoOptions.call_args[0][1] == "libx264"
    assert notices == []


def test_launch_handles_the_trial_flag_before_any_app_setup():
    """The branch must come before the update installer and Qt imports."""
    source = (SRC / "launch.py").read_text(encoding="utf-8")
    flag_at = source.index("_TRIAL_FLAG:")
    assert flag_at < source.index("update_installer")
    assert flag_at < source.index("crash_handler.install()")
    assert flag_at < source.index("from qt_api import")


def test_trial_runs_this_checkout_s_launcher():
    cmd = hw_encode._trial_command("h264_videotoolbox")
    assert cmd[0] == sys.executable
    assert Path(cmd[1]).resolve() == (SRC / "launch.py").resolve()
    assert cmd[2:] == [encoder_trial.TRIAL_FLAG, "h264_videotoolbox"]


def test_frozen_build_trial_runs_the_app_binary(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert hw_encode._trial_command("h264_nvenc") == [
        sys.executable, encoder_trial.TRIAL_FLAG, "h264_nvenc"]
