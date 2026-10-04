"""
 @file
 @brief Shots and camera motion, measured from the file in one decode pass.

 ffmpeg decodes the original at ten small frames a second; numpy finds where the picture
 changes (hard cuts, dissolves, fades to black), assembles shots (no record shorter than
 ``SHOT_MIN_SECONDS`` or longer than ``SHOT_MAX_SECONDS``) and estimates how the camera and the
 scene move inside each shot. The same frames feed ``look.LookAccumulator`` for colour.

 Everything here is *measured*: it reports the numbers it computed (and how sure the
 estimate is), never a model's opinion. Limits that matter, so nobody trusts it blindly:
 a cut between two near-identical scenes can be missed, and "busy" means the scene moves,
 not that the camera does.
"""

from __future__ import annotations

import math
import subprocess
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

import numpy as np

from classes.ffmpeg_cli import popen_ffmpeg
from classes.media_index import schema as S
from classes.media_index.quality import frame_sharpness
from classes.media_index.probe import analysis_size

# --- hard cuts (calibrated on real footage joined at known times: every cut scored
#     A >= 0.16 and B >= 0.54, the busiest non-cut frame A = 0.05 and B = 0.08) ---------
CUT_A_ABS = 0.09      # mean absolute grey difference between consecutive frames, 0..1
CUT_B_ABS = 0.18      # half the L1 distance between 32-bin grey histograms, 0..1
CUT_B_STRONG = 0.35   # a histogram jump this large is a cut on its own
CUT_A_STRONG = 0.25   # so is a pixel difference this large
CUT_ADAPT_A = 2.5     # busy footage raises the bar: this many times the recent median
CUT_ADAPT_B = 3.0
FLASH_RETURN = 0.04   # a bright flash returns to the pre-flash picture within this difference

# --- dissolves: the middle of the window is a blend of its two ends --------------------
DISSOLVE_K = 6                # frames each side of the centre (0.6 s at 10 fps)
DISSOLVE_MIN_END_DIFF = 0.12  # the two ends must be clearly different pictures
DISSOLVE_MAX_RATIO = 0.22     # residual of the blend fit relative to the end difference
DISSOLVE_MIN_FRAMES = 2       # candidate centres in a row that make one dissolve
DISSOLVE_CELL_DIFF = 0.05     # a cell of the frame counts as changed above this RMS difference
DISSOLVE_MIN_AREA = 0.75      # a dissolve changes the picture, not a corner of it (an element
                              # fading into a UI panel, an overlay fading off a person)
GRID = (4, 4)
SMALL = (80, 45)              # frames are reduced to this for the blend test (w, h)

# --- fades through black ------------------------------------------------------------------
BLACK_LUMA = 0.05
BLACK_MIN_SECONDS = 0.3
EDGE_MARGIN = 0.4             # boundaries this close to the start or end are not boundaries

# --- a transition is a run of steps, a cut is one spike ------------------------------------
# Each step of a dissolve or fade can look like a cut by itself (the histogram jumps), so the cut test alone reports
# several "cuts" in one transition. These rules look at the run around a reported cut and, when it is a smooth ramp,
# replace the cuts with one boundary. They only reclassify boundaries already reported; they never add a new cut.
DIP_LUMA = 0.08               # a fade through black reaches (nearly) black ...
DIP_SHOULDER_LUMA = 0.10      # ... from and back to a picture at least this bright
DIP_MIN_STEPS = 3             # steps (frames) of steady darkening before it and steady brightening after it
DIP_STEP_LUMA = 0.01          # a step of the ramp changes the mean brightness by at least this (a scene drifts by far less)
DIP_SPAN_SECONDS = 3.0        # the whole dip, down and up, is at most this long
TRANSITION_GUARD_SECONDS = 0.8   # no second transition starts this soon after one ends: such a "dissolve" is its tail
GRADUAL_B = 0.12              # histogram change per step that counts as "the picture is changing"
GRADUAL_MAX_A = CUT_A_ABS     # but the pixels move less than a cut's step: a blend, not a jump
GRADUAL_MIN_STEPS = 4         # consecutive steps of that

# --- motion -----------------------------------------------------------------------------
WIN = 128                     # phase correlation runs on the centre WIN x WIN of the grey frame
MIN_PEAK = 0.10               # below this the correlation peak is noise
PAN_SPEED = 0.03              # fractions of the frame per second
ZOOM_RATE = 0.04              # fractional scale change per second
STILL_DIFF = 0.008
STILL_SHIFT_PX = 0.25
JITTER_PX = 0.35
BUSY_DIFF = 0.03

# --- sharpness: a per-shot median needs far fewer frames than cut detection does ----------------
SHARP_EVERY = 3


# ============================ transitions: ramps, not cuts ============================
def find_dips(lumas: List[float], fps: float) -> List[Dict[str, float]]:
    """Fades through black: the picture darkens steadily to (nearly) black and brightens steadily again.

    Returns ``{"t", "start", "end"}`` (seconds) for each dip: the darkest frame and the span of both ramps. A dip needs
    ``DIP_MIN_STEPS`` frames of steady change on each side, so a black frame between two cuts, a one-frame dropout and
    a dark scene are none of them dips. Black held for ``BLACK_MIN_SECONDS`` or more is a shot of its own (the black
    run logic reports it), not a dip.
    """
    out: List[Dict[str, float]] = []
    n = len(lumas)
    i = 1
    while i < n - 1:
        if lumas[i] >= DIP_LUMA or lumas[i] > lumas[i - 1] + 1e-9:
            i += 1
            continue
        lo = i
        while lo > 0 and lumas[lo - 1] > lumas[lo] + DIP_STEP_LUMA:         # walk back up the way down
            lo -= 1
        j = i
        while j + 1 < n and lumas[j + 1] <= lumas[j] + 1e-3 and lumas[j + 1] < DIP_LUMA:   # across the bottom
            j += 1
        hi = j
        while hi + 1 < n and lumas[hi + 1] > lumas[hi] + DIP_STEP_LUMA:      # up the other side
            hi += 1
        down, up = i - lo, hi - j
        held = (j - i + 1) / fps >= BLACK_MIN_SECONDS
        if (not held and down >= DIP_MIN_STEPS and up >= DIP_MIN_STEPS and lumas[lo] >= DIP_SHOULDER_LUMA
                and lumas[hi] >= DIP_SHOULDER_LUMA and (hi - lo) / fps <= DIP_SPAN_SECONDS):
            darkest = min(range(i, j + 1), key=lambda k: lumas[k])
            out.append({"t": darkest / fps, "start": lo / fps, "end": hi / fps})
        i = max(j, hi) + 1
    return out


def find_gradual_runs(pairs: List[Dict[str, Any]], fps: float) -> List[Dict[str, float]]:
    """Runs of steps where the histogram keeps moving but the pixels hardly do: the signature of a dissolve."""
    out: List[Dict[str, float]] = []
    run: List[Dict[str, Any]] = []

    def close() -> None:
        if len(run) >= GRADUAL_MIN_STEPS:
            first, last = run[0]["t"], run[-1]["t"]
            out.append({"t": (first + last) / 2.0, "start": first - 1.0 / fps, "end": last})
        run.clear()

    for p in pairs:
        if p.get("b") is not None and p["b"] >= GRADUAL_B and p["diff"] < GRADUAL_MAX_A:
            run.append(p)
        else:
            close()
    close()
    return out


def reconcile_transitions(boundaries: List[Dict[str, Any]], dips: List[Dict[str, float]],
                          runs: List[Dict[str, float]], fps: float) -> List[Dict[str, Any]]:
    """Replace the cuts reported inside a fade or a dissolve by one boundary at its centre.

    The tail of a reconciled transition (a "dissolve" reported just after it) is dropped too.
    """
    kept = list(boundaries)
    for dip in dips:
        inside = [b for b in kept if b["kind"] in ("hard", "dissolve") and dip["start"] - 1.0 / fps <= b["t"] <= dip["end"] + 1.0 / fps]
        if not inside:
            continue
        kept = [b for b in kept if b not in inside]
        if not any(b["kind"] == "fade" and abs(b["t"] - dip["t"]) < DIP_SPAN_SECONDS / 2 for b in kept):
            kept.append({"t": round(dip["t"], 3), "kind": "fade", "score": 1.0})
    for run in runs:
        inside = [b for b in kept if b["kind"] == "hard" and run["start"] - 1.0 / fps <= b["t"] <= run["end"] + 1.0 / fps]
        if not inside:
            continue                       # nothing was reported as a cut here: not this rule's business
        kept = [b for b in kept if b not in inside]
        kept = [b for b in kept if not (b["kind"] == "dissolve" and (run["start"] - TRANSITION_GUARD_SECONDS <= b["t"] <= run["end"] + TRANSITION_GUARD_SECONDS))]
        kept.append({"t": round(run["t"], 3), "kind": "dissolve", "score": 1.0})
    return sorted(kept, key=lambda b: b["t"])


# ============================ small pure helpers ============================
def rgb_to_gray(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 grey, rounded to 8 bits (the luma FrameScope uses)."""
    luma = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    return np.rint(luma).clip(0, 255).astype(np.uint8)


def gray_hist32(gray: np.ndarray) -> np.ndarray:
    h = np.bincount((gray >> 3).ravel(), minlength=32).astype(np.float64)
    return h / max(1.0, h.sum())


def cut_features(prev_gray: np.ndarray, gray: np.ndarray, prev_hist: np.ndarray,
                 hist: np.ndarray) -> Tuple[float, float]:
    a = float(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)).mean()) / 255.0
    b = float(0.5 * np.abs(hist - prev_hist).sum())
    return a, b


class CutDetector:
    """Online hard-cut test on consecutive-frame features, raised by recent busyness."""

    def __init__(self) -> None:
        self._a: Deque[float] = deque(maxlen=10)
        self._b: Deque[float] = deque(maxlen=10)

    def is_cut(self, a: float, b: float) -> bool:
        med_a = float(np.median(self._a)) if self._a else 0.0
        med_b = float(np.median(self._b)) if self._b else 0.0
        thr_a = max(CUT_A_ABS, CUT_ADAPT_A * med_a)
        thr_b = max(CUT_B_ABS, CUT_ADAPT_B * med_b)
        cut = ((a >= thr_a and b >= CUT_B_ABS * 0.67) or b >= max(CUT_B_STRONG, thr_b)
               or a >= max(CUT_A_STRONG, thr_a))
        if not cut:  # a cut must not teach the detector that the footage is busy
            self._a.append(a)
            self._b.append(b)
        return cut


def small_gray(gray: np.ndarray) -> np.ndarray:
    """Grey frame reduced to SMALL by block averaging (float32), for the blend test."""
    h, w = gray.shape
    bw, bh = max(1, w // SMALL[0]), max(1, h // SMALL[1])
    h2, w2 = (h // bh) * bh, (w // bw) * bw
    view = gray[:h2, :w2].reshape(h2 // bh, bh, w2 // bw, bw).mean(axis=(1, 3)).astype(np.float32)
    # resample to exactly SMALL with nearest indices so every video yields the same shape
    yi = (np.arange(SMALL[1]) * view.shape[0] / SMALL[1]).astype(int)
    xi = (np.arange(SMALL[0]) * view.shape[1] / SMALL[0]).astype(int)
    return view[np.ix_(yi, xi)]


def changed_area(start: np.ndarray, end: np.ndarray) -> float:
    """Fraction of the frame (on a coarse grid) that differs clearly between two frames."""
    gh, gw = GRID
    h, w = start.shape
    ch, cw = h // gh, w // gw
    diff = (end[:ch * gh, :cw * gw] - start[:ch * gh, :cw * gw]) / 255.0
    cells = diff.reshape(gh, ch, gw, cw)
    rms = np.sqrt((cells ** 2).mean(axis=(1, 3)))
    return float((rms >= DISSOLVE_CELL_DIFF).mean())


def blend_ratio(window: List[np.ndarray]) -> Tuple[Optional[float], float]:
    """How well the middle frames of *window* are a cross-fade between its first and last.

    Returns (ratio, end_difference). ratio is the median fit residual relative to how
    different the two ends are (small = a dissolve), or None when it is not monotonic.
    """
    start, end = window[0], window[-1]
    d = (end - start).ravel()
    dd = float(d @ d)
    end_diff = math.sqrt(dd / d.size) / 255.0
    if end_diff < DISSOLVE_MIN_END_DIFF:
        return None, end_diff
    if changed_area(start, end) < DISSOLVE_MIN_AREA:
        return None, end_diff
    residuals, alphas = [], []
    for frame in window[1:-1]:
        alpha = float(((frame - start).ravel() @ d) / dd)
        alpha = min(1.0, max(0.0, alpha))
        fit = start + alpha * (end - start)
        residuals.append(math.sqrt(float(np.mean((frame - fit) ** 2))) / 255.0)
        alphas.append(alpha)
    if not np.all(np.diff(alphas) >= -0.08):
        return None, end_diff
    return float(np.median(residuals)) / end_diff, end_diff


# ============================ motion ============================
_HANN: Dict[int, np.ndarray] = {}


def _hann(n: int) -> np.ndarray:
    win = _HANN.get(n)
    if win is None:
        w1 = np.hanning(n).astype(np.float32)
        win = np.outer(w1, w1)
        _HANN[n] = win
    return win


def center_crop(gray: np.ndarray, n: int = WIN) -> np.ndarray:
    h, w = gray.shape
    n = min(n, h, w)
    y0, x0 = (h - n) // 2, (w - n) // 2
    return gray[y0:y0 + n, x0:x0 + n].astype(np.float32)


def phase_shift(prev: np.ndarray, cur: np.ndarray) -> Tuple[float, float, float]:
    """Where the content of *prev* went in *cur*: (dx, dy) in pixels (+x right, +y down), and
    the correlation peak (0..1, how sure)."""
    n = prev.shape[0]
    win = _hann(n)
    fa = np.fft.rfft2((prev - prev.mean()) * win)
    fb = np.fft.rfft2((cur - cur.mean()) * win)
    cross = fb * np.conj(fa)
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.irfft2(cross, s=(n, n))
    py_i, px_i = np.unravel_index(int(np.argmax(corr)), corr.shape)
    py, px = int(py_i), int(px_i)
    peak = float(corr[py, px])

    def refine(c: np.ndarray, idx: int, axis: int) -> float:
        lo = c[(idx - 1) % n, px] if axis == 0 else c[py, (idx - 1) % n]
        mid = c[py, px]
        hi = c[(idx + 1) % n, px] if axis == 0 else c[py, (idx + 1) % n]
        denom = lo - 2.0 * mid + hi
        return 0.0 if abs(denom) < 1e-12 else 0.5 * (lo - hi) / denom

    dy = py + refine(corr, py, 0)
    dx = px + refine(corr, px, 1)
    if dy > n / 2:
        dy -= n
    if dx > n / 2:
        dx -= n
    return float(dx), float(dy), peak


def motion_between(prev_gray: np.ndarray, gray: np.ndarray) -> Optional[Dict[str, float]]:
    """Camera motion between two consecutive grey frames, from four quadrant correlations.

    dx/dy: common shift (pan/tilt); zoom: outward drift of the quadrants, in pixels at
    the quadrant centres (about half the crop); peak: the weakest quadrant's confidence.
    """
    a, b = center_crop(prev_gray), center_crop(gray)
    n = a.shape[0]
    if n < 32:
        return None
    half = n // 2
    shifts, peaks = [], []
    for sy, sx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        ys = slice(0, half) if sy < 0 else slice(half, n)
        xs = slice(0, half) if sx < 0 else slice(half, n)
        dx, dy, peak = phase_shift(a[ys, xs], b[ys, xs])
        shifts.append((dx, dy, sx, sy))
        peaks.append(peak)
    dx = float(np.mean([s[0] for s in shifts]))
    dy = float(np.mean([s[1] for s in shifts]))
    # outward component of each quadrant's own drift, relative to the common shift
    zoom = float(np.mean([((s[0] - dx) * s[2] + (s[1] - dy) * s[3]) / 2.0 for s in shifts]))
    return {"dx": dx, "dy": dy, "zoom": zoom, "peak": float(min(peaks)), "crop": float(n)}


def classify_motion(pairs: List[Dict[str, float]], fps: float) -> Dict[str, Any]:
    """Summarise a shot's frame pairs: camera class plus the numbers behind it."""
    out: Dict[str, Any] = {"class": "unknown", "direction": None, "confidence": 0.0,
                           "level": 0.0, "diff_median": None, "pan_speed": 0.0, "tilt_speed": 0.0,
                           "zoom_rate": 0.0, "jitter_px": 0.0, "valid_pairs": 0, "pairs": len(pairs)}
    if not pairs:
        return out
    diffs = np.array([p["diff"] for p in pairs], dtype=np.float64)
    out["diff_median"] = round(float(np.median(diffs)), 5)
    out["level"] = round(float(min(1.0, np.median(diffs) / 0.06)), 3)
    good = [p for p in pairs if p.get("peak", 0.0) >= MIN_PEAK and p.get("crop")]
    out["valid_pairs"] = len(good)
    if len(good) < 3:
        if np.median(diffs) < STILL_DIFF:
            out.update({"class": "static", "confidence": 0.5})
        elif np.median(diffs) >= BUSY_DIFF:
            out.update({"class": "busy", "confidence": 0.4})
        return out

    crop = good[0]["crop"]
    dx = np.array([p["dx"] for p in good])
    dy = np.array([p["dy"] for p in good])
    zoom = np.array([p["zoom"] for p in good])
    peaks = np.array([p["peak"] for p in good])
    mean_dx, mean_dy, mean_zoom = float(dx.mean()), float(dy.mean()), float(zoom.mean())
    pan = mean_dx * fps / crop     # content speed, fractions of the crop per second
    tilt = mean_dy * fps / crop
    zrate = mean_zoom * fps / (crop / 4.0)   # quadrant centres sit crop/4 from the centre
    out["pan_speed"], out["tilt_speed"], out["zoom_rate"] = round(pan, 4), round(tilt, 4), round(zrate, 4)

    def consistency(v: np.ndarray) -> float:
        total = float(np.abs(v).sum())
        return abs(float(v.sum())) / total if total > 1e-9 else 0.0

    # jitter: what is left of the shift after removing its slow trend
    if len(good) >= 5:
        kernel = np.ones(5) / 5.0
        hp = np.concatenate([dx - np.convolve(dx, kernel, mode="same"),
                             dy - np.convolve(dy, kernel, mode="same")])
        jitter = float(np.std(hp[2:-2])) if hp.size > 4 else 0.0
    else:
        jitter = float(np.std(np.concatenate([dx, dy])))
    out["jitter_px"] = round(jitter, 3)
    still = float(np.median(diffs)) < STILL_DIFF and float(np.mean(np.hypot(dx, dy))) < STILL_SHIFT_PX
    conf = float(np.clip(np.median(peaks) / 0.5, 0.0, 1.0))

    zoom_ok = abs(zrate) >= ZOOM_RATE and consistency(zoom) >= 0.7
    pan_ok = abs(pan) >= PAN_SPEED and consistency(dx) >= 0.7
    tilt_ok = abs(tilt) >= PAN_SPEED and consistency(dy) >= 0.7
    if still:
        out.update({"class": "static", "confidence": round(max(conf, 0.6), 3)})
    elif zoom_ok and abs(zrate) * 1.0 >= max(abs(pan), abs(tilt)) * 0.8:
        out.update({"class": "zoom_in" if zrate > 0 else "zoom_out", "confidence": round(conf, 3)})
    elif pan_ok or tilt_ok:
        horizontal = abs(pan) >= abs(tilt)
        out["class"] = "pan" if horizontal else "tilt"
        # content moves opposite to the camera
        if horizontal:
            out["direction"] = "right" if pan < 0 else "left"
        else:
            out["direction"] = "down" if tilt < 0 else "up"
        out["confidence"] = round(conf, 3)
    elif jitter >= JITTER_PX:
        out.update({"class": "handheld", "confidence": round(conf, 3)})
    elif float(np.median(diffs)) >= BUSY_DIFF:
        out.update({"class": "busy", "confidence": round(conf, 3)})
    else:
        out.update({"class": "static", "confidence": round(conf * 0.7, 3)})
    return out


# ============================ shots ============================
def merge_boundaries(boundaries: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Sort, drop edge ones, and keep one boundary per SHOT_MIN_SECONDS (hard beats soft)."""
    rank = {"hard": 2, "dissolve": 1, "fade": 1}
    kept: List[Dict[str, Any]] = []
    for b in sorted(boundaries, key=lambda x: x["t"]):
        if b["t"] <= EDGE_MARGIN or (duration and b["t"] >= duration - EDGE_MARGIN):
            continue
        if kept and b["t"] - kept[-1]["t"] < S.SHOT_MIN_SECONDS:
            if rank.get(b["kind"], 0) > rank.get(kept[-1]["kind"], 0):
                kept[-1] = b
            continue
        kept.append(b)
    return kept


def build_shots(boundaries: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Shots between boundaries; long ones split evenly so none exceeds SHOT_MAX_SECONDS."""
    edges = [0.0] + [b["t"] for b in boundaries] + [float(duration)]
    opens: List[Optional[Dict[str, Any]]] = [None] + list(boundaries)
    shots: List[Dict[str, Any]] = []
    for i in range(len(edges) - 1):
        start, end = edges[i], edges[i + 1]
        if end - start <= 0:
            continue
        parts = max(1, int(math.ceil((end - start) / S.SHOT_MAX_SECONDS - 1e-9)))
        step = (end - start) / parts
        for j in range(parts):
            b = opens[i] if j == 0 else None
            shots.append({
                "start": round(start + j * step, 3),
                "end": round(start + (j + 1) * step if j < parts - 1 else end, 3),
                "opens_with": ({"kind": b["kind"], "score": round(float(b.get("score", 0.0)), 3)}
                               if b else ({"kind": "split"} if j > 0 else None)),
                "split": j > 0,
            })
    for n, shot in enumerate(shots):
        shot["id"] = n
        shot["duration"] = round(shot["end"] - shot["start"], 3)
    return shots


# ============================ the decode pass ============================
class Cancelled(Exception):
    """Raised inside the analysis loop when the caller asked to stop."""


def _frames(path: str, width: int, height: int, should_cancel: Optional[Callable[[], bool]],
            still: bool = False) -> Iterator[np.ndarray]:
    # A still image is one frame with no duration: the fps filter would drop it, so ask for it directly.
    rate = "" if still else f"fps={S.ANALYSIS_FPS:g},"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", f"{rate}scale={width}:{height}:flags=area,format=rgb24"]
    cmd += (["-frames:v", "1"] if still else []) + ["-f", "rawvideo", "-"]
    proc = popen_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=width * height * 3 * 4)
    size = width * height * 3
    try:
        assert proc.stdout is not None
        while True:
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def analyze_structure(
    path: str,
    probe: Dict[str, Any],
    *,
    look: Any = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Shots and motion for *path*. Returns None when there is no video to analyse.

    *look* is an optional ``look.LookAccumulator``: it is fed every analysis frame.
    Raises ``Cancelled`` if *should_cancel* turns true.
    """
    video = probe.get("video") if isinstance(probe, dict) else None
    if not video:
        return None
    size = analysis_size(int(video["width"]), int(video["height"]), S.ANALYSIS_LONG_EDGE)
    if size is None:
        return None
    width, height = size
    fps = S.ANALYSIS_FPS
    duration = float(probe.get("duration") or 0.0)
    still = duration <= 0.0      # an image: one frame, no cuts, no motion

    detector = CutDetector()
    boundaries: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []          # per consecutive-frame pair: t, diff, motion
    ring: Deque[Tuple[int, np.ndarray]] = deque(maxlen=2 * DISSOLVE_K + 1)
    soft_candidates: List[Tuple[float, float, float]] = []  # (centre t, ratio, end_diff)
    last_hard_idx = -10 ** 9
    black_run_start: Optional[int] = None
    frames_seen = 0
    lumas: List[float] = []                   # mean grey of every frame, 0..1
    sharps: Dict[int, float] = {}             # edge strength of every SHARP_EVERY-th frame
    prev_gray = prev_hist = None
    pending_cut: Optional[Tuple[int, float, float]] = None  # (index, a, b) awaiting flash check
    gray_before_pending: Optional[np.ndarray] = None

    def finish_black(end_idx: int, at_end: bool = False) -> None:
        """Close a black run. One that touches the start or end of the file is a fade-in or
        fade-out, not a boundary between two shots."""
        nonlocal black_run_start
        if black_run_start is not None:
            run = end_idx - black_run_start
            if run / fps >= BLACK_MIN_SECONDS and black_run_start > 0 and not at_end:
                centre = (black_run_start + end_idx) / 2.0 / fps
                boundaries.append({"t": round(centre, 3), "kind": "fade", "score": 1.0})
            black_run_start = None

    for idx, rgb in enumerate(_frames(path, width, height, should_cancel, still=still)):
        frames_seen = idx + 1
        gray = rgb_to_gray(rgb)
        hist = gray_hist32(gray)
        if look is not None:
            look.add(idx, rgb, gray)
        small = small_gray(gray)
        ring.append((idx, small))

        mean_luma = float(gray.mean()) / 255.0
        lumas.append(mean_luma)
        if idx % SHARP_EVERY == 0:
            sharps[idx] = frame_sharpness(gray)
        if mean_luma < BLACK_LUMA:
            if black_run_start is None:
                black_run_start = idx
        else:
            finish_black(idx)

        pair: Optional[Dict[str, Any]] = None
        if prev_gray is not None and prev_hist is not None:
            a, b = cut_features(prev_gray, gray, prev_hist, hist)
            is_cut = detector.is_cut(a, b)
            # Flash: a one-frame spike that returns to the earlier picture is not a cut, and
            # neither is the jump back (this pair).
            if pending_cut is not None:
                p_idx, p_a, p_b = pending_cut
                returned = gray_before_pending is not None and float(
                    np.abs(gray.astype(np.int16) - gray_before_pending.astype(np.int16)).mean()) / 255.0 < FLASH_RETURN
                if returned:
                    is_cut = False
                else:
                    boundaries.append({"t": round(p_idx / fps, 3), "kind": "hard",
                                       "score": round(max(p_a / CUT_A_ABS, p_b / CUT_B_ABS), 3)})
                    last_hard_idx = p_idx
                pending_cut = None
                gray_before_pending = None
            if is_cut:
                pending_cut = (idx, a, b)
                gray_before_pending = prev_gray
                pair = {"t": idx / fps, "diff": a, "b": b, "cut": True}
            else:
                m = motion_between(prev_gray, gray)
                pair = {"t": idx / fps, "diff": a, "b": b, "cut": False}
                if m:
                    pair.update(m)
            pairs.append(pair)

            # dissolve test on the window that just filled (centre = idx - K)
            if len(ring) == ring.maxlen and idx - last_hard_idx > 2 * DISSOLVE_K and pending_cut is None:
                window = [f for _, f in ring]
                window_pairs = pairs[-(2 * DISSOLVE_K):]
                if not any(p.get("cut") for p in window_pairs):
                    ratio, end_diff = blend_ratio(window)
                    if ratio is not None and ratio <= DISSOLVE_MAX_RATIO:
                        soft_candidates.append(((idx - DISSOLVE_K) / fps, ratio, end_diff))
        prev_gray, prev_hist = gray, hist

        if on_progress is not None and duration > 0 and idx % 50 == 0:
            on_progress(min(0.99, idx / fps / duration))

    if pending_cut is not None:
        p_idx, p_a, p_b = pending_cut
        boundaries.append({"t": round(p_idx / fps, 3), "kind": "hard",
                           "score": round(max(p_a / CUT_A_ABS, p_b / CUT_B_ABS), 3)})
    finish_black(frames_seen, at_end=True)

    # one dissolve per run of neighbouring candidate centres
    run: List[Tuple[float, float, float]] = []
    runs: List[List[Tuple[float, float, float]]] = []
    for cand in soft_candidates:
        if run and cand[0] - run[-1][0] > 1.5 / fps:
            runs.append(run)
            run = []
        run.append(cand)
    if run:
        runs.append(run)
    hard_times = [b["t"] for b in boundaries if b["kind"] == "hard"]
    for r in runs:
        if len(r) < DISSOLVE_MIN_FRAMES:
            continue
        centre = float(np.mean([c[0] for c in r]))
        if any(abs(centre - t) < (DISSOLVE_K + 1) / fps for t in hard_times):
            continue
        boundaries.append({"t": round(centre, 3), "kind": "dissolve",
                           "score": round(1.0 - float(np.min([c[1] for c in r])), 3)})

    if still:
        if on_progress is not None:
            on_progress(1.0)
        return {"frames": frames_seen, "still": True, "analysis": {"fps": fps, "width": width, "height": height},
                "boundaries": [], "shots": []}
    total = duration if duration > 0 else frames_seen / fps
    boundaries = reconcile_transitions(boundaries, find_dips(lumas, fps), find_gradual_runs(pairs, fps), fps)
    merged = merge_boundaries(boundaries, total)
    shots = build_shots(merged, total)
    for shot in shots:
        shot["motion"] = _shot_motion(shot, pairs, merged, fps)
        lo, hi = int(round(shot["start"] * fps)), max(int(round(shot["end"] * fps)), int(round(shot["start"] * fps)) + 1)
        frame_luma = lumas[lo:hi] or lumas[-1:] or [0.0]
        shot["mean_luma"] = round(float(np.mean(frame_luma)), 4)
        shot["black"] = bool(np.median(frame_luma) < BLACK_LUMA)   # a black screen, not a scene
        in_shot = [v for i, v in sharps.items() if lo <= i < hi]
        shot["sharpness"] = None if shot["black"] or not in_shot else round(float(np.median(in_shot)), 4)
    if on_progress is not None:
        on_progress(1.0)
    return {
        "frames": frames_seen,
        "analysis": {"fps": fps, "width": width, "height": height},
        "boundaries": merged,
        "shots": shots,
    }


def _shot_motion(shot: Dict[str, Any], pairs: List[Dict[str, Any]],
                 boundaries: List[Dict[str, Any]], fps: float) -> Dict[str, Any]:
    """Motion summary for one shot from the frame pairs wholly inside it."""
    soft = [b["t"] for b in boundaries if b["kind"] != "hard"]
    inside = []
    for p in pairs:
        t = p["t"]
        if t - 1.0 / fps < shot["start"] - 1e-6 or t >= shot["end"] - 1e-6:
            continue
        if p.get("cut"):
            continue
        if any(abs(t - s) <= (DISSOLVE_K + 1) / fps for s in soft):
            continue  # a cross-fade is not camera motion
        inside.append(p)
    return classify_motion(inside, fps)
