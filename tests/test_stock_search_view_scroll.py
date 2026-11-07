"""Files dock (#393): with many project files the dock scrolls past them all to Music Gen."""

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from PyQt5.QtWidgets import QApplication, QListView  # noqa: E402

_APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("width", [600, 300])
def test_many_files_scroll_down_to_the_last_file_and_music_gen(width):
    from windows.views.stock_search_view import StockSearchView

    files = QListView()
    files.setViewMode(QListView.IconMode)
    files.setResizeMode(QListView.Adjust)
    files.setWrapping(True)
    model = QStandardItemModel()
    for i in range(60):
        model.appendRow(QStandardItem("clip %d.mp4" % i))
    files.setModel(model)

    v = StockSearchView()
    v.set_files_view(files)
    v.resize(600, 500)
    v.show()
    _APP.processEvents()
    v.resize(width, 500)   # a narrower dock wraps the files into more rows
    for _ in range(3):
        _APP.processEvents()

    last = files.visualRect(model.index(59, 0))
    assert last.isValid() and last.bottom() <= files.height(), "the last files are cut off"
    bar = v._scroll.verticalScrollBar()
    assert bar.maximum() > 0, "no scrollbar although the files overflow the dock"
    bar.setValue(bar.maximum())
    _APP.processEvents()
    composer = v._music_composer
    bottom = composer.mapTo(v._scroll.viewport(), composer.rect().bottomLeft()).y()
    assert bottom <= v._scroll.viewport().height(), "Music Gen is out of reach at the end of the scroll"
    v.close()
