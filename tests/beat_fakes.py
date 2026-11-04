"""Synthetic music for the beat tracker and build_reel_tool tests.

``drum_track`` is zenvi-web's beats.test.ts groove (a kick on every beat,
heavier on the first of each bar, a noise snare on 2 and 4, hats on every
eighth, over a quiet pad); ``write_wav`` stores it with the ``wave`` module for
the tests that run ffmpeg. ``groove_energies`` gives the same kind of music as
per-hop band energies (what ffmpeg's band split hands the analysis), so the
tracker runs without audio or ffmpeg.
"""

from __future__ import annotations

import array
import math
import sys
import wave
from typing import Callable, List, Sequence

from classes.speech import beats

RATE = 16000  # what detect_beats extracts; everything above 8 kHz is gone anyway


def _noise(seed: int) -> Callable[[], float]:
    state = [seed]

    def rand() -> float:
        state[0] = (state[0] * 1103515245 + 12345) % 2147483648
        return state[0] / 1073741824 - 1

    return rand


def drum_track(bpm: float, seconds: float, lead_in: float = 0.0, rate: int = RATE) -> List[float]:
    """beats.test.ts ``track()``: kick, snare on 2 and 4, eighth-note hats, a quiet pad.

    Each hit lasts up to 0.3 s as there, but stops once it has decayed below 1e-4
    of its level (the hats after ~0.1 s): the same music, generated faster.
    """
    out = [0.02 * math.sin(2 * math.pi * 220 * i / rate) for i in range(round(seconds * rate))]
    beat = 60 / bpm
    rand = _noise(7)
    # The kick is the same every time; only its level changes.
    kick = [math.exp(-k / (0.08 * rate)) * math.sin(2 * math.pi * (50 + 80 * math.exp(-k / (0.02 * rate))) * k / rate)
            for k in range(int(rate * 0.3))]

    def add(at: float, gain: float, shape: Sequence[float]) -> None:
        start = round(at * rate)
        for k in range(min(len(shape), len(out) - start)):
            out[start + k] += gain * shape[k]

    def burst(decay: float) -> List[float]:
        return [math.exp(-k / (decay * rate)) * rand() for k in range(min(int(rate * 0.3), int(10 * decay * rate)))]

    n = 0
    while lead_in + n * beat < seconds:
        t = lead_in + n * beat
        add(t, 0.9 if n % 4 == 0 else 0.6, kick)
        if n % 2 == 1:
            add(t, 0.35, burst(0.05))
        add(t, 0.08, burst(0.008))
        add(t + beat / 2, 0.12, burst(0.01))
        n += 1
    if lead_in > 0:
        for i in range(max(0, round(lead_in * rate) - 1)):
            out[i] = 0.0
    return out


def write_wav(path, samples: Sequence[float], rate: int = RATE) -> str:
    """16-bit mono PCM WAV (little-endian, as the format requires)."""
    pcm = array.array("h", (max(-32768, min(32767, int(round(s * 32767)))) for s in samples))
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return str(path)


def _decay_energy(gain: float, decay: float, first: int, last: int, rate: int) -> float:
    """Sum of (gain * exp(-k / (decay * rate)))^2 / 2 over samples first..last-1 after the hit (a sine's power)."""
    if last <= first:
        return 0.0
    r = math.exp(-2 / (decay * rate))
    return 0.5 * gain * gain * (r ** first - r ** last) / (1 - r)


def groove_energies(bpm: float, seconds: float, *, lead_in: float = 0.0, quiet_until: float = 0.0,
                    kick_gain: float = 0.9, offbeat_hat_gain: float = 0.0, snare: bool = False,
                    pad: float = 0.0) -> beats.BandEnergies:
    """Per-hop band energies of a groove at *bpm*: kicks (heavier on each bar's first beat) in the
    low band, optional off-beat hats in the top band and a snare on 2 and 4 across the upper bands.
    Hits before *quiet_until* seconds play at a fifth of their level."""
    rate, hop = beats.BEAT_SAMPLE_RATE, beats.HOP
    count = int(seconds * rate) // hop
    bands = len(beats.BAND_SPLITS_HZ) + 1
    hops = [[0.0] * count for _ in range(bands)]
    # A quiet pad in the low mids keeps the levels off zero, like beats.test.ts's 220 Hz sine.
    for j in range(count):
        hops[1][j] += 0.5 * pad * pad * hop
    period = 60 / bpm
    noise_share = (0.02, 0.08, 0.4, 0.5)  # white noise up to 8 kHz, by bandwidth

    def hit(at: float, band_gains: Sequence[float], gain: float, decay: float) -> None:
        if at >= seconds:
            return
        level = gain * (0.2 if at < quiet_until else 1.0)
        start = int(round(at * rate))
        for j in range(start // hop, min(count, (start + int(rate * 0.3)) // hop + 1)):
            first, last = max(0, j * hop - start), (j + 1) * hop - start
            energy = _decay_energy(level, decay, first, last, rate)
            for b, share in enumerate(band_gains):
                hops[b][j] += share * energy

    n = 0
    while lead_in + n * period < seconds:
        t = lead_in + n * period
        hit(t, (1.0, 0.0, 0.0, 0.0), kick_gain * (1.0 if n % 4 == 0 else 0.65), 0.07)
        if snare and n % 2 == 1:
            hit(t, noise_share, 0.35 * math.sqrt(2 / 3), 0.05)  # uniform noise: power 1/3
        if offbeat_hat_gain:
            hit(t + period / 2, (0.0, 0.0, 0.2, 0.8), offbeat_hat_gain, 0.01)
        n += 1
    return beats.BandEnergies(hops=hops, samples=count * hop)


def analyse(energies: beats.BandEnergies) -> dict:
    """What TempoBeatEngine.detect answers for these energies."""
    result = beats.beats_from_envelopes(beats.envelopes(energies))
    result["duration"] = energies.samples / energies.sample_rate
    return result
