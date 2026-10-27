"""The recording dock, scope panels and color wheels take their colors from the
cosmic design tokens, and the dock keeps Start Recording in view in a short panel.
Real-Qt test (offscreen). The dock and painters are the real classes; only get_app is stubbed.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tokenize
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtGui import QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from themes.cosmic.tokens import TOKENS  # noqa: E402
import windows.audio_recording as audio_recording  # noqa: E402
import windows.color_grade_editor as color_grade_editor  # noqa: E402
import windows.recording_widgets as recording_widgets  # noqa: E402
import windows.scope_panel as scope_panel  # noqa: E402

RESTYLED = [
    "src/windows/audio_recording.py",
    "src/windows/recording_widgets.py",
    "src/windows/color_grade_editor.py",
    "src/windows/scope_panel.py",
    "src/windows/region.py",
]
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


class _Translator:
    def _tr(self, text):
        return text


@pytest.fixture
def dock(monkeypatch):
    monkeypatch.setattr(audio_recording, "get_app", lambda: _Translator())
    monkeypatch.setattr(recording_widgets, "get_app", lambda: _Translator())
    content = audio_recording.AudioRecordingDockContent(None)
    yield content
    content.deleteLater()


def test_restyled_files_have_no_hex_color_literals():
    offenders = []
    for name in RESTYLED:
        with (SRC.parent / name).open("rb") as source:
            for token in tokenize.tokenize(source.readline):
                if token.type == tokenize.STRING and HEX.search(token.string):
                    offenders.append("%s:%d %s" % (name, token.start[0], token.string[:60]))
    assert offenders == []


def test_start_recording_stays_visible_in_a_short_panel(dock):
    dock.resize(340, 260)
    dock.show()
    _APP.processEvents()
    button_bottom = dock.record_button.mapTo(dock, QPoint(0, dock.record_button.height())).y()
    assert button_bottom <= dock.height()


def test_source_cards_and_sections_scroll_inside_a_scroll_area(dock):
    for widget in (dock.mic_card, dock.screen_card, dock.camera_card,
                   dock.mic_section, dock.screen_section, dock.camera_section):
        assert dock.scroll_area.isAncestorOf(widget)


def test_source_cards_use_16px_monochrome_icons(dock):
    for card in (dock.mic_card, dock.screen_card, dock.camera_card):
        pixmap = card.findChild(QLabel, "recordingCardIcon").pixmap()
        assert pixmap is not None and not pixmap.isNull()
        assert (pixmap.width(), pixmap.height()) == (16, 16)


def test_start_recording_is_the_light_primary_button(dock):
    dock.resize(340, 260)
    dock.show()
    _APP.processEvents()
    dock._set_record_button_idle()  # showEvent re-syncs the button, so enable it after
    dock.record_button.setEnabled(True)
    image = dock.record_button.grab().toImage()
    assert image.pixelColor(12, image.height() // 2).name() == TOKENS["primary"]


@pytest.mark.parametrize("scope_cls, sentence", [
    (scope_panel.WaveformWidget, "waveform."),
    (scope_panel.HistogramWidget, "histogram."),
    (scope_panel.VectorscopeWidget, "vectorscope."),
    (scope_panel.AudioMeterWidget, "levels."),
])
def test_empty_scope_paints_one_text_faint_sentence(monkeypatch, scope_cls, sentence):
    painted = []

    class SpyPainter(QPainter):
        def drawText(self, *args):
            painted.append((args[-1], self.pen().color().name()))
            super().drawText(*args)

    monkeypatch.setattr(scope_panel, "QPainter", SpyPainter)
    widget = scope_cls()
    widget.resize(240, 160)
    widget.grab()
    assert len(painted) == 1
    text, color = painted[0]
    assert text.startswith("Play or select a clip to see ") and text.endswith(sentence)
    assert color == TOKENS["text_faint"]


def test_color_wheel_outline_comes_from_line_strong(monkeypatch):
    ellipse_pens = []

    class SpyPainter(QPainter):
        def drawEllipse(self, *args):
            ellipse_pens.append(self.pen().color().name())
            super().drawEllipse(*args)

    monkeypatch.setattr(color_grade_editor, "QPainter", SpyPainter)
    wheel = color_grade_editor.ColorWheelControl(None, title="")
    wheel.resize(170, 170)
    wheel.grab()
    assert ellipse_pens[0] == TOKENS["line_strong"]


def test_many_audio_channels_stay_inside_the_meter(monkeypatch):
    # Bars had an 8px floor and were centred, so a 16-channel meter lost its outer channels
    from PyQt5.QtGui import QColor, QImage

    meter = scope_panel.AudioMeterWidget()
    meter.resize(60, 80)
    meter.update_data({"present": True, "channels": 16,  # only the first channel has signal
                       "summary": {"peak": [0.0] * 16, "rms": [0.9] + [0.0] * 15, "clipped_samples": [0] * 16}})
    image = QImage(meter.size(), QImage.Format_ARGB32)
    image.fill(QColor("#ff00ff"))
    meter.render(image)
    greens = [x for x in range(meter.width()) if QColor(image.pixel(x, 79)).green() > 150
              and QColor(image.pixel(x, 79)).red() < 100]
    assert greens and min(greens) < 8  # channel 1's level bar is on screen at the left edge


def test_vectorscope_labels_fade_when_idle():
    source = Path(importlib.import_module("windows.scope_panel").__file__).read_text(encoding="utf-8")
    assert "base.blue(), 255" not in source  # a fixed 255 alpha left nothing to fade toward
