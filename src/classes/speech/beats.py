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


def _from_media_index(media_path: str) -> Optional[dict]:
    """Beats from the media index's tempo analysis: the saved one when the file is indexed, else a fresh one.

    The index tracks beats against a tempo (and finds real bars), so this is the one beat implementation;
    ``EnergyBeatEngine`` below is only the fallback when the index cannot read the file. A file with no
    steady rhythm returns no beats and bpm 0, instead of beats invented from speech or noise.
    """
    try:
        from classes.media_fingerprint import fingerprint
        from classes.media_index import audio as index_audio, default_shelf, sha_of
        from classes.media_index.probe import probe_media

        shelf = default_shelf()
        sha = sha_of(fingerprint(media_path))
        saved = shelf.read_json(sha, "audio.json") if sha and shelf.layer_ready(sha, "audio") else None
        audio = saved or index_audio.analyze_audio(media_path, probe_media(media_path))
        if not audio:
            return None
        tempo = audio.get("tempo")
        if not tempo or not tempo.get("beats"):
            return {"beats": [], "downbeats": [], "bpm": 0.0, "source": "media-index", "rhythmic": False}
        times = [float(t) for t in tempo["beats"]]
        downs = (audio.get("music") or {}).get("downbeats") or times[::4]
        return {"beats": [{"timeSec": t, "strength": 1.0} for t in times], "downbeats": [{"timeSec": float(t)} for t in downs],
                "bpm": float(tempo["bpm"]), "source": "media-index", "rhythmic": True}
    except Exception:  # noqa: BLE001 - any failure falls back to the older engine
        log.debug("media index beats unavailable for %s", media_path, exc_info=True)
        return None


def detect_beats(
    media_path: str,
    *,
    token: Optional[CancelToken] = None,
    use_index: bool = True,
) -> dict:
    import os

    if use_index and _factory is EnergyBeatEngine:      # a test or caller that installed its own engine gets that engine
        found = _from_media_index(media_path)
        if found is not None:
            return found
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
