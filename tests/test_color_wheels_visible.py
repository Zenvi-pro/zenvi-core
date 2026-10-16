"""Color Wheels stay visible on the dark theme, whose palette has Base and Mid both black."""

import os

import pytest


@pytest.fixture
def dark_wheels(monkeypatch):
    if os.environ.get("ZENVI_REAL_QT") != "1":
        pytest.skip("needs real Qt (ZENVI_REAL_QT=1)")
    import types

    from qt_api import QApplication, QColor, QPalette

    app = QApplication.instance() or QApplication([])
    from classes import app as zapp

    fake = types.SimpleNamespace(_tr=lambda s: s, window=None, theme_manager=None)
    monkeypatch.setattr(zapp, "get_app", lambda: fake)
    import windows.color_grade_editor as cge

    palette = QPalette()
    black, light = QColor("#000000"), QColor("#d4d4d4")
    for role in (QPalette.Base, QPalette.Mid, QPalette.Window, QPalette.Dark):
        palette.setColor(role, black)
    palette.setColor(QPalette.Text, light)
    palette.setColor(QPalette.WindowText, light)
    return app, cge, palette


def _visible_pixels(widget, background):
    image = widget.grab().toImage()
    return sum(1 for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
               if abs(image.pixelColor(x, y).lightness() - background.lightness()) > 30)


def test_a_disabled_wheel_is_drawn_not_black_on_black(dark_wheels):
    app, cge, palette = dark_wheels
    from qt_api import QColor

    wheel = cge.ColorWheelControl(title="Global")
    wheel.setPalette(palette)
    wheel.resize(200, 200)
    wheel.setEnabled(False)
    # The ring alone is ~15% of the square; a lone puck and title are well under 3%.
    assert _visible_pixels(wheel, QColor("#000000")) > (100 * 100) * 0.08


def test_the_enabled_wheel_outline_shows_on_a_black_base(dark_wheels):
    app, cge, palette = dark_wheels
    from qt_api import QColor

    wheel = cge.ColorWheelControl()
    wheel.setPalette(palette)
    wheel.resize(200, 200)
    image = wheel.grab().toImage()
    # A point on the dashed crosshair, between the centre puck and the ring.
    crosshair = [image.pixelColor(100, y).lightness() for y in range(40, 90)]
    assert max(crosshair) - QColor("#000000").lightness() > 30


def test_the_panel_says_what_to_select_while_disabled(dark_wheels):
    app, cge, _palette = dark_wheels
    panel = cge.ColorGradeWheelsPanel(frame_number=1)
    panel.setEnabled(False)
    assert not panel.hint.isHidden() and "Color Grade" in panel.hint.text()
    panel.setEnabled(True)
    assert panel.hint.isHidden()
