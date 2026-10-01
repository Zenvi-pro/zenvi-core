"""Web video ingest via pinned yt-dlp (YouTube and other extractors).

Fetches user-initiated URLs into a local cache, then callers import into the
media bin. Not a scrape-without-download path — frames require a fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from typing import Any, Optional
from urllib.parse import urlparse


# Pin floor documented in requirements; runtime checks major capability only.
YT_DLP_MIN_VERSION = (2024, 1, 1)

# Cache entries older than this are eligible for purge (seconds).
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600

# After a real YouTube 429, refuse further pulls for this URL briefly.
RATE_LIMIT_COOLDOWN_SECONDS = 10 * 60

VALID_INTENTS = frozenset({"reference", "timeline", "recreate"})

_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "music.youtube.com",
})


class WebVideoIngestError(Exception):
    """User-facing ingest failure (missing yt-dlp, bad URL, download failed)."""


def is_probable_web_video_url(url: str) -> bool:
    """True for http(s) page/media URLs (not local paths or file://)."""
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    if host in _YOUTUBE_HOSTS:
        return True
    return host.endswith(".youtube.com")


def normalize_intent(intent: str) -> str:
    raw = str(intent or "reference").strip().lower()
    aliases = {
        # Colour / look — NEVER place on timeline
        "ref": "reference",
        "look": "reference",
        "colour": "reference",
        "color": "reference",
        "grade": "reference",
        "match": "reference",
        # Explicit place only
        "place": "timeline",
        "add": "timeline",
        "timeline": "timeline",
        # Remake
        "remake": "recreate",
        "recreate_like": "recreate",
    }
    raw = aliases.get(raw, raw)
    if raw not in VALID_INTENTS:
        raise WebVideoIngestError(
            f"Unknown intent '{intent}'. Use reference, timeline, or recreate."
        )
    return raw


def cache_root(user_path: str) -> str:
    return os.path.join(str(user_path or ""), "web-video-cache")


def url_cache_key(url: str) -> str:
    return hashlib.sha256(str(url or "").strip().encode("utf-8")).hexdigest()[:24]


def purge_expired_cache(
    root: str,
    *,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    now: Optional[float] = None,
) -> int:
    """Delete cache subdirs older than ttl. Returns number removed."""
    if not root or not os.path.isdir(root):
        return 0
    try:
        ttl = max(3600, int(ttl_seconds))
    except (TypeError, ValueError):
        ttl = DEFAULT_CACHE_TTL_SECONDS
    cutoff = float(now if now is not None else time.time()) - ttl
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except Exception:
                pass
    return removed


def _ensure_yt_dlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise WebVideoIngestError(
            "YouTube/web ingest needs yt-dlp. "
            "Install with: pip install 'yt-dlp>=2024.8.6,<2027' then restart Zenvi."
        ) from exc
    return yt_dlp


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def format_selector_for_intent(intent: str, *, has_ffmpeg: bool = True) -> str:
    """Prefer 720p for reference/recreate; allow higher for timeline edit.

    Without ffmpeg, avoid video+audio merge formats (progressive single file only).
    """
    intent = normalize_intent(intent)
    height = 1080 if intent == "timeline" else 720
    if not has_ffmpeg:
        # Single-file progressive formats — no merge step.
        return (
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/"
            f"best[ext=mp4]/best"
        )
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )


def humanize_ingest_failure(exc: BaseException) -> str:
    """Short agent/user-facing reason — no stack traces."""
    if isinstance(exc, WebVideoIngestError):
        return str(exc)
    msg = str(exc or "").strip()
    low = msg.lower()
    name = type(exc).__name__
    _upload = " Download the MP4 yourself and import it (Imports / import_files_tool)."
    if "ffmpeg" in low or "ffprobe" in low:
        return (
            "Pull needs ffmpeg to combine YouTube video+audio. "
            "Install ffmpeg (e.g. brew install ffmpeg) and try again, or upload an MP4 yourself."
        )
    if any(
        x in low
        for x in (
            "sign in", "login required", "private video", "members only",
            "members-only", "join this channel", "confirm your age",
            "age-restricted", "restricted", "premium",
        )
    ):
        return (
            "This video is private, restricted, or requires sign-in."
            + _upload
        )
    if "unavailable" in low or "not available" in low or "removed" in low:
        return (
            "That video is unavailable (removed, region-blocked, or bad link)."
            + _upload
        )
    if "unsupported url" in low or "no suitable" in low:
        return (
            "That URL is not a supported video page. Paste a public YouTube watch link, "
            "or upload an MP4 yourself."
        )
    if "http error 429" in low or "too many requests" in low:
        return (
            "YouTube rate-limited the download. Wait about 10 minutes, "
            "or upload an MP4 yourself."
        )
    if "timed out" in low or "timeout" in low:
        return "Pull timed out. Check the network and try again, or upload an MP4 yourself."
    if "js runtime" in low or "javascript runtime" in low:
        return (
            "YouTube blocked some formats (JS runtime). Try again once; "
            "if it still fails, update yt-dlp or upload an MP4 yourself."
        )
    one = re.sub(r"\s+", " ", msg).strip()
    if len(one) > 180:
        one = one[:177] + "..."
    if one:
        return f"Pull failed ({one})." + _upload
    return f"Pull failed ({name})." + _upload


def is_rate_limit_error(exc: BaseException) -> bool:
    low = str(exc or "").lower()
    return "http error 429" in low or "too many requests" in low or "rate-limited" in low


def _cooldown_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "cooldown.json")


def write_rate_limit_cooldown(cache_dir: str, *, now: Optional[float] = None) -> None:
    """Record a 429 so the next call can refuse without hitting YouTube again."""
    if not cache_dir:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        payload = {
            "until": float(now if now is not None else time.time()) + RATE_LIMIT_COOLDOWN_SECONDS,
            "reason": "rate_limit",
        }
        with open(_cooldown_path(cache_dir), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


def check_rate_limit_cooldown(
    cache_dir: str, *, now: Optional[float] = None
) -> Optional[str]:
    """Return a user-facing error if still cooling down; else None (and clear expired)."""
    path = _cooldown_path(cache_dir)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        until = float(data.get("until") or 0)
    except Exception:
        return None
    t = float(now if now is not None else time.time())
    if t >= until:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    mins = max(1, int((until - t + 59) // 60))
    return (
        f"YouTube is still rate-limiting this link. Wait about {mins} minute(s), "
        "or upload an MP4 yourself."
    )


def load_cached_download(cache_dir: str) -> Optional[dict[str, Any]]:
    """Return a prior successful download meta if the video file still exists."""
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    side = os.path.join(cache_dir, "ingest.json")
    if not os.path.isfile(side):
        return None
    try:
        with open(side, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    video_path = str(meta.get("video_path") or "").strip()
    if not video_path or not os.path.isfile(video_path):
        # Sidecar may be stale after a purge; try largest media in the folder.
        video_path = _largest_media_file(cache_dir) or ""
        if not video_path:
            return None
        meta["video_path"] = os.path.abspath(video_path)
    meta["ok"] = True
    meta["from_cache"] = True
    return meta


def download_web_video(
    url: str,
    *,
    cache_dir: str,
    intent: str = "reference",
    write_subs: bool = False,
    ydl_factory=None,
) -> dict[str, Any]:
    """Download URL into cache_dir via yt-dlp. Returns paths + metadata.

    ``ydl_factory`` is optional for tests: callable(opts) -> context manager
    with extract_info(url, download=True).
    """
    if not is_probable_web_video_url(url):
        raise WebVideoIngestError(
            "URL must be http(s). Local paths use import_files_tool; "
            "direct MP4s may use import_video_url_and_add_to_timeline_tool."
        )
    intent = normalize_intent(intent)
    os.makedirs(cache_dir, exist_ok=True)

    cool = check_rate_limit_cooldown(cache_dir)
    if cool:
        raise WebVideoIngestError(cool)

    cached = load_cached_download(cache_dir)
    if cached:
        cached["intent"] = intent
        cached["url"] = str(url).strip()
        return cached

    has_ffmpeg = ffmpeg_available()
    outtmpl = os.path.join(cache_dir, "media.%(ext)s")
    opts: dict[str, Any] = {
        "format": format_selector_for_intent(intent, has_ffmpeg=has_ffmpeg),
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "ignoreerrors": False,
        "no_color": True,
    }
    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"
        opts["prefer_ffmpeg"] = True
    if write_subs or intent in ("reference", "recreate"):
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB"],
            "subtitlesformat": "vtt/srt/best",
            "skip_download": False,
        })
    if is_youtube_url(url):
        # Prefer android then web — fewer bot challenges than default alone.
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "web"]},
        }

    yt_dlp = _ensure_yt_dlp() if ydl_factory is None else None
    factory = ydl_factory or (lambda o: yt_dlp.YoutubeDL(o))

    try:
        with factory(opts) as ydl:
            info = ydl.extract_info(str(url).strip(), download=True)
    except WebVideoIngestError:
        raise
    except Exception as exc:
        if is_rate_limit_error(exc):
            write_rate_limit_cooldown(cache_dir)
        raise WebVideoIngestError(humanize_ingest_failure(exc)) from exc

    if not isinstance(info, dict):
        raise WebVideoIngestError("Download finished but YouTube returned no video info.")

    # Resolve output path (merged file or single format).
    video_path = ""
    requested = info.get("requested_downloads") or []
    if isinstance(requested, list) and requested:
        for item in requested:
            if isinstance(item, dict) and item.get("filepath"):
                video_path = str(item["filepath"])
                break
    if not video_path:
        # Fallback: scan cache_dir for largest media file.
        video_path = _largest_media_file(cache_dir) or ""
    if not video_path or not os.path.isfile(video_path):
        # Try prepared filename from info
        try:
            ext = str(info.get("ext") or "mp4")
            candidate = os.path.join(cache_dir, f"media.{ext}")
            if os.path.isfile(candidate):
                video_path = candidate
        except Exception:
            pass
    if not video_path or not os.path.isfile(video_path):
        raise WebVideoIngestError("Download finished but no video file landed on disk. Try again or update yt-dlp.")

    subs = _find_subtitle_files(cache_dir)
    title = str(info.get("title") or info.get("id") or "web_video").strip()
    title = re.sub(r"\s+", " ", title)[:180]
    meta = {
        "ok": True,
        "url": str(url).strip(),
        "intent": intent,
        "title": title,
        "extractor": str(info.get("extractor") or info.get("ie_key") or ""),
        "duration_seconds": _safe_float(info.get("duration")),
        "width": info.get("width"),
        "height": info.get("height"),
        "video_path": os.path.abspath(video_path),
        "subtitle_paths": subs,
        "webpage_url": str(info.get("webpage_url") or url).strip(),
        "id": str(info.get("id") or ""),
        "is_youtube": is_youtube_url(url),
        "product_note": (
            "User-initiated local cache for personal reference/edit in Zenvi. "
            "Do not re-upload the original as your own content."
        ),
    }
    # Persist sidecar for cache reuse / debugging.
    try:
        with open(os.path.join(cache_dir, "ingest.json"), "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in meta.items() if k != "product_note"}, fh, indent=2)
    except OSError:
        pass
    return meta


def _safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _largest_media_file(directory: str) -> Optional[str]:
    exts = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3"}
    best = None
    best_size = -1
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > best_size:
                best_size = size
                best = path
    except OSError:
        return None
    return best


def _find_subtitle_files(directory: str) -> list[str]:
    out = []
    try:
        for name in os.listdir(directory):
            lower = name.lower()
            if lower.endswith(".vtt") or lower.endswith(".srt"):
                out.append(os.path.abspath(os.path.join(directory, name)))
    except OSError:
        return []
    return sorted(out)
