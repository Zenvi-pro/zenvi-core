"""Beat tracking for music-cut edits: tempo, a regular beat grid, downbeats and hits.

``detect_beats(media_path)`` extracts the audio (16 kHz mono WAV, at most ten
minutes) and hands it to the engine the factory hook makes
(``set_beat_factory`` / ``reset_beat_factory``):

* ``TempoBeatEngine`` (the default) tracks a tempo-consistent beat grid, which
  is what cutting on the beat needs. It is the analysis of zenvi-web
  (``packages/engine/src/analysis/beats.ts``), step for step and with the same
  constants:

    onset envelope  per-band log-energy flux (a kick counts as much as a
                    hi-hat), local mean removed
    tempo           autocorrelation of the envelope with harmonic support and
                    a log-normal prior around 120 BPM
    beats           dynamic programming over the envelope (Ellis 2007,
                    "Beat Tracking by Dynamic Programming")
    phase check     a grid sitting on the off-beats (hi-hats) moves half a
                    beat when the half-beats carry more low-band (kick) flux
    downbeats       the bar phase (4/4) whose beats carry the most bass
    beat energy     loudness from each beat to the next, relative to the
                    loudest beat

  numpy is not in the frozen app, so ffmpeg does the signal processing: it
  splits the audio into four bands (``acrossover`` at 150, 800 and 4000 Hz)
  and squares them; Python sums every 256-sample hop as the PCM streams in and
  does the flux, tempo and dynamic programming. The web measures 36 FFT bands
  where this measures four, so each band's flux counts as many of the web's
  bands as it spans.
* ``EnergyBeatEngine`` picks energy peaks (no grid). It answers when ffmpeg
  cannot split the bands.

Both return ``{"beats": [{"timeSec", "strength"}], "downbeats": [...], "bpm"}``;
the tempo engine adds ``onsets`` (seconds), ``beatEnergy`` (0..1, parallel to
``beats``) and ``duration`` (seconds analysed).
"""

from __future__ import annotations

import array
import logging
import math
import operator
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence

from classes.frame_time import round_half_up as _round
from classes.speech.audio_extract import extract_mono_16k_wav
from classes.speech.runtime import CancelToken, inference_slot
from classes.speech.vad import _read_pcm16_mono

log = logging.getLogger("speech.beats")

# Longest stretch of a file analysed, seconds (a song; a long video's first ten minutes).
MAX_SECONDS = 600


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


# ---------------------------------------------------------------------------
# Tempo-grid tracker (zenvi-web analysis/beats.ts)
# ---------------------------------------------------------------------------

# Analysis rate and hop the web's constants are tuned for (86.13 frames/s).
BEAT_SAMPLE_RATE = 22050
HOP = 256
# A frame is four hops (1024 samples, the web's FFT window).
WIN = 4 * HOP
# Crossover frequencies: kick and bass | low mids | presence | air.
BAND_SPLITS_HZ = (150, 800, 4000)
# The kick's band (its flux moves an off-beat grid) and the bass bands (their
# level marks the bar's first beat): beats.ts's 21-150 Hz and 21-280 Hz.
KICK_HZ = 150
BASS_HZ = 280
# Band energy -> the scale of the web's FFT band power (bins of a Hann-windowed
# 1024-point frame: N/2 x mean(hann^2) = 512 x 0.375), so its log1p(1000 x p)
# compresses the same way.
POWER_SCALE = (WIN / 2) * 0.375
# Samples from a frame's start to its onset: a rise in a rectangular window
# comes from the hop that just entered it (its middle).
ONSET_OFFSET = WIN - HOP / 2


def _web_band_edges_hz() -> List[float]:
    """beats.ts's 36 band edges in Hz: log-spaced 30 Hz-8 kHz, each at least one FFT bin wide."""
    bin_hz = BEAT_SAMPLE_RATE / 1024
    lo, hi = math.log(30), math.log(8000)
    edges = [max(1, _round(math.exp(lo + (hi - lo) * b / 36) / bin_hz)) for b in range(37)]
    for b in range(1, 37):
        edges[b] = max(edges[b], edges[b - 1] + 1)
    return [e * bin_hz for e in edges]


def band_weights(splits: Sequence[float] = BAND_SPLITS_HZ) -> List[float]:
    """How many of beats.ts's 36 bands each crossover band spans: its flux weighs as much as theirs."""
    bounds = [0.0, *splits, math.inf]
    edges = _web_band_edges_hz()
    weights = []
    for a, b in zip(bounds, bounds[1:]):
        weights.append(sum(max(0.0, min(b, hi) - max(a, lo)) / (hi - lo) for lo, hi in zip(edges, edges[1:])))
    return weights


def _band_filter(splits: Sequence[float]) -> str:
    """ffmpeg graph: resample, split at *splits*, square, one channel per band (low to high)."""
    labels = "".join(f"[b{i}]" for i in range(len(splits) + 1))
    return (f"[0:a]aresample={BEAT_SAMPLE_RATE},aformat=sample_fmts=flt:channel_layouts=mono,"
            f"acrossover=split={' '.join(str(f) for f in splits)}{labels};"
            f"{labels}amerge=inputs={len(splits) + 1},asplit[x][y];[x][y]amultiply[out]")


@dataclass
class BandEnergies:
    """Energy (sum of squared samples) of each band in every hop, low band first."""

    hops: List[List[float]]
    samples: int
    splits: Sequence[float] = BAND_SPLITS_HZ
    sample_rate: int = BEAT_SAMPLE_RATE


@dataclass
class Envelopes:
    onset: List[float]
    """Full-band onset strength per frame, 0..1."""
    low: List[float]
    """Low-band (kick) flux per frame, 0..1."""
    low_level: List[float]
    """Low-band (kick and bass) level per frame, log power."""
    rms: List[float]
    """RMS per frame."""
    frame_rate: float


def band_energies(path: str, *, token: Optional[CancelToken] = None, max_seconds: float = MAX_SECONDS,
                  splits: Sequence[float] = BAND_SPLITS_HZ) -> BandEnergies:
    """Per-hop energy of each band of *path*'s audio at 22050 Hz (ffmpeg splits and squares).

    The PCM streams in and is summed hop by hop, so a long file never sits in
    memory whole. Raises RuntimeError when ffmpeg is missing or fails.
    """
    from classes.ffmpeg_cli import popen_ffmpeg

    bands = len(splits) + 1
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path,
           "-t", f"{float(max_seconds):.3f}", "-filter_complex", _band_filter(splits), "-map", "[out]",
           "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"]
    frame_bytes = 4 * bands
    chunk = HOP * 512 * frame_bytes  # about six seconds per read
    hops: List[List[float]] = [[] for _ in range(bands)]
    samples = 0
    with tempfile.TemporaryFile() as errors:
        try:
            proc = popen_ffmpeg(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found on PATH") from None
        try:
            stdout = proc.stdout
            assert stdout is not None
            while True:
                if token is not None:
                    token.raise_if_cancelled()
                data = stdout.read(chunk)
                if not data:
                    break
                pcm = array.array("f")
                pcm.frombytes(data[:len(data) - len(data) % frame_bytes])
                if sys.byteorder != "little":
                    pcm.byteswap()
                count = len(pcm) // bands
                samples += count
                for b in range(bands):
                    band = pcm[b::bands]
                    out = hops[b]
                    for k in range(0, count - HOP + 1, HOP):
                        out.append(sum(band[k:k + HOP]))
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if code != 0:
            errors.seek(0)
            message = errors.read().decode("utf-8", "replace").strip()
            raise RuntimeError(message or f"ffmpeg exited with code {code}")
    return BandEnergies(hops=hops, samples=samples, splits=tuple(splits))


def _remove_local_mean(x: List[float], radius: int) -> List[float]:
    out = [0.0] * len(x)
    total = 0.0
    lo = 0
    hi = -1
    for i in range(len(x)):
        want = min(len(x) - 1, i + radius)
        while hi < want:
            hi += 1
            total += x[hi]
        while lo < i - radius:
            total -= x[lo]
            lo += 1
        out[i] = max(0.0, x[i] - total / (hi - lo + 1))
    return out


def _normalize(x: List[float]) -> List[float]:
    peak = 0.0
    for v in x:
        if v > peak:
            peak = v
    return [v / peak for v in x] if peak > 0 else x


def envelopes(energies: BandEnergies) -> Envelopes:
    """Onset, kick flux, bass level and RMS per frame (four hops) from per-hop band energies."""
    hops = energies.hops
    count = min((len(h) for h in hops), default=0)
    frames = count - 3 if count >= 4 else 0
    frame_rate = energies.sample_rate / HOP
    windows = [[h[f] + h[f + 1] + h[f + 2] + h[f + 3] for f in range(frames)] for h in hops]
    levels = [[math.log1p(1000.0 * POWER_SCALE * e) for e in w] for w in windows]
    weights = band_weights(energies.splits)
    tops = [*energies.splits, math.inf]
    kick = [b for b, top in enumerate(tops) if top <= KICK_HZ] or [0]
    bass = [b for b, top in enumerate(tops) if top <= BASS_HZ] or [0]
    total_weight = sum(weights)
    onset = [0.0] * frames
    low = [0.0] * frames
    low_level = [0.0] * frames
    rms = [0.0] * frames
    for f in range(frames):
        if f > 0:
            flux = 0.0
            kick_flux = 0.0
            for b, weight in enumerate(weights):
                rise = levels[b][f] - levels[b][f - 1]
                if rise > 0:
                    flux += weight * rise
                    if b in kick:
                        kick_flux += weight * rise
            onset[f] = flux / total_weight
            low[f] = kick_flux
        low_level[f] = math.log1p(POWER_SCALE * sum(windows[b][f] for b in bass))
        rms[f] = math.sqrt(sum(w[f] for w in windows) / WIN)
    return Envelopes(_normalize(_remove_local_mean(onset, 16)), _normalize(low), low_level, rms, frame_rate)


def _autocorrelation(env: List[float], max_lag: int) -> List[float]:
    mean = sum(env) / (len(env) or 1)
    centred = [v - mean for v in env]
    n = len(centred)
    return [sum(map(operator.mul, centred[:n - lag], centred[lag:])) for lag in range(max_lag + 2)]


def _sample_at(ac: List[float], lag: float) -> float:
    i = math.floor(lag)
    if i < 0 or i + 1 >= len(ac):
        return 0.0
    t = lag - i
    return ac[i] * (1 - t) + ac[i + 1] * t


def estimate_tempo(env: List[float], frame_rate: float, min_bpm: float = 60, max_bpm: float = 200) -> float:
    """Tempo (BPM) of an onset envelope, or 0 when it has no periodicity."""
    if len(env) < frame_rate * 2:
        return 0.0
    max_lag = math.ceil((frame_rate * 60 * 2) / min_bpm)
    ac = _autocorrelation(env, min(max_lag, len(env) - 2))
    if not ac[0] > 0:
        return 0.0
    best = 0.0
    best_bpm = 0.0
    bpm = float(min_bpm)
    while bpm <= max_bpm:
        lag = (frame_rate * 60) / bpm
        support = _sample_at(ac, lag) + 0.5 * _sample_at(ac, 2 * lag) + 0.5 * _sample_at(ac, lag / 2)
        prior = math.exp(-0.5 * (math.log2(bpm / 120) / 0.9) ** 2)
        score = support * prior
        if score > best:
            best = score
            best_bpm = bpm
        bpm += 0.25
    return best_bpm if best > 0 else 0.0


def _track_beats(env: List[float], period: float, tightness: float) -> List[int]:
    """Beat frames by dynamic programming: each beat rewards onset strength and a gap close to the period."""
    n = len(env)
    score = list(env)
    back = [-1] * n
    lo = max(1, _round(period / 2))
    hi = _round(period * 2)
    # Penalty of each gap, longest first: the candidates p = t-hi .. t-lo in order.
    penalty = [tightness * math.log(gap / period) ** 2 for gap in range(hi, lo - 1, -1)]
    for t in range(n):
        first = max(0, t - hi)
        last = t - lo
        if last < first:
            continue
        values = list(map(operator.sub, score[first:last + 1], penalty[hi - (t - first):]))
        best = max(values)
        if best > 0:
            score[t] += best
            back[t] = first + values.index(best)
    end = max(0, n - _round(period))
    for t in range(end, n):
        if score[t] > score[end]:
            end = t
    frames = []
    t = end
    while t >= 0:
        frames.append(t)
        t = back[t]
    frames.reverse()
    return frames


def _pick_onsets(env: List[float], frame_rate: float) -> List[float]:
    min_gap = _round(0.1 * frame_rate)
    out = []
    last = -min_gap
    for i in range(1, len(env) - 1):
        v = env[i]
        if v >= 0.3 and v >= env[i - 1] and v > env[i + 1] and i - last >= min_gap:
            out.append(i / frame_rate)
            last = i
    return out


def _empty() -> dict:
    return {"bpm": 0.0, "beats": [], "downbeats": [], "onsets": [], "beatEnergy": []}


def _mean_at(values: List[float], frames: Sequence[int]) -> float:
    total = 0.0
    for f in frames:
        total += values[f] if 0 <= f < len(values) else 0.0
    return total / max(1, len(frames))


def beats_from_envelopes(env: Envelopes, *, min_bpm: float = 60, max_bpm: float = 200,
                         tightness: float = 100) -> dict:
    """Tempo, beat grid, downbeats, hits and per-beat loudness. Silence or noise gives bpm 0 and no beats."""
    onset, low, low_level, rms, frame_rate = env.onset, env.low, env.low_level, env.rms, env.frame_rate
    if len(onset) < frame_rate * 2:
        return _empty()
    bpm = estimate_tempo(onset, frame_rate, min_bpm, max_bpm)
    if not bpm > 0:
        return _empty()
    period = (frame_rate * 60) / bpm
    frames = _track_beats(onset, period, tightness)

    # Locked onto the off-beats (hats, claps) while the kicks fall between: move half a beat.
    half = _round(period / 2)
    shifted = [f + half for f in frames if f + half < len(onset)]
    if _mean_at(low, shifted) > 1.5 * _mean_at(low, frames) + 0.02:
        frames = shifted

    # No beats in the silent lead-in or tail: start and stop where the music does.
    first = 0
    while first < len(onset) and onset[first] < 0.1:
        first += 1
    last = len(onset) - 1
    while last > 0 and onset[last] < 0.1:
        last -= 1
    frames = [f for f in frames if first - period / 2 <= f <= last + period / 2]
    if len(frames) < 2:
        return _empty()

    # The bar's first beat carries the most kick and bass (snares on 2 and 4 are often louder overall).
    window = max(1, _round(0.06 * frame_rate))

    def bass(f: int) -> float:
        peak = 0.0
        for k in range(f, min(len(low_level), f + window)):
            if low_level[k] > peak:
                peak = low_level[k]
        return peak

    phase = 0
    best_accent = -1.0
    for p in range(4):
        total = 0.0
        count = 0
        for f in frames[p::4]:
            total += bass(f)
            count += 1
        accent = total / count if count > 0 else 0.0
        if accent > best_accent:
            best_accent = accent
            phase = p

    mean_rms = []
    for i, f in enumerate(frames):
        end = frames[i + 1] if i + 1 < len(frames) else min(len(rms), f + _round(period))
        total = 0.0
        for k in range(f, end):
            total += rms[k]
        mean_rms.append(total / (end - f) if end > f else 0.0)
    loudest = max([*mean_rms, 0.0])
    beat_energy = [_round((v / loudest) * 1000) / 1000 if loudest > 0 else 0.0 for v in mean_rms]

    sample_rate = frame_rate * HOP

    def time(frame: int) -> float:
        return _round(((frame * HOP + ONSET_OFFSET) / sample_rate) * 1000) / 1000

    return {
        "bpm": _round(bpm * 10) / 10,
        "beats": [{"timeSec": time(f), "strength": _round(onset[f] * 1000) / 1000} for f in frames],
        "downbeats": [{"timeSec": time(f)} for i, f in enumerate(frames) if i % 4 == phase],
        "onsets": [_round((t + ONSET_OFFSET / sample_rate) * 1000) / 1000 for t in _pick_onsets(onset, frame_rate)],
        "beatEnergy": beat_energy,
    }


class TempoBeatEngine:
    """Tempo grid, downbeats and per-beat loudness (zenvi-web's tracker); energy peaks without ffmpeg."""

    def __init__(self, *, min_bpm: float = 60, max_bpm: float = 200, tightness: float = 100):
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.tightness = tightness

    def detect(self, wav_path: str, *, token: CancelToken) -> dict:
        try:
            energies = band_energies(wav_path, token=token)
        except InterruptedError:
            raise
        except (RuntimeError, OSError) as exc:
            log.warning("beat tracking: ffmpeg could not split the bands (%s); using energy peaks", exc)
            return EnergyBeatEngine().detect(wav_path, token=token)
        token.raise_if_cancelled()
        result = beats_from_envelopes(envelopes(energies), min_bpm=self.min_bpm, max_bpm=self.max_bpm,
                                      tightness=self.tightness)
        result["duration"] = energies.samples / energies.sample_rate
        return result


_factory: Callable[[], BeatEngine] = TempoBeatEngine


def set_beat_factory(factory: Callable[[], BeatEngine]) -> None:
    global _factory
    _factory = factory


def reset_beat_factory() -> None:
    global _factory
    _factory = TempoBeatEngine


def detect_beats(
    media_path: str,
    *,
    token: Optional[CancelToken] = None,
    max_seconds: float = MAX_SECONDS,
) -> dict:
    """Beats of *media_path*'s audio (its first *max_seconds*). Raises RuntimeError when it has none."""
    with inference_slot(token) as tok:
        wav, err = extract_mono_16k_wav(media_path, duration_sec=max_seconds)
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
