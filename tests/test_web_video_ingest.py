"""Unit tests for web_video_ingest (yt-dlp wrapper) — no network."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from classes.web_video_ingest import (
    WebVideoIngestError,
    assert_public_http_url,
    cache_satisfies_request,
    check_rate_limit_cooldown,
    download_web_video,
    format_selector_for_intent,
    is_probable_web_video_url,
    is_youtube_url,
    load_cached_download,
    normalize_intent,
    purge_expired_cache,
    url_cache_key,
    write_rate_limit_cooldown,
)


def test_url_helpers():
    assert is_probable_web_video_url("https://youtu.be/dQw4w9WgXcQ")
    assert is_youtube_url("https://www.youtube.com/watch?v=abc")
    assert is_youtube_url("https://youtu.be/abc")
    assert not is_probable_web_video_url("/tmp/local.mp4")
    assert not is_probable_web_video_url("file:///tmp/x.mp4")
    assert url_cache_key("https://a") == url_cache_key("https://a")
    assert url_cache_key("https://a") != url_cache_key("https://b")


def test_normalize_intent_aliases():
    assert normalize_intent("ref") == "reference"
    assert normalize_intent("grade") == "reference"
    assert normalize_intent("place") == "timeline"
    assert normalize_intent("remake") == "recreate"
    # Vague words must NOT force timeline (colour ask safety)
    with pytest.raises(WebVideoIngestError):
        normalize_intent("edit")
    with pytest.raises(WebVideoIngestError):
        normalize_intent("explode")


def test_format_selector_caps_reference_lower_than_timeline():
    ref = format_selector_for_intent("reference")
    tl = format_selector_for_intent("timeline")
    assert "720" in ref
    assert "1080" in tl


def test_purge_expired_cache(tmp_path):
    root = tmp_path / "cache"
    old = root / "old"
    new = root / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "f.txt").write_text("x")
    (new / "f.txt").write_text("y")
    # Make old very old
    old_age = 100
    os.utime(old, (old_age, old_age))
    removed = purge_expired_cache(str(root), ttl_seconds=3600, now=1_000_000)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_download_web_video_with_fake_ydl(tmp_path):
    cache = tmp_path / "dl"
    cache.mkdir()
    media = cache / "media.mp4"
    media.write_bytes(b"fake-mp4-bytes")

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            assert download is True
            assert "youtube" in url
            return {
                "title": "Test Clip",
                "id": "abc123",
                "ext": "mp4",
                "duration": 12.5,
                "extractor": "youtube",
                "webpage_url": url,
                "requested_downloads": [{"filepath": str(media)}],
            }

    meta = download_web_video(
        "https://www.youtube.com/watch?v=abc123",
        cache_dir=str(cache),
        intent="reference",
        write_subs=False,
        ydl_factory=FakeYDL,
    )
    assert meta["ok"] is True
    assert meta["title"] == "Test Clip"
    assert meta["intent"] == "reference"
    assert meta["is_youtube"] is True
    assert os.path.isfile(meta["video_path"])
    assert (cache / "ingest.json").is_file()
    sidecar = json.loads((cache / "ingest.json").read_text())
    assert sidecar["id"] == "abc123"


def test_download_rejects_local_path(tmp_path):
    with pytest.raises(WebVideoIngestError):
        download_web_video(
            "/tmp/local.mp4",
            cache_dir=str(tmp_path),
            intent="reference",
            ydl_factory=lambda o: None,
        )


def test_ingest_web_video_tool_reference_imports_without_place(monkeypatch, tmp_path):
    import json
    import types
    from classes import tool_handlers as th
    from classes import web_video_ingest as wvi

    media = tmp_path / "media.mp4"
    media.write_bytes(b"x")

    monkeypatch.setattr(
        wvi,
        "download_web_video",
        lambda url, cache_dir, intent="reference", write_subs=False, ydl_factory=None: {
            "ok": True,
            "url": url,
            "intent": intent,
            "title": "Look Ref",
            "extractor": "youtube",
            "duration_seconds": 30.0,
            "video_path": str(media),
            "subtitle_paths": [],
            "webpage_url": url,
            "id": "id1",
            "is_youtube": True,
            "product_note": "note",
        },
    )
    monkeypatch.setattr(wvi, "purge_expired_cache", lambda *a, **k: 0)
    monkeypatch.setattr(wvi, "cache_root", lambda *_: str(tmp_path / "cache"))
    monkeypatch.setattr(wvi, "url_cache_key", lambda *_: "key")

    class FakeFile:
        def __init__(self):
            self.id = "file-xyz"
            self.data = {"tags": "", "path": str(media)}

        def save(self):
            return None

    monkeypatch.setattr(th, "_import_generated_video", lambda *_a, **_k: (FakeFile(), None))
    placed = []

    def _fake_place(**kwargs):
        placed.append(kwargs)
        return "placed"

    monkeypatch.setattr(th, "add_clip_to_timeline", _fake_place)

    out = json.loads(
        th.ingest_web_video(
            url="https://youtu.be/abc",
            intent="reference",
        )
    )
    assert out["ok"] is True
    assert out["file_id"] == "file-xyz"
    assert out["placed"] is False
    assert placed == []
    assert "match_color_to_reference" in out["next"]

    out2 = json.loads(
        th.ingest_web_video(
            url="https://youtu.be/abc",
            intent="timeline",
        )
    )
    assert out2["placed"] is True
    assert placed and placed[0]["file_id"] == "file-xyz"

    out3 = json.loads(
        th.ingest_web_video(
            url="https://youtu.be/abc",
            intent="recreate",
            place="true",  # must still not place
        )
    )
    assert out3["placed"] is False
    assert out3["intent"] == "recreate"
    assert "recreate" in out3["next"].lower() or "stock" in out3["next"].lower() or "video_gen" in out3["next"].lower()


def test_ingest_web_video_registered():
    from classes.tool_handlers import AGENT_TOOL_HANDLERS, TOOL_DISPLAY_LABELS

    assert "ingest_web_video_tool" in AGENT_TOOL_HANDLERS
    assert "ingest_web_video_tool" in TOOL_DISPLAY_LABELS


def test_humanize_ingest_failure_is_brief():
    from classes.web_video_ingest import WebVideoIngestError, humanize_ingest_failure

    assert "ffmpeg" in humanize_ingest_failure(RuntimeError("Merging requires ffmpeg")).lower()
    unavail = humanize_ingest_failure(RuntimeError("Video unavailable")).lower()
    assert "unavailable" in unavail
    assert "mp4" in unavail or "import" in unavail
    restricted = humanize_ingest_failure(RuntimeError("This video is age-restricted")).lower()
    assert "restricted" in restricted or "sign-in" in restricted or "private" in restricted
    assert "mp4" in restricted or "import" in restricted
    brief = humanize_ingest_failure(WebVideoIngestError("Paste a link."))
    assert brief == "Paste a link."
    assert "\n" not in humanize_ingest_failure(RuntimeError("line1\nline2\nTRACE"))
    rate = humanize_ingest_failure(RuntimeError("HTTP Error 429: Too Many Requests")).lower()
    assert "rate-limited" in rate or "rate limit" in rate


def test_load_cached_download_reuses_sidecar(tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    media = cache / "media.mp4"
    media.write_bytes(b"cached")
    (cache / "ingest.json").write_text(json.dumps({
        "ok": True,
        "video_path": str(media),
        "title": "Cached",
        "id": "z1",
        "is_youtube": True,
    }))
    meta = load_cached_download(str(cache))
    assert meta is not None
    assert meta["from_cache"] is True
    assert meta["video_path"] == str(media)


def test_rate_limit_cooldown_blocks_then_expires(tmp_path):
    cache = str(tmp_path / "cool")
    write_rate_limit_cooldown(cache, now=1_000.0)
    msg = check_rate_limit_cooldown(cache, now=1_010.0)
    assert msg and "rate-limiting" in msg.lower()
    assert check_rate_limit_cooldown(cache, now=1_000.0 + 11 * 60) is None


def test_download_reuses_cache_without_ydl(tmp_path):
    cache = tmp_path / "dl"
    cache.mkdir()
    media = cache / "media.mp4"
    media.write_bytes(b"x")
    (cache / "ingest.json").write_text(json.dumps({
        "video_path": str(media),
        "title": "Hit",
        "id": "h1",
        "is_youtube": True,
    }))

    def _boom(opts):
        raise AssertionError("yt-dlp must not run when cache is complete")

    meta = download_web_video(
        "https://www.youtube.com/watch?v=h1",
        cache_dir=str(cache),
        intent="timeline",
        ydl_factory=_boom,
    )
    assert meta["from_cache"] is True
    assert meta["intent"] == "timeline"


def test_format_selector_without_ffmpeg_avoids_plus_merge():
    from classes.web_video_ingest import format_selector_for_intent

    sel = format_selector_for_intent("reference", has_ffmpeg=False)
    assert "+" not in sel
    assert "720" in sel


def test_ingest_fail_json_does_not_raise(monkeypatch):
    import json
    from classes import tool_handlers as th
    from classes import web_video_ingest as wvi

    monkeypatch.setattr(
        wvi,
        "download_web_video",
        lambda *a, **k: (_ for _ in ()).throw(wvi.WebVideoIngestError("ffmpeg is required to combine YouTube video+audio.")),
    )
    monkeypatch.setattr(wvi, "purge_expired_cache", lambda *a, **k: 0)
    monkeypatch.setattr(wvi, "cache_root", lambda *_: "/tmp/zenvi-cache-test")
    monkeypatch.setattr(wvi, "url_cache_key", lambda *_: "k")

    out = json.loads(th.ingest_web_video(url="https://youtu.be/abc", intent="reference"))
    assert out["ok"] is False
    assert "ffmpeg" in out["error"].lower()
    assert "do not retry" in out["next"].lower()


def _slow_ingest_setup(monkeypatch, tmp_path, *, sync_budget=0.2, poll_budget=2.0):
    import threading
    from classes import tool_handlers as th
    from classes import web_video_ingest as wvi

    media = tmp_path / "media.mp4"
    media.write_bytes(b"x")
    started = threading.Event()
    release = threading.Event()

    def _slow_download(url, cache_dir, intent="reference", write_subs=False, ydl_factory=None):
        started.set()
        assert release.wait(timeout=10)
        return {
            "ok": True,
            "url": url,
            "intent": intent,
            "title": "Slow",
            "extractor": "youtube",
            "duration_seconds": 120.0,
            "video_path": str(media),
            "subtitle_paths": [],
            "webpage_url": url,
            "id": "slow1",
            "is_youtube": True,
            "product_note": "note",
        }

    monkeypatch.setattr(wvi, "download_web_video", _slow_download)
    monkeypatch.setattr(wvi, "purge_expired_cache", lambda *a, **k: 0)
    monkeypatch.setattr(wvi, "cache_root", lambda *_: str(tmp_path / "cache"))
    monkeypatch.setattr(wvi, "url_cache_key", lambda *_: "slow-key")
    monkeypatch.setattr(th, "_INGEST_SYNC_BUDGET_S", sync_budget)
    monkeypatch.setattr(th, "_INGEST_POLL_BUDGET_S", poll_budget)

    class FakeFile:
        def __init__(self):
            self.id = "file-slow"
            self.data = {"tags": "", "path": str(media)}

        def save(self):
            return None

    monkeypatch.setattr(th, "_import_generated_video", lambda *_a, **_k: (FakeFile(), None))
    monkeypatch.setattr(th, "add_clip_to_timeline", lambda **_k: "placed")

    with th._ingest_jobs_lock:
        th._ingest_jobs.clear()
        th._ingest_inflight.clear()

    return th, started, release


def test_ingest_returns_running_then_completes_via_job_id(monkeypatch, tmp_path):
    """Long pulls must return before MCP ~60s timeout; job_id long-polls to finish."""
    import json
    import time
    from classes import tool_handlers as th

    th, started, release = _slow_ingest_setup(
        monkeypatch, tmp_path, sync_budget=0.2, poll_budget=0.2
    )

    first = json.loads(
        th.ingest_web_video(url="https://youtu.be/slow", intent="timeline")
    )
    assert first["ok"] is True
    assert first["status"] == "running"
    assert first["job_id"]
    assert started.wait(timeout=2)

    # Same URL while running must reuse the job, not start a second download.
    again = json.loads(
        th.ingest_web_video(url="https://youtu.be/slow", intent="timeline")
    )
    assert again["job_id"] == first["job_id"]
    assert again["status"] == "running"

    release.set()
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        final = json.loads(th.ingest_web_video(job_id=first["job_id"]))
        if final.get("status") != "running":
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "completed"
    assert final["ok"] is True
    assert final["file_id"] == "file-slow"
    assert final["placed"] is True


def test_ingest_job_id_returns_as_soon_as_worker_finishes(monkeypatch, tmp_path):
    """Long-poll must wake on the event, not wait out the full poll budget."""
    import json
    import threading
    import time
    from classes import tool_handlers as th

    th, started, release = _slow_ingest_setup(
        monkeypatch, tmp_path, sync_budget=0.15, poll_budget=3.0
    )

    first = json.loads(
        th.ingest_web_video(url="https://youtu.be/slow", intent="timeline")
    )
    assert first["status"] == "running"
    assert started.wait(timeout=2)

    result_box = []

    def _poll():
        t0 = time.time()
        out = json.loads(th.ingest_web_video(job_id=first["job_id"]))
        result_box.append((out, time.time() - t0))

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    time.sleep(0.1)
    release.set()
    t.join(timeout=5)
    assert result_box, "job_id wait did not return"
    out, elapsed = result_box[0]
    assert out["status"] == "completed"
    assert out["file_id"] == "file-slow"
    assert elapsed < 1.5, f"waited {elapsed:.2f}s; should wake on event"


def test_ingest_job_id_still_running_after_poll_budget(monkeypatch, tmp_path):
    """If the worker is not done, long-poll returns running after the budget."""
    import json
    import time
    from classes import tool_handlers as th

    th, started, release = _slow_ingest_setup(
        monkeypatch, tmp_path, sync_budget=0.1, poll_budget=0.35
    )

    first = json.loads(
        th.ingest_web_video(url="https://youtu.be/slow", intent="timeline")
    )
    assert first["status"] == "running"
    assert started.wait(timeout=2)

    t0 = time.time()
    mid = json.loads(th.ingest_web_video(job_id=first["job_id"]))
    elapsed = time.time() - t0
    assert mid["status"] == "running"
    assert mid["job_id"] == first["job_id"]
    assert elapsed >= 0.3
    assert "45s" in mid["next"] or "waits" in mid["next"].lower()

    release.set()
    # Drain so the daemon thread does not leak into other tests.
    deadline = time.time() + 5
    while time.time() < deadline:
        if json.loads(th.ingest_web_video(job_id=first["job_id"])).get("status") != "running":
            break
        time.sleep(0.05)


def test_server_instructions_mention_ingest_job_poll():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS

    text = SERVER_INSTRUCTIONS.lower()
    assert "job_id" in text
    assert "timed out" in text or "timeout" in text
    assert "45s" in text or "waits until" in text
    assert "every ~15s" not in text


def test_assert_public_http_url_rejects_loopback(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(WebVideoIngestError, match="private or local"):
        assert_public_http_url("https://evil.example/video.mp4")


def test_assert_public_http_url_rejects_private_ip(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0))],
    )
    with pytest.raises(WebVideoIngestError, match="private or local"):
        assert_public_http_url("http://internal.corp/clip.mp4")


def test_cache_satisfies_request_rejects_low_height_and_missing_subs():
    low = {"height": 720, "requested_height": 720, "subtitle_paths": [], "write_subs_requested": False}
    assert cache_satisfies_request(low, intent="reference", write_subs=False) is False
    assert cache_satisfies_request(low, intent="timeline", write_subs=False) is False
    hi = {"height": 1080, "requested_height": 1080, "subtitle_paths": ["/a.vtt"], "write_subs_requested": True}
    assert cache_satisfies_request(hi, intent="timeline", write_subs=False) is True
    assert cache_satisfies_request(hi, intent="reference", write_subs=True) is True
    tried = {"height": 720, "requested_height": 720, "subtitle_paths": [], "write_subs_requested": True}
    assert cache_satisfies_request(tried, intent="reference", write_subs=True) is True


def test_download_skips_cache_when_height_too_low(tmp_path, monkeypatch):
    import classes.web_video_ingest as wvi

    cache = tmp_path / "dl"
    cache.mkdir()
    media = cache / "media.mp4"
    media.write_bytes(b"x")
    (cache / "ingest.json").write_text(json.dumps({
        "video_path": str(media),
        "title": "Low",
        "id": "h1",
        "is_youtube": True,
        "height": 720,
        "requested_height": 720,
        "write_subs_requested": False,
        "subtitle_paths": [],
    }))
    calls = {"n": 0}

    @contextmanager
    def _ydl(opts):
        calls["n"] += 1

        class _Y:
            def extract_info(self, url, download=True):
                return {
                    "id": "h1",
                    "title": "Fresh",
                    "ext": "mp4",
                    "duration": 1,
                    "width": 1920,
                    "height": 1080,
                    "webpage_url": url,
                    "requested_downloads": [{"filepath": str(media)}],
                }

        yield _Y()

    monkeypatch.setattr(wvi, "assert_public_http_url", lambda url: None)
    monkeypatch.setattr(wvi, "ffmpeg_available", lambda: True)
    meta = download_web_video(
        "https://www.youtube.com/watch?v=h1",
        cache_dir=str(cache),
        intent="timeline",
        ydl_factory=_ydl,
    )
    assert calls["n"] == 1
    assert meta.get("from_cache") is not True
    assert meta["height"] == 1080
    assert meta["requested_height"] == 1080
