"""Files dock stock results: every result gets a card and its picture, an older search
cannot paint the newer one, the next page appends, provider downloads never collide,
and Project Files says so when the search hides every file."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtGui import QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _videos(n, start=0):
    return [{"id": 100 + i, "duration": 5, "image": "https://cdn/%d.jpg" % i, "video_files": [],
             "video_pictures": [], "user_name": "u"} for i in range(start, start + n)]


def _sounds(n, start=0):
    return [{"id": "uuid-%d" % i, "name": "Track %d" % i, "duration": 30, "username": "a",
             "license": "CC BY 3.0", "attribution": "credit", "thumbnail": "https://ov/%d/thumb/" % i,
             "previews": {"hq_mp3": "https://j/%d.mp3" % i}} for i in range(start, start + n)]


@pytest.fixture
def view():
    from windows.views.stock_search_view import StockSearchView

    v = StockSearchView()
    v.resize(600, 800)
    v._pool = MagicMock()
    with patch.object(v, "_start_search"):
        v.run_search("ocean")
    return v


def _started(v):
    return [c.args[0] for c in v._pool.start.call_args_list]


def test_every_footage_result_gets_a_card_and_a_thumbnail(view):
    view._on_footage_results({"videos": _videos(6), "total_results": 6, "provider": "pixabay"})
    assert view._footage_grid.count() == 6
    assert len(_started(view)) == 6


def test_a_thumbnail_from_an_older_search_is_ignored(view):
    view._on_footage_results({"videos": _videos(1), "total_results": 1})
    card = view._cards["v100"]
    key = _started(view)[0]._video_id
    image = QImage(4, 4, QImage.Format_RGB32)
    with patch.object(card, "set_thumbnail") as paint:
        view._on_thumbnail_ready(key, image, view._search_gen - 1)
        paint.assert_not_called()
        view._on_thumbnail_ready(key, image, view._search_gen)
        paint.assert_called_once()


def test_music_cards_load_openverse_album_art(view):
    view._on_music_results({"sounds": _sounds(3), "count": 3})
    assert view._music_grid.count() == 3
    runnables = _started(view)
    assert [r._url for r in runnables] == ["https://ov/0/thumb/", "https://ov/1/thumb/", "https://ov/2/thumb/"]
    card = view._cards["suuid-1"]
    with patch.object(card, "set_waveform") as paint:
        view._on_waveform_ready(runnables[1]._sound_id, QImage(4, 4, QImage.Format_RGB32), view._search_gen)
        paint.assert_called_once()


def test_load_more_appends_the_next_page(view):
    view._on_footage_results({"videos": _videos(16), "total_results": 40, "per_page": 16})
    assert not view._footage_more.isHidden()
    with patch.object(view, "_start_search") as start:
        view._footage_more.click()
    worker = start.call_args.args[0]
    assert worker._page == 2 and worker._query == "ocean"
    start.call_args.args[1]({"videos": _videos(16, 16), "total_results": 40, "per_page": 16})
    assert view._footage_grid.count() == 32
    view._on_music_results({"sounds": _sounds(16), "count": 16})
    assert view._music_more.isHidden()   # everything is shown


def test_a_late_page_from_an_older_search_is_dropped(view):
    view._on_footage_results({"videos": _videos(16), "total_results": 40, "per_page": 16})
    with patch.object(view, "_start_search") as start:
        view._footage_more.click()
    late = start.call_args.args[1]
    with patch.object(view, "_start_search"):
        view.run_search("forest")
    late({"videos": _videos(16, 16), "total_results": 40, "per_page": 16})
    assert view._footage_grid.count() == 0


def test_pixabay_downloads_are_named_for_their_provider(view):
    view._on_footage_results({"videos": [dict(_videos(1)[0], video_files=[
        {"quality": "hd", "file_type": "video/mp4", "link": "https://cdn/x.mp4"}])], "provider": "pixabay"})
    with patch.object(view, "_start_download") as dl:
        view._on_footage_clicked(view._cards["v100"]._video)
    assert dl.call_args.args[1]._filename == "pixabay_100"


def test_project_files_say_when_the_search_hides_every_file(view):
    from PyQt5.QtCore import QStringListModel
    from PyQt5.QtWidgets import QListView

    files = QListView()
    src = QStringListModel(["clip.mp4", "song.mp3"])
    files.setModel(QStringListModel([]))   # the filtered view: nothing matches
    files.files_model = MagicMock()
    files.files_model.proxy_model.sourceModel.return_value = src
    view.set_files_view(files)
    assert not view._files_empty.isHidden() and "match" in view._files_empty.text()
    files.setModel(QStringListModel(["clip.mp4"]))
    view._refit_files()
    assert view._files_empty.isHidden()


def test_a_failed_search_can_be_retried_with_the_same_query(view):
    view._on_music_results({"sounds": [], "rate_limited": True, "error": "Stock search is busy, try again in 30s."})
    with patch.object(view, "_start_search") as start:
        view.run_search("ocean")
    assert start.call_count == 2


def test_a_music_import_keeps_its_credit_line(view):
    view._on_music_results({"sounds": _sounds(1), "count": 1})
    project_file = MagicMock(data={})
    with patch("classes.query.File.get", return_value=project_file), \
         patch.object(view, "_add_to_project"):
        view._on_download_done("suuid-0", "/tmp/a.mp3", "")
    assert project_file.data["stock_credit"] == "credit"
    project_file.save.assert_called_once()


def test_the_freesound_dock_handles_openverse_ids_and_album_art():
    from windows.freesound_dock import FreesoundDock, _DownloadWorker

    dock = FreesoundDock()
    dock._wave_pool = MagicMock()
    dock._current_page, dock._search_gen = 1, 1
    dock._on_search_done({"sounds": _sounds(1), "count": 1}) if hasattr(dock, "_on_search_done") else \
        dock._on_results({"sounds": _sounds(1), "count": 1})
    runnable = dock._wave_pool.start.call_args.args[0]
    assert runnable._url == "https://ov/0/thumb/"
    worker = _DownloadWorker("uuid-0", "https://j/0.mp3", "a")
    worker.finished.connect(dock._on_download_done)   # an int-typed slot raises TypeError here
    worker.finished.emit("uuid-0", "", "boom")
