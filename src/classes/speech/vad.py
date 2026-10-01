"""Voice activity detection — energy baseline + optional Silero ONNX."""

from __future__ import annotations

import logging
import struct
import wave
from typing import Callable, Optional, Protocol, Sequence

from classes.speech.audio_extract import extract_mono_16k_wav
from classes.speech.runtime import CancelToken, inference_slot

log = logging.getLogger("speech.vad")


class VadEngine(Protocol):
    def speech_windows(
        self,
        wav_path: str,
        *,
        token: CancelToken,
        min_speech_sec: float,
        min_silence_sec: float,
    ) -> list[tuple[float, float]]:
        """Return speech [start, end) windows in source seconds."""
        ...


def _read_pcm16_mono(wav_path: str) -> tuple[list[float], int]:
    with wave.open(wav_path, "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    if sw != 2:
        raise RuntimeError(f"expected 16-bit PCM, got sample width {sw}")
    count = len(raw) // 2
    samples = struct.unpack("<" + "h" * count, raw)
    if nch > 1:
        mono = [
            sum(samples[i : i + nch]) / (nch * 32768.0)
            for i in range(0, count, nch)
        ]
    else:
        mono = [s / 32768.0 for s in samples]
    return mono, rate


class EnergyVad:
    """Always-available VAD: RMS energy vs a speech-relative threshold.

    Music beds that sit near speech level can still trigger — remove_silence
    applies a separate level gate. This matches 'portable first' over Silero.
    """

    def speech_windows(
        self,
        wav_path: str,
        *,
        token: CancelToken,
        min_speech_sec: float = 0.15,
        min_silence_sec: float = 0.35,
        frame_ms: float = 30.0,
        speech_ratio: float = 0.35,
    ) -> list[tuple[float, float]]:
        token.raise_if_cancelled()
        samples, rate = _read_pcm16_mono(wav_path)
        if not samples:
            return []
        frame = max(1, int(rate * frame_ms / 1000.0))
        energies: list[float] = []
        for i in range(0, len(samples), frame):
            token.raise_if_cancelled()
            chunk = samples[i : i + frame]
            if not chunk:
                break
            rms = (sum(x * x for x in chunk) / len(chunk)) ** 0.5
            energies.append(rms)
        if not energies:
            return []
        peak = max(energies) or 1e-9
        thr = peak * float(speech_ratio)
        # Binary mask → merge
        mask = [e >= thr for e in energies]
        windows: list[tuple[float, float]] = []
        i = 0
        while i < len(mask):
            if not mask[i]:
                i += 1
                continue
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            start = i * frame / rate
            end = j * frame / rate
            if end - start >= min_speech_sec:
                windows.append((start, end))
            i = j
        # Merge windows separated by short silence
        if not windows:
            return []
        merged = [windows[0]]
        for s, e in windows[1:]:
            ps, pe = merged[-1]
            if s - pe < min_silence_sec:
                merged[-1] = (ps, e)
            else:
                merged.append((s, e))
        return merged


class SileroOnnxVad:
    """Optional Silero VAD via onnxruntime. Falls back by raising on import miss."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path
        self._session = None

    def _load(self):
        if self._session is not None:
            return self._session
        try:
            import onnxruntime as ort  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is not installed. pip install -r requirements-speech.txt"
            ) from exc
        # Without a bundled Silero .onnx we refuse rather than download blindly
        # in product code paths that expect a local file.
        if not self.model_path:
            raise RuntimeError(
                "Silero ONNX model path not configured; using energy VAD instead."
            )
        import onnxruntime as ort
        self._session = ort.InferenceSession(self.model_path)
        return self._session

    def speech_windows(
        self,
        wav_path: str,
        *,
        token: CancelToken,
        min_speech_sec: float = 0.15,
        min_silence_sec: float = 0.35,
    ) -> list[tuple[float, float]]:
        # Real Silero frame loop is model-specific; until the weights are
        # packaged, raise so the factory falls back to EnergyVad.
        self._load()
        raise RuntimeError("Silero weights not bundled yet")


_vad_factory: Callable[[], VadEngine] = EnergyVad


def set_vad_factory(factory: Callable[[], VadEngine]) -> None:
    global _vad_factory
    _vad_factory = factory


def reset_vad_factory() -> None:
    global _vad_factory
    _vad_factory = EnergyVad


def detect_speech_windows(
    media_path: str,
    *,
    min_speech_sec: float = 0.15,
    min_silence_sec: float = 0.35,
    token: Optional[CancelToken] = None,
) -> list[tuple[float, float]]:
    """Return speech windows for *media_path* (extracts mono 16 kHz wav)."""
    import os

    with inference_slot(token) as tok:
        wav, err = extract_mono_16k_wav(media_path)
        if err:
            raise RuntimeError(err)
        try:
            engine = _vad_factory()
            try:
                return engine.speech_windows(
                    wav,
                    token=tok,
                    min_speech_sec=min_speech_sec,
                    min_silence_sec=min_silence_sec,
                )
            except RuntimeError as exc:
                log.info("VAD engine unavailable (%s); energy fallback", exc)
                return EnergyVad().speech_windows(
                    wav,
                    token=tok,
                    min_speech_sec=min_speech_sec,
                    min_silence_sec=min_silence_sec,
                )
        finally:
            try:
                if os.path.isfile(wav):
                    os.remove(wav)
            except OSError:
                pass


def silence_ranges_from_speech(
    speech: Sequence[tuple[float, float]],
    *,
    duration: float,
    min_pause_sec: float,
    pad_sec: float = 0.05,
) -> list[tuple[float, float]]:
    """Invert speech windows into removable silence, with pad kept around speech."""
    dur = float(duration)
    if dur <= 0:
        return []
    # Expand speech by pad, clamp, merge
    padded: list[tuple[float, float]] = []
    for s, e in speech:
        padded.append((max(0.0, float(s) - pad_sec), min(dur, float(e) + pad_sec)))
    if not padded:
        # Entire file is silence — refuse upstream; here return one range.
        if dur >= min_pause_sec:
            return [(0.0, dur)]
        return []
    padded.sort()
    merged = [padded[0]]
    for s, e in padded[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if s - cursor >= min_pause_sec:
            gaps.append((cursor, s))
        cursor = e
    if dur - cursor >= min_pause_sec:
        gaps.append((cursor, dur))
    return gaps
