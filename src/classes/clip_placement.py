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


def default_underlay_layer_number(layers) -> int:
    """Lowest layer_number (bottom underlay). Used when track= is omitted."""
    if not layers:
        return 1
    return int(min(layers, key=lambda l: l.get("number", 0)).get("number", 1))


def apply_audio_only_clip_overrides(clip_data, file_data, *, constant_interpolation, scale_none) -> bool:
    """Stop an audio-only file from compositing an (often cover-art) video frame.

    Same override the Split Audio menu applies — see Split_Audio_Triggered and
    https://github.com/OpenShot/openshot-qt/issues/2882. Returns True when the
    clip was audio-only and got the overrides.
    """
    from classes.image_types import is_audio_only_media

    if not isinstance(clip_data, dict) or not is_audio_only_media(file_data):
        return False
    clip_data["has_video"] = {
        "Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": int(constant_interpolation)}]
    }
    clip_data["scale"] = scale_none
    reader = clip_data.get("reader")
    if isinstance(reader, dict):
        reader["has_video"] = False
    return True


def repair_audio_only_project_data(data, *, constant_interpolation, scale_none) -> int:
    """Clear has_video on audio-only files and every clip that reads them.

    Projects saved before the cover-art fix carry has_video=True on the file and
    on clips already placed, so reopening one still blacks out lower layers.
    Returns the number of clips repaired.
    """
    from classes.image_types import is_audio_only_media

    if not isinstance(data, dict):
        return 0

    audio_files = {}
    for file_data in data.get("files") or []:
        if not isinstance(file_data, dict) or not is_audio_only_media(file_data):
            continue
        file_data["has_video"] = False
        audio_files[str(file_data.get("id") or "")] = file_data
    if not audio_files:
        return 0

    repaired = 0
    for clip_data in data.get("clips") or []:
        if not isinstance(clip_data, dict):
            continue
        reader = clip_data.get("reader")
        reader = reader if isinstance(reader, dict) else {}
        file_data = audio_files.get(str(clip_data.get("file_id") or reader.get("id") or ""))
        if file_data and apply_audio_only_clip_overrides(
            clip_data, file_data,
            constant_interpolation=constant_interpolation, scale_none=scale_none,
        ):
            repaired += 1
    return repaired


def _seconds_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_SECONDS_SUFFIXES = ("seconds", "second", "secs", "sec", "s")


def parse_timecode_token(token: object) -> float | None:
    """Seconds for 'SS', 'M:SS' or 'H:M:SS' tokens, else None."""
    if isinstance(token, (int, float)):
        return _seconds_float(token)
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None
    if ":" not in token:
        return _seconds_float(token)
    parts = token.split(":")
    if len(parts) > 3:
        return None
    nums = []
    for part in parts:
        value = _seconds_float(part)
        if value is None:
            return None
        nums.append(value)
    if len(nums) == 2:
        return nums[0] * 60.0 + nums[1]
    return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]


def parse_seconds_arg(value: object, *, default: float | None = None, field: str = "") -> float | None:
    """Parse an agent-supplied time argument into seconds.

    Blank means "not supplied" and yields *default*. Anything that is not a time
    raises ValueError naming the raw value, so callers can answer with a usable
    "Error: ..." instead of a bare `could not convert string to float`.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field or 'value'}={value!r} is not a time in seconds")
    if isinstance(value, (int, float)):
        parsed = _seconds_float(value)
        if parsed is None:
            raise ValueError(f"{field or 'value'}={value!r} is not a time in seconds")
        return parsed
    text = str(value).strip()
    if not text:
        return default
    lowered = text.lower().replace(",", "")
    for suffix in _SECONDS_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            lowered = lowered[: -len(suffix)].strip()
            break
    parsed = parse_timecode_token(lowered)
    if parsed is None:
        raise ValueError(
            f"{field or 'value'}={value!r} is not a time in seconds "
            "(use seconds like 12 or 12.5, or a timecode like 0:12)"
        )
    return parsed


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
