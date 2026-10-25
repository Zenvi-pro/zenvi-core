"""Keep playback smooth after a big edit: build small preview copies of the heavy footage a timeline now uses, and say when other tracks are
still being drawn underneath. Measured on a real 80-cut timeline (720p project, phone footage): playing the originals, 7% of frames missed the
33 ms budget and the worst took 470 ms (the first frame after a cut seeks into a long-GOP file); with the editor's own optimized copies
2% missed and the worst took 165 ms. Exports always use the originals. Workstream: media-files.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from classes.editor_tools._base import get_app, on_main
from classes.logger import log

HEAVY_HEIGHT = 721            # a video taller than the preview copy (720p) is worth a copy
HEAVY_CODECS = ("hevc", "h265", "prores", "av1", "vp9")
MIN_CLIPS = 12                # fewer clips than this and the originals play fine
HOP_TIMEOUT = 60


def _is_heavy(data: Dict[str, Any]) -> bool:
    reader = data if isinstance(data, dict) else {}
    height = int(reader.get("height") or 0)
    codec = str(reader.get("vcodec") or reader.get("video_codec") or "").lower()
    return height >= HEAVY_HEIGHT or codec in HEAVY_CODECS


def start_preview_copies(file_ids: Iterable[str], clip_count: int) -> str:
    """Start (without waiting) optimized preview copies for the heavy video files among *file_ids*; returns a sentence for the tool's summary,
    or "" when nothing was needed or possible. It never raises: a failure here must not undo a good edit."""
    try:
        if clip_count < MIN_CLIPS:
            return ""
        service = getattr(get_app().window, "proxy_service", None)
        if service is None or not hasattr(service, "create_for_files"):
            return ""
        from classes.editor_tools.media_files import ACTIVE_PROXY_STATES, _proxy_state, kind_of, media_missing
        from classes.query import File
        todo: List[Any] = []
        for file_id in dict.fromkeys(str(x) for x in file_ids):
            f = File.get(id=file_id)
            if f is None or kind_of(f.data) != "video" or media_missing(f.data) or not _is_heavy(f.data):
                continue
            if _proxy_state(f) in ACTIVE_PROXY_STATES + ("ready",):
                continue
            todo.append(f)
        if not todo:
            return ""
        on_main(service.create_for_files, todo, timeout=HOP_TIMEOUT, batch=False)
        return (f" Building smooth-playback copies of {len(todo)} heavy video(s) in the background (the preview plays them; export uses the originals);"
                f" progress: run_tool(name='manage_optimized_previews_tool', args={{'action': 'status'}}).")
    except Exception:  # noqa: BLE001 - playback copies are a courtesy
        log.warning("could not start preview copies", exc_info=True)
        return ""


def stacked_tracks_note(layer: int, start: float, end: float) -> str:
    """A sentence naming other tracks that still hold picture clips over [start, end): the player draws them all at once, which slows playback
    (an old attempt left under a new one doubles the work). "" when there are none."""
    try:
        from classes import audio_mix as am
        from classes.query import Clip, Track
        counts: Dict[int, int] = {}
        for c in Clip.filter():
            data = c.data or {}
            other = int(data.get("layer") or 0)
            reader = data.get("reader") or {}
            if other == layer or not reader.get("has_video", True) or reader.get("media_type") == "audio":
                continue
            lo, hi = am.clip_timeline_extent(data)
            if lo < end and hi > start:
                counts[other] = counts.get(other, 0) + 1
        if not counts:
            return ""
        label = {int((t.data or {}).get("number") or 0): str((t.data or {}).get("label") or "") for t in Track.filter()}
        parts = [f"{label.get(n) or 'track ' + str(n)} ({k} clip(s))" for n, k in sorted(counts.items())]
        return (" Other tracks still hold picture clips over this stretch and are drawn too, which slows playback: " + ", ".join(parts)
                + ". Hide or delete the ones you no longer need.")
    except Exception:  # noqa: BLE001
        log.warning("could not check for stacked tracks", exc_info=True)
        return ""
