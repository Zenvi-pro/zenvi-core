"""get_transcript_tool / remove_words_tool / transcribe_media_tool (Phase 5)."""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("agent_tools.transcript")


def _first_nonempty(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int_list(value):
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            parsed = parts
        return _as_int_list(parsed)
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return None


def _fps_from_project(project) -> Any:
    from classes.clip_utils import project_fps_fraction
    try:
        return project_fps_fraction()
    except Exception:
        fps = (project.get("fps") if project is not None else None) or {}
        num = float(fps.get("num", 30) or 30)
        den = float(fps.get("den", 1) or 1) or 1.0
        from fractions import Fraction
        return Fraction(num) / Fraction(den)


def _find_clip(project_data: dict, clip_id: str) -> Optional[dict]:
    for clip in project_data.get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id")) == str(clip_id):
            return clip
    return None


def _file_for_id(file_id: str):
    from classes.query import File
    return File.get(id=str(file_id))


def _file_data_for_clip(clip: dict) -> Optional[dict]:
    from classes.query import File
    fid = clip.get("file_id")
    if fid:
        f = File.get(id=str(fid))
        if f and isinstance(f.data, dict):
            return f.data
    reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
    path = reader.get("path")
    if path:
        f = File.get(path=path)
        if f and isinstance(f.data, dict):
            return f.data
    return None


def _ensure_transcript(
    media_path: str,
    *,
    language: str,
    model_id: str,
    force: bool,
    engine: str = "auto",
):
    from classes.speech.asr import transcribe_file
    from classes.speech.cache import DEFAULT_MODEL_ID
    return transcribe_file(
        media_path,
        language=language or "auto",
        model_id=model_id or DEFAULT_MODEL_ID,
        force=force,
        engine=engine or "auto",
    )


def _no_audio_receipt(tool: str = "get_transcript_tool"):
    from classes.agent_tools.present import NO_AUDIO_USER_MSG
    from classes.agent_tools.receipt import ToolReceipt
    return ToolReceipt.unchanged(tool, NO_AUDIO_USER_MSG, data={"reason": "no_audio"})


def _is_no_audio_exc(exc: BaseException) -> bool:
    from classes.agent_tools.present import is_no_audio_message
    return is_no_audio_message(exc)


def _words_payload(record_words, fps, *, generation: int) -> list[dict]:
    from classes.frame_time import to_frame
    words = []
    for i, w in enumerate(record_words):
        sf = to_frame(w.startSec, fps)
        ef = to_frame(w.endSec, fps)
        if ef <= sf:
            ef = sf + 1
        words.append({
            "index": i,
            "text": w.text,
            "startFrame": sf,
            "endFrame": ef,
            "startSec": w.startSec,
            "endSec": w.endSec,
            "transcriptGeneration": generation,
        })
    return words


def _want_full_words(detail_level: str = "", include_words=None, **_kw) -> bool:
    """Verbose words[] array — only when explicitly requested."""
    if include_words is None and _kw.get("includeWords") is not None:
        include_words = _kw.get("includeWords")
    if include_words is None and _kw.get("include_words") is not None:
        include_words = _kw.get("include_words")
    return _as_bool(include_words, False)


def _want_compact_words(detail_level: str = "", include_words=None, **_kw) -> bool:
    """compactWords rows — detail_level=full or includeWords."""
    if _want_full_words(detail_level, include_words, **_kw):
        return True
    detail = _first_nonempty(detail_level, _kw.get("detail_level")).lower()
    return detail in ("full", "all", "verbose", "words", "compact")


def _compact_word_rows(words: list[dict]) -> list[list]:
    return [[w["index"], w["text"], w["startFrame"]] for w in words]


def _clip_transcript_payload(
    words: list[dict],
    *,
    include_words: bool = False,
    include_compact: bool = False,
    **meta,
) -> dict:
    row = dict(meta)
    row["wordCount"] = len(words)
    row["transcriptGeneration"] = meta.get("transcriptGeneration") or (
        words[0].get("transcriptGeneration") if words else 1
    )
    # Default: no word arrays — remove_words_tool(matches=[...]) does not need them.
    if include_compact or include_words:
        row["compactWords"] = _compact_word_rows(words)
    if include_words:
        row["words"] = words
    return row


def _transcript_notes(*, for_edit: bool = True) -> list[str]:
    notes = [
        "data.script is the full dialogue for the user. "
        "data.clipIds are TIMELINE clip ids (not media-bin fileIds). "
        "For cuts: call remove_words_tool ONCE next with data.clipIds[0] + matches "
        "(e.g. [\"FlowCut\",\"flocut\"]) — do NOT call get_transcript_tool again. "
        "Keep language=auto. Never pass fileId to remove_words_tool.",
    ]
    if for_edit:
        notes.append(
            "matches=[...] is enough for brand/filler cuts — you do not need "
            "compactWords or a matching transcriptGeneration. After a successful cut, "
            "use remove_words_tool data.script (full remaining dialogue) — do not "
            "truncate or invent a message limit."
        )
    return notes


def _short_transcript_summary(
    *,
    word_count: int,
    source: str,
    language: str = "",
    clip_ids: Optional[list] = None,
    file_ids: Optional[list] = None,
    suffix: str = "",
) -> str:
    clips = [str(c) for c in (clip_ids or []) if c]
    files = [str(f) for f in (file_ids or []) if f]
    lang = f", {language}" if language else ""
    if clips:
        id_bit = f" clipId={clips[0]}" if len(clips) == 1 else f" clipIds={clips}"
        action = (
            "Full dialogue in data.script. For word cuts call remove_words_tool next "
            "with that clipId + matches=[...] — do not re-call get_transcript_tool."
        )
    elif files:
        id_bit = f" fileId={files[0]}" if len(files) == 1 else f" fileIds={files}"
        action = (
            "Full dialogue in data.script. This is a media-bin fileId, NOT a timeline "
            "clipId — place the file on the timeline (or re-call get_transcript_tool "
            "with empty args once it is placed) before remove_words_tool."
        )
    else:
        id_bit = ""
        action = "Full dialogue in data.script."
    base = (
        f"Transcribed {int(word_count)} words ({source or 'local'}{lang}).{id_bit} "
        f"{action}"
    )
    if suffix:
        return f"{base} {suffix}".strip()
    return base


def _clips_for_file_id(project_data: dict, file_id: str) -> list[dict]:
    """Timeline clips whose media-bin file_id matches (models often confuse the two)."""
    want = str(file_id or "").strip()
    if not want:
        return []
    out: list[dict] = []
    for clip in project_data.get("clips") or []:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        if str(clip.get("file_id") or "") == want:
            out.append(clip)
            continue
        reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
        if str(reader.get("id") or "") == want:
            out.append(clip)
    return out


def _transcript_data_payload(
    *,
    script: str,
    word_count: int,
    clips_out: list[dict],
    source: str,
    model_out: str,
    language_out: str,
    generation: int,
) -> dict:
    # Only real timeline clipIds belong in clipIds — never promote fileId.
    clip_ids = [
        str(c.get("clipId"))
        for c in clips_out
        if c.get("clipId")
    ]
    file_ids = [
        str(c.get("fileId"))
        for c in clips_out
        if c.get("fileId") and not c.get("clipId")
    ]
    if clip_ids:
        next_action = "remove_words_tool"
    elif file_ids:
        next_action = (
            "place file on timeline then get_transcript_tool again — "
            "remove_words_tool needs data.clipIds (timeline), not fileId"
        )
    else:
        next_action = "pass clipId to remove_words_tool or place the clip first"
    # Put actionable edit fields FIRST so they survive any truncation;
    # keep the long script last.
    return {
        "nextAction": next_action,
        "clipIds": clip_ids,
        "fileIds": file_ids,
        "transcriptGeneration": generation,
        "wordCount": word_count,
        "transcriptionSource": source,
        "modelId": model_out,
        "language": language_out,
        "engine": source,
        "clips": clips_out,
        "script": script,
    }


def _file_transcript_receipt(
    *,
    file_id: str,
    words: list[dict],
    record,
    warnings: Optional[list] = None,
    summary_suffix: str = "",
    include_words: bool = False,
    include_compact: bool = False,
):
    from classes.agent_tools.present import words_to_script
    from classes.agent_tools.receipt import ToolReceipt

    script = words_to_script(words)
    clips = [
        _clip_transcript_payload(
            words,
            include_words=include_words,
            include_compact=include_compact,
            fileId=str(file_id),
            transcriptGeneration=record.generation,
        ),
    ]
    summary = _short_transcript_summary(
        word_count=len(words),
        source=record.transcriptionSource,
        language=record.language,
        file_ids=[file_id],
        suffix=summary_suffix,
    ) if words else (
        "No spoken dialogue found." + (f" {summary_suffix}" if summary_suffix else "")
    )
    return ToolReceipt.applied(
        "get_transcript_tool",
        summary,
        undo_steps=0,
        warnings=list(warnings or []),
        notes=_transcript_notes(),
        data=_transcript_data_payload(
            script=script,
            word_count=len(words),
            clips_out=clips,
            source=record.transcriptionSource,
            model_out=record.modelId,
            language_out=record.language,
            generation=record.generation,
        ),
    ).to_json()


def transcribe_media(
    fileId: str = "",
    language: str = "auto",
    modelId: str = "",
    force: bool = False,
    engine: str = "auto",
    **_kw,
):
    """Fill the local word-level transcript cache for a media-bin file."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.speech.cache import DEFAULT_MODEL_ID
    from classes.speech.map_timeline import resolve_media_path

    fileId = _first_nonempty(fileId, _kw.get("file_id"))
    force = _as_bool(force, False)

    if not fileId:
        return ToolReceipt.refused(
            "transcribe_media_tool", "Error: fileId is required.",
        ).to_json()

    fobj = _file_for_id(str(fileId))
    if fobj is None or not isinstance(getattr(fobj, "data", None), dict):
        return ToolReceipt.refused(
            "transcribe_media_tool", f"Error: Unknown fileId '{fileId}'.",
        ).to_json()

    path = resolve_media_path(fobj.data)
    if not path:
        return ToolReceipt.refused(
            "transcribe_media_tool", "Error: File has no media path.",
        ).to_json()

    model = (modelId or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    try:
        record = _ensure_transcript(
            path,
            language=language or "auto",
            model_id=model,
            force=bool(force),
            engine=engine or "auto",
        )
    except InterruptedError:
        return ToolReceipt.error(
            "transcribe_media_tool", "Error: Transcription cancelled.",
        ).to_json()
    except Exception as exc:
        if _is_no_audio_exc(exc):
            return _no_audio_receipt("transcribe_media_tool").to_json()
        return ToolReceipt.error(
            "transcribe_media_tool", f"Error: Transcription failed: {exc}",
        ).to_json()

    from classes.agent_tools.present import words_to_script
    script = words_to_script(record.words)
    summary = (
        _short_transcript_summary(
            word_count=len(record.words),
            source=record.transcriptionSource,
            language=record.language,
            file_ids=[str(fileId)],
        )
        if record.words
        else "No spoken dialogue found."
    )
    return ToolReceipt.applied(
        "transcribe_media_tool",
        summary,
        undo_steps=0,
        notes=[
            "Show data.script to the user — never paste this receipt JSON. "
            "Do not re-call this tool; use get_transcript_tool / remove_words_tool next.",
        ],
        data={
            "fileId": str(fileId),
            "wordCount": len(record.words),
            "language": record.language,
            "modelId": record.modelId,
            "transcriptionSource": record.transcriptionSource,
            "transcriptGeneration": record.generation,
            "engine": record.transcriptionSource,
            "script": script,
        },
    ).to_json()


def get_transcript(
    clipId: str = "",
    fileId: str = "",
    trackIndex=None,
    language: str = "auto",
    modelId: str = "",
    force: bool = False,
    engine: str = "auto",
    detail_level: str = "",
    includeWords=None,
    **_kw,
):
    """Return spoken words in project frames for a clip, file, or whole track."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.inspect_render import snapshot_project
    from classes.speech.cache import DEFAULT_MODEL_ID
    from classes.speech.map_timeline import clip_speed, resolve_media_path, words_for_clip

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    fileId = _first_nonempty(fileId, _kw.get("file_id"))
    if trackIndex in (None, "") and _kw.get("track_index") not in (None, ""):
        trackIndex = _kw.get("track_index")
    force = _as_bool(force, False)
    include_words = _want_full_words(detail_level, includeWords, **_kw)
    include_compact = _want_compact_words(detail_level, includeWords, **_kw)

    try:
        from classes.app import get_app
        app = get_app()
    except Exception as exc:
        return ToolReceipt.error("get_transcript_tool", str(exc)).to_json()

    try:
        project_data = snapshot_project(app)
    except Exception as exc:
        return ToolReceipt.error(
            "get_transcript_tool", f"snapshot failed: {exc}",
        ).to_json()

    fps = _fps_from_project(app.project)
    model = (modelId or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    eng = engine or "auto"
    clips_out: list[dict] = []
    warnings: list[str] = []

    targets: list[dict] = []
    if clipId:
        clip = _find_clip(project_data, str(clipId))
        if clip is None:
            # Models often pass media-bin fileId as clipId after a file-only receipt.
            file_hits = _clips_for_file_id(project_data, str(clipId))
            if file_hits:
                targets = file_hits
            else:
                fobj = _file_for_id(str(clipId))
                if fobj is not None and isinstance(getattr(fobj, "data", None), dict):
                    # Treat as fileId — same path as explicit fileId=.
                    fileId = str(clipId)
                    clipId = ""
                else:
                    return ToolReceipt.refused(
                        "get_transcript_tool", f"Error: Unknown clipId '{clipId}'.",
                    ).to_json()
        else:
            targets = [clip]
    if fileId and not targets:
        # Synthetic single-file view (source seconds == timeline at 0).
        fobj = _file_for_id(str(fileId))
        if fobj is None or not isinstance(getattr(fobj, "data", None), dict):
            return ToolReceipt.refused(
                "get_transcript_tool", f"Error: Unknown fileId '{fileId}'.",
            ).to_json()
        path = resolve_media_path(fobj.data)
        if not path:
            return ToolReceipt.refused(
                "get_transcript_tool", "Error: File has no media path.",
            ).to_json()
        try:
            record = _ensure_transcript(
                path, language=language or "auto", model_id=model,
                force=bool(force), engine=eng,
            )
        except InterruptedError:
            return ToolReceipt.error(
                "get_transcript_tool", "Error: Transcription cancelled.",
            ).to_json()
        except Exception as exc:
            if _is_no_audio_exc(exc):
                return _no_audio_receipt().to_json()
            return ToolReceipt.error(
                "get_transcript_tool", f"Error: Transcription failed: {exc}",
            ).to_json()
        words = _words_payload(record.words, fps, generation=record.generation)
        return _file_transcript_receipt(
            file_id=str(fileId), words=words, record=record,
            include_words=include_words,
            include_compact=include_compact,
        )

    if not targets and not fileId and not clipId:
        # All clips, optionally filtered by UI track index.
        from classes.track_display import layer_number_to_display_index
        layers = project_data.get("layers") or []
        for clip in project_data.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            if trackIndex is not None:
                try:
                    want = int(trackIndex)
                except (TypeError, ValueError):
                    return ToolReceipt.refused(
                        "get_transcript_tool", "Error: trackIndex must be an integer.",
                    ).to_json()
                layer = int(clip.get("layer", -1))
                display = layer_number_to_display_index(layer, layers)
                if display != want:
                    continue
            targets.append(clip)

    # Imports-only: timeline empty → transcribe media-bin video/audio files.
    if not targets and not fileId and not clipId:
        from classes.query import File
        media_files = []
        try:
            media_files = list(File.filter() or [])
        except Exception:
            media_files = []
        usable = []
        for fobj in media_files:
            data = getattr(fobj, "data", None)
            if not isinstance(data, dict):
                continue
            path = resolve_media_path(data)
            if not path or not os.path.isfile(path):
                continue
            media_type = str(data.get("media_type") or "").lower()
            if media_type in ("image", "image/sequence", "font"):
                continue
            usable.append((str(data.get("id") or getattr(fobj, "id", "") or ""), path, data))
        if len(usable) == 1:
            only_id, only_path, _ = usable[0]
            try:
                record = _ensure_transcript(
                    only_path, language=language or "auto", model_id=model,
                    force=bool(force), engine=eng,
                )
            except InterruptedError:
                return ToolReceipt.error(
                    "get_transcript_tool", "Error: Transcription cancelled.",
                ).to_json()
            except Exception as exc:
                if _is_no_audio_exc(exc):
                    return _no_audio_receipt().to_json()
                return ToolReceipt.error(
                    "get_transcript_tool", f"Error: Transcription failed: {exc}",
                ).to_json()
            words = _words_payload(record.words, fps, generation=record.generation)
            return _file_transcript_receipt(
                file_id=only_id,
                words=words,
                record=record,
                include_words=include_words,
                include_compact=include_compact,
                warnings=[
                    "Timeline had no clips — transcribed the single media-bin file. "
                    "Place it on the timeline for clip-scoped word frames.",
                ],
                summary_suffix="(timeline was empty; transcribed the media-bin file.)",
            )
        if len(usable) > 1:
            ids = ", ".join(fid for fid, _, _ in usable[:8] if fid) or "unknown"
            return ToolReceipt.refused(
                "get_transcript_tool",
                "Error: Timeline is empty and multiple media-bin files exist. "
                f"Pass fileId for one of: {ids}",
            ).to_json()

    if not targets and not fileId:
        return ToolReceipt.refused(
            "get_transcript_tool",
            "Error: No clips to transcribe. Pass clipId/timeline_clip_id, "
            "fileId/file_id, put a clip on the timeline, or import media first.",
        ).to_json()

    generations: list[int] = []
    source = "local"
    language_out = ""
    model_out = model
    script_parts: list[dict] = []

    for clip in targets:
        cid = str(clip.get("id") or "")
        file_data = _file_data_for_clip(clip)
        path = resolve_media_path(file_data) if file_data else ""
        if not path:
            reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
            path = str(reader.get("path") or "")
        if not path:
            warnings.append(f"clip {cid}: no media path")
            continue
        try:
            record = _ensure_transcript(
                path, language=language or "auto", model_id=model,
                force=bool(force), engine=eng,
            )
        except InterruptedError:
            return ToolReceipt.error(
                "get_transcript_tool", "Error: Transcription cancelled.",
            ).to_json()
        except Exception as exc:
            if _is_no_audio_exc(exc):
                warnings.append(f"clip {cid}: no audio track")
            else:
                warnings.append(f"clip {cid}: {exc}")
            continue

        pos = float(clip.get("position") or 0)
        start = float(clip.get("start") or 0)
        end = float(clip.get("end") or start)
        mapped = words_for_clip(
            record.words,
            clip_id=cid,
            position=pos,
            start=start,
            end=end,
            fps=fps,
            speed=clip_speed(clip),
            generation=record.generation,
        )
        for w in mapped:
            if isinstance(w, dict) and w.get("text"):
                script_parts.append({"text": w["text"]})
        clips_out.append(
            _clip_transcript_payload(
                mapped,
                include_words=include_words,
                include_compact=include_compact,
                clipId=cid,
                fileId=str(clip.get("file_id") or ""),
                position=pos,
                start=start,
                end=end,
                transcriptGeneration=record.generation,
            )
        )
        generations.append(record.generation)
        source = record.transcriptionSource
        language_out = record.language
        model_out = record.modelId

    if not clips_out:
        if warnings and all(_is_no_audio_exc(w) or "no audio" in str(w).lower() for w in warnings):
            return _no_audio_receipt().to_json()
        return ToolReceipt.error(
            "get_transcript_tool",
            "Error: Could not transcribe any clip. " + "; ".join(warnings[:3]),
        ).to_json()

    from classes.agent_tools.present import words_to_script
    gen = max(generations) if generations else 1
    script = words_to_script(script_parts)
    word_count = sum(int(c.get("wordCount") or 0) for c in clips_out)
    clip_ids = [str(c.get("clipId") or "") for c in clips_out if c.get("clipId")]
    summary = (
        _short_transcript_summary(
            word_count=word_count,
            source=source,
            language=language_out,
            clip_ids=clip_ids,
        )
        if word_count
        else "No spoken dialogue found."
    )
    return ToolReceipt.applied(
        "get_transcript_tool",
        summary,
        undo_steps=0,
        warnings=warnings,
        notes=_transcript_notes(),
        data=_transcript_data_payload(
            script=script,
            word_count=word_count,
            clips_out=clips_out,
            source=source,
            model_out=model_out,
            language_out=language_out,
            generation=gen,
        ),
    ).to_json()


def _as_str_list(value):
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        out = [str(item).strip() for item in value if str(item).strip()]
        return out or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            return parts or None
        return _as_str_list(parsed)
    return [str(value).strip()] if str(value).strip() else None


def remove_words(
    clipId: str = "",
    wordIndices=None,
    matches=None,
    fillerPreset: str = "",
    transcriptGeneration=None,
    language: str = "auto",
    modelId: str = "",
    engine: str = "auto",
    **_kw,
):
    """Cut selected words out of a clip and close the gaps (one undo)."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.inspect_render import snapshot_project
    from classes.speech.cache import DEFAULT_MODEL_ID
    from classes.speech.map_timeline import clip_speed, resolve_media_path, words_for_clip
    from classes.speech.word_ranges import (
        FILLER_PRESETS,
        compact_fragments_after_remove,
        indices_for_filler_preset,
        indices_for_matches,
        ranges_for_word_indices,
    )
    from classes.tool_handlers import (
        _new_transaction_id,
    )

    clipId = _first_nonempty(clipId, _kw.get("timeline_clip_id"))
    wordIndices = _as_int_list(wordIndices if wordIndices not in (None, "") else _kw.get("word_indices"))
    matches = _as_str_list(matches if matches not in (None, "") else _kw.get("matchTexts") or _kw.get("match_texts"))
    fillerPreset = _first_nonempty(fillerPreset, _kw.get("filler_preset"))
    if transcriptGeneration in (None, "") and _kw.get("transcript_generation") not in (None, ""):
        transcriptGeneration = _kw.get("transcript_generation")
    warnings: list[str] = []

    try:
        from classes.app import get_app
        app = get_app()
    except Exception as exc:
        return ToolReceipt.error("remove_words_tool", str(exc)).to_json()

    try:
        project_data = snapshot_project(app)
    except Exception as exc:
        return ToolReceipt.error(
            "remove_words_tool", f"snapshot failed: {exc}",
        ).to_json()

    if not clipId:
        # Single-clip projects: allow matches-only calls without making the agent re-read.
        candidates = [
            c for c in (project_data.get("clips") or [])
            if isinstance(c, dict) and c.get("id")
        ]
        if len(candidates) == 1:
            clipId = str(candidates[0].get("id"))
        else:
            return ToolReceipt.refused(
                "remove_words_tool",
                "Error: clipId is required when the timeline has zero or multiple clips. "
                "Pass clipId from get_transcript_tool data.clipIds.",
            ).to_json()

    clip = _find_clip(project_data, str(clipId))
    if clip is None:
        # Agents often pass media-bin fileId after a file-only transcript receipt.
        file_hits = _clips_for_file_id(project_data, str(clipId))
        if len(file_hits) == 1:
            clip = file_hits[0]
            clipId = str(clip.get("id"))
        elif len(file_hits) > 1:
            ids = ", ".join(str(c.get("id")) for c in file_hits[:8])
            return ToolReceipt.refused(
                "remove_words_tool",
                f"Error: '{clipId}' is a media-bin fileId with multiple timeline clips. "
                f"Pass one timeline clipId: {ids}",
            ).to_json()
        else:
            fobj = _file_for_id(str(clipId))
            if fobj is not None:
                return ToolReceipt.refused(
                    "remove_words_tool",
                    f"Error: '{clipId}' is a media-bin fileId, not a timeline clipId. "
                    "Place the file on the timeline, call get_transcript_tool (no args), "
                    "then remove_words_tool with data.clipIds[0].",
                ).to_json()
            return ToolReceipt.refused(
                "remove_words_tool", f"Error: Unknown clipId '{clipId}'.",
            ).to_json()

    fps = _fps_from_project(app.project)
    model = (modelId or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    file_data = _file_data_for_clip(clip)
    path = resolve_media_path(file_data) if file_data else ""
    if not path:
        reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
        path = str(reader.get("path") or "")
    if not path:
        return ToolReceipt.refused(
            "remove_words_tool", "Error: Clip has no media path.",
        ).to_json()

    try:
        record = _ensure_transcript(
            path,
            language=language or "auto",
            model_id=model,
            force=False,
            engine=engine or "auto",
        )
    except Exception as exc:
        if _is_no_audio_exc(exc):
            return _no_audio_receipt("remove_words_tool").to_json()
        return ToolReceipt.error(
            "remove_words_tool", f"Error: Transcription failed: {exc}",
        ).to_json()

    if transcriptGeneration is not None:
        try:
            want = int(transcriptGeneration)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "remove_words_tool",
                "Error: transcriptGeneration must be an integer.",
            ).to_json()
        if want != int(record.generation):
            # matches / fillerPreset recompute indices from the live record.
            # wordIndices win over both below, so stale ones always refuse.
            if wordIndices is not None:
                return ToolReceipt.refused(
                    "remove_words_tool",
                    "Error: transcriptGeneration mismatch — call get_transcript_tool again.",
                    data={
                        "transcriptGeneration": record.generation,
                        "requested": want,
                    },
                ).to_json()
            warnings.append(
                f"transcriptGeneration {want} stale; using live generation "
                f"{record.generation}"
            )

    from classes.speech.map_timeline import is_retimed
    if is_retimed(clip):
        # Fragments are packed in source seconds; on a retimed clip that
        # spaces them (and ripples later clips) wrongly.
        return ToolReceipt.refused(
            "remove_words_tool",
            "Error: this clip has a speed change or time curve; reset its speed "
            "(Time > Normal) before removing words.",
        ).to_json()
    pos = float(clip.get("position") or 0)
    start = float(clip.get("start") or 0)
    end = float(clip.get("end") or start)
    mapped = words_for_clip(
        record.words,
        clip_id=str(clipId),
        position=pos,
        start=start,
        end=end,
        fps=fps,
        speed=clip_speed(clip),
        generation=record.generation,
    )

    indices: list[int] = []
    if fillerPreset:
        if fillerPreset not in FILLER_PRESETS:
            return ToolReceipt.refused(
                "remove_words_tool",
                f"Error: Unknown fillerPreset '{fillerPreset}'. "
                f"Use one of: {', '.join(sorted(FILLER_PRESETS))}.",
            ).to_json()
        indices = indices_for_filler_preset(mapped, fillerPreset)
    if matches is not None:
        matched = indices_for_matches(mapped, matches)
        # Union with any preset hits; wordIndices below replace when provided.
        indices = sorted(set(indices) | set(matched))
    if wordIndices is not None:
        try:
            indices = [int(i) for i in list(wordIndices)]
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "remove_words_tool", "Error: wordIndices must be integers.",
            ).to_json()

    if not indices:
        hint = ""
        if matches:
            hint = f" (no transcript words matched {matches!r})"
        return ToolReceipt.unchanged(
            "remove_words_tool",
            f"No words matched — nothing removed.{hint}",
            data={"transcriptGeneration": record.generation, "matches": matches or []},
            warnings=warnings,
        ).to_json()

    ranges = ranges_for_word_indices(mapped, indices, fps=fps)
    if not ranges:
        return ToolReceipt.unchanged(
            "remove_words_tool",
            "No removable ranges — nothing removed.",
            data={"transcriptGeneration": record.generation},
            warnings=warnings,
        ).to_json()

    fragments, removed = compact_fragments_after_remove(
        position=pos,
        start=start,
        end=end,
        remove_ranges=ranges,
        fps=fps,
    )
    if removed <= 0 and len(fragments) == 1:
        fs, fe = fragments[0][1], fragments[0][2]
        if abs(fs - start) < 1e-9 and abs(fe - end) < 1e-9:
            return ToolReceipt.unchanged(
                "remove_words_tool",
                "Selection produced no change.",
                data={"transcriptGeneration": record.generation},
                warnings=warnings,
            ).to_json()

    if not fragments:
        # Removing the entire clip content → delete the clip + ripple.
        return _delete_clip_and_ripple(
            app, clip, removed_duration=end - start, fps=fps,
            word_count=len(indices), generation=record.generation,
        )

    tid = _new_transaction_id()
    try:
        created_ids, shifted = apply_compacted_fragments(
            app,
            clip_id=str(clipId),
            fragments=fragments,
            removed=removed,
            fps=fps,
            tid=tid,
        )
    except Exception as exc:
        return ToolReceipt.error(
            "remove_words_tool", f"Error: {exc}",
        ).to_json()

    # Remaining dialogue after the cut — so the agent need not re-call
    # get_transcript (and invent a truncated script from the pre-cut receipt).
    kept_words = []
    remove_set = set(indices)
    for w in mapped:
        try:
            idx = int(w.get("index"))
        except (TypeError, ValueError):
            continue
        if idx in remove_set:
            continue
        text = str(w.get("text") or "").strip()
        if text:
            kept_words.append({"text": text})
    from classes.agent_tools.present import words_to_script
    script = words_to_script(kept_words)

    return ToolReceipt.applied(
        "remove_words_tool",
        f"Removed {len(indices)} word(s), closed {removed:.2f}s.",
        undo_steps=1,
        shifted=shifted,
        warnings=warnings,
        data={
            "clipId": str(clipId),
            "createdClipIds": created_ids,
            "removedWordIndices": indices,
            "removedDurationSec": removed,
            "fragmentCount": len(fragments),
            "transcriptGeneration": record.generation,
            "transcriptionSource": record.transcriptionSource,
            "modelId": record.modelId,
            "language": record.language,
            "script": script,
            "wordCount": len(kept_words),
        },
    ).to_json()


def apply_compacted_fragments(
    app,
    *,
    clip_id: str,
    fragments: list[tuple[float, float, float]],
    removed: float,
    fps,
    tid: str,
) -> tuple[list[str], list[dict]]:
    """Apply packed fragments to the live timeline (main-thread)."""
    from classes.tool_handlers import _run_on_main_thread, _transaction

    created_ids: list[str] = []
    shifted: list[dict] = []
    error_box: list[Optional[str]] = [None]

    def _mutate():
        try:
            from classes.query import Clip
            from classes.frame_time import snap
            original = Clip.get(id=str(clip_id))
            if original is None:
                error_box[0] = f"Unknown clipId '{clip_id}'"
                return
            layer = int(original.data.get("layer", 0))
            original_end_tl = float(original.data.get("position") or 0) + max(
                0.0,
                float(original.data.get("end") or 0)
                - float(original.data.get("start") or 0),
            )
            with _transaction(app, tid=tid):
                first = fragments[0]
                original.data["position"] = first[0]
                original.data["start"] = first[1]
                original.data["end"] = first[2]
                original.data["duration"] = max(0.0, first[2] - first[1])
                original.save()

                for frag in fragments[1:]:
                    data = copy.deepcopy(original.data)
                    data.pop("id", None)
                    data.pop("ai_metadata", None)
                    data["position"] = frag[0]
                    data["start"] = frag[1]
                    data["end"] = frag[2]
                    data["duration"] = max(0.0, frag[2] - frag[1])
                    new_clip = Clip()
                    new_clip.id = None
                    new_clip.type = "insert"
                    new_clip.data = data
                    key = list(original.key)
                    if len(key) > 1:
                        key = key[:1]
                    new_clip.key = key
                    new_clip.save()
                    created_ids.append(str(new_clip.id))

                if removed > 1e-9:
                    gap = snap(float(removed), fps)
                    for other in Clip.filter(layer=layer):
                        if other is None:
                            continue
                        oid = str(other.id)
                        if oid == str(clip_id) or oid in created_ids:
                            continue
                        opos = float(other.data.get("position") or 0)
                        if opos + 1e-9 >= original_end_tl:
                            other.data["position"] = snap(opos - gap, fps)
                            other.save()
                            shifted.append({"clipId": oid, "deltaSec": -gap})
        except Exception as exc:
            error_box[0] = str(exc)

    _run_on_main_thread(_mutate)
    if error_box[0]:
        raise RuntimeError(error_box[0])
    return created_ids, shifted


def _delete_clip_and_ripple(app, clip: dict, *, removed_duration: float, fps, word_count: int, generation: int):
    from classes.agent_tools.receipt import ToolReceipt
    from classes.tool_handlers import _new_transaction_id, _run_on_main_thread, _transaction
    from classes.frame_time import snap

    tid = _new_transaction_id()
    cid = str(clip.get("id"))
    error_box: list[Optional[str]] = [None]
    shifted: list[dict] = []

    def _mutate():
        try:
            from classes.query import Clip
            obj = Clip.get(id=cid)
            if obj is None:
                error_box[0] = f"Unknown clipId '{cid}'"
                return
            layer = int(obj.data.get("layer", 0))
            pos = float(obj.data.get("position") or 0)
            with _transaction(app, tid=tid):
                obj.delete()
                gap = snap(float(removed_duration), fps)
                if gap > 1e-9:
                    for other in Clip.filter(layer=layer):
                        if other is None:
                            continue
                        opos = float(other.data.get("position") or 0)
                        if opos + 1e-9 >= pos:
                            other.data["position"] = snap(opos - gap, fps)
                            other.save()
                            shifted.append({"clipId": str(other.id), "deltaSec": -gap})
        except Exception as exc:
            error_box[0] = str(exc)

    try:
        _run_on_main_thread(_mutate)
    except Exception as exc:
        return ToolReceipt.error("remove_words_tool", f"Error: {exc}").to_json()
    if error_box[0]:
        return ToolReceipt.error("remove_words_tool", f"Error: {error_box[0]}").to_json()

    return ToolReceipt.applied(
        "remove_words_tool",
        f"Removed {word_count} word(s) (entire clip).",
        undo_steps=1,
        removedClipIds=[cid],
        shifted=shifted,
        data={
            "clipId": cid,
            "removedDurationSec": removed_duration,
            "transcriptGeneration": generation,
        },
    ).to_json()
