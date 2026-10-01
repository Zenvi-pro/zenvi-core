"""Project Files export helpers: animated titles (image sequences) export as
one MP4 and silent sources get audio disabled (OpenShot #6032)."""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import classes.app as _app_module  # noqa: E402


def _import_export_clips():
    """windows.export_clips reads get_app()._tr at import time; stub the app for that."""
    fake_app = types.SimpleNamespace(_tr=lambda text: text)
    original = _app_module.get_app
    _app_module.get_app = lambda: fake_app
    try:
        return importlib.import_module("windows.export_clips")
    finally:
        _app_module.get_app = original


export_clips = _import_export_clips()


class _Fraction:
    def __init__(self, num, den):
        self.num, self.den = num, den


@pytest.fixture(autouse=True)
def _openshot_fraction(monkeypatch):
    monkeypatch.setattr(export_clips.openshot, "Fraction", _Fraction, raising=False)


def test_image_sequence_detection_matches_printf_pattern():
    sequence = types.SimpleNamespace(data={"path": os.path.join("titles", "Title%04d.png")})
    normal_file = types.SimpleNamespace(data={"path": os.path.join("titles", "Title0001.png")})

    assert export_clips.isImageSequence(sequence)
    assert not export_clips.isImageSequence(normal_file)


def test_image_sequence_export_name_is_single_mp4():
    file_obj = types.SimpleNamespace(data={
        "path": os.path.join("titles", "Title%04d.png"),
        "fps": {"num": 25, "den": 1},
        "video_length": 300,
    })

    assert export_clips.nameOfImageSequenceExport(file_obj) == "title [0.00 - 12.00].mp4"


def test_image_sequence_as_clip_sets_full_duration_range():
    file_obj = types.SimpleNamespace(data={
        "path": os.path.join("titles", "Title%04d.png"),
        "fps": {"num": 25, "den": 1},
        "video_length": 300,
        "width": 1920,
    })

    clip_obj = export_clips.imageSequenceAsClip(file_obj)

    assert clip_obj.data["start"] == 0.0
    assert clip_obj.data["end"] == 12.0
    assert clip_obj.data["path"] == os.path.join("titles", "Title%04d.png")
    assert clip_obj.data["width"] == 1920


def test_setup_writer_disables_audio_for_silent_image_sequence():
    clip_obj = types.SimpleNamespace(data={
        "path": os.path.join("titles", "Title%04d.png"),
        "fps": {"num": 25, "den": 1},
        "pixel_ratio": {"num": 1, "den": 1},
        "width": 1920,
        "height": 1080,
        "has_audio": False,
        "sample_rate": 0,
        "channels": 0,
        "channel_layout": 0,
    })
    writer = types.SimpleNamespace(audio_options=None, video_options=None, prepare_count=0, opened=False)
    writer.SetVideoOptions = lambda *args: setattr(writer, "video_options", args)
    writer.SetAudioOptions = lambda *args: setattr(writer, "audio_options", args)
    writer.PrepareStreams = lambda: setattr(writer, "prepare_count", writer.prepare_count + 1)
    writer.Open = lambda: setattr(writer, "opened", True)

    export_clips.setupWriter(clip_obj, writer)

    assert writer.audio_options[0] is False
    assert writer.audio_options[2:5] == (48000, 2, 3)
    assert writer.prepare_count == 2
    assert writer.opened


def test_frame_range_is_inclusive_and_one_based():
    clip = types.SimpleNamespace(data={"fps": {"num": 25, "den": 1}, "start": 0.0, "end": 2.0})

    assert export_clips.startAndEndFrames(clip) == (1, 50)
    assert export_clips.framesInClip(clip) == 50
