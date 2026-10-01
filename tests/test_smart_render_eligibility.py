"""Smart render engages for untouched clips, and only copies what it can copy exactly.

libopenshot 1.0 writes every keyframed property into a clip's JSON: location,
rotation and shear default to 0, and time is a one-point curve. The old check
compared every single point against 1.0 and rejected any time dict, so a clip
nobody had touched looked moved, rotated, sheared and retimed, and smart render
never ran. ``tests/fixtures/libopenshot_1_0_untouched_clip.json`` is that JSON,
as libopenshot 1.0 wrote it for a 12 s 576x1024 25 fps H.264 + AAC file.

Turning it on exposed what a ``-c copy`` cut can and cannot do: it starts on
the keyframe before the cut and overshoots a cut end by a few frames, so only a
whole clip that plays its whole file is copied, and the copy is counted.
"""

from __future__ import annotations

import copy
import types
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from classes.export_acceleration import smart_render
from classes.export_acceleration.smart_render import (
    analyze_smart_render_spans,
    clip_smart_render_reasons,
    try_smart_render_export,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "libopenshot_1_0_untouched_clip.json"
_AUDIO = {"acodec": "aac", "sample_rate": 48000, "channels": 2,
          "channel_layout": 3, "audio_bitrate": 192000}


def _kf(*values):
    return {"Points": [
        {"co": {"X": float(1 + 30 * i), "Y": float(v)}, "interpolation": 0}
        for i, v in enumerate(values)]}


@pytest.fixture
def untouched(tmp_path):
    source = tmp_path / "portrait_dialogue_12s.mp4"
    source.write_bytes(b"not decoded by these tests")
    clip = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    clip["reader"]["path"] = str(source)
    return clip


def _reasons(clip, audio=_AUDIO):
    return clip_smart_render_reasons(
        clip, export_width=576, export_height=1024, export_fps=25.0,
        export_vcodec="libx264", export_audio=audio)


def _spans(clips, *, end_frame, transitions=(), audio=_AUDIO, start_frame=1):
    project = {"fps": {"num": 25, "den": 1}, "clips": list(clips), "effects": list(transitions)}
    return analyze_smart_render_spans(
        project, export_width=576, export_height=1024, export_fps=25.0,
        export_vcodec="libx264", start_frame=start_frame, end_frame=end_frame,
        export_audio=audio)


# --- the reported bug ------------------------------------------------------

def test_an_untouched_libopenshot_clip_is_eligible(untouched):
    assert _reasons(untouched) == []


def test_an_untouched_clip_covering_the_export_is_one_copy_span(untouched):
    spans = _spans([untouched], end_frame=300)
    assert [(s.kind, s.start_frame, s.end_frame) for s in spans] == [("copy", 1, 300)]


@pytest.mark.parametrize("prop, value, reason", [
    ("location_x", 0.25, "translated"),
    ("location_y", -0.1, "translated"),
    ("rotation", 15.0, "rotated"),
    ("shear_x", 0.2, "sheared"),
    ("scale_x", 0.5, "scaled"),
    ("scale_y", 1.5, "scaled"),
    ("alpha", 0.5, "alpha-change"),
    ("margin", 0.1, "margin"),
    ("corner_radius", 12.0, "rounded-corners"),
    ("perspective_c1_x", 0.2, "perspective"),
    # A one-point volume of 0.5 used to slip through as "unchanged".
    ("volume", 0.5, "volume-change"),
    ("channel_filter", 0.0, "audio-channels"),
    ("has_video", 0.0, "video-off"),
    ("has_audio", 0.0, "audio-off"),
])
def test_a_property_away_from_its_default_is_encoded(untouched, prop, value, reason):
    untouched[prop] = _kf(value)
    assert reason in _reasons(untouched)


def test_a_curve_that_stays_at_the_default_is_still_untouched(untouched):
    untouched["location_x"] = _kf(0.0, 0.0, 0.0)
    untouched["alpha"] = _kf(1.0, 1.0)
    assert _reasons(untouched) == []


def test_a_keyframed_fade_is_encoded(untouched):
    untouched["alpha"] = _kf(0.0, 1.0)
    assert "alpha-change" in _reasons(untouched)


def test_a_time_curve_is_a_speed_change(untouched):
    untouched["time"] = _kf(1.0, 150.0)
    assert "speed-change" in _reasons(untouched)


def test_waveform_display_is_encoded(untouched):
    untouched["waveform"] = True
    assert "waveform" in _reasons(untouched)


def test_audio_only_properties_do_not_matter_without_audio(untouched):
    untouched["volume"] = _kf(0.2)
    untouched["has_audio"] = _kf(0.0)
    assert _reasons(untouched, audio=None) == []


# --- what a stream copy cannot do exactly -----------------------------------

def test_a_trimmed_start_is_encoded(untouched):
    untouched["start"] = 3.0
    assert "trimmed" in _reasons(untouched)


def test_a_trimmed_end_is_encoded(untouched):
    untouched["end"] = 8.0
    assert "trimmed" in _reasons(untouched)


def test_an_export_range_that_cuts_the_clip_is_encoded(untouched):
    spans = _spans([untouched], end_frame=150)
    assert [s.kind for s in spans] == ["encode"]
    assert spans[0].reasons == ("partial-clip",)


def test_transitions_are_read_from_where_the_project_keeps_them(untouched):
    fade = {"id": "T1", "layer": 1, "position": 0.0, "start": 0.0, "end": 1.0}
    spans = _spans([untouched], end_frame=300, transitions=[fade])
    assert not all(s.kind == "copy" for s in spans)
    assert "transition" in {r for s in spans for r in s.reasons}


def test_mismatched_source_audio_is_encoded(untouched):
    untouched["reader"]["sample_rate"] = 44100
    assert "audio-mismatch" in _reasons(untouched)


def test_a_ten_bit_source_is_encoded(untouched):
    untouched["reader"]["pixel_format"] = 64
    assert "pixel-format-mismatch" in _reasons(untouched)


def test_an_image_is_never_copied(untouched):
    untouched["reader"].update(type="QtImageReader", has_single_image=True, vcodec="")
    reasons = _reasons(untouched)
    assert "not-a-video-file" in reasons


# --- the copy itself (real ffmpeg) -------------------------------------------

_needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg + ffprobe")


def _make_source(path, seconds=2, fps=25):
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
        "-shortest", str(path)], check=True)


def _streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.split()
    return sorted(out)


def _clip_for(source, untouched, seconds=2.0):
    clip = copy.deepcopy(untouched)
    clip["reader"].update(path=str(source), width=320, height=240, duration=seconds)
    clip.update(position=0.0, start=0.0, end=seconds)
    return clip


def _export(clip, out, audio):
    return try_smart_render_export(
        {"fps": {"num": 25, "den": 1}, "clips": [clip], "effects": []},
        export_file_path=str(out),
        video_settings={"width": 320, "height": 240, "fps": {"num": 25, "den": 1}, "vcodec": "libx264"},
        start_frame=1, end_frame=50, encode_span=lambda *a: False, audio_settings=audio)


@_needs_ffmpeg
def test_a_whole_untouched_clip_is_copied_frame_exact(tmp_path, untouched):
    source = tmp_path / "src.mp4"
    _make_source(source)
    out = tmp_path / "out.mp4"
    result = _export(_clip_for(source, untouched), out, _AUDIO)
    assert result and result["copy_spans"] == 1
    assert smart_render._video_frame_count(str(out)) == 50
    assert _streams(out) == ["audio", "video"]


@_needs_ffmpeg
def test_a_video_only_copy_drops_the_audio(tmp_path, untouched):
    source = tmp_path / "src.mp4"
    _make_source(source)
    out = tmp_path / "out.mp4"
    assert _export(_clip_for(source, untouched), out, None)
    assert _streams(out) == ["video"]


@_needs_ffmpeg
def test_a_copy_with_the_wrong_frame_count_falls_back(tmp_path, untouched, monkeypatch):
    source = tmp_path / "src.mp4"
    _make_source(source)
    monkeypatch.setattr(smart_render, "_video_frame_count", lambda path: 53)
    assert _export(_clip_for(source, untouched), tmp_path / "out.mp4", _AUDIO) is None


@_needs_ffmpeg
def test_cutting_mid_gop_really_does_overshoot(tmp_path, untouched):
    """Why trims are encoded: this is what the copy command does to one."""
    source = tmp_path / "src.mp4"
    _make_source(source, seconds=4)
    seg = tmp_path / "seg.mp4"
    clip = _clip_for(source, untouched, seconds=4.0)
    clip.update(start=1.3, end=2.5)
    assert smart_render._stream_copy_span(
        clip, start_frame=1, end_frame=30, fps=25.0, output_path=str(seg))
    assert smart_render._video_frame_count(str(seg)) != 30


def test_a_flat_time_curve_is_still_a_freeze_frame(untouched):
    untouched["time"] = _kf(1.0, 1.0)
    assert "speed-change" in _reasons(untouched)


def test_hardware_encoders_do_not_get_partial_segments(tmp_path, untouched, monkeypatch):
    """Partial segments are written outside run_export's encoder checks."""
    calls = []
    clean = copy.deepcopy(untouched)
    dirty = copy.deepcopy(untouched)
    dirty.update(id="B", position=12.0)
    dirty["alpha"] = _kf(0.5)
    project = {"fps": {"num": 25, "den": 1}, "clips": [clean, dirty], "effects": []}
    result = try_smart_render_export(
        project, export_file_path=str(tmp_path / "out.mp4"),
        video_settings={"width": 576, "height": 1024, "fps": {"num": 25, "den": 1},
                        "vcodec": "h264_videotoolbox"},
        start_frame=1, end_frame=600,
        encode_span=lambda *a: calls.append(a) or False, audio_settings=_AUDIO)
    assert result is None
    assert calls == []


def test_a_smart_rendered_export_reports_progress_the_dialog_can_format(monkeypatch):
    """The success path emitted "100.0%% " (nothing to format into), so the
    dialog's updateProgressBar raised TypeError, popped "Something went wrong"
    and never reached its finished state."""
    import sys
    from unittest.mock import MagicMock
    for name in ("openshot", "classes.openshot_rc", "classes.ui_util",
                 "classes.metrics", "classes.app", "classes.query", "PyQt5.QtGui", "PyQt5"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    import windows.export as export_mod

    monkeypatch.setattr(export_mod, "get_app", lambda: MagicMock(
        _tr=lambda s: s, project=MagicMock(get=lambda *a, **k: {"num": 30, "den": 1})))
    monkeypatch.setattr(export_mod, "pause_window_auto_save", lambda: False)
    monkeypatch.setattr(export_mod, "resume_window_auto_save", lambda was_active: None)
    monkeypatch.setattr(export_mod, "try_smart_render_export",
                        lambda *a, **k: {"mode": "full_copy", "path": "/tmp/out.mp4"})
    monkeypatch.setattr(export_mod, "decide_smart_render", lambda *a, **k: None)
    monkeypatch.setattr(export_mod, "openshot", MagicMock())
    # run_export imports QApplication from PyQt5.QtWidgets; under the headless
    # stub that is a bare mock class without processEvents.
    monkeypatch.setattr(sys.modules["PyQt5.QtWidgets"], "QApplication", MagicMock(), raising=False)
    monkeypatch.setattr(export_mod, "QCoreApplication", MagicMock())

    frames, finished = [], []
    job = types.SimpleNamespace(
        _headless=False, exporting=True, s=None, timeline=MagicMock(), project=MagicMock(),
        cache_thread=MagicMock(), ExportStarted=MagicMock(), ExportEnded=MagicMock(),
        ExportFrame=types.SimpleNamespace(emit=lambda *args: frames.append(args)),
        _cleanup_export_resources=lambda: None,
        _show_export_finished=lambda: finished.append(True))
    job._complete_export_success = types.MethodType(export_mod.Export._complete_export_success, job)
    video = {"vformat": "mp4", "vcodec": "libx264", "fps": {"num": 30, "den": 1},
             "width": 640, "height": 360, "pixel_ratio": {"num": 1, "den": 1},
             "video_bitrate": 2_000_000, "start_frame": 1, "end_frame": 240,
             "interlace": False, "topfirst": False, "spherical": False}
    export_mod.Export.run_export(job, "/tmp/out.mp4", video, dict(_AUDIO), "Video & Audio")

    title, start, end, current, fmt = frames[-1]
    assert (fmt % ((current - start) / (end - start) * 100)).strip() == "100.0%"
    assert finished == [True]
