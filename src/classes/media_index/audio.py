"""
 @file
 @brief What a file sounds like, measured locally: loudness, silence, spectrum, tempo and beats.

 ffmpeg decodes the native audio track once to 22.05 kHz mono and numpy works through it in
 blocks, so memory stays flat however long the file is. Loudness (LUFS, loudness range, true
 peak) comes from ffmpeg's ``ebur128`` so it is the broadcast-standard measurement, not an
 approximation. Everything is *measured*; whether a stretch is "music" or "speech" is not
 decided here (that is the transcript's and the lazy audio description's job).

 Tempo is reported only when the onset pattern is rhythmic enough to trust it (speech and
 ambience come back with no tempo, not a made-up one).

 Levels and spectral numbers are measured over 0-11 kHz (the analysis rate is 22.05 kHz), so
 broadband noise reads about 3 dB lower than at the native rate; loudness (LUFS) is measured on
 the native audio.
"""

from __future__ import annotations

import math
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from classes.ffmpeg_cli import popen_ffmpeg, run_ffmpeg
from classes.media_index import schema as S

SAMPLE_RATE = 22050
N_FFT = 1024
HOP = 512
FRAME_HZ = SAMPLE_RATE / HOP                 # about 43 analysis frames a second
BLOCK_SECONDS = 30.0

WINDOW_SECONDS = 5.0                         # per-window facts overlap by half
WINDOW_HOP_SECONDS = 2.5
ENVELOPE_HZ = 2.0
SILENCE_DB = -50.0                           # RMS below this is silence
SILENCE_MIN_SECONDS = 0.5

BPM_RANGE = (60.0, 200.0)
BPM_PRIOR_CENTRE = 120.0
BPM_PRIOR_OCTAVES = 0.8                      # width of the "people play near 120" prior
TEMPO_MIN_CONFIDENCE = 0.35
# Real onsets are sharp peaks well above the typical onset strength. A steady tone's flux is
# only ripple (its 99th percentile sits near 1.5 standard deviations) and hiss's is about 2.3,
# so without this gate z-scoring the envelope turns that ripple into a confident fake tempo.
ONSET_PEAK_Z = 3.0
BEAT_MIN_SUPPORT = 0.3                       # z-scored onset strength a beat at the start or end of the chain needs
BEAT_TIGHTNESS = 100.0                       # Ellis (2007): the cost of straying from the tempo
# A frame's flux peaks before the click's own time, so tracked beats come out early. Measured on
# click tracks at 90, 100, 128 and 140 BPM: -26 to -28 ms at every tempo (p10/p90 -36/-17 ms,
# which is the 23 ms frame hop). Add it back so beat times land on the sound.
BEAT_OFFSET_SECONDS = 0.027
_EPS = 1e-10
RUMBLE_HZ = 100.0                            # energy below this reads as wind or handling noise, not content
CLIP_PEAK = 0.995                            # a frame that reaches this is at (or against) full scale
SECTION_MIN_SECONDS = 6.0
SECTION_MIN_TRACK = 24.0                     # shorter material has no sections worth naming
ARC_SECONDS = 4.0
BARS_PER_PHRASE = 4
BEATS_PER_BAR = 4


class Cancelled(Exception):
    """Raised inside the analysis when the caller asked to stop."""


# ============================ pure helpers ============================
def to_db(x: Any, floor: float = -120.0) -> Any:
    """Level in dB of an amplitude (array or scalar), floored so silence is finite."""
    return np.maximum(20.0 * np.log10(np.maximum(x, _EPS)), floor)


def frame_features(buf: np.ndarray, prev_mag: Optional[np.ndarray]) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """Per-frame features for every whole N_FFT frame in *buf* (hop HOP), plus the last magnitudes."""
    frames = np.lib.stride_tricks.sliding_window_view(buf, N_FFT)[::HOP]
    if frames.shape[0] == 0:
        return {k: np.zeros(0, np.float32) for k in ("rms", "peak", "centroid", "flatness", "flux", "low")}, (
            prev_mag if prev_mag is not None else np.zeros(N_FFT // 2 + 1, np.float32))
    window = np.hanning(N_FFT).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE).astype(np.float32)
    total = mag.sum(axis=1) + _EPS
    low = mag[:, freqs < RUMBLE_HZ].sum(axis=1) / total
    centroid = (mag * freqs).sum(axis=1) / total
    flatness = np.exp(np.log(mag + _EPS).mean(axis=1)) / (mag.mean(axis=1) + _EPS)
    logmag = np.log1p(mag * 10.0)
    prev_log = np.log1p((prev_mag if prev_mag is not None else mag[0]) * 10.0)
    stacked = np.vstack([prev_log[None, :], logmag])
    flux = np.maximum(stacked[1:] - stacked[:-1], 0.0).sum(axis=1)
    return {
        "rms": np.sqrt((frames.astype(np.float32) ** 2).mean(axis=1)),
        "peak": np.abs(frames).max(axis=1),
        "centroid": centroid.astype(np.float32),
        "flatness": flatness.astype(np.float32),
        "flux": flux.astype(np.float32),
        "low": low.astype(np.float32),
    }, mag[-1]


def estimate_tempo(flux: np.ndarray, frame_hz: float = FRAME_HZ) -> Tuple[Optional[float], float]:
    """(bpm, confidence 0..1) from the onset envelope, or (None, confidence) when not rhythmic."""
    if flux.size < frame_hz * 8:
        return None, 0.0
    x = flux.astype(np.float64) - flux.mean()
    std = x.std()
    if std < 1e-9:
        return None, 0.0
    x /= std
    if float(np.percentile(x, 99)) < ONSET_PEAK_Z:
        return None, 0.0
    lo = int(math.floor(frame_hz * 60.0 / BPM_RANGE[1]))
    hi = int(math.ceil(frame_hz * 60.0 / BPM_RANGE[0]))
    lags = np.arange(max(2, lo - 1), hi + 2)
    acf = np.array([float(np.dot(x[:-lag], x[lag:])) / (x.size - lag) for lag in lags])
    bpms = frame_hz * 60.0 / lags
    prior = np.exp(-0.5 * (np.log2(bpms / BPM_PRIOR_CENTRE) / BPM_PRIOR_OCTAVES) ** 2)
    # a tempo is also supported by its double period (every other beat), which resolves octave errors
    support = acf.copy()
    for i, lag in enumerate(lags):
        j = int(np.argmin(np.abs(lags - 2 * lag)))
        if abs(lags[j] - 2 * lag) <= 1:
            support[i] += 0.5 * acf[j]
    best = int(np.argmax(support * prior))
    if best <= 0 or best >= len(lags) - 1:
        return None, 0.0
    a, b, c = acf[best - 1], acf[best], acf[best + 1]
    denom = a - 2 * b + c
    shift = 0.0 if abs(denom) < 1e-12 else 0.5 * (a - c) / denom
    lag = float(lags[best]) + float(np.clip(shift, -1.0, 1.0))
    bpm = frame_hz * 60.0 / lag
    confidence = float(np.clip(acf[best] / 0.6, 0.0, 1.0))   # normalised autocorrelation of the onset envelope
    return (bpm if confidence >= TEMPO_MIN_CONFIDENCE else None), confidence


def track_beats(flux: np.ndarray, bpm: float, frame_hz: float = FRAME_HZ) -> List[float]:
    """Beat times (seconds) by dynamic programming: strong onsets, spaced like the tempo."""
    period = frame_hz * 60.0 / bpm
    x = flux.astype(np.float64)
    std = x.std()
    if std < 1e-9 or x.size < 4:
        return []
    x = (x - x.mean()) / std
    n = x.size
    lo, hi = max(1, int(round(period / 2.0))), max(2, int(round(period * 2.0)))
    offsets = np.arange(lo, hi + 1)
    penalty = BEAT_TIGHTNESS * (np.log(offsets / period)) ** 2
    score = x.copy()
    back = np.full(n, -1, np.int64)
    for t in range(lo, n):
        prev_idx = t - offsets
        valid = prev_idx >= 0
        if not valid.any():
            continue
        cand = score[prev_idx[valid]] - penalty[valid]
        j = int(np.argmax(cand))
        best = cand[j]
        if best > 0.0:           # chaining only helps when it beats starting afresh
            score[t] = x[t] + best
            back[t] = int(prev_idx[valid][j])
    # the best chain ends in the last stretch of the file
    tail = slice(max(0, n - int(period * 2)), n)
    t = int(np.argmax(score[tail])) + tail.start
    beats = []
    while t >= 0:
        beats.append(t)
        t = int(back[t])
    beats.reverse()
    # The chain happily keeps stepping through silence at the tempo it has; only beats with an onset under
    # them are real, so unsupported ones at the start and end (before the groove starts, after it stops) go.
    while beats and x[beats[0]] < BEAT_MIN_SUPPORT:
        beats.pop(0)
    while beats and x[beats[-1]] < BEAT_MIN_SUPPORT:
        beats.pop()
    return [round(b / frame_hz + BEAT_OFFSET_SECONDS, 3) for b in beats]


def silence_ranges(rms_db: np.ndarray, frame_hz: float = FRAME_HZ) -> List[List[float]]:
    """[start, end] seconds of every stretch below SILENCE_DB that lasts SILENCE_MIN_SECONDS."""
    below = rms_db < SILENCE_DB
    out: List[List[float]] = []
    start = None
    for i, flag in enumerate(below):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if (i - start) / frame_hz >= SILENCE_MIN_SECONDS:
                out.append([round(start / frame_hz, 3), round(i / frame_hz, 3)])
            start = None
    if start is not None and (len(below) - start) / frame_hz >= SILENCE_MIN_SECONDS:
        out.append([round(start / frame_hz, 3), round(len(below) / frame_hz, 3)])
    return out


def window_facts(feat: Dict[str, np.ndarray], duration: float, frame_hz: float = FRAME_HZ) -> List[Dict[str, Any]]:
    """Facts per overlapping window: level, silence, brightness, flatness, onset density."""
    n = feat["rms"].size
    if n == 0:
        return []
    rms_db = to_db(feat["rms"])
    peak_db = to_db(feat["peak"])
    flux = feat["flux"]
    onset_thr = float(np.mean(flux) + 1.5 * np.std(flux))
    out = []
    start = 0.0
    total = max(duration, n / frame_hz)
    while start < total - 0.25:
        end = min(start + WINDOW_SECONDS, total)
        a, b = int(start * frame_hz), max(int(end * frame_hz), int(start * frame_hz) + 1)
        sl = slice(a, min(b, n))
        if sl.start >= n:
            break
        seg_rms = feat["rms"][sl]
        loud = seg_rms > 1e-5
        onsets = int(np.sum((flux[sl][1:-1] > onset_thr) & (flux[sl][1:-1] >= flux[sl][:-2]) & (flux[sl][1:-1] >= flux[sl][2:]))) if flux[sl].size > 2 else 0
        out.append({
            "start": round(start, 3), "end": round(end, 3),
            "rms_db": round(float(to_db(float(np.sqrt(np.mean(seg_rms ** 2))))), 2),
            "peak_db": round(float(peak_db[sl].max()), 2),
            "silence_ratio": round(float(np.mean(rms_db[sl] < SILENCE_DB)), 3),
            "centroid_hz": round(float(np.average(feat["centroid"][sl], weights=seg_rms + _EPS)) if loud.any() else 0.0, 1),
            "flatness": round(float(np.mean(feat["flatness"][sl][loud])) if loud.any() else 0.0, 4),
            "onsets_per_sec": round(onsets / max(1e-6, (end - start)), 3),
            "clipped_ratio": round(float(np.mean(feat["peak"][sl] >= CLIP_PEAK)), 4),
            "rumble_ratio": round(float(np.mean(feat["low"][sl][loud])) if loud.any() else 0.0, 3),
        })
        start += WINDOW_HOP_SECONDS
    return out


def _level_facts(rms_db: np.ndarray) -> Dict[str, Any]:
    """Noise floor (the quiet 10th percentile of non-silent frames) and dynamic range, in dB."""
    live = rms_db[rms_db > -90.0]
    if live.size < 10:
        return {"noise_floor_db": None, "dynamic_range_db": None}
    floor, top = float(np.percentile(live, 10)), float(np.percentile(live, 95))
    return {"noise_floor_db": round(floor, 1), "dynamic_range_db": round(top - floor, 1)}


def per_second(values: np.ndarray, frame_hz: float = FRAME_HZ) -> np.ndarray:
    """Mean of a per-frame feature over each whole second."""
    seconds = int(values.size / frame_hz)
    if seconds <= 0:
        return np.zeros(0, np.float32)
    idx = (np.arange(values.size) / frame_hz).astype(int)
    keep = idx < seconds
    return np.bincount(idx[keep], weights=values[keep], minlength=seconds) / np.maximum(1, np.bincount(idx[keep], minlength=seconds))


def novelty_boundaries(features: np.ndarray, min_gap: float = SECTION_MIN_SECONDS) -> List[int]:
    """Second indices where the music changes character (Foote's checkerboard novelty on a self-similarity matrix)."""
    t_len = features.shape[0]
    half = int(min_gap)
    if t_len < 4 * half:
        return []
    z = (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-9)
    smooth = np.stack([np.convolve(z[:, j], np.ones(3) / 3.0, mode="same") for j in range(z.shape[1])], axis=1)
    unit = smooth / (np.linalg.norm(smooth, axis=1, keepdims=True) + 1e-9)
    sim = unit @ unit.T
    taper = np.exp(-0.5 * (np.arange(-half, half) + 0.5) ** 2 / (half / 2.0) ** 2)
    sign = np.where((np.arange(-half, half) < 0)[:, None] == (np.arange(-half, half) < 0)[None, :], 1.0, -1.0)
    kernel = sign * np.outer(taper, taper)
    nov = np.zeros(t_len)
    for t in range(half, t_len - half):
        nov[t] = float(np.sum(kernel * sim[t - half:t + half, t - half:t + half]))
    live = nov[half:t_len - half]
    if live.size == 0 or live.std() < 1e-9:
        return []
    threshold = live.mean() + 0.8 * live.std()
    peaks = [t for t in range(half, t_len - half) if nov[t] >= threshold and nov[t] == nov[max(0, t - half):t + half + 1].max()]
    return peaks


RAMP_MIN_SECONDS = 8          # a build or a fade is at least this long
RAMP_MIN_RISE = 0.35          # and moves the energy (0..1) at least this far
RAMP_TOLERANCE = 0.06         # a ramp may dip this much and still be one
RAMP_EDGE = 0.05              # a ramp starts where it has covered this much of the way (5%) and ends where it first gets within the same of its top


def ramp_boundaries(energy_by_second: np.ndarray) -> List[float]:
    """Seconds where a steady build (or fade) starts and ends. A crescendo has no sharp edge for novelty to find.

    A ramp is a stretch of at least ``RAMP_MIN_SECONDS`` where the smoothed energy keeps climbing (or falling) by at least
    ``RAMP_MIN_RISE``. It starts where it has covered 5% of the way and ends where it first gets within 5% of its top, so a long flat
    intro or plateau around it is not counted as part of it.
    """
    n = int(energy_by_second.size)
    if n < 2 * RAMP_MIN_SECONDS:
        return []
    sm = np.convolve(energy_by_second, np.ones(5) / 5.0, mode="same")
    out: List[float] = []
    for sign in (1.0, -1.0):
        series = sign * sm
        i = 0
        while i < n - 1:
            best = i
            k = i
            while k + 1 < n and series[k + 1] >= series[best] - RAMP_TOLERANCE:
                k += 1
                if series[k] > series[best]:
                    best = k
            rise = series[best] - series[i]
            if best - i >= RAMP_MIN_SECONDS and rise >= RAMP_MIN_RISE:
                lo_level = series[i] + RAMP_EDGE * rise
                start = max(j for j in range(i, best + 1) if series[j] <= lo_level)
                top = min(j for j in range(start, best + 1) if series[j] >= series[best] - RAMP_EDGE * rise)       # within 5% of the top
                if top - start >= RAMP_MIN_SECONDS:        # a smoothed step climbs for about 5 s; a build climbs for longer
                    out += [float(start), float(top)]
                i = best + 1
            else:
                i += 1
    return sorted(set(out))


def merge_bounds(novelty: List[float], ramps: List[float], min_gap: float = SECTION_MIN_SECONDS) -> List[float]:
    """Section edges from novelty and ramps: a ramp's edges win (novelty edges inside or beside one are dropped), and no two edges
    are closer than *min_gap* seconds. *ramps* is the flat list of start, end pairs ``ramp_boundaries`` returns."""
    kept = list(ramps)
    spans = list(zip(ramps[0::2], ramps[1::2]))
    for b in novelty:
        inside = any(a < b < z for a, z in spans)                     # a gradual change has no edge in the middle of it
        if not inside and all(abs(b - r) >= min_gap + 2.0 for r in ramps):   # novelty smears an edge over a few seconds: next to a ramp it is that ramp
            kept.append(b)
    kept = sorted(kept)
    out: List[float] = []
    for b in kept:
        if not out or b - out[-1] >= min_gap:
            out.append(b)
    return out


def label_sections(bounds: List[float], energy_by_second: np.ndarray) -> List[Dict[str, Any]]:
    """Sections between *bounds* named by position and relative energy: intro, build, peak, break, steady, outro."""
    edges = [0.0] + [b for b in bounds] + [float(energy_by_second.size)]
    energies = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = energy_by_second[int(a):max(int(b), int(a) + 1)]
        energies.append(float(seg.mean()) if seg.size else 0.0)
    top = max(energies) if energies else 0.0
    out = []
    for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        e, prev = energies[i], energies[i - 1] if i else None
        if i == len(energies) - 1 and len(energies) > 1 and e < 0.5:
            label = "outro"
        elif e >= 0.7 and e >= top - 0.05:
            label = "peak"
        elif i == 0 and e < 0.55:
            label = "intro"
        elif prev is not None and e - prev >= 0.2:
            label = "build"
        elif prev is not None and prev - e >= 0.2:
            label = "break"
        else:
            label = "steady"
        out.append({"start": round(a, 2), "end": round(b, 2), "label": label, "energy": round(e, 3)})
    return out


def music_profile(feat: Dict[str, np.ndarray], rms_db: np.ndarray, bpm: Optional[float], beats: List[float],
                  duration: float) -> Dict[str, Any]:
    """The shape of a piece of audio: energy arc, sections, bars and phrase points, brightness. All measured.

    Energy is level normalised to the file's own quiet-to-loud range, so a quiet song and a loud one both span 0..1.
    """
    per_s = per_second(rms_db.astype(np.float64))
    if per_s.size < 8:
        return {"arc": [], "sections": [], "downbeats": [], "phrase_points": [], "brightness_hz": None}
    lo, hi = float(np.percentile(per_s, 5)), float(np.percentile(per_s, 95))
    energy = np.full(per_s.shape, 0.5) if hi - lo < 3.0 else np.clip((per_s - lo) / (hi - lo), 0.0, 1.0)
    arc_n = int(ARC_SECONDS)
    arc = [round(float(energy[i:i + arc_n].mean()), 3) for i in range(0, energy.size, arc_n)]
    sections: List[Dict[str, Any]] = []
    if duration >= SECTION_MIN_TRACK:
        cols = [per_s, per_second(feat["centroid"].astype(np.float64)), per_second(feat["flatness"].astype(np.float64)),
                per_second(feat["flux"].astype(np.float64)), per_second(feat["low"].astype(np.float64))]
        n = min(c.size for c in cols)
        matrix = np.stack([c[:n] for c in cols], axis=1)
        bounds = merge_bounds([float(b) for b in novelty_boundaries(matrix)], ramp_boundaries(energy[:n]))
        sections = label_sections(bounds, energy[:n])
        if sections:
            sections[-1]["end"] = round(float(duration), 2)
    downbeats: List[float] = []
    phrases: List[float] = []
    if len(beats) >= 2 * BEATS_PER_BAR:
        flux = feat["flux"]
        idx = [min(flux.size - 1, max(0, int(round((t - BEAT_OFFSET_SECONDS) * FRAME_HZ)))) for t in beats]
        strengths = np.array([float(flux[i]) for i in idx])
        phase = int(np.argmax([strengths[p::BEATS_PER_BAR].sum() for p in range(BEATS_PER_BAR)]))
        downbeats = [round(float(t), 3) for t in beats[phase::BEATS_PER_BAR]]
        phrases = downbeats[::BARS_PER_PHRASE]
    loud = feat["rms"] > 1e-5
    brightness = round(float(np.median(feat["centroid"][loud])), 1) if loud.any() else None
    return {"arc": arc, "arc_seconds": ARC_SECONDS, "sections": sections, "downbeats": downbeats, "phrase_points": phrases,
            "brightness_hz": brightness}


# ============================ decoding and loudness ============================
_LUFS = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_LRA = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


def parse_ebur128(stderr: str) -> Optional[Dict[str, float]]:
    """Integrated loudness, loudness range and true peak from ffmpeg's ebur128 summary."""
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else ""
    i, lra, peak = _LUFS.search(summary), _LRA.search(summary), _PEAK.search(summary)
    if not i:
        return None

    def num(m: Optional[re.Match[str]]) -> Optional[float]:
        if not m or m.group(1) == "-inf":
            return None
        return float(m.group(1))

    return {"integrated_lufs": num(i), "lra": num(lra), "true_peak_db": num(peak)}  # type: ignore[dict-item]


def measure_loudness(path: str) -> Optional[Dict[str, float]]:
    try:
        proc = run_ffmpeg(
            ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-vn",
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False, timeout=3600)
    except Exception:
        return None
    return parse_ebur128(proc.stderr or "")


def _blocks(path: str, should_cancel: Optional[Callable[[], bool]]):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    proc = popen_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = int(BLOCK_SECONDS * SAMPLE_RATE) * 4
    try:
        assert proc.stdout is not None
        while True:
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            data = proc.stdout.read(size)
            if not data:
                break
            usable = len(data) - (len(data) % 4)
            if usable:
                yield np.frombuffer(data[:usable], dtype="<f4")
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def analyze_audio(
    path: str,
    probe: Dict[str, Any],
    *,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Audio facts for *path*, or None when it has no audio stream."""
    if not (probe or {}).get("has_audio"):
        return None
    duration = float(probe.get("duration") or 0.0)
    feats: Dict[str, List[np.ndarray]] = {k: [] for k in ("rms", "peak", "centroid", "flatness", "flux", "low")}
    carry = np.zeros(0, np.float32)
    prev_mag: Optional[np.ndarray] = None
    samples = 0
    for block in _blocks(path, should_cancel):
        samples += block.size
        buf = np.concatenate([carry, block]) if carry.size else block
        if buf.size >= N_FFT:
            frames = (buf.size - N_FFT) // HOP + 1
            got, prev_mag = frame_features(buf[: (frames - 1) * HOP + N_FFT], prev_mag)
            for k in feats:
                feats[k].append(got[k])
            carry = buf[frames * HOP:]
        else:
            carry = buf
        if on_progress is not None and duration > 0:
            on_progress(min(0.95, samples / SAMPLE_RATE / duration))
    feat = {k: (np.concatenate(v) if v else np.zeros(0, np.float32)) for k, v in feats.items()}
    if feat["rms"].size == 0:
        return {"version": S.LAYER_VERSIONS[S.LAYER_AUDIO], "has_audio": True, "decoded_seconds": 0.0,
                "loudness": measure_loudness(path), "windows": [], "envelope": {"hz": ENVELOPE_HZ, "db": []},
                "tempo": None, "silence_ranges": [], "kind": S.MEASURED}

    decoded = samples / SAMPLE_RATE
    rms_db = to_db(feat["rms"])
    step = max(1, int(round(FRAME_HZ / ENVELOPE_HZ)))
    env = [round(float(to_db(float(np.sqrt(np.mean(feat["rms"][i:i + step] ** 2))))), 1)
           for i in range(0, feat["rms"].size, step)]
    bpm, confidence = estimate_tempo(feat["flux"])
    beats = track_beats(feat["flux"], bpm) if bpm else []
    if on_progress is not None:
        on_progress(1.0)
    return {
        "version": S.LAYER_VERSIONS[S.LAYER_AUDIO],
        "has_audio": True,
        "decoded_seconds": round(decoded, 3),
        "native": {"sample_rate": (probe.get("audio") or {}).get("sample_rate"),
                   "channels": (probe.get("audio") or {}).get("channels"), "analysed_at": SAMPLE_RATE},
        "loudness": measure_loudness(path),
        "windows": window_facts(feat, duration or decoded),
        "envelope": {"hz": ENVELOPE_HZ, "db": env},
        "tempo": {"bpm": round(bpm, 2), "confidence": round(confidence, 3), "beats": beats} if bpm else None,
        "tempo_confidence": round(confidence, 3),
        "silence_ranges": silence_ranges(rms_db),
        **_level_facts(rms_db),
        "music": music_profile(feat, rms_db, bpm, beats, duration or decoded),
        "kind": S.MEASURED,
    }
