"""JSON-only visibility of clips at a project time (no libopenshot)."""

from __future__ import annotations

from typing import Any, Optional


def _clip_dict(clip: Any) -> dict:
    if isinstance(clip, dict):
        return clip
    data = getattr(clip, "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _layer_hidden(layers: list, layer_number: int) -> bool:
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        try:
            num = int(layer.get("number"))
        except (TypeError, ValueError):
            continue
        if num == layer_number:
            return bool(layer.get("y", 1) == 0) or bool(layer.get("hidden"))
    return False


def _is_audio_only(clip: dict) -> bool:
    reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else {}
    if reader.get("has_video") is False:
        return True
    if clip.get("has_video") is False:
        return True
    if str(clip.get("id_type") or "").lower() == "audio":
        return True
    return False


def visible_clips_at(project_data: dict, *, seconds: float, fps=None) -> list[dict]:
    from classes.frame_time import to_frame

    clips = list(project_data.get("clips") or [])
    layers = list(project_data.get("layers") or [])
    t = float(seconds)
    visible: list[tuple[int, dict]] = []

    for raw in clips:
        clip = _clip_dict(raw)
        if not clip:
            continue
        try:
            layer = int(clip.get("layer") if clip.get("layer") is not None else 0)
        except (TypeError, ValueError):
            layer = 0
        if _layer_hidden(layers, layer):
            continue
        if _is_audio_only(clip):
            continue
        try:
            position = float(clip.get("position") or 0)
            start = float(clip.get("start") or 0)
            end = float(clip.get("end") if clip.get("end") is not None else start)
        except (TypeError, ValueError):
            continue
        duration = end - start
        if duration < 0:
            continue
        if not (position <= t < position + max(duration, 1e-9)):
            continue
        entry: dict[str, Any] = {
            "id": str(clip.get("id") or ""),
            "track": layer,
            "position": position,
            "start": start,
            "end": end,
        }
        if fps is not None:
            try:
                entry["landedFrame"] = to_frame(position, fps)
            except Exception:
                pass
        for key in ("scale_x", "scale_y", "location_x", "location_y", "rotation"):
            val = clip.get(key)
            if val is not None and not isinstance(val, dict):
                try:
                    entry[key] = float(val)
                except (TypeError, ValueError):
                    pass
        effects = clip.get("effects")
        if isinstance(effects, list):
            names = []
            for eff in effects:
                if isinstance(eff, dict):
                    name = eff.get("class_name") or eff.get("type") or eff.get("name")
                    if name:
                        names.append(str(name))
            if names:
                entry["effects"] = names
        visible.append((layer, entry))

    visible.sort(key=lambda pair: pair[0], reverse=True)
    return [e for _, e in visible]


def mid_bin_frames(start: int, end: int, count: int) -> list[int]:
    start_i = int(start)
    end_i = int(end)
    span = end_i - start_i
    if span <= 0:
        return []
    n = max(1, min(int(count), span))
    return [start_i + int((span * (i + 0.5) / n)) for i in range(n)]


def mid_bin_seconds(start: float, end: float, count: int) -> list[float]:
    span = float(end) - float(start)
    if span <= 0:
        return []
    n = max(1, int(count))
    return [float(start) + span * (i + 0.5) / n for i in range(n)]
