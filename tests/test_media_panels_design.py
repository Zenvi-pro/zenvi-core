"""The media panels (Project Files, Effects, Transitions, Emojis, Titles, Pexels,
Freesound) paint from the design tokens: no hard-coded colors or emoji icons, a
shared thumbnail tile whose border follows the accent, and badges whose colors
are read at paint time. Real-Qt test for the painters; the view modules need libopenshot.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5.QtWidgets")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from PyQt5.QtCore import QRect, QSize, QSortFilterProxyModel, QItemSelectionModel  # noqa: E402
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QPixmap  # noqa: E402
from PyQt5.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from PyQt5.QtWidgets import QApplication, QComboBox, QLineEdit, QListView  # noqa: E402
from PyQt5.QtWidgets import QStyle, QStyleOptionViewItem  # noqa: E402

from themes.cosmic.tokens import TOKENS  # noqa: E402

_APP = QApplication.instance() or QApplication([])

MEDIA_PANEL_SOURCES = [
    "windows/pexels_dock.py",
    "windows/freesound_dock.py",
    "windows/views/stock_search_view.py",
    "windows/views/indexing_badge.py",
    "windows/views/generation_badge.py",
    "windows/views/files_listview.py",
    "windows/views/files_thumbnail_overlay.py",
    "windows/views/files_treeview.py",
    "windows/views/effects_listview.py",
    "windows/views/transitions_listview.py",
    "windows/views/emojis_listview.py",
    "windows/views/titles_listview.py",
]
HEX_OR_RGB = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgba?\(\s*\d|QColor\(\s*\d")
PICTOGRAPHS = re.compile("[\U0001F300-\U0001FAFF\u23E9-\u23FA]")


def test_media_panels_have_no_hard_coded_colors():
    offenders = []
    for rel in MEDIA_PANEL_SOURCES:
        for number, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1):
            if HEX_OR_RGB.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == []


def test_media_panels_have_no_emoji_icons():
    offenders = []
    for rel in MEDIA_PANEL_SOURCES:
        for number, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1):
            if PICTOGRAPHS.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == []


def _paint_on_canvas(paint, width, height, background=None):
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor(background) if background else 0)
    painter = QPainter(image)
    paint(painter)
    painter.end()
    return image


def _colors(image):
    return {image.pixelColor(x, y).name() for y in range(image.height()) for x in range(image.width())}


@pytest.mark.parametrize("state_name, token", [
    ("SUCCESS", "success"),
    ("FAILED", "danger"),
    ("RUNNING", "accent"),
    ("PENDING", "text_faint"),
    ("SKIPPED", "text_muted"),
])
def test_status_badge_paints_its_token_color(monkeypatch, state_name, token):
    from classes import indexing_status
    from windows.views import indexing_badge

    monkeypatch.setitem(TOKENS, token, "#00ff00")
    rect = QRect(4, 4, 12, 12)
    image = _paint_on_canvas(
        lambda p: indexing_badge.paint_status_badge(p, rect, getattr(indexing_status, state_name), 0),
        20, 20, background="#000000",
    )
    assert "#00ff00" in _colors(image)


def test_proxy_badge_glyph_paints_its_token_color(monkeypatch):
    from windows.views.files_thumbnail_overlay import paint_proxy_badge

    monkeypatch.setitem(TOKENS, "text_muted", "#00ff00")
    image = _paint_on_canvas(lambda p: paint_proxy_badge(p, QRect(0, 0, 100, 65), "ready"), 100, 65,
                             background="#000000")
    assert "#00ff00" in _colors(image)


def _tile_view(icon_color="#3b5b7a"):
    from windows.views.files_thumbnail_overlay import TileDelegate

    view = QListView()
    model = QStandardItemModel()
    item = QStandardItem("Interview_take3.mp4")
    pixmap = QPixmap(100, 65)
    pixmap.fill(QColor(icon_color))
    item.setIcon(QIcon(pixmap))
    model.appendRow(item)
    view.setModel(model)
    delegate = TileDelegate(view)
    return view, delegate, model.index(0, 0)


def _paint_tile(view, delegate, index, state, size=(120, 100)):
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, *size)
    option.state = state
    option.font = view.font()
    return _paint_on_canvas(lambda p: delegate.paint(p, option, index), *size)


def test_selected_tile_border_is_the_accent_token(monkeypatch):
    view, delegate, index = _tile_view()
    # Frame is inset 4px in the cell, so its left edge is column 4.
    assert _paint_tile(view, delegate, index, QStyle.State_Selected).pixelColor(4, 50) == QColor(TOKENS["accent"])
    monkeypatch.setitem(TOKENS, "accent", "#ff00ff")
    assert _paint_tile(view, delegate, index, QStyle.State_Selected).pixelColor(4, 50) == QColor("#ff00ff")


def test_hover_tile_gets_a_line_strong_border_and_idle_tiles_get_none(monkeypatch):
    view, delegate, index = _tile_view()
    assert _paint_tile(view, delegate, index, QStyle.State_MouseOver).pixelColor(4, 50) == QColor(TOKENS["line_strong"])
    assert _paint_tile(view, delegate, index, QStyle.State_None).pixelColor(4, 50).alpha() == 0


def test_selected_tile_fill_is_the_accent_soft_token(monkeypatch):
    monkeypatch.setitem(TOKENS, "accent_soft", "#123456")
    view, delegate, index = _tile_view()
    # Above the thumbnail (which starts 8px in), inside the frame's straight top edge.
    assert _paint_tile(view, delegate, index, QStyle.State_Selected).pixelColor(60, 6) == QColor("#123456")


def _fake_app(window):
    return SimpleNamespace(
        window=window,
        _tr=lambda text: text,
        get_settings=lambda: SimpleNamespace(get=lambda key, default=None: default, set=lambda key, value: None),
    )


class _Signal:
    def connect(self, *args, **kwargs):
        pass


def test_list_views_install_the_tile_delegate(monkeypatch):
    pytest.importorskip("openshot")
    from windows.views.files_thumbnail_overlay import TileDelegate

    def proxy_for(names):
        std = QStandardItemModel()
        for name in names:
            std.appendRow(QStandardItem(name))
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(std)
        return proxy, QItemSelectionModel(proxy)

    effects = importlib.import_module("windows.views.effects_listview")
    transitions = importlib.import_module("windows.views.transitions_listview")
    emojis = importlib.import_module("windows.views.emojis_listview")
    titles = importlib.import_module("windows.views.titles_listview")

    proxy, selection = proxy_for(["Fade"])
    window = SimpleNamespace(effectsFilter=QLineEdit(), refreshEffectsSignal=_Signal())
    monkeypatch.setattr(effects, "get_app", lambda: _fake_app(window))
    assert isinstance(effects.EffectsListView(SimpleNamespace(list_proxy_model=proxy, list_selection_model=selection)).itemDelegate(), TileDelegate)

    proxy, selection = proxy_for(["Wipe"])
    window = SimpleNamespace(transitionsFilter=QLineEdit(), refreshTransitionsSignal=_Signal())
    monkeypatch.setattr(transitions, "get_app", lambda: _fake_app(window))
    model = SimpleNamespace(list_proxy_model=proxy, list_selection_model=selection, proxy_model=proxy)
    assert isinstance(transitions.TransitionsListView(model).itemDelegate(), TileDelegate)

    proxy, _selection = proxy_for(["Smile"])
    window = SimpleNamespace(emojisFilter=QLineEdit(), emojiFilterGroup=QComboBox())
    monkeypatch.setattr(emojis, "get_app", lambda: _fake_app(window))
    model = SimpleNamespace(proxy_model=proxy, group_model=proxy, ModelRefreshed=_Signal(),
                            emoji_groups=[], set_group_filter=lambda group: None)
    assert isinstance(emojis.EmojisListView(model).itemDelegate(), TileDelegate)

    class _TitlesModel:
        def __init__(self, win):
            self.proxy_model, _ = proxy_for(["Lower third"])

        def update_model(self):
            pass

    monkeypatch.setattr(titles, "TitlesModel", _TitlesModel)
    assert isinstance(titles.TitlesListView(window=SimpleNamespace()).itemDelegate(), TileDelegate)


@pytest.mark.parametrize("module_name, class_name, card_args", [
    ("windows.pexels_dock", "_VideoCard",
     {"id": 1, "duration": 65, "width": 1920, "height": 1080, "user_name": "Ava Lin"}),
    ("windows.freesound_dock", "_SoundCard",
     {"id": 2, "name": "Rain on tin roof", "username": "soundscaper", "duration": 65.0, "tags": []}),
])
def test_stock_card_hover_border_is_a_line_strong_hairline(module_name, class_name, card_args):
    card = getattr(importlib.import_module(module_name), class_name)(card_args)
    card.underMouse = lambda: True
    image = card.grab().toImage()
    assert image.pixelColor(0, card.height() // 2) == QColor(TOKENS["line_strong"])


def test_generation_progress_fills_with_the_accent_token(monkeypatch):
    from windows.views import generation_badge
    from windows.views.indexing_badge import IndexingBadgeDelegate

    queue = SimpleNamespace(get_file_badge=lambda file_id: {"status": "running", "progress": 50},
                            get_job=lambda job_id: None)
    monkeypatch.setattr(generation_badge, "get_app", lambda: _fake_app(SimpleNamespace(generation_queue=queue)))

    view = QListView()
    model = QStandardItemModel(0, 6)
    row = [QStandardItem("clip.mp4") for _ in range(6)]
    row[0].setIcon(QIcon(QPixmap(100, 65)))
    row[5].setText("6")
    model.appendRow(row)
    view.setModel(model)
    view.setIconSize(QSize(100, 65))
    delegate = IndexingBadgeDelegate(view)
    view.setItemDelegate(delegate)

    option = view.viewOptions()
    option.rect = QRect(0, 0, 120, 100)
    index = model.index(0, 0)
    image = _paint_on_canvas(lambda p: generation_badge.paint_generation_progress(delegate, p, option, index),
                             120, 100, background="#000000")
    assert QColor(TOKENS["accent"]).name() in _colors(image)


def test_tile_delegates_size_items_to_the_tile_grid():
    # QListView sizes items from the delegate's sizeHint; the default hint gave tiles
    # smaller than tile_grid_size, so thumbnails and hover areas were misplaced
    from windows.views.files_thumbnail_overlay import TileDelegate, tile_grid_size

    view = QListView()
    view.setIconSize(QSize(128, 72))
    delegate = TileDelegate(view)
    hint = delegate.sizeHint(QStyleOptionViewItem(), QStandardItemModel().index(0, 0))
    assert hint == tile_grid_size(QSize(128, 72))


def test_freesound_card_stops_loading_when_the_waveform_fails(monkeypatch):
    freesound = importlib.import_module("windows.freesound_dock")
    with_wave = freesound._SoundCard({"id": 1, "name": "rain", "duration": 3.0,
                                      "images": {"waveform_m": "https://x/w.png"}})
    no_wave = freesound._SoundCard({"id": 2, "name": "wind", "duration": 3.0, "images": {}})
    assert with_wave._waveform_pending and not no_wave._waveform_pending

    import requests

    def offline(*_a, **_k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", offline)
    done = []
    runnable = freesound._WaveformRunnable(1, "https://x/w.png")
    runnable.signals.finished.connect(done.append)
    runnable.run()
    assert done == [1]
    with_wave.set_waveform_pending(False)
    assert not with_wave._waveform_pending
