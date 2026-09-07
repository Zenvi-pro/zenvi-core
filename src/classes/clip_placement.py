"""Pure helpers for clip placement trim + underlay defaults (no Qt deps)."""

from __future__ import annotations

WATCH_MAX_WINDOW_SEC = 45.0
DEFAULT_WATCH_QUERY = "the main visible action in this clip"
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"})


def source_window_for_file(file_data, *, eps: float = 1e-3) -> tuple:
    """Prefer file start/end over parent duration (subclips store both)."""
    data = file_data if isinstance(file_data, dict) else {}
    f_start = float(data.get("start", 0.0) or 0.0)
    f_end = float(data.get("end", 0.0) or 0.0)
    if f_end > f_start + eps:
        return f_start, f_end
    try:
        file_dur = float(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        file_dur = 0.0
    if file_dur <= f_start + eps:
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        try:
            file_dur = float(reader.get("duration") or 0)
        except (TypeError, ValueError):
            file_dur = 0.0
    if file_dur > f_start + eps:
        return f_start, file_dur
    return f_start, f_start + 0.1


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


def file_looks_like_image(file_data) -> bool:
    data = file_data if isinstance(file_data, dict) else {}
    if str(data.get("media_type") or "").lower() == "image":
        return True
    path = str(data.get("path") or data.get("name") or "")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in _IMAGE_EXTS


def _basename_stem(name: str) -> str:
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.replace("_", " ").replace("-", " ").strip()


def placement_watch_query(file_data, query="", *, extra="") -> str:
    """Query for vision-watch before place. Indexing is not required."""
    q = str(query or "").strip()
    if q:
        return q[:200]
    data = file_data if isinstance(file_data, dict) else {}
    ai = data.get("ai_metadata") if isinstance(data.get("ai_metadata"), dict) else {}
    for key in ("prompt", "short_summary", "description"):
        text = str(ai.get(key) or "").strip()
        if text:
            return text[:200]
    extra_s = str(extra or "").replace("_", " ").strip()
    if extra_s:
        return extra_s[:200]
    tags = data.get("tags")
    if isinstance(tags, str):
        tag_s = tags.replace(",", " ").strip()
    elif isinstance(tags, list):
        tag_s = " ".join(str(t) for t in tags if str(t).strip()).strip()
    else:
        tag_s = ""
    if tag_s:
        return tag_s[:200]
    stem = _basename_stem(str(data.get("name") or data.get("path") or ""))
    if stem:
        return stem[:200]
    return DEFAULT_WATCH_QUERY


def should_watch_placement(
    *,
    is_audio: bool = False,
    is_image: bool = False,
    skip_explicit_times: bool = False,
    is_already_watched_subclip: bool = False,
    explicit_query: bool = False,
    window_sec: float = 0.0,
    max_window_sec: float = WATCH_MAX_WINDOW_SEC,
) -> bool:
    """Watch a bounded candidate window before place (AI gen, MG, stock, short footage).

    Skip audio/images, explicit 'from Xs to Ys', untrimmed long files, and
    place_moment subclips that were already watched unless a new query is given.
    """
    if is_audio or is_image or skip_explicit_times:
        return False
    if is_already_watched_subclip and not explicit_query:
        return False
    try:
        span = float(window_sec or 0.0)
    except (TypeError, ValueError):
        span = 0.0
    return 1e-3 < span <= float(max_window_sec) + 1e-6
