"""Beat tracking (classes.speech.beats): the tempo-grid engine ported from zenvi-web's beats.ts.

The analysis is tested on synthetic band energies (no audio, no ffmpeg); the
same grooves as beats.test.ts are then written as WAVs with the ``wave`` module
and run through ffmpeg's band split, ``detect_beats`` and ``detect_beats_tool``
(skipped where ffmpeg is missing).
"""

from __future__ import annotations

import math
import shutil
from fractions import Fraction
from unittest.mock import MagicMock, patch

import pytest

import beat_fakes as bf
from classes.agent_tools.receipt import parse_receipt
from classes.speech import beats
from classes.speech.runtime import CancelToken

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _off_grid(times, period, lead_in=0.0):
    return max(abs(t - (lead_in + round((t - lead_in) / period) * period)) for t in times)


# ---------------------------------------------------------------------------
# The analysis (no ffmpeg)
# ---------------------------------------------------------------------------

def test_finds_the_tempo_a_regular_grid_on_the_kicks_and_the_bars_first_beats():
    result = bf.analyse(bf.groove_energies(120, 12, snare=True, offbeat_hat_gain=0.12, pad=0.02))
    assert 118.5 < result["bpm"] < 121.5
    times = [b["timeSec"] for b in result["beats"]]
    assert len(times) >= 20
    # Every beat sits on a kick (multiples of 0.5 s), within 30 ms.
    assert _off_grid(times, 0.5) < 0.03
    # Downbeats are the heavy kicks: every 2 s.
    assert result["downbeats"] and all(abs(d["timeSec"] / 2 - round(d["timeSec"] / 2)) < 0.02
                                       for d in result["downbeats"])
    assert len(result["beatEnergy"]) == len(times)
    assert len(result["onsets"]) > 10
    assert all(0 <= b["strength"] <= 1 for b in result["beats"])


def test_does_not_fold_an_unusual_tempo_onto_the_prior():
    result = bf.analyse(bf.groove_energies(92, 15, snare=True, offbeat_hat_gain=0.12, pad=0.02))
    assert abs(result["bpm"] - 92) < 1.5


def test_starts_with_the_music_not_in_the_silence_before_it():
    result = bf.analyse(bf.groove_energies(128, 12, lead_in=2.5, snare=True, offbeat_hat_gain=0.12, pad=0.02))
    assert result["beats"][0]["timeSec"] > 2.3


def test_answers_no_beats_for_silence_and_for_audio_shorter_than_two_seconds():
    silent = bf.analyse(bf.groove_energies(120, 5, kick_gain=0))
    assert silent["bpm"] == 0 and silent["beats"] == [] and silent["downbeats"] == []
    assert bf.analyse(bf.groove_energies(120, 1.5, snare=True, pad=0.02))["beats"] == []


def test_a_grid_on_the_off_beats_moves_half_a_beat_onto_the_kicks():
    # Onsets peak on the off-beats (loud hats) while the kick flux is on the beats.
    rate = beats.BEAT_SAMPLE_RATE / beats.HOP
    period = rate / 2  # 120 BPM
    n = int(rate * 10)
    onset, low = [0.0] * n, [0.0] * n
    k = 0
    while k * period < n:
        beat = int(round(k * period))
        off = int(round(k * period + period / 2))
        if beat < n:
            onset[beat], low[beat] = 0.4, 1.0
        if off < n:
            onset[off] = 1.0
        k += 1
    env = beats.Envelopes(onset=onset, low=low, low_level=[1.0] * n, rms=[0.1] * n, frame_rate=rate)
    result = beats.beats_from_envelopes(env)
    assert abs(result["bpm"] - 120) < 1.5
    times = [b["timeSec"] for b in result["beats"]]
    # On the kicks (beat frames, timed where their onset sits), not between them.
    offset = beats.ONSET_OFFSET / beats.BEAT_SAMPLE_RATE
    assert _off_grid([t - offset for t in times], 0.5) < 0.02


def test_beat_energy_follows_the_loudness_of_the_music():
    result = bf.analyse(bf.groove_energies(120, 30, quiet_until=8))
    times = [b["timeSec"] for b in result["beats"]]
    quiet = [e for t, e in zip(times, result["beatEnergy"]) if t < 7.5]
    loud = [e for t, e in zip(times, result["beatEnergy"]) if t > 8.5]
    assert max(result["beatEnergy"]) == 1.0
    assert max(quiet) < 0.5 < min(loud)


def test_band_weights_count_the_web_trackers_bands():
    weights = beats.band_weights()
    assert len(weights) == len(beats.BAND_SPLITS_HZ) + 1
    assert abs(sum(weights) - 36) < 1e-6
    # beats.ts's six lowest bands (one FFT bin each, 21-150 Hz) are the kick band.
    assert abs(weights[0] - 6) < 0.1


def test_the_tempo_engine_falls_back_to_energy_peaks_when_ffmpeg_cannot_split(tmp_path):
    wav = bf.write_wav(tmp_path / "t.wav", bf.drum_track(120, 3))
    with patch.object(beats, "band_energies", side_effect=RuntimeError("No such filter: 'acrossover'")):
        result = beats.TempoBeatEngine().detect(wav, token=CancelToken())
    assert result["beats"] and result["bpm"] > 0  # EnergyBeatEngine's peaks


def test_detect_beats_analyses_at_most_ten_minutes_and_removes_its_wav(tmp_path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"")
    seen = {}

    def extract(path, **kwargs):
        seen.update(kwargs, path=path)
        return str(wav), ""

    engine = MagicMock()
    engine.detect.return_value = {"beats": [], "downbeats": [], "bpm": 0.0}
    beats.set_beat_factory(lambda: engine)
    try:
        with patch.object(beats, "extract_mono_16k_wav", side_effect=extract):
            beats.detect_beats("/music/song.mp3")
    finally:
        beats.reset_beat_factory()
    assert seen["duration_sec"] == beats.MAX_SECONDS and seen["path"] == "/music/song.mp3"
    assert not wav.exists()
    assert beats._factory is beats.TempoBeatEngine


def test_detect_beats_reports_audio_it_cannot_read():
    with patch.object(beats, "extract_mono_16k_wav", return_value=("", "no audio track in this media file")):
        with pytest.raises(RuntimeError, match="no audio"):
            beats.detect_beats("/media/silent.mp4")


# ---------------------------------------------------------------------------
# Real audio through ffmpeg (beats.test.ts's grooves as WAV files)
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_ffmpeg_splits_the_bands_low_to_high(tmp_path):
    rate = bf.RATE
    for freq, band in ((80, 0), (400, 1), (2000, 2), (6000, 3)):
        path = bf.write_wav(tmp_path / f"{freq}.wav", [0.5 * math.sin(2 * math.pi * freq * i / rate)
                                                      for i in range(rate * 2)])
        energies = beats.band_energies(path)
        totals = [sum(h) for h in energies.hops]
        assert max(range(4), key=totals.__getitem__) == band, (freq, totals)
        # A 0.5 sine carries 0.125 per sample. The crossover keeps most of it (its bands add up in
        # amplitude, not power, so a tone 0.6 octave above a split keeps ~83%); a channel mix-up would not.
        assert 0.75 < sum(totals) / energies.samples / 0.125 < 1.05, (freq, totals)
        assert abs(energies.samples / energies.sample_rate - 2.0) < 0.05


@needs_ffmpeg
def test_detect_beats_tracks_drum_grooves(tmp_path):
    groove = beats.detect_beats(bf.write_wav(tmp_path / "120.wav", bf.drum_track(120, 12)))
    assert 118.5 < groove["bpm"] < 121.5
    times = [b["timeSec"] for b in groove["beats"]]
    assert len(times) >= 20 and _off_grid(times, 0.5) < 0.03
    assert all(abs(d["timeSec"] / 2 - round(d["timeSec"] / 2)) < 0.02 for d in groove["downbeats"])
    assert len(groove["beatEnergy"]) == len(times) and len(groove["onsets"]) > 10
    assert abs(groove["duration"] - 12) < 0.05

    slow = beats.detect_beats(bf.write_wav(tmp_path / "92.wav", bf.drum_track(92, 15)))
    assert abs(slow["bpm"] - 92) < 1.5

    late = beats.detect_beats(bf.write_wav(tmp_path / "128.wav", bf.drum_track(128, 12, lead_in=2.5)))
    assert late["beats"][0]["timeSec"] > 2.3

    silence = beats.detect_beats(bf.write_wav(tmp_path / "silence.wav", [0.0] * (bf.RATE * 5)))
    assert silence["bpm"] == 0 and silence["beats"] == []
    assert beats.detect_beats(bf.write_wav(tmp_path / "short.wav", bf.drum_track(120, 1.5)))["beats"] == []


@needs_ffmpeg
def test_detect_beats_tool_returns_its_receipt_shape_with_a_tempo_grid(tmp_path):
    wav = bf.write_wav(tmp_path / "song.wav", bf.drum_track(120, 8))
    project = {"fps": {"num": 30, "den": 1}, "clips": []}
    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: project.get(k, d)
    music = MagicMock()
    music.data = {"path": wav}
    with patch("classes.app.get_app", return_value=app), \
            patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
            patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
            patch("classes.query.File.get", return_value=music):
        from classes.agent_tools.speech_extra import detect_beats_tool_handler
        receipt = parse_receipt(detect_beats_tool_handler(fileId="SONG"))
    assert receipt["status"] == "applied"
    data = receipt["data"]
    assert abs(data["bpm"] - 120) < 2 and data["provider"] == "local" and data["fileId"] == "SONG"
    assert data["beats"] and set(data["beats"][0]) == {"timeSec", "frame", "strength"}
    first = data["beats"][0]
    assert first["frame"] == round(first["timeSec"] * 30)
    assert data["downbeats"] and set(data["downbeats"][0]) == {"timeSec", "frame"}
    assert receipt["undoSteps"] == 0


def test_a_stalled_ffmpeg_band_split_is_killed_on_cancel(monkeypatch):
    """CodeRabbit #345: the pipe read blocked while ffmpeg wrote nothing, holding the inference slot."""
    import subprocess
    import sys
    import threading
    import time
    from classes import ffmpeg_cli

    def stalled(cmd, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(ffmpeg_cli, "popen_ffmpeg", stalled)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        beats.band_energies("in.wav", token=token)
    assert time.monotonic() - began < 10
