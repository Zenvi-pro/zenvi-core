"""A HyperFrames primitive as Zenvi clip properties: placement, keyframes and audio gain (pure Python).

* **Placement.** The element's CSS box (``left`` / ``top`` / ``width`` /
  ``height`` / ``inset`` in px, % or vw/vh against its positioned ancestors,
  the composition being the outermost box) and ``object-fit`` /
  ``object-position`` give the rectangle the media is drawn in. Zenvi gets
  that rectangle as FIT + centre gravity + ``scale_x/y`` + ``location_x/y``
  (``transform.geometry`` puts the media back on exactly those pixels); a
  full-frame ``cover`` / ``contain`` / ``fill`` becomes plain CROP / FIT /
  STRETCH. A static CSS ``transform`` (translate / scale / rotate) and
  ``opacity`` are the starting values.
* **Animation.** Parsed GSAP tweens (``opacity``, ``autoAlpha``, ``x``, ``y``,
  ``scale``, ``scaleX``, ``scaleY``, ``rotation``) become ``alpha``,
  ``location_x/y``, ``scale_x/y`` and ``rotation`` keyframes. An ease that is
  a cubic bezier (``none``, ``power1``, ``power2``) is one Zenvi bezier
  segment; any other ease (or a tween off the frame grid) is sampled at
  every frame with linear keys, which libopenshot then shows exactly.
* **Audio.** ``data-volume`` x ``data-automation`` volume lane x
  ``data-fade-in`` / ``data-fade-out`` ramps become ``volume`` keyframes;
  ``muted`` / ``data-has-audio="false"`` switch the clip's audio off.

Whatever cannot be rebuilt exactly -- computed values, overlapping tweens,
unsupported properties, ``calc()`` layouts, a ``cover`` crop inside a smaller
box -- is listed in the plan's ``problems`` (the importer flattens such a
project in auto mode, and warns in native mode).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from classes.handoff import transform as tf
from classes.handoff.hyperframes import gsap
from classes.handoff.hyperframes.parser import Clip, Composition, Element, computed_style

BEZIER, LINEAR, CONSTANT = 0, 1, 2
CHANNELS = ("opacity", "x", "y", "scaleX", "scaleY", "rotation")
_PROP_CHANNELS = {"opacity": ("opacity",), "autoAlpha": ("opacity",), "x": ("x",), "y": ("y",),
                  "scale": ("scaleX", "scaleY"), "scaleX": ("scaleX",), "scaleY": ("scaleY",),
                  "rotation": ("rotation",), "rotationZ": ("rotation",), "rotate": ("rotation",)}
GRID_TOLERANCE = 1e-3   # frames: a tween boundary further off the frame grid is sampled


# ---------------------------------------------------------------------------
# CSS values
# ---------------------------------------------------------------------------

_LENGTH = re.compile(r"^\s*(-?\d*\.?\d+(?:e[+-]?\d+)?)\s*(px|%|vw|vh|vmin|vmax)?\s*$", re.I)


def css_length(value: Optional[str], reference: float, canvas: Tuple[float, float]) -> Optional[float]:
    """A CSS length in px (``%`` of *reference*); None for auto / absent; raises ValueError for calc() & co."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == "auto":
        return None
    m = _LENGTH.match(text)
    if not m:
        raise ValueError("uses a CSS length Zenvi cannot evaluate (%s)" % value)
    num = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "px":
        return num
    if unit == "%":
        return num * reference / 100.0
    w, h = canvas
    return num * {"vw": w, "vh": h, "vmin": min(w, h), "vmax": max(w, h)}[unit] / 100.0


def _angle(text: str) -> float:
    m = re.match(r"^\s*(-?\d*\.?\d+(?:e[+-]?\d+)?)\s*(deg|rad|turn|grad)?\s*$", text, re.I)
    if not m:
        raise ValueError("angle %r" % text)
    num = float(m.group(1))
    unit = (m.group(2) or "deg").lower()
    return {"deg": num, "rad": math.degrees(num), "turn": num * 360.0, "grad": num * 0.9}[unit]


@dataclass(frozen=True)
class CssTransform:
    translate_x: float = 0.0
    translate_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0


def parse_transform(value: Optional[str], box: Tuple[float, float], canvas: Tuple[float, float]) -> CssTransform:
    """``translate()`` / ``scale()`` / ``rotate()`` (in an order GSAP would also produce); ValueError otherwise."""
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return CssTransform()
    tx = ty = rot = 0.0
    sx = sy = 1.0
    seen_rotate = seen_scale = False
    for name, args in re.findall(r"([a-zA-Z0-9]+)\(([^)]*)\)", text):
        parts = [a.strip() for a in re.split(r"[,\s]+", args.strip()) if a.strip()]
        name = name.lower()
        if name in ("translate", "translatex", "translatey", "translate3d"):
            if seen_rotate or seen_scale:
                raise ValueError("translate after rotate/scale (%s)" % text)
            if name == "translatey":
                ty += css_length(parts[0], box[1], canvas) or 0.0
            else:
                tx += css_length(parts[0], box[0], canvas) or 0.0
                if name in ("translate", "translate3d") and len(parts) > 1:
                    ty += css_length(parts[1], box[1], canvas) or 0.0
        elif name in ("rotate", "rotatez"):
            if seen_scale:
                raise ValueError("rotate after scale (%s)" % text)
            rot += _angle(parts[0])
            seen_rotate = True
        elif name in ("scale", "scalex", "scaley", "scale3d"):
            if name == "scaley":
                sy *= float(parts[0])
            else:
                sx *= float(parts[0])
                sy *= float(parts[1]) if name in ("scale", "scale3d") and len(parts) > 1 else (
                    float(parts[0]) if name == "scale" else 1.0)
            seen_scale = True
        else:
            raise ValueError("CSS transform %s() is not supported" % name)
    leftover = re.sub(r"[a-zA-Z0-9]+\([^)]*\)", "", text).strip()
    if leftover:
        raise ValueError("CSS transform %r" % text)
    return CssTransform(tx, ty, sx, sy, rot)


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    def same_as(self, other: "Box", tol: float = 0.5) -> bool:
        return (abs(self.x - other.x) <= tol and abs(self.y - other.y) <= tol and abs(self.w - other.w) <= tol
                and abs(self.h - other.h) <= tol)


def _positioned(style: Dict[str, str]) -> bool:
    return style.get("position", "").strip().lower() in ("absolute", "fixed", "relative", "sticky")


def _inset(style: Dict[str, str]) -> Dict[str, str]:
    out = dict(style)
    inset = style.get("inset")
    if inset:
        parts = inset.split()
        if len(parts) == 1:
            parts *= 4
        elif len(parts) == 2:
            parts = [parts[0], parts[1], parts[0], parts[1]]
        elif len(parts) == 3:
            parts = [parts[0], parts[1], parts[2], parts[1]]
        for key, value in zip(("top", "right", "bottom", "left"), parts[:4]):
            out.setdefault(key, value)
    return out


def element_box(el: Element, style: Dict[str, str], container: Box, canvas: Tuple[float, float],
                intrinsic: Optional[Tuple[float, float]], problems: List[str]) -> Box:
    """The border box of *el* inside *container* (canvas px), from its absolute-layout CSS."""
    style = _inset(style)
    position = style.get("position", "").strip().lower()
    try:
        left = css_length(style.get("left"), container.w, canvas)
        right = css_length(style.get("right"), container.w, canvas)
        top = css_length(style.get("top"), container.h, canvas)
        bottom = css_length(style.get("bottom"), container.h, canvas)
        width = css_length(style.get("width"), container.w, canvas)
        height = css_length(style.get("height"), container.h, canvas)
    except ValueError as exc:
        problems.append(str(exc))
        return container
    if position not in ("absolute", "fixed"):
        # in normal flow: Zenvi cannot run a layout engine, so it assumes the container's top-left
        left = left if position == "relative" else None
        top = top if position == "relative" else None
        right = bottom = None
    if width is None and left is not None and right is not None:
        width = max(0.0, container.w - left - right)
    if height is None and top is not None and bottom is not None:
        height = max(0.0, container.h - top - bottom)
    if intrinsic and intrinsic[0] > 0 and intrinsic[1] > 0:
        iw, ih = intrinsic
        if width is None and height is not None:
            width = height * iw / ih
        elif height is None and width is not None:
            height = width * ih / iw
        elif width is None and height is None:
            width, height = iw, ih
    if width is None:
        width = container.w
    if height is None:
        height = container.h
    if left is None:
        left = (container.w - right - width) if right is not None else 0.0
    if top is None:
        top = (container.h - bottom - height) if bottom is not None else 0.0
    return Box(container.x + left, container.y + top, width, height)


def clip_box(clip: Clip, comp: Composition, intrinsic: Optional[Tuple[float, float]], problems: List[str]) -> Box:
    """Box of a root clip: its ancestors up to the composition are laid out first (positioned ones)."""
    canvas = (float(comp.width), float(comp.height))
    box = Box(0.0, 0.0, canvas[0], canvas[1])
    chain: List[Element] = []
    for anc in clip.element.ancestors():
        if anc is comp.element:
            break
        chain.append(anc)
    for anc in reversed(chain):
        style = computed_style(anc, comp.rules)
        if _positioned(style) or style.get("width") or style.get("height"):
            box = element_box(anc, style, box, canvas, None, problems)
        if style.get("transform", "").strip().lower() not in ("", "none"):
            problems.append(f"its wrapper <{anc.tag}> has a CSS transform")
    return element_box(clip.element, clip.style, box, canvas, intrinsic, problems)


def content_rect(box: Box, fit: str, position: str, media: Tuple[float, float], problems: List[str]) -> Box:
    """Where the media's pixels land inside its box (CSS object-fit / object-position)."""
    mw, mh = media
    if mw <= 0 or mh <= 0:
        return box
    fit = (fit or "fill").strip().lower()
    if fit == "fill":
        return box
    if fit == "contain":
        s = min(box.w / mw, box.h / mh)
    elif fit == "cover":
        s = max(box.w / mw, box.h / mh)
    elif fit == "none":
        s = 1.0
    elif fit == "scale-down":
        s = min(1.0, min(box.w / mw, box.h / mh))
    else:
        problems.append("object-fit %r" % fit)
        return box
    w, h = mw * s, mh * s
    fx, fy = _object_position(position, problems)
    return Box(box.x + (box.w - w) * fx, box.y + (box.h - h) * fy, w, h)


def _object_position(text: str, problems: List[str]) -> Tuple[float, float]:
    words = {"left": 0.0, "top": 0.0, "center": 0.5, "right": 1.0, "bottom": 1.0}
    parts = (text or "").strip().lower().split()
    if not parts:
        return 0.5, 0.5
    vals = []
    for p in parts[:2]:
        if p in words:
            vals.append(words[p])
        elif p.endswith("%"):
            try:
                vals.append(float(p[:-1]) / 100.0)
            except ValueError:
                problems.append("object-position %r" % text)
                return 0.5, 0.5
        else:
            problems.append("object-position %r (lengths are not supported)" % text)
            return 0.5, 0.5
    if len(vals) == 1:
        if parts[0] in ("top", "bottom"):
            return 0.5, vals[0]
        return vals[0], 0.5
    if parts[0] in ("top", "bottom"):
        return vals[1], vals[0]
    return vals[0], vals[1]


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

@dataclass
class Placement:
    """Zenvi transform properties that put the media where HyperFrames draws it (no animation)."""

    scale_mode: int = tf.SCALE_FIT
    gravity: int = tf.GRAVITY_CENTER
    scale_x: float = 1.0
    scale_y: float = 1.0
    location_x: float = 0.0
    location_y: float = 0.0
    rotation: float = 0.0
    alpha: float = 1.0
    px_to_location: Tuple[float, float] = (1.0 / 1920, 1.0 / 1080)   # HyperFrames px -> location units
    rect: Optional[Box] = None            # media rectangle in HyperFrames canvas px
    plain: bool = False                   # full-frame CROP / FIT / STRETCH (scale 1, location 0)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# CSS a native Zenvi clip cannot reproduce: the browser would draw the media differently.
_CSS_EFFECTS = ("animation", "animation-name", "transition", "filter", "backdrop-filter", "clip-path", "mask",
                "mask-image", "-webkit-mask-image", "mix-blend-mode", "border-radius", "box-shadow", "perspective")
_CSS_NEUTRAL = ("", "none", "0", "0px", "normal", "initial", "unset", "inherit", "all 0s ease 0s")


def css_effects(style: Dict[str, str]) -> List[str]:
    """The CSS effects on an element that Zenvi does not rebuild (``filter: blur(4px)`` ...)."""
    out = []
    for key in _CSS_EFFECTS:
        value = style.get(key, "").strip().lower()
        if value and value not in _CSS_NEUTRAL:
            out.append("%s: %s" % (key, style[key].strip()))
    return out


def placement(clip: Clip, comp: Composition, media: Tuple[int, int], canvas: Tuple[int, int]) -> Placement:
    """Placement of a visual primitive (video / img) on a *canvas* (the Zenvi project's width, height)."""
    problems: List[str] = ["its CSS %s is not rebuilt in Zenvi" % e for e in css_effects(clip.style)]
    warnings: List[str] = []
    W, H = float(comp.width), float(comp.height)
    zw, zh = float(canvas[0]), float(canvas[1])
    mw, mh = (float(media[0] or 0), float(media[1] or 0))
    box = clip_box(clip, comp, (mw, mh) if mw and mh else None, problems)
    style = clip.style
    default_fit = "contain" if clip.tag == "video" else "fill"
    fit = (style.get("object-fit") or default_fit).strip().lower()
    rect = content_rect(box, fit, style.get("object-position", ""), (mw, mh), problems)
    try:
        css = parse_transform(style.get("transform"), (box.w, box.h), (W, H))
    except ValueError as exc:
        problems.append(str(exc))
        css = CssTransform()
    origin = style.get("transform-origin", "").strip().lower()
    if origin and origin not in ("50% 50%", "center", "center center", "50% 50% 0", "50%"):
        if css.rotation or css.scale_x != 1 or css.scale_y != 1:
            problems.append(f"transform-origin {origin!r} with a CSS scale/rotation")
    try:
        opacity = float(style.get("opacity", "1") or 1)
    except ValueError:
        problems.append("opacity %r" % style.get("opacity"))
        opacity = 1.0
    full = Box(0.0, 0.0, W, H)
    if fit == "cover" and not box.same_as(full) and (rect.w > box.w + 0.5 or rect.h > box.h + 0.5):
        warnings.append(f"{clip.label} is cropped by its {box.w:.0f}x{box.h:.0f} box (object-fit: cover); Zenvi "
                        "shows the whole scaled media")
    kx, ky = zw / W, zh / H
    if abs(kx - ky) > 1e-6:
        warnings.append(f"the composition is {comp.width}x{comp.height} and the project {canvas[0]}x{canvas[1]}: "
                        f"{clip.label} keeps its relative place but is stretched to the new shape")
    p = Placement(px_to_location=(kx / zw, ky / zh), rect=rect, problems=problems, warnings=warnings,
                  alpha=max(0.0, min(1.0, opacity)), rotation=css.rotation)
    untransformed = not (css.translate_x or css.translate_y or css.rotation or css.scale_x != 1 or css.scale_y != 1)
    animated = any(not t.is_spacer for t in clip.tweens)
    if box.same_as(full) and untransformed and not animated and fit in ("cover", "contain", "fill"):
        p.scale_mode = {"cover": tf.SCALE_CROP, "contain": tf.SCALE_FIT, "fill": tf.SCALE_STRETCH}[fit]
        p.plain = True
        return p
    if mw <= 0 or mh <= 0:
        problems.append("its media size is unknown")
        return p
    # FIT into the Zenvi canvas, then scale + move so the media covers `rect` (scaled about its centre)
    fw, fh = tf.scaled_source_size(int(round(mw)), int(round(mh)), tf.SCALE_FIT, int(zw), int(zh))
    cw, ch = rect.w * kx, rect.h * ky
    cx = (rect.x + rect.w / 2.0) * kx
    cy = (rect.y + rect.h / 2.0) * ky
    p.scale_x = (cw / fw if fw else 1.0) * css.scale_x
    p.scale_y = (ch / fh if fh else 1.0) * css.scale_y
    # a CSS translate moves the box; scale/rotate turn about the box centre (= the media's when centred)
    cx += css.translate_x * kx
    cy += css.translate_y * ky
    box_cx = (box.x + box.w / 2.0) * kx + css.translate_x * kx
    box_cy = (box.y + box.h / 2.0) * ky + css.translate_y * ky
    if (abs(box_cx - cx) > 0.5 or abs(box_cy - cy) > 0.5) and (css.rotation or css.scale_x != 1 or
                                                                 css.scale_y != 1 or animated):
        problems.append("its media is not centred in its box, so scaling and rotation would pivot elsewhere")
    p.location_x = (cx - zw / 2.0) / zw
    p.location_y = (cy - zh / 2.0) / zh
    return p


# ---------------------------------------------------------------------------
# Animation: tweens -> channel segments -> Zenvi keyframes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Seg:
    """One change of a channel: from *v0* at *t0* to *v1* at *t1* (clip-local seconds) with *ease*."""

    t0: float
    t1: float
    v0: float
    v1: float
    ease: Optional[gsap.EaseSpec]      # None = hold (an instant ``set``)


def _channel_value(raw: Any, channel: str, current: float, problems: List[str], prop: str) -> Optional[float]:
    pv = gsap.prop_value(raw)
    if pv is None:
        problems.append(f"{prop}: {raw!r} is not a number")
        return None
    unit = pv.unit
    value = pv.value
    if channel == "rotation":
        if unit in ("", "deg"):
            pass
        elif unit == "rad":
            value = math.degrees(value)
        elif unit == "turn":
            value *= 360.0
        else:
            problems.append(f"{prop}: unit {unit!r}")
            return None
    elif channel in ("x", "y"):
        if unit not in ("", "px"):
            problems.append(f"{prop}: unit {unit!r} (only px translates)")
            return None
    elif unit not in ("",):
        problems.append(f"{prop}: unit {unit!r}")
        return None
    return current + value if pv.relative else value


def channel_segments(clip: Clip, base: Dict[str, float]) -> Tuple[Dict[str, List[Seg]], Dict[str, float], List[str]]:
    """Per channel: segments in clip-local time, the value before the first one, and problems."""
    problems: List[str] = []
    segs: Dict[str, List[Seg]] = {ch: [] for ch in CHANNELS}
    initial = dict(base)
    current = dict(base)
    t_clip = clip.start or 0.0
    tweens = sorted((t for t in clip.tweens if not t.is_spacer), key=lambda t: (t.start is None, t.start or 0.0))
    for tw in tweens:
        if tw.reasons:
            problems.extend(f"line {tw.line}: {r}" for r in tw.reasons)
            continue
        if tw.start is None:
            continue
        ease = gsap.ease_spec(tw.ease if tw.ease is not None else gsap.DEFAULT_EASE)
        if ease is None:
            problems.append(f"line {tw.line}: ease {tw.ease!r} is not a standard GSAP ease")
            continue
        props = set(tw.to_vars) | set(tw.from_vars)
        unknown = sorted(p for p in props if p not in _PROP_CHANNELS)
        if unknown:
            problems.append(f"line {tw.line}: animates {', '.join(unknown)}")
            continue
        t0 = tw.start - t_clip
        t1 = t0 + tw.duration
        for prop in sorted(props):
            for ch in _PROP_CHANNELS[prop]:
                chan = segs[ch]
                if chan and t0 < chan[-1].t1 - 1e-9:
                    problems.append(f"line {tw.line}: overlapping tweens on {prop}")
                    continue
                cur = current[ch]
                if prop in tw.from_vars:
                    v0 = _channel_value(tw.from_vars[prop], ch, cur, problems, prop)
                else:
                    v0 = cur
                if prop in tw.to_vars:
                    v1 = _channel_value(tw.to_vars[prop], ch, cur, problems, prop)
                else:
                    v1 = cur
                if v0 is None or v1 is None:
                    continue
                if not chan and tw.method in ("from", "fromTo") and tw.immediate_render:
                    initial[ch] = v0  # GSAP renders the start state at once
                if tw.method == "set" or tw.duration <= 0:
                    chan.append(Seg(t0, t0, v0, v1, None))
                else:
                    chan.append(Seg(t0, t1, v0, v1, ease))
                current[ch] = v1
    return segs, initial, problems


def _point(x: float, y: float, interpolation: int, handle_left=(0.5, 1.0), handle_right=(0.5, 0.0)) -> dict:
    return {"co": {"X": float(x), "Y": float(y)}, "interpolation": int(interpolation), "handle_type": 0,
            "handle_left": {"X": float(handle_left[0]), "Y": float(handle_left[1])},
            "handle_right": {"X": float(handle_right[0]), "Y": float(handle_right[1])}}


class KeyBuilder:
    """Zenvi keyframe points for one property, appended in time order (clip-local seconds -> X)."""

    def __init__(self, fps: float, start_trim: float):
        self.fps = float(fps)
        self.start_trim = float(start_trim)
        self.points: List[dict] = []

    def x_of(self, t: float) -> float:
        return float(round((t + self.start_trim) * self.fps)) + 1.0

    def on_grid(self, t: float) -> bool:
        f = (t + self.start_trim) * self.fps
        return abs(f - round(f)) <= GRID_TOLERANCE

    def add(self, x: float, value: float, interpolation: int, handle_left=(0.5, 1.0)) -> None:
        if self.points and self.points[-1]["co"]["X"] >= x:
            last = self.points[-1]
            if last["co"]["X"] == x:  # a later key on the same frame wins (libopenshot AddPoint)
                last["co"]["Y"] = float(value)
                last["interpolation"] = int(interpolation)
                last["handle_left"] = {"X": float(handle_left[0]), "Y": float(handle_left[1])}
            return
        self.points.append(_point(x, value, interpolation, handle_left))

    def set_right_handle(self, handle_right: Tuple[float, float]) -> None:
        if self.points:
            self.points[-1]["handle_right"] = {"X": float(handle_right[0]), "Y": float(handle_right[1])}

    def keyframe(self) -> dict:
        return {"Points": self.points}


def channel_keyframe(segments: Sequence[Seg], initial: float, fps: float, start_trim: float,
                     transform_value=lambda v: v) -> Optional[dict]:
    """Keyframes for one channel (values mapped by *transform_value*); None when it never changes.

    The first key sits on the clip's first frame with the value before the
    first tween; each tween then holds (or jumps, for a ``fromTo`` / ``set``
    that starts elsewhere) to its start and runs to its end -- as one bezier
    segment per exact ease piece when its ends lie on the frame grid, else
    as one linear key per frame.
    """
    if not segments:
        return None
    kb = KeyBuilder(fps, start_trim)
    fn = transform_value
    first_x = kb.x_of(0.0)
    value = initial
    kb.add(first_x, fn(initial), BEZIER)
    for seg in segments:
        x0 = kb.x_of(seg.t0)
        if seg.ease is None:  # an instant set: hold the old value up to this frame, then the new one
            kb.add(max(x0, first_x), fn(seg.v1), CONSTANT)
            value = seg.v1
            continue
        if seg.t0 >= 0:
            kb.add(x0, fn(seg.v0), LINEAR if abs(seg.v0 - value) < 1e-12 else CONSTANT)
        if not _exact_segment(kb, seg, fn):
            _sampled_segment(kb, seg, fn, first_x)
        value = seg.v1
    return kb.keyframe()


def _exact_segment(kb: KeyBuilder, seg: Seg, fn) -> bool:
    """Write *seg* as bezier / linear pieces when its ease allows it and every piece ends on a frame."""
    pieces = seg.ease.pieces if seg.ease is not None else None
    if pieces is None or seg.t0 < 0 or seg.t1 <= seg.t0:
        return False
    dt = seg.t1 - seg.t0
    bounds = []
    for a, b, shape in pieces:
        ta, tb = seg.t0 + a * dt, seg.t0 + b * dt
        if not (kb.on_grid(ta) and kb.on_grid(tb)) or kb.x_of(tb) <= kb.x_of(ta):
            return False
        bounds.append((ta, tb, a, b, shape))
    assert seg.ease is not None
    for ta, tb, a, b, shape in bounds:
        va = seg.v0 + (seg.v1 - seg.v0) * seg.ease.fn(a)
        vb = seg.v0 + (seg.v1 - seg.v0) * seg.ease.fn(b)
        xa, xb = kb.x_of(ta), kb.x_of(tb)
        if not kb.points or kb.points[-1]["co"]["X"] != xa:
            kb.add(xa, fn(va), LINEAR)
        if shape == "linear":
            kb.add(xb, fn(vb), LINEAR)
        else:
            x1h, y1h, x2h, y2h = shape
            kb.set_right_handle((x1h, y1h))
            kb.add(xb, fn(vb), BEZIER, handle_left=(x2h, y2h))
    return True


def gsap_progress(t: float, t0: float, t1: float) -> float:
    """A tween's progress at *t* the way GSAP renders it: within 1e-8 s of either end it snaps to it."""
    duration = t1 - t0
    local = t - t0
    if duration <= 0 or local > duration - 1e-8:
        return 1.0
    if local < 1e-8:
        return 0.0
    return local / duration


def _sampled_segment(kb: KeyBuilder, seg: Seg, fn, first_x: float) -> None:
    """One linear key per whole frame: exact where libopenshot evaluates the curve."""
    assert seg.ease is not None
    lo = math.ceil((seg.t0 + kb.start_trim) * kb.fps - GRID_TOLERANCE)
    hi = math.floor((seg.t1 + kb.start_trim) * kb.fps + GRID_TOLERANCE)
    lo = max(lo, int(first_x) - 1)
    for f in range(int(lo), int(hi) + 1):
        t = f / kb.fps - kb.start_trim
        kb.add(float(f) + 1.0, fn(seg.v0 + (seg.v1 - seg.v0) * seg.ease.fn(gsap_progress(t, seg.t0, seg.t1))),
               LINEAR)
    if not kb.on_grid(seg.t1):  # it ends between frames: the next frame already shows the end value
        kb.add(float(hi) + 2.0, fn(seg.v1), LINEAR)


# ---------------------------------------------------------------------------
# Audio gain
# ---------------------------------------------------------------------------

def _lane_points(clip: Clip, problems: List[str]) -> Optional[List[Tuple[float, float]]]:
    auto = clip.automation
    if not isinstance(auto, dict):
        return None
    raw_lanes = auto.get("lanes")
    lanes = raw_lanes if isinstance(raw_lanes, list) else []
    points: Optional[List[Tuple[float, float]]] = None
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        target = str(lane.get("target") or "")
        if target == "volume":
            pts = []
            for pt in lane.get("points") or []:
                try:
                    pts.append((float(pt["t"]), float(pt["v"])))
                except (KeyError, TypeError, ValueError):
                    problems.append("a volume automation point is not {t, v}")
            pts.sort()
            points = pts or None
            if lane.get("interpolation") not in (None, "linear"):
                problems.append("volume automation interpolation %r" % lane.get("interpolation"))
        elif target == "rate":
            problems.append("its playback rate is automated (a speed ramp)")
        elif target:
            problems.append(f"it automates {target}")
    return points


def gain_at(t: float, duration: float, volume: float, fade_in: float, fade_out: float,
            lane: Optional[List[Tuple[float, float]]]) -> float:
    """HyperFrames' gain at clip-local *t*: volume x lane x clip-edge fades (linear ramps)."""
    g = volume
    if lane:
        if t <= lane[0][0]:
            g *= lane[0][1]
        elif t >= lane[-1][0]:
            g *= lane[-1][1]
        else:
            for (ta, va), (tb, vb) in zip(lane, lane[1:]):
                if ta <= t <= tb:
                    g *= va if tb <= ta else va + (vb - va) * (t - ta) / (tb - ta)
                    break
    if fade_in > 0 and t < fade_in:
        g *= max(0.0, t / fade_in)
    if fade_out > 0 and t > duration - fade_out:
        g *= max(0.0, (duration - t) / fade_out)
    return g


def volume_keyframe(clip: Clip, duration: float, fps: float, start_trim: float,
                    problems: List[str]) -> Optional[dict]:
    """Zenvi ``volume`` keyframes for the clip's gain; None when it is a constant 1."""
    lane = _lane_points(clip, problems)
    fade_in, fade_out = min(clip.fade_in, duration), min(clip.fade_out, duration)
    if not lane and not fade_in and not fade_out:
        if abs(clip.volume - 1.0) < 1e-9:
            return None
        return {"Points": [_point(1.0 + round(start_trim * fps), clip.volume, BEZIER)]}
    kb = KeyBuilder(fps, start_trim)
    breaks = {0.0, duration}
    if fade_in:
        breaks.add(fade_in)
    if fade_out:
        breaks.add(max(0.0, duration - fade_out))
    for t, _v in lane or []:
        if 0.0 < t < duration:
            breaks.add(t)
    times = sorted(breaks)
    on_grid = all(kb.on_grid(t) for t in times)
    # one varying factor per interval -> linear keys at the breaks are exact; otherwise sample each frame
    varying_overlap = bool(lane) and bool(fade_in or fade_out) and len(lane) > 1
    if on_grid and not varying_overlap:
        for t in times:
            kb.add(kb.x_of(t), gain_at(t, duration, clip.volume, fade_in, fade_out, lane), LINEAR)
    else:
        last = int(math.floor(duration * fps + GRID_TOLERANCE))
        for f in range(0, last + 1):
            t = f / fps
            kb.add(kb.x_of(t), gain_at(t, duration, clip.volume, fade_in, fade_out, lane), LINEAR)
    return kb.keyframe()


# ---------------------------------------------------------------------------
# The whole clip
# ---------------------------------------------------------------------------

@dataclass
class NativePlan:
    """Everything the importer writes on the Zenvi clip of one primitive."""

    clip: Clip
    position: float              # timeline seconds (before the import offset)
    start: float                 # Zenvi clip start (source in, seconds)
    end: float
    props: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)    # animation / layout Zenvi does not rebuild
    warnings: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def constant(value: float) -> dict:
    return {"Points": [_point(1.0, value, BEZIER)]}


def native_plan(clip: Clip, comp: Composition, media: dict, *, fps: float, canvas: Tuple[int, int]) -> NativePlan:
    """The Zenvi clip for HyperFrames primitive *clip*, given its probed *media* (reader dict)."""
    problems = list(clip.problems)
    warnings: List[str] = []
    try:
        media_duration = float(media.get("duration") or 0.0)
    except (TypeError, ValueError):
        media_duration = 0.0
    duration = clip.duration
    if duration is None:
        duration = max(0.0, (media_duration - clip.media_start) / clip.playback_rate) if media_duration else 0.0
    rate = clip.playback_rate if clip.kind != "img" else 1.0
    frame = 1.0 / fps
    duration = max(frame, round(duration * fps) / fps)
    if clip.kind == "img":
        start, end = 0.0, duration
    else:
        start = round(clip.media_start * fps) / fps
        needed = start + duration * rate
        if media_duration and needed > media_duration + frame / 2:
            playable = max(frame, (media_duration - start) / rate)
            warnings.append(f"{clip.label} runs {duration:.2f}s but its media has only {playable:.2f}s left after "
                            f"data-media-start; Zenvi ends the clip with the media")
            duration = max(frame, math.floor(playable * fps + 1e-6) / fps)
        end = start + duration
    plan = NativePlan(clip=clip, position=round((clip.start or 0.0) * fps) / fps, start=start, end=end,
                      problems=problems, warnings=warnings)
    props: Dict[str, Any] = {}
    if abs(rate - 1.0) > 1e-9:
        # constant speed: clip frames X -> source frames Y (libopenshot time curve), window from 0
        src0 = start * fps + 1.0
        frames = round(duration * fps)
        props["time"] = {"Points": [_point(1.0, src0, LINEAR), _point(frames + 1.0, src0 + frames * rate, LINEAR)]}
        props["start"], props["end"] = 0.0, duration
        plan.start, plan.end = 0.0, duration
    trim = plan.start
    if clip.kind in ("video", "img"):
        place = placement(clip, comp, (int(media.get("width") or 0), int(media.get("height") or 0)), canvas)
        problems.extend(place.problems)
        warnings.extend(place.warnings)
        props["scale"] = place.scale_mode
        props["gravity"] = place.gravity
        base = {"opacity": place.alpha, "x": 0.0, "y": 0.0, "scaleX": 1.0, "scaleY": 1.0,
                "rotation": place.rotation}
        segs, initial, anim_problems = channel_segments(clip, base)
        problems.extend(anim_problems)
        kx, ky = place.px_to_location
        mapping = {
            "alpha": ("opacity", lambda v: max(0.0, min(1.0, v)), place.alpha),
            "location_x": ("x", lambda v, b=place.location_x: b + v * kx, place.location_x),
            "location_y": ("y", lambda v, b=place.location_y: b + v * ky, place.location_y),
            "scale_x": ("scaleX", lambda v, b=place.scale_x: b * v, place.scale_x),
            "scale_y": ("scaleY", lambda v, b=place.scale_y: b * v, place.scale_y),
            "rotation": ("rotation", lambda v: v, place.rotation),
        }
        for key, (channel, fn, static) in mapping.items():
            kf = channel_keyframe(segs[channel], initial[channel], fps, trim, fn)
            if kf is not None:
                props[key] = kf
            elif key == "rotation" or abs(static - tf.TRANSFORM_DEFAULTS[key]) > 1e-12:
                if abs(static - tf.TRANSFORM_DEFAULTS[key]) > 1e-12:
                    props[key] = constant(static)
    if clip.kind in ("video", "audio"):
        off = clip.muted or clip.has_audio is False
        if off:
            props["has_audio"] = {"Points": [_point(1.0, 0.0, CONSTANT)]}
        else:
            kf = volume_keyframe(clip, duration, fps, trim, problems)
            if kf is not None:
                props["volume"] = kf
    plan.props = props
    plan.problems = _dedupe(problems)
    plan.warnings = _dedupe(warnings)
    return plan


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


__all__ = [
    "css_effects", "css_length", "parse_transform", "CssTransform", "Box", "element_box", "clip_box", "content_rect",
    "Placement", "placement", "Seg", "channel_segments", "channel_keyframe", "KeyBuilder", "gain_at",
    "gsap_progress",
    "volume_keyframe", "NativePlan", "native_plan", "constant", "CHANNELS",
]
