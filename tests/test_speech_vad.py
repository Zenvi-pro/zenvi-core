"""EnergyVad on talking-head speech: only the real pauses read as silence."""

from __future__ import annotations

import math
import random
import struct
import wave

import pytest

from classes.speech.runtime import CancelToken
from classes.speech.vad import EnergyVad, silence_ranges_from_speech

RATE = 16000
# (kind, seconds): two real pauses inside continuous speech.
LAYOUT = [("speech", 2.0), ("pause", 0.6), ("speech", 2.5), ("pause", 1.0), ("speech", 1.5)]
PAUSES = [(2.0, 2.6), (5.4, 6.1)]
SYLLABLE = (5.25, 5.33)  # a short "ha" just inside the second pause: speech, not silence


def _write_talking_head(path, layout=LAYOUT):
    """Room noise at about -50 dB; syllables swing -33..-13 dB; one plosive near 0 dB."""
    rng = random.Random(7)
    out, t0 = [], 0.0
    for kind, secs in layout:
        for i in range(int(secs * RATE)):
            t = t0 + i / RATE
            x = rng.gauss(0.0, 0.003)
            env = 0.0
            if kind == "flat":  # compressed voice, no dips: about -23 dB throughout
                env = 0.1
            elif kind == "speech":
                env = 0.03 + 0.27 * abs(math.sin(2 * math.pi * 3.0 * t))
                if 1.0 <= t < 1.03:
                    env = 0.95
            elif SYLLABLE[0] <= t < SYLLABLE[1]:
                env = 0.2
            if env:
                x += env * math.sin(2 * math.pi * 180.0 * t)
            out.append(max(-32768, min(32767, int(x * 32767))))
        t0 += secs
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(struct.pack("<" + "h" * len(out), *out))
    return t0


def test_energy_vad_keeps_speech_and_finds_only_real_pauses(tmp_path):
    wav = tmp_path / "talk.wav"
    duration = _write_talking_head(wav)
    speech = EnergyVad().speech_windows(
        str(wav), token=CancelToken(), min_speech_sec=0.12, min_silence_sec=0.4,
    )
    gaps = silence_ranges_from_speech(
        speech, duration=duration, min_pause_sec=0.4, pad_sec=0.08,
    )
    assert len(gaps) == 2, gaps
    for (a, b), (pa, pb) in zip(gaps, PAUSES):
        assert pa - 0.05 <= a and b <= pb + 0.05, (gaps, PAUSES)
        assert (b - a) >= (pb - pa) - 0.3, (gaps, PAUSES)
    assert all(b <= SYLLABLE[0] or a >= SYLLABLE[1] for a, b in gaps), gaps


@pytest.mark.parametrize("layout", [
    [("speech", 4.0)],
    # No room floor to measure: a soft, flat sentence is the quietest 10%.
    [("flat", 6.0), ("speech", 2.0)],
])
def test_energy_vad_continuous_speech_has_no_silence(tmp_path, layout):
    wav = tmp_path / "talk.wav"
    duration = _write_talking_head(wav, layout)
    speech = EnergyVad().speech_windows(
        str(wav), token=CancelToken(), min_speech_sec=0.12, min_silence_sec=0.4,
    )
    assert silence_ranges_from_speech(
        speech, duration=duration, min_pause_sec=0.4, pad_sec=0.08,
    ) == []


def test_energy_vad_digital_silence_has_no_speech(tmp_path):
    wav = tmp_path / "silent.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(b"\0\0" * RATE * 2)
    assert EnergyVad().speech_windows(
        str(wav), token=CancelToken(), min_speech_sec=0.12, min_silence_sec=0.4,
    ) == []
