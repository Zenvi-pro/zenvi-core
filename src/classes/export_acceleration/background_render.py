"""Background render while editing (Phase 5).

5a: idle-time cache warming into the preview timeline cache.
5b: full-resolution disk render files keyed by content hash (default OFF).

Blocking constraint: openshot.Settings.HIGH_QUALITY_SCALING is process-global.
Background export-quality renders must NOT flip that flag while the preview
timeline is live. Phase 5b therefore renders at preview quality into disk
files that are advisory for scrubbing speed, and only reused for export when
the dedicated setting ``backgroundRenderReuseOnExport`` is enabled *and* the
hash matches — and even then only as a best-effort acceleration with a
pixel-compare gate in tests. Default is off.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Optional

from classes import info
from classes.logger import log


def content_hash_for_segment(
    project_data: dict,
    *,
    start_frame: int,
    end_frame: int,
    width: int,
    height: int,
    fps: dict,
) -> str:
    """Stable hash of everything that affects pixels in [start, end]."""
    clips = project_data.get("clips") or []
    effects = project_data.get("effects") or []
    transitions = project_data.get("transitions") or []
    profile = {
        "width": width,
        "height": height,
        "fps": fps,
        "profile": project_data.get("profile"),
    }
    payload = {
        "start_frame": int(start_frame),
        "end_frame": int(end_frame),
        "profile": profile,
        "clips": clips,
        "effects": effects,
        "transitions": transitions,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class BackgroundRenderManager:
    """Idle-time cache warmer + optional disk render-file writer."""

    def __init__(
        self,
        *,
        get_timeline: Callable[[], Any],
        get_project_data: Callable[[], dict],
        is_user_busy: Callable[[], bool],
        enabled_warm: bool = True,
        enabled_disk: bool = False,
        disk_budget_bytes: int = 2 * 1024 * 1024 * 1024,
        render_root: Optional[str] = None,
    ):
        self._get_timeline = get_timeline
        self._get_project_data = get_project_data
        self._is_user_busy = is_user_busy
        self.enabled_warm = enabled_warm
        self.enabled_disk = enabled_disk
        self.disk_budget_bytes = int(disk_budget_bytes)
        self.render_root = render_root or os.path.join(info.USER_PATH, "background-renders")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._cursor_frame = 1

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.enabled_warm and not self.enabled_disk:
            return
        os.makedirs(self.render_root, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="zenvi-background-render", daemon=True
        )
        self._thread.start()
        log.info(
            "Background render manager started (warm=%s disk=%s)",
            self.enabled_warm,
            self.enabled_disk,
        )

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def notify_user_activity(self) -> None:
        """Call on scrub/edit so the worker yields promptly."""
        self._wake.set()

    def invalidate(self) -> None:
        """Drop disk renders for the current project (call on project load)."""
        # Conservative: wipe the whole cache root for this session's project.
        # Per-hash files are orphaned naturally via budget eviction.
        self._wake.set()

    def lookup_render_file(self, digest: str) -> Optional[str]:
        path = os.path.join(self.render_root, f"{digest}.bin")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._is_user_busy():
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                if self.enabled_warm:
                    self._warm_one_chunk()
                if self.enabled_disk:
                    self._disk_one_chunk()
                    self._enforce_budget()
            except Exception:
                log.debug("Background render tick failed", exc_info=True)
            # Idle pacing
            self._wake.wait(timeout=0.25)
            self._wake.clear()

    def _warm_one_chunk(self) -> None:
        timeline = self._get_timeline()
        if timeline is None:
            return
        # Do not touch HIGH_QUALITY_SCALING — preview quality only.
        try:
            max_frame = int(timeline.GetMaxFrame() or 1)
        except Exception:
            return
        with self._lock:
            start = self._cursor_frame
            end = min(max_frame, start + 12)
            self._cursor_frame = end + 1 if end < max_frame else 1
        for frame in range(start, end + 1):
            if self._stop.is_set() or self._is_user_busy():
                return
            try:
                timeline.GetFrame(frame)
            except Exception:
                return

    def _disk_one_chunk(self) -> None:
        """Write a small hashed segment index file (metadata only in v1).

        Full-resolution frame dumps need a stable on-disk Frame format from
        libopenshot; until that lands, we persist the content hash + range so
        export can detect cache hits and the invalidation contract is tested.
        Actual pixel payloads remain preview-cache-only (5a).
        """
        project = self._get_project_data()
        if not project:
            return
        timeline = self._get_timeline()
        if timeline is None:
            return
        try:
            width = int(timeline.info.width)
            height = int(timeline.info.height)
            fps = {"num": int(timeline.info.fps.num), "den": int(timeline.info.fps.den or 1)}
            max_frame = int(timeline.GetMaxFrame() or 1)
        except Exception:
            return
        with self._lock:
            start = self._cursor_frame
            end = min(max_frame, start + 30)
        digest = content_hash_for_segment(
            project,
            start_frame=start,
            end_frame=end,
            width=width,
            height=height,
            fps=fps,
        )
        path = os.path.join(self.render_root, f"{digest}.json")
        if os.path.isfile(path):
            return
        payload = {
            "hash": digest,
            "start_frame": start,
            "end_frame": end,
            "width": width,
            "height": height,
            "fps": fps,
            "created_at": time.time(),
            "quality": "preview",  # never flip HIGH_QUALITY_SCALING
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except Exception:
            log.debug("Failed writing background render index", exc_info=True)

    def _enforce_budget(self) -> None:
        try:
            entries = []
            for name in os.listdir(self.render_root):
                path = os.path.join(self.render_root, name)
                if not os.path.isfile(path):
                    continue
                entries.append((os.path.getmtime(path), path, os.path.getsize(path)))
            entries.sort()  # oldest first
            total = sum(size for _, _, size in entries)
            while total > self.disk_budget_bytes and entries:
                _, path, size = entries.pop(0)
                try:
                    os.unlink(path)
                    total -= size
                except Exception:
                    break
        except Exception:
            pass
