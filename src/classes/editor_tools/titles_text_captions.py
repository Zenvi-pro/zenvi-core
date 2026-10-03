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

import os
import re
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

SOURCE = "hyperframes_captions"
RENDER_TIMEOUT = 1800
MATTE_TIMEOUT = 3600
_CLIP_ID_RE = re.compile(r"timeline_clip_id=([A-Za-z0-9]+)")


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


def transcript_words(clip) -> List[dict]:
    """The clip's spoken words in timeline seconds.

    Word-timed from Zenvi's on-device recogniser; when that is not installed or
    fails, phrase-timed from the transcript stored when the file was indexed.
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


def _place(file_id: str, start: float, duration: float) -> str:
    result = th().place_motion_graphic(file_id=file_id, position_seconds=str(start),
                                       duration_seconds=str(duration), mode="overlay",
                                       max_duration_seconds=duration)
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
    words = clip_caption_words(clip)
    if text.strip():
        words = cap.retime_words(words, text)
    if not words:
        raise ToolError("no spoken words were found in this clip, so there is nothing to caption")
    media = ""
    if behind_subject:
        from classes.query import File

        f = File.get(id=str(clip.data.get("file_id") or ""))
        media = f.absolute_path() if f else ""
        if not media or not os.path.isfile(media):
            raise ToolError("the clip's media file was not found, so the speaker cannot be cut out")

    project = "captions-" + re.sub(r"[^a-z0-9]", "", clip_id.lower())[:40]
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
    _run(project, "hyperframes", ["render", ".", "-c", rel, "--format", "webm", "-o", out_rel, "--quiet"],
         RENDER_TIMEOUT)
    if behind_subject:
        _run(project, "ffmpeg", ["-y", "-v", "error", "-ss", str(float(clip.data.get("start") or 0.0)),
                                 "-t", str(duration), "-i", media, "-an", "-c:v", "libx264", "-crf", "18",
                                 "subject-source.mp4"], RENDER_TIMEOUT, inputs=[media])
        _run(project, "hyperframes", ["remove-background", "subject-source.mp4", "-o", subject_rel], MATTE_TIMEOUT)

    title = str(clip.data.get("title") or clip_id)
    info = {"clip_id": clip_id, "style": style, "behind_subject": bool(behind_subject), "role": "captions",
            "text": " ".join(w["text"] for w in words)}
    caption_file = _import_overlay(os.path.join(root, out_rel.replace("/", os.sep)), "Captions: " + title, info)
    subject = behind_subject and _import_overlay(os.path.join(root, subject_rel.replace("/", os.sep)),
                                                 "Subject: " + title, dict(info, role="subject"))
    removed = _remove(clip_id)                           # one caption track per clip: this replaces the last
    receipt = {"timeline_clip_id": _place(caption_file.id, start, duration), "file_id": caption_file.id,
               "clip_id": clip_id, "style": style, "words": len(words), "replaced": removed,
               "behind_subject": bool(behind_subject)}
    if subject:
        # Placed after the captions, so the placement rule puts it on the track above them.
        receipt["subject_clip_id"] = _place(subject.id, start, duration)
        receipt["subject_file_id"] = subject.id
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
    }),
    background_safe=True,
    covers=("caption.add",),
)
def add_captions(timeline_clip_id="", clip_query="", style=cap.DEFAULT_STYLE, behind_subject=False, text=""):
    """Add animated captions of what is said in a clip, word-timed, as an overlay on a track above it.

    The words come from Zenvi's on-device transcript of the clip, the look from a
    HyperFrames caption style (karaoke pill, highlight sweep, kinetic slam, ...),
    and the render happens on this computer. Use behind_subject=true for captions
    that sit behind the speaker. Captioning a clip again replaces its captions,
    so call this again to change the style or to fix words with text=. One undo
    step removes them.

    Example: {"clip_query": "the interview", "style": "highlight"}.
    """
    clip = on_main(resolve_clip, timeline_clip_id, clip_query)
    r = build_captions(clip, style, bool(behind_subject), text)
    return ok("Added %d-word '%s' captions above the clip%s." % (
        r["words"], cap.STYLES[style]["label"], ", with the subject in front of them" if behind_subject else ""), **r)


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
