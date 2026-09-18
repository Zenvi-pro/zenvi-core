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

import os
from collections import deque

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QImage

from classes import info
from classes.logger import log
from classes.thumbnail import GetThumbPath

# Cap pending work so fast scroll/zoom cannot unbounded-queue the machine.
_MAX_PENDING_JOBS = 64


def existing_thumb_path(file_id, frame):
    """Resolve an on-disk thumbnail path without generating a new one."""
    file_id = str(file_id or "")
    frame = int(frame or 0)
    if not file_id or frame <= 0:
        return ""
    subdir = os.path.join(info.THUMBNAIL_PATH, file_id)
    candidates = [
        os.path.join(subdir, f"{frame}.png"),
    ]
    if frame == 1:
        candidates.append(os.path.join(info.THUMBNAIL_PATH, f"{file_id}.png"))
    else:
        candidates.append(os.path.join(info.THUMBNAIL_PATH, f"{file_id}-{frame}.png"))

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def load_thumbnail_image(file_id, frame):
    """Load a thumbnail off the GUI thread as a QImage (thread-safe).

    Tries an existing on-disk file first, then asks GetThumbPath to generate.
    """
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
    if not path or not os.path.exists(path):
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

    @pyqtSlot(str, str, int, int)
    def request_thumbnail(self, clip_id, file_id, frame, generation):
        """Queue a thumbnail request; drop stale / overflow jobs."""
        generation = int(generation or 0)
        if generation < self._current_generation:
            return
        if generation > self._current_generation:
            self._current_generation = generation
            # Drop jobs from older generations.
            self._queue = deque(
                job for job in self._queue if job[3] >= self._current_generation
            )
        self._queue.append((clip_id, file_id, frame, generation))
        while len(self._queue) > _MAX_PENDING_JOBS:
            self._queue.popleft()
        if not self._processing:
            self._process_next()

    @pyqtSlot()
    def clear_pending(self):
        """Discard any pending thumbnail work."""
        self._queue.clear()
        self._processing = False

    def _process_next(self):
        while self._queue:
            clip_id, file_id, frame, generation = self._queue.popleft()
            if generation < self._current_generation:
                continue
            self._processing = True
            image = QImage()
            if clip_id and file_id and frame > 0:
                image, _path = load_thumbnail_image(file_id, frame)
            self.thumbnail_ready.emit(clip_id, frame, image, generation)
        self._processing = False


class TimelineThumbnailManager(QObject):
    """Qt helper that forwards thumbnail requests to a worker thread."""

    thumbnail_ready = pyqtSignal(str, int, object, int)
    _request_job = pyqtSignal(str, str, int, int)
    _clear_jobs = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _ThumbnailWorker()
        self._worker.moveToThread(self._thread)
        self._request_job.connect(self._worker.request_thumbnail)
        self._clear_jobs.connect(self._worker.clear_pending)
        self._worker.thumbnail_ready.connect(self.thumbnail_ready)
        self._thread.start()

    def request_thumbnail(self, clip_id, file_id, frame, generation):
        """Queue a thumbnail request."""
        clip_id = str(clip_id or "")
        file_id = str(file_id or "")
        frame = int(frame or 0)
        generation = int(generation or 0)
        self._request_job.emit(clip_id, file_id, frame, generation)

    def clear_pending(self):
        """Drop any pending requests."""
        self._clear_jobs.emit()

    def shutdown(self):
        """Stop the worker thread."""
        self._clear_jobs.emit()
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
