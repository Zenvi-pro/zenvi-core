"""Phase 5 agent tools beyond get/remove words: silence, captions, beats, search, diarize."""

from __future__ import annotations

import logging
import os
import uuid

log = logging.getLogger("agent_tools.speech_extra")


def _fps(app):
    from classes.clip_utils import project_fps_fraction
    try:
        return project_fps_fraction()
    except Exception:
        from fractions import Fraction
        fps = app.project.get("fps") or {}
        return Fraction(float(fps.get("num", 30) or 30), float(fps.get("den", 1) or 1) or 1)


def _snapshot(app):
    from classes.agent_tools.inspect_render import snapshot_project
    return snapshot_project(app)


def _find_clip(project_data, clip_id: str):
    for c in project_data.get("clips") or []:
        if isinstance(c, dict) and str(c.get("id")) == str(clip_id):
            return c
    return None


def _media_path_for_clip(clip: dict) -> str:
    from classes.agent_tools.transcript import _file_data_for_clip
    from classes.speech.map_timeline import resolve_media_path
    fd = _file_data_for_clip(clip)
    path = resolve_media_path(fd) if fd else ""
    if path:
        return path
    reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
    return str(reader.get("path") or "")


def remove_silence(
    clipId: str = "",
    minPauseSec=0.4,
    padSec=0.08,
    maxRemoveFraction=0.85,
    **_kw,
):
    """Remove quiet non-speech gaps inside a clip and close them (one undo)."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.transcript import (
        _first_nonempty,
        apply_compacted_fragments,
    )
    from classes.speech.vad import detect_speech_windows, silence_ranges_from_speech
    from classes.speech.word_ranges import compact_fragments_after_remove
    from classes.tool_handlers import _new_transaction_id

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))

    if not clipId:
        return ToolReceipt.refused(
            "remove_silence_tool", "Error: clipId is required.",
        ).to_json()

    try:
        from classes.app import get_app
        app = get_app()
        project_data = _snapshot(app)
    except Exception as exc:
        return ToolReceipt.error("remove_silence_tool", str(exc)).to_json()

    clip = _find_clip(project_data, str(clipId))
    if clip is None:
        return ToolReceipt.refused(
            "remove_silence_tool", f"Error: Unknown clipId '{clipId}'.",
        ).to_json()

    path = _media_path_for_clip(clip)
    if not path or not os.path.isfile(path):
        return ToolReceipt.refused(
            "remove_silence_tool", "Error: Clip has no readable media path.",
        ).to_json()

    try:
        min_pause = float(minPauseSec)
        pad = float(padSec)
        max_frac = float(maxRemoveFraction)
    except (TypeError, ValueError):
        return ToolReceipt.refused(
            "remove_silence_tool", "Error: Invalid numeric arguments.",
        ).to_json()

    pos = float(clip.get("position") or 0)
    start = float(clip.get("start") or 0)
    end = float(clip.get("end") or start)
    duration = max(0.0, end - start)
    if duration <= 0:
        return ToolReceipt.unchanged(
            "remove_silence_tool", "Clip has zero duration.",
        ).to_json()

    try:
        speech = detect_speech_windows(
            path, min_speech_sec=0.12, min_silence_sec=min_pause,
        )
    except Exception as exc:
        return ToolReceipt.error(
            "remove_silence_tool", f"Error: VAD failed: {exc}",
        ).to_json()

    # Clip speech windows to the trimmed source range, rebased into source time.
    clipped = []
    for s, e in speech:
        s2, e2 = max(s, start), min(e, end)
        if e2 > s2:
            clipped.append((s2, e2))
    gaps = silence_ranges_from_speech(
        clipped, duration=end, min_pause_sec=min_pause, pad_sec=pad,
    )
    # Only gaps inside [start, end]
    gaps = [(max(start, a), min(end, b)) for a, b in gaps if min(end, b) > max(start, a)]
    if not gaps:
        return ToolReceipt.unchanged(
            "remove_silence_tool", "No removable silence found.",
        ).to_json()

    fps = _fps(app)
    fragments, removed = compact_fragments_after_remove(
        position=pos, start=start, end=end, remove_ranges=gaps, fps=fps,
    )
    if duration > 0 and removed / duration > max_frac:
        return ToolReceipt.refused(
            "remove_silence_tool",
            f"Error: Would remove {removed / duration:.0%} of the clip — "
            "refusing (raise maxRemoveFraction or lower minPauseSec).",
            data={"removedDurationSec": removed, "clipDurationSec": duration},
        ).to_json()
    if removed <= 1e-9:
        return ToolReceipt.unchanged(
            "remove_silence_tool", "No silence to remove after padding.",
        ).to_json()
    if not fragments:
        return ToolReceipt.refused(
            "remove_silence_tool",
            "Error: Silence removal would delete the entire clip.",
        ).to_json()

    tid = _new_transaction_id()
    try:
        created, shifted = apply_compacted_fragments(
            app, clip_id=str(clipId), fragments=fragments,
            removed=removed, fps=fps, tid=tid,
        )
    except Exception as exc:
        return ToolReceipt.error("remove_silence_tool", f"Error: {exc}").to_json()

    return ToolReceipt.applied(
        "remove_silence_tool",
        f"Removed {removed:.2f}s silence ({len(gaps)} gap(s)).",
        undo_steps=1,
        shifted=shifted,
        data={
            "clipId": str(clipId),
            "removedDurationSec": removed,
            "gaps": [{"startSec": a, "endSec": b} for a, b in gaps],
            "createdClipIds": created,
            "speechWindows": [{"startSec": a, "endSec": b} for a, b in clipped],
            "vadSource": "local",
        },
    ).to_json()


def _caption_track(app, clip_id: str, cues: list[dict]) -> str:
    """Layer number for this caption group: one track above everything it covers.

    The track must sit above the captioned clip and above every other clip
    that overlaps the captions in time, or that video draws over them. The
    lowest such track is free in that span by construction, so reuse it;
    when there is none, add a new track on top. Runs on the GUI thread.
    """
    from classes.query import Clip

    layers = sorted(
        int(t.get("number")) for t in (app.project.get("layers") or [])
        if isinstance(t, dict) and t.get("number") is not None
    )
    if not layers or not cues:
        return ""
    span_start = min(float(c.get("startSec") or 0.0) for c in cues)
    span_end = max(float(c.get("endSec") or 0.0) for c in cues)

    floor = None
    if clip_id:
        target = Clip.get(id=clip_id)
        if target is not None:
            floor = int(target.data.get("layer") or 0)
    for clip in Clip.filter():
        data = clip.data
        start = float(data.get("position") or 0.0)
        end = start + float(data.get("end") or 0.0) - float(data.get("start") or 0.0)
        if start < span_end and end > span_start:
            layer = int(data.get("layer") or 0)
            floor = layer if floor is None else max(floor, layer)
    if floor is None:
        floor = layers[0]

    above = [n for n in layers if n > floor]
    if above:
        return str(above[0])
    new_layer = layers[-1] + 1000000
    app.window.ensure_tracks_for_layers([new_layer])
    return str(new_layer)


def _discard_group(app, tid) -> bool:
    """Revert what a caption request recorded when it placed nothing.

    The track it made and any caption images it imported go away with no
    undo or redo entry left behind. Only the tail transaction can be
    reverted this way; if anything else landed after it, keep history as
    is. Runs on the GUI thread.
    """
    updates = app.updates
    history = updates.actionHistory
    if not tid or not history or history[-1].transaction != tid:
        return False
    redo_len = len(updates.redoHistory)
    updates.undo()
    del updates.redoHistory[redo_len:]
    return True


def add_captions(
    clipId: str = "",
    trackIndex=None,
    maxWords=8,
    maxChars=42,
    language: str = "auto",
    modelId: str = "",
    engine: str = "auto",
    srtPath: str = "",
    **_kw,
):
    """Place timed dialogue captions from on-device transcript (or import an SRT/VTT)."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.transcript import _first_nonempty, get_transcript
    from classes.agent_tools.titles import add_title
    from classes.speech.captions import parse_srt_or_vtt, phrase_words
    from classes.agent_tools.receipt import parse_receipt

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    srtPath = _first_nonempty(srtPath, _kw.get("srt_path"))
    if trackIndex in (None, "") and _kw.get("track_index") not in (None, ""):
        trackIndex = _kw.get("track_index")
    eng = _first_nonempty(engine, _kw.get("engine")) or "auto"

    try:
        from classes.app import get_app
        app = get_app()
    except Exception as exc:
        return ToolReceipt.error("add_captions_tool", str(exc)).to_json()

    fps = _fps(app)
    caption_group = f"cap_{uuid.uuid4().hex[:10]}"
    cues: list[dict] = []
    source = "local"
    generation = None

    if srtPath:
        if not os.path.isfile(srtPath):
            return ToolReceipt.refused(
                "add_captions_tool", f"Error: srtPath not found: {srtPath}",
            ).to_json()
        with open(srtPath, "r", encoding="utf-8") as fh:
            text = fh.read()
        try:
            cues = parse_srt_or_vtt(text, fps=fps)
        except Exception as exc:
            return ToolReceipt.error(
                "add_captions_tool", f"Error: Failed to parse subtitles: {exc}",
            ).to_json()
        source = "srt"
    else:
        # Need the verbose words array (startSec/endSec) for phrasing — not compactWords.
        raw = get_transcript(
            clipId=clipId or "",
            trackIndex=trackIndex,
            language=language,
            modelId=modelId,
            engine=eng,
            includeWords=True,
        )
        receipt = parse_receipt(raw)
        if receipt.get("status") in ("error", "refused"):
            return raw if isinstance(raw, str) else ToolReceipt.error(
                "add_captions_tool", receipt.get("summary", "Error: transcript failed"),
            ).to_json()
        if receipt.get("status") == "unchanged":
            return ToolReceipt.unchanged(
                "add_captions_tool",
                str(receipt.get("summary") or "No spoken dialogue to caption."),
                data=receipt.get("data") if isinstance(receipt.get("data"), dict) else {},
            ).to_json()
        data = receipt.get("data") or {}
        source = data.get("transcriptionSource") or "local"
        generation = data.get("transcriptGeneration")
        words = []
        for c in data.get("clips") or []:
            words.extend(c.get("words") or [])
        if not words:
            return ToolReceipt.unchanged(
                "add_captions_tool", "No words to caption.",
            ).to_json()
        try:
            mw = int(maxWords)
            mc = int(maxChars)
        except (TypeError, ValueError):
            mw, mc = 8, 42
        cues = phrase_words(words, max_words=mw, max_chars=mc, fps=fps)

    if not cues:
        return ToolReceipt.unchanged(
            "add_captions_tool", "No caption cues produced.",
        ).to_json()

    placed = []
    warnings = []
    # The dispatcher's undo group for this call; every caption joins it.
    tid = getattr(app.updates, "transaction_id", None)
    # One track for the whole group, above the clip and anything over it.
    from classes.tool_handlers import QThread, _run_on_main_thread
    off_gui = QThread is not None and QThread.currentThread() is not app.thread()
    if off_gui:
        track = _run_on_main_thread(_caption_track, app, str(clipId or ""), cues)
    else:
        track = _caption_track(app, str(clipId or ""), cues)
    for cue in cues:
        body = str(cue.get("text") or "").strip()
        if not body:
            continue
        pos = float(cue.get("startSec") or 0)
        dur = max(0.05, float(cue.get("endSec") or 0) - pos)
        result = add_title(
            text=body,
            position_seconds=str(pos),
            duration_seconds=str(dur),
            track=track,
            file_name=f"{caption_group}_{len(placed)}.svg",
            raster=True,
        )
        pr = parse_receipt(result)
        if pr.get("status") in ("error", "refused"):
            warnings.append(pr.get("summary") or "caption place failed")
            continue
        placed.append({
            "text": body,
            "startSec": pos,
            "endSec": pos + dur,
            "startFrame": cue.get("startFrame"),
            "endFrame": cue.get("endFrame"),
            "file_id": (pr.get("data") or {}).get("file_id"),
        })

    if not placed:
        # A failed request must not leave a track or an undo step behind.
        if off_gui:
            _run_on_main_thread(_discard_group, app, tid)
        else:
            _discard_group(app, tid)
        return ToolReceipt.error(
            "add_captions_tool",
            "Error: Failed to place any captions. " + "; ".join(warnings[:2]),
        ).to_json()

    first = placed[0]
    return ToolReceipt.applied(
        "add_captions_tool",
        f"Placed {len(placed)} caption(s) (group {caption_group}).",
        undo_steps=1,
        warnings=warnings,
        watchSuggested={
            "clipId": str(clipId or ""),
            "start": float(first["startSec"]),
            "end": float(first["endSec"]),
        } if clipId else None,
        data={
            "captionGroupId": caption_group,
            "captions": placed,
            "transcriptionSource": source,
            "transcriptGeneration": generation,
            "count": len(placed),
        },
    ).to_json()


def export_captions(
    path: str = "",
    format: str = "srt",
    clipId: str = "",
    trackIndex=None,
    language: str = "auto",
    **_kw,
):
    """Export timeline (or clip) dialogue as SRT/VTT."""
    from classes.agent_tools.receipt import ToolReceipt, parse_receipt
    from classes.agent_tools.transcript import _first_nonempty, get_transcript
    from classes.speech.captions import cues_to_srt, cues_to_vtt, phrase_words

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    if trackIndex in (None, "") and _kw.get("track_index") not in (None, ""):
        trackIndex = _kw.get("track_index")

    if not path:
        return ToolReceipt.refused(
            "export_captions_tool", "Error: path is required.",
        ).to_json()

    try:
        from classes.app import get_app
        app = get_app()
        fps = _fps(app)
    except Exception as exc:
        return ToolReceipt.error("export_captions_tool", str(exc)).to_json()

    raw = get_transcript(
        clipId=clipId or "",
        trackIndex=trackIndex,
        language=language,
        includeWords=True,
        engine="auto",
    )
    receipt = parse_receipt(raw)
    if receipt.get("status") in ("error", "refused"):
        return raw if isinstance(raw, str) else ToolReceipt.error(
            "export_captions_tool", receipt.get("summary", "Error"),
        ).to_json()
    words = []
    for c in (receipt.get("data") or {}).get("clips") or []:
        words.extend(c.get("words") or [])
    cues = phrase_words(words, fps=fps)
    fmt = (format or "srt").lower().strip()
    body = cues_to_vtt(cues, fps=fps) if fmt == "vtt" else cues_to_srt(cues, fps=fps)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        tmp = path + f".{os.getpid()}.partial"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except Exception as exc:
        return ToolReceipt.error(
            "export_captions_tool", f"Error: write failed: {exc}",
        ).to_json()

    return ToolReceipt.applied(
        "export_captions_tool",
        f"Exported {len(cues)} cue(s) to {path}.",
        undo_steps=0,
        data={"path": path, "format": fmt, "count": len(cues)},
    ).to_json()


def detect_beats_tool_handler(
    fileId: str = "",
    clipId: str = "",
    **_kw,
):
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.transcript import _first_nonempty
    from classes.speech.beats import detect_beats
    from classes.frame_time import to_frame

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    fileId = _first_nonempty(fileId, _kw.get("file_id"))

    try:
        from classes.app import get_app
        app = get_app()
        fps = _fps(app)
        project_data = _snapshot(app)
    except Exception as exc:
        return ToolReceipt.error("detect_beats_tool", str(exc)).to_json()

    path = ""
    if clipId:
        clip = _find_clip(project_data, str(clipId))
        if clip is None:
            return ToolReceipt.refused(
                "detect_beats_tool", f"Error: Unknown clipId '{clipId}'.",
            ).to_json()
        path = _media_path_for_clip(clip)
    elif fileId:
        from classes.query import File
        from classes.speech.map_timeline import resolve_media_path
        f = File.get(id=str(fileId))
        if f is None or not isinstance(getattr(f, "data", None), dict):
            return ToolReceipt.refused(
                "detect_beats_tool", f"Error: Unknown fileId '{fileId}'.",
            ).to_json()
        path = resolve_media_path(f.data)
    else:
        return ToolReceipt.refused(
            "detect_beats_tool", "Error: Provide fileId or clipId.",
        ).to_json()

    if not path or not os.path.isfile(path):
        return ToolReceipt.refused(
            "detect_beats_tool", "Error: Media path missing.",
        ).to_json()

    try:
        result = detect_beats(path)
    except Exception as exc:
        return ToolReceipt.error(
            "detect_beats_tool", f"Error: Beat detection failed: {exc}",
        ).to_json()

    beats = []
    for b in result.get("beats") or []:
        t = float(b["timeSec"])
        beats.append({
            "timeSec": t,
            "frame": to_frame(t, fps),
            "strength": b.get("strength"),
        })
    downbeats = []
    for b in result.get("downbeats") or []:
        t = float(b["timeSec"])
        downbeats.append({"timeSec": t, "frame": to_frame(t, fps)})

    return ToolReceipt.applied(
        "detect_beats_tool",
        f"Detected {len(beats)} beat(s), BPM≈{result.get('bpm', 0):.1f}.",
        undo_steps=0,
        data={
            "beats": beats,
            "downbeats": downbeats,
            "bpm": result.get("bpm"),
            "fileId": fileId or "",
            "clipId": clipId or "",
            "provider": "local",
        },
    ).to_json()


def diarize_media(
    fileId: str = "",
    clipId: str = "",
    maxSpeakers=2,
    language: str = "auto",
    modelId: str = "",
    **_kw,
):
    """Attach speakerIds to cached transcript words for a file/clip."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.transcript import _first_nonempty
    from classes.speech.asr import transcribe_file
    from classes.speech.cache import DEFAULT_MODEL_ID, get_default_cache
    from classes.speech.diarize import diarize_file_words, speaker_turns
    from classes.speech.map_timeline import resolve_media_path, words_for_clip

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    fileId = _first_nonempty(fileId, _kw.get("file_id"))

    try:
        from classes.app import get_app
        app = get_app()
        project_data = _snapshot(app)
        fps = _fps(app)
    except Exception as exc:
        return ToolReceipt.error("diarize_media_tool", str(exc)).to_json()

    path = ""
    clip = None
    if clipId:
        clip = _find_clip(project_data, str(clipId))
        if clip is None:
            return ToolReceipt.refused(
                "diarize_media_tool", f"Error: Unknown clipId '{clipId}'.",
            ).to_json()
        path = _media_path_for_clip(clip)
    elif fileId:
        from classes.query import File
        f = File.get(id=str(fileId))
        if f is None or not isinstance(getattr(f, "data", None), dict):
            return ToolReceipt.refused(
                "diarize_media_tool", f"Error: Unknown fileId '{fileId}'.",
            ).to_json()
        path = resolve_media_path(f.data)
    else:
        return ToolReceipt.refused(
            "diarize_media_tool", "Error: Provide fileId or clipId.",
        ).to_json()

    if not path:
        return ToolReceipt.refused(
            "diarize_media_tool", "Error: Media path missing.",
        ).to_json()

    model = (modelId or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    try:
        record = transcribe_file(path, language=language or "auto", model_id=model)
        labelled, warnings = diarize_file_words(
            path, record.words, max_speakers=int(maxSpeakers or 2),
        )
    except Exception as exc:
        return ToolReceipt.error(
            "diarize_media_tool", f"Error: Diarization failed: {exc}",
        ).to_json()

    record.words = labelled
    record.generation = int(record.generation) + 1
    get_default_cache().put(record)

    mapped = []
    if clip is not None:
        mapped = words_for_clip(
            labelled,
            clip_id=str(clipId),
            position=float(clip.get("position") or 0),
            start=float(clip.get("start") or 0),
            end=float(clip.get("end") or 0),
            fps=fps,
            generation=record.generation,
        )
    else:
        from classes.frame_time import to_frame
        for i, w in enumerate(labelled):
            mapped.append({
                "index": i,
                "text": w.text,
                "startSec": w.startSec,
                "endSec": w.endSec,
                "startFrame": to_frame(w.startSec, fps),
                "endFrame": to_frame(w.endSec, fps),
                "speakerId": w.speakerId,
                "transcriptGeneration": record.generation,
            })

    speakers = sorted({w.get("speakerId") for w in mapped if w.get("speakerId")})
    return ToolReceipt.applied(
        "diarize_media_tool",
        f"Labelled {len(speakers)} speaker(s) on {len(mapped)} words.",
        undo_steps=0,
        warnings=warnings,
        data={
            "speakers": speakers,
            "words": mapped,
            "turns": speaker_turns(mapped),
            "transcriptGeneration": record.generation,
            "transcriptionSource": record.transcriptionSource,
        },
    ).to_json()


def search_media_local(
    query: str = "",
    top_k=5,
    provider: str = "local",
    **_kw,
):
    """Search the local visual embedding index (CLIP/hash). Cloud is opt-in elsewhere."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.speech.visual_index import get_visual_index

    q = str(query or "").strip()
    if not q:
        return ToolReceipt.refused(
            "search_media_local_tool", "Error: query is required.",
        ).to_json()

    prov = (provider or "local").lower().strip()
    if prov not in ("local", "auto"):
        return ToolReceipt.refused(
            "search_media_local_tool",
            "Error: provider must be 'local' or 'auto' (cloud via search_clips_tool).",
        ).to_json()

    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 5

    # Best-effort: index media-bin files that aren't in the index yet.
    try:
        from classes.app import get_app
        from classes.query import File
        app = get_app()
        index = get_visual_index()
        project = _snapshot(app)
        for fdata in project.get("files") or []:
            if not isinstance(fdata, dict):
                continue
            path = str(fdata.get("path") or "")
            fid = str(fdata.get("id") or "")
            if path and os.path.isfile(path):
                try:
                    index.upsert_file(fid, path)
                except Exception:
                    continue
        # Also try File.query if snapshot lacks files
        try:
            for fobj in File.filter():
                if fobj and isinstance(fobj.data, dict):
                    p = str(fobj.data.get("path") or "")
                    if p and os.path.isfile(p):
                        index.upsert_file(str(fobj.id), p)
        except Exception:
            pass
        hits = index.search(q, top_k=k)
    except Exception as exc:
        return ToolReceipt.error(
            "search_media_local_tool", f"Error: Local search failed: {exc}",
        ).to_json()

    return ToolReceipt.applied(
        "search_media_local_tool",
        f"{len(hits)} local hit(s) for {q!r}.",
        undo_steps=0,
        data={"query": q, "hits": hits, "provider": "local"},
        notes=["Scores are for ordering only — uncalibrated."] if hits else [],
    ).to_json()
