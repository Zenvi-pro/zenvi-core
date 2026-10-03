"""Rendering: export presets, export video / audio / GIF / image sequence, export settings, frames, files.

Workstream: project-export. export_video_tool resolves a target the way the
Export dialog's Simple tab does (preset XML -> format, codecs, Low/Med/High
bitrates, the allowed video profiles; All Formats bitrates from the shared
bits-per-pixel rule), applies Advanced-tab overrides, then renders through the
editor's headless export path (``windows.export.export_video_headless``) with an
explicit frame range. It never opens a dialog.

The encode runs on its own QThread (``run_on_qthread``), so the editor stays
responsive while it renders; ``export_video_headless`` builds its Export dialog
object on the GUI thread and touches no widget after that (off-thread widget
access is what crashed macOS before PR #151). Everything else (preset parsing,
path and codec checks, verification) happens on the calling worker thread.
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
import uuid
from typing import Optional

from classes.editor_tools._base import (
    ToolError, array, boolean, enum, get_app, integer, number, obj, ok, on_main, playhead_seconds, string, th,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.project_export import CHANNEL_LAYOUTS, normalize_path, project_path, window
from classes.qt_main_thread import is_gui_thread

_TYPE_LABELS = {"video_audio": "Video & Audio", "video_only": "Video Only", "audio_only": "Audio Only",
                "image_sequence": "Image Sequence"}
_TYPE_OF_LABEL = {v: k for k, v in _TYPE_LABELS.items()}

# Everyday names for export targets -> preset titles.
_PRESET_ALIASES = {
    "mp4": "MP4 (h.264)", "h264": "MP4 (h.264)", "h.264": "MP4 (h.264)", "x264": "MP4 (h.264)",
    "video": "MP4 (h.264)", "default": "MP4 (h.264)", "hevc": "MP4 (h.265)", "h265": "MP4 (h.265)",
    "h.265": "MP4 (h.265)", "gif": "GIF (animated)", "animated_gif": "GIF (animated)",
    "mp3": "MP3 (audio only)", "audio": "MP3 (audio only)", "audio_only": "MP3 (audio only)",
    "webm": "WEBM (vp9)", "vp9": "WEBM (vp9)", "prores": "MOV (ProRes 422)", "mov": "MOV (h.264)",
    "quicktime": "MOV (h.264)", "mkv": "MKV (h.264)", "avi": "AVI (h.264)", "ogg": "OGG (theora/vorbis)",
    "ogv": "OGG (theora/vorbis)", "av1": "MP4 (AV1 svt)", "lossless": "MP4 (h.264) lossless",
    "dvd": "DVD-NTSC", "youtube_4k": "YouTube (4K)", "youtube_2k": "YouTube (2K)",
    "youtube_8k": "YouTube (8K)", "twitter": "Twitter / X", "x": "Twitter / X",
    "instagram_reels": "Instagram Reels", "reels": "Instagram Reels",
}
_AUDIO_CONTAINERS = {"wav": "pcm_s16le", "flac": "flac", "mp3": "libmp3lame", "m4a": "aac", "ogg": "libvorbis",
                     "aac": "aac"}


def _tr(text: str) -> str:
    try:
        return get_app()._tr(text)
    except Exception:
        return text


def _norm(text) -> str:
    return re.sub(r"[\s\-/]+", "_", str(text or "").strip().lower()).strip("_")


def presets():
    from classes.export_presets import load_presets
    return load_presets()


def resolve_preset(name: str) -> dict:
    """A preset record for a title, alias or social preset name ('Instagram Reels', 'tiktok', 'gif')."""
    from classes.editor_tools.project_export_profiles import social_preset

    items = presets()
    if not items:
        raise ToolError("no export presets were found")
    text = str(name or "").strip()
    by_title = {}
    for p in items:
        by_title.setdefault(p["title"].lower(), p)
        if p.get("user"):
            by_title[p["title"].lower()] = p
    if text.lower() in by_title:
        return by_title[text.lower()]
    alias = _PRESET_ALIASES.get(_norm(text))
    if not alias:
        _key, spec = social_preset(text)
        alias = spec[3] if spec else None
    if alias and alias.lower() in by_title:
        return by_title[alias.lower()]
    tokens = [t for t in re.split(r"[\s,()]+", text.lower()) if t]
    hits = [p for p in items if tokens and all(t in p["title"].lower() for t in tokens)]
    if len({h["title"] for h in hits}) == 1:
        return hits[0]
    if hits:
        raise ToolError(f"'{text}' matches several presets: " + ", ".join(sorted({h['title'] for h in hits})))
    raise ToolError(f"no export preset matches '{text}'. Try 'MP4 (h.264)', 'YouTube', 'Instagram Reels', "
                    "'TikTok', 'YouTube Shorts', 'GIF', 'MP3', 'WEBM', 'ProRes' (list_export_presets_tool lists all)")


# Render threads that outlived their timeout, kept until they stop: a QThread
# destroyed while it still runs aborts the app.
_ABANDONED_JOBS: list = []


def run_on_qthread(func, timeout_seconds: float = 6 * 60 * 60):
    """Run *func()* on a fresh QThread and wait for it; returns its result.

    On a timeout the thread is asked to stop (a frame loop sees it through
    render_interrupted()) and given a moment to, so the caller does not start
    the next render over one that is still writing.

    Frame rendering (libopenshot readers, SVG/text through Qt) must not run on a
    plain ``threading.Thread`` such as the MCP worker: Qt's font cache mutex and
    the GIL can deadlock the app there. A QThread is safe; so is the GUI thread for
    short work. Falls back to a direct call without Qt (headless tests).
    """
    QThread = th().QThread
    if QThread is None:
        return func()

    class _Job(QThread):
        result = None
        error = None

        def run(self):
            try:
                self.result = func()
            except BaseException as exc:  # re-raised in the caller
                self.error = exc

    job = _Job()
    job.start()
    if not job.wait(int(timeout_seconds * 1000)):
        job.requestInterruption()
        if not job.wait(_INTERRUPT_GRACE_MS):
            _ABANDONED_JOBS.append(job)
            job.finished.connect(lambda: _ABANDONED_JOBS.remove(job) if job in _ABANDONED_JOBS else None)
        raise ToolError(f"the render did not finish within {int(timeout_seconds)} s")
    if job.error is not None:
        raise job.error
    return job.result


_INTERRUPT_GRACE_MS = 10000


def render_interrupted() -> bool:
    """True on a run_on_qthread thread whose caller gave up waiting."""
    QThread = th().QThread
    try:
        return QThread is not None and bool(QThread.currentThread().isInterruptionRequested())
    except Exception:
        return False


def _codec_ok(codec: str) -> Optional[bool]:
    """True/False from libopenshot, None when it cannot be asked (headless tests)."""
    if not codec:
        return True
    try:
        import openshot
        return bool(openshot.FFmpegWriter.IsValidCodec(codec))
    except Exception:
        return None


def _timeline_bounds() -> tuple:
    clips = get_app().project.get("clips") or []
    if not clips:
        return None
    start = min(float(c.get("position") or 0.0) for c in clips)
    end = max(float(c.get("position") or 0.0) + max(0.0, float(c.get("end") or 0.0) - float(c.get("start") or 0.0))
              for c in clips)
    return start, end


def _selection_bounds() -> tuple:
    from classes.editor_tools._base import selected_clip_ids

    ids = set(selected_clip_ids())
    clips = [c for c in (get_app().project.get("clips") or []) if c.get("id") in ids]
    if not clips:
        raise ToolError("no clips are selected in the timeline; select some or give start/end seconds")
    start = min(float(c.get("position") or 0.0) for c in clips)
    end = max(float(c.get("position") or 0.0) + max(0.0, float(c.get("end") or 0.0) - float(c.get("start") or 0.0))
              for c in clips)
    return start, end


def _pick_profile(preset: dict, project_record: dict) -> tuple:
    """The export profile the Simple tab would pick for this preset, plus a note."""
    from classes.editor_tools.project_export_profiles import catalog

    allowed = preset.get("profiles") or []
    if not allowed:
        return project_record, None
    profiles = {p["description"]: p for p in reversed(catalog())}
    options = [profiles[name] for name in allowed if name in profiles]
    if not options:
        return project_record, None
    if project_record.get("description") in allowed:
        return profiles.get(project_record["description"], project_record), None
    pw, ph = project_record["width"], project_record["height"]
    pfps = project_record["fps_num"] / float(project_record["fps_den"])
    paspect = pw / float(ph)

    def fps_gap(p):
        return abs(p["fps_num"] / float(p["fps_den"]) - pfps)

    for bucket in (
        [p for p in options if (p["width"], p["height"]) == (pw, ph)],
        [p for p in options if abs(p["width"] / float(p["height"]) - paspect) < 0.01],
        [p for p in options if (p["width"] > p["height"]) == (pw > ph) and (p["width"] == p["height"]) == (pw == ph)],
        options,
    ):
        if bucket:
            best = sorted(bucket, key=lambda p: (fps_gap(p), -p["width"] * p["height"]))[0]
            note = None
            if abs(best["width"] / float(best["height"]) - paspect) > 0.01:
                sizes = ", ".join(sorted({"%dx%d" % (o["width"], o["height"]) for o in options}))
                note = (f"'{preset['title']}' only renders {sizes}; exporting {best['width']}x{best['height']} "
                        f"from a {pw}x{ph} project re-frames every clip (bars on 'fit' clips, crops on 'fill' "
                        "clips). For a proper vertical/square edit switch the project with set_project_profile_tool "
                        "(reframe='fill') first, or pass profile= to keep the project size")
            return best, note
    return project_record, None


def _project_record() -> dict:
    from classes.editor_tools.project_export_profiles import catalog, match_profile
    from classes.project_profile import project_profile_values

    cur = project_profile_values(get_app().project)
    for p in catalog():
        if p["description"] == cur["description"] and (p["width"], p["height"]) == (cur["width"], cur["height"]):
            return p
    found = match_profile(cur["width"], cur["height"], cur["fps_num"] / float(cur["fps_den"]))
    if found:
        return found
    cur.setdefault("key", "")
    cur.setdefault("path", "")
    return cur


def _channels_arg(channels: str):
    from classes.editor_tools.project_export_profiles import _LAYOUT_BY_NAME
    layout = _LAYOUT_BY_NAME.get(str(channels or "").strip().lower())
    if not layout:
        raise ToolError("channels must be mono, stereo, surround, 5.1 or 7.1")
    code, count = CHANNEL_LAYOUTS[layout]
    return count, code


_RATE_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(kb/s|mb/s|crf|cqp|qp)\s*$", re.I)


def build_export_plan(preset="", quality="", export_type="auto", range_mode="", start=0.0, end=0.0,
                      output_path="", folder="", file_name="", profile="", width=0, height=0, fps=0.0,
                      container="", video_codec="", audio_codec="", video_bitrate="", audio_bitrate="",
                      sample_rate=0, channels="", interlaced=False, image_format="png") -> dict:
    """Everything a render needs, resolved and validated (no side effects)."""
    from classes import info
    from classes.editor_tools.project_export_profiles import resolve_profile_request
    from classes.export_presets import all_formats_bitrate, available_qualities, is_quality_mode_rate
    from classes.project_profile import aspect_text, fps_fraction

    app = get_app()
    ov = {k: v for k, v in dict(app.project.get("export_overrides") or {}).items() if v not in (None, "")}
    notes = []

    p = resolve_preset(preset or ov.get("preset") or "MP4 (h.264)")
    qualities = available_qualities(p) or ["High"]
    q = (quality or ov.get("quality") or "high").strip().capitalize()
    if q not in qualities:
        notes.append(f"'{p['title']}' has no {q} quality; using {qualities[-1]}")
        q = qualities[-1]

    etype = export_type if export_type and export_type != "auto" else None
    if not etype and ov.get("export_type") in _TYPE_OF_LABEL:
        etype = _TYPE_OF_LABEL[ov["export_type"]]
    etype = etype or _TYPE_OF_LABEL.get(p["export_to"], "video_audio")

    # Video profile: explicit > stored override > preset's allowed list (Simple tab)
    project_rec = _project_record()
    if (profile or "").strip():
        prof = resolve_profile_request(profile)
    else:
        prof, note = _pick_profile(p, project_rec)
        if note:
            notes.append(note)
    w = int(width or ov.get("width") or prof["width"])
    h = int(height or ov.get("height") or prof["height"])
    if fps:
        fps_num, fps_den = fps_fraction(fps)
    elif ov.get("fps_num") and ov.get("fps_den"):
        fps_num, fps_den = int(ov["fps_num"]), int(ov["fps_den"])
    else:
        fps_num, fps_den = prof["fps_num"], prof["fps_den"]
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        raise ToolError(f"export size {w}x{h} is invalid; width and height must be positive even numbers")
    fps_value = fps_num / float(fps_den)

    # Container and codecs
    vformat = (container or ov.get("vformat") or p["vformat"] or "mp4").lower().lstrip(".")
    vcodec = video_codec or ov.get("video_codec") or p["vcodec"]
    acodec = audio_codec or ov.get("audio_codec") or p["acodec"]
    if etype == "audio_only" and vformat in _AUDIO_CONTAINERS and not (audio_codec or ov.get("audio_codec")):
        if vformat != (p["vformat"] or "").lower():
            acodec = _AUDIO_CONTAINERS[vformat]
    if etype == "image_sequence":
        image_format = (image_format or ov.get("image_format") or "png").lower().lstrip(".")
        if image_format not in ("png", "jpg", "jpeg", "bmp", "tiff", "ppm"):
            raise ToolError("image_format must be png, jpg, bmp, tiff or ppm")
        vformat = image_format
        vcodec = "mjpeg" if image_format in ("jpg", "jpeg") else image_format
    if etype in ("video_audio", "video_only", "image_sequence") and not vcodec:
        raise ToolError(f"'{p['title']}' has no video codec; use export_type='audio_only' or another preset")
    if etype in ("video_audio", "audio_only") and not acodec:
        if etype == "audio_only":
            raise ToolError(f"'{p['title']}' has no audio codec; pick an audio preset such as 'MP3'")
        notes.append(f"'{p['title']}' has no audio; exporting video only")
        etype = "video_only"
    for codec, kind in ((vcodec, "video"), (acodec, "audio")):
        if kind == "video" and etype == "audio_only" or kind == "audio" and etype in ("video_only", "image_sequence"):
            continue
        if _codec_ok(codec) is False and not (kind == "audio" and codec in ("aac", "libfaac", "libvo_aacenc")):
            raise ToolError(f"this build of Zenvi cannot encode {kind} codec '{codec}'; pick another preset")

    # Bitrates
    vrate = (video_bitrate or ov.get("video_bitrate") or p["video_bitrate"].get(q) or "").strip()
    if not video_bitrate and not ov.get("video_bitrate") and p["category"] == "All Formats" \
            and vrate and not is_quality_mode_rate(vrate):
        vrate = all_formats_bitrate(w, h, fps_value, q) or vrate
    arate = (audio_bitrate or ov.get("audio_bitrate") or p["audio_bitrate"].get(q) or "192 kb/s").strip()
    for rate, label in ((vrate, "video_bitrate"), (arate, "audio_bitrate")):
        if rate and not _RATE_RE.match(rate):
            raise ToolError(f"{label} must look like '8 Mb/s', '192 kb/s' or '23 crf', got {rate!r}")
    sr = int(sample_rate or ov.get("sample_rate") or p["sample_rate"] or app.project.get("sample_rate") or 48000)
    if (channels or "").strip():
        ch, layout = _channels_arg(channels)
    elif ov.get("channels") and ov.get("channel_layout"):
        ch, layout = int(ov["channels"]), int(ov["channel_layout"])
    else:
        ch, layout = int(p["channels"] or app.project.get("channels") or 2), int(p["channel_layout"] or 3)
    if etype == "audio_only" and (not sr or not ch):
        sr, ch, layout = sr or 48000, ch or 2, layout or 3

    # Frame range, in export frames
    bounds = _timeline_bounds()
    if bounds is None:
        raise ToolError("the timeline is empty; add clips before exporting")
    rmode = range_mode or ""
    if not rmode:
        rmode = "custom" if (start or end or ov.get("start_frame") or ov.get("end_frame")) else "whole"
    if rmode == "whole":
        t0, t1 = bounds
    elif rmode == "selection":
        t0, t1 = _selection_bounds()
    else:
        pfps = project_rec["fps_num"] / float(project_rec["fps_den"])
        t0 = float(start) if start else ((int(ov["start_frame"]) - 1) / pfps if ov.get("start_frame") else 0.0)
        t1 = float(end) if end else ((int(ov["end_frame"])) / pfps if ov.get("end_frame") else bounds[1])
    if t1 <= t0:
        raise ToolError(f"the range ends ({t1:.3f}s) before it starts ({t0:.3f}s)")
    if t0 >= bounds[1]:
        raise ToolError(f"the range starts at {t0:.3f}s, after the last clip ends ({bounds[1]:.3f}s)")
    start_frame = int(round(t0 * fps_value)) + 1
    end_frame = max(start_frame + 1, int(round(t1 * fps_value)))

    # Output path (Export dialog rules: folder + file name + extension)
    ext = vformat
    if (output_path or ov.get("output_path") or "").strip():
        out = normalize_path(output_path or ov.get("output_path"))
        if os.path.isdir(out):
            folder, out = out, ""
    else:
        out = ""
    if not out:
        base_folder = normalize_path(folder or ov.get("folder") or "")
        if not base_folder:
            try:
                s = app.get_settings()
                base_folder = s.getDefaultPath(s.actionType.EXPORT)
            except Exception:
                base_folder = ""
            base_folder = base_folder or info.DOWNLOADS_PATH
        name = (file_name or ov.get("file_name") or "").strip()
        if not name:
            name = os.path.splitext(os.path.basename(project_path()))[0] if project_path() else "Untitled Project"
        out = os.path.join(base_folder, name)
    if etype == "image_sequence":
        root = os.path.splitext(out)[0] if os.path.splitext(out)[1].lower().lstrip(".") in (ext, "png", "jpg") else out
        if "%" not in os.path.basename(root):
            root += "-%05d"
        out = f"{root}.{ext}"
    else:
        current_ext = os.path.splitext(out)[1].lower().lstrip(".")
        if not current_ext or current_ext.isdigit():
            out = f"{out}.{ext}"
        elif current_ext != ext:
            if (container or "").strip():
                out = f"{os.path.splitext(out)[0]}.{ext}"
            else:
                if current_ext == "gif" and etype != "audio_only" and vcodec != "gif":
                    raise ToolError(f"a .gif file needs the GIF preset ('{p['title']}' encodes {vcodec}): "
                                    "pass preset='GIF', or another file extension")
                vformat = ext = current_ext
    aspect_mismatch = abs(w / float(h) - project_rec["width"] / float(project_rec["height"])) > 0.01
    if aspect_mismatch and not any("re-frames every clip" in n for n in notes):
        notes.append(f"export is {aspect_text(w, h)} but the project is "
                     f"{aspect_text(project_rec['width'], project_rec['height'])}: each clip is re-framed by its "
                     "scale mode (bars on 'fit' clips, crops on 'fill' clips)")
    if abs(fps_value - project_rec["fps_num"] / float(project_rec["fps_den"])) > 0.01:
        notes.append(f"export frame rate {fps_value:.3f} differs from the project's; keyframes are rescaled for the render")
    export_profile_path = prof.get("path") or None
    return {
        "preset": p["title"], "preset_category": p["category"], "quality": q, "export_type": etype,
        "path": out, "vformat": vformat, "vcodec": vcodec if etype != "audio_only" else "",
        "acodec": acodec if etype in ("video_audio", "audio_only") else "",
        "width": w, "height": h, "fps_num": fps_num, "fps_den": fps_den, "fps": round(fps_value, 3),
        "pixel_ratio": dict(prof.get("pixel_ratio") or {"num": 1, "den": 1}),
        "video_bitrate": vrate, "audio_bitrate": arate, "sample_rate": sr, "channels": ch, "channel_layout": layout,
        "interlaced": bool(interlaced), "start_seconds": round(t0, 3), "end_seconds": round(t1, 3),
        "start_frame": start_frame, "end_frame": end_frame, "range": rmode,
        "profile": prof.get("description") or "", "profile_path": export_profile_path, "notes": notes,
    } if etype != "audio_only" else {
        "preset": p["title"], "preset_category": p["category"], "quality": q, "export_type": etype,
        "path": out, "vformat": vformat, "vcodec": "", "acodec": acodec,
        "width": w, "height": h, "fps_num": fps_num, "fps_den": fps_den, "fps": round(fps_value, 3),
        "pixel_ratio": dict(prof.get("pixel_ratio") or {"num": 1, "den": 1}),
        "video_bitrate": "", "audio_bitrate": arate, "sample_rate": sr, "channels": ch, "channel_layout": layout,
        "interlaced": False, "start_seconds": round(t0, 3), "end_seconds": round(t1, 3),
        "start_frame": start_frame, "end_frame": end_frame, "range": rmode,
        "profile": prof.get("description") or "", "profile_path": None,
        "notes": [n for n in notes if "re-frame" not in n and "differs from the project" not in n],
    }


def _settings_for(plan: dict) -> tuple:
    video = {
        "vformat": plan["vformat"], "vcodec": plan["vcodec"],
        "fps": {"num": plan["fps_num"], "den": plan["fps_den"]},
        "width": plan["width"], "height": plan["height"], "pixel_ratio": plan["pixel_ratio"],
        "video_bitrate": plan["video_bitrate"] or "8 Mb/s", "start_frame": plan["start_frame"],
        "end_frame": plan["end_frame"], "interlace": plan["interlaced"], "topfirst": plan["interlaced"],
        "spherical": False,
    }
    audio = {"acodec": plan["acodec"], "sample_rate": plan["sample_rate"], "channels": plan["channels"],
             "channel_layout": plan["channel_layout"], "audio_bitrate": plan["audio_bitrate"]}
    if plan["export_type"] in ("video_only", "image_sequence"):
        audio.update(sample_rate=0, channels=0)
    return video, audio


def _summary_of(plan: dict) -> dict:
    keys = ("preset", "quality", "export_type", "path", "vformat", "vcodec", "acodec", "width", "height", "fps",
            "video_bitrate", "audio_bitrate", "sample_rate", "channels", "start_seconds", "end_seconds",
            "start_frame", "end_frame", "range", "profile")
    return {k: plan[k] for k in keys}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@editor_tool(
    "list_export_presets_tool",
    label="List export presets",
    schema=obj({
        "category": enum(["", "All Formats", "Web", "Device", "DVD", "Blu-Ray/AVCHD"],
                         "Only this Export dialog 'Profile' group.", ""),
        "query": string("Words in the preset title, e.g. 'youtube', 'h.264', 'audio'.", ""),
        "include_unavailable": boolean("Also list presets whose codec this build cannot encode.", False),
    }),
    read_only=True,
    covers=("export.presets",),
)
def list_export_presets(category="", query="", include_unavailable=False):
    """List export targets (the Export dialog's Profile / Target / Quality choices) with what they produce.

    Each preset has its title (pass it as export_video_tool's preset), category,
    export_to (Video & Audio / Video Only / Audio Only), container format,
    video/audio codecs and whether this build can encode them, the Low/Med/High
    video and audio bitrates, and the video profiles it allows ("any" when it
    follows the project). Social names like 'tiktok' or 'instagram_reel' also
    work as export_video_tool presets.
    """
    tokens = [t for t in re.split(r"[\s,()]+", (query or "").lower()) if t]
    rows, seen = [], set()
    for p in presets():
        if p["title"] in seen:
            continue
        if category and p["category"] != category:
            continue
        if tokens and not all(t in p["title"].lower() for t in tokens):
            continue
        v_ok = _codec_ok(p["vcodec"]) if p["export_to"] != "Audio Only" else True
        a_ok = _codec_ok(p["acodec"]) if p["export_to"] not in ("Video Only",) else True
        available = not (v_ok is False or (a_ok is False and p["acodec"] not in ("aac",)))
        if not available and not include_unavailable:
            continue
        seen.add(p["title"])
        rows.append({
            "title": p["title"], "category": p["category"], "export_to": p["export_to"],
            "format": p["vformat"], "video_codec": p["vcodec"], "audio_codec": p["acodec"],
            "available": available,
            "qualities": {q.lower(): {"video": p["video_bitrate"].get(q), "audio": p["audio_bitrate"].get(q)}
                          for q in ("Low", "Med", "High")
                          if p["video_bitrate"].get(q) or p["audio_bitrate"].get(q)},
            "profiles": p["profiles"] or "any",
            "user": bool(p.get("user")),
        })
    rows.sort(key=lambda r: (r["category"] != "Web", r["category"], r["title"]))
    return ok(f"{len(rows)} export preset(s).", presets=rows)


_EXPORT_SCHEMA = {
    "preset": string("Target: an Export dialog preset title or a common name -- 'MP4 (h.264)' (default), 'YouTube', "
                     "'YouTube (4K)', 'YouTube Shorts', 'Instagram Reels', 'Instagram', 'TikTok', 'Facebook', "
                     "'Twitter / X', 'LinkedIn', 'Vimeo', 'GIF', 'MP3', 'WEBM', 'ProRes', 'MOV', 'MKV', 'HEVC'. "
                     "See list_export_presets_tool.", ""),
    "quality": enum(["", "low", "med", "high"], "Preset quality (bitrate). Empty = stored setting or high.", ""),
    "export_type": enum(["auto", "video_audio", "video_only", "audio_only", "image_sequence"],
                        "auto = the preset's (GIF = video only, MP3 = audio only). audio_only with container "
                        "'wav'/'flac' writes uncompressed/lossless audio.", "auto"),
    "range": enum(["", "whole", "selection", "custom"],
                  "whole = first clip to last clip; selection = the selected clips' span; custom = start/end. "
                  "Empty = custom when start/end are given (or stored), else whole.", ""),
    "start": number("Range start in timeline seconds (custom range).", 0, minimum=0),
    "end": number("Range end in timeline seconds (custom range). 0 = end of the last clip.", 0, minimum=0),
    "output_path": string("Full output file path. Empty = folder + file_name. The extension follows the container.", ""),
    "folder": string("Output folder (default: the Export folder preference, usually Downloads).", ""),
    "file_name": string("Output file name without folder (default: the project name).", ""),
    "profile": string("Export video profile/size instead of the preset's (Advanced > Profile): a profile name, "
                      "social preset or 'WIDTHxHEIGHT'.", ""),
    "width": integer("Override export width in pixels (even). 0 = from profile.", 0, minimum=0, maximum=16384),
    "height": integer("Override export height in pixels (even). 0 = from profile.", 0, minimum=0, maximum=16384),
    "fps": number("Override export frame rate. 0 = from profile.", 0, minimum=0, maximum=240),
    "container": string("Override container/extension, e.g. 'mov', 'mkv', 'wav', 'flac'.", ""),
    "video_codec": string("Override video codec, e.g. 'libx264', 'libx265', 'prores_ks', 'libvpx-vp9'.", ""),
    "audio_codec": string("Override audio codec, e.g. 'aac', 'libmp3lame', 'pcm_s16le', 'flac'.", ""),
    "video_bitrate": string("Override video rate: '8 Mb/s', '500 kb/s' or a quality like '20 crf'.", ""),
    "audio_bitrate": string("Override audio bitrate, e.g. '192 kb/s'.", ""),
    "sample_rate": integer("Override audio sample rate (Hz), e.g. 44100, 48000. 0 = preset's.", 0, minimum=0,
                           maximum=192000),
    "channels": string("Override audio channels: mono, stereo, surround, 5.1, 7.1.", ""),
    "interlaced": boolean("Interlaced output (top field first).", False),
    "image_format": enum(["png", "jpg", "bmp", "tiff"], "Image type for export_type='image_sequence'.", "png"),
    "overwrite": boolean("Replace an existing output file.", False),
    "show_dialog": boolean("Instead of rendering, open the Export Video dialog for the user (returns at once).", False),
}


@editor_tool(
    "export_video_tool",
    label="Export video",
    schema=obj(_EXPORT_SCHEMA),
    background_safe=True,
    covers=("export.video", "export.audio_only", "export.image_sequence", "export.gif", "export.advanced"),
)
def export_video(preset="", quality="", export_type="auto", range="", start=0, end=0, output_path="",
                 folder="", file_name="", profile="", width=0, height=0, fps=0, container="", video_codec="",
                 audio_codec="", video_bitrate="", audio_bitrate="", sample_rate=0, channels="", interlaced=False,
                 image_format="png", overwrite=False, show_dialog=False):
    """Render the timeline to a file: video, audio only (MP3/WAV/FLAC), animated GIF or an image sequence.

    Picks the target like the Export dialog: preset (YouTube, Instagram Reels,
    TikTok, YouTube Shorts, MP4 (h.264), GIF, MP3...), quality low/med/high, the
    preset's video profile (closest to the project), and optional Advanced
    overrides (size, fps, codecs, bitrates, sample rate, channels, container).
    Range: the whole timeline (first to last clip), the selected clips, or
    start/end seconds. Never opens a dialog unless show_dialog=true; refuses to
    overwrite unless overwrite=true. Waits for the render (the editor stays
    usable meanwhile) and returns the file path, size, format and range. For social
    vertical formats, switch the project first with set_project_profile_tool
    (reframe='fill'), otherwise landscape footage renders with bars.
    Example: {"preset": "Instagram Reels", "start": 0, "end": 5}
    """
    if not _EXPORT_LOCK.acquire(blocking=False):
        running = dict(_RUNNING_EXPORT)
        raise ToolError(
            f"an export is already running (to {running.get('path', '?')}, started "
            f"{running.get('started', '?')}); this call did nothing. Wait for it to finish, then check the "
            "file -- a render keeps going even when a tool call times out")
    try:
        return _export_video_locked(preset, quality, export_type, range, start, end, output_path, folder,
                                    file_name, profile, width, height, fps, container, video_codec, audio_codec,
                                    video_bitrate, audio_bitrate, sample_rate, channels, interlaced,
                                    image_format, overwrite, show_dialog)
    finally:
        if not _RUNNING_EXPORT.get("draining"):
            _release_export_lock()


def _release_export_lock():
    _RUNNING_EXPORT.clear()
    _EXPORT_LOCK.release()


def _release_export_lock_when_stopped():
    """Hold the export lock until a timed-out render has really stopped encoding."""
    from windows.export import cancel_headless_exports
    while not cancel_headless_exports(wait_seconds=60):
        pass
    _release_export_lock()


# One render at a time: a render takes minutes, and a second call (an agent
# retrying after its own call timed out) would re-render over the same file.
_EXPORT_LOCK = threading.Lock()
_RUNNING_EXPORT: dict = {}


def _export_video_locked(preset, quality, export_type, range, start, end, output_path, folder, file_name,
                         profile, width, height, fps, container, video_codec, audio_codec, video_bitrate,
                         audio_bitrate, sample_rate, channels, interlaced, image_format, overwrite,
                         show_dialog):
    if show_dialog:
        def _open():
            from windows.export import Export
            dlg = Export()
            window()._agent_export_dialog = dlg
            dlg.show()
        on_main(_open)
        return ok("Opened the Export Video dialog for the user; nothing was rendered.", dialog_opened=True)
    plan = build_export_plan(preset, quality, export_type, range, start, end, output_path, folder, file_name,
                             profile, width, height, fps, container, video_codec, audio_codec,
                             video_bitrate, audio_bitrate, sample_rate, channels, interlaced, image_format)
    path = plan["path"]
    from classes.query import File
    if File.get(path=path):  # compares normalized paths (media_paths_equal)
        raise ToolError(f"{path} is one of the project's input files; choose a different name")
    if plan["export_type"] == "image_sequence":
        pattern = re.sub(r"%0?\d*d", "*", path)
        existing = glob.glob(pattern)
        if existing and not overwrite:
            raise ToolError(f"{len(existing)} frame file(s) matching {os.path.basename(pattern)} already exist; "
                            "pass overwrite=true or another file_name")
    elif os.path.exists(path) and not overwrite:
        raise ToolError(f"{path} already exists; pass overwrite=true to replace it")
    folder_path = os.path.dirname(path)
    try:
        os.makedirs(folder_path, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create {folder_path}: {exc}") from exc

    video, audio = _settings_for(plan)
    label = _tr(_TYPE_LABELS[plan["export_type"]])
    _RUNNING_EXPORT.update(path=path, started=time.strftime("%H:%M:%S"))
    fps_differs = any("rescaled" in n for n in plan["notes"])

    def _render():
        from windows.export import export_video_headless
        return export_video_headless(path, video, audio, label, video_bitrate_text=plan["video_bitrate"].lower(),
                                     profile_path_for_rescale=plan["profile_path"] if fps_differs else None)

    try:
        # Off the GUI thread, so the editor is not frozen for the whole render. A caller
        # already on it renders inline: a QThread would wait for a dialog only this thread can build.
        err = _render() if th().QThread is not None and is_gui_thread() else run_on_qthread(_render)
    except ToolError:
        # Timed out: stop the render, or it would write over the next export of this file.
        from windows.export import cancel_headless_exports
        if not cancel_headless_exports(wait_seconds=_INTERRUPT_GRACE_MS / 1000):
            # Still encoding: the lock stays held until it stops, so a retry cannot overlap it.
            _RUNNING_EXPORT["draining"] = True
            threading.Thread(target=_release_export_lock_when_stopped, name="zenvi-export-drain",
                             daemon=True).start()
        raise
    except Exception as exc:
        raise ToolError(f"export failed: {exc}") from exc
    if err:
        raise ToolError(f"export failed: {err}")
    if plan["export_type"] == "image_sequence":
        frames = sorted(glob.glob(re.sub(r"%0?\d*d", "*", path)))
        if not frames:
            raise ToolError(f"the render finished but no frame files match {path}")
        size = sum(os.path.getsize(f) for f in frames)
        extra = {"frame_files": len(frames), "first_frame": frames[0]}
    else:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ToolError(f"the render finished but {path} is missing or empty")
        size = os.path.getsize(path)
        extra = {}
    try:
        s = get_app().get_settings()
        s.setDefaultPath(s.actionType.EXPORT, path)
    except Exception:
        pass
    seconds = plan["end_seconds"] - plan["start_seconds"]
    what = {"audio_only": "audio", "image_sequence": "image sequence", "video_only": "video (no audio)"}.get(
        plan["export_type"], "video")
    summary = (f"Exported {seconds:.2f}s of {what} with '{plan['preset']}' ({plan['quality']}) to {path} "
               f"({size / 1048576.0:.1f} MB)")
    if plan["export_type"] != "audio_only":
        summary += f", {plan['width']}x{plan['height']} at {plan['fps']} fps"
    summary += "."
    if plan["notes"]:
        summary += " Note: " + "; ".join(plan["notes"]) + "."
    return ok(summary, size_bytes=size, duration_seconds=round(seconds, 3), notes=plan["notes"],
              **_summary_of(plan), **extra)


# Keys set_export_setting_tool accepts -> (storage key, parser description)
_SETTING_KEYS = {
    "preset": "export preset title or alias (e.g. 'YouTube', 'Instagram Reels', 'GIF', 'MP3')",
    "quality": "low | med | high",
    "export_type": "video_audio | video_only | audio_only | image_sequence",
    "width": "even integer pixels", "height": "even integer pixels",
    "fps": "frame rate number (e.g. 24, 29.97)", "fps_num": "integer", "fps_den": "integer",
    "start": "range start, timeline seconds", "end": "range end, timeline seconds",
    "start_frame": "range start frame (1-based, project fps)", "end_frame": "range end frame",
    "video_codec": "e.g. libx264", "audio_codec": "e.g. aac", "format": "container, e.g. mp4, mov, webm",
    "video_bitrate": "'8 Mb/s' | '23 crf'", "audio_bitrate": "'192 kb/s'",
    "sample_rate": "22050 | 44100 | 48000 | 96000 | 192000", "channels": "mono | stereo | surround | 5.1 | 7.1",
    "output_path": "full output file path", "folder": "output folder", "file_name": "output file name",
    "image_format": "png | jpg | bmp | tiff",
}
_KEY_ALIASES = {"vcodec": "video_codec", "acodec": "audio_codec", "vformat": "format", "container": "format",
                "path": "output_path", "type": "export_type", "target": "preset", "bitrate": "video_bitrate",
                "frame_rate": "fps", "framerate": "fps"}


def _parse_setting(key: str, value: str) -> dict:
    """{storage_key: value} for one set_export_setting_tool key; ToolError when invalid."""
    from classes.project_profile import fps_fraction

    v = str(value if value is not None else "").strip()
    if not v:
        raise ToolError(f"give a value for {key}")

    def as_int(text, even=False, lo=1):
        try:
            n = int(float(text))
        except ValueError:
            raise ToolError(f"{key} must be a whole number, got {text!r}") from None
        if n < lo or (even and n % 2):
            raise ToolError(f"{key} must be {'an even number' if even else 'a number'} >= {lo}, got {text!r}")
        return n

    fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    pfps = float(fps.get("num") or 30) / float(fps.get("den") or 1)
    if key == "preset":
        return {"preset": resolve_preset(v)["title"]}
    if key == "quality":
        if v.lower() not in ("low", "med", "high"):
            raise ToolError("quality must be low, med or high")
        return {"quality": v.lower()}
    if key == "export_type":
        k = _norm(v)
        label = _TYPE_LABELS.get(k) or (v if v in _TYPE_OF_LABEL else None)
        if not label:
            raise ToolError("export_type must be video_audio, video_only, audio_only or image_sequence")
        return {"export_type": label}
    if key in ("width", "height"):
        return {key: as_int(v, even=True, lo=2)}
    if key == "fps":
        try:
            num, den = fps_fraction(v)
        except (ValueError, ZeroDivisionError):
            raise ToolError(f"fps must be a positive number, got {v!r}") from None
        return {"fps_num": num, "fps_den": den}
    if key in ("fps_num", "fps_den", "start_frame", "end_frame"):
        return {key: as_int(v)}
    if key in ("start", "end"):
        try:
            sec = float(v.rstrip("s"))
        except ValueError:
            raise ToolError(f"{key} must be seconds, got {v!r}") from None
        if sec < 0:
            raise ToolError(f"{key} must be >= 0")
        frame = int(round(sec * pfps)) + (1 if key == "start" else 0)
        return {"start_frame" if key == "start" else "end_frame": max(1, frame)}
    if key in ("video_codec", "audio_codec"):
        if _codec_ok(v) is False:
            raise ToolError(f"this build cannot encode '{v}'")
        return {key: v}
    if key == "format":
        return {"vformat": v.lower().lstrip(".")}
    if key in ("video_bitrate", "audio_bitrate"):
        if not _RATE_RE.match(v):
            raise ToolError(f"{key} must look like '8 Mb/s', '192 kb/s' or '23 crf'")
        return {key: v}
    if key == "sample_rate":
        n = as_int(v)
        if n not in (22050, 44100, 48000, 96000, 192000):
            raise ToolError("sample_rate must be 22050, 44100, 48000, 96000 or 192000")
        return {"sample_rate": n}
    if key == "channels":
        count, layout = _channels_arg(v)
        return {"channels": count, "channel_layout": layout}
    if key in ("output_path", "folder"):
        return {key: normalize_path(v)}
    if key == "file_name":
        if os.sep in v:
            raise ToolError("file_name is only a name; use output_path or folder for folders")
        return {"file_name": v}
    if key == "image_format":
        if v.lower() not in ("png", "jpg", "bmp", "tiff"):
            raise ToolError("image_format must be png, jpg, bmp or tiff")
        return {"image_format": v.lower()}
    raise ToolError(f"unknown export setting {key!r}")


@editor_tool(
    "get_export_settings_tool",
    label="Read export settings",
    schema=obj({}),
    read_only=True,
    covers=("export.settings",),
)
def get_export_settings():
    """Show what export_video_tool would render right now with no arguments, and the stored overrides.

    Reports preset, quality, type, output path, size, fps, codecs, bitrates,
    audio format and frame range (seconds and frames), the overrides saved with
    set_export_setting_tool, and the keys that tool accepts.
    """
    overrides = {k: v for k, v in dict(get_app().project.get("export_overrides") or {}).items() if v is not None}
    try:
        plan = build_export_plan()
        effective = _summary_of(plan)
        notes = plan["notes"]
    except ToolError as exc:
        effective, notes = None, [str(exc)]
    summary = (f"Next export: '{effective['preset']}' {effective['quality']} {effective['export_type']} "
               f"{effective['width']}x{effective['height']} {effective['fps']} fps, "
               f"{effective['start_seconds']}-{effective['end_seconds']}s -> {effective['path']}."
               if effective else "Nothing to export yet: " + "; ".join(notes))
    return ok(summary, effective=effective, overrides=overrides, notes=notes, valid_keys=_SETTING_KEYS)


@editor_tool(
    "set_export_setting_tool",
    label="Update export setting",
    schema=obj({
        "key": string("Setting to store: preset, quality, export_type, width, height, fps, start, end, video_codec, "
                      "audio_codec, format, video_bitrate, audio_bitrate, sample_rate, channels, output_path, folder, "
                      "file_name, image_format (also start_frame, end_frame, fps_num, fps_den).", ""),
        "value": string("New value (see get_export_settings_tool valid_keys for each key's format).", ""),
        "clear": boolean("Remove the stored override for key (or every override when key is empty).", False),
    }),
    covers=("export.settings",),
)
def set_export_setting(key="", value="", clear=False):
    """Store an export default in the project (used by export_video_tool when that argument is not given).

    Values are validated (unknown keys are refused with the list of valid ones)
    and saved with the project, outside undo history -- like the Export
    dialog remembering its fields. Prefer passing arguments to export_video_tool
    directly; use this when the user wants a setting remembered.
    Example: {"key": "preset", "value": "YouTube"}; {"key": "start", "clear": true}
    """
    app = get_app()
    k = _KEY_ALIASES.get(_norm(key), _norm(key))
    current = dict(app.project.get("export_overrides") or {})
    if clear:
        if not k:
            targets = [name for name, v in current.items() if v is not None]
        else:
            if k not in _SETTING_KEYS:
                raise ToolError(f"unknown export setting {key!r}; valid keys: {', '.join(_SETTING_KEYS)}")
            storage = {"start": ["start_frame"], "end": ["end_frame"], "fps": ["fps_num", "fps_den"],
                       "format": ["vformat"], "channels": ["channels", "channel_layout"]}.get(k, [k])
            targets = [name for name in storage if current.get(name) is not None]
        if not targets:
            return ok("No stored override to clear.", cleared=[], overrides={n: v for n, v in current.items()
                                                                          if v is not None})
        with th()._ignore_history(app):
            app.updates.update(["export_overrides"], {name: None for name in targets})
        left = {n: v for n, v in dict(app.project.get("export_overrides") or {}).items() if v is not None}
        return ok(f"Cleared {', '.join(targets)}.", cleared=targets, overrides=left)
    if not k:
        raise ToolError(f"give key and value; valid keys: {', '.join(_SETTING_KEYS)}")
    if k not in _SETTING_KEYS:
        raise ToolError(f"unknown export setting {key!r}; valid keys: {', '.join(_SETTING_KEYS)}")
    values = _parse_setting(k, value)
    if all(current.get(name) == v for name, v in values.items()):
        return ok(f"{k} is already {value}.", changed=False, overrides={n: v for n, v in current.items()
                                                                        if v is not None})
    with th()._ignore_history(app):
        app.updates.update(["export_overrides"], values)
    left = {n: v for n, v in dict(app.project.get("export_overrides") or {}).items() if v is not None}
    return ok(f"Stored export {k} = {value}.", changed=True, stored=values, overrides=left)


@editor_tool(
    "save_frame_image_tool",
    label="Save frame as image",
    schema=obj({
        "time": number("Timeline seconds of the frame. -1 = the playhead.", -1, minimum=-1),
        "file_path": string("Output image path (.png or .jpg). Empty = the export folder, 'Frame-<n>.png'.", ""),
        "overwrite": boolean("Replace an existing file.", False),
    }),
    background_safe=True,
    covers=("export.frame",),
)
def save_frame_image(time=-1, file_path="", overwrite=False):
    """Save one frame of the timeline as a full-resolution image (File > Save Current Frame).

    For a thumbnail or still ("save this frame", "grab a still at 12 s").
    Renders at the project's full size, not the preview size. Changes nothing
    in the project.
    """
    from classes import info
    from classes.editor_tools._base import frame_to_seconds, seconds_to_frame

    bounds = _timeline_bounds()
    if bounds is None:
        raise ToolError("the timeline is empty")
    seconds = playhead_seconds() if time is None or time < 0 else float(time)
    if seconds > bounds[1]:
        raise ToolError(f"{seconds:.3f}s is after the end of the last clip ({bounds[1]:.3f}s)")
    frame = seconds_to_frame(seconds)
    path = normalize_path(file_path)
    if not path:
        try:
            s = get_app().get_settings()
            folder = s.getDefaultPath(s.actionType.EXPORT)
        except Exception:
            folder = ""
        path = os.path.join(folder or info.DOWNLOADS_PATH, "Frame-%05d.png" % frame)
    if os.path.isdir(path):
        path = os.path.join(path, "Frame-%05d.png" % frame)
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg"):
        path += ".png"
        ext = ".png"
    if os.path.exists(path) and not overwrite:
        raise ToolError(f"{path} already exists; pass overwrite=true")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = "JPG" if ext in (".jpg", ".jpeg") else "PNG"
    from classes.editor_tools.effects_color_analysis import render_frame

    def _render():
        # A private full-size timeline on a QThread: the GUI thread (and the
        # preview timeline File > Save Current Frame borrows) stay untouched.
        rendered = render_frame(None, frame_to_seconds(frame))
        try:
            rendered.frame.Save(staged, 1.0, fmt)
        finally:
            rendered.close()
        if not os.path.isfile(staged):
            return False
        os.replace(staged, path)
        return True

    # Staged, so a failed render never passes for an older file at *path*.
    staged = os.path.join(os.path.dirname(path), ".%s.partial" % os.path.basename(path))
    try:
        saved = run_on_qthread(_render, timeout_seconds=120)
    finally:
        try:
            os.remove(staged)  # a timed-out render thread may still own it
        except OSError:
            pass
    if not saved or not os.path.isfile(path):
        raise ToolError(f"the frame could not be saved to {path}")
    proj = get_app().project
    return ok(f"Saved frame {frame} ({frame_to_seconds(frame):.3f}s) as {path}.", path=path, frame=frame,
              time=round(frame_to_seconds(frame), 3), width=proj.get("width"), height=proj.get("height"),
              size_bytes=os.path.getsize(path))


@editor_tool(
    "export_files_to_folder_tool",
    label="Export project files",
    schema=obj({
        "file_ids": array({"type": "string"}, "Project file ids (media_bin_file_id from list_files_tool)."),
        "folder": string("Destination folder (created if missing). Empty = the export folder preference.", ""),
    }, required=["file_ids"]),
    background_safe=True,
    covers=("export.selected_files",),
)
def export_files_to_folder(file_ids, folder=""):
    """Export chosen project files to a folder (Project Files > Export Selected Clips).

    Plain files are copied; sub-clips (files with in/out points, e.g. from
    split_file_add_clip_tool) and image sequences are rendered as H.264/AAC MP4
    of just that part, at the file's own size and frame rate (on a QThread,
    never the GUI or a plain worker thread). Existing
    destination files are skipped, like the dialog. Changes nothing in the project.
    """
    from classes import info
    from classes.query import File

    objs = []
    for fid in file_ids or []:
        f = File.get(id=str(fid).strip())
        if not f:
            raise ToolError(f"no project file with id={fid!r}")
        objs.append(f)
    if not objs:
        raise ToolError("give at least one file id")
    dest = normalize_path(folder)
    if not dest:
        try:
            s = get_app().get_settings()
            dest = s.getDefaultPath(s.actionType.EXPORT)
        except Exception:
            dest = ""
        dest = dest or info.DOWNLOADS_PATH
    os.makedirs(dest, exist_ok=True)

    import openshot
    from windows import export_clips as ec

    results = []
    for f in objs:
        entry = {"file_id": f.id, "source": f.data.get("path")}
        try:
            if ec.isClip(f) or ec.isImageSequence(f):
                clip = f if ec.isClip(f) else ec.imageSequenceAsClip(f)
                name = ec.nameOfExport(f) if ec.isClip(f) else ec.nameOfImageSequenceExport(f)
                out = os.path.join(dest, name)
                entry["path"] = out
                if os.path.exists(out):
                    entry["status"] = "skipped (exists)"
                else:
                    def _render(clip=clip, out=out):
                        start_frame, end_frame = ec.startAndEndFrames(clip)
                        # Written under a staged name (same extension, FFmpeg
                        # picks the container from it) and moved into place
                        # only once complete: Close() can still write after a
                        # failure, and a file at *out* is skipped next time.
                        stem, ext = os.path.splitext(out)
                        # Unique, so two exports of the same file never share it.
                        staged = "%s.partial-%s%s" % (stem, uuid.uuid4().hex[:8], ext)
                        writer = openshot.FFmpegWriter(staged)
                        reader = None
                        try:
                            try:
                                ec.setupWriter(clip, writer)
                                reader = openshot.Clip(clip.data.get("path"))
                                reader.Open()
                                for frame in range(start_frame, end_frame + 1):
                                    if render_interrupted():
                                        raise ToolError("the render was stopped")
                                    writer.WriteFrame(reader.GetFrame(frame))
                            finally:
                                if reader:
                                    reader.Close()
                                writer.Close()
                            os.replace(staged, out)
                        finally:
                            if os.path.exists(staged):
                                os.remove(staged)

                    run_on_qthread(_render)
                    entry["status"] = "rendered"
            else:
                out = os.path.join(dest, ec.nameOfExport(f))
                entry["path"] = out
                existed = os.path.exists(out)
                ec.copyFileToFolder(f, dest)
                entry["status"] = "skipped (exists)" if existed else "copied"
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
        results.append(entry)
    errors = [r for r in results if r["status"] == "error"]
    done = [r for r in results if r["status"] in ("copied", "rendered")]
    if errors and not done:
        raise ToolError("could not export: " + "; ".join(f"{r['source']}: {r['error']}" for r in errors))
    return ok(f"Exported {len(done)} file(s) to {dest}"
              + (f"; {len(results) - len(done) - len(errors)} already there" if len(results) - len(done) - len(errors) else "")
              + (f"; {len(errors)} failed" if errors else "") + ".", folder=dest, files=results)
