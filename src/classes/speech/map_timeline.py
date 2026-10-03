"""Map source-media word timings onto project timeline frames."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from classes.frame_time import to_frame, to_seconds
from classes.speech.cache import Word


def clip_speed(clip: dict) -> float:
    """Playback speed multiplier; 1.0 when unset / invalid."""
    for key in ("time", "speed", "reader"):
        raw = clip.get(key)
        if key == "reader" and isinstance(raw, dict):
            raw = raw.get("duration")  # not speed
            continue
        try:
            if raw is None:
                continue
            # OpenShot stores time curve; simple scalar speed key if present.
            if isinstance(raw, (int, float)) and float(raw) > 0:
                return float(raw)
        except (TypeError, ValueError):
            continue
    # Common Zenvi/OpenShot: no speed field ⇒ 1.0
    speed = clip.get("speed")
    try:
        if speed is not None and float(speed) > 0:
            return float(speed)
    except (TypeError, ValueError):
        pass
    return 1.0


def is_retimed(clip: dict) -> bool:
    """True when the clip plays at another speed or through a time curve."""
    from classes.export_acceleration.smart_render import _time_curve_is_identity

    return abs(clip_speed(clip) - 1.0) > 1e-9 or not _time_curve_is_identity(clip.get("time"))


def source_to_timeline_sec(
    source_sec: float,
    *,
    position: float,
    start: float,
    speed: float = 1.0,
) -> float:
    rate = float(speed) if speed and float(speed) > 0 else 1.0
    return float(position) + (float(source_sec) - float(start)) / rate


def words_for_clip(
    words: Sequence[Word],
    *,
    clip_id: str,
    position: float,
    start: float,
    end: float,
    fps,
    speed: float = 1.0,
    generation: int,
) -> list[dict[str, Any]]:
    """Words overlapping the clip's source trim, with project frame fields.

    Each row: ``[index, text, startFrame]``-friendly dict plus source times.
    A word runs until the next word's startFrame (Palmier compact form); we
    also expose endFrame for remove_words.
    """
    rate = float(speed) if speed and float(speed) > 0 else 1.0
    out: list[dict[str, Any]] = []
    idx = 0
    for w in words:
        # Keep words that overlap the trimmed source window.
        if w.endSec <= start or w.startSec >= end:
            continue
        src_s = max(float(start), float(w.startSec))
        src_e = min(float(end), float(w.endSec))
        if src_e <= src_s:
            continue
        tl_s = source_to_timeline_sec(src_s, position=position, start=start, speed=rate)
        tl_e = source_to_timeline_sec(src_e, position=position, start=start, speed=rate)
        start_f = to_frame(tl_s, fps)
        end_f = to_frame(tl_e, fps)
        if end_f <= start_f:
            end_f = start_f + 1
        row = {
            "index": idx,
            "text": w.text,
            "startFrame": start_f,
            "endFrame": end_f,
            "startSec": src_s,
            "endSec": src_e,
            "timelineStartSec": to_seconds(start_f, fps),
            "timelineEndSec": to_seconds(end_f, fps),
            "clipId": clip_id,
            "transcriptGeneration": int(generation),
        }
        if w.confidence is not None:
            row["confidence"] = w.confidence
        if w.speakerId is not None:
            row["speakerId"] = w.speakerId
        out.append(row)
        idx += 1
    return out


def resolve_media_path(file_data: Optional[dict]) -> str:
    if not isinstance(file_data, dict):
        return ""
    path = file_data.get("path") or ""
    if path:
        return str(path)
    reader = file_data.get("reader") if isinstance(file_data.get("reader"), dict) else {}
    return str(reader.get("path") or "")
