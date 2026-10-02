"""Project video profiles: social presets, profile search, switching the profile, custom profiles.

Workstream: project-export. "Make this an Instagram reel" = set_project_profile_tool
(profile='instagram_reel', reframe='fill'): the social preset maps to a real
profile file (FHD Vertical 1080p <fps>), the project switches through the Choose
Profile dialog's own code path (``MainWindow.apply_project_profile``), and
full-frame landscape clips are cropped to fill the new frame instead of being
letterboxed. One undo step reverts the profile and the reframe together.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from classes.editor_tools._base import (
    ToolError, boolean, enum, get_app, integer, nullable, number, obj, ok, on_main, string,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.project_export import CHANNEL_LAYOUTS, project_snapshot, window

# name -> (width, height, default fps or None = keep the project's, export preset title, aliases)
SOCIAL_PRESETS = {
    "instagram_reel": (1080, 1920, None, "Instagram Reels",
                       ("instagram reels", "ig reel", "ig reels", "reel", "reels", "instagram_reels")),
    "instagram_story": (1080, 1920, None, "Instagram Reels",
                        ("instagram stories", "ig story", "story", "stories", "instagram_stories")),
    "tiktok": (1080, 1920, None, "TikTok", ("tik tok", "tik_tok")),
    "youtube_shorts": (1080, 1920, None, "YouTube Shorts",
                       ("shorts", "youtube short", "yt shorts", "yt_shorts", "youtube_short")),
    "snapchat": (1080, 1920, None, "Snapchat", ("snap", "snapchat spotlight")),
    "facebook_reel": (1080, 1920, None, "Facebook", ("facebook reels", "fb reel", "facebook story")),
    "vertical": (1080, 1920, None, "MP4 (h.264)", ("9:16", "portrait", "vertical video", "phone", "9x16")),
    "instagram_post": (1080, 1080, None, "Instagram",
                       ("instagram square", "ig post", "instagram feed", "square", "1:1", "1x1", "instagram")),
    "instagram_portrait": (1080, 1350, None, "MP4 (h.264)",
                           ("4:5", "4x5", "instagram 4:5", "ig portrait", "portrait post", "instagram feed portrait")),
    "youtube": (1920, 1080, None, "YouTube",
                ("youtube 1080p", "youtube hd", "yt", "youtube_1080p", "youtube_hd", "1080p")),
    "youtube_2k": (2560, 1440, None, "YouTube (2K)", ("youtube 1440p", "1440p", "2k", "qhd", "wqhd")),
    "youtube_4k": (3840, 2160, None, "YouTube (4K)", ("youtube 2160p", "yt 4k", "4k youtube")),
    "facebook": (1920, 1080, None, "Facebook", ("fb", "facebook video")),
    "x": (1920, 1080, None, "Twitter / X", ("twitter", "x twitter", "x_twitter", "tweet")),
    "linkedin": (1920, 1080, None, "LinkedIn", ("linked in",)),
    "vimeo": (1920, 1080, None, "Vimeo", ()),
    "landscape": (1920, 1080, None, "MP4 (h.264)", ("16:9", "16x9", "widescreen", "hd", "full hd", "fhd")),
    "hd_720p": (1280, 720, None, "MP4 (h.264)", ("720p", "hd 720p")),
    "4k": (3840, 2160, None, "MP4 (h.264)", ("uhd", "4k uhd", "2160p", "ultra hd")),
    "cinema": (1920, 1080, 24, "MP4 (h.264)", ("film", "cinematic", "movie", "24fps", "24 fps")),
    "cinema_4k": (3840, 2160, 24, "MP4 (h.264)", ("4k cinema", "4k film", "cinematic 4k", "4k 24 fps")),
}

# A frame-rate change rescales every keyframe and reloads the timeline on the GUI thread.
PROFILE_TIMEOUT = 120

# libopenshot ScaleType / GravityType
SCALE_CROP, SCALE_FIT = 0, 1
GRAVITY_CENTER = 4


def _norm(text: str) -> str:
    return re.sub(r"[\s\-/]+", "_", str(text or "").strip().lower()).strip("_")


def social_preset(name: str):
    """(key, spec) for a social preset name or alias, else (None, None)."""
    key = _norm(name)
    key = re.sub(r"_(profile|preset|format|video)$", "", key)
    if key in SOCIAL_PRESETS:
        return key, SOCIAL_PRESETS[key]
    for k, spec in SOCIAL_PRESETS.items():
        if key in {_norm(a) for a in spec[4]}:
            return k, spec
    return None, None


def catalog():
    from classes.project_profile import profile_catalog
    return profile_catalog()


def _fps_value(record) -> float:
    return record["fps_num"] / float(record["fps_den"])


def match_profile(width: int, height: int, fps: Optional[float] = None) -> Optional[dict]:
    """Best existing profile of this size (and fps): progressive, square pixels, not 360."""
    candidates = [p for p in catalog() if p["width"] == int(width) and p["height"] == int(height)]
    if fps:
        exact = [p for p in candidates if abs(_fps_value(p) - float(fps)) < 0.011]
        if not exact:
            return None
        candidates = exact
    if not candidates:
        return None

    def rank(p):
        return (p["interlaced"], p["spherical"], p["pixel_ratio"] != {"num": 1, "den": 1},
                bool(p.get("user")), "anamorphic" in p["description"].lower(), len(p["description"]))
    return sorted(candidates, key=rank)[0]


def ensure_profile(width: int, height: int, fps_num: int, fps_den: int) -> dict:
    """An existing profile of this size and exact fps, or a new user profile (profile editor format)."""
    from classes import info
    from classes.project_profile import reduced_display_ratio, write_user_profile

    fps = fps_num / float(fps_den)
    found = match_profile(width, height, fps)
    if found:
        return found
    fps_text = f"{fps:.2f}".rstrip("0").rstrip(".")
    record = {
        "description": f"Custom {width}x{height} {fps_text} fps",
        "width": int(width), "height": int(height), "fps_num": int(fps_num), "fps_den": int(fps_den),
        "display_ratio": reduced_display_ratio(width, height),
        "pixel_ratio": {"num": 1, "den": 1}, "interlaced": False, "spherical": False,
    }
    for p in catalog():
        if p["description"] == record["description"]:
            return p
    record["path"] = os.path.join(info.USER_PROFILES_PATH, _key(record))
    record["key"] = os.path.basename(record["path"])
    try:
        write_user_profile(record, info.USER_PROFILES_PATH)
    except OSError as exc:
        raise ToolError(f"no built-in profile is {width}x{height} at {fps_text} fps and a custom one "
                        f"could not be saved: {exc}") from exc
    record["user"] = True
    return record


def _key(record):
    from classes.project_profile import profile_key
    return profile_key(record)


def _current_fps() -> float:
    fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    return float(fps.get("num") or 30) / float(fps.get("den") or 1)


def _size_for(width, height, fps):
    """Profile at this size: requested fps, else the project's, else 30, else any."""
    for want in (fps, None if fps else _current_fps(), None if fps else 30.0):
        if want:
            found = match_profile(width, height, want)
            if found:
                return found
    if fps:
        from classes.project_profile import fps_fraction
        num, den = fps_fraction(fps)
        return ensure_profile(width, height, num, den)
    return match_profile(width, height)


_SIZE_RE = re.compile(r"(\d{3,5})\s*[x×*]\s*(\d{3,5})(?:\D+(\d+(?:\.\d+)?)\s*(?:fps|p)?)?", re.I)


def resolve_profile_request(profile: str = "", fps: Optional[float] = None) -> dict:
    """A profile record for a name, key, social preset or 'WIDTHxHEIGHT[@fps]' text."""
    text = str(profile or "").strip()
    if not text:
        raise ToolError("name a profile or social preset (see list_project_profiles_tool)")
    key, spec = social_preset(text)
    if spec:
        width, height, default_fps, _export, _aliases = spec
        found = _size_for(width, height, fps or default_fps)
        if not found:
            raise ToolError(f"no profile is {width}x{height}")
        return found
    lowered = text.lower()
    profiles = catalog()
    for p in profiles:
        if p["description"].lower() == lowered or p["key"].lower() == lowered:
            if fps and abs(_fps_value(p) - float(fps)) > 0.011:
                return _size_for(p["width"], p["height"], fps)
            return p
    m = _SIZE_RE.search(text)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
        want = fps or (float(m.group(3)) if m.group(3) else None)
        found = _size_for(width, height, want)
        if found:
            return found
    tokens = [t for t in re.split(r"[\s,]+", lowered) if t]
    hits = [p for p in profiles if all(t in p["description"].lower() for t in tokens)]
    if fps:
        hits = [p for p in hits if abs(_fps_value(p) - float(fps)) < 0.011] or hits
    if len(hits) == 1:
        return hits[0]
    if hits:
        names = ", ".join(sorted({h["description"] for h in hits})[:6])
        raise ToolError(f"'{text}' matches {len(hits)} profiles ({names}...); use the exact name or add fps")
    raise ToolError(f"no profile or social preset matches '{text}'. Social presets: "
                    + ", ".join(SOCIAL_PRESETS) + ". Or give WIDTHxHEIGHT, e.g. '1080x1920'.")


def _flip(record: dict, orientation: str, fps: Optional[float]) -> dict:
    """Same profile family turned to landscape / portrait / square."""
    w, h = record["width"], record["height"]
    if orientation == "square":
        side = min(w, h)
        w, h = side, side
    elif orientation == "portrait" and w > h:
        w, h = h, w
    elif orientation == "landscape" and h > w:
        w, h = h, w
    else:
        return record
    found = _size_for(w, h, fps or _fps_value(record))
    if not found:
        raise ToolError(f"no profile is {w}x{h}")
    return found


# ---------------------------------------------------------------------------
# Reframe clips for a new frame shape
# ---------------------------------------------------------------------------

def _constant(kf, value) -> bool:
    pts = (kf or {}).get("Points") if isinstance(kf, dict) else None
    if not pts:
        return True
    return all(abs(float(p.get("co", {}).get("Y", value)) - value) < 1e-6 for p in pts)


def _source_aspect(clip: dict) -> Optional[float]:
    reader = clip.get("reader") or {}
    dar = reader.get("display_ratio") or {}
    try:
        if dar.get("num") and dar.get("den"):
            return float(dar["num"]) / float(dar["den"])
        if reader.get("width") and reader.get("height"):
            return float(reader["width"]) / float(reader["height"])
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def plan_reframe(mode: str, width: int, height: int) -> tuple:
    """(changes [(clip_id, {scale, gravity})], skipped [{id, reason}], letterboxed ids) for a frame size."""
    from classes.editor_tools._base import is_locked

    target = float(width) / float(height)
    changes, skipped, letterboxed = [], [], []
    for clip in get_app().project.get("clips") or []:
        cid = clip.get("id")
        reader = clip.get("reader") or {}
        path = str(reader.get("path") or clip.get("title") or "")
        if not reader.get("has_video", True) or reader.get("media_type") == "audio":
            continue
        aspect = _source_aspect(clip)
        if aspect is None or abs(aspect - target) / target < 0.01:
            continue
        scale = int(clip.get("scale") if clip.get("scale") is not None else SCALE_FIT)
        if mode == "keep":
            if scale == SCALE_FIT and not path.lower().endswith(".svg"):
                letterboxed.append(cid)
            continue
        if path.lower().endswith(".svg"):
            skipped.append({"timeline_clip_id": cid, "reason": "title (SVG) kept as designed"})
            continue
        full_frame = (_constant(clip.get("scale_x"), 1.0) and _constant(clip.get("scale_y"), 1.0)
                      and _constant(clip.get("location_x"), 0.0) and _constant(clip.get("location_y"), 0.0))
        if not full_frame:
            skipped.append({"timeline_clip_id": cid, "reason": "resized or moved (picture-in-picture) clip kept"})
            continue
        want = SCALE_CROP if mode == "fill" else SCALE_FIT
        if scale == want and int(clip.get("gravity", GRAVITY_CENTER)) == GRAVITY_CENTER:
            continue
        if is_locked(int(clip.get("layer") or 0)):
            skipped.append({"timeline_clip_id": cid, "reason": "track is locked"})
            continue
        changes.append((cid, {"scale": want, "gravity": GRAVITY_CENTER}))
    return changes, skipped, letterboxed


def apply_profile_and_reframe(record: dict, reframe: str) -> dict:
    """Switch profile (dialog core) and reframe clips inside one transaction; GUI thread."""
    from classes.project_profile import profile_differs

    app = get_app()
    changes, skipped, letterboxed = plan_reframe(reframe, record["width"], record["height"])
    changed = profile_differs(app.project, record)
    # Reframing only touches clips already placed; the receipt warns when there are none.
    video_clips = sum(1 for c in app.project.get("clips") or []
                      if (c.get("reader") or {}).get("has_video", True)
                      and (c.get("reader") or {}).get("media_type") != "audio")
    if not changed and not changes:
        return {"changed": False, "reframed": [], "skipped": skipped, "letterboxed": letterboxed,
                "video_clips": video_clips}

    def _reframe():
        for cid, values in changes:
            app.updates.update(["clips", {"id": cid}], values)

    if changed:
        window().apply_project_profile(record, before_seek=_reframe)
    else:
        _reframe()
        window().refreshFrameSignal.emit()
    return {"changed": changed, "reframed": [c for c, _v in changes], "skipped": skipped,
            "letterboxed": letterboxed, "video_clips": video_clips}


def _profile_receipt(result: dict, before: dict, record: dict, reframe: str, social_key=None) -> str:
    from classes.project_profile import describe_profile

    after = describe_profile(record)
    social = SOCIAL_PRESETS[social_key][3] if social_key in SOCIAL_PRESETS else None
    if social is None:
        for spec in SOCIAL_PRESETS.values():
            if (spec[0], spec[1]) == (after["width"], after["height"]):
                social = spec[3]
                break
    parts = []
    if result["changed"]:
        parts.append(f"Project is now {after['name']} ({after['width']}x{after['height']} {after['aspect']}, "
                     f"{after['fps']} fps)")
    else:
        parts.append(f"Project already uses {after['name']}")
    if result["reframed"]:
        parts.append(f"{len(result['reframed'])} clip(s) set to {'crop-to-fill' if reframe == 'fill' else 'fit'}")
    if result["letterboxed"]:
        parts.append(f"{len(result['letterboxed'])} clip(s) of another shape will show bars; "
                     "call again with reframe='fill' to crop them to fill the frame")
    if reframe == "fill" and not result.get("video_clips"):
        parts.append("the timeline has no video clips yet, so nothing was reframed: clips placed later are "
                     "fitted with bars when their shape differs, so call set_project_profile_tool(reframe='fill') "
                     "again once they are on the timeline")
    return ok("; ".join(parts) + ".", changed=result["changed"], profile=after,
              previous_profile={k: before[k] for k in ("profile", "width", "height", "fps", "aspect")},
              reframe=reframe, reframed_clip_ids=result["reframed"], skipped=result["skipped"],
              letterboxed_clip_ids=result["letterboxed"], suggested_export_preset=social)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@editor_tool(
    "list_project_profiles_tool",
    label="List video profiles",
    schema=obj({
        "query": string("Words in the profile name, e.g. 'vertical 1080p', 'PAL', '4K'.", ""),
        "width": integer("Only profiles this wide (pixels). 0 = any.", 0, minimum=0),
        "height": integer("Only profiles this tall (pixels). 0 = any.", 0, minimum=0),
        "fps": number("Only this frame rate, e.g. 24, 29.97, 60. 0 = any.", 0, minimum=0),
        "aspect": string("Only this aspect, e.g. '16:9', '9:16', '1:1', '4:5'.", ""),
        "orientation": enum(["", "landscape", "portrait", "square"], "Only this orientation.", ""),
        "include_social_presets": boolean("Also list the social preset names (instagram_reel, tiktok, ...).", True),
        "limit": integer("Maximum profiles returned.", 30, minimum=1, maximum=500),
    }),
    read_only=True,
    covers=("project.profile_list",),
)
def list_project_profiles(query="", width=0, height=0, fps=0, aspect="", orientation="",
                          include_social_presets=True, limit=30):
    """Search the 425 built-in video profiles plus the user's custom ones (the Choose Profile dialog).

    Also lists the social presets that set_project_profile_tool understands
    (instagram_reel 1080x1920, instagram_post 1080x1080, instagram_portrait
    1080x1350, tiktok, youtube_shorts, youtube, youtube_4k, cinema 24 fps...).
    Each profile has name (pass it to set_project_profile_tool), key, size,
    fps, aspect, orientation, interlaced, spherical and user (custom).
    """
    from classes.project_profile import describe_profile

    tokens = [t for t in re.split(r"[\s,]+", (query or "").lower()) if t]
    rows = []
    for p in catalog():
        d = describe_profile(p)
        if tokens and not all(t in (p["description"] + " " + p["key"]).lower() for t in tokens):
            continue
        if width and d["width"] != width or height and d["height"] != height:
            continue
        if fps and abs(d["fps"] - float(fps)) > 0.011:
            continue
        if aspect and d["aspect"] != aspect.replace("x", ":").strip():
            continue
        if orientation and d["orientation"] != orientation:
            continue
        rows.append(d)
    total = len(rows)
    rows.sort(key=lambda d: (d["user"] is False, -d["width"] * d["height"], d["fps"]))
    out = {"profiles": rows[:limit], "total_matches": total,
           "current": get_app().project.get("profile") or ""}
    if include_social_presets:
        out["social_presets"] = [
            {"name": k, "width": s[0], "height": s[1], "fps": s[2] or "project fps", "export_preset": s[3],
             "aliases": list(s[4])[:4]}
            for k, s in SOCIAL_PRESETS.items()]
    return ok(f"{total} profile(s) match" + (f"; showing {min(total, limit)}" if total > limit else "") + ".", **out)


_PROFILE_ARGS = {
    "profile": string("Profile name ('FHD Vertical 1080p 30 fps'), key ('01080x1920p0030_09-16'), social preset "
                      "('instagram_reel', 'tiktok', 'youtube_shorts', 'instagram_post', 'instagram_portrait', "
                      "'youtube', 'youtube_4k', 'cinema', '4k') or 'WIDTHxHEIGHT'. Empty = keep the current one.", ""),
    "width": integer("Frame width in pixels (with height) instead of a profile name. 0 = unchanged.", 0,
                     minimum=0, maximum=16384),
    "height": integer("Frame height in pixels (with width). 0 = unchanged.", 0, minimum=0, maximum=16384),
    "fps": number("Frame rate, e.g. 24, 25, 29.97, 30, 60. 0 = keep (or the preset's).", 0, minimum=0, maximum=240),
    "orientation": enum(["", "landscape", "portrait", "square"],
                        "Turn the (current or named) profile to this shape: 'portrait' makes 1920x1080 -> 1080x1920.", ""),
    "from_file_id": string("Match a project file's size and frame rate (media_bin_file_id from list_files_tool).", ""),
    "reframe": enum(["keep", "fit", "fill"],
                    "Existing full-frame clips whose shape differs from the new frame: keep = unchanged (letterboxed/"
                    "pillarboxed), fit = whole picture with bars, fill = crop to fill, centred. Titles and "
                    "picture-in-picture clips are never changed.", "keep"),
}


def _target_record(profile, width, height, fps, orientation, from_file_id):
    from classes.project_profile import fps_fraction, project_profile_values
    from classes.query import File

    fps = float(fps or 0) or None
    if from_file_id:
        f = File.get(id=str(from_file_id).strip())
        if not f:
            raise ToolError(f"no project file with id={from_file_id!r}")
        d = f.data
        if not d.get("has_video") or d.get("media_type") == "audio":
            raise ToolError("that file has no picture; pick a video or image file")
        fw, fh = int(d.get("width") or 0), int(d.get("height") or 0)
        ffps = d.get("fps") or {}
        file_fps = fps or (float(ffps.get("num") or 30) / float(ffps.get("den") or 1)
                           if d.get("media_type") == "video" else _current_fps())
        num, den = fps_fraction(file_fps)
        return ensure_profile(fw, fh, num, den)
    if width or height:
        if not (width and height):
            raise ToolError("give both width and height")
        if width % 2 or height % 2:
            raise ToolError("width and height must be even numbers (H.264 needs even sizes)")
        want = fps or _current_fps()
        num, den = fps_fraction(want)
        record = ensure_profile(width, height, num, den)
    elif (profile or "").strip():
        record = resolve_profile_request(profile, fps)
    else:
        cur = project_profile_values(get_app().project)
        record = match_profile(cur["width"], cur["height"], fps or _current_fps())
        if record is None:
            num, den = fps_fraction(fps or _current_fps())
            record = ensure_profile(cur["width"], cur["height"], num, den)
    if orientation:
        record = _flip(record, orientation, fps)
    return record


@editor_tool(
    "set_project_profile_tool",
    label="Set project format",
    schema=obj(_PROFILE_ARGS),
    background_safe=True,
    covers=("project.profile_set",),
)
def set_project_profile(profile="", width=0, height=0, fps=0, orientation="", from_file_id="", reframe="keep"):
    """Change the project's video format: size, aspect and frame rate (File > Choose Profile).

    Use for "make this an Instagram reel / TikTok / YouTube Short" (profile=
    'instagram_reel' etc. -> 1080x1920 9:16), "square for Instagram",
    "switch to 4K 24 fps" (profile='cinema_4k' or profile='4k', fps=24), "make
    it vertical" (orientation='portrait'), or "match this clip" (from_file_id).
    Landscape footage in a vertical project is letterboxed unless you pass
    reframe='fill' (crop to fill, centred) -- do that when the user wants the
    picture to fill the frame. Keyframes and trims follow a frame-rate change.
    One undo step reverts the profile and the reframe. Export settings saved
    in the Export dialog are reset when the profile changes.
    Example: {"profile": "instagram_reel", "reframe": "fill"}
    """
    before = project_snapshot()
    if not any([(profile or "").strip(), width, height, fps, orientation, from_file_id]) and reframe == "keep":
        raise ToolError("say what to change: profile (name or social preset), width+height, fps, orientation, "
                        "from_file_id, or reframe")
    record = _target_record(profile, width, height, fps, orientation, from_file_id)
    result = on_main(lambda: apply_profile_and_reframe(record, reframe), timeout=PROFILE_TIMEOUT)
    return _profile_receipt(result, before, record, reframe, social_preset(profile)[0] if profile else None)


_LAYOUT_BY_NAME = {"mono": "mono", "1": "mono", "stereo": "stereo", "2": "stereo", "surround": "surround",
                   "3": "surround", "5.1": "5.1", "6": "5.1", "7.1": "7.1", "8": "7.1"}
_LAYOUT_SETTING = {"mono": "LAYOUT_MONO", "stereo": "LAYOUT_STEREO", "surround": "LAYOUT_SURROUND",
                   "5.1": "LAYOUT_5POINT1", "7.1": "LAYOUT_7POINT1"}
_SAMPLE_RATES = (22050, 44100, 48000, 96000, 192000)


@editor_tool(
    "set_project_setting_tool",
    label="Set project settings",
    schema=obj({
        "fps": nullable(number("Frame rate, e.g. 24, 29.97, 30, 60.", minimum=1, maximum=240)),
        "fps_num": nullable(integer("Exact frame-rate numerator (with fps_den), e.g. 30000.", minimum=1)),
        "fps_den": nullable(integer("Exact frame-rate denominator, e.g. 1001.", minimum=1)),
        "width": nullable(integer("Frame width in pixels (even).", minimum=16, maximum=16384)),
        "height": nullable(integer("Frame height in pixels (even).", minimum=16, maximum=16384)),
        "sample_rate": nullable(integer("Project audio sample rate: 22050, 44100, 48000, 96000 or 192000.",
                                        minimum=8000, maximum=192000)),
        "profile": string("Profile name or social preset instead of size/fps (see set_project_profile_tool).", ""),
        "preset": string("Alias of profile for social presets: 'instagram_reel', 'tiktok', 'youtube', ...", ""),
        "orientation": enum(["", "landscape", "portrait", "square"], "Turn the frame to this shape.", ""),
        "channels": string("Audio channels: 'mono', 'stereo', 'surround', '5.1', '7.1' (or 1, 2, 3, 6, 8).", ""),
        "reframe": enum(["keep", "fit", "fill"], "What happens to existing full-frame clips of another shape "
                        "(see set_project_profile_tool).", "keep"),
        "save_as_default": boolean("Also store sample_rate/channels as the Preferences default for new projects "
                                   "(takes effect after restart).", False),
    }),
    background_safe=True,
    covers=("project.audio_settings",),
)
def set_project_setting(fps=None, fps_num=None, fps_den=None, width=None, height=None, sample_rate=None,
                        profile="", preset="", orientation="", channels="", reframe="keep", save_as_default=False):
    """Change individual project settings: frame rate, frame size, audio sample rate and channels.

    Video values go through the same profile switch as set_project_profile_tool
    (a matching built-in profile, or a saved custom one for unusual sizes), so
    keyframes follow an fps change. Audio: sample_rate / channels set the
    project's mix format (used by export and the timeline); the preview may
    re-sync the sample rate to the audio device, and opening a project re-applies
    Preferences > Default Audio Sample Rate / Channels unless save_as_default=true.
    One undo step; a call that changes nothing adds none.
    Example: {"fps": 24} or {"sample_rate": 44100, "channels": "mono"}
    """
    app = get_app()
    before = project_snapshot()
    video_requested = any(v not in (None, "", 0) for v in (fps, fps_num, fps_den, width, height)) \
        or bool((profile or preset or "").strip()) or bool(orientation)
    if fps_num is not None or fps_den is not None:
        if fps_num is None or fps_den is None:
            raise ToolError("give both fps_num and fps_den")
        fps = fps_num / float(fps_den)
    layout = None
    if (channels or "").strip():
        layout = _LAYOUT_BY_NAME.get(str(channels).strip().lower())
        if not layout:
            raise ToolError("channels must be mono, stereo, surround, 5.1 or 7.1 (or 1, 2, 3, 6, 8)")
    if sample_rate is not None and sample_rate not in _SAMPLE_RATES:
        raise ToolError(f"sample_rate must be one of {', '.join(map(str, _SAMPLE_RATES))}")
    if not video_requested and sample_rate is None and not layout and reframe == "keep":
        raise ToolError("nothing to change: pass fps, width/height, profile/preset, orientation, sample_rate or channels")

    record = None
    if video_requested or reframe != "keep":
        if (width is None) != (height is None):
            cur_w, cur_h = before["width"], before["height"]
            width, height = width or cur_w, height or cur_h
        record = _target_record(profile or preset, width or 0, height or 0, fps or 0, orientation, "")
        if fps_num is not None and (record["fps_num"], record["fps_den"]) != (fps_num, fps_den):
            record = ensure_profile(record["width"], record["height"], fps_num, fps_den)

    audio = {}
    if sample_rate is not None and int(app.project.get("sample_rate") or 0) != sample_rate:
        audio["sample_rate"] = int(sample_rate)
    if layout:
        code, count = CHANNEL_LAYOUTS[layout]
        if app.project.get("channel_layout") != code or int(app.project.get("channels") or 0) != count:
            audio["channels"] = count
            audio["channel_layout"] = code

    def _apply():
        result = {"changed": False, "reframed": [], "skipped": [], "letterboxed": []}
        if record is not None:
            result = apply_profile_and_reframe(record, reframe)
        for key, value in audio.items():
            app.updates.update([key], value)
        return result

    result = on_main(_apply, timeout=PROFILE_TIMEOUT)
    if save_as_default and (sample_rate is not None or layout):
        s = app.get_settings()
        if sample_rate is not None:
            s.set("default-samplerate", int(sample_rate))
        if layout:
            s.set("default-channellayout", _LAYOUT_SETTING[layout])
        s.save()
    changed = result["changed"] or bool(result["reframed"]) or bool(audio)
    after = project_snapshot()
    notes = []
    if result["changed"]:
        notes.append(f"{after['width']}x{after['height']} at {after['fps']} fps ({after['profile']})")
    if result["reframed"]:
        notes.append(f"{len(result['reframed'])} clip(s) reframed")
    if audio:
        notes.append(f"audio {after['sample_rate']} Hz {after['channel_layout']}")
    summary = ("Updated project settings: " + ", ".join(notes) + ".") if changed else "Settings already matched."
    if result["letterboxed"]:
        summary += (f" {len(result['letterboxed'])} clip(s) of another shape will show bars; "
                    "pass reframe='fill' to crop them.")
    return ok(summary, changed=changed, width=after["width"], height=after["height"], fps=after["fps"],
              fps_num=after["fps_num"], fps_den=after["fps_den"], profile=after["profile"],
              sample_rate=after["sample_rate"], channels=after["channels"],
              channel_layout=after["channel_layout"], reframed_clip_ids=result["reframed"],
              letterboxed_clip_ids=result["letterboxed"], saved_as_default=bool(save_as_default))


@editor_tool(
    "manage_custom_profiles_tool",
    label="Custom video profiles",
    schema=obj({
        "action": enum(["create", "duplicate", "edit", "delete", "set_default"],
                       "create = new profile from the values below; duplicate = copy 'profile' with changes; "
                       "edit = change a custom profile; delete = remove a custom profile file (never deletes clips); "
                       "set_default = profile used for new projects (Preferences > Default Profile)."),
        "profile": string("Existing profile name/key (for duplicate, edit, delete, set_default).", ""),
        "name": string("Description for a created/duplicated/edited profile; must be unique.", ""),
        "width": integer("Width in pixels (even). 0 = from 'profile'.", 0, minimum=0, maximum=40000),
        "height": integer("Height in pixels (even). 0 = from 'profile'.", 0, minimum=0, maximum=40000),
        "fps": number("Frame rate. 0 = from 'profile' (or 30).", 0, minimum=0, maximum=240),
        "pixel_ratio": string("Pixel aspect 'num:den' (1:1 for square pixels).", ""),
        "interlaced": boolean("Interlaced (true) or progressive (false, default).", False),
        "spherical": boolean("360° spherical video.", False),
    }, required=["action"]),
    background_safe=True,
    covers=("project.profile_custom",),
)
def manage_custom_profiles(action, profile="", name="", width=0, height=0, fps=0, pixel_ratio="",
                           interlaced=False, spherical=False):
    """Create, duplicate, edit or delete custom video profiles, or set the default profile for new projects.

    Custom profiles live in the user's profiles folder and appear in the Choose
    Profile dialog and list_project_profiles_tool. Built-in profiles cannot be
    edited or deleted (duplicate them instead). The current project's profile
    and the default profile cannot be deleted. Does not change the open
    project -- use set_project_profile_tool for that. Not an undo step.
    """
    from classes import info
    from classes.project_profile import (
        describe_profile, fps_fraction, invalidate_catalog, reduced_display_ratio, write_user_profile,
    )

    settings = get_app().get_settings()
    base = resolve_profile_request(profile) if (profile or "").strip() else None
    if action in ("duplicate", "edit", "delete", "set_default") and base is None:
        raise ToolError(f"{action} needs 'profile' (an existing profile name or key)")
    if action == "set_default":
        if settings.get("default-profile") == base["description"]:
            return ok(f"'{base['description']}' is already the default profile.", default_profile=base["description"])
        settings.set("default-profile", base["description"])
        settings.save()
        return ok(f"New projects will use '{base['description']}'.", default_profile=base["description"])
    if action in ("edit", "delete") and not base.get("user"):
        raise ToolError(f"'{base['description']}' is a built-in profile; duplicate it to make a custom copy")
    if action == "delete":
        if base["description"].strip() in ((get_app().project.get("profile") or "").strip(),
                                           (settings.get("default-profile") or "").strip()):
            raise ToolError("you cannot delete the current project's profile or the default profile")
        try:
            os.unlink(base["path"])
        except OSError as exc:
            raise ToolError(f"could not delete {base['path']}: {exc}") from exc
        invalidate_catalog()
        return ok(f"Deleted the custom profile '{base['description']}'. No clip changed.",
                  deleted=base["description"], path=base["path"])

    src = base or {}
    w = width or src.get("width") or 0
    h = height or src.get("height") or 0
    if not (w and h):
        raise ToolError("give width and height (or a 'profile' to start from)")
    if w % 2 or h % 2:
        raise ToolError("width and height must be even")
    if fps:
        num, den = fps_fraction(fps)
    elif src:
        num, den = src["fps_num"], src["fps_den"]
    else:
        num, den = 30, 1
    par = {"num": 1, "den": 1}
    if (pixel_ratio or "").strip():
        m = re.match(r"^\s*(\d+)\s*[:/]\s*(\d+)\s*$", pixel_ratio)
        if not m or not int(m.group(1)) or not int(m.group(2)):
            raise ToolError("pixel_ratio must look like '1:1' or '10:11'")
        par = {"num": int(m.group(1)), "den": int(m.group(2))}
    elif src:
        par = dict(src["pixel_ratio"])
    desc = (name or "").strip()
    if action == "edit" and not desc:
        desc = src["description"]
    if action == "duplicate" and not desc:
        desc = f"{src['description']} (copy)"
    if not desc:
        fps_text = f"{num / float(den):.2f}".rstrip("0").rstrip(".")
        desc = f"Custom {w}x{h} {fps_text} fps"
    for p in catalog():
        if p["description"].strip() == desc and not (action == "edit" and p["path"] == src.get("path")):
            raise ToolError(f"a profile named '{desc}' already exists; pick a unique name")
    record = {"description": desc, "width": w, "height": h, "fps_num": num, "fps_den": den,
              "display_ratio": reduced_display_ratio(w, h, par["num"], par["den"]), "pixel_ratio": par,
              "interlaced": bool(interlaced), "spherical": bool(spherical)}
    if action == "edit":
        record["path"] = src["path"]
    else:
        path = os.path.join(info.USER_PROFILES_PATH, _key(record))
        suffix = 1
        while os.path.exists(path):
            path = os.path.join(info.USER_PROFILES_PATH, f"{_key(record)}-{suffix}")
            suffix += 1
        record["path"] = path
    try:
        path = write_user_profile(record, info.USER_PROFILES_PATH)
    except OSError as exc:
        raise ToolError(f"could not save the profile: {exc}") from exc
    record.update(key=os.path.basename(path), user=True)
    verb = {"create": "Created", "duplicate": "Created", "edit": "Updated"}[action]
    return ok(f"{verb} custom profile '{desc}' ({w}x{h}, {num / float(den):.3g} fps).",
              profile=describe_profile(record), path=path)
