"""
 @file
 @brief Lightweight thumbnail request manager for the QWidget timeline backend.
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

from collections import OrderedDict, deque

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QImage

from classes.logger import log
from classes.thumbnail import GetThumbPath, resolve_thumbnail_path

# Cap pending work so fast scroll/zoom cannot unbounded-queue the machine.
_MAX_PENDING_JOBS = 64


def _file_fingerprint(file_id):
    """Best-effort fingerprint lookup for media-cache thumbnail paths."""
    try:
        from classes.query import File

        f = File.get(id=file_id)
        if f and isinstance(getattr(f, "data", None), dict):
            return f.data.get("fingerprint")
    except Exception:
        return None
    return None


def existing_thumb_path(file_id, frame, fingerprint=None):
    """Resolve an on-disk thumbnail path without generating a new one."""
    file_id = str(file_id or "")
    frame = int(frame or 0)
    if not file_id or frame <= 0:
        return ""
    if fingerprint is None:
        fingerprint = _file_fingerprint(file_id)
    return resolve_thumbnail_path(file_id, frame, fingerprint=fingerprint) or ""


def load_thumbnail_image(file_id, frame, *, clear_cache=False):
    """Load a thumbnail off the GUI thread as a QImage (thread-safe).

    Tries an existing on-disk file first, then asks GetThumbPath to generate.
    """
    path = ""
    if clear_cache:
        try:
            path = GetThumbPath(file_id, frame, clear_cache=True) or ""
        except Exception:
            log.warning(
                "Thumbnail force-refresh failed for file_id=%s frame=%s",
                file_id,
                frame,
                exc_info=1,
            )
            path = ""
    else:
        path = existing_thumb_path(file_id, frame)
        if not path:
            try:
                path = GetThumbPath(file_id, frame) or ""
            except Exception:
                log.warning(
                    "Thumbnail request failed for file_id=%s frame=%s",
                    file_id,
                    frame,
                    exc_info=1,
                )
                path = ""
    if not path:
        return QImage(), ""
    # Existence re-check belongs here (worker thread), not in paintEvent.
    import os

    if not os.path.exists(path):
        return QImage(), ""
    image = QImage(path)
    if image.isNull():
        return QImage(), path
    return image, path


class _ThumbnailWorker(QObject):
    """Worker object that resolves thumbnail images on a background thread."""

    thumbnail_ready = pyqtSignal(str, int, object, int)

    def __init__(self):
        super().__init__()
        self._queue = deque()
        self._processing = False
        self._current_generation = 0
        self._drain_scheduled = False

    @pyqtSlot(object)
    def enqueue_batch(self, jobs):
        """Accept a bounded batch from the manager; schedule one drain."""
        if not jobs:
            return
        for job in jobs:
            if not isinstance(job, (tuple, list)) or len(job) < 5:
                continue
            clip_id, file_id, frame, generation, clear_cache = job[:5]
            generation = int(generation or 0)
            if generation < self._current_generation:
                continue
            if generation > self._current_generation:
                self._current_generation = generation
                self._queue = deque(
                    j for j in self._queue if j[3] >= self._current_generation
                )
            self._queue.append(
                (
                    str(clip_id or ""),
                    str(file_id or ""),
                    int(frame or 0),
                    generation,
                    bool(clear_cache),
                )
            )
        while len(self._queue) > _MAX_PENDING_JOBS:
            self._queue.popleft()
        self._schedule_drain()

    @pyqtSlot()
    def clear_pending(self):
        """Discard any pending thumbnail work."""
        self._queue.clear()
        self._processing = False
        self._drain_scheduled = False

    def _schedule_drain(self):
        if self._drain_scheduled or self._processing:
            return
        self._drain_scheduled = True
        # Queued so new enqueue_batch slots can coalesce into the deque first.
        QTimer.singleShot(0, self._process_next)

    def _process_next(self):
        self._drain_scheduled = False
        if self._processing:
            return
        self._processing = True
        try:
            while self._queue:
                clip_id, file_id, frame, generation, clear_cache = self._queue.popleft()
                if generation < self._current_generation:
                    continue
                image = QImage()
                if clip_id and file_id and frame > 0:
                    image, _path = load_thumbnail_image(
                        file_id, frame, clear_cache=clear_cache
                    )
                self.thumbnail_ready.emit(clip_id, frame, image, generation)
        finally:
            self._processing = False
            if self._queue:
                self._schedule_drain()


class TimelineThumbnailManager(QObject):
    """Qt helper that forwards thumbnail requests to a worker thread.

    Requests are coalesced and bounded on the caller thread before any
    cross-thread Qt queued delivery, so paint storms cannot unbounded-queue
    the worker event loop.
    """

    thumbnail_ready = pyqtSignal(str, int, object, int)
    _request_batch = pyqtSignal(object)
    _clear_jobs = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _ThumbnailWorker()
        self._worker.moveToThread(self._thread)
        self._pending = OrderedDict()
        self._emit_scheduled = False
        self._request_batch.connect(self._worker.enqueue_batch)
        self._clear_jobs.connect(self._worker.clear_pending)
        self._worker.thumbnail_ready.connect(self.thumbnail_ready)
        self._thread.start()

    def request_thumbnail(
        self, clip_id, file_id, frame, generation, clear_cache=False
    ):
        """Queue a thumbnail request (coalesced, ≤64 outstanding)."""
        clip_id = str(clip_id or "")
        file_id = str(file_id or "")
        frame = int(frame or 0)
        generation = int(generation or 0)
        key = (clip_id, frame)
        # Newer generation / force-refresh for the same slot replaces older.
        if key in self._pending:
            self._pending.pop(key, None)
        self._pending[key] = (
            clip_id,
            file_id,
            frame,
            generation,
            bool(clear_cache),
        )
        while len(self._pending) > _MAX_PENDING_JOBS:
            self._pending.popitem(last=False)
        if not self._emit_scheduled:
            self._emit_scheduled = True
            QTimer.singleShot(0, self._flush_pending)

    def _flush_pending(self):
        self._emit_scheduled = False
        if not self._pending:
            return
        jobs = list(self._pending.values())
        self._pending.clear()
        self._request_batch.emit(jobs)

    def clear_pending(self):
        """Drop any pending requests."""
        self._pending.clear()
        self._emit_scheduled = False
        self._clear_jobs.emit()

    def shutdown(self):
        """Stop the worker thread."""
        self.clear_pending()
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
