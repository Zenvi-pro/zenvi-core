"""Portable beat / onset detection (energy peaks; optional ONNX later)."""

from __future__ import annotations

import logging
import math
from typing import Callable, Optional, Protocol

from classes.speech.audio_extract import extract_mono_16k_wav
from classes.speech.runtime import CancelToken, inference_slot
from classes.speech.vad import _read_pcm16_mono

log = logging.getLogger("speech.beats")


class BeatEngine(Protocol):
    def detect(self, wav_path: str, *, token: CancelToken) -> dict:
        """Return {beats: [{timeSec, strength}], bpm, downbeats: [...]}."""
        ...


class EnergyBeatEngine:
    """Lightweight onset peaks — good enough for music-cut scaffolding in CI."""

    def detect(self, wav_path: str, *, token: CancelToken) -> dict:
        samples, rate = _read_pcm16_mono(wav_path)
        if len(samples) < rate // 4:
            return {"beats": [], "downbeats": [], "bpm": 0.0}
        frame = max(1, rate // 100)  # 10 ms
        hop = frame
        energies = []
        for i in range(0, len(samples) - frame, hop):
            token.raise_if_cancelled()
            chunk = samples[i : i + frame]
            energies.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)))
        if len(energies) < 4:
            return {"beats": [], "downbeats": [], "bpm": 0.0}
        # Spectral flux proxy: positive energy delta
        flux = [0.0]
        for i in range(1, len(energies)):
            flux.append(max(0.0, energies[i] - energies[i - 1]))
        # Adaptive threshold
        mean = sum(flux) / len(flux)
        thr = mean * 1.6
        min_sep = int(0.25 * rate / hop)  # 250 ms
        peaks: list[tuple[int, float]] = []
        last = -min_sep
        for i, v in enumerate(flux):
            if v >= thr and i - last >= min_sep:
                peaks.append((i, v))
                last = i
        if not peaks:
            return {"beats": [], "downbeats": [], "bpm": 0.0}
        beats = []
        for idx, strength in peaks:
            t = idx * hop / rate
            beats.append({"timeSec": t, "strength": float(strength)})
        # BPM from median inter-beat interval
        intervals = [
            beats[i + 1]["timeSec"] - beats[i]["timeSec"]
            for i in range(len(beats) - 1)
            if beats[i + 1]["timeSec"] > beats[i]["timeSec"]
        ]
        bpm = 0.0
        if intervals:
            intervals.sort()
            med = intervals[len(intervals) // 2]
            if med > 1e-6:
                bpm = 60.0 / med
                # Fold into reasonable range
                while bpm < 60:
                    bpm *= 2
                while bpm > 180:
                    bpm /= 2
        # Downbeats: every 4th beat
        downbeats = [b for i, b in enumerate(beats) if i % 4 == 0]
        return {"beats": beats, "downbeats": downbeats, "bpm": float(bpm)}


_factory: Callable[[], BeatEngine] = EnergyBeatEngine


def set_beat_factory(factory: Callable[[], BeatEngine]) -> None:
    global _factory
    _factory = factory


def reset_beat_factory() -> None:
    global _factory
    _factory = EnergyBeatEngine


def detect_beats(
    media_path: str,
    *,
    token: Optional[CancelToken] = None,
) -> dict:
    import os

    with inference_slot(token) as tok:
        wav, err = extract_mono_16k_wav(media_path)
        if err:
            raise RuntimeError(err)
        try:
            return _factory().detect(wav, token=tok)
        finally:
            try:
                if os.path.isfile(wav):
                    os.remove(wav)
            except OSError:
                pass
