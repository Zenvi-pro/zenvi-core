"""
 @file
 @brief Media health: what about a file will cause trouble in an edit, and what to do about it.

 Pure rules over the technical facts the index keeps beside every file (``facts.technical_of``): variable frame rate,
 interlacing, HDR, a frame rate that does not fit the project, a clip too small for the project, a portrait clip in a
 landscape project, and so on. Each issue says how serious it is and what to do. Nothing is changed here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

PROBLEM, WARN, INFO = "problem", "warn", "info"
_ORDER = {PROBLEM: 0, WARN: 1, INFO: 2}
LONG_SECONDS = 30 * 60
STANDARD_AUDIO_RATES = (44100, 48000)
HEAVY_CODECS = ("hevc", "h265", "av1", "vp9")


def _issue(code: str, severity: str, note: str, fix: str) -> Dict[str, str]:
    return {"code": code, "severity": severity, "note": note, "fix": fix}


def _near(a: float, b: float, tol: float = 0.02) -> bool:
    return b > 0 and abs(a - b) / b <= tol


def fps_fit(clip_fps: float, project_fps: float) -> Optional[Dict[str, str]]:
    """How a clip's frame rate sits in a project of another rate; None when they match."""
    if clip_fps <= 0 or project_fps <= 0 or _near(clip_fps, project_fps, 0.01):
        return None
    ratio = clip_fps / project_fps
    if ratio >= 1 and abs(ratio - round(ratio)) <= 0.03:
        return _issue("fps_mismatch", INFO, f"{clip_fps:g} fps in a {project_fps:g} fps project: every {round(ratio)}th frame is used",
                      "fine for most cuts; for the smoothest slow motion keep the clip's own frame rate")
    if ratio < 1 and abs(1 / ratio - round(1 / ratio)) <= 0.03:
        return _issue("fps_mismatch", INFO, f"{clip_fps:g} fps in a {project_fps:g} fps project: each frame is shown {round(1 / ratio)} times",
                      "fine for cutaways; it will look steppy if it moves a lot")
    return _issue("fps_mismatch", WARN, f"{clip_fps:g} fps in a {project_fps:g} fps project does not divide evenly, so motion will judder",
                  f"set the project to {clip_fps:g} fps, or convert the clip to {project_fps:g} fps first")


def file_issues(tech: Optional[Dict[str, Any]], project: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """The issues of one file, worst first. *tech* is ``facts.technical_of``; *project* is ``{"width", "height", "fps"}`` or None."""
    tech = tech or {}
    video, audio = tech.get("video") or {}, tech.get("audio") or {}
    out: List[Dict[str, str]] = []
    duration = float(tech.get("duration") or 0.0)
    if video:
        fps = float(video.get("fps") or 0.0)
        if video.get("vfr"):
            out.append(_issue("variable_frame_rate", WARN,
                              f"the frame rate varies (average {fps:g} fps, nominal {float(video.get('nominal_fps') or 0.0):g}): typical of phone recordings",
                              "convert it to a constant frame rate before editing, or audio and picture can drift apart and cuts land on the wrong frame"))
        if video.get("interlaced"):
            out.append(_issue("interlaced", WARN, "the picture is interlaced, so moving edges show combing", "deinterlace it (a deinterlace effect or a re-encode)"))
        if video.get("hdr"):
            out.append(_issue("hdr", WARN, f"HDR footage ({video.get('color_transfer')}): it looks flat and washed out in an ordinary (SDR) project",
                              "tone-map it to SDR before grading, then match it to the other clips"))
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if width and height:
            aspect = width / height
            if aspect > 2.4 or aspect < 0.5:
                out.append(_issue("odd_aspect", INFO, f"an unusual picture shape ({width}x{height})", "reframe or letterbox it deliberately"))
        if str(video.get("codec") or "").lower() in HEAVY_CODECS and (int(video.get("bit_depth") or 8) >= 10 or width >= 3840):
            out.append(_issue("heavy_codec", INFO, f"{video.get('codec')} {video.get('bit_depth') or 8}-bit {width}x{height} is heavy to play back smoothly",
                              "use proxy or optimised media while editing"))
        if project:
            pfps = float(project.get("fps") or 0.0)
            fit = fps_fit(fps, pfps)
            if fit:
                out.append(fit)
            pw, ph = int(project.get("width") or 0), int(project.get("height") or 0)
            if pw and ph and width and height:
                if height < 0.5 * ph and height < 720:
                    out.append(_issue("low_resolution", WARN, f"{width}x{height} is small for a {pw}x{ph} project, so it will look soft",
                                      "use it small, or as a short cutaway, or upscale it"))
                clip_portrait, project_portrait = height > width, ph > pw
                if clip_portrait != project_portrait and width != height and pw != ph:
                    shape = "portrait" if clip_portrait else "landscape"
                    out.append(_issue("orientation_mismatch", INFO, f"a {shape} clip in a {'portrait' if project_portrait else 'landscape'} project",
                                      "it will be letterboxed unless you reframe it (scale to fill, or use subject-aware framing)"))
        if not audio and tech.get("video"):
            out.append(_issue("no_audio", INFO, "no audio track", "fine for b-roll; it adds no sound"))
    if audio:
        rate = int(audio.get("sample_rate") or 0)
        if rate and rate not in STANDARD_AUDIO_RATES:
            out.append(_issue("audio_sample_rate", INFO, f"audio at {rate} Hz", "it is resampled on export; nothing to do unless it sounds off"))
    if duration > LONG_SECONDS:
        out.append(_issue("long_file", INFO, f"{duration / 60:.0f} minutes long", "description needs your approval (index_long_file_tool); cut it down first if you only need part"))
    return sorted(out, key=lambda i: _ORDER[i["severity"]])


def project_summary(rows: Sequence[Dict[str, Any]], project: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the files add up to: mixed frame rates, how many files have problems, the most common issues.

    *rows*: ``{"file_id", "name", "fps", "orientation", "issues"}``.
    """
    rates: Dict[float, List[str]] = {}
    for r in rows:
        fps = float(r.get("fps") or 0.0)
        if fps > 0:
            rates.setdefault(round(fps, 2), []).append(str(r.get("name") or r.get("file_id")))
    counts: Dict[str, int] = {}
    for r in rows:
        for i in r.get("issues") or []:
            counts[i["code"]] = counts.get(i["code"], 0) + 1
    summary: Dict[str, Any] = {
        "files": len(rows),
        "with_problems": sum(1 for r in rows if any(i["severity"] in (PROBLEM, WARN) for i in r.get("issues") or [])),
        "common_issues": [{"code": c, "files": n} for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        "frame_rates": {f"{fps:g}": names[:6] for fps, names in sorted(rates.items())},
    }
    if len(rates) > 1:
        pfps = float((project or {}).get("fps") or 0.0)
        summary["mixed_frame_rates"] = ("the files use " + ", ".join(f"{fps:g}" for fps in sorted(rates)) + " fps"
                                        + (f" in a {pfps:g} fps project" if pfps else "")
                                        + ": pick one rate for the project and convert the odd ones out, or accept judder on them")
    return summary
