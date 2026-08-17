"""Editor-side ffmpeg chunk extraction for Gemini indexing."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from classes.ffmpeg_cli import resolve_ffmpeg_args


def _short_ffmpeg_error(raw: str, *, limit: int = 400) -> str:
    """Keep user-facing ffmpeg errors short (no version banners)."""
    text = (raw or "").strip()
    if not text:
        return "ffmpeg failed"
    # Prefer the last non-empty lines — real errors are usually at the end.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    useful = [
        ln for ln in lines
        if not ln.lower().startswith("ffmpeg version")
        and not ln.lower().startswith("built with")
        and not ln.lower().startswith("configuration:")
        and not ln.lower().startswith("libav")
        and not ln.lower().startswith("libsw")
        and "copyright" not in ln.lower()
    ]
    pick = useful[-6:] if useful else lines[-4:]
    msg = " | ".join(pick)
    if len(msg) > limit:
        msg = msg[-limit:]
    return msg or "ffmpeg failed"


def _ffmpeg_run(args: list) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            resolve_ffmpeg_args(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"ffmpeg failed").decode(
                "utf-8", errors="replace"
            )
            return False, _short_ffmpeg_error(err)
        return True, ""
    except FileNotFoundError:
        return False, "ffmpeg not found."
    except Exception as exc:
        return False, str(exc)


def guess_mime(path: str, media_type: str = "video") -> str:
    mt = (media_type or "video").strip().lower()
    guessed = None
    try:
        guessed, _ = mimetypes.guess_type(path or "")
    except Exception:
        guessed = None
    if guessed:
        return guessed
    if mt == "image":
        ext = os.path.splitext(path or "")[1].lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(ext, "image/png")
    if mt == "audio":
        ext = os.path.splitext(path or "")[1].lower()
        return {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".flac": "audio/flac",
        }.get(ext, "audio/mpeg")
    return "video/mp4"


def _probe_duration(path: str) -> float:
    try:
        proc = subprocess.run(
            resolve_ffmpeg_args([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return float((proc.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _is_full_file_chunk(start: float, end: float, source_path: str) -> bool:
    """True when the plan covers essentially the whole source (skip re-encode)."""
    if start > 0.05:
        return False
    src_dur = _probe_duration(source_path)
    if src_dur <= 0:
        return False
    return end >= src_dur - 0.5


def extract_chunk(
    video_path: str,
    *,
    start: float,
    end: float,
    out_dir: Optional[str] = None,
    chunk_index: int = 0,
    media_type: str = "video",
) -> Tuple[str, str]:
    """Extract [start, end) to a temp file. Returns (path, error).

    For audio: prefer the original file when the chunk spans the whole asset;
    otherwise stream-copy or fall back to WAV (Gemini accepts audio/wav).
    """
    if not video_path or not os.path.isfile(video_path):
        return "", f"File not found: {video_path}"
    mt = (media_type or "video").strip().lower()
    if mt == "image":
        return video_path, ""

    try:
        start_f = max(0.0, float(start))
        end_f = max(start_f + 0.1, float(end))
    except Exception:
        return "", "Invalid start/end"
    duration = end_f - start_f

    dest_dir = out_dir or tempfile.mkdtemp(prefix="zenvi_idx_chunks_")
    os.makedirs(dest_dir, exist_ok=True)

    if mt == "audio":
        # Whole-file plan → upload original (avoids brittle mp3 re-encode).
        if _is_full_file_chunk(start_f, end_f, video_path):
            return video_path, ""

        base = f"chunk_{int(chunk_index):04d}_{start_f:.3f}_{end_f:.3f}"
        # 1) Try stream copy into same container (fast, preserves codec).
        ext = os.path.splitext(video_path)[1].lower() or ".mp3"
        if ext not in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4"):
            ext = ".mp3"
        copy_path = os.path.join(dest_dir, base + ext)
        ok, err = _ffmpeg_run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path,
            "-ss", f"{start_f:.3f}",
            "-t", f"{duration:.3f}",
            "-vn",
            "-c:a", "copy",
            "-map", "0:a:0",
            copy_path,
        ])
        if ok and os.path.isfile(copy_path) and os.path.getsize(copy_path) > 0:
            return copy_path, ""

        # 2) Re-encode to WAV (widely accepted by Gemini audio embed/analyze).
        wav_path = os.path.join(dest_dir, base + ".wav")
        ok, err = _ffmpeg_run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path,
            "-ss", f"{start_f:.3f}",
            "-t", f"{duration:.3f}",
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            wav_path,
        ])
        if ok and os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0:
            return wav_path, ""
        return "", err or "ffmpeg audio extract failed"

    out_path = os.path.join(
        dest_dir, f"chunk_{int(chunk_index):04d}_{start_f:.3f}_{end_f:.3f}.mp4"
    )
    ok, err = _ffmpeg_run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_f:.3f}",
        "-i", video_path,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        out_path,
    ])
    if not ok or not os.path.isfile(out_path):
        return "", err or "ffmpeg extract failed"
    return out_path, ""


def _looks_like_audio_path(path: str) -> bool:
    ext = os.path.splitext(path or "")[1].lower()
    return ext in (
        ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
        ".opus", ".aiff", ".aif", ".oga",
    )


def extract_chunks(
    video_path: str,
    plan: List[Dict[str, Any]],
    *,
    out_dir: Optional[str] = None,
    media_type: str = "video",
) -> Tuple[List[Dict[str, Any]], str, str]:
    """Execute a server chunk plan. Returns (chunk_infos, work_dir, error).

    Each chunk_info: {chunk_index, start, end, path, size, mime_type, owned?}
    """
    mt = (media_type or "video").strip().lower()
    # Guard: never run the video→mp4 path on pure-audio files.
    if mt != "audio" and _looks_like_audio_path(video_path):
        mt = "audio"
    if mt == "image":
        if not video_path or not os.path.isfile(video_path):
            return [], "", f"File not found: {video_path}"
        mime = guess_mime(video_path, "image")
        return [{
            "chunk_index": 0,
            "start": 0.0,
            "end": 0.0,
            "path": video_path,
            "size": os.path.getsize(video_path),
            "mime_type": mime,
            "owned": False,
        }], "", ""

    if not plan:
        return [], "", "Empty chunk plan"

    # Single full-file audio plan → no temp dir / no ffmpeg.
    if mt == "audio" and len(plan) == 1:
        start = float(plan[0].get("start") or 0)
        end = float(plan[0].get("end") or start)
        if _is_full_file_chunk(start, end, video_path):
            mime = guess_mime(video_path, "audio")
            return [{
                "chunk_index": int(plan[0].get("chunk_index") or 0),
                "start": start,
                "end": end,
                "path": video_path,
                "size": os.path.getsize(video_path),
                "mime_type": mime,
                "owned": False,
            }], "", ""

    work = out_dir or tempfile.mkdtemp(prefix="zenvi_idx_chunks_")
    results: List[Dict[str, Any]] = []
    try:
        for item in plan:
            idx = int(item.get("chunk_index") or 0)
            start = float(item.get("start") or 0)
            end = float(item.get("end") or start)
            path, err = extract_chunk(
                video_path,
                start=start,
                end=end,
                out_dir=work,
                chunk_index=idx,
                media_type=mt,
            )
            if err:
                cleanup_chunk_dir(work)
                return [], "", err
            owned = os.path.abspath(path) != os.path.abspath(video_path)
            mime = guess_mime(path, mt)
            results.append({
                "chunk_index": idx,
                "start": start,
                "end": end,
                "path": path,
                "size": os.path.getsize(path),
                "mime_type": mime,
                "owned": owned,
            })
        # If nothing was written into work (all originals), drop empty temp dir.
        if results and all(not r.get("owned") for r in results):
            cleanup_chunk_dir(work)
            return results, "", ""
        return results, work, ""
    except Exception as exc:
        cleanup_chunk_dir(work)
        return [], "", str(exc)


def cleanup_chunk_dir(path: str) -> None:
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
