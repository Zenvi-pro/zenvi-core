"""
 @file
 @brief Lazy cloud audio understanding: describe music by meaning, and listen to a finished mix.

 Both send ONE small audio file (AAC, mono, 48 kbps: about 0.36 MB a minute) and get the model's
 reading back. Music descriptions are kept on the shelf (they depend only on the audio, so any project
 reuses them, and nothing is paid twice). A mix audit is never cached: the mix changes with every edit.
 The returned fields are the model's opinion (``inferred``); the local measurements stay separate.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from classes.ffmpeg_cli import run_ffmpeg
from classes.media_index import schema as S
from classes.media_index.cloud import Cancelled, _wait_for_job
from classes.media_index.store import Shelf

WINDOW_SECONDS = 30.0
MIN_WINDOW = 3.0
MAX_WINDOW = 120.0
MAX_WINDOWS = 60
MAX_AUDIO_SECONDS = 3600.0
MIME = "audio/aac"


def make_audio_proxy(path: str, out_path: str, start: Optional[float] = None, end: Optional[float] = None) -> Tuple[bool, str]:
    """A small mono AAC (ADTS) copy of the audio of *path*, optionally just [start, end). Metadata is dropped."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}"]
    cmd += ["-i", path]
    if end is not None:
        cmd += ["-t", f"{max(0.1, float(end) - float(start or 0.0)):.3f}"]
    cmd += ["-vn", "-map", "0:a:0", "-map_metadata", "-1", "-ac", "1", "-ar", "32000", "-c:a", "aac", "-b:a", "48k", "-f", "adts", out_path]
    try:
        proc = run_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=1800)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        return False, (proc.stderr or b"audio encode failed").decode("utf-8", "replace")[-300:]
    return True, ""


def plan_windows(duration: float, sections: Optional[Sequence[Dict[str, Any]]] = None, *, target: float = WINDOW_SECONDS,
                 min_len: float = MIN_WINDOW, max_len: float = MAX_WINDOW, max_windows: int = MAX_WINDOWS) -> List[Dict[str, float]]:
    """Windows that cover [0, duration): along the music's own sections when known, else about *target* seconds each.

    Every window lasts min_len..max_len seconds and there are at most max_windows; tiny leftovers merge into a neighbour.
    """
    duration = min(float(duration), MAX_AUDIO_SECONDS)
    if duration < min_len:
        return []
    edges: List[float] = [0.0]
    for sec in sections or []:
        end = min(float(sec["end"]), duration)
        if end - edges[-1] >= min_len:
            edges.append(end)
    if edges[-1] < duration:
        if duration - edges[-1] < min_len and len(edges) > 1:
            edges[-1] = duration
        else:
            edges.append(duration)
    windows: List[Dict[str, float]] = []
    use_sections = bool(sections) and len(edges) > 2
    for a, b in zip(edges[:-1], edges[1:]):
        length = b - a
        step = max_len if use_sections else target
        parts = max(1, math.ceil(length / step - 1e-9))
        if length / parts < min_len:
            parts = max(1, int(length // min_len))
        for i in range(parts):
            windows.append({"start": round(a + length * i / parts, 3), "end": round(a + length * (i + 1) / parts, 3)})
    if len(windows) > max_windows:                      # widen evenly rather than drop the end of the track
        per = math.ceil(len(windows) / max_windows)
        windows = [{"start": windows[i]["start"], "end": windows[min(i + per, len(windows)) - 1]["end"]}
                   for i in range(0, len(windows), per)]
        windows = [w for w in windows if w["end"] - w["start"] <= max_len] or windows
    return windows


def _upload_and_run(client: Any, audio_path: str, file_id: str, start_call: Callable[[str, str, Any], Dict[str, Any]], *, what: str,
                    should_cancel: Optional[Callable[[], bool]], on_progress: Optional[Callable[[float], None]],
                    uploader: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable

    uploader = uploader or upload_file_to_gemini_resumable
    session = client._new_http_session()
    if should_cancel and should_cancel():
        raise Cancelled()
    sess = client.v2_upload_session(file_id, os.path.basename(audio_path), os.path.getsize(audio_path), MIME, session=session)
    if sess.get("error") or not sess.get("upload_url"):
        return {k: v for k, v in sess.items() if k in ("error", "unsupported", "auth")} or {"error": "no upload url"}
    info, up_err = uploader(audio_path, sess["upload_url"], mime_type=MIME)
    if up_err:
        return {"error": f"upload failed: {up_err}"}
    if on_progress:
        on_progress(0.3)
    started = start_call(info.get("name", ""), info.get("uri", ""), session)
    if started.get("error") or not started.get("job_id"):
        return {k: v for k, v in started.items() if k in ("error", "unsupported", "auth")} or {"error": "the backend did not start the job"}
    result = _wait_for_job(client, session, started["job_id"], should_cancel,
                           on_tick=(lambda f: on_progress(0.3 + 0.7 * f)) if on_progress else None, what=what)
    if on_progress:
        on_progress(1.0)
    return result


def describe_music(client: Any, path: str, probe: Dict[str, Any], sha: str, shelf: Shelf, *, file_id: str = "",
                   sections: Optional[Sequence[Dict[str, Any]]] = None, should_cancel: Optional[Callable[[], bool]] = None,
                   on_progress: Optional[Callable[[float], None]] = None, uploader: Optional[Callable[..., Any]] = None,
                   force: bool = False) -> Dict[str, Any]:
    """Genre, mood, energy and vocals per window of a music file, kept on the shelf.

    Returns ``{"windows", "usage"?, "cached"}`` or ``{"error", "unsupported"?, "auth"?}``. Asking again is free.
    """
    version = S.LAZY_LAYERS[S.LAYER_MUSIC_DESC]
    if not force and shelf.layer_ready(sha, S.LAYER_MUSIC_DESC):
        saved = shelf.read_json(sha, "music_desc.json") or {}
        if saved.get("windows"):
            return {"windows": saved["windows"], "cached": True, "usage": saved.get("usage")}
    if not (probe or {}).get("has_audio"):
        return {"error": "this file has no audio track"}
    duration = min(float((probe or {}).get("duration") or 0.0), MAX_AUDIO_SECONDS)
    windows = plan_windows(duration, sections)
    if not windows:
        return {"error": "the audio is too short to describe"}
    with tempfile.TemporaryDirectory(prefix="zenvi_music_") as tmp:
        proxy = os.path.join(tmp, "music.aac")
        ok, err = make_audio_proxy(path, proxy, 0.0, duration)
        if not ok:
            return {"error": f"could not prepare the audio: {err}"}
        result = _upload_and_run(
            client, proxy, file_id or sha[:12],
            lambda name, uri, session: client.v2_describe_audio(name, uri, duration, windows, MIME, session=session),
            what="describing the music", should_cancel=should_cancel, on_progress=on_progress, uploader=uploader)
    if result.get("error") or not result.get("windows"):
        return {k: v for k, v in result.items() if k in ("error", "unsupported", "auth")} or {"error": "the backend returned no description"}
    shelf.write_json(sha, "music_desc.json", {"version": version, "kind": S.INFERRED, "windows": result["windows"],
                                              "usage": result.get("usage"), "created": time.time()})
    shelf.set_layer(sha, S.LAYER_MUSIC_DESC, version=version, status="ready", windows=len(result["windows"]))
    return {"windows": result["windows"], "usage": result.get("usage"), "cached": False}


def listen(client: Any, audio_path: str, duration: float, context: Optional[Dict[str, Any]] = None, *, file_id: str = "mix",
           should_cancel: Optional[Callable[[], bool]] = None, on_progress: Optional[Callable[[float], None]] = None,
           uploader: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Audit a rendered mix (any audio file, already small): ``{"overall", "findings", "summary", "usage"}`` or ``{"error"}``."""
    if not 0 < float(duration) <= MAX_AUDIO_SECONDS:
        return {"error": f"the mix must be between 0 and {int(MAX_AUDIO_SECONDS)} seconds"}
    return _upload_and_run(
        client, audio_path, file_id,
        lambda name, uri, session: client.v2_listen(name, uri, float(duration), context or {}, MIME, session=session),
        what="listening to the mix", should_cancel=should_cancel, on_progress=on_progress, uploader=uploader)
