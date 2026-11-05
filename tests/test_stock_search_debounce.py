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


@pytest.fixture
def isolated_main_window(monkeypatch):
    """windows.main_window imported against a stub timeline, both undone after the test.

    monkeypatch restores sys.modules, so neither the stub timeline nor a main_window
    built on it leaks into later tests in the same process.
    """
    import types

    stub = types.ModuleType("windows.views.timeline")
    stub.TimelineView = type("TimelineView", (), {})
    monkeypatch.setitem(sys.modules, "windows.views.timeline", stub)
    monkeypatch.delitem(sys.modules, "windows.main_window", raising=False)
    from windows.main_window import MainWindow

    yield MainWindow
    sys.modules.pop("windows.main_window", None)


def _window(MainWindow, text):
    view = MagicMock()
    fake = SimpleNamespace(
        stockSearchView=view,
        _stock_search_timer=MagicMock(),
        filesFilter=MagicMock(text=MagicMock(return_value=text)),
    )
    return MainWindow, fake, view


def test_two_chars_never_search_three_chars_debounce(qapp, isolated_main_window):
    mw, fake, view = _window(isolated_main_window, "oc")
    mw._on_files_filter_changed(fake, "oc")
    fake._stock_search_timer.start.assert_not_called()
    view.clear_stock.assert_called_once()

    mw._on_files_filter_changed(fake, "oce")
    fake._stock_search_timer.start.assert_called_once_with(700)


def test_enter_cancels_the_pending_timer(qapp, isolated_main_window):
    mw, fake, view = _window(isolated_main_window, "ocean")
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


def test_showing_the_files_view_inside_a_dock_does_not_crash(qapp):
    from qt_api import QWidget, QVBoxLayout, QGridLayout
    from windows.views.stock_search_view import StockSearchView

    host = QWidget()
    grid = QGridLayout(host)
    column = QVBoxLayout()
    grid.addLayout(column, 0, 0)
    view = StockSearchView()
    column.addWidget(view)
    host.resize(420, 640)
    host.show()
    qapp.processEvents()
    assert not view._music_gen_header.isHidden()


def test_the_composer_stays_in_the_files_dock_with_no_search(qapp):
    from windows.views.stock_search_view import FILE_CANVAS_MIN, StockSearchView

    v = StockSearchView()
    assert v._footage_header.isHidden() and v._music_header.isHidden()
    assert not v._music_prompt.isHidden()
    assert v._music_lyrics.isHidden()
    assert [v._music_kind.itemData(i) for i in range(v._music_kind.count())] == ["bed", "song"]
    assert not v._music_engine.isHidden()
    assert not v._music_generate.isHidden()
    assert v._gen_progress.isHidden()
    v.resize(420, 400)
    v.show()
    qapp.processEvents()
    assert v._files_block.minimumHeight() == FILE_CANVAS_MIN
    v.resize(420, 800)
    qapp.processEvents()
    assert v._files_block.minimumHeight() == max(FILE_CANVAS_MIN, v._scroll.viewport().height() // 2)
    assert v._music_gen_header.text() == "Music Gen"
    assert v._music_gen_header.objectName() == "sectionHeader"
    assert not v._music_rule.isHidden()
    assert v._music_rule.y() >= v._files_block.y() + v._files_block.minimumHeight() - 2
    v._music_prompt.setText("lofi piano")
    v._music_engine.setCurrentIndex(1)
    with patch.object(v, "_start_local_bed") as bed:
        v._music_generate.click()
    bed.assert_called_once_with("lofi piano", "yue2", "", "bed")


def test_no_stock_music_fills_the_prompt_and_does_not_start(qapp):
    from windows.views.stock_search_view import StockSearchView

    v = StockSearchView()
    v._query = "ocean waves"
    with patch.object(v, "_start_local_bed") as bed:
        v._on_music_results({"sounds": [], "count": 0})
        bed.assert_not_called()
        assert v._music_prompt.text() == "ocean waves"
        v._on_music_results({"sounds": [], "rate_limited": True, "error": "Stock search is busy, try again in 30s."})
        bed.assert_not_called()


def _stub_music(monkeypatch, state):
    """classes.music_local and ai_generation_music stand-ins: consent is asked only while not installed."""
    import types

    import classes

    music_local = types.ModuleType("classes.music_local")
    music_local.generate = lambda engine, style, lyrics, duration: state["generated"].append(engine) or "t.mp3"
    music = types.ModuleType("classes.editor_tools.ai_generation_music")
    music.Declined = type("Declined", (Exception,), {})

    def ensure_installed(engine, progress=None):
        if engine in state["installed"]:
            return
        state["asked"].append(engine)
        if not state["answers"].pop(0):
            raise music.Declined("the user declined the music download")
        state["installed"].add(engine)

    music.ensure_installed = ensure_installed
    package = types.ModuleType("classes.editor_tools")
    package.__path__ = []
    package.ai_generation_music = music
    # "from classes import music_local" reads the package attribute first: patch both.
    monkeypatch.setattr(classes, "music_local", music_local, raising=False)
    monkeypatch.setattr(classes, "editor_tools", package, raising=False)
    monkeypatch.setitem(sys.modules, "classes.music_local", music_local)
    monkeypatch.setitem(sys.modules, "classes.editor_tools", package)
    monkeypatch.setitem(sys.modules, "classes.editor_tools.ai_generation_music", music)


def test_after_a_declined_download_generate_asks_again_and_runs_once_installed(qapp, monkeypatch):
    # #360 blocked the engine for the session after a "No": Generate stayed enabled and did
    # nothing, even once the model was installed another way.
    from windows.views.stock_search_view import StockSearchView

    state = {"installed": set(), "asked": [], "answers": [False, False, True], "generated": []}
    _stub_music(monkeypatch, state)
    v = StockSearchView()

    def run_here(worker, on_done):   # the real worker, on this thread, straight back to the panel
        worker.finished.connect(on_done)
        worker.run()

    with patch.object(v, "_start_search", side_effect=run_here), \
            patch.object(v, "_add_to_project", return_value=True):
        v._start_local_bed("rain", "ace_step")                      # No
        assert "declined" in v._gen_status.text().lower()
        v._start_local_bed("rain", "ace_step")                      # asked again, No again
        assert state["asked"] == ["ace_step", "ace_step"] and state["generated"] == []
        state["installed"].add("ace_step")                          # installed some other way
        v._start_local_bed("rain", "ace_step")                      # runs without asking
        v._start_local_bed("rain", "yue2")                          # the other engine: its own question
    assert state["asked"] == ["ace_step", "ace_step", "yue2"]
    assert state["generated"] == ["ace_step", "yue2"]
    assert "added it to project files" in v._gen_status.text().lower()
