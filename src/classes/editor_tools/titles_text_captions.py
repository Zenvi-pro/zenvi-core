"""Captions: animated HyperFrames caption styles, rendered on this computer (titles-text workstream).

One captioning path for the Captions dock and the assistant. A caption track is
a transparent video: the clip's words come from Zenvi's on-device transcript,
go into a HyperFrames caption component (``classes.hyperframes_captions``), are
rendered locally and placed on a track above the clip. For captions behind the
speaker, a cut-out of the subject is placed on the track above the captions.

Threading: transcript, render and matting run on the calling worker thread;
only the import, the metadata stamp and the timeline edits hop to the GUI thread.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from typing import List, Optional

from classes import hyperframes_captions as cap
from classes import hyperframes_local as hf
from classes.editor_tools._base import (
    ToolError,
    boolean,
    clip_extent,
    enum,
    obj,
    ok,
    on_main,
    resolve_clip,
    string,
    th,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

SOURCE = "hyperframes_captions"
RENDER_TIMEOUT = 1800
MATTE_TIMEOUT = 3600
BEHIND_SUBJECT_CENTRE = 0.42          # share of the frame height the captions centre on behind a speaker
SUBJECT_COVERAGE = (0.03, 0.9)        # share of the frame a cut-out covers when there is a subject to cut out
# What makes the cut-out sit exactly over the footage it was cut from.
_GEOMETRY = ("scale", "gravity", "scale_x", "scale_y", "location_x", "location_y", "rotation",
             "shear_x", "shear_y", "origin_x", "origin_y")
_CLIP_ID_RE = re.compile(r"timeline_clip_id=([A-Za-z0-9]+)")
_BBOX_RE = re.compile(r"\by1:(\d+) y2:(\d+)")
_ALPHA_RE = re.compile(r"YAVG=([0-9.]+)")
_SILENCE_RE = re.compile(r"silence_start=([0-9.]+).*?silence_end=([0-9.]+)", re.DOTALL)
# What counts as a pause between words. Written to a file: a long clip's log would be cut short.
PAUSE = "silencedetect=noise=-32dB:d=0.25,ametadata=mode=print:file=silences.txt"

# A clip's project folder is shared by the dock's checks and a running caption job.
_probe_lock, _speech_lock = threading.Lock(), threading.Lock()
on_progress = None       # set by the Captions dock: called with one line per stage, from the worker thread


def _stage(message: str):
    log.info("Captions: %s", message)
    if on_progress:
        try:
            on_progress(message)
        except Exception:
            log.debug("Captions: progress listener failed", exc_info=True)


def _project(clip_id) -> str:
    return "captions-" + re.sub(r"[^a-z0-9]", "", str(clip_id).lower())[:40]


def _media(clip) -> str:
    from classes.query import File

    f = File.get(id=str(clip.data.get("file_id") or ""))
    path = f.absolute_path() if f else ""
    return path if path and os.path.isfile(path) else ""


def _run(project: str, program: str, args: list, timeout: int, inputs=()) -> str:
    try:
        code, out = hf.run(project, program, args, timeout=timeout, inputs=inputs)
    except hf.HyperframesError as exc:
        raise ToolError(str(exc))
    if code != 0:
        raise ToolError("%s %s failed (exit code %d):\n%s" % (program, args[0], code, out[-1500:]))
    return out


def _indexed_words(clip) -> List[dict]:
    """Words from the transcript stored when the clip's file was indexed (phrase timing)."""
    position, source_start = float(clip.data.get("position") or 0.0), float(clip.data.get("start") or 0.0)
    cues = []
    for cue in th()._clip_transcript_cues(str(clip.id)) or []:
        if cue.get("source_start") is not None:          # source seconds -> timeline seconds
            start = float(cue["source_start"]) - source_start
            end = float(cue.get("source_end") or cue["source_start"]) - source_start
        else:                                            # already relative to the clip's start
            start, end = float(cue.get("start") or 0.0), float(cue.get("end") or 0.0)
        cues.append({"text": cue.get("text"), "start": position + start, "end": position + end})
    return [{"text": w["text"], "timelineStartSec": w["start"], "timelineEndSec": w["end"]}
            for w in cap.cue_words(cues)]


def _speech_words(clip) -> List[dict]:
    """The clip's words timed on this computer; [] when no recogniser can be had or it fails.

    Whistle (small, downloaded on first use) unless the larger Parakeet was installed,
    which ``hyperframes transcribe`` runs. Where Whistle has no engine (Intel Macs),
    Parakeet is the recogniser and is downloaded on first use instead.
    """
    media = _media(clip)
    if not media:
        return []
    installed = bool(hf.status().get("parakeet"))
    engine = "parakeet" if installed or not hf.speech_supported() else "whistle"
    position, _end, duration = clip_extent(clip.data)
    start = float(clip.data.get("start") or 0.0)
    project = _project(clip.id)
    try:
        stat = os.stat(media)                           # replaced or re-exported media is read again
        kept = os.path.join(hf.project_dir(project), "words-%s-%d-%d-%d-%d.json" % (
            engine, start * 1000, duration * 1000, stat.st_size, stat.st_mtime_ns))
        with _speech_lock:
            fresh = os.path.join(hf.project_dir(project), "transcript.json")
            if not os.path.isfile(kept):
                _run(project, "ffmpeg", ["-y", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", media,
                                         "-vn", "-ac", "1", "-ar", "16000", "speech.wav"], RENDER_TIMEOUT,
                     inputs=[media])
                if engine == "parakeet":
                    if not installed:
                        # Too large to fetch unasked: the same confirmation the setup tool shows.
                        from classes.editor_tools import ai_generation_hyperframes as setup

                        wanted = [s for s in hf.install_plan(hf.status(), parakeet=True) if s["id"] == "parakeet"]
                        if not setup._ask_install_consent(wanted):
                            raise ToolError("the speech recogniser download was declined")
                        _stage("Downloading the speech recogniser (about 900 MB, one time)...")
                        hf.install_parakeet()
                    _run(project, "hyperframes", ["transcribe", "speech.wav", "--engine", "parakeet", "--json"],
                         RENDER_TIMEOUT)
                else:
                    if not hf.speech_ready():
                        _stage("Downloading the speech recogniser (18 MB, one time)...")
                        hf.install_speech()
                    with open(fresh, "w", encoding="utf-8") as fh:
                        json.dump(hf.transcribe(os.path.join(hf.project_dir(project), "speech.wav")), fh)
            with open(kept if os.path.isfile(kept) else fresh, encoding="utf-8") as fh:
                entries = json.load(fh)
            spoken = [{"text": w["text"], "start": float(w["start"]), "end": float(w["end"])}
                      for w in entries if str(w.get("text") or "").strip()]
            words = [{"text": w["text"], "timelineStartSec": position + w["start"],
                      "timelineEndSec": position + w["end"]}
                     for w in cap.trim_to_speech(spoken, _silences(project, media, start, duration))]
            if not os.path.isfile(kept):                # kept only once it is known to be usable
                os.replace(fresh, kept)
        return words
    except (ToolError, hf.HyperframesError, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("Captions: the %s recogniser could not time the words: %s", engine, exc)
        return []


def _silences(project: str, media: str, start: float, duration: float) -> List[tuple]:
    """The pauses in a stretch of the media's sound, in seconds from *start*; [] if they cannot be measured."""
    try:
        _run(project, "ffmpeg", ["-y", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", media, "-vn",
                                 "-af", PAUSE, "-f", "null", "-"], RENDER_TIMEOUT, inputs=[media])
        with open(os.path.join(hf.project_dir(project), "silences.txt"), encoding="utf-8", errors="replace") as fh:
            return [(float(a), float(b)) for a, b in _SILENCE_RE.findall(fh.read())]
    except (ToolError, hf.HyperframesError, OSError):
        return []


def transcript_words(clip) -> List[dict]:
    """The clip's spoken words in timeline seconds.

    Word-timed from Zenvi's on-device recogniser, else from the recogniser
    HyperFrames setup installs; when neither works, phrase-timed from the transcript stored when the file
    was indexed (its words are spread evenly, so they only roughly follow speech).
    """
    from classes.agent_tools.receipt import parse_receipt
    from classes.agent_tools.transcript import get_transcript

    try:
        receipt = parse_receipt(get_transcript(clipId=str(clip.id), includeWords=True)) or {}
    except Exception as exc:
        receipt = {"status": "error", "summary": str(exc)}
    words = []
    for entry in (receipt.get("data") or {}).get("clips") or []:
        words.extend(entry.get("words") or [])
    words = words or _speech_words(clip)
    if words:
        return words
    indexed = _indexed_words(clip)
    if not indexed and receipt.get("status") in ("error", "refused"):
        raise ToolError(str(receipt.get("summary") or "the transcript is not available").removeprefix("Error: "))
    return indexed


def clip_caption_words(clip) -> List[dict]:
    start, end, _duration = clip_extent(clip.data)
    return cap.clip_words(transcript_words(clip), start, end)


def _caption_clips(clip_id: Optional[str] = None) -> list:
    """Timeline clips that are caption (or subject cut-out) overlays, optionally of one clip."""
    from classes.query import Clip, File

    out = []
    for c in Clip.filter():
        f = File.get(id=str(c.data.get("file_id") or ""))
        info = ((f.data.get("ai_metadata") or {}).get("captions") if f else None) or {}
        if info and (clip_id is None or info.get("clip_id") == clip_id):
            out.append((c, info))
    return out


def _remove(clip_id: Optional[str]) -> int:
    def _delete():
        found = _caption_clips(clip_id)
        for c, _info in found:
            c.delete()
        return len(found)

    return int(on_main(_delete) or 0)


def _import_overlay(path: str, label: str, info: dict):
    handlers = th()
    f, err = handlers._import_generated_video(path, preserve_alpha=True)
    if err or not f:
        raise ToolError("import failed: %s" % (err or "no project file was created"))

    def _stamp():
        handlers._stamp_motion_graphics_file_metadata(f, label, True)
        ai = dict(f.data.get("ai_metadata") or {})
        ai["source"], ai["captions"] = SOURCE, info
        f.data["ai_metadata"] = ai
        f.save()

    on_main(_stamp)
    return f


def _subject_coverage(project: str, media: str, at: float) -> Optional[float]:
    """Share of the frame at *at* seconds that the matting keeps (0 = nothing to cut out); None if unknown."""
    try:
        with _probe_lock:
            _run(project, "ffmpeg", ["-y", "-v", "error", "-ss", str(at), "-i", media, "-frames:v", "1",
                                     "probe-frame.png"], 300, inputs=[media])
            _run(project, "hyperframes", ["remove-background", "probe-frame.png", "-o", "probe-cut.png"],
                 600)
            out = _run(project, "ffmpeg", ["-v", "info", "-i", "probe-cut.png", "-vf",
                                           "alphaextract,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                                           "-f", "null", "-"], 300)
        return float(_ALPHA_RE.findall(out)[-1]) / 255.0
    except (ToolError, hf.HyperframesError, IndexError, ValueError) as exc:
        log.warning("Captions: could not check the shot for a subject: %s", exc)
        return None


def _people_on_camera(clip) -> Optional[int]:
    """The most people in shot at once during the clip, from the file's indexed chapters; None if not recorded."""
    from classes.query import File

    f = File.get(id=str(clip.data.get("file_id") or ""))
    chapters = ((f.data.get("ai_metadata") or {}).get("chapters") if f else None) or []
    start, end = float(clip.data.get("start") or 0.0), float(clip.data.get("end") or 0.0)
    counts = [int(c["people"]) for c in chapters if isinstance(c, dict) and isinstance(c.get("people"), int)
              and float(c.get("start") or 0.0) < end and float(c.get("end") or 0.0) > start]
    return max(counts) if counts else None


def has_subject(clip) -> bool:
    """Whether anyone is in shot at some point in the clip, so captions can go behind them.

    A speaker who walks in or out, or several speakers, need nothing more: the cut-out
    is made frame by frame, so it holds whoever is there and is empty when nobody is.
    The answer comes from the file's index (people per chapter); a file indexed before
    that was recorded is checked by cutting out three of its frames instead.

    ponytail: that fallback counts any salient foreground, not only people; re-index the
    file for the real answer.
    """
    media = _media(clip)
    if not media:
        return False
    people = _people_on_camera(clip)
    if people is not None:
        return people > 0
    _start, _end, duration = clip_extent(clip.data)
    source_start = float(clip.data.get("start") or 0.0)
    seen = []
    for share in (0.5, 0.2, 0.8):
        coverage = _subject_coverage(_project(clip.id), media, source_start + duration * share)
        if coverage is not None and SUBJECT_COVERAGE[0] <= coverage <= SUBJECT_COVERAGE[1]:
            return True
        seen.append(coverage)
    return all(c is None for c in seen)                  # the check itself failed: do not rule it out


def _visible_band(project: str, rel: str) -> Optional[tuple]:
    """(top, bottom) of everything a transparent render draws, as shares of its height."""
    try:
        out = _run(project, "ffmpeg", ["-v", "info", "-c:v", "libvpx-vp9", "-i", rel, "-vf",
                                       "fps=1,alphaextract,scale=-2:1000,bbox", "-f", "null", "-"], RENDER_TIMEOUT)
    except (ToolError, hf.HyperframesError):
        return None
    rows = [(int(a), int(b)) for a, b in _BBOX_RE.findall(out)]
    return (min(a for a, _b in rows) / 1000.0, max(b for _a, b in rows) / 1000.0) if rows else None


def _set_geometry(clip_id: str, values: dict):
    def _apply():
        from classes.query import Clip

        placed = Clip.get(id=clip_id)
        if placed:
            placed.data.update(copy.deepcopy(values))
            placed.save()

    on_main(_apply)


def _static(value) -> bool:
    return not isinstance(value, dict) or len(value.get("Points") or []) <= 1


def _place(file_id: str, start: float, duration: float) -> str:
    result = th().place_motion_graphic(file_id=file_id, position_seconds=str(start),
                                       duration_seconds=str(duration), mode="overlay",
                                       max_duration_seconds=duration, full_file="true")
    if str(result).startswith("Error"):
        raise ToolError(str(result).removeprefix("Error: "))
    found = _CLIP_ID_RE.search(str(result))
    return found.group(1) if found else ""


def _is_retimed(data: dict) -> bool:
    points = (data.get("time") or {}).get("Points") if isinstance(data.get("time"), dict) else None
    return len(points or []) > 1


def build_captions(clip, style: str = cap.DEFAULT_STYLE, behind_subject: bool = False, text: str = "") -> dict:
    """Render and place captions for one timeline clip; returns a receipt dict.

    Everything that can fail (transcript, render, subject matting) happens before
    the project is touched, so a refusal leaves the clip's current captions alone.
    """
    try:
        component = cap.component_id(style)
    except cap.CaptionError as exc:
        raise ToolError(str(exc))
    if not hf.status()["ready"]:
        raise ToolError("HyperFrames is not set up on this computer yet; call hyperframes_setup_tool "
                        "(install=true) first")
    if _is_retimed(clip.data):
        raise ToolError("this clip's speed was changed, so its words no longer line up with the timeline; "
                        "caption it before changing the speed")
    clip_id = str(clip.id)
    start, _end, duration = clip_extent(clip.data)
    _stage("Reading the clip's speech...")
    words = clip_caption_words(clip)
    if text.strip():
        words = cap.retime_words(words, text)
    if not words:
        raise ToolError("no spoken words were found in this clip, so there is nothing to caption")
    behind_subject = bool(behind_subject) and has_subject(clip)      # nobody in shot: plain captions
    media = _media(clip)

    project = _project(clip_id)
    root = hf.project_dir(project)
    if not os.path.isfile(os.path.join(root, "hyperframes.json")):
        _run(project, "hyperframes", ["init", ".", "--example", "blank", "--non-interactive"], 300)
    rel = "compositions/components/%s.html" % component
    source = os.path.join(root, "captions", component + ".source.html")
    if not os.path.isfile(source):                      # keep the component as published; compile from it
        _run(project, "hyperframes", ["add", component], 300)
        os.makedirs(os.path.dirname(source), exist_ok=True)
        os.replace(os.path.join(root, rel.replace("/", os.sep)), source)
    with open(source, encoding="utf-8") as fh:
        try:
            html = cap.compile_composition(fh.read(), words, duration)
        except cap.CaptionError as exc:
            raise ToolError(str(exc))
    with open(os.path.join(root, rel.replace("/", os.sep)), "w", encoding="utf-8") as fh:
        fh.write(html)

    stamp = time.time_ns() // 1000000
    out_rel, subject_rel = "renders/captions-%d.webm" % stamp, "renders/subject-%d.webm" % stamp
    _stage("Rendering the captions...")
    _run(project, "hyperframes", ["render", ".", "-c", rel, "--format", "webm", "-o", out_rel, "--quiet"],
         RENDER_TIMEOUT)
    if behind_subject:
        _stage("Cutting out the speaker (the slow part: minutes for each minute of video)...")
        _run(project, "ffmpeg", ["-y", "-v", "error", "-ss", str(float(clip.data.get("start") or 0.0)),
                                 "-t", str(duration), "-i", media, "-an", "-c:v", "libx264", "-crf", "18",
                                 "subject-source.mp4"], RENDER_TIMEOUT, inputs=[media])
        _run(project, "hyperframes", ["remove-background", "subject-source.mp4", "-o", subject_rel], MATTE_TIMEOUT)

    _stage("Placing the captions on the timeline...")
    title = str(clip.data.get("title") or clip_id)
    info = {"clip_id": clip_id, "style": style, "behind_subject": bool(behind_subject), "role": "captions",
            "text": " ".join(w["text"] for w in words)}
    caption_file = _import_overlay(os.path.join(root, out_rel.replace("/", os.sep)), "Captions: " + title, info)
    subject = behind_subject and _import_overlay(os.path.join(root, subject_rel.replace("/", os.sep)),
                                                 "Subject: " + title, dict(info, role="subject"))
    previous = on_main(lambda: [copy.deepcopy(c.data) for c, _info in _caption_clips(clip_id)])
    tracks = on_main(lambda: {int(t.get("number") or 0) for t in th()._get_app().project.get("layers") or []})
    removed = _remove(clip_id)                           # one caption track per clip: this replaces the last
    placed = []
    try:
        receipt = _place_overlays(clip, caption_file, subject, start, duration, project, out_rel, placed)
    except Exception:
        # A failed placement leaves the project as it was: old captions, no new files, no spare track.
        on_main(_put_back, placed, previous, [caption_file, subject], tracks)
        raise
    receipt.update({"clip_id": clip_id, "style": style, "words": len(words), "replaced": removed,
                    "behind_subject": bool(behind_subject)})
    return receipt


def _put_back(placed: list, previous: list, files: list, tracks: set):
    from classes.query import Clip, Track

    for cid in placed:
        new = Clip.get(id=cid)
        if new:
            new.delete()
    for f in files:
        if f:
            f.delete()
    for data in previous:
        old = Clip()
        old.data = {k: v for k, v in data.items() if k != "id"}
        old.save()
    used = {int(c.data.get("layer") or 0) for c in Clip.filter()}
    for track in Track.filter():                         # a lane the failed placement added and left empty
        number = int(track.data.get("number") or 0)
        if number not in tracks and number not in used:
            track.delete()


def _place_overlays(clip, caption_file, subject, start, duration, project, out_rel, placed) -> dict:
    receipt = {"timeline_clip_id": _place(caption_file.id, start, duration), "file_id": caption_file.id}
    placed.append(receipt["timeline_clip_id"])
    if subject:
        # Placed after the captions, so the placement rule puts it on the track above them.
        receipt["subject_clip_id"] = _place(subject.id, start, duration)
        placed.append(receipt["subject_clip_id"])
        receipt["subject_file_id"] = subject.id
        # The cut-out covers the footage exactly: same fit and transform. Animated ones are
        # left alone -- their keyframes count from the footage's own in-point, not the cut-out's.
        _set_geometry(receipt["subject_clip_id"], {k: clip.data[k] for k in _GEOMETRY
                                                   if k in clip.data and _static(clip.data[k])})
        # Bottom-of-frame captions would hide behind the body; lift them to head height,
        # where the speaker overlaps them and the ends stay readable.
        band = _visible_band(project, out_rel)
        if band:
            lift = BEHIND_SUBJECT_CENTRE - (band[0] + band[1]) / 2.0
            _set_geometry(receipt["timeline_clip_id"],
                          {"location_y": {"Points": [{"co": {"X": 1.0, "Y": lift}, "interpolation": 0}]}})
    return receipt


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@editor_tool(
    "add_captions_tool",
    label="Add captions",
    schema=obj({
        "timeline_clip_id": string("The clip to caption (id from get_timeline_state_tool).", ""),
        "clip_query": string("Or describe the clip ('the interview clip').", ""),
        "style": enum(list(cap.STYLES), "Caption style; list_captions_tool describes each.", cap.DEFAULT_STYLE),
        "behind_subject": boolean("Put the captions BEHIND the speaker: a cut-out of the subject is placed on "
                                  "the track above the captions. Slower (subject matting).", False),
        "text": string("Corrected caption text, to fix what the transcript got wrong. Keep the word count to "
                       "keep each word's timing.", ""),
        "whole_project": boolean("Caption every clip on the timeline that has speech, in one call (no clip id "
                                 "needed). Clips with no spoken words are skipped and listed.", False),
    }),
    background_safe=True,
    covers=("caption.add",),
)
def add_captions(timeline_clip_id="", clip_query="", style=cap.DEFAULT_STYLE, behind_subject=False, text="",
                 whole_project=False):
    """Add animated captions of what is said in a clip, word-timed, as an overlay on a track above it.

    The words come from Zenvi's on-device transcript of the clip, the look from a
    HyperFrames caption style (karaoke pill, highlight sweep, kinetic slam, ...),
    and the render happens on this computer. Use behind_subject=true for captions
    that sit behind the speaker: only when someone is on camera in the clip (see the
    file's chapters), never for b-roll, screen recordings or graphics. People coming
    and going, or several speakers, are fine. Captioning a clip again replaces its captions,
    so call this again to change the style or to fix words with text=. One undo
    step removes them.

    Example: {"clip_query": "the interview", "style": "highlight"}; the whole edit at once:
    {"whole_project": true, "style": "highlight"}.
    """
    if whole_project:
        return _caption_project(style, bool(behind_subject))
    clip = on_main(resolve_clip, timeline_clip_id, clip_query)
    r = build_captions(clip, style, bool(behind_subject), text)
    note = ""
    if r["behind_subject"]:
        note = ", with the subject in front of them"
    elif behind_subject:
        note = "; no speaker was found in the shot, so they sit on top"
    return ok("Added %d-word '%s' captions above the clip%s." % (r["words"], cap.STYLES[style]["label"], note), **r)


def _speech_clips() -> list:
    """Timeline clips that can carry speech, in playing order: everything with sound that is not an overlay."""
    from classes.query import Clip

    overlays = {str(c.id) for c, _info in _caption_clips()}
    clips = [c for c in Clip.filter() if str(c.id) not in overlays
             and (c.data.get("reader") or {}).get("has_audio") is not False]
    return sorted(clips, key=lambda c: (float(c.data.get("position") or 0.0), -int(c.data.get("layer") or 0)))


def _caption_project(style: str, behind_subject: bool):
    """Caption every clip with speech. One clip failing (no words, changed speed) does not stop the rest."""
    clips = on_main(_speech_clips)
    if not clips:
        raise ToolError("there are no clips with sound on the timeline to caption")
    captioned, skipped = [], []
    for n, clip in enumerate(clips, 1):
        title = str(clip.data.get("title") or clip.id)
        _stage("Clip %d of %d: %s" % (n, len(clips), title))
        try:
            captioned.append(dict(build_captions(clip, style, behind_subject), title=title))
        except ToolError as exc:
            skipped.append({"clip_id": str(clip.id), "title": title, "reason": str(exc)})
    if not captioned:
        raise ToolError("no clip could be captioned: " + "; ".join(
            "%s: %s" % (s["title"], s["reason"]) for s in skipped[:5]))
    return ok("Captioned %d of %d clip(s) with '%s' captions%s." % (
        len(captioned), len(clips), cap.STYLES.get(style, {}).get("label", style),
        "; %d skipped (no speech found or not captionable)" % len(skipped) if skipped else ""),
        captioned=captioned, skipped=skipped, style=style)


@editor_tool(
    "list_captions_tool",
    label="List captions",
    schema=obj({}),
    read_only=True,
    covers=("caption.edit",),
)
def list_captions():
    """List the caption styles that can be used and the caption tracks already on the timeline."""
    tracks = [{"timeline_clip_id": str(c.id), "captioned_clip_id": info.get("clip_id"), "style": info.get("style"),
               "role": info.get("role"), "text": info.get("text", "")[:400]} for c, info in _caption_clips()]
    styles = [{"style": key, "name": s["label"], "look": s["description"]} for key, s in cap.STYLES.items()]
    return ok("%d caption style(s); %d caption overlay(s) on the timeline." % (len(styles), len(tracks)),
              styles=styles, captions=tracks)


@editor_tool(
    "remove_captions_tool",
    label="Remove captions",
    schema=obj({
        "timeline_clip_id": string("Remove the captions of this clip. Empty with clip_query empty removes "
                                   "every caption track.", ""),
        "clip_query": string("Or describe the captioned clip.", ""),
    }),
    covers=("caption.edit",),
)
def remove_captions(timeline_clip_id="", clip_query=""):
    """Remove the captions (and any subject cut-out) of one clip, or all captions on the timeline."""
    target = None
    if str(timeline_clip_id).strip() or str(clip_query).strip():
        target = str(resolve_clip(timeline_clip_id, clip_query).id)
    removed = _remove(target)
    if not removed:
        raise ToolError("there are no captions to remove" + (" on that clip" if target else ""))
    return ok("Removed %d caption overlay(s)." % removed, removed=removed, clip_id=target)
