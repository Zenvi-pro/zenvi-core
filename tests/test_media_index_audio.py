"""Audio facts measured from signals whose answers are known: a tone at a set level, noise, a
silent gap, and click tracks at known tempos and beat times."""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import media_fixtures as mf  # noqa: E402
from classes.media_index import audio as au  # noqa: E402
from classes.media_index.probe import probe_media  # noqa: E402

SR = 44100


def write_wav(path, x, sr=SR, channels=1):
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767).astype("<i2")
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1).ravel()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return str(path)


def tone(seconds, hz=440.0, peak_db=-20.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (10 ** (peak_db / 20.0)) * np.sin(2 * np.pi * hz * t)


def clicks(seconds, bpm, sr=SR, offset=0.25, noise_db=-55.0, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 10 ** (noise_db / 20.0), int(seconds * sr))
    period = 60.0 / bpm
    times = []
    t = offset
    while t < seconds - 0.1:
        n0 = int(t * sr)
        n = int(0.025 * sr)
        env = np.exp(-np.arange(n) / (0.006 * sr))
        burst = (rng.normal(0, 1, n) * 0.6 + np.sin(2 * np.pi * 1800 * np.arange(n) / sr) * 0.5) * env * 0.7
        x[n0:n0 + n] += burst[: len(x) - n0]
        times.append(round(t, 3))
        t += period
    return x, times


def analyse(path, **kw):
    mf.need_ffmpeg()
    return au.analyze_audio(path, probe_media(path), **kw)


# ============================ pure helpers ============================
def test_ebur128_summary_is_parsed_and_silence_has_no_loudness():
    text = ("[Parsed_ebur128_0 @ 0x1] Summary:\n\n  Integrated loudness:\n    I:         -23.4 LUFS\n"
            "    Threshold: -33.4 LUFS\n\n  Loudness range:\n    LRA:         6.1 LU\n\n  True peak:\n    Peak:       -1.2 dBFS\n")
    assert au.parse_ebur128(text) == {"integrated_lufs": -23.4, "lra": 6.1, "true_peak_db": -1.2}
    silent = text.replace("-23.4 LUFS", "-inf LUFS").replace("-1.2 dBFS", "-inf dBFS")
    got = au.parse_ebur128(silent)
    assert got["integrated_lufs"] is None and got["true_peak_db"] is None
    assert au.parse_ebur128("no summary here") is None


def test_to_db_floors_silence():
    assert au.to_db(1.0) == pytest.approx(0.0)
    assert au.to_db(0.1) == pytest.approx(-20.0, abs=1e-6)
    assert au.to_db(0.0) == -120.0


def test_silence_ranges_need_the_minimum_length():
    hz = au.FRAME_HZ
    db = np.full(int(10 * hz), -20.0)
    db[int(2 * hz): int(3 * hz)] = -80.0        # 1.0 s: counts
    db[int(5 * hz): int(5.3 * hz)] = -80.0      # 0.3 s: too short
    db[int(9 * hz):] = -80.0                    # runs to the end
    got = au.silence_ranges(db, hz)
    assert len(got) == 2
    assert got[0][0] == pytest.approx(2.0, abs=0.05) and got[0][1] == pytest.approx(3.0, abs=0.05)
    assert got[1][0] == pytest.approx(9.0, abs=0.05)


def test_a_flat_onset_envelope_has_no_tempo():
    assert au.estimate_tempo(np.ones(1000, np.float32)) == (None, 0.0)
    assert au.estimate_tempo(np.zeros(10, np.float32)) == (None, 0.0)


# ============================ level, spectrum, silence ============================
def test_a_tone_at_a_known_level_reads_as_that_level_pitch_and_purity(tmp_path):
    res = analyse(write_wav(tmp_path / "tone.wav", tone(10, 440.0, -20.0)))
    w = res["windows"][1]
    assert w["peak_db"] == pytest.approx(-20.0, abs=0.6)
    assert w["rms_db"] == pytest.approx(-23.0, abs=0.6)        # a sine's RMS is 3 dB under its peak
    assert w["centroid_hz"] == pytest.approx(440.0, abs=40.0)
    assert w["flatness"] < 0.05 and w["silence_ratio"] == 0.0
    assert res["loudness"]["integrated_lufs"] == pytest.approx(-23.0, abs=0.7)
    assert res["tempo"] is None, "a steady tone has no rhythm"
    assert res["silence_ranges"] == []
    assert res["kind"] == "measured" and res["has_audio"] is True


def test_noise_is_flat_and_bright_where_a_tone_is_not(tmp_path):
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 10 ** (-23 / 20.0), int(8 * SR))
    res = analyse(write_wav(tmp_path / "noise.wav", noise))
    w = res["windows"][0]
    assert w["flatness"] > 0.4 and w["centroid_hz"] > 3000.0
    # Levels are measured over 0-11 kHz (the analysis rate is 22.05 kHz), so white noise at -23 dBFS
    # reads 3 dB lower: half its power sits above 11 kHz. Loudness (LUFS) uses the native rate.
    assert w["rms_db"] == pytest.approx(-26.0, abs=1.0)
    # K-weighting lifts the highs, so bright noise measures louder in LUFS than its flat RMS.
    assert -22.0 <= res["loudness"]["integrated_lufs"] <= -18.0


def test_a_silent_gap_is_found_with_its_edges(tmp_path):
    x = tone(12, 440.0, -20.0)
    x[int(2.0 * SR): int(9.0 * SR)] = 0.0
    res = analyse(write_wav(tmp_path / "gap.wav", x))
    assert len(res["silence_ranges"]) == 1
    s, e = res["silence_ranges"][0]
    assert s == pytest.approx(2.0, abs=0.15) and e == pytest.approx(9.0, abs=0.15)
    in_gap = [w for w in res["windows"] if w["start"] >= 2.0 and w["end"] <= 9.0]   # windows are 5 s long
    assert in_gap and all(w["silence_ratio"] > 0.9 for w in in_gap)


def test_the_energy_envelope_follows_the_signal(tmp_path):
    x = np.concatenate([tone(4, 440.0, -40.0), tone(4, 440.0, -10.0)])
    res = analyse(write_wav(tmp_path / "step.wav", x))
    env = res["envelope"]
    assert env["hz"] == 2.0 and len(env["db"]) == pytest.approx(16, abs=2)
    quiet, loud = np.mean(env["db"][:6]), np.mean(env["db"][-6:])
    assert loud - quiet == pytest.approx(30.0, abs=2.0)


def test_stereo_at_another_rate_is_read_correctly(tmp_path):
    res = analyse(write_wav(tmp_path / "st.wav", tone(6, 1000.0, -20.0, sr=48000), sr=48000, channels=2))
    assert res["native"]["sample_rate"] == 48000 and res["native"]["channels"] == 2
    assert res["windows"][0]["centroid_hz"] == pytest.approx(1000.0, abs=60.0)


# ============================ tempo and beats ============================
@pytest.mark.parametrize("bpm", [90, 100, 128, 140])
def test_tempo_and_beat_times_of_a_click_track(tmp_path, bpm):
    x, truth = clicks(30, bpm)
    res = analyse(write_wav(tmp_path / f"c{bpm}.wav", x))
    tempo = res["tempo"]
    assert tempo is not None, res["tempo_confidence"]
    assert tempo["bpm"] == pytest.approx(bpm, abs=1.5)
    assert tempo["confidence"] >= au.TEMPO_MIN_CONFIDENCE
    beats = np.array(tempo["beats"])
    signed = np.array([beats[np.argmin(np.abs(beats - t))] - t for t in truth[2:-2]])
    assert abs(np.median(signed)) <= 0.012, f"beats are systematically {np.median(signed) * 1000:+.0f} ms off"
    assert np.mean(np.abs(signed) <= 0.03) >= 0.9, "most beats must land within 30 ms of the real ones"
    assert 0.8 * len(truth) <= len(beats) <= 1.2 * len(truth)


def test_irregular_hits_are_not_given_a_tempo(tmp_path):
    rng = np.random.default_rng(9)
    x = rng.normal(0, 10 ** (-55 / 20.0), 30 * SR)
    t = 0.3
    while t < 29.5:
        n0, n = int(t * SR), int(0.025 * SR)
        x[n0:n0 + n] += rng.normal(0, 0.3, n) * np.exp(-np.arange(n) / (0.006 * SR))
        t += float(rng.uniform(0.15, 1.4))   # no steady spacing
    res = analyse(write_wav(tmp_path / "irregular.wav", x))
    assert res["tempo"] is None


def test_steady_noise_has_no_tempo(tmp_path):
    rng = np.random.default_rng(4)
    res = analyse(write_wav(tmp_path / "hiss.wav", rng.normal(0, 0.05, 20 * SR)))
    assert res["tempo"] is None


# ============================ streaming, edges, control ============================
def test_block_size_does_not_change_the_answer(tmp_path, monkeypatch):
    x, _ = clicks(24, 120)
    x[int(5 * SR): int(7 * SR)] = 0.0
    path = write_wav(tmp_path / "blocks.wav", x)
    whole = analyse(path)
    monkeypatch.setattr(au, "BLOCK_SECONDS", 2.7)
    chunked = analyse(path)
    assert chunked["tempo"]["bpm"] == pytest.approx(whole["tempo"]["bpm"], abs=0.05)
    assert chunked["silence_ranges"] == whole["silence_ranges"]
    assert len(chunked["windows"]) == len(whole["windows"])
    for a, b in zip(chunked["windows"], whole["windows"]):
        assert a["rms_db"] == pytest.approx(b["rms_db"], abs=0.05) and a["centroid_hz"] == pytest.approx(b["centroid_hz"], abs=2.0)
    assert chunked["envelope"]["db"] == pytest.approx(whole["envelope"]["db"], abs=0.2)


def test_a_file_without_audio_has_no_audio_facts():
    d = mf.need_ffmpeg()
    fixture = Path(__file__).parent / "fixtures" / "media" / "h264_720p30_2s.mp4"
    probe = probe_media(str(fixture))
    if probe.get("has_audio"):
        pytest.skip("the fixture has audio")
    assert au.analyze_audio(str(fixture), probe) is None and d


def test_a_very_short_clip_does_not_crash(tmp_path):
    res = analyse(write_wav(tmp_path / "blip.wav", tone(0.02, 440.0)))
    assert res is not None and res["windows"] == [] and res["tempo"] is None


def test_progress_runs_forward_and_finishes(tmp_path):
    seen = []
    analyse(write_wav(tmp_path / "p.wav", tone(40, 300.0)), on_progress=seen.append)
    assert seen and seen[-1] == 1.0 and all(a <= b for a, b in zip(seen, seen[1:]))


def test_cancelling_stops_the_decode(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "BLOCK_SECONDS", 1.0)
    path = write_wav(tmp_path / "long.wav", tone(30, 300.0))
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(au.Cancelled):
        analyse(path, should_cancel=cancel)
    assert calls["n"] <= 6


# ============================ trouble: clipping, rumble, noise floor ============================
def test_a_clipped_signal_reports_clipping_and_a_clean_one_does_not(tmp_path):
    loud = analyse(write_wav(tmp_path / "clip.wav", tone(6, 440.0, 0.0) * 3.0))
    clean = analyse(write_wav(tmp_path / "clean.wav", tone(6, 440.0, -20.0)))
    assert all(w["clipped_ratio"] > 0.9 for w in loud["windows"])
    assert all(w["clipped_ratio"] == 0.0 for w in clean["windows"])


def test_low_frequency_rumble_is_told_from_content(tmp_path):
    rng = np.random.default_rng(1)
    rumble = analyse(write_wav(tmp_path / "wind.wav", tone(6, 60.0, -10.0) + rng.normal(0, 0.01, 6 * SR)))
    voice_band = analyse(write_wav(tmp_path / "mid.wav", tone(6, 1000.0, -10.0)))
    assert all(w["rumble_ratio"] > 0.55 for w in rumble["windows"])
    assert all(w["rumble_ratio"] < 0.05 for w in voice_band["windows"])


def test_the_quiet_floor_and_dynamic_range_are_measured(tmp_path):
    rng = np.random.default_rng(2)
    x = np.concatenate([rng.normal(0, 10 ** (-50 / 20), 10 * SR), tone(10, 440.0, -10.0)])
    out = analyse(write_wav(tmp_path / "range.wav", x))
    assert out["noise_floor_db"] == pytest.approx(-53, abs=2.5)       # noise at -50 dBFS reads about 3 dB lower in the 11 kHz analysis band
    assert out["dynamic_range_db"] > 30


def test_an_empty_or_silent_file_has_no_floor(tmp_path):
    out = analyse(write_wav(tmp_path / "silence.wav", np.zeros(4 * SR)))
    assert out["noise_floor_db"] is None and out["dynamic_range_db"] is None


# ============================ the shape of a piece of music ============================
def three_part_song():
    """12 s quiet pad, 12 s loud groove at 120 BPM, 12 s quiet pad."""
    groove, _ = clicks(12, 120, noise_db=-50)
    t = np.arange(12 * SR) / SR
    return np.concatenate([tone(12, 200.0, -38.0), groove * 1.4 + tone(12, 220.0, -14.0) + 0.15 * np.sin(2 * np.pi * 55 * t),
                           tone(12, 200.0, -38.0)])


def test_sections_and_energy_arc_follow_quiet_loud_quiet(tmp_path):
    music = analyse(write_wav(tmp_path / "song.wav", three_part_song()))["music"]
    sections = music["sections"]
    assert [s["label"] for s in sections] == ["intro", "peak", "outro"]
    assert sections[0]["end"] == pytest.approx(12.0, abs=2.0) and sections[1]["end"] == pytest.approx(24.0, abs=2.0)
    assert sections[-1]["end"] == pytest.approx(36.0, abs=0.1)
    assert sections[1]["energy"] > 0.8 > 0.2 > sections[0]["energy"]
    arc = music["arc"]
    assert len(arc) == 9 and arc[0] < 0.1 and arc[4] > 0.9 and arc[-1] < 0.1
    assert music["brightness_hz"] is not None


def test_beats_stop_where_the_groove_stops(tmp_path):
    out = analyse(write_wav(tmp_path / "song.wav", three_part_song()))
    beats = out["tempo"]["beats"]
    assert beats[0] == pytest.approx(12.25, abs=0.2) and beats[-1] == pytest.approx(23.75, abs=0.2)
    assert len(beats) == pytest.approx(24, abs=1)


def accented_clicks(seconds, bpm, accent_at, sr=SR):
    """Clicks where every 4th is loud; *accent_at* picks which click of the bar is the loud one."""
    rng = np.random.default_rng(4)
    x = rng.normal(0, 10 ** (-55 / 20.0), int(seconds * sr))
    period, t, k, loud = 60.0 / bpm, 0.25, 0, []
    while t < seconds - 0.1:
        n0, n = int(t * sr), int(0.025 * sr)
        env = np.exp(-np.arange(n) / (0.006 * sr))
        gain = 1.0 if k % 4 == accent_at else 0.4
        x[n0:n0 + n] += (rng.normal(0, 1, n) * 0.6 + np.sin(2 * np.pi * 1800 * np.arange(n) / sr) * 0.5) * env * 0.7 * gain
        if k % 4 == accent_at:
            loud.append(round(t, 3))
        t += period
        k += 1
    return x, loud


@pytest.mark.parametrize("accent_at", [0, 1, 2])
def test_downbeats_land_on_the_accented_beat_whichever_beat_of_the_bar_it_is(tmp_path, accent_at):
    x, loud = accented_clicks(40, 120, accent_at)
    music = analyse(write_wav(tmp_path / f"acc{accent_at}.wav", x))["music"]
    downbeats = music["downbeats"]
    assert len(downbeats) >= 8
    near = [min(abs(d - t) for t in loud) for d in downbeats]
    assert max(near) < 0.08, near
    assert music["phrase_points"] == downbeats[::4] and len(music["phrase_points"]) >= 2


def test_audio_without_a_rhythm_has_no_bars_but_still_has_an_arc(tmp_path):
    out = analyse(write_wav(tmp_path / "tone.wav", tone(20, 330.0, -20.0)))
    assert out["tempo"] is None and out["music"]["downbeats"] == [] and out["music"]["phrase_points"] == []
    assert len(out["music"]["arc"]) == 5 and out["music"]["sections"] == [] or out["music"]["sections"][0]["label"] in ("steady", "intro")


def test_a_short_clip_has_no_sections(tmp_path):
    out = analyse(write_wav(tmp_path / "short.wav", tone(10, 330.0, -20.0)))
    assert out["music"]["sections"] == [] and len(out["music"]["arc"]) == 3
