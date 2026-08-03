"""Editor-side ffmpeg chunk extraction for Gemini indexing."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple


def _ffmpeg_run(args: list) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"ffmpeg failed").decode("utf-8", errors="replace")
            return False, err.strip()
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


def extract_chunk(
    video_path: str,
    *,
    start: float,
    end: float,
    out_dir: Optional[str] = None,
    chunk_index: int = 0,
    media_type: str = "video",
) -> Tuple[str, str]:
    """Extract [start, end) to a temp file. Returns (path, error)."""
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
        out_path = os.path.join(
            dest_dir, f"chunk_{int(chunk_index):04d}_{start_f:.3f}_{end_f:.3f}.mp3"
        )
        ok, err = _ffmpeg_run([
            "ffmpeg", "-y",
            "-ss", f"{start_f:.3f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-vn",
            "-acodec", "libmp3lame", "-b:a", "128k",
            out_path,
        ])
    else:
        out_path = os.path.join(
            dest_dir, f"chunk_{int(chunk_index):04d}_{start_f:.3f}_{end_f:.3f}.mp4"
        )
        ok, err = _ffmpeg_run([
            "ffmpeg", "-y",
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


def extract_chunks(
    video_path: str,
    plan: List[Dict[str, Any]],
    *,
    out_dir: Optional[str] = None,
    media_type: str = "video",
) -> Tuple[List[Dict[str, Any]], str, str]:
    """Execute a server chunk plan. Returns (chunk_infos, work_dir, error).

    Each chunk_info: {chunk_index, start, end, path, size, mime_type}
    """
    mt = (media_type or "video").strip().lower()
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
            mime = guess_mime(path, mt)
            results.append({
                "chunk_index": idx,
                "start": start,
                "end": end,
                "path": path,
                "size": os.path.getsize(path),
                "mime_type": mime,
                "owned": True,
            })
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
