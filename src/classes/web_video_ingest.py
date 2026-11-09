"""Web video ingest via yt-dlp (YouTube + IG Reels) and optional gallery-dl.

Fetches user-initiated URLs into a local cache, then callers import into the
media bin. yt-dlp is imported on first use. Instagram profile / stories /
highlights use gallery-dl only when requested (optional install — not required
for YouTube or single Reels). Cookies stay on-machine via cookies-from-browser.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

_GDL_CONFIG_LOCK = threading.Lock()


# Pin floor documented in requirements; runtime checks major capability only.


# Cache entries older than this are eligible for purge (seconds).
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600

# After a real YouTube/IG 429, refuse further pulls for this URL briefly.
RATE_LIMIT_COOLDOWN_SECONDS = 10 * 60

VALID_INTENTS = frozenset({"reference", "timeline", "recreate"})

DEFAULT_PROFILE_MAX_ITEMS = 12
HARD_PROFILE_MAX_ITEMS = 20

# Instagram cookie browser preference / tool override.
VALID_COOKIE_BROWSERS = frozenset({
    "firefox", "chrome", "edge", "chromium", "brave", "opera", "safari", "off",
})
DEFAULT_COOKIE_BROWSER = "firefox"

_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "music.youtube.com",
})

_INSTAGRAM_HOSTS = frozenset({
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
})

# Path segments that are never a creator profile username.
_IG_RESERVED_PATHS = frozenset({
    "reel", "reels", "p", "tv", "stories", "highlights", "explore",
    "accounts", "direct", "about", "legal", "developer", "directory",
    "web", "graphql", "api", "tags", "locations", "share", "s",
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


def assert_public_http_url(url: str) -> None:
    """Reject loopback / link-local / private destinations (SSRF).

    Resolves the hostname and refuses any non-public address before outbound
    fetch (yt-dlp or urllib). Raises WebVideoIngestError on refusal.
    """
    if not is_probable_web_video_url(url):
        raise WebVideoIngestError(
            "URL must be http(s). Local paths use import_files_tool; "
            "direct MP4s may use ingest_web_video_tool."
        )
    host = (urlparse(str(url).strip()).hostname or "").strip()
    if not host:
        raise WebVideoIngestError("URL host is missing.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebVideoIngestError(f"Could not resolve host {host!r}.") from exc
    if not infos:
        raise WebVideoIngestError(f"Could not resolve host {host!r}.")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise WebVideoIngestError(
                "That URL points at a private or local network address, "
                "which cannot be fetched."
            )


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to a public address: checking the first URL is not enough when the server can send us elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public_url(request, timeout: float):
    """``urlopen`` for a user-supplied URL that also refuses redirects to private, loopback or link-local addresses.

    The URL itself is checked with ``assert_public_http_url`` before anything is opened. Raises WebVideoIngestError on a refused hop.
    """
    assert_public_http_url(request.full_url if isinstance(request, urllib.request.Request) else request)
    return urllib.request.build_opener(_PublicOnlyRedirects).open(request, timeout=timeout)


def requested_height_for_intent(intent: str) -> int:
    return 1080 if normalize_intent(intent) == "timeline" else 720


def cache_satisfies_request(
    meta: dict[str, Any],
    *,
    intent: str,
    write_subs: bool,
    max_items: Optional[int] = None,
) -> bool:
    """True when a cached download is good enough for this request."""
    if not isinstance(meta, dict):
        return False
    items = meta.get("items")
    if isinstance(items, list) and items:
        want_n = clamp_profile_max_items(
            max_items if max_items is not None else meta.get("requested_max_items")
        )
        have_n = len(items)
        try:
            prior_want = int(meta.get("requested_max_items") or have_n)
        except (TypeError, ValueError):
            prior_want = have_n
        if have_n < want_n and prior_want < want_n:
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            path = str(item.get("video_path") or "").strip()
            if not path or not os.path.isfile(path):
                return False
        return True
    want_h = requested_height_for_intent(intent)
    try:
        have_h = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        have_h = 0
    if have_h and have_h < want_h:
        return False
    # Unknown height: only reuse for the same-or-lower intent height that
    # originally wrote the cache (stored as requested_height).
    if not have_h:
        try:
            stored = int(meta.get("requested_height") or 0)
        except (TypeError, ValueError):
            stored = 0
        if stored and stored < want_h:
            return False
    need_subs = bool(write_subs) or normalize_intent(intent) in ("reference", "recreate")
    if need_subs:
        subs = meta.get("subtitle_paths") or []
        if not isinstance(subs, list) or not subs:
            # Allow reuse when the prior fetch already tried for subs
            # (write_subs_requested) even if none existed on the site.
            if not meta.get("write_subs_requested"):
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


def is_instagram_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    if host in _INSTAGRAM_HOSTS:
        return True
    return host.endswith(".instagram.com")


def classify_instagram_url(url: str) -> Optional[str]:
    """Classify an Instagram URL for ingest routing.

    Returns:
      ``single`` — /reel/, /reels/<code>, /p/, /tv/ (yt-dlp)
      ``profile`` — /<username> or /<username>/reels (gallery-dl capped Reels)
      ``stories`` — /stories/<user>/... (gallery-dl, on demand)
      ``highlights`` — /stories/highlights/<id> or /<user>/highlights (gallery-dl)
      ``None`` — not IG, or unsupported (tagged, explore, …)
    """
    if not is_instagram_url(url):
        return None
    try:
        path = (urlparse(str(url).strip()).path or "").strip("/")
    except Exception:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    first = parts[0].lower()
    if first == "stories":
        if len(parts) >= 2 and parts[1].lower() == "highlights":
            return "highlights" if len(parts) >= 3 else None
        return "stories" if len(parts) >= 2 else None
    if first in ("reel", "p", "tv"):
        return "single" if len(parts) >= 2 else None
    if first == "reels":
        # Site short-code reel, not a username named "reels".
        return "single" if len(parts) >= 2 else None
    if first in _IG_RESERVED_PATHS:
        return None
    if len(parts) == 1:
        return "profile"
    second = parts[1].lower()
    if second == "reels":
        return "profile"
    if second == "highlights":
        return "highlights"
    # /username/tagged, /username/followers, etc.
    return None


def instagram_username_from_url(url: str) -> str:
    """Best-effort username for profile/reels URLs (empty if unknown)."""
    try:
        parts = [p for p in (urlparse(str(url).strip()).path or "").strip("/").split("/") if p]
    except Exception:
        return ""
    if not parts:
        return ""
    first = parts[0].lower()
    if first in _IG_RESERVED_PATHS or first in ("reel", "reels", "p", "tv", "stories"):
        return ""
    return parts[0]


def clamp_profile_max_items(value: Any) -> int:
    """Default 12, hard-capped at 20. Empty/invalid → default."""
    if value is None or str(value).strip() == "":
        return DEFAULT_PROFILE_MAX_ITEMS
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_PROFILE_MAX_ITEMS
    if n < 1:
        return DEFAULT_PROFILE_MAX_ITEMS
    return min(n, HARD_PROFILE_MAX_ITEMS)


def normalize_cookies_browser(value: Any) -> Optional[str]:
    """Return a yt-dlp browser name, or None when cookies are disabled."""
    raw = str(value or "").strip().lower()
    if not raw or raw in ("off", "none", "false", "0", "no"):
        return None
    if raw not in VALID_COOKIE_BROWSERS or raw == "off":
        return DEFAULT_COOKIE_BROWSER
    return raw


def resolve_cookies_browser(explicit: Any = None) -> Optional[str]:
    """Tool override → preference ``web-ingest-cookies-browser`` → Firefox."""
    if explicit is not None and str(explicit).strip() != "":
        return normalize_cookies_browser(explicit)
    try:
        from classes.app import get_app
        pref = get_app().get_settings().get("web-ingest-cookies-browser")
        if pref is not None and str(pref).strip() != "":
            return normalize_cookies_browser(pref)
    except Exception:
        pass
    return DEFAULT_COOKIE_BROWSER


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


def _ensure_gallery_dl():
    """Optional: only required for IG profile / stories / highlights."""
    try:
        import gallery_dl  # type: ignore
        from gallery_dl import config as gdl_config  # type: ignore
        from gallery_dl import job as gdl_job  # type: ignore
    except ImportError as exc:
        raise WebVideoIngestError(
            "Instagram profile, stories, or highlights need gallery-dl "
            "(optional — not required for YouTube or a single Reel). "
            "Install with: pip install 'gallery-dl>=1.27' then retry."
        ) from exc
    return gallery_dl, gdl_config, gdl_job


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def format_selector_for_intent(intent: str, *, has_ffmpeg: bool = True) -> str:
    """Prefer 720p for reference/recreate; allow higher for timeline edit.

    Without ffmpeg, avoid video+audio merge formats (progressive single file only).
    """
    intent = normalize_intent(intent)
    height = requested_height_for_intent(intent)
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


def humanize_ingest_failure(exc: BaseException, *, url: str = "") -> str:
    """Short agent/user-facing reason — no stack traces."""
    if isinstance(exc, WebVideoIngestError):
        return str(exc)
    msg = str(exc or "").strip()
    low = msg.lower()
    name = type(exc).__name__
    ig = is_instagram_url(url) if url else ("instagram" in low)
    _upload = " Download the MP4 yourself and import it (Imports / import_files_tool)."
    _ig_cookies = (
        " Log into Instagram in Firefox, fully quit Firefox, then retry "
        "(Preferences → Web ingest cookies browser)."
    )
    if "ffmpeg" in low or "ffprobe" in low:
        return (
            "Pull needs ffmpeg to combine video+audio. "
            "Install ffmpeg (e.g. brew install ffmpeg) and try again, or upload an MP4 yourself."
        )
    if any(
        x in low
        for x in (
            "cookiesfrombrowser", "could not copy cookies", "failed to decrypt",
            "unable to load cookies", "no cookies",
        )
    ):
        return (
            "Could not read browser cookies for Instagram."
            + _ig_cookies
            + _upload
        )
    if any(
        x in low
        for x in (
            "sign in", "login required", "private video", "members only",
            "members-only", "join this channel", "confirm your age",
            "age-restricted", "restricted", "premium",
            "please wait a few minutes", "challenge",
        )
    ):
        if ig:
            return (
                "Instagram needs a logged-in browser session."
                + _ig_cookies
                + _upload
            )
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
            "That URL is not a supported video page. Paste a YouTube or Instagram "
            "Reel/profile link, or upload an MP4 yourself."
        )
    if "http error 429" in low or "too many requests" in low:
        site = "Instagram" if ig else "YouTube"
        return (
            f"{site} rate-limited the download. Wait about 10 minutes, "
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
        f"This link is still rate-limited. Wait about {mins} minute(s), "
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


_PRODUCT_NOTE = (
    "User-initiated local cache for personal reference/edit in Zenvi. "
    "Do not re-upload the original as your own content."
)

_GDL_SAMPLE_KINDS = frozenset({"profile", "stories", "highlights"})


def download_web_video(
    url: str,
    *,
    cache_dir: str,
    intent: str = "reference",
    write_subs: bool = False,
    max_items: Any = None,
    cookies_browser: Any = None,
    ydl_factory=None,
    gdl_runner=None,
) -> dict[str, Any]:
    """Download URL into cache_dir. Returns paths + metadata.

    Routing:
      - YouTube / IG single Reel|post → yt-dlp
      - IG profile / stories / highlights → optional gallery-dl (capped)
    """
    assert_public_http_url(url)
    intent = normalize_intent(intent)
    os.makedirs(cache_dir, exist_ok=True)

    cool = check_rate_limit_cooldown(cache_dir)
    if cool:
        raise WebVideoIngestError(cool)

    ig_kind = classify_instagram_url(url)
    if is_instagram_url(url) and ig_kind is None:
        raise WebVideoIngestError(
            "That Instagram URL is not supported. Paste a Reel/post, a creator "
            "profile (recent Reels sample), or a stories/highlights link — "
            "not tagged, explore, or similar."
        )

    is_sample = ig_kind in _GDL_SAMPLE_KINDS
    want_n = clamp_profile_max_items(max_items) if is_sample else 1

    want_subs = bool(write_subs) or intent in ("reference", "recreate")
    if is_instagram_url(url):
        want_subs = bool(write_subs)

    cached = load_cached_download(cache_dir)
    if cached and cache_satisfies_request(
        cached, intent=intent, write_subs=want_subs, max_items=want_n if is_sample else None
    ):
        cached["intent"] = intent
        cached["url"] = str(url).strip()
        if is_sample and isinstance(cached.get("items"), list) and len(cached["items"]) > want_n:
            cached = dict(cached)
            cached["items"] = cached["items"][:want_n]
            cached["sampled"] = len(cached["items"])
            first = cached["items"][0] if cached["items"] else {}
            cached["video_path"] = first.get("video_path") or cached.get("video_path")
        return cached

    if is_sample:
        meta = download_instagram_gallery_dl(
            url,
            cache_dir=cache_dir,
            intent=intent,
            ig_kind=ig_kind or "profile",
            max_items=want_n,
            cookies_browser=cookies_browser,
            gdl_runner=gdl_runner,
        )
        _write_ingest_sidecar(cache_dir, meta)
        return meta

    return _download_via_yt_dlp(
        url,
        cache_dir=cache_dir,
        intent=intent,
        want_subs=want_subs,
        cookies_browser=cookies_browser,
        ydl_factory=ydl_factory,
    )


def _write_ingest_sidecar(cache_dir: str, meta: dict[str, Any]) -> None:
    try:
        with open(os.path.join(cache_dir, "ingest.json"), "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in meta.items() if k != "product_note"}, fh, indent=2)
    except OSError:
        pass


def _download_via_yt_dlp(
    url: str,
    *,
    cache_dir: str,
    intent: str,
    want_subs: bool,
    cookies_browser: Any,
    ydl_factory=None,
) -> dict[str, Any]:
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
    if want_subs:
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB"],
            "subtitlesformat": "vtt/srt/best",
            "skip_download": False,
        })
    if is_youtube_url(url):
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "web"]},
        }
    if is_instagram_url(url):
        browser = resolve_cookies_browser(cookies_browser)
        if browser:
            opts["cookiesfrombrowser"] = (browser,)

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
        raise WebVideoIngestError(humanize_ingest_failure(exc, url=url)) from exc

    if not isinstance(info, dict):
        raise WebVideoIngestError("Download finished but no video info was returned.")

    meta = _meta_from_single(
        info,
        url=url,
        cache_dir=cache_dir,
        intent=intent,
        want_subs=want_subs,
        product_note=_PRODUCT_NOTE,
    )
    _write_ingest_sidecar(cache_dir, meta)
    return meta


def download_instagram_gallery_dl(
    url: str,
    *,
    cache_dir: str,
    intent: str,
    ig_kind: str,
    max_items: int,
    cookies_browser: Any = None,
    gdl_runner=None,
) -> dict[str, Any]:
    """Pull a capped Instagram sample via optional gallery-dl."""
    want_n = clamp_profile_max_items(max_items)
    browser = resolve_cookies_browser(cookies_browser)
    kind_key = {
        "profile": "profile_sample",
        "stories": "stories_sample",
        "highlights": "highlights_sample",
    }.get(ig_kind, "profile_sample")

    target_url = str(url).strip()
    if ig_kind == "profile":
        user = instagram_username_from_url(target_url)
        if user:
            target_url = f"https://www.instagram.com/{user}/reels/"

    runner = gdl_runner or _default_gallery_dl_runner
    try:
        collected = runner(
            target_url,
            cache_dir=cache_dir,
            want_n=want_n,
            browser=browser,
            ig_kind=ig_kind,
        )
    except WebVideoIngestError:
        raise
    except Exception as exc:
        if is_rate_limit_error(exc):
            write_rate_limit_cooldown(cache_dir)
        raise WebVideoIngestError(humanize_ingest_failure(exc, url=url)) from exc

    if not isinstance(collected, list):
        collected = []
    items: list[dict[str, Any]] = []
    for raw in collected[:want_n]:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("video_path") or "").strip()
        if not path or not os.path.isfile(path):
            continue
        items.append({
            "video_path": os.path.abspath(path),
            "title": re.sub(r"\s+", " ", str(raw.get("title") or "reel")).strip()[:180],
            "description": str(raw.get("description") or "")[:2000],
            "uploader": str(raw.get("uploader") or ""),
            "id": str(raw.get("id") or ""),
            "webpage_url": str(raw.get("webpage_url") or "").strip(),
            "duration_seconds": _safe_float(raw.get("duration_seconds")),
            "width": raw.get("width"),
            "height": raw.get("height"),
        })

    if not items:
        if ig_kind == "stories":
            raise WebVideoIngestError(
                "No story videos landed (expired, empty, or cookies/login). "
                "Log into Instagram in Firefox, quit Firefox, retry — or paste a Reel."
            )
        raise WebVideoIngestError(
            "Sample finished but no videos landed on disk. "
            "Log into Instagram in Firefox, quit Firefox, retry — or save MP4s and import them."
        )

    first = items[0]
    title = first.get("uploader") or first.get("title") or ig_kind
    title = re.sub(r"\s+", " ", str(title)).strip()[:180]
    return {
        "ok": True,
        "kind": kind_key,
        "url": str(url).strip(),
        "intent": intent,
        "title": title,
        "description": first.get("description") or "",
        "uploader": first.get("uploader") or "",
        "extractor": "gallery-dl",
        "duration_seconds": first.get("duration_seconds"),
        "width": first.get("width"),
        "height": first.get("height"),
        "requested_height": requested_height_for_intent(intent),
        "write_subs_requested": False,
        "video_path": first["video_path"],
        "subtitle_paths": [],
        "webpage_url": str(url).strip(),
        "id": first.get("id") or "",
        "is_youtube": False,
        "is_instagram": True,
        "sampled": len(items),
        "requested_max_items": want_n,
        "items": items,
        "product_note": _PRODUCT_NOTE,
    }


def _default_gallery_dl_runner(
    target_url: str,
    *,
    cache_dir: str,
    want_n: int,
    browser: Optional[str],
    ig_kind: str,
) -> list[dict[str, Any]]:
    _gdl, gdl_config, gdl_job = _ensure_gallery_dl()
    before = set(_list_media_files(cache_dir))

    with _GDL_CONFIG_LOCK:
        clear = getattr(gdl_config, "clear", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass
        # Keep downloads flat in our cache_dir; do not nest category folders.
        gdl_config.set(("extractor",), "base-directory", cache_dir)
        gdl_config.set(("extractor",), "directory", [])
        gdl_config.set(
            ("extractor",),
            "filename",
            "{num:>03}_{media_id}.{extension}",
        )
        gdl_config.set(("extractor", "instagram"), "max-posts", int(want_n))
        gdl_config.set(("extractor", "instagram"), "videos", True)
        if browser:
            gdl_config.set(("extractor",), "cookies", [browser])
        try:
            gdl_job.DownloadJob(target_url).run()
        except Exception as exc:
            low = str(exc).lower()
            if "login" in low or "challenge" in low or "abort" in low:
                raise WebVideoIngestError(humanize_ingest_failure(exc, url=target_url)) from exc
            if is_rate_limit_error(exc):
                raise
            raise WebVideoIngestError(humanize_ingest_failure(exc, url=target_url)) from exc

    after = _list_media_files(cache_dir)
    new_paths = [p for p in after if p not in before] or after
    # Prefer video containers; skip obvious stills if both exist.
    video_exts = {".mp4", ".mkv", ".webm", ".mov"}
    videos = [p for p in new_paths if os.path.splitext(p)[1].lower() in video_exts]
    chosen = videos[:want_n] if videos else new_paths[:want_n]
    out: list[dict[str, Any]] = []
    for path in chosen:
        stem = os.path.splitext(os.path.basename(path))[0]
        out.append({
            "video_path": path,
            "title": stem,
            "description": "",
            "uploader": instagram_username_from_url(target_url) or "",
            "id": stem,
            "webpage_url": target_url,
        })
    return out


def _entry_caption(info: dict[str, Any]) -> str:
    for key in ("description", "title"):
        raw = info.get(key)
        if raw is None:
            continue
        text = re.sub(r"\s+", " ", str(raw)).strip()
        if text:
            return text[:2000]
    return ""


def _entry_video_path(info: dict[str, Any], cache_dir: str) -> str:
    video_path = ""
    requested = info.get("requested_downloads") or []
    if isinstance(requested, list) and requested:
        for item in requested:
            if isinstance(item, dict) and item.get("filepath"):
                video_path = str(item["filepath"])
                break
    if not video_path:
        for key in ("filepath", "_filename", "filename"):
            cand = str(info.get(key) or "").strip()
            if cand and os.path.isfile(cand):
                video_path = cand
                break
    if video_path and os.path.isfile(video_path):
        return os.path.abspath(video_path)
    return ""


def _meta_from_single(
    info: dict[str, Any],
    *,
    url: str,
    cache_dir: str,
    intent: str,
    want_subs: bool,
    product_note: str,
) -> dict[str, Any]:
    video_path = _entry_video_path(info, cache_dir)
    if not video_path:
        video_path = _largest_media_file(cache_dir) or ""
    if not video_path or not os.path.isfile(video_path):
        try:
            ext = str(info.get("ext") or "mp4")
            candidate = os.path.join(cache_dir, f"media.{ext}")
            if os.path.isfile(candidate):
                video_path = candidate
        except Exception:
            pass
    if not video_path or not os.path.isfile(video_path):
        raise WebVideoIngestError(
            "Download finished but no video file landed on disk. Try again or update yt-dlp."
        )

    title = str(info.get("title") or info.get("id") or "web_video").strip()
    title = re.sub(r"\s+", " ", title)[:180]
    caption = _entry_caption(info)
    return {
        "ok": True,
        "kind": "single",
        "url": str(url).strip(),
        "intent": intent,
        "title": title,
        "description": caption,
        "uploader": str(info.get("uploader") or info.get("channel") or info.get("creator") or ""),
        "extractor": str(info.get("extractor") or info.get("ie_key") or ""),
        "duration_seconds": _safe_float(info.get("duration")),
        "width": info.get("width"),
        "height": info.get("height"),
        "requested_height": requested_height_for_intent(intent),
        "write_subs_requested": bool(want_subs),
        "video_path": os.path.abspath(video_path),
        "subtitle_paths": _find_subtitle_files(cache_dir),
        "webpage_url": str(info.get("webpage_url") or url).strip(),
        "id": str(info.get("id") or ""),
        "is_youtube": is_youtube_url(url),
        "is_instagram": is_instagram_url(url),
        "product_note": product_note,
    }


def _list_media_files(directory: str) -> list[str]:
    exts = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3"}
    found: list[tuple[str, str]] = []
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            found.append((name.lower(), os.path.abspath(path)))
    except OSError:
        return []
    found.sort(key=lambda t: t[0])
    return [p for _n, p in found]


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
