"""Narration from text: text-to-speech placed on a real timeline track.

Workstream: ai-generation. ``generate_tts_and_add_to_timeline_tool`` replaces
the legacy handler of the same name, which ran the backend HTTP call on the
GUI thread and inserted an unprobed file plus a zero-length clip on layer 0.

Threading: the backend call and the MP3 write run on the calling worker thread
(``background_safe``). One GUI hop then does what dropping the file on the
timeline does: ``files_model.add_files`` probes and imports it, the track rule
picks (or creates) a narration track, and ``timeline.addClip`` places the clip
with its real duration. Everything the hop changes is one undo step.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Iterable, Optional

from classes.editor_tools._base import (
    ToolError,
    get_app,
    is_locked,
    layers,
    nullable,
    number,
    obj,
    ok,
    on_main,
    playhead_seconds,
    resolve_layer,
    snap_seconds,
    string,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

MAX_TTS_CHARS = 4096          # OpenAI speech input limit (the backend forwards to it)
NARRATION_TRACK_NAME = "Narration"
TTS_SOURCE = "tts"
_VOICE_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_EPS = 1e-3
# One GUI hop imports, names and places the clip; probing a short MP3 is quick,
# but add_files pumps events, so give it more than the default 30 s.
PLACE_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Where narration goes when the caller does not name a track
# ---------------------------------------------------------------------------

def _clip_span(clip: dict) -> tuple:
    try:
        start = float(clip.get("position") or 0.0)
        return start, start + max(0.0, float(clip.get("end") or 0.0) - float(clip.get("start") or 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def overlapping_clips(clips: Iterable[dict], layer: int, start: float, end: float) -> list:
    """Clips on *layer* that overlap [start, end) in timeline seconds."""
    out = []
    for c in clips:
        if not isinstance(c, dict):
            continue
        try:
            if int(c.get("layer") or 0) != int(layer):
                continue
        except (TypeError, ValueError):
            continue
        c0, c1 = _clip_span(c)
        if c0 < end - _EPS and c1 > start + _EPS:
            out.append(c)
    return out


def pick_narration_track(layer_rows: Iterable[dict], clips: Iterable[dict], start: float, end: float,
                         narration_file_ids: Iterable[str] = ()) -> tuple:
    """The track narration placed at [start, end) goes on when the caller names none.

    Returns ``(layer_number, reason)``, or ``(None, reason)`` meaning "create a
    narration track directly above the lowest track". Never the lowest track
    (the main picture track, where add_clip_to_timeline_tool puts footage) and
    never a locked one. In order:

    1. a track that already holds only narration clips and is free over the range;
    2. the lowest empty track above the lowest track.
    """
    rows = sorted((t for t in layer_rows if isinstance(t, dict)), key=lambda t: int(t.get("number") or 0))
    clips = [c for c in clips if isinstance(c, dict)]
    narration = {str(f) for f in narration_file_ids}
    if len(rows) < 2:
        return None, "no free track above the lowest one"
    by_layer: dict = {}
    for c in clips:
        try:
            by_layer.setdefault(int(c.get("layer") or 0), []).append(c)
        except (TypeError, ValueError):
            continue
    candidates = [t for t in rows[1:] if not t.get("lock")]
    for t in candidates:
        number_ = int(t.get("number") or 0)
        on_track = by_layer.get(number_, [])
        if on_track and all(str(c.get("file_id") or "") in narration for c in on_track) \
                and not overlapping_clips(on_track, number_, start, end):
            return number_, "the narration track"
    for t in candidates:
        number_ = int(t.get("number") or 0)
        if not by_layer.get(number_):
            return number_, "the lowest empty track above the main track"
    return None, "every track above the main track is in use"


def track_name(layer: int) -> str:
    """The track's label as the timeline shows it ('' when unnamed)."""
    for t in layers():
        try:
            if int(t.get("number") or 0) == int(layer):
                return str(t.get("label") or "")
        except (TypeError, ValueError):
            continue
    return ""


def _narration_file_ids() -> list:
    from classes.query import File
    ids = []
    for f in File.filter():
        ai = f.data.get("ai_metadata") if isinstance(f.data, dict) else None
        if isinstance(ai, dict) and ai.get("source") == TTS_SOURCE:
            ids.append(f.id)
    return ids


# ---------------------------------------------------------------------------
# Metadata the rest of the editor reads (speech role, transcript, captions)
# ---------------------------------------------------------------------------

def estimated_cues(text: str, duration: float) -> list:
    """Sentence cues with times shared out by length (TTS reads at an even pace).

    Marked estimated in the metadata; enough for ducking windows, speech-safe
    cuts and first-pass captions.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()] or [text.strip()]
    weights = [max(1, len(s)) for s in sentences]
    total = float(sum(weights))
    cues, t = [], 0.0
    for sentence, weight in zip(sentences, weights):
        span = duration * weight / total
        cues.append({"start": round(t, 3), "end": round(min(duration, t + span), 3), "text": sentence})
        t += span
    if cues:
        cues[-1]["end"] = round(duration, 3)
    return cues


def _narration_metadata(text: str, duration: float, voice: str, model: str, speed: float) -> dict:
    summary = "Narration: " + text
    return {
        "analyzed": True,
        "provider": "zenvi-tts",
        "source": TTS_SOURCE,
        "short_summary": summary[:200],
        "description": summary[:400],
        "transcript": text,
        "transcript_cues": estimated_cues(text, duration),
        "cue_timing": "estimated",
        "has_speech": True,
        "tts": {"voice": voice, "model": model, "speed": speed},
    }


def default_name(text: str) -> str:
    words = text.split()
    head = " ".join(words[:6])
    return "Narration - " + (head + ("..." if len(words) > 6 else ""))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _start_seconds(position_seconds, position) -> float:
    if position_seconds is not None and position is not None and abs(float(position_seconds) - float(position)) > _EPS:
        raise ToolError(f"position ({position}) and position_seconds ({position_seconds}) disagree; "
                        "pass position_seconds only")
    value = position_seconds if position_seconds is not None else position
    if value is None:
        value = on_main(playhead_seconds)
    return snap_seconds(max(0.0, float(value)))


def _explicit_layer(track) -> Optional[int]:
    raw = str(track if track is not None else "").strip()
    if raw in ("", "0"):
        return None
    layer = resolve_layer(raw)
    if is_locked(layer):
        raise ToolError(f"track {ui_track_number(layer) or layer} is locked; unlock it or leave track empty "
                        "to use a free narration track")
    return layer


def _synthesize(text: str, voice: str, model: str, speed: float) -> str:
    """Backend TTS -> an MP3 in the project's media folder (worker thread)."""
    from classes.api_client import get_backend_client
    from classes.assets import durable_media_path

    resp = get_backend_client().generate_tts(text=text, voice=voice, model=model, speed=float(speed))
    if not isinstance(resp, dict) or not resp.get("success"):
        err = (resp or {}).get("error") if isinstance(resp, dict) else resp
        raise ToolError(f"text-to-speech failed: {err or 'the backend returned no audio'}")
    try:
        raw = base64.b64decode(resp.get("audio_base64") or "")
    except (ValueError, TypeError) as exc:
        raise ToolError(f"text-to-speech returned unreadable audio ({exc})") from None
    if not raw:
        raise ToolError("text-to-speech returned empty audio")
    generated = durable_media_path(ext=".mp3")
    # A readable file name too (narration_welcome_to_lisbon_1a2b3c.mp3): it is what
    # Project Files falls back to and what the person sees in the media folder.
    slug = re.sub(r"[^a-z0-9]+", "_", " ".join(text.lower().split()[:5])).strip("_")[:40] or "voiceover"
    tag = os.path.splitext(os.path.basename(generated))[0].rsplit("_", 1)[-1][:6]
    path = os.path.join(os.path.dirname(generated), f"narration_{slug}_{tag}.mp3")
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        raise ToolError(f"could not write the narration audio to {path}: {exc}") from None
    return path


def _qpoint(x: float):
    from qt_api import QPointF
    return QPointF(float(x), 0.0)


def _import_and_place(path: str, text: str, start: float, explicit_layer: Optional[int], name: str,
                      voice: str, model: str, speed: float) -> dict:
    """GUI thread: import (probe), name, pick/create the track, place. One undo step (the caller's)."""
    from classes import track_ops
    from classes.query import Clip, File

    win = get_app().window
    imported = win.files_model.add_files([path], quiet=True, prevent_image_seq=True,
                                         prevent_recent_folder=True, skip_indexing=True) or []
    file_obj = next((f for f in imported if f), None) or File.get(path=path)
    if not file_obj:
        raise ToolError(f"the narration audio could not be imported (unreadable file {path})")
    try:
        duration = float(file_obj.data.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        raise ToolError(f"the narration audio has no duration (file {path})")

    file_obj.data["name"] = (name or "").strip() or default_name(text)
    file_obj.data["ai_metadata"] = _narration_metadata(text, duration, voice, model, speed)
    tags = [t.strip() for t in str(file_obj.data.get("tags") or "").split(",") if t.strip()]
    for tag in ("narration", "tts"):
        if tag not in tags:
            tags.append(tag)
    file_obj.data["tags"] = ", ".join(tags)
    file_obj.save()
    # Project Files keeps the row's own name text; without this refresh its next
    # item change writes the old (file) name back over the new one (seen live).
    try:
        win.FileUpdated.emit(str(file_obj.id))
    except Exception:
        log.debug("FileUpdated refresh skipped", exc_info=True)

    end = start + duration
    created = False
    overlaps: list = []
    if explicit_layer is not None:
        layer = explicit_layer
        clips_now = list(get_app().project.get("clips") or [])
        overlaps = [c.get("id") for c in overlapping_clips(clips_now, layer, start, end)]
        why = "the requested track"
    else:
        clips_now = list(get_app().project.get("clips") or [])
        layer, why = pick_narration_track(layers(), clips_now, start, end,
                                          [fid for fid in _narration_file_ids() if fid != file_obj.id])
        if layer is None:
            numbers = track_ops.track_numbers()
            if numbers:
                layer = track_ops.insert_track("above", numbers[0], label=NARRATION_TRACK_NAME)
            else:
                layer = track_ops.insert_track("top", label=NARRATION_TRACK_NAME)
            created = True
            why = "a new narration track above the main track"

    placed = win.timeline.addClip(file_obj.id, _qpoint(start), int(layer), ignore_refresh=False,
                                  call_manual_move=False)
    clip_id = placed.get("id") if isinstance(placed, dict) else None
    clip = Clip.get(id=clip_id) if clip_id else None
    if not clip:
        raise ToolError(f"the narration was imported (file_id={file_obj.id}) but could not be placed on the timeline")
    data = clip.data
    c0, c1 = _clip_span(data)
    return {
        "file_id": file_obj.id,
        "timeline_clip_id": clip.id,
        "track": ui_track_number(int(layer)),
        "track_name": track_name(int(layer)),
        "layer": int(layer),
        "position": round(c0, 3),
        "end": round(c1, 3),
        "duration": round(c1 - c0, 3),
        "created_track": created,
        "track_choice": why,
        "overlaps": overlaps,
        "path": path,
    }


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@editor_tool(
    "generate_tts_and_add_to_timeline_tool",
    label="Add narration (TTS)",
    schema=obj({
        "text": string("The words to speak, exactly as they should be read (up to 4096 characters). "
                       "Punctuation shapes the pauses."),
        "voice": string("Speaker voice, e.g. alloy (neutral, default), echo, fable, onyx (deep), nova, "
                        "shimmer, ash, coral, sage.", "alloy"),
        "model": string("Speech model: tts-1 (default, fast), tts-1-hd (higher quality), gpt-4o-mini-tts.",
                        "tts-1"),
        "speed": number("Speaking rate: 1 = normal, 0.8 = slower, 1.25 = faster.", 1.0, minimum=0.25, maximum=4.0),
        "track": string("Track for the narration: UI track number (1 = bottom), track name or layer number. "
                        "Leave empty to use a free narration track (never the bottom picture track; a "
                        "'Narration' track is created above it when every track is busy).", ""),
        "position_seconds": nullable(number("Timeline seconds where the narration starts (0:05 = 5). Omit to "
                                            "start at the playhead.", minimum=0)),
        "position": nullable(number("Older name for position_seconds (same meaning); prefer position_seconds.",
                                    minimum=0)),
        "name": string("Project Files name for the narration (default 'Narration - <first words>').", ""),
    }, required=["text"]),
    background_safe=True,
    covers=("ai.tts",),
)
def generate_tts_and_add_to_timeline(text, voice="alloy", model="tts-1", speed=1.0, track="",
                                     position_seconds=None, position=None, name=""):
    """Speak text with AI text-to-speech and place it on the timeline as a narration / voiceover clip.

    Use for "add a voiceover saying ...", "narrate this intro", "read this script over the
    video". The audio is imported into Project Files (probed, with its transcript) and placed
    at position_seconds (timeline seconds; default the playhead) with its real duration, on
    the given track or, by default, a free narration track above the bottom picture track
    (created when needed). Uses AI credits; generate one line or paragraph per call.
    Returns the new timeline_clip_id, file_id, track and start/end. One undo step removes the
    clip, the file and any track it created. Example: {"text": "Welcome to Lisbon.",
    "position_seconds": 2} -> a ~1.5 s clip at 0:02 on track 2.
    """
    narration = str(text or "").strip()
    if not narration:
        raise ToolError("text is empty; pass the words to speak")
    if len(narration) > MAX_TTS_CHARS:
        raise ToolError(f"text is {len(narration)} characters; the limit is {MAX_TTS_CHARS} per call. "
                        "Split the script and place each part after the previous one.")
    voice = str(voice or "alloy").strip().lower()
    if not _VOICE_RE.match(voice):
        raise ToolError(f"voice {voice!r} is not a voice name (e.g. alloy, echo, nova, onyx, shimmer)")
    model = str(model or "tts-1").strip()
    if not _MODEL_RE.match(model):
        raise ToolError(f"model {model!r} is not a model name (tts-1, tts-1-hd, gpt-4o-mini-tts)")
    start = _start_seconds(position_seconds, position)
    explicit_layer = on_main(lambda: _explicit_layer(track))

    path = _synthesize(narration, voice, model, speed)
    try:
        placed = on_main(lambda: _import_and_place(path, narration, start, explicit_layer, name, voice, model,
                                                   float(speed)), timeout=PLACE_TIMEOUT)
    except ToolError:
        _discard_unused(path)
        raise
    placed.update(voice=voice, model=model, speed=float(speed), characters=len(narration))
    where = f"track {placed['track']}" + (f" ({placed['track_name']})" if placed.get("track_name") else "")
    note = ""
    if placed["created_track"]:
        note = " Created that track."
    if placed["overlaps"]:
        note += f" Warning: it overlaps {len(placed['overlaps'])} clip(s) already on that track."
    head = " ".join(narration.split()[:8])
    return ok(f"Added a {placed['duration']:.2f} s narration ('{head}{'...' if len(narration.split()) > 8 else ''}') "
              f"on {where} at {placed['position']:.2f}-{placed['end']:.2f} s (clip {placed['timeline_clip_id']}).{note}",
              **placed)


def _discard_unused(path: str) -> None:
    """Remove the generated MP3 when nothing in the project uses it."""
    try:
        from classes.query import File
        if on_main(lambda: File.get(path=path)):
            return
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        log.debug("could not remove unused narration audio %s", path, exc_info=True)
