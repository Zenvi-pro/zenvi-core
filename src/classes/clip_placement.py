"""Pure helpers for clip placement trim + underlay defaults (no Qt deps)."""

from __future__ import annotations


def compute_clip_trim_bounds(
    source_len: float,
    *,
    trim_start: float = 0.0,
    trim_dur: float,
    file_start: float = 0.0,
    min_duration: float = 1.0 / 30.0,
) -> tuple:
    """
    Source-relative start/end for a placed clip trimmed to trim_dur seconds.
    Returns (start_sec, end_sec) with end-start ≈ trim_dur (clamped to source).
    """
    source_len = max(0.0, float(source_len or 0.0))
    trim_start = max(0.0, float(trim_start or 0.0))
    trim_dur = max(0.0, float(trim_dur or 0.0))
    file_start = float(file_start or 0.0)
    start_sec = file_start + trim_start
    if source_len > 0:
        max_end = file_start + source_len
        if start_sec > max_end:
            start_sec = max_end
        end_sec = min(start_sec + trim_dur, max_end)
    else:
        end_sec = start_sec + trim_dur
    if end_sec <= start_sec:
        end_sec = start_sec + max(min_duration, 1e-3)
    return start_sec, end_sec


def default_underlay_layer_number(layers, *, audio: bool = False) -> int:
    """Lowest layer_number (bottom underlay). Used when track= is omitted."""
    del audio
    if not layers:
        return 1
    return int(min(layers, key=lambda l: l.get("number", 0)).get("number", 1))
