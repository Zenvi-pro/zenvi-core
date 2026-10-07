"""
 @file
 @brief How a file looks: colour measured per shot and per file, from the decoded frames.

 Built on Phase 6's ``color_agent`` so the stored numbers and a live grade use one definition:
 the per-frame scope is exactly what libopenshot's FrameScope reports (Rec.601 luma rounded to
 eight bits, 256-bin histograms, clipped shadows = luma <= 2, clipped highlights = luma >= 253;
 checked against the real FrameScope in ``tests/test_media_index_look.py``), turned into a
 ``color_agent`` LookProfile with ``build_look_profile``. On top of that this adds what a colourist
 asks for and Phase 6 does not measure: saturation spread, hue balance, the colour of the
 shadows, mids and highlights, black and white points, a dominant palette, and flags for footage
 that is HDR or looks flat (log), because those must not be graded as ordinary Rec.709.

 Measured from the file's own aspect (never a project canvas), so a portrait clip is not
 judged on its black bars. Numbers are unitless 0..1 unless named otherwise.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from classes import color_agent as ca
from classes.media_index import schema as S

SAMPLE_STRIDE = 5          # every 5th analysis frame: 2 colour samples a second
SHADOW_MAX = 84            # 8-bit luma bands for the tone-band colours
HIGHLIGHT_MIN = 171
HUE_BANDS = 12
PALETTE_COLOURS = 5
PALETTE_PIXELS_PER_SAMPLE = 96
LUMA_PERCENTILES = (1, 5, 25, 50, 75, 95, 99)

# flat-looking footage (camera log, or just low contrast): narrow luma range and little colour
FLAT_BLACK_FLOOR = 0.08
FLAT_WHITE_CEILING = 0.82
FLAT_MAX_SAT = 0.28
FLAT_MAX_SPAN = 0.35


class Sample:
    """One analysed frame: FrameScope-equivalent histograms plus the extras.

    ``hist`` is a compact (4, 256) array, rows luma / red / green / blue, so an hour of footage
    costs tens of megabytes, not the hundreds that Python lists of ints would.
    """

    __slots__ = ("t", "hist", "clip_shadows", "clip_highlights", "avg_luma", "sat_mean", "sat_p90",
                 "hue", "tones", "pixels")

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def scope(self) -> Dict[str, Any]:
        """The raw FrameScope-shaped ``video`` dict for this frame (clipped values are pixel counts)."""
        return {
            "present": True,
            "histogram": {"luma": self.hist[0].tolist(), "red": self.hist[1].tolist(),
                          "green": self.hist[2].tolist(), "blue": self.hist[3].tolist()},
            "summary": {"avg_luma": self.avg_luma, "clipped_shadows": self.clip_shadows,
                        "clipped_highlights": self.clip_highlights},
        }


def frame_scope(luma8: np.ndarray, rgb: np.ndarray) -> Dict[str, Any]:
    """The raw ``video`` dict FrameScope would report for this frame (clipped values are pixel counts)."""
    hist = [np.bincount(rgb[..., c].ravel(), minlength=256) for c in range(3)]
    hist_luma = np.bincount(luma8.ravel(), minlength=256)
    return {
        "present": True,
        "histogram": {"luma": hist_luma.tolist(), "red": hist[0].tolist(),
                      "green": hist[1].tolist(), "blue": hist[2].tolist()},
        "summary": {"avg_luma": float(luma8.mean()) / 255.0,
                    "clipped_shadows": int((luma8 <= 2).sum()),
                    "clipped_highlights": int((luma8 >= 253).sum())},
    }


def _saturation_hue(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    f = rgb.astype(np.float32) / 255.0
    mx, mn = f.max(axis=-1), f.min(axis=-1)
    delta = mx - mn
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    hue = np.zeros_like(mx)
    d = np.maximum(delta, 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6.0, hue)
    hue = np.where((mx == g) & (mx != r), (b - r) / d + 2.0, hue)
    hue = np.where((mx == b) & (mx != r) & (mx != g), (r - g) / d + 4.0, hue)
    return sat.astype(np.float32), (hue * 60.0).astype(np.float32) % 360.0


def make_sample(t: float, rgb: np.ndarray, luma8: np.ndarray) -> Sample:
    sat, hue = _saturation_hue(rgb)
    # hue balance: how much saturated colour sits in each of 12 hue bands
    band = np.minimum((hue / (360.0 / HUE_BANDS)).astype(np.int64), HUE_BANDS - 1)
    hue_w = np.bincount(band.ravel(), weights=sat.ravel(), minlength=HUE_BANDS)
    tones = []
    for lo, hi in ((0, SHADOW_MAX), (SHADOW_MAX + 1, HIGHLIGHT_MIN - 1), (HIGHLIGHT_MIN, 255)):
        mask = (luma8 >= lo) & (luma8 <= hi)
        n = int(mask.sum())
        mean = (rgb[mask].reshape(-1, 3).mean(axis=0) / 255.0).tolist() if n else [0.0, 0.0, 0.0]
        tones.append((n, mean))
    h, w = luma8.shape
    ys = np.linspace(0, h - 1, 8).astype(int)
    xs = np.linspace(0, w - 1, PALETTE_PIXELS_PER_SAMPLE // 8).astype(int)
    hist = np.stack([np.bincount(luma8.ravel(), minlength=256)] +
                    [np.bincount(rgb[..., c].ravel(), minlength=256) for c in range(3)]).astype(np.int32)
    return Sample(
        t=t,
        hist=hist,
        clip_shadows=int((luma8 <= 2).sum()), clip_highlights=int((luma8 >= 253).sum()),
        avg_luma=float(luma8.mean()) / 255.0,
        sat_mean=float(sat.mean()), sat_p90=float(np.percentile(sat, 90)),
        hue=hue_w, tones=tones,
        pixels=rgb[np.ix_(ys, xs)].reshape(-1, 3).astype(np.float32) / 255.0,
    )


def kmeans_palette(pixels: np.ndarray, k: int = PALETTE_COLOURS, iterations: int = 12) -> List[Dict[str, Any]]:
    """Dominant colours by deterministic k-means (farthest-point start): [{rgb, hex, weight}]."""
    if pixels.shape[0] == 0:
        return []
    k = max(1, min(k, pixels.shape[0]))
    centres = [pixels.mean(axis=0)]
    for _ in range(k - 1):
        dist = np.min([((pixels - c) ** 2).sum(axis=1) for c in centres], axis=0)
        centres.append(pixels[int(np.argmax(dist))])
    c = np.array(centres, np.float32)
    for _ in range(iterations):
        d = ((pixels[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
        label = d.argmin(axis=1)
        for j in range(k):
            members = pixels[label == j]
            if members.shape[0]:
                c[j] = members.mean(axis=0)
    label = ((pixels[:, None, :] - c[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
    out = []
    for j in range(k):
        n = int((label == j).sum())
        if n:
            rgb = np.clip(c[j], 0.0, 1.0)
            out.append({"rgb": [round(float(x), 4) for x in rgb], "weight": round(n / pixels.shape[0], 4),
                        "hex": "#%02x%02x%02x" % tuple(int(round(x * 255)) for x in rgb)})
    return sorted(out, key=lambda x: -x["weight"])


def _luma_percentiles(hist_luma: np.ndarray) -> Dict[str, float]:
    total = float(hist_luma.sum())
    if total <= 0:
        return {}
    cum = np.cumsum(hist_luma) / total
    return {f"p{p}": round(float(np.searchsorted(cum, p / 100.0)) / 255.0, 4) for p in LUMA_PERCENTILES}


class LookAccumulator:
    """Collects colour samples while the structure pass decodes, then profiles any time range."""

    def __init__(self, fps: float = S.ANALYSIS_FPS, stride: int = SAMPLE_STRIDE) -> None:
        self.fps = float(fps)
        self.stride = max(1, int(stride))
        self.samples: List[Sample] = []

    def add(self, idx: int, rgb: np.ndarray, gray: np.ndarray) -> None:
        if idx % self.stride:
            return
        self.samples.append(make_sample(idx / self.fps, rgb, gray))

    def _in_range(self, start: float, end: float) -> List[Sample]:
        picked = [s for s in self.samples if start <= s.t < end]
        if picked or not self.samples:
            return picked
        mid = (start + end) / 2.0   # a shot shorter than the sampling gap uses its nearest sample
        return [min(self.samples, key=lambda s: abs(s.t - mid))]

    def profile(self, start: float, end: float) -> Optional[Dict[str, Any]]:
        picked = self._in_range(start, end)
        if not picked:
            return None
        scopes = [ca.scope_from_raw_video(s.scope()) for s in picked]
        profile = ca.build_look_profile(scopes)
        pooled = np.sum([s.hist[0].astype(np.int64) for s in picked], axis=0)
        sat_mean = float(np.median([s.sat_mean for s in picked]))
        hue = np.sum([s.hue for s in picked], axis=0)
        tones: Dict[str, Any] = {}
        for i, name in enumerate(("shadows", "mids", "highlights")):
            weight = np.array([s.tones[i][0] for s in picked], np.float64)
            if weight.sum() > 0:
                mean = np.average([s.tones[i][1] for s in picked], axis=0, weights=weight)
                tones[name] = {"mean_rgb": [round(float(x), 4) for x in mean],
                               "fraction": round(float(weight.sum() / sum(sum(t[0] for t in s.tones) for s in picked)), 4)}
            else:
                tones[name] = {"mean_rgb": None, "fraction": 0.0}
        pcts = _luma_percentiles(pooled)
        extras = {
            "saturation": {"mean": round(sat_mean, 4), "p90": round(float(np.median([s.sat_p90 for s in picked])), 4)},
            "hue_balance": [round(float(x), 4) for x in (hue / hue.sum() if hue.sum() > 0 else hue)],
            "tone_bands": tones,
            "luma_percentiles": pcts,
            "black_point": pcts.get("p1"), "white_point": pcts.get("p99"),
            "palette": kmeans_palette(np.concatenate([s.pixels for s in picked], axis=0)),
        }
        return {"profile": profile, "extras": extras, "samples": len(picked)}

    def finalize(self, shots: List[Dict[str, Any]], pipeline: Dict[str, Any]) -> Dict[str, Any]:
        """The look layer: per-shot profiles, the file profile and shot-to-shot variance."""
        per_shot = []
        for shot in shots:
            got = self.profile(shot["start"], shot["end"])
            per_shot.append({"id": shot["id"], "start": shot["start"], "end": shot["end"], **(got or {"samples": 0})})
        end = max([s["end"] for s in shots] + [self.samples[-1].t + 1.0 / self.fps if self.samples else 0.0])
        whole = self.profile(0.0, end + 1.0)
        variance: Dict[str, float] = {}
        usable = [p["profile"] for p in per_shot if p.get("profile", {}).get("present")]
        for key in ("avg_luma", "warm_cool", "sat_proxy", "contrast_span"):
            vals = [p[key] for p in usable if p.get(key) is not None]
            if len(vals) >= 2:
                variance[key] = round(float(np.std(vals)), 4)
        extras = (whole or {}).get("extras", {})
        flat = flat_look(extras)
        pipeline = dict(pipeline)
        pipeline["log_guess"] = bool(flat["flat"] and not pipeline.get("hdr"))
        pipeline["log_confidence"] = flat["confidence"] if pipeline["log_guess"] else 0.0
        pipeline["display_referred"] = not pipeline.get("hdr") and not pipeline["log_guess"]
        warnings = []
        if pipeline.get("hdr"):
            warnings.append("hdr_not_tonemapped: HDR footage was read as SDR, so its numbers are not display-referred")
        if pipeline["log_guess"]:
            warnings.append("looks_flat: narrow range and little colour; may be camera log or a low-contrast scene")
        return {
            "version": S.LAYER_VERSIONS[S.LAYER_LOOK],
            "pipeline": pipeline,
            "warnings": warnings,
            "sampling": {"fps": self.fps / self.stride, "frames": len(self.samples), "long_edge": S.ANALYSIS_LONG_EDGE},
            "file": {**(whole or {"samples": 0}), "shot_variance": variance, "kind": S.MEASURED},
            "shots": per_shot,
        }


def flat_look(extras: Dict[str, Any]) -> Dict[str, Any]:
    """Whether the whole file looks flat: a narrow luma range and little colour (inferred)."""
    if not extras:
        return {"flat": False, "confidence": 0.0}
    black, white = extras.get("black_point"), extras.get("white_point")
    sat = (extras.get("saturation") or {}).get("mean")
    pcts = extras.get("luma_percentiles") or {}
    if black is None or white is None or sat is None:
        return {"flat": False, "confidence": 0.0}
    span = (pcts.get("p95", white) - pcts.get("p5", black))
    flat = black >= FLAT_BLACK_FLOOR and white <= FLAT_WHITE_CEILING and sat <= FLAT_MAX_SAT and span <= FLAT_MAX_SPAN
    margins = [(black - FLAT_BLACK_FLOOR) / 0.1, (FLAT_WHITE_CEILING - white) / 0.15,
               (FLAT_MAX_SAT - sat) / 0.15, (FLAT_MAX_SPAN - span) / 0.2]
    confidence = float(np.clip(min(margins) * 0.5 + 0.5, 0.0, 1.0)) if flat else 0.0
    return {"flat": bool(flat), "confidence": round(confidence, 3)}


def pipeline_from_probe(probe: Dict[str, Any]) -> Dict[str, Any]:
    v = (probe or {}).get("video") or {}
    return {
        "primaries": v.get("color_primaries") or "unspecified",
        "transfer": v.get("color_transfer") or "unspecified",
        "matrix": v.get("color_space") or "unspecified",
        "range": v.get("color_range") or "unspecified",
        "bit_depth": int(v.get("bit_depth") or 8),
        "hdr": bool(v.get("hdr")),
        "kind": S.MEASURED,
    }
