"""get_transcript_tool / remove_words_tool / transcribe_media_tool (Phase 5)."""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

log = logging.getLogger("agent_tools.transcript")


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
        return ToolReceipt.error(
            "transcribe_media_tool", f"Error: Transcription failed: {exc}",
        ).to_json()

    return ToolReceipt.applied(
        "transcribe_media_tool",
        f"Transcribed {len(record.words)} words ({record.language}).",
        undo_steps=0,
        data={
            "fileId": str(fileId),
            "wordCount": len(record.words),
            "language": record.language,
            "modelId": record.modelId,
            "transcriptionSource": record.transcriptionSource,
            "transcriptGeneration": record.generation,
            "engine": record.transcriptionSource,
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
    **_kw,
):
    """Return spoken words in project frames for a clip, file, or whole track."""
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.inspect_render import snapshot_project
    from classes.speech.cache import DEFAULT_MODEL_ID
    from classes.speech.map_timeline import clip_speed, resolve_media_path, words_for_clip

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
            return ToolReceipt.refused(
                "get_transcript_tool", f"Error: Unknown clipId '{clipId}'.",
            ).to_json()
        targets = [clip]
    elif fileId:
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
            return ToolReceipt.error(
                "get_transcript_tool", f"Error: Transcription failed: {exc}",
            ).to_json()
        from classes.frame_time import to_frame
        words = []
        for i, w in enumerate(record.words):
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
                "transcriptGeneration": record.generation,
            })
        return ToolReceipt.applied(
            "get_transcript_tool",
            f"{len(words)} words from file.",
            undo_steps=0,
            data={
                "clips": [{
                    "fileId": str(fileId),
                    "words": words,
                    "transcriptGeneration": record.generation,
                }],
                "transcriptionSource": record.transcriptionSource,
                "modelId": record.modelId,
                "language": record.language,
                "transcriptGeneration": record.generation,
                "engine": record.transcriptionSource,
            },
        ).to_json()
    else:
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

    if not targets and not fileId:
        return ToolReceipt.refused(
            "get_transcript_tool",
            "Error: No clips to transcribe. Pass clipId, fileId, or put clips on the timeline.",
        ).to_json()

    generations: list[int] = []
    source = "local"
    language_out = ""
    model_out = model

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
        clips_out.append({
            "clipId": cid,
            "fileId": str(clip.get("file_id") or ""),
            "position": pos,
            "start": start,
            "end": end,
            "words": mapped,
            "transcriptGeneration": record.generation,
            "compactWords": [
                [w["index"], w["text"], w["startFrame"]] for w in mapped
            ],
        })
        generations.append(record.generation)
        source = record.transcriptionSource
        language_out = record.language
        model_out = record.modelId

    if not clips_out:
        return ToolReceipt.error(
            "get_transcript_tool",
            "Error: Could not transcribe any clip. " + "; ".join(warnings[:3]),
        ).to_json()

    # One generation pin for the whole receipt (max across clips).
    gen = max(generations) if generations else 1
    word_count = sum(len(c["words"]) for c in clips_out)
    return ToolReceipt.applied(
        "get_transcript_tool",
        f"{word_count} words across {len(clips_out)} clip(s).",
        undo_steps=0,
        warnings=warnings,
        data={
            "clips": clips_out,
            "transcriptionSource": source,
            "modelId": model_out,
            "language": language_out,
            "transcriptGeneration": gen,
            "engine": source,
        },
    ).to_json()


def remove_words(
    clipId: str = "",
    wordIndices=None,
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
        ranges_for_word_indices,
    )
    from classes.tool_handlers import (
        _new_transaction_id,
    )

    if not clipId:
        return ToolReceipt.refused(
            "remove_words_tool", "Error: clipId is required.",
        ).to_json()

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

    clip = _find_clip(project_data, str(clipId))
    if clip is None:
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
            return ToolReceipt.refused(
                "remove_words_tool",
                "Error: transcriptGeneration mismatch — call get_transcript_tool again.",
                data={
                    "transcriptGeneration": record.generation,
                    "requested": want,
                },
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
    if wordIndices is not None:
        try:
            indices = [int(i) for i in list(wordIndices)]
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "remove_words_tool", "Error: wordIndices must be integers.",
            ).to_json()

    if not indices:
        return ToolReceipt.unchanged(
            "remove_words_tool",
            "No words matched — nothing removed.",
            data={"transcriptGeneration": record.generation},
        ).to_json()

    ranges = ranges_for_word_indices(mapped, indices, fps=fps)
    if not ranges:
        return ToolReceipt.unchanged(
            "remove_words_tool",
            "No removable ranges — nothing removed.",
            data={"transcriptGeneration": record.generation},
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

    return ToolReceipt.applied(
        "remove_words_tool",
        f"Removed {len(indices)} word(s), closed {removed:.2f}s.",
        undo_steps=1,
        shifted=shifted,
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
