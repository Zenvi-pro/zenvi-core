"""Helpers shared by the titles-text tools: placement, fonts, colours, overlay carrier.

No tools are registered here. Everything that touches project data or Qt runs
on the GUI thread (callers wrap it in ``_base.on_main``); the rest is pure.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
from typing import Optional, Tuple

from classes.editor_tools._base import (
    BEZIER, ToolError, clip_extent, ensure_unlocked, get_app, is_locked, keyframe, layers,
    on_main, parse_color, project, project_fps, resolve_layer, track_label, ui_track_number,
)
from classes.logger import log

# libopenshot GravityType (openshot.GRAVITY_*)
GRAVITY = {
    "top_left": 0, "top_center": 1, "top_right": 2,
    "center_left": 3, "center": 4, "center_right": 5,
    "bottom_left": 6, "bottom_center": 7, "bottom_right": 8,
}
SCREEN_POSITIONS = list(GRAVITY)
TRACK_STEP = 1000000          # actionAddTrack_trigger numbers new tracks max + 1000000
ONE_FRAME_TOLERANCE = 0.5     # frames of overlap ignored when checking a track window


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------

class _Point:
    """What Timeline.addClip reads from its QPointF argument (only .x())."""

    def __init__(self, x: float):
        self._x = float(x)

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return 0.0


def color_hex_alpha(value, what: str) -> Tuple[str, float]:
    """'#RRGGBB[AA]' / 'rgb(a)(...)' / name -> ('#rrggbb', alpha 0..1); ToolError names the argument."""
    try:
        r, g, b, a = parse_color(value)
    except ToolError as exc:
        raise ToolError(f"{what}: {exc}") from None
    return "#%02x%02x%02x" % (r, g, b), round(a / 255.0, 4)


def color_keyframes(hex_color: str, alpha: float = 1.0) -> dict:
    """OpenShot Color JSON (red/green/blue/alpha keyframes, 0-255)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": keyframe([(1, r)]), "green": keyframe([(1, g)]), "blue": keyframe([(1, b)]),
            "alpha": keyframe([(1, round(255 * max(0.0, min(1.0, alpha))))])}


def constant(value: float) -> dict:
    return keyframe([(1, float(value), BEZIER)])


# Background-safe tools wait this long for their GUI-thread hops: a busy editor (thumbnails,
# autosave, font scans) can take far longer than the 30 s default, and the worker can wait.
PRECHECK_TIMEOUT = 90
COMMIT_TIMEOUT = 240


class CommitTimeout(ToolError):
    """The GUI thread did not finish the change in time; it may still complete."""


def commit_on_main(func, *args):
    """Run the mutating part on the GUI thread; a timeout becomes a clear, honest error.

    When the work outlives the wait it keeps running (in this call's undo step), so callers
    must not clean up files it uses (catch CommitTimeout before Exception).
    """
    from classes.tool_handlers import MainThreadStillRunning, MainThreadTimeout
    try:
        return on_main(func, *args, timeout=COMMIT_TIMEOUT)
    except MainThreadStillRunning:
        raise CommitTimeout(f"the editor was too busy to finish within {COMMIT_TIMEOUT}s; the change is still "
                            "running and will land shortly -- check get_timeline_state_tool before trying "
                            "again") from None
    except MainThreadTimeout:
        # withdrawn before it started: nothing happened and the caller may clean up
        raise ToolError(f"the editor was too busy to start the change within {COMMIT_TIMEOUT}s; nothing "
                        "was changed, try again") from None


def precheck_on_main(func, *args):
    """A read-only GUI-thread hop (validation, planning) with a generous timeout."""
    from classes.tool_handlers import MainThreadTimeout
    try:
        return on_main(func, *args, timeout=PRECHECK_TIMEOUT)
    except MainThreadTimeout:
        raise ToolError(f"the editor did not respond within {PRECHECK_TIMEOUT}s; nothing was changed") from None


def frame_seconds() -> float:
    return 1.0 / max(1.0, project_fps())


def project_size() -> Tuple[int, int]:
    p = project()
    try:
        return int(p.get("width") or 1920), int(p.get("height") or 1080)
    except (TypeError, ValueError):
        return 1920, 1080


def is_vertical() -> bool:
    w, h = project_size()
    return h > w


def title_dir() -> str:
    from classes import info
    path = info.TITLE_PATH
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Fonts (Qt's font database, read on the GUI thread)
# ---------------------------------------------------------------------------

def installed_font_families() -> Optional[list]:
    """Installed font families, or None when Qt cannot tell (headless tests)."""
    try:
        from qt_api import QFontDatabase
        try:
            families = QFontDatabase().families()
        except TypeError:  # Qt6: static
            families = QFontDatabase.families()
        families = list(families)
        if families and all(isinstance(f, str) for f in families):
            return families
    except Exception:
        log.debug("font database unavailable", exc_info=True)
    return None


def resolve_font(name: str, families: Optional[list]) -> str:
    """An installed family for *name* (exact, then prefix/contains), else ToolError with suggestions."""
    wanted = str(name or "").strip().strip("'\"")
    if not wanted or families is None:
        return wanted
    low = wanted.lower()
    for fam in families:
        if fam.lower() == low:
            return fam
    starts = [f for f in families if f.lower().startswith(low)]
    if starts:
        return sorted(starts, key=len)[0]
    contains = [f for f in families if low in f.lower()]
    if contains:
        return sorted(contains, key=len)[0]
    close = difflib.get_close_matches(wanted, families, n=8, cutoff=0.5)
    hint = ", ".join(close) if close else ", ".join(sorted(families)[:8])
    raise ToolError(f"font {wanted!r} is not installed; installed fonts like it: {hint}")


# ---------------------------------------------------------------------------
# Tracks: where an overlay goes
# ---------------------------------------------------------------------------

def _is_visual(clip_data: dict) -> bool:
    reader = clip_data.get("reader") if isinstance(clip_data.get("reader"), dict) else {}
    if reader.get("has_video") is False or reader.get("media_type") == "audio":
        return False
    hv = clip_data.get("has_video")
    if isinstance(hv, dict):
        pts = hv.get("Points") or []
        if pts and float((pts[0].get("co") or {}).get("Y", 1)) == 0.0 and len(pts) == 1:
            return False
    return True


def clips_in_window(layer_num: Optional[int], start: float, end: float, exclude_ids=()) -> list:
    from classes.query import Clip
    tol = ONE_FRAME_TOLERANCE * frame_seconds()
    out = []
    for c in Clip.filter():
        data = c.data if isinstance(c.data, dict) else {}
        if c.id in exclude_ids:
            continue
        if layer_num is not None and int(data.get("layer") or 0) != int(layer_num):
            continue
        s, e, _ = clip_extent(data)
        if s < end - tol and e > start + tol:
            out.append(c)
    return out


def plan_overlay_track(start: float, end: float, track: str = "", exclude_ids=()) -> Tuple[int, bool]:
    """(layer number, needs_new_track) for an overlay shown over [start, end).

    Explicit *track*: it must exist, be unlocked and be free in the window.
    Otherwise: the lowest free, unlocked track above every clip with video in
    the window; a new track on top when none is free. Read-only.
    """
    if str(track or "").strip():
        layer = resolve_layer(track)
        ensure_unlocked(layer)
        busy = clips_in_window(layer, start, end, exclude_ids)
        if busy:
            d = busy[0].data
            s, e, _ = clip_extent(d)
            raise ToolError(
                f"track {ui_track_number(layer) or layer} already has {d.get('title') or busy[0].id!r} at "
                f"{s:.2f}-{e:.2f}s during {start:.2f}-{end:.2f}s; pick another track or leave track "
                f"empty to use the first free track above the video")
        return layer, False
    numbers = sorted(int(t.get("number") or 0) for t in layers())
    visual = [c for c in clips_in_window(None, start, end, exclude_ids) if _is_visual(c.data)]
    floor = max((int(c.data.get("layer") or 0) for c in visual), default=None)
    for num in numbers:
        if floor is not None and num <= floor:
            continue
        if is_locked(num):
            continue
        if not clips_in_window(num, start, end, exclude_ids):
            return num, False
    top = numbers[-1] if numbers else 0
    return top + TRACK_STEP, True


def create_track(number: int, label: str) -> int:
    """Add a track lane on top (actionAddTrack_trigger's record + a label). GUI thread."""
    from classes.query import Track
    t = Track()
    t.data = {"number": int(number), "y": 0, "label": str(label or ""), "lock": False}
    t.save()
    return int(number)


def overlay_track(start: float, end: float, track: str, label: str, exclude_ids=()) -> Tuple[int, bool]:
    layer, new = plan_overlay_track(start, end, track, exclude_ids)
    if new:
        create_track(layer, label)
    return layer, new


def track_info(layer_num: int) -> dict:
    return {"track": ui_track_number(layer_num), "layer": int(layer_num), "track_label": track_label(layer_num)}


# ---------------------------------------------------------------------------
# Files and clips (the editor's own code paths)
# ---------------------------------------------------------------------------

def import_media_file(path: str, name: str = "", extra: Optional[dict] = None):
    """Add *path* to Project Files like the Title Editor's Save does (no indexing). GUI thread."""
    from classes.query import File
    existing = File.get(path=path)
    if existing:
        return existing
    win = get_app().window
    win.files_model.add_files([path], quiet=True, prevent_image_seq=True,
                              prevent_recent_folder=True, skip_indexing=True)
    f = File.get(path=path)
    if not f:
        raise ToolError(f"could not import {os.path.basename(path)} into Project Files")
    changes = {}
    if name:
        changes["name"] = name
    if extra:
        changes.update(extra)
    if changes:
        f.data = changes
        f.save()
        f = File.get(path=path)
    return f


def place_clip(file_id: str, position: float, duration: float, layer: int, *,
               title: str = "", props: Optional[dict] = None) -> dict:
    """Timeline.addClip (the drop path) + trim/props in the same undo step. GUI thread."""
    from classes.editor_tools._base import timeline_ui
    from classes.query import Clip
    timeline = timeline_ui()
    new_clip = timeline.addClip(file_id, _Point(position), int(layer), call_manual_move=False)
    if not isinstance(new_clip, dict) or not new_clip.get("id"):
        raise ToolError("the timeline did not create the clip")
    start = float(new_clip.get("start") or 0.0)
    new_clip["start"] = start
    new_clip["end"] = start + float(duration)
    new_clip["duration"] = float(duration)
    if title:
        new_clip["title"] = title
    for key, value in (props or {}).items():
        new_clip[key] = copy.deepcopy(value)
    timeline.update_clip_data(new_clip, only_basic_props=False, ignore_refresh=False)
    placed = Clip.get(id=new_clip["id"])
    return placed.data if placed else new_clip


def fade_alpha(start: float, end: float, fade_in: float, fade_out: float, base: float = 1.0) -> Optional[dict]:
    """Alpha keyframes fading in over *fade_in* s and out over *fade_out* s (clip-local frames)."""
    fade_in, fade_out = max(0.0, float(fade_in or 0)), max(0.0, float(fade_out or 0))
    if fade_in <= 0 and fade_out <= 0:
        return None
    fps = project_fps()
    length = max(frame_seconds(), end - start)
    if fade_in + fade_out > length:
        scale = length / (fade_in + fade_out)
        fade_in, fade_out = fade_in * scale, fade_out * scale
    first = round(start * fps) + 1
    last = round(end * fps) + 1
    points = []
    if fade_in > 0:
        points += [(first, 0.0), (first + max(1, round(fade_in * fps)), base)]
    else:
        points.append((first, base))
    if fade_out > 0:
        points += [(last - max(1, round(fade_out * fps)), base), (last, 0.0)]
    dedup = {}
    for x, y in points:
        dedup[x] = y
    return keyframe(sorted(dedup.items()))


def refresh_file_and_clips(file_id: str, clip_ids=()) -> None:
    """Thumbnails + preview after an SVG changed (what actionEditTitle emits)."""
    win = get_app().window
    try:
        win.FileUpdated.emit(file_id)
        for cid in clip_ids:
            win.ThumbnailUpdated.emit(cid, 1)
        win.refreshFrameSignal.emit()
    except Exception:
        log.debug("title refresh signals skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Effects (libopenshot JSON) and the transparent overlay carrier
# ---------------------------------------------------------------------------

def new_effect_json(class_name: str) -> dict:
    """A new effect as the Effects dock creates it (EffectInfo().CreateEffect + a project id).

    GUI thread: never build or render libopenshot objects from a plain Python thread
    (Qt's font cache can deadlock against the GIL there).
    """
    import openshot
    effect = openshot.EffectInfo().CreateEffect(class_name)
    if effect is None:
        raise ToolError(f"this libopenshot build has no {class_name} effect")
    effect.Id(project().generate_id())
    data = json.loads(effect.Json())
    try:
        from windows.views.timeline_backend.colors import effect_color_hex
        data.setdefault("ui", {}).setdefault("icon_color", effect_color_hex(data))
    except Exception:
        pass
    return data


def fresh_effect(template: dict) -> dict:
    """A copy of an effect made by new_effect_json with its own project id (no libopenshot call)."""
    data = copy.deepcopy(template)
    data["id"] = project().generate_id()
    return data


OVERLAY_ROLE = "text_overlay"
OVERLAY_FILE_NAME = "Text Overlay.svg"


def overlay_svg_text(width: int, height: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<rect width="%d" height="%d" fill="#000000" fill-opacity="0"/></svg>\n'
            % (width, height, width, height, width, height))


def overlay_file_path(width: int, height: int) -> str:
    """Path of the transparent carrier SVG for this project size (written if missing). Any thread."""
    from classes import title_svg
    folder = title_dir()
    path = os.path.join(folder, "Text Overlay %dx%d.svg" % (width, height))
    if not os.path.exists(path):
        doc = title_svg.parse_string(overlay_svg_text(width, height))
        title_svg.write(doc, path)
    return path


def overlay_file(path: str):
    """The carrier's project File (imported once, tagged). GUI thread."""
    return import_media_file(path, name=OVERLAY_FILE_NAME, extra={"zenvi_role": OVERLAY_ROLE})
