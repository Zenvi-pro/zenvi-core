"""
 @file
 @brief Where the subject is in the picture, and where to put a crop window so it stays in shot.

 Turning landscape footage into a vertical video with a plain centre crop cuts people in half. This reads a few frames of a
 shot, finds the part of the picture that draws the eye (spectral-residual saliency: what differs from the picture's own
 typical texture), and says where a window of the target shape should sit. With faces (when the people index is on) they are
 added to the weight, so a face wins over a bright sign. It is measured, but it is a heuristic about attention, not an
 understanding of the picture: every answer carries a confidence, and a shot with no clear subject says so.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

SAMPLES = 5
SIZE = (128, 72)               # frames are reduced to this for the saliency map (w, h)
SAL_SIZE = (64, 36)
PEAK_POWER = 2.0               # saliency is squared so a strong object outweighs a lot of mild texture
MOVES_AT = 0.12                # a subject whose position changes by more than this share of the frame is moving
FACE_WEIGHT = 3.0              # how much more a face counts than the same area of saliency
SAME_SHAPE = 0.02              # aspect ratios this close need no reframing
LOW_CONFIDENCE = 0.25
# People put their subject near the middle, so a mild bias toward it helps. Measured on 12 real frames whose subject position was read by
# eye (the window held the subject in 7 of 12 without it, 8 of 12 at 0.25; mean error 0.154 -> 0.115). Thin evidence, so it is mild; faces
# from the people index are the real answer for people.
CENTER_SIGMA = 0.25


def _box(a: np.ndarray, r: int = 1) -> np.ndarray:
    """Mean over a (2r+1) square, edges repeated."""
    padded = np.pad(a, r, mode="edge")
    out = np.zeros_like(a, dtype=np.float64)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out += padded[dy:dy + a.shape[0], dx:dx + a.shape[1]]
    return out / float((2 * r + 1) ** 2)


def _blur(a: np.ndarray) -> np.ndarray:
    k = np.array([1, 4, 6, 4, 1], dtype=np.float64) / 16.0
    padded = np.pad(a, 2, mode="edge")
    horizontal = sum(k[i] * padded[2:-2, i:i + a.shape[1]] for i in range(5))
    padded = np.pad(horizontal, ((2, 2), (0, 0)), mode="edge")
    return sum(k[i] * padded[i:i + a.shape[0], :] for i in range(5))


def _shrink(gray: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """Area-average *gray* down to size (w, h)."""
    h, w = gray.shape
    tw, th = size
    ys = np.linspace(0, h, th + 1).astype(int)
    xs = np.linspace(0, w, tw + 1).astype(int)
    out = np.empty((th, tw), dtype=np.float64)
    for i in range(th):
        for j in range(tw):
            out[i, j] = gray[ys[i]:max(ys[i + 1], ys[i] + 1), xs[j]:max(xs[j + 1], xs[j] + 1)].mean()
    return out


def _gauss(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur, edges repeated."""
    radius = max(1, int(round(3 * sigma)))
    x = np.arange(-radius, radius + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(a, ((0, 0), (radius, radius)), mode="edge")
    horizontal = sum(k[i] * padded[:, i:i + a.shape[1]] for i in range(2 * radius + 1))
    padded = np.pad(horizontal, ((radius, radius), (0, 0)), mode="edge")
    return sum(k[i] * padded[i:i + a.shape[0], :] for i in range(2 * radius + 1))


def saliency(gray: np.ndarray) -> np.ndarray:
    """A map (``SAL_SIZE``) of how much each part of the picture stands out; its sum is 1. A flat picture gives a flat map.

    Centre-surround contrast (a patch against its surroundings, at three sizes), which lets texture and noise cancel, then a mild
    bias toward the middle of the picture. (A second cue, distance from the picture's dominant tone, was tried and dropped: on real
    frames it made the window worse, 6 of 12 against 8 of 12 holding the subject.)
    """
    img = _shrink(np.asarray(gray, dtype=np.float64), SAL_SIZE)
    if float(img.std()) < 1e-6:                     # a flat picture has no subject: only the middle bias is left
        flat = np.outer(_prior_profile(img.shape[0]), _prior_profile(img.shape[1]))
        return flat / flat.sum()
    contrast = sum(np.abs(_gauss(img, s) - _gauss(img, 4.0 * s)) for s in (1.2, 2.4, 4.8))
    sal = _gauss(contrast, 1.0)
    sal = sal - float(sal.min())
    h, w = sal.shape
    xs, ys = (np.arange(w) + 0.5) / w, (np.arange(h) + 0.5) / h
    sal = sal * np.exp(-0.5 * ((ys - 0.5) / CENTER_SIGMA) ** 2)[:, None] * np.exp(-0.5 * ((xs - 0.5) / CENTER_SIGMA) ** 2)[None, :]
    total = float(sal.sum())
    return sal / total if total > 0 else np.full(img.shape, 1.0 / img.size)


def with_faces(sal: np.ndarray, faces: Optional[Sequence[Sequence[float]]]) -> np.ndarray:
    """Add faces (boxes as fractions of the picture: x, y, w, h) to a saliency map, so a face outweighs plain texture."""
    if not faces:
        return sal
    out = sal.copy()
    h, w = out.shape
    for x, y, bw, bh in faces:
        x0, x1 = max(0, int(x * w)), min(w, max(int((x + bw) * w), int(x * w) + 1))
        y0, y1 = max(0, int(y * h)), min(h, max(int((y + bh) * h), int(y * h) + 1))
        out[y0:y1, x0:x1] += FACE_WEIGHT * float(sal.max())              # a face counts for more than the strongest plain patch
    return out / out.sum()


def subject_of(sal: np.ndarray) -> Dict[str, float]:
    """The centre (x, y as fractions of the picture) of the strongest part of the map, and how spread out it is."""
    h, w = sal.shape
    weights = sal ** PEAK_POWER
    weights = weights / weights.sum()
    xs = (np.arange(w) + 0.5) / w
    ys = (np.arange(h) + 0.5) / h
    x = float((weights.sum(axis=0) * xs).sum())
    y = float((weights.sum(axis=1) * ys).sum())
    sx = float(np.sqrt((weights.sum(axis=0) * (xs - x) ** 2).sum()))
    sy = float(np.sqrt((weights.sum(axis=1) * (ys - y) ** 2).sum()))
    return {"x": round(x, 4), "y": round(y, 4), "spread_x": round(sx, 4), "spread_y": round(sy, 4)}


def _prior_profile(n: int) -> np.ndarray:
    """What the middle bias alone puts at each position along an axis of *n* cells (sums to 1)."""
    xs = (np.arange(n) + 0.5) / n
    prior = np.exp(-0.5 * ((xs - 0.5) / CENTER_SIGMA) ** 2)
    return prior / prior.sum()


def best_window(sal: np.ndarray, fraction: float, axis: str = "x") -> Dict[str, float]:
    """Where a window covering *fraction* of the picture along *axis* holds the most of the map: its centre and the share held.

    ``confidence`` is how much more of the map the window holds than the middle bias alone would give it, as a share of what could
    be gained (0 = no better than chance, 1 = everything inside the window).
    """
    profile = sal.sum(axis=0) if axis == "x" else sal.sum(axis=1)
    n = profile.size
    width = max(1, min(n, int(round(fraction * n))))
    sums = np.convolve(profile, np.ones(width), mode="valid")
    start = int(np.argmax(sums))
    held = float(sums[start])
    baseline = float(_prior_profile(n)[start:start + width].sum())
    return {"center": round((start + width / 2.0) / n, 4), "held": round(held, 4), "confidence": round(max(0.0, (held - baseline) / max(1e-9, 1.0 - baseline)), 3)}


def source_frames(path: str, start: float, end: float, samples: int = SAMPLES) -> List[Tuple[float, np.ndarray]]:
    """A few frames of [start, end), as small grey pictures with their times."""
    from classes.media_index import refine
    out: List[Tuple[float, np.ndarray]] = []
    span = max(0.0, end - start)
    for k in range(samples):
        t = start + span * (k + 0.5) / samples
        frames, _times = refine.decode_window(path, t, t + 0.12, SIZE)
        out.append((round(t, 3), frames[0]))
    return out


def framing_of(path: str, start: float, end: float, window_fraction: float, axis: str = "x", *, samples: int = SAMPLES,
               faces_at: Optional[Callable[[float], Optional[Sequence[Sequence[float]]]]] = None,
               frames: Optional[Callable[[str, float, float, int], List[Tuple[float, np.ndarray]]]] = None) -> Dict[str, Any]:
    """The subject of a stretch of a file and where a window of *window_fraction* of the picture should sit, over time.

    ``axis`` is the way the window slides ("x" for a vertical crop of landscape footage, "y" for the reverse).
    """
    rows = []
    for t, gray in (frames or source_frames)(path, start, end, samples):
        sal = with_faces(saliency(gray), faces_at(t) if faces_at else None)
        subject = subject_of(sal)
        window = best_window(sal, window_fraction, axis)
        rows.append({"t": t, "x": subject["x"], "y": subject["y"], "window_center": window["center"], "held": window["held"], "confidence": window["confidence"]})
    key = "window_center"
    centres = [r[key] for r in rows]
    median = float(np.median(centres))
    moves = (max(centres) - min(centres)) > MOVES_AT
    confidence = round(float(np.median([r["confidence"] for r in rows])), 3)
    return {"axis": axis, "samples": rows, "center": round(median, 4), "range": [round(min(centres), 4), round(max(centres), 4)], "moves": bool(moves),
            "confidence": confidence, "sure": confidence >= LOW_CONFIDENCE, "method": "saliency", "kind": "measured",
            "window_fraction": round(window_fraction, 4)}


def offsets(framing: Dict[str, Any], source_aspect: float, frame_aspect: float) -> Dict[str, Any]:
    """The position to give a clip that fills a frame of *frame_aspect* so the window sits on the subject.

    For footage wider than the frame the window slides along x and the clip is moved by ``location_x`` (fractions of the frame's
    width); for taller footage, along y by ``location_y``. Returns the property, a static value (the median), keyframe values
    over the samples when the subject moves, and whether a value had to be limited so no bar shows at the edge.
    """
    if source_aspect <= 0 or frame_aspect <= 0 or abs(source_aspect - frame_aspect) / frame_aspect < SAME_SHAPE:
        return {"property": None, "note": "the footage already has the shape of the frame"}
    wide = source_aspect > frame_aspect
    k = source_aspect / frame_aspect if wide else frame_aspect / source_aspect          # how many frames wide (or tall) the filled picture is
    limit = (k - 1.0) / 2.0

    def value(center: float) -> Tuple[float, bool]:
        raw = (0.5 - center) * k
        return max(-limit, min(limit, raw)), abs(raw) > limit + 1e-9

    static, clamped = value(framing["center"])
    out: Dict[str, Any] = {"property": "location_x" if wide else "location_y", "factor": round(k, 4), "static": round(static, 4), "limit": round(limit, 4), "clamped": bool(clamped)}
    if framing.get("moves"):
        track = [(r["t"], *value(r["window_center"])) for r in framing["samples"]]
        out["keyframes"] = [{"t": t, "value": round(v, 4)} for t, v, _c in track]
        out["clamped"] = bool(clamped or any(c for _t, _v, c in track))
    return out
