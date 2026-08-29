"""Files dock card-view drop target (empty bin / StockSearchView)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import QPoint, Qt, QUrl, QMimeData  # noqa: E402
from PyQt5.QtGui import QDropEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QListView  # noqa: E402

from classes.file_drop import EMPTY_FILES_DROP_MIN_HEIGHT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_empty_files_view_keeps_drop_zone_and_imports(qapp, tmp_path, monkeypatch):
    from windows.views.stock_search_view import StockSearchView

    clip = tmp_path / "drop_me.mp4"
    clip.write_bytes(b"x")

    dummy = QListView()
    seen = []

    def process_urls(urls, **kw):
        seen.append([u.toLocalFile() for u in urls])
        return []

    dummy.files_model = SimpleNamespace(
        ModelRefreshed=SimpleNamespace(connect=lambda *a, **k: None),
        process_urls=process_urls,
    )

    app = MagicMock()
    monkeypatch.setattr("windows.views.stock_search_view.get_app", lambda: app)

    view = StockSearchView()
    view.set_files_view(dummy)
    assert dummy.height() >= EMPTY_FILES_DROP_MIN_HEIGHT

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(clip))])
    event = QDropEvent(QPoint(8, 8), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    view._import_os_drop(event)

    assert seen
    assert os.path.normpath(seen[0][0]) == os.path.normpath(str(clip))

    seen.clear()
    from PyQt5.QtGui import QDragEnterEvent
    enter = QDragEnterEvent(QPoint(8, 8), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    assert view.eventFilter(view._files_holder, enter) is True
    assert enter.isAccepted()
    drop = QDropEvent(QPoint(8, 8), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    assert view.eventFilter(view._files_holder, drop) is True
    assert seen

    clip_mime = QMimeData()
    clip_mime.setHtml("clip")
    clip_mime.setText('["id"]')
    from classes.file_drop import mime_has_file_drop
    assert mime_has_file_drop(clip_mime) is False
    enter_clip = QDragEnterEvent(QPoint(8, 8), Qt.CopyAction, clip_mime, Qt.LeftButton, Qt.NoModifier)
    assert view.eventFilter(view._files_holder, enter_clip) is False

    view.deleteLater()
    dummy.deleteLater()
