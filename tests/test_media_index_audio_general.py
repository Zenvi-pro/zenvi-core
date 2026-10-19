"""Audio facts that do not depend on one song: sections need a steady rhythm, nothing is cut into bars of four beats,
phrase points are where the sections change, and a clean 90 BPM track is not pulled toward 120."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from test_media_index_audio import SR, analyse, clicks, three_part_song, write_wav  # noqa: E402


def speech_like(seconds, seed=1):
    """Short noise bursts at irregular gaps, grouped in phrases that are loud or quiet."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 10 ** (-55 / 20.0), int(seconds * SR))
    t = 0.2
    while t < seconds - 0.3:
        level = rng.choice([0.05, 0.2, 0.5])
        phrase_end = t + rng.uniform(2.0, 6.0)
        while t < phrase_end and t < seconds - 0.3:
            d = rng.uniform(0.12, 0.3)
            n0, m = int(t * SR), int(d * SR)
            x[n0:n0 + m] += rng.normal(0, 1, m) * np.hanning(m) * level
            t += d + rng.uniform(0.15, 1.4)
        t += rng.uniform(0.3, 0.8)
    return x


def test_speech_like_audio_with_no_steady_rhythm_gets_no_sections(tmp_path):
    out = analyse(write_wav(tmp_path / "speech.wav", speech_like(30)))
    assert out["tempo"] is None, "irregular bursts have no tempo, so they are not music"
    assert out["music"]["sections"] == []


def test_a_song_with_a_steady_groove_still_gets_its_sections(tmp_path):
    out = analyse(write_wav(tmp_path / "song.wav", three_part_song()))
    assert out["tempo"] is not None
    assert [s["label"] for s in out["music"]["sections"]] == ["intro", "peak", "outro"]


def test_a_click_track_gets_no_downbeats_because_no_four_beat_bar_is_assumed(tmp_path):
    out = analyse(write_wav(tmp_path / "click.wav", clicks(30, 120)[0]))
    assert out["tempo"] is not None and len(out["tempo"]["beats"]) >= 40, "the beats themselves are still measured"
    assert not out["music"].get("downbeats"), "every fourth beat is not a bar"


def test_phrase_points_are_where_the_sections_change(tmp_path):
    music = analyse(write_wav(tmp_path / "song.wav", three_part_song()))["music"]
    starts = [s["start"] for s in music["sections"][1:]]
    assert len(starts) == 2 and music["phrase_points"] == starts


def test_a_clean_90_bpm_click_track_is_not_pulled_toward_120(tmp_path):
    out = analyse(write_wav(tmp_path / "c90.wav", clicks(30, 90)[0]))
    assert out["tempo"] is not None
    assert out["tempo"]["bpm"] == pytest.approx(90.0, abs=1.0)
