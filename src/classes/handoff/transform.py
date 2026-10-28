"""Clip geometry on the canvas: where libopenshot 1.0 draws a clip, in pixels.

:func:`geometry` ports ``Clip::get_transform`` (libopenshot src/Clip.cpp)
from the web engine, whose port is parity-tested against libopenshot 1.0
(zenvi-web ``packages/engine/src/render/plan/transform.ts``, ``clipTransform``,
and ``render/plan/reader.ts`` ``scaleSize`` / ``qsizeScaled``):

1. the layout box is the canvas minus ``margin`` (fraction of the shorter
   side) on every edge;
2. the source is sized into the box by the scale mode (``QSize::scaled``,
   integer math: FIT keeps aspect inside, CROP expands to cover, STRETCH
   fills, NONE keeps the source size), then multiplied by ``scale_x/y``;
3. gravity places that box inside the layout box;
4. location moves it: for every mode except CROP by ``canvas * location``,
   for CROP by ``location * (anchored + size)`` when negative and
   ``location * (canvas - anchored)`` when positive (1.0 semantics);
5. rotation (degrees, clockwise on screen) and shear turn it about the
   origin point (``origin_x/y`` fractions of the displayed size).

Parent clips (``parentObjectId``) are not followed; exporters report them.

The legacy helpers :func:`scale_mode_size`, :func:`gravity_offset` and
:func:`normalized_to_center_pixels` are the Final Cut Pro XML exporter's
original float approximations, moved here unchanged (the FCP exporter and
importer import them); new code uses :func:`geometry`.

Pure Python: no Qt, no project access.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

# libopenshot ScaleType (openshot.SCALE_*)
SCALE_CROP, SCALE_FIT, SCALE_STRETCH, SCALE_NONE = 0, 1, 2, 3
SCALE_NAMES = {SCALE_CROP: "crop", SCALE_FIT: "fit", SCALE_STRETCH: "stretch", SCALE_NONE: "none"}

# libopenshot GravityType (openshot.GRAVITY_*)
(GRAVITY_TOP_LEFT, GRAVITY_TOP, GRAVITY_TOP_RIGHT, GRAVITY_LEFT, GRAVITY_CENTER, GRAVITY_RIGHT,
 GRAVITY_BOTTOM_LEFT, GRAVITY_BOTTOM, GRAVITY_BOTTOM_RIGHT) = range(9)
GRAVITY_NAMES = {
    GRAVITY_TOP_LEFT: "top_left", GRAVITY_TOP: "top_center", GRAVITY_TOP_RIGHT: "top_right",
    GRAVITY_LEFT: "center_left", GRAVITY_CENTER: "center", GRAVITY_RIGHT: "center_right",
    GRAVITY_BOTTOM_LEFT: "bottom_left", GRAVITY_BOTTOM: "bottom_center", GRAVITY_BOTTOM_RIGHT: "bottom_right",
}

# Clip constructor defaults (Clip.cpp init_settings) for absent transform keys.
TRANSFORM_DEFAULTS = {
    "scale_x": 1.0, "scale_y": 1.0, "location_x": 0.0, "location_y": 0.0,
    "origin_x": 0.5, "origin_y": 0.5, "rotation": 0.0, "shear_x": 0.0, "shear_y": 0.0,
    "alpha": 1.0, "margin": 0.0, "volume": 1.0, "corner_radius": 0.0,
}
# Keys libopenshot repairs to the default when the keyframe exists but has no points
# (ensure_default_keyframe); other empty keyframes evaluate to 0.
REPAIRED_WHEN_EMPTY = frozenset({"scale_x", "scale_y", "location_x", "location_y", "origin_x", "origin_y",
                                 "rotation", "corner_radius", "margin"})


# ---------------------------------------------------------------------------
# Legacy FCP helpers (moved verbatim from classes/exporters/final_cut_pro.py)
# ---------------------------------------------------------------------------

def normalized_to_center_pixels(x_norm, y_norm, frame_width, frame_height):
    """Map normalized OpenShot coords (-1..1, origin center) to pixel center values."""
    try:
        w = float(frame_width)
        h = float(frame_height)
    except (TypeError, ValueError):
        return x_norm, y_norm
    if w <= 0 or h <= 0:
        return x_norm, y_norm
    return (w / 2.0) + (x_norm * w / 2.0), (h / 2.0) + (y_norm * h / 2.0)


def scale_mode_size(src_w, src_h, frame_w, frame_h, scale_mode):
    """Return base scaled dimensions after applying scale mode (before per-axis scale)."""
    try:
        sw = float(src_w)
        sh = float(src_h)
        fw = float(frame_w)
        fh = float(frame_h)
    except (TypeError, ValueError):
        return src_w, src_h
    if sw <= 0 or sh <= 0 or fw <= 0 or fh <= 0:
        return src_w, src_h
    if scale_mode == SCALE_STRETCH:
        return fw, fh
    if scale_mode == SCALE_CROP:
        factor = max(fw / sw, fh / sh)
        return sw * factor, sh * factor
    if scale_mode == SCALE_FIT:
        factor = min(fw / sw, fh / sh)
        return sw * factor, sh * factor
    # SCALE_NONE or unknown
    return sw, sh


def gravity_offset(gravity, frame_w, frame_h, scaled_w, scaled_h):
    """Top-left origin based on gravity inside the frame."""
    try:
        frame_w = float(frame_w)
        frame_h = float(frame_h)
        scaled_w = float(scaled_w)
        scaled_h = float(scaled_h)
    except (TypeError, ValueError):
        return 0.0, 0.0
    x = 0.0
    y = 0.0
    if gravity == GRAVITY_TOP:
        x = (frame_w - scaled_w) / 2.0
    elif gravity == GRAVITY_TOP_RIGHT:
        x = frame_w - scaled_w
    elif gravity == GRAVITY_LEFT:
        y = (frame_h - scaled_h) / 2.0
    elif gravity == GRAVITY_CENTER:
        x = (frame_w - scaled_w) / 2.0
        y = (frame_h - scaled_h) / 2.0
    elif gravity == GRAVITY_RIGHT:
        x = frame_w - scaled_w
        y = (frame_h - scaled_h) / 2.0
    elif gravity == GRAVITY_BOTTOM_LEFT:
        y = frame_h - scaled_h
    elif gravity == GRAVITY_BOTTOM:
        x = (frame_w - scaled_w) / 2.0
        y = frame_h - scaled_h
    elif gravity == GRAVITY_BOTTOM_RIGHT:
        x = frame_w - scaled_w
        y = frame_h - scaled_h
    return x, y


# ---------------------------------------------------------------------------
# libopenshot 1.0 geometry
# ---------------------------------------------------------------------------

def qsize_scaled(w: int, h: int, tw: int, th: int, mode: str) -> Tuple[int, int]:
    """``QSize::scaled(target, mode)``: integer math, truncating; mode ignore / keep / expand."""
    if mode == "ignore" or w == 0 or h == 0:
        return tw, th
    rw = int((th * w) / h)
    use_height = rw <= tw if mode == "keep" else rw >= tw
    return (rw, th) if use_height else (tw, int((tw * h) / w))


def scaled_source_size(src_w: int, src_h: int, scale_mode: int, box_w: int, box_h: int) -> Tuple[int, int]:
    """``Clip::scale_size``: the source sized into a box by scale mode (before scale_x/y)."""
    if scale_mode == SCALE_FIT:
        return qsize_scaled(src_w, src_h, box_w, box_h, "keep")
    if scale_mode == SCALE_STRETCH:
        return qsize_scaled(src_w, src_h, box_w, box_h, "ignore")
    if scale_mode == SCALE_CROP:
        return qsize_scaled(src_w, src_h, box_w, box_h, "expand")
    return src_w, src_h


@dataclass(frozen=True)
class Geometry:
    """Where a clip's source image lands on the canvas at one instant.

    * ``x, y``: top-left of the displayed image before rotation/shear (canvas px).
    * ``width, height``: displayed size (canvas px), ``scale_x/y`` = canvas px per
      source px (an After Effects layer scale of ``scale_x * 100`` %).
    * ``anchor_x, anchor_y``: the origin point on the canvas -- what rotation turns
      about (an After Effects layer's Position with Anchor Point = ``origin * source size``).
    * ``center_x, center_y``: centre of the displayed image after rotation/shear.
    * ``rotation`` degrees clockwise on screen; ``opacity`` 0..1.
    * ``matrix``: (m11, m12, m21, m22, dx, dy) mapping source px to canvas px
      (``x' = m11*x + m21*y + dx``, ``y' = m12*x + m22*y + dy``, Qt order).
    """

    center_x: float
    center_y: float
    width: float
    height: float
    scale_x: float
    scale_y: float
    rotation: float
    opacity: float
    x: float
    y: float
    anchor_x: float
    anchor_y: float
    origin_x: float
    origin_y: float
    shear_x: float
    shear_y: float
    source_width: int
    source_height: int
    matrix: Tuple[float, float, float, float, float, float]
    time: Optional[float] = None
    frame: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)

    def map_point(self, sx: float, sy: float) -> Tuple[float, float]:
        """Canvas position of source pixel (sx, sy)."""
        m11, m12, m21, m22, dx, dy = self.matrix
        return m11 * sx + m21 * sy + dx, m12 * sx + m22 * sy + dy


class _Affine:
    """The subset of QTransform that ``Clip::get_transform`` uses (row-vector convention)."""

    def __init__(self):
        self.m = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # m11, m12, m21, m22, dx, dy

    def _pre(self, a11, a12, a21, a22, adx, ady):
        # QTransform::translate/rotate/scale/shear: the new op applies before the existing ones.
        m11, m12, m21, m22, dx, dy = self.m
        self.m = [
            a11 * m11 + a12 * m21, a11 * m12 + a12 * m22,
            a21 * m11 + a22 * m21, a21 * m12 + a22 * m22,
            adx * m11 + ady * m21 + dx, adx * m12 + ady * m22 + dy,
        ]

    def translate(self, dx, dy):
        self._pre(1.0, 0.0, 0.0, 1.0, dx, dy)

    def scale(self, sx, sy):
        self._pre(sx, 0.0, 0.0, sy, 0.0, 0.0)

    def rotate(self, degrees):
        rad = math.radians(degrees)
        c, s = math.cos(rad), math.sin(rad)
        if degrees % 360 == 0:
            c, s = 1.0, 0.0
        elif degrees % 360 == 90:
            c, s = 0.0, 1.0
        elif degrees % 360 == 180:
            c, s = -1.0, 0.0
        elif degrees % 360 == 270:
            c, s = 0.0, -1.0
        self._pre(c, s, -s, c, 0.0, 0.0)

    def shear(self, sh, sv):
        self._pre(1.0, sv, sh, 1.0, 0.0, 0.0)

    def map(self, x, y):
        m11, m12, m21, m22, dx, dy = self.m
        return m11 * x + m21 * y + dx, m12 * x + m22 * y + dy


def _near(a: float, b: float) -> bool:
    return abs(a - b) < 0.000001


def delivered_size(src_w: int, src_h: int, max_scale_x: float, max_scale_y: float, *,
                   still: bool = False) -> Tuple[int, int]:
    """Size libopenshot decodes a SCALE_NONE source at (export at the timeline's own size).

    The reader is asked for a "max box" of the source size times the largest
    ``scale_x`` / ``scale_y`` keyframe value (``QtImageReader::calculate_max_size``,
    ``FFmpegReader::ProcessVideoPacket``; web engine ``render/plan/reader.ts``
    ``maxBox`` / ``deliveredSource``). Stills scale to that box either way;
    video only scales down, keeping its aspect. SCALE_NONE then draws the
    delivered image at ``scale`` again, so a 1920x1080 video at scale 0.5
    shows 480x270.
    """
    box_w = math.trunc(float(src_w) * float(max_scale_x))
    box_h = math.trunc(float(src_h) * float(max_scale_y))
    if still:
        return qsize_scaled(src_w, src_h, box_w, box_h, "keep") if box_w > 0 and box_h > 0 else (src_w, src_h)
    if box_w != 0 and box_h != 0 and box_w < src_w and box_h < src_h:
        ratio = float(src_w) / float(src_h)
        possible_w = int(math.floor(box_h * ratio + 0.5))
        possible_h = int(math.floor(box_w / ratio + 0.5))
        if possible_w <= box_w:
            return possible_w, box_h
        return box_w, possible_h
    return src_w, src_h


def geometry(src_w: float, src_h: float, canvas_w: float, canvas_h: float, *, scale_mode: int = SCALE_FIT,
             gravity: int = GRAVITY_CENTER, scale_x: float = 1.0, scale_y: float = 1.0,
             location_x: float = 0.0, location_y: float = 0.0, rotation: float = 0.0,
             origin_x: float = 0.5, origin_y: float = 0.5, shear_x: float = 0.0, shear_y: float = 0.0,
             alpha: float = 1.0, margin: float = 0.0, time: Optional[float] = None,
             frame: Optional[float] = None, max_scale_x: Optional[float] = None,
             max_scale_y: Optional[float] = None, still: bool = False) -> Geometry:
    """Canvas placement of a *src_w* x *src_h* source with already-evaluated property values.

    For SCALE_NONE pass the largest ``scale_x`` / ``scale_y`` of the whole
    clip (*max_scale_x/y*, default: this instant's) and whether the source is
    a *still*: libopenshot decodes at that size first (:func:`delivered_size`).
    """
    src_w_i = max(1, int(round(float(src_w or 0) or canvas_w)))
    src_h_i = max(1, int(round(float(src_h or 0) or canvas_h)))
    width, height = float(canvas_w), float(canvas_h)
    margin = max(0.0, min(0.5, float(margin)))
    margin_px = margin * min(width, height)
    layout_x = layout_y = margin_px
    layout_w = max(1.0, width - margin_px * 2)
    layout_h = max(1.0, height - margin_px * 2)
    if int(scale_mode) == SCALE_NONE:
        size_w, size_h = delivered_size(src_w_i, src_h_i, scale_x if max_scale_x is None else max_scale_x,
                                        scale_y if max_scale_y is None else max_scale_y, still=still)
    else:
        size_w, size_h = scaled_source_size(src_w_i, src_h_i, int(scale_mode), math.trunc(layout_w),
                                            math.trunc(layout_h))
    ssw = size_w * float(scale_x)
    ssh = size_h * float(scale_y)

    cx = layout_x + (layout_w - ssw) / 2
    cy = layout_y + (layout_h - ssh) / 2
    rx = layout_x + layout_w - ssw
    by = layout_y + (layout_h - ssh)
    g = int(gravity)
    x = {GRAVITY_TOP_LEFT: layout_x, GRAVITY_TOP: cx, GRAVITY_TOP_RIGHT: rx, GRAVITY_LEFT: layout_x,
         GRAVITY_CENTER: cx, GRAVITY_RIGHT: rx, GRAVITY_BOTTOM_LEFT: layout_x, GRAVITY_BOTTOM: cx,
         GRAVITY_BOTTOM_RIGHT: rx}.get(g, 0.0)
    y = {GRAVITY_TOP_LEFT: layout_y, GRAVITY_TOP: layout_y, GRAVITY_TOP_RIGHT: layout_y, GRAVITY_LEFT: cy,
         GRAVITY_CENTER: cy, GRAVITY_RIGHT: cy, GRAVITY_BOTTOM_LEFT: by, GRAVITY_BOTTOM: by,
         GRAVITY_BOTTOM_RIGHT: by}.get(g, 0.0)

    lx, ly = float(location_x), float(location_y)
    if int(scale_mode) == SCALE_CROP:
        def offset(location, anchored, canvas, clip_size):
            return location * (anchored + clip_size) if location < 0 else location * (canvas - anchored)
        x = x + offset(lx, x - layout_x, layout_w, ssw)
        y = y + offset(ly, y - layout_y, layout_h, ssh)
    else:
        x = width * lx + x
        y = height * ly + y

    r, shx, shy = float(rotation), float(shear_x), float(shear_y)
    ox, oy = float(origin_x), float(origin_y)
    t = _Affine()
    if not _near(x, 0) or not _near(y, 0):
        t.translate(x, y)
    oxo, oyo = ssw * ox, ssh * oy
    if not _near(r, 0) or not _near(shx, 0) or not _near(shy, 0):
        t.translate(oxo, oyo)
        t.rotate(r)
        t.shear(shx, shy)
        t.translate(-oxo, -oyo)
    sws = (size_w / src_w_i) * float(scale_x)
    shs = (size_h / src_h_i) * float(scale_y)
    if not _near(sws, 1) or not _near(shs, 1):
        t.scale(sws, shs)

    center = t.map(src_w_i / 2.0, src_h_i / 2.0)
    anchor = (x + oxo, y + oyo)
    return Geometry(
        center_x=center[0], center_y=center[1], width=ssw, height=ssh, scale_x=sws, scale_y=shs,
        rotation=r, opacity=float(alpha), x=x, y=y, anchor_x=anchor[0], anchor_y=anchor[1],
        origin_x=ox, origin_y=oy, shear_x=shx, shear_y=shy, source_width=src_w_i, source_height=src_h_i,
        matrix=tuple(t.m), time=time, frame=frame)  # type: ignore[arg-type]


TRANSFORM_KEYS = ("alpha", "location_x", "location_y", "scale_x", "scale_y", "rotation", "origin_x", "origin_y",
                  "shear_x", "shear_y")


def clip_geometry(clip, time: float, canvas_w: float, canvas_h: float, *, src_w: Optional[float] = None,
                  src_h: Optional[float] = None) -> Geometry:
    """Geometry of a :class:`~classes.handoff.timeline_view.ClipView` at timeline second *time*.

    The source size defaults to the clip's file (width/height); pass *src_w* /
    *src_h* for media whose decoded size differs (rotated phone video).
    """
    curves = clip.curves
    frame = curves["alpha"].frame_at(time) if "alpha" in curves else None
    values = {k: curves[k].value_at(time) for k in TRANSFORM_KEYS if k in curves}
    margin = curves["margin"].value_at(time) if "margin" in curves else 0.0
    file = clip.file
    sw = src_w if src_w is not None else (file.width if file is not None else None)
    sh = src_h if src_h is not None else (file.height if file is not None else None)

    def _max(key: str) -> Optional[float]:  # Keyframe::GetMaxPoint().co.Y
        curve = curves.get(key)
        return max((p.value for p in curve.points), default=None) if curve is not None else None

    return geometry(sw or canvas_w, sh or canvas_h, canvas_w, canvas_h, scale_mode=clip.scale_mode,
                    gravity=clip.gravity, margin=margin, time=time, frame=frame, max_scale_x=_max("scale_x"),
                    max_scale_y=_max("scale_y"), still=bool(file is not None and file.is_still), **values)


def clip_geometry_keys(clip, canvas_w: float, canvas_h: float, *, src_w: Optional[float] = None,
                       src_h: Optional[float] = None) -> list:
    """Geometry at every keyframe time of the clip's transform curves inside its visible window.

    Always includes the clip's first and last visible frame, so a static clip
    yields its placement and an animated one the poses an exporter keys
    (interpolate between them with each curve's own segments).
    """
    from classes.handoff.keyframes import union_times
    last = clip.timeline_out - 1.0 / float(clip.fps) if clip.timeline_out > clip.timeline_in else clip.timeline_in
    times = union_times(*(clip.curves.get(k) for k in TRANSFORM_KEYS), start=clip.timeline_in, end=last)
    return [clip_geometry(clip, t, canvas_w, canvas_h, src_w=src_w, src_h=src_h) for t in times]
