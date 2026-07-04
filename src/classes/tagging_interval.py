"""Tagging frame interval logic (mirrors zenvi-backend gemini_provider.get_tagging_interval)."""

MAX_FRAMES = 30
MAX_CLIP_SECONDS = 30 * 60


def get_tagging_interval(duration_seconds: float) -> tuple:
    """Return (frame_interval_sec, max_frames) based on clip duration."""
    if duration_seconds > MAX_CLIP_SECONDS:
        raise ValueError(
            f"Clip duration {duration_seconds / 60:.1f} min exceeds the "
            f"30-minute tagging limit."
        )
    if duration_seconds <= 60:
        interval, cap = 2.0, 30
    elif duration_seconds <= 300:
        interval, cap = 20.0, 30
    elif duration_seconds <= 600:
        interval, cap = 30.0, 20
    else:
        interval, cap = 60.0, 30
    return interval, min(cap, MAX_FRAMES)
