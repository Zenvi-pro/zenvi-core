"""Zenvi (libopenshot) clip effects as After Effects effects, masks and warnings.

Each mapping names the AE effect by match name (locale independent) and sets
parameters by parameter match name first, then by English display name, so
an unverified id still works in an English After Effects and a missing one
warns instead of failing. Parameter ids marked *verified* were read from
After Effects-generated exports (Lottie files written by Bodymovin, which
store every effect parameter's match name next to its display name; e.g.
Skia's ``resources/skottie`` test corpus); the others follow the
``"<effect match name>-<4-digit id>"`` scheme After Effects uses
(After Effects Scripting Guide, PropertyGroup.addProperty example) and carry
a display-name fallback.

==================  =============================================  ==========================================
Zenvi effect         After Effects                                  Conversion
==================  =============================================  ==========================================
Blur                 Gaussian Blur (``ADBE Gaussian Blur 2``)       libopenshot runs ``iterations`` box blurs
                     *verified* -0001 Blurriness, -0002 Blur        of radius r (Blur.cpp): sigma^2 = n r(r+1)/3
                     Dimensions, -0003 Repeat Edge Pixels           at decode size; Blurriness = sigma / 0.3
                                                                     (Skia Skottie's AE-matched constant), in
                                                                     source pixels; different H/V radii = two
                                                                     one-direction blurs (separable). Repeat
                                                                     Edge Pixels on (libopenshot clamps).
Brightness           Brightness & Contrast (``ADBE Brightness &     Use Legacy on (linear like libopenshot);
                     Contrast 2``) *verified* -0001/-0002/-0003     brightness 255*b, contrast 100*c/128
                                                                     (approximate, clamped to AE's ranges).
Saturation           Hue/Saturation (``ADBE HUE SATURATION``)       Master Saturation (s - 1) * 100;
                     *verified* -0005 Master Saturation             per-channel R/G/B saturation warns.
Hue                  Hue/Saturation *verified* -0004 Master Hue    360 * hue degrees.
Negate               Invert (``ADBE Invert``) *verified* -0001      Channel RGB (menu value 1), no blend.
Pixelate             Mosaic (``ADBE Mosaic``)                       Horizontal blocks = decode width * 0.001^p
                                                                     (Pixelate.cpp scales to that width).
Sharpen              Sharpen (``ADBE Sharpen``) *verified* -0001    Amount 0..40 -> 0..100 (approximate).
ChromaKey            Keylight (``Keylight 906``), else Color Key    Screen Colour / Key Color = key colour;
                     (``ADBE Color Key``)                           Color Key tolerance = fuzz (approximate).
Crop                 a layer mask (Add)                             The visible rectangle in source pixels;
                                                                     x/y offsets and Resize warn.
ColorGrade           Lumetri Color (``ADBE Lumetri``), Basic        Exposure, Contrast, Highlights, Shadows,
                     Correction by display name                     Saturation, Vibrance, Temperature, Tint
                                                                     (approximate); curves, wheels, LUT, Mix
                                                                     warn (LUT path in the warning).
others               --                                             Warning + listed in the layer comment.
==================  =============================================  ==========================================

Every parameter is evaluated at every frame the clip shows: a parameter that
is an affine function of one animated curve keeps that curve's eases; any
other animated parameter is sampled per frame (``after_effects_keys``).

Pure Python: no Qt, no project access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from classes.exporters import after_effects_keys as K
from classes.handoff.keyframes import Curve, float32
from classes.handoff.transform import SCALE_CROP, SCALE_FIT, SCALE_NONE, SCALE_STRETCH, delivered_size

# Skia Skottie (modules/skottie/src/SkottiePriv.h): "Close-enough to AE".
AE_BLUR_SIZE_TO_SIGMA = 0.3


@dataclass
class EffectContext:
    """What a mapping needs about the clip: frames, the decode scale, the source size, and a warning sink."""

    frames: List[float]
    t_in: float
    t_out: float
    decode_scale: float           # libopenshot decode width / natural width (pixel-based params)
    decode_width: float           # the image width libopenshot's effects see
    decode_height: float
    source_width: float           # the AE layer's source size (natural pixels)
    source_height: float
    clip_name: str
    warn: Callable[[str], None]


@dataclass
class MappedEffects:
    effects: List[dict] = field(default_factory=list)   # {"label", "try": [{"match", "name", "params"}], "on"}
    masks: List[dict] = field(default_factory=list)     # {"name", "inv", "shape"}
    unmapped: List[str] = field(default_factory=list)   # effect names AE gets no equivalent for
    approximate: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curve(effect, key: str, default: float) -> Curve:
    value = effect.params.get(key)
    if isinstance(value, Curve):
        return value
    try:
        number = float(value) if value is not None and not isinstance(value, (dict, list)) else default
    except (TypeError, ValueError):
        number = default
    return Curve.constant(number)


def _samples(ctx: EffectContext, curve: Curve) -> List[float]:
    return [K.exact_value(curve, t) for t in ctx.frames]


def _param(ctx: EffectContext, ids: Sequence[str], values: Sequence[float], curve: Optional[Curve],
           tol: float, *, verified: bool = False) -> dict:
    """A parameter entry: ``ids`` to try in order and the static value or keys."""
    built = K.build_property(ctx.frames, [K.Dimension(list(values), curve)], t_in=ctx.t_in, t_out=ctx.t_out,
                             tol=[tol])
    entry: Dict[str, Any] = {"ids": list(ids), "val": keys_spec(built.keys)}
    if verified:
        entry["ok"] = 1
    return entry


def _const(ids: Sequence[str], value: Any, verified: bool = False) -> dict:
    entry: Dict[str, Any] = {"ids": list(ids), "val": value}
    if verified:
        entry["ok"] = 1
    return entry


def keys_spec(keys: K.PropertyKeys) -> Any:
    """Static value (number or list) or a keyframe dict for the script runtime."""
    if isinstance(keys, K.Track):
        spec: Dict[str, Any] = {
            "t": keys.times,
            "v": [v[0] if len(v) == 1 else list(v) for v in keys.values],
            "i": keys.kinds,
        }
        if any(kind == K.BEZIER for kind in keys.kinds):
            spec["o"] = [[[s, i * 100.0] for s, i in span] for span in keys.outs]
            spec["n"] = [[[s, i * 100.0] for s, i in span] for span in keys.ins]
        if keys.spatial or (keys.sampled and keys.dims > 1):
            spec["sp"] = 1  # keyed as one spatial property (straight motion path) when the property is spatial
        return spec
    return keys[0] if len(keys) == 1 else list(keys)


def _effect(label: str, match: str, params: List[dict], *, name: str = "", enabled: bool = True,
            alternatives: Sequence[Tuple[str, List[dict]]] = ()) -> dict:
    tries = [{"match": match, "params": params}] + [{"match": m, "params": p} for m, p in alternatives]
    out: Dict[str, Any] = {"label": label, "try": tries}
    if name:
        out["name"] = name
    if not enabled:
        out["on"] = False
    return out


def _nonzero(ctx: EffectContext, effect, keys: Sequence[str]) -> bool:
    return any(abs(v) > 1e-9 for k in keys for v in _samples(ctx, _curve(effect, k, 0.0)))


def _color(effect, key: str) -> Optional[Tuple[float, float, float, float]]:
    value = effect.params.get(key)
    if not isinstance(value, Mapping):  # a dict of channel Curves (a MappingProxy in snapshots)
        return None
    out = []
    for channel, default in (("red", 0.0), ("green", 0.0), ("blue", 0.0), ("alpha", 255.0)):
        c = value.get(channel)
        out.append((c.first_value if isinstance(c, Curve) else default) / 255.0)
    return tuple(max(0.0, min(1.0, x)) for x in out)  # type: ignore[return-value]


def _get_int(value: float) -> int:
    """libopenshot GetInt: round half away from zero, then truncate."""
    return int(math.floor(abs(value) + 0.5)) * (1 if value >= 0 else -1)


# ---------------------------------------------------------------------------
# libopenshot decode size (pixel-based parameters)
# ---------------------------------------------------------------------------

def _max_point(curve: Optional[Curve]) -> float:
    if curve is None or not curve.points:
        return 1.0
    return max(p.value for p in curve.points)


def decode_size(clip, file, canvas_w: int, canvas_h: int) -> Tuple[float, float]:
    """The image size libopenshot's effects see for *clip* (QtImageReader / FFmpegReader max size).

    Ports the engine's ``maxBox`` / ``deliveredSource`` (libopenshot
    QtImageReader::calculate_max_size, FFmpegReader decode size) for a final
    render at the project size: stills scale to the box both ways, video
    only shrinks, and only when smaller than the stream on both axes.
    SCALE_NONE is ``transform.delivered_size``.
    """
    w = float(getattr(file, "width", 0) or 0)
    h = float(getattr(file, "height", 0) or 0)
    if w <= 0 or h <= 0:
        return float(canvas_w), float(canvas_h)
    sx = _max_point(clip.curves.get("scale_x"))
    sy = _max_point(clip.curves.get("scale_y"))
    mode = clip.scale_mode
    still = bool(getattr(file, "is_still", False) or getattr(file, "is_title", False))
    if mode == SCALE_NONE:
        dw, dh = delivered_size(int(w), int(h), sx, sy, still=still)
        return float(dw), float(dh)
    if mode in (SCALE_FIT, SCALE_STRETCH):
        box_w = math.trunc(max(canvas_w, float32(canvas_w * sx)))
        box_h = math.trunc(max(canvas_h, float32(canvas_h * sy)))
    elif mode == SCALE_CROP:
        ratio_wh, ratio_hw = w / h, h / w
        width_size = (math.trunc(canvas_w * sx), int(math.floor(canvas_w / ratio_wh + 0.5)))
        height_size = (int(math.floor(canvas_h / ratio_hw + 0.5)), math.trunc(canvas_h * sy))
        if width_size[0] >= canvas_w and width_size[1] >= canvas_h:
            box_w, box_h = max(canvas_w, width_size[0]), max(canvas_h, width_size[1])
        else:
            box_w, box_h = max(canvas_w, height_size[0]), max(canvas_h, height_size[1])
    else:
        return w, h
    if still:
        rw = int((box_h * w) / h)
        if rw <= box_w:
            return float(rw), float(box_h)
        return float(box_w), float(int((box_w * h) / w))
    if box_w < w and box_h < h:
        ratio = w / h
        possible_w = int(math.floor(box_h * ratio + 0.5))
        if possible_w <= box_w:
            return float(possible_w), float(box_h)
        return float(box_w), float(int(math.floor(box_w / ratio + 0.5)))
    return w, h


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

def _blur(effect, ctx: EffectContext, out: MappedEffects) -> None:
    h_curve, v_curve = _curve(effect, "horizontal_radius", 6.0), _curve(effect, "vertical_radius", 6.0)
    it_curve = _curve(effect, "iterations", 3.0)
    hs, vs, its = _samples(ctx, h_curve), _samples(ctx, v_curve), _samples(ctx, it_curve)

    def sigma(r: float, n: float) -> float:
        r_i = max(0, math.trunc(r))
        n_i = max(0, _get_int(n))
        return math.sqrt(n_i * r_i * (r_i + 1) / 3.0) / max(ctx.decode_scale, 1e-9)

    bh = [sigma(r, n) / AE_BLUR_SIZE_TO_SIGMA for r, n in zip(hs, its)]
    bv = [sigma(r, n) / AE_BLUR_SIZE_TO_SIGMA for r, n in zip(vs, its)]
    if _nonzero(ctx, effect, ("left", "top", "right", "bottom")):
        ctx.warn(f"{ctx.clip_name}: Blur only covers part of the frame in Zenvi (margins); After Effects blurs "
                 "the whole layer")
    ids = ("ADBE Gaussian Blur 2-0001", "Blurriness")
    dims = ("ADBE Gaussian Blur 2-0002", "Blur Dimensions")
    edge = _const(("ADBE Gaussian Blur 2-0003", "Repeat Edge Pixels"), 1, True)
    animated_h = h_curve if h_curve.is_animated else (it_curve if it_curve.is_animated else None)
    animated_v = v_curve if v_curve.is_animated else (it_curve if it_curve.is_animated else None)
    if all(abs(a - b) < 1e-9 for a, b in zip(bh, bv)):
        if max(bh) <= 0:
            return
        out.effects.append(_effect("Blur", "ADBE Gaussian Blur 2", [
            _param(ctx, ids, bh, animated_h, 0.01, verified=True), _const(dims, 1, True), edge], name="Blur"))
        return
    if max(bh) > 0:
        out.effects.append(_effect("Blur (horizontal)", "ADBE Gaussian Blur 2", [
            _param(ctx, ids, bh, animated_h, 0.01, verified=True), _const(dims, 2, True), edge],
            name="Blur horizontal"))
    if max(bv) > 0:
        out.effects.append(_effect("Blur (vertical)", "ADBE Gaussian Blur 2", [
            _param(ctx, ids, bv, animated_v, 0.01, verified=True), _const(dims, 3, True), edge],
            name="Blur vertical"))
    out.approximate.append("Blur")


def _brightness(effect, ctx: EffectContext, out: MappedEffects) -> None:
    b_curve, c_curve = _curve(effect, "brightness", 0.0), _curve(effect, "contrast", 3.0)
    bright = [max(-150.0, min(150.0, 255.0 * v)) for v in _samples(ctx, b_curve)]
    contrast = [max(-100.0, min(100.0, 100.0 * v / 128.0)) for v in _samples(ctx, c_curve)]
    out.effects.append(_effect("Brightness & Contrast", "ADBE Brightness & Contrast 2", [
        _const(("ADBE Brightness & Contrast 2-0003", "Use Legacy (supports HDR)", "Use Legacy"), 1, True),
        _param(ctx, ("ADBE Brightness & Contrast 2-0001", "Brightness"), bright, b_curve, 0.01, verified=True),
        _param(ctx, ("ADBE Brightness & Contrast 2-0002", "Contrast"), contrast, c_curve, 0.01, verified=True),
    ], name="Brightness & Contrast"))
    out.approximate.append("Brightness")


def _saturation(effect, ctx: EffectContext, out: MappedEffects) -> None:
    s_curve = _curve(effect, "saturation", 1.0)
    values = [max(-100.0, min(100.0, (v - 1.0) * 100.0)) for v in _samples(ctx, s_curve)]
    if any(abs(v - 1.0) > 1e-6 for key in ("saturation_R", "saturation_G", "saturation_B")
           for v in _samples(ctx, _curve(effect, key, 1.0))):
        ctx.warn(f"{ctx.clip_name}: Saturation per colour channel (R/G/B) has no Hue/Saturation equivalent; "
                 "only the overall saturation was exported")
    out.effects.append(_effect("Saturation", "ADBE HUE SATURATION", [
        _param(ctx, ("ADBE HUE SATURATION-0005", "Master Saturation"), values, s_curve, 0.01, verified=True),
    ], name="Saturation"))
    out.approximate.append("Saturation")


def _hue(effect, ctx: EffectContext, out: MappedEffects) -> None:
    h_curve = _curve(effect, "hue", 0.0)
    values = [360.0 * v for v in _samples(ctx, h_curve)]
    out.effects.append(_effect("Hue", "ADBE HUE SATURATION", [
        _param(ctx, ("ADBE HUE SATURATION-0004", "Master Hue"), values, h_curve, 0.001, verified=True),
    ], name="Hue"))
    out.approximate.append("Hue")


def _negate(effect, ctx: EffectContext, out: MappedEffects) -> None:
    out.effects.append(_effect("Negate", "ADBE Invert", [
        _const(("ADBE Invert-0001", "Channel"), 1, True),
        _const(("ADBE Invert-0002", "Blend With Original"), 0, True),
    ], name="Negate"))


def _pixelate(effect, ctx: EffectContext, out: MappedEffects) -> None:
    p_curve = _curve(effect, "pixelization", 0.5)
    w, h = max(1.0, ctx.decode_width), max(1.0, ctx.decode_height)
    hb, vb = [], []
    for p in _samples(ctx, p_curve):
        value = min(math.pow(0.001, abs(p)), 1.0)
        blocks = max(1, math.trunc(w * value))
        hb.append(float(blocks))
        vb.append(float(max(1, math.trunc(blocks * h / w + 0.9999))))
    if _nonzero(ctx, effect, ("left", "top", "right", "bottom")):
        ctx.warn(f"{ctx.clip_name}: Pixelate only covers part of the frame in Zenvi (margins); Mosaic covers "
                 "the whole layer")
    out.effects.append(_effect("Pixelate", "ADBE Mosaic", [
        _param(ctx, ("ADBE Mosaic-0001", "Horizontal Blocks"), hb, None, 0.5),
        _param(ctx, ("ADBE Mosaic-0002", "Vertical Blocks"), vb, None, 0.5),
        _const(("ADBE Mosaic-0003", "Sharp Colors"), 0),
    ], name="Pixelate"))


def _sharpen(effect, ctx: EffectContext, out: MappedEffects) -> None:
    a_curve = _curve(effect, "amount", 10.0)
    values = [max(0.0, min(100.0, 2.5 * v)) for v in _samples(ctx, a_curve)]
    out.effects.append(_effect("Sharpen", "ADBE Sharpen", [
        _param(ctx, ("ADBE Sharpen-0001", "Sharpen Amount"), values, a_curve, 0.01, verified=True),
    ], name="Sharpen"))
    out.approximate.append("Sharpen")


def _chroma_key(effect, ctx: EffectContext, out: MappedEffects) -> None:
    color = _color(effect, "color") or (0.0, 1.0, 0.0, 1.0)
    fuzz = _samples(ctx, _curve(effect, "fuzz", 20.0))
    keylight = [_const(("Screen Colour", "Keylight 906-0002"), list(color))]
    color_key = [
        _const(("ADBE Color Key-0001", "Key Color"), list(color)),
        _const(("ADBE Color Key-0002", "Color Tolerance"), max(0.0, min(255.0, fuzz[0] if fuzz else 20.0))),
    ]
    out.effects.append(_effect("Chroma key", "Keylight 906", keylight, name="Chroma Key",
                               alternatives=[("ADBE Color Key", color_key)]))
    out.approximate.append("ChromaKey")


def _crop(effect, ctx: EffectContext, out: MappedEffects) -> None:
    sw, sh = ctx.source_width, ctx.source_height
    curves = [_curve(effect, k, 0.0) for k in ("left", "top", "right", "bottom")]
    if _nonzero(ctx, effect, ("x", "y")):
        ctx.warn(f"{ctx.clip_name}: Crop offsets (x/y) are not exported; only the crop rectangle is")
    resize = effect.params.get("resize")
    if resize not in (None, False, 0, "0", "false"):
        ctx.warn(f"{ctx.clip_name}: Crop 'Resize' is not exported; the layer keeps its size with a crop mask")
    rects = []
    for t in ctx.frames:
        left, top, right, bottom = (max(0.0, min(1.0, K.exact_value(c, t))) for c in curves)
        x0, y0 = left * sw, top * sh
        x1, y1 = max(x0, (1.0 - right) * sw), max(y0, (1.0 - bottom) * sh)
        rects.append((x0, y0, x1, y1))
    model = [tuple(r) for r in rects]
    if K.is_constant(model, [0.01] * 4):
        shape: Any = _rect_shape(*rects[0])
    else:
        keep = K.simplify(ctx.frames, model, [0.05] * 4)
        shape = {"t": [ctx.frames[k] for k in keep], "v": [_rect_shape(*rects[k]) for k in keep]}
    out.masks.append({"name": "Crop", "shape": shape})


def _rect_shape(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"pts": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "closed": True}


def _color_grade(effect, ctx: EffectContext, out: MappedEffects) -> None:
    params = []
    table = (
        ("exposure", 0.0, ("Exposure",), lambda v: max(-5.0, min(5.0, v))),
        ("contrast", 0.0, ("Contrast",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
        ("highlights", 0.0, ("Highlights",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
        ("shadows", 0.0, ("Shadows",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
        ("saturation", 1.0, ("Saturation",), lambda v: max(0.0, min(200.0, 100.0 * v))),
        ("vibrance", 0.0, ("Vibrance",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
        ("temperature", 0.0, ("Temperature",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
        ("tint", 0.0, ("Tint",), lambda v: max(-100.0, min(100.0, 100.0 * v))),
    )
    for key, neutral, ids, convert in table:
        curve = _curve(effect, key, neutral)
        values = [convert(v) for v in _samples(ctx, curve)]
        if all(abs(v - convert(neutral)) < 1e-6 for v in values):
            continue
        params.append(_param(ctx, ids, values, curve, 0.01))
    lut = str(effect.params.get("lut_path") or "").strip()
    if lut:
        ctx.warn(f"{ctx.clip_name}: Colour grade LUT is not applied automatically; in Lumetri Color > Creative > "
                 f"Look, choose Browse and pick {lut}")
    if any(abs(v - 1.0) > 1e-6 for v in _samples(ctx, _curve(effect, "mix", 1.0))):
        ctx.warn(f"{ctx.clip_name}: Colour grade Mix below 100% is not exported (Lumetri applies fully)")
    for key in ("curve_all", "curve_red", "curve_green", "curve_blue", "wheels"):
        if _grade_extra_set(key, effect.params.get(key)):
            ctx.warn(f"{ctx.clip_name}: Colour grade {key.replace('_', ' ')} is not exported; recreate it in "
                     "Lumetri Color > Curves / Color Wheels")
    if params:
        out.effects.append(_effect("Colour grade", "ADBE Lumetri", params, name="Colour Grade (Zenvi)"))
    out.approximate.append("ColorGrade")


def _kf_values(value: Any) -> List[float]:
    """Every value a keyframe dict (or bare number) takes; [] for anything else."""
    if isinstance(value, Curve):
        return [p.value for p in value.points] or [value.default]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if isinstance(value, dict) and isinstance(value.get("Points"), list):
        out = []
        for p in value["Points"]:
            y = (p.get("co") or {}).get("Y") if isinstance(p, dict) else None
            if isinstance(y, (int, float)) and not isinstance(y, bool):
                out.append(float(y))
        return out
    return []


def _curve_is_neutral(value: Any) -> bool:
    """A Color Grade curve (``color_presets.curve_data``) that is off or lies on the identity diagonal."""
    if not isinstance(value, dict):
        return value in (None, "", {})
    enabled = _kf_values(value.get("enabled"))
    if enabled and all(v < 0.5 for v in enabled):
        return True
    for node in value.get("nodes") or []:
        if not isinstance(node, dict):
            return False
        xs, ys = _kf_values(node.get("x")), _kf_values(node.get("y"))
        if len(set(xs)) > 1 or len(set(ys)) > 1 or not xs or not ys or abs(xs[0] - ys[0]) > 1e-6:
            return False
    return True


def _wheels_are_neutral(value: Any) -> bool:
    """Color Grade wheels (``color_presets.default_wheels_data``) that are off or all at amount 0, luma 0."""
    if not isinstance(value, dict):
        return value in (None, "", {})
    if value.get("enabled") is False or (_kf_values(value.get("enabled_keyframes")) and
                                         all(v < 0.5 for v in _kf_values(value.get("enabled_keyframes")))):
        return True
    for key in ("global", "shadows", "midtones", "highlights"):
        wheel = value.get(key)
        if not isinstance(wheel, dict):
            continue
        amounts = _kf_values(wheel.get("amount_keyframes")) or _kf_values(wheel.get("amount"))
        lumas = _kf_values(wheel.get("luma_keyframes")) or _kf_values(wheel.get("luma"))
        if any(abs(v) > 1e-6 for v in amounts + lumas):
            return False
    return True


def _grade_extra_set(key: str, value: Any) -> bool:
    """True when a ColorGrade curve/wheel parameter holds a non-neutral setting."""
    if key == "wheels":
        return not _wheels_are_neutral(value)
    return not _curve_is_neutral(value)


MAPPINGS: Dict[str, Callable[[Any, EffectContext, MappedEffects], None]] = {
    "Blur": _blur,
    "Brightness": _brightness,
    "Saturation": _saturation,
    "Hue": _hue,
    "Negate": _negate,
    "Pixelate": _pixelate,
    "Sharpen": _sharpen,
    "ChromaKey": _chroma_key,
    "Crop": _crop,
    "ColorGrade": _color_grade,
}

# Effects that change only audio or analysis data: not video effects to map.
AUDIO_OR_DATA = frozenset({"Compressor", "Expander", "Delay", "Echo", "Distortion", "Noise", "ParametricEQ",
                           "Robotization", "Whisperization", "Stabilizer", "Tracker", "ObjectDetection"})


def map_effects(clip, ctx: EffectContext) -> MappedEffects:
    """AE effects and masks for every effect on *clip* (in libopenshot order), plus what could not map."""
    out = MappedEffects()
    effects = sorted(clip.effects, key=lambda e: (_order(e), e.id))
    for effect in effects:
        name = effect.class_name or effect.name
        mapper = MAPPINGS.get(name)
        if mapper is None:
            out.unmapped.append(name)
            continue
        before = len(out.effects)
        mapper(effect, ctx, out)
        disabled = effect.data.get("enabled") is False if hasattr(effect, "data") else False
        if disabled:
            for fx in out.effects[before:]:
                fx["on"] = False
    return out


def _order(effect) -> float:
    try:
        return float(effect.data.get("order") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


__all__ = ["EffectContext", "MappedEffects", "MAPPINGS", "AUDIO_OR_DATA", "map_effects", "decode_size",
           "keys_spec", "AE_BLUR_SIZE_TO_SIGMA"]
