"""Paint-path thumbnail helpers must not touch the filesystem."""

from __future__ import annotations

import importlib.util
import pathlib
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock

from windows.views.timeline_backend.paint.byte_lru import ByteBudgetLRU


def _load_thumbnails_module():
    """Load thumbnails.py without importing qwidget package (avoids theme/Qt)."""
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "windows"
        / "views"
        / "timeline_backend"
        / "qwidget"
        / "thumbnails.py"
    )
    spec = importlib.util.spec_from_file_location(
        "timeline_thumbnails_under_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _StubClipPainter:
    """Minimal stand-in that uses the real _get_thumbnail_pixmap body."""

    def __init__(self):
        self.thumb_cache = ByteBudgetLRU()
        self._thumb_pending = {}
        self._thumb_regions = {}
        self._thumb_missing_logged = set()
        self.w = SimpleNamespace(
            thumbnail_manager=MagicMock(),
        )

    from windows.views.timeline_backend.paint.clip import ClipPainter

    _get_thumbnail_pixmap = ClipPainter._get_thumbnail_pixmap
    _existing_thumb_path = ClipPainter._existing_thumb_path


def test_get_thumbnail_pixmap_queues_without_filesystem(monkeypatch):
    painter = _StubClipPainter()

    def boom(*_a, **_k):
        raise AssertionError("paint path must not call os.path.exists")

    monkeypatch.setattr("os.path.exists", boom)

    from PyQt5.QtCore import QRectF

    result = painter._get_thumbnail_pixmap(
        "clip1", "file1", 1, QRectF(), generation=1, allow_request=True
    )
    assert result is None
    painter.w.thumbnail_manager.request_thumbnail.assert_called_once()
    assert painter._thumb_pending[("clip1", 1)] == 1

    # Null cache entry must not re-queue forever after a failed load.
    null_pix = MagicMock()
    null_pix.isNull.return_value = True
    painter.thumb_cache[("clip1", 1)] = null_pix
    painter._thumb_pending.pop(("clip1", 1), None)
    painter.w.thumbnail_manager.request_thumbnail.reset_mock()
    result2 = painter._get_thumbnail_pixmap(
        "clip1", "file1", 1, QRectF(), generation=1, allow_request=True
    )
    assert result2 is None
    painter.w.thumbnail_manager.request_thumbnail.assert_not_called()


def test_existing_thumb_path_is_inert():
    painter = _StubClipPainter()
    assert painter._existing_thumb_path("file", 1) == ""


def test_thumbnail_worker_caps_queue():
    mod = _load_thumbnails_module()
    worker = mod._ThumbnailWorker()
    worker._schedule_drain = lambda: None
    jobs = [
        (f"c{i}", "f", i + 1, 1, False)
        for i in range(mod._MAX_PENDING_JOBS + 20)
    ]
    worker.enqueue_batch(jobs)
    assert len(worker._queue) <= mod._MAX_PENDING_JOBS


def test_thumbnail_worker_drops_stale_generation():
    mod = _load_thumbnails_module()
    worker = mod._ThumbnailWorker()
    worker._schedule_drain = lambda: None
    worker.enqueue_batch([("c1", "f", 1, 1, False)])
    worker.enqueue_batch([("c2", "f", 2, 2, False)])
    assert all(job[3] >= 2 for job in worker._queue)


def test_manager_coalesces_before_cross_thread_emit(monkeypatch):
    """Paint storms must not unbounded-queue Qt deliveries to the worker."""
    mod = _load_thumbnails_module()

    class _FakeTimer:
        pending = []

        @classmethod
        def singleShot(cls, _ms, cb):
            cls.pending.append(cb)

        @classmethod
        def flush(cls):
            while cls.pending:
                cls.pending.pop(0)()

    _FakeTimer.pending = []
    monkeypatch.setattr(mod, "QTimer", _FakeTimer)

    emitted = []

    manager = mod.TimelineThumbnailManager.__new__(mod.TimelineThumbnailManager)
    QObject = __import__("PyQt5.QtCore", fromlist=["QObject"]).QObject
    QObject.__init__(manager)
    manager._pending = OrderedDict()
    manager._emit_scheduled = False
    manager._request_batch = SimpleNamespace(
        emit=lambda jobs: emitted.append(list(jobs))
    )
    manager._clear_jobs = MagicMock()
    manager._thread = MagicMock()
    manager._worker = MagicMock()

    for i in range(mod._MAX_PENDING_JOBS + 40):
        manager.request_thumbnail(f"c{i}", "f", i + 1, generation=1)
    _FakeTimer.flush()

    assert len(emitted) == 1
    assert len(emitted[0]) <= mod._MAX_PENDING_JOBS
