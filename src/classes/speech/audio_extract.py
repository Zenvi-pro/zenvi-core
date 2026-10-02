"""Extract mono 16 kHz PCM for ASR / VAD (ffmpeg)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Optional, Tuple

log = logging.getLogger("speech.audio_extract")

_SAMPLE_RATE = 16000


def probe_has_audio(media_path: str) -> Tuple[Optional[bool], str]:
    """Return ``(has_audio, error)``. *has_audio* is None when probe fails."""
    if not media_path or not os.path.isfile(media_path):
        return None, f"media not found: {media_path!r}"
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-hide_banner", "-loglevel", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "json",
                media_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return None, "ffprobe not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out"
    except Exception as exc:
        return None, f"ffprobe failed: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "ffprobe failed"
        return None, err
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None, "ffprobe returned invalid JSON"
    streams = data.get("streams") if isinstance(data, dict) else None
    return bool(streams), ""


def extract_mono_16k_wav(
    media_path: str,
    *,
    dest_path: Optional[str] = None,
    start_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Tuple[str, str]:
    """Return ``(wav_path, error)``. On success *error* is empty.

    Writes atomically into *dest_path* when given; otherwise a temp file the
    caller must delete.
    """
    if not media_path or not os.path.isfile(media_path):
        return "", f"media not found: {media_path!r}"

    has_audio, probe_err = probe_has_audio(media_path)
    if has_audio is False:
        return (
            "",
            "no audio track in this media file (video-only / silent).",
        )
    if has_audio is None and probe_err:
        log.debug("audio probe inconclusive for %s: %s", media_path, probe_err)

    if dest_path:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        out = dest_path
        tmp = out + ".partial"
    else:
        fd, out = tempfile.mkstemp(suffix=".wav", prefix="zenvi_asr_")
        os.close(fd)
        tmp = out

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{float(start_sec):.3f}"])
    cmd.extend(["-i", media_path])
    if duration_sec is not None and duration_sec > 0:
        cmd.extend(["-t", f"{float(duration_sec):.3f}"])
    cmd.extend([
        "-vn",
        "-ac", "1",
        "-ar", str(_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        tmp,
    ])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        if tmp != out and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if dest_path is None and os.path.isfile(out):
            try:
                os.remove(out)
            except OSError:
                pass
        return "", "ffmpeg not found on PATH"
    except subprocess.TimeoutExpired:
        _cleanup(tmp, out, dest_path)
        return "", "ffmpeg audio extract timed out"
    except Exception as exc:
        _cleanup(tmp, out, dest_path)
        return "", f"ffmpeg audio extract failed: {exc}"

    if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) <= 0:
        err = (proc.stderr or "").strip() or "ffmpeg audio extract failed"
        if "does not contain any stream" in err.lower():
            err = (
                "no audio track in this media file (video-only / silent)."
            )
        _cleanup(tmp, out, dest_path)
        return "", err

    if tmp != out:
        os.replace(tmp, out)
    return out, ""


def _cleanup(tmp: str, out: str, dest_path: Optional[str]) -> None:
    for path in {tmp, out}:
        if not path:
            continue
        if dest_path is None or path != dest_path:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
