"""Effects and color: objective frame analysis (the scopes, as numbers) for grading and checking effects.

Workstream: effects-color (see ``effects_color``). Frames are rendered on a
private libopenshot Timeline built from a snapshot of the project (the export
path, ``export_pipeline._clone_timeline``), on the calling worker thread; the
live preview timeline and the Qt GUI thread are never touched. Statistics
come from libopenshot's FrameScope (the same analysis the Histogram,
Waveform and Vectorscope docks draw).
"""

from __future__ import annotations

import copy
import json
import math
import os
from typing import Optional

from classes.editor_tools._base import (
    CLIP_TARGET,
    ToolError,
    boolean,
    clip_extent,
    clip_frame_at,
    get_app,
    mapping,
    nullable,
    number,
    obj,
    ok,
    playhead_seconds,
    resolve_clip,
    seconds_to_frame,
    string,
)
from classes.editor_tools._registry import editor_tool

# FrameScope's vectorscope axes (libopenshot FrameScope.cpp kVectorscopeUMax / VMax).
_U_MAX, _V_MAX = 0.436, 0.615


# ---------------------------------------------------------------------------
# Rendering (worker thread)
# ---------------------------------------------------------------------------

def project_snapshot() -> dict:
    """A deep copy of the live project data, taken without the GUI thread."""
    data = get_app().project._data
    last = None
    for _attempt in range(5):
        try:
            return json.loads(json.dumps(data))
        except RuntimeError as exc:  # the GUI thread mutated a dict mid-copy; retry
            last = exc
    raise ToolError(f"could not snapshot the project while it was being edited ({last})")


def _strip_effects(clip_data: dict, strip) -> dict:
    c = copy.deepcopy(clip_data)
    if strip == "all":
        c["effects"] = []
    elif strip:
        c["effects"] = [e for e in (c.get("effects") or []) if e.get("class_name") not in strip]
    return c


class RenderedFrame:
    """A rendered openshot.Frame plus the private timeline that owns its readers."""

    def __init__(self, frame, timeline, cache, clip=None):
        self.frame, self._timeline, self._cache, self._clip = frame, timeline, cache, clip

    def close(self):
        for obj_ in (self._clip, self._timeline):
            try:
                if obj_ is not None:
                    obj_.Close()
            except Exception:
                pass
        self._timeline = self._cache = self._clip = None


def render_frame(clip_data: Optional[dict], time_s: float, strip=None) -> RenderedFrame:
    """Render one frame at timeline time *time_s*.

    clip_data given: that clip alone with its effects (transparent where it is
    transparent), at its own resolution. None: the composite timeline frame.
    strip: None, "all", or a set of effect class names to leave out.
    """
    try:
        from classes.export_acceleration.export_pipeline import _clone_timeline
    except ImportError as exc:
        raise ToolError(f"frame rendering needs libopenshot ({exc})") from None
    proj = project_snapshot()
    fps = proj.get("fps") or {"num": 30, "den": 1}
    video = {"fps": fps, "width": int(proj.get("width") or 1920), "height": int(proj.get("height") or 1080)}
    audio = {"sample_rate": int(proj.get("sample_rate") or 48000), "channels": int(proj.get("channels") or 2),
             "channel_layout": int(proj.get("channel_layout") or 3)}
    if clip_data is not None:
        proj["clips"] = [_strip_effects(clip_data, strip)]
        proj["effects"] = []
    elif strip:
        proj["clips"] = [_strip_effects(c, strip) for c in proj.get("clips") or []]
    try:
        timeline, cache = _clone_timeline(proj, video, audio, 64 * 1024 * 1024)
        if clip_data is not None:
            clips = list(timeline.Clips())
            if not clips:
                raise ToolError("the clip could not be loaded for rendering (missing media?)")
            clip = clips[0]
            clip.Open()
            frame = clip.GetFrame(clip_frame_at(clip_data, time_s))
            return RenderedFrame(frame, timeline, cache, clip)
        frame = timeline.GetFrame(seconds_to_frame(time_s))
        return RenderedFrame(frame, timeline, cache)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"rendering the frame failed: {exc}") from None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def scope_data(frame, region: Optional[dict] = None) -> dict:
    """FrameScope histograms (luma/r/g/b, 256 bins) and vectorscope density for a frame or a region."""
    import openshot
    scope = openshot.FrameScope()
    scope.SetVectorscopeSize(128)
    if region:
        scope.SetVideoRegionNormalized(float(region["x"]), float(region["y"]),
                                       float(region["width"]), float(region["height"]))
    scope.SetFrame(frame)
    if not scope.HasVideo():
        raise ToolError("the rendered frame has no picture")
    w, h = int(frame.GetWidth()), int(frame.GetHeight())
    area = (region["width"] * region["height"]) if region else 1.0
    return {
        "luma": list(scope.GetVideoHistogramLuma()), "red": list(scope.GetVideoHistogramRed()),
        "green": list(scope.GetVideoHistogramGreen()), "blue": list(scope.GetVideoHistogramBlue()),
        "vectorscope": list(scope.GetVideoVectorscope()), "vectorscope_size": int(scope.GetVectorscopeSize()),
        "clipped_shadows": int(scope.GetVideoClippedShadows()),
        "clipped_highlights": int(scope.GetVideoClippedHighlights()),
        "total_pixels": int(round(w * h * area)), "width": w, "height": h,
    }


def _percentile(hist, q) -> float:
    total = sum(hist)
    if total <= 0:
        return 0.0
    target = q * total
    run = 0
    for i, n in enumerate(hist):
        run += n
        if run >= target:
            return i / (len(hist) - 1)
    return 1.0


def _mean(hist) -> float:
    total = sum(hist)
    return sum(i * n for i, n in enumerate(hist)) / total / (len(hist) - 1) if total else 0.0


def frame_pixels(frame, region: Optional[dict] = None, target_width: int = 160) -> list:
    """A small RGBA sample of *frame* ([(r, g, b, a)] 0-255), for per-pixel white balance and saturation.

    libopenshot saves a downscaled PNG to a temp file (worker thread); QImage reads it back.
    Returns [] when Qt is unavailable, so callers fall back to the scope histograms.
    """
    import tempfile
    try:
        from qt_api import QImage
    except ImportError:
        return []
    width = max(1, int(frame.GetWidth()))
    scale = min(1.0, float(target_width) / width)
    fd, path = tempfile.mkstemp(prefix="zenvi-frame-", suffix=".png")
    os.close(fd)
    try:
        frame.Save(path, scale, "PNG", 100)
        img = QImage(path)
        if img.isNull():
            return []
        img = img.convertToFormat(QImage.Format_RGBA8888)
        w, h, bpl = img.width(), img.height(), img.bytesPerLine()
        ptr = img.constBits()
        ptr.setsize(bpl * h)
        data = bytes(ptr)
    except Exception:
        return []
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    x0, y0, x1, y1 = 0, 0, w, h
    if region:
        x0, y0 = int(region["x"] * w), int(region["y"] * h)
        x1 = max(x0 + 1, int(round((region["x"] + region["width"]) * w)))
        y1 = max(y0 + 1, int(round((region["y"] + region["height"]) * h)))
    out = []
    for yy in range(y0, min(y1, h)):
        row = yy * bpl
        for xx in range(x0, min(x1, w)):
            o = row + xx * 4
            out.append((data[o], data[o + 1], data[o + 2], data[o + 3]))
    return out


def pixel_stats(pixels: list) -> Optional[dict]:
    """HSV saturation and the white balance of near-neutral mid-tones from an RGBA sample (pure)."""
    sats, neutral = [], [0.0, 0.0, 0.0, 0]
    for r, g, b, a in pixels:
        if a < 128:
            continue
        r, g, b = r / 255.0, g / 255.0, b / 255.0
        hi, lo = max(r, g, b), min(r, g, b)
        sat = (hi - lo) / hi if hi > 0 else 0.0
        sats.append(sat)
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        if 0.25 <= luma <= 0.92 and sat < 0.25:
            neutral[0] += r
            neutral[1] += g
            neutral[2] += b
            neutral[3] += 1
    if not sats:
        return None
    sats.sort()
    out = {"saturation_mean": round(sum(sats) / len(sats), 3),
           "saturation_p90": round(sats[min(len(sats) - 1, int(0.9 * len(sats)))], 3),
           "neutral_pct": round(100.0 * neutral[3] / len(sats), 1)}
    if neutral[3] >= max(20, 0.03 * len(sats)):
        nr, ng, nb = (neutral[i] / neutral[3] for i in range(3))
        out["neutral_rgb"] = [round(nr, 3), round(ng, 3), round(nb, 3)]
        out["neutral_warmth"] = round(nr - nb, 3)
        out["neutral_green_magenta"] = round(ng - (nr + nb) / 2.0, 3)
    return out


def compute_stats(d: dict, pixels: Optional[dict] = None) -> dict:
    """Numbers a colorist reads off the scopes: FrameScope data plus an optional pixel_stats() sample."""
    luma = d["luma"]
    counted = sum(luma)
    if counted <= 0:
        return {"transparent_pct": 100.0, "note": "every pixel is transparent"}
    pct = {f"p{int(q * 100)}": round(_percentile(luma, q), 3) for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)}
    mean_rgb = [round(_mean(d[c]), 3) for c in ("red", "green", "blue")]
    r, g, b = mean_rgb
    warmth = round(r - b, 3)
    green_magenta = round(g - (r + b) / 2.0, 3)
    size = int(d.get("vectorscope_size") or 0)
    vec = d.get("vectorscope") or []
    chroma_hist = [0] * 101  # 0 .. 0.5 UV magnitude in 0.005 steps
    nu = nv = nn = 0.0  # near-neutral pixels (whites, greys): their tint is the white balance
    if size > 1 and len(vec) == size * size:
        c = (size - 1) / 2.0
        for idx, n in enumerate(vec):
            if not n:
                continue
            y, x = divmod(idx, size)
            u = (x - c) / c * _U_MAX
            v = (c - y) / c * _V_MAX
            chroma = math.hypot(u, v)
            chroma_hist[min(100, int(round(chroma / 0.005)))] += n
            if chroma < 0.06:
                nu, nv, nn = nu + u * n, nv + v * n, nn + n
    chroma_total = sum(chroma_hist)
    chroma_mean = sum(i * 0.005 * n for i, n in enumerate(chroma_hist)) / chroma_total if chroma_total else 0.0
    gray = sum(chroma_hist[:7]) / chroma_total if chroma_total else 0.0
    cast, neutral_uv = "neutral", None
    if pixels and "neutral_warmth" in pixels:
        w, gm = pixels["neutral_warmth"], pixels["neutral_green_magenta"]
        if max(abs(w), abs(gm)) >= 0.025:
            if abs(w) >= abs(gm):
                cast = "warm (orange)" if w > 0 else "cool (blue)"
            else:
                cast = "green" if gm > 0 else "magenta"
    elif chroma_total and nn / chroma_total >= 0.05:
        # U > 0 = blue, U < 0 = yellow/orange; V > 0 = red/magenta, V < 0 = green.
        neutral_uv = (nu / nn, nv / nn)
        mu, mv = neutral_uv
        if max(abs(mu), abs(mv)) >= 0.008:
            if abs(mu) >= abs(mv):
                cast = "cool (blue)" if mu > 0 else "warm (orange)"
            else:
                cast = "magenta" if mv > 0 else "green"
    elif abs(warmth) >= 0.04 or abs(green_magenta) >= 0.04:  # few neutral pixels: fall back to mean RGB
        if abs(warmth) >= abs(green_magenta):
            cast = "warm (orange)" if warmth > 0 else "cool (blue)"
        else:
            cast = "green" if green_magenta > 0 else "magenta"
    total = max(int(d.get("total_pixels") or counted), counted)
    stats = {
        "luma": {**pct, "mean": round(_mean(luma), 3)},
        "clipped_shadows_pct": round(100.0 * d.get("clipped_shadows", 0) / counted, 2),
        "clipped_highlights_pct": round(100.0 * d.get("clipped_highlights", 0) / counted, 2),
        "mean_rgb": mean_rgb,
        "warmth": warmth,
        "green_magenta": green_magenta,
        "cast": cast,
        "neutral_tint_uv": [round(neutral_uv[0], 4), round(neutral_uv[1], 4)] if neutral_uv else None,
        "chroma_mean": round(chroma_mean, 4),
        "chroma_p90": round(_percentile(chroma_hist, 0.9) * 0.5, 4),
        "gray_pct": round(100.0 * gray, 1),
        "transparent_pct": round(100.0 * (1.0 - counted / float(total)), 1),
    }
    if pixels:
        stats.update(pixels)
    stats["verdict"] = _verdict(stats)
    stats["suggested_grade"] = _suggest(stats)
    return stats


def _verdict(s: dict) -> list:
    out = []
    p50, p95, p5 = s["luma"]["p50"], s["luma"]["p95"], s["luma"]["p5"]
    if p50 < 0.28:
        out.append("underexposed (median luma %.2f)" % p50)
    elif p50 > 0.65:
        out.append("overexposed (median luma %.2f)" % p50)
    else:
        out.append("exposure ok (median luma %.2f)" % p50)
    if s["clipped_highlights_pct"] > 2:
        out.append("highlights clipped (%.1f%%)" % s["clipped_highlights_pct"])
    if s["clipped_shadows_pct"] > 5:
        out.append("crushed shadows (%.1f%%)" % s["clipped_shadows_pct"])
    if p95 - p5 < 0.4:
        out.append("flat / low contrast (p5-p95 %.2f)" % (p95 - p5))
    elif p95 - p5 > 0.9:
        out.append("very high contrast")
    sat = s.get("saturation_mean")
    if s["gray_pct"] > 97:
        out.append("black and white")
    elif (sat is not None and sat < 0.12) or (sat is None and s["chroma_mean"] < 0.03):
        out.append("muted colors")
    elif (sat is not None and sat > 0.55) or (sat is None and s["chroma_mean"] > 0.14):
        out.append("very saturated")
    if s["cast"] != "neutral":
        out.append(f"{s['cast']} cast")
    return out


def _suggest(s: dict) -> dict:
    """Heuristic color_grade_clip_tool values toward a neutral, well-exposed frame."""
    out = {}
    p50 = s["luma"]["p50"]
    if (p50 < 0.3 or p50 > 0.62) and p50 > 0.005:
        out["exposure"] = round(max(-1.5, min(1.5, 0.8 * math.log2(0.45 / p50))), 2)
    if s["clipped_highlights_pct"] > 2:
        out["highlights"] = -0.25
    if s["clipped_shadows_pct"] > 5:
        out["shadows"] = 0.15
    if s["luma"]["p95"] - s["luma"]["p5"] < 0.4:
        out["contrast"] = 0.2
    uv = s.get("neutral_tint_uv")
    if "neutral_warmth" in s:
        if s["cast"] != "neutral":
            if abs(s["neutral_warmth"]) >= 0.025:
                out["temperature"] = round(max(-0.4, min(0.4, -3.0 * s["neutral_warmth"])), 2)
            if abs(s["neutral_green_magenta"]) >= 0.025:
                out["tint"] = round(max(-0.4, min(0.4, 4.0 * s["neutral_green_magenta"])), 2)
    elif uv and s["cast"] != "neutral":
        if abs(uv[0]) >= 0.008:
            out["temperature"] = round(max(-0.4, min(0.4, 12.0 * uv[0])), 2)
        if abs(uv[1]) >= 0.008:
            out["tint"] = round(max(-0.4, min(0.4, -12.0 * uv[1])), 2)
    elif s["cast"] != "neutral":
        if abs(s["warmth"]) >= 0.04:
            out["temperature"] = round(max(-0.4, min(0.4, -2.0 * s["warmth"])), 2)
        if abs(s["green_magenta"]) >= 0.04:
            out["tint"] = round(max(-0.4, min(0.4, 3.0 * s["green_magenta"])), 2)
    if "muted colors" in (s.get("verdict") or _verdict(s)):
        out["saturation"] = 1.25
    return out


def _region_arg(region) -> Optional[dict]:
    if not region:
        return None
    if not isinstance(region, dict):
        raise ToolError('region is {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5} (fractions of the frame)')
    try:
        r = {k: float(region[k]) for k in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        raise ToolError("region needs x, y, width, height as fractions 0-1 of the frame") from None
    if not (0 <= r["x"] < 1 and 0 <= r["y"] < 1 and 0 < r["width"] <= 1 and 0 < r["height"] <= 1
            and r["x"] + r["width"] <= 1.0001 and r["y"] + r["height"] <= 1.0001):
        raise ToolError("region must lie inside the frame (x, y, width, height are fractions 0-1)")
    return r


def _analysis_time(clip_data: Optional[dict], time) -> float:
    if clip_data is None:
        return float(time) if time is not None else playhead_seconds()
    start, end, duration = clip_extent(clip_data)
    if time is None:
        return start + duration / 2.0
    t = float(time)
    if not start - 1e-6 <= t <= end + 1e-6:
        raise ToolError(f"time {t:g}s is outside the clip ({start:g}-{end:g}s on the timeline)")
    return min(t, end - 1e-3) if end > start else start


@editor_tool(
    "analyze_frame_colors_tool",
    label="Analyze colors",
    schema=obj({
        **CLIP_TARGET,
        "time": nullable(number("Timeline seconds to analyze (default: the clip's middle, or the playhead for the "
                                "whole timeline).")),
        "whole_timeline": boolean("Analyze the composite of every track at `time` instead of one clip alone.",
                                  False),
        "region": mapping("Only this part of the frame: {\"x\", \"y\", \"width\", \"height\"} as fractions 0-1 "
                          "(e.g. a face or the sky)."),
        "compare_without_effects": boolean("Also analyze the same frame with the effects removed (before/after).",
                                           False),
        "save_frame_path": string("Also save the analyzed frame as a PNG at this absolute path (to look at it).",
                                  ""),
    }),
    read_only=True,
    covers=("color.analyze",),
)
def analyze_frame_colors(timeline_clip_id="", clip_query="", track="", time=None, whole_timeline=False,
                         region=None, compare_without_effects=False, save_frame_path=""):
    """Measure a frame the way the scopes do, as numbers, to grade objectively and to check that an
    effect or grade did what was asked: luma percentiles p1-p99 and mean (0 = black, 1 = white),
    clipped shadows/highlights %, mean RGB, warmth (R-B), green/magenta balance, the white-balance cast
    of near-neutral pixels (neutral_tint_uv, cast label),
    saturation_mean (HSV 0-1: <0.12 muted, 0.2-0.4 natural, >0.55 very saturated), chroma (UV),
    gray_pct (100 = black and white), transparent_pct (share keyed out / transparent), a plain-words verdict and
    suggested_grade values for color_grade_clip_tool. Renders off the GUI thread; changes nothing.

    Use it before grading ("it's too dark", "fix the colors", "match the look") and after, to verify
    ("is it black and white now?", "did the green screen key cleanly?"). With a clip it renders that
    clip alone with its effects; whole_timeline=true measures what the viewer sees at `time`.
    compare_without_effects gives before/after numbers. save_frame_path writes a PNG you can view.
    """
    region_r = _region_arg(region)
    clip = None
    if not whole_timeline:
        try:
            clip = resolve_clip(timeline_clip_id, clip_query, track)
        except ToolError:
            if timeline_clip_id or clip_query:
                raise
            whole_timeline = True
    clip_data = copy.deepcopy(clip.data) if clip is not None else None
    t = _analysis_time(clip_data, time)
    if save_frame_path:
        if not os.path.isabs(save_frame_path):
            raise ToolError("save_frame_path must be an absolute path ending in .png")
        parent = os.path.dirname(save_frame_path)
        if not os.path.isdir(parent):
            raise ToolError(f"folder {parent} does not exist")
    rendered = render_frame(clip_data, t)
    try:
        stats = compute_stats(scope_data(rendered.frame, region_r), pixel_stats(frame_pixels(rendered.frame, region_r)))
        if save_frame_path:
            rendered.frame.Save(save_frame_path, 1.0, "PNG", 100)
        size = [int(rendered.frame.GetWidth()), int(rendered.frame.GetHeight())]
    finally:
        rendered.close()
    receipt = {"time": round(t, 3), "target": f"clip {clip.id}" if clip is not None else "timeline",
               "frame_size": size, "stats": stats}
    if clip is not None:
        receipt["timeline_clip_id"] = clip.id
        receipt["effects"] = [e.get("class_name") for e in (clip.data.get("effects") or []) if isinstance(e, dict)]
    if region_r:
        receipt["region"] = region_r
    if compare_without_effects:
        before = render_frame(clip_data, t, strip="all")
        try:
            receipt["without_effects"] = compute_stats(scope_data(before.frame, region_r),
                                                       pixel_stats(frame_pixels(before.frame, region_r)))
        finally:
            before.close()
    if save_frame_path:
        receipt["saved_frame"] = save_frame_path
    where = f"clip {clip.id}" if clip is not None else "the timeline"
    return ok(f"Frame at {t:.2f}s of {where}: " + "; ".join(stats.get("verdict") or [stats.get("note", "")]),
              **receipt)


# ---------------------------------------------------------------------------
# Chroma key helper
# ---------------------------------------------------------------------------

_EDGE_STRIPS = ({"x": 0.0, "y": 0.0, "width": 0.08, "height": 1.0},
                {"x": 0.92, "y": 0.0, "width": 0.08, "height": 1.0},
                {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.08})


def screen_color_from_histograms(hists: list) -> tuple:
    """Per-channel medians of the edge strips -> (#rrggbb, 'green'|'blue'|None)."""
    med = []
    for channel in ("red", "green", "blue"):
        total = [0] * 256
        for h in hists:
            for i, n in enumerate(h[channel]):
                total[i] += n
        med.append(int(round(_percentile(total, 0.5) * 255)))
    r, g, b = med
    kind = None
    if g > 60 and g - max(r, b) >= 30:
        kind = "green"
    elif b > 60 and b - max(r, g) >= 30:
        kind = "blue"
    return "#%02x%02x%02x" % (r, g, b), kind


def sample_screen_color(clip, sample_time=None) -> tuple:
    """(#rrggbb, detail) of the green/blue screen at the clip's frame edges; ToolError if none."""
    data = copy.deepcopy(clip.data)
    t = _analysis_time(data, sample_time)
    rendered = render_frame(data, t, strip={"ChromaKey"})
    try:
        hists = [scope_data(rendered.frame, strip) for strip in _EDGE_STRIPS]
    finally:
        rendered.close()
    color, kind = screen_color_from_histograms(hists)
    if kind is None:
        raise ToolError(f"the edges of clip {clip.id} at {t:.2f}s are {color}, not a green or blue screen; "
                        "pass key_color ('green', 'blue' or #RRGGBB)")
    return color, f"{kind} screen at {t:.2f}s"
