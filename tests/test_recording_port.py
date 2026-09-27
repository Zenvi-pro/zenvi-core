"""Headless checks for the pieces the recording port touches outside the dock.

The Recording dock itself needs real Qt and libopenshot (see
src/tests/test_recording_preview.py). These tests cover the helpers the
timeline, thumbnail and cache layers gained with it:

* peak/RMS waveform metadata helpers in ``classes.waveform``
* the coarse thumbnail grid live recordings write to
* the media-cache waveform payload now carrying RMS/format/rate
"""

import types
from unittest.mock import patch

import pytest

from classes import info
from classes import waveform
from classes import thumbnail
from classes.media_cache import load_waveform, save_waveform


def test_waveform_format_defaults_to_legacy_for_untagged_projects():
    assert waveform.waveform_data_format({}) == waveform.LEGACY_WAVEFORM_FORMAT
    assert waveform.waveform_data_format(None) == waveform.LEGACY_WAVEFORM_FORMAT
    tagged = {waveform.WAVEFORM_FORMAT_KEY: waveform.ABSOLUTE_WAVEFORM_FORMAT}
    assert waveform.waveform_data_format(tagged) == waveform.ABSOLUTE_WAVEFORM_FORMAT


def test_waveform_sample_rate_defaults_to_20hz_and_rejects_bad_values():
    assert waveform.waveform_sample_rate({}) == waveform.LEGACY_SAMPLES_PER_SECOND
    assert waveform.waveform_sample_rate({waveform.WAVEFORM_RATE_KEY: 200}) == 200
    assert waveform.waveform_sample_rate({waveform.WAVEFORM_RATE_KEY: "abc"}) == 20
    assert waveform.waveform_sample_rate({waveform.WAVEFORM_RATE_KEY: -5}) == 20


def test_legacy_waveform_amplitude_is_linear_and_absolute_uses_root_curve():
    legacy = waveform.LEGACY_WAVEFORM_FORMAT
    absolute = waveform.ABSOLUTE_WAVEFORM_FORMAT
    assert waveform.waveform_display_amplitude(0.25, legacy) == pytest.approx(0.25)
    assert waveform.waveform_display_amplitude(0.25, absolute) == pytest.approx(0.5, abs=1e-3)
    assert waveform.waveform_display_amplitude(0.0, absolute) == 0.0
    assert waveform.waveform_display_amplitude(float("nan"), absolute) == 0.0
    assert waveform.waveform_display_amplitude("bad", absolute) == 0.0
    # Peaks are clamped to full height, never above it.
    assert waveform.waveform_display_amplitude(1.5, absolute) == pytest.approx(1.0)


def test_configured_sample_rate_follows_timeline_preference_within_bounds():
    def app_with(value):
        settings = types.SimpleNamespace(get=lambda key: value if key == "timeline-waveform-samples-per-second" else None)
        return types.SimpleNamespace(get_settings=lambda: settings)

    with patch.object(waveform, "get_app", return_value=app_with(120)):
        assert waveform.configured_waveform_sample_rate() == 120
    with patch.object(waveform, "get_app", return_value=app_with(5)):
        assert waveform.configured_waveform_sample_rate() == 20
    with patch.object(waveform, "get_app", return_value=app_with(5000)):
        assert waveform.configured_waveform_sample_rate() == 1000
    with patch.object(waveform, "get_app", return_value=app_with(None)):
        assert waveform.configured_waveform_sample_rate() == waveform.DEFAULT_SAMPLES_PER_SECOND


def test_thumbnail_grid_rounds_to_four_frames_per_second():
    assert thumbnail.ThumbnailFrameStepForFps(30) == 8
    assert thumbnail.ThumbnailFrameStepForFps(0) == 1
    assert thumbnail.RoundFrameToThumbnailGrid(1, 30) == 1
    assert thumbnail.RoundFrameToThumbnailGrid(6, 30) == 9
    assert thumbnail.RoundFrameToThumbnailGrid(9, 30) == 9
    assert thumbnail.RoundFrameToThumbnailGrid(12, 30) == 9
    assert thumbnail.RoundFrameToThumbnailGrid(13, 30) == 17
    assert thumbnail.RoundFrameToThumbnailGrid(0, 30) == 1


def test_thumbnail_path_for_frame_uses_per_file_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "THUMBNAIL_PATH", str(tmp_path / "thumbs"))
    path = thumbnail.ThumbnailPathForFrame("REC123", 17)
    assert path == str(tmp_path / "thumbs" / "REC123" / "17.png")
    assert thumbnail.ThumbnailPathForFrame("REC123", None).endswith("1.png")


def test_waveform_cache_keeps_rms_format_and_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "recw" * 8, "size": 10, "mtime": 1.0}
    ui = {
        "audio_data": [0.5, 0.25],
        waveform.WAVEFORM_RMS_KEY: [0.3, 0.1],
        waveform.WAVEFORM_FORMAT_KEY: waveform.ABSOLUTE_WAVEFORM_FORMAT,
        waveform.WAVEFORM_RATE_KEY: 200,
        "ignored": None,
    }
    assert save_waveform(fp, ui["audio_data"], extra=ui)
    loaded = load_waveform(fp)
    assert loaded["audio_data"] == [0.5, 0.25]
    assert loaded[waveform.WAVEFORM_RMS_KEY] == [0.3, 0.1]
    assert waveform.waveform_data_format(loaded) == waveform.ABSOLUTE_WAVEFORM_FORMAT
    assert waveform.waveform_sample_rate(loaded) == 200
    assert "ignored" not in loaded


def test_waveform_cache_without_extra_reads_back_as_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "legw" * 8, "size": 10, "mtime": 1.0}
    assert save_waveform(fp, [0.1])
    loaded = load_waveform(fp)
    assert waveform.waveform_data_format(loaded) == waveform.LEGACY_WAVEFORM_FORMAT
    assert waveform.waveform_sample_rate(loaded) == waveform.LEGACY_SAMPLES_PER_SECOND
