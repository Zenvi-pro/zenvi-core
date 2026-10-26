"""Files dock stock search: no network under 3 chars, ~700 ms debounce, Enter
cancels the pending timer, an unchanged query is not re-sent, and a music
card with an Openverse UUID id downloads."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402


_APP = QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def qapp():
    if not hasattr(_APP, "get_settings"):
        _APP.get_settings = lambda: MagicMock(get=lambda key, default=None: default)
    if not hasattr(_APP, "_tr"):
        _APP._tr = lambda s: s
    yield _APP


def _window(text):
    import types

    if "windows.views.timeline" not in sys.modules:
        stub = types.ModuleType("windows.views.timeline")
        stub.TimelineView = type("TimelineView", (), {})
        sys.modules["windows.views.timeline"] = stub
    from windows.main_window import MainWindow

    view = MagicMock()
    fake = SimpleNamespace(
        stockSearchView=view,
        _stock_search_timer=MagicMock(),
        filesFilter=MagicMock(text=MagicMock(return_value=text)),
    )
    return MainWindow, fake, view


def test_two_chars_never_search_three_chars_debounce(qapp):
    mw, fake, view = _window("oc")
    mw._on_files_filter_changed(fake, "oc")
    fake._stock_search_timer.start.assert_not_called()
    view.clear_stock.assert_called_once()

    mw._on_files_filter_changed(fake, "oce")
    fake._stock_search_timer.start.assert_called_once_with(700)


def test_enter_cancels_the_pending_timer(qapp):
    mw, fake, view = _window("ocean")
    mw._on_files_search(fake)
    fake._stock_search_timer.stop.assert_called_once()
    view.run_search.assert_called_once_with("ocean")


def test_unchanged_query_is_not_resent(qapp):
    from windows.views.stock_search_view import StockSearchView

    v = StockSearchView()
    with patch.object(v, "_start_search") as start:
        v.run_search("ocean")
        v.run_search(" ocean ")
        assert start.call_count == 2  # footage + music, once
        v.clear_stock()
        v.run_search("ocean")
        assert start.call_count == 4


def test_music_download_worker_carries_a_uuid_id(qapp):
    from windows.freesound_dock import _DownloadWorker

    seen = []
    w = _DownloadWorker("3b4580e1-uuid", "https://x/a.mp3", "a")
    w.finished.connect(lambda sid, path, err: seen.append((sid, path, err)))
    backend = MagicMock()
    backend.freesound_download.return_value = {"local_path": "/tmp/a.mp3"}
    with patch("classes.api_client.get_backend_client", return_value=backend), \
         patch("classes.credits_client.check_operation", return_value=(0, 0, "")), \
         patch("classes.credits_client.charge_operation_on_success"):
        w.run()
    assert seen == [("3b4580e1-uuid", "/tmp/a.mp3", "")]
