"""Real Qt for the After Effects export: titles and wipe images rendered with QSvgRenderer, read with QImage.

Needs real PyQt5 (and libopenshot on PYTHONPATH for the tool_handlers import):
``ZENVI_REAL_QT=1 PYTHONPATH=~/zenvi-deps-1.0/python .venv/bin/python -m pytest tests/test_handoff_ae_qt.py``.
Skipped under the headless stub.
"""

import os
import shutil

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
QtGui = pytest.importorskip("PyQt5.QtGui")

from ae_project import ProjectBuilder  # noqa: E402
from classes.handoff import after_effects_export as X  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _render(name, folder, tmp_path, w=1920, h=1080):
    png = str(tmp_path / (name + ".png"))
    X.render_svg_png(os.path.join(SRC, folder, name + ".svg"), png, w, h)
    return png, QtGui.QImage(png)


def test_a_title_renders_to_a_transparent_png_at_the_requested_size(tmp_path):
    path, image = _render("Gold_1", "titles", tmp_path)
    assert (image.width(), image.height()) == (1920, 1080)
    assert QtGui.QColor.fromRgba(image.pixel(0, 0)).alpha() == 0
    assert any(QtGui.QColor.fromRgba(image.pixel(x, 540)).alpha() > 0 for x in range(0, 1920, 8))


def test_fade_is_a_uniform_black_mask_and_wipes_are_images(tmp_path):
    fade, _ = _render("fade", os.path.join("transitions", "common"), tmp_path)
    info = X.analyze_mask_image(fade)
    assert info.uniform and info.gray == 0 and info.alpha == 255
    wipe, _ = _render("wipe_left_to_right", os.path.join("transitions", "common"), tmp_path)
    assert not X.analyze_mask_image(wipe).uniform
    jpg = os.path.join(SRC, "transitions", "extra", "4_squares_leftt_barr.jpg")
    info = X.analyze_mask_image(jpg)
    assert not info.uniform and (info.width, info.height) == (720, 576)


def test_an_export_with_real_rendering(tmp_path):
    b = ProjectBuilder()
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"a")
    (media / "b.mp4").write_bytes(b"b")
    a = b.add_file("video", path=str(media / "a.mp4"))
    c = b.add_file("video", path=str(media / "b.mp4"))
    b.add_clip(a, track=1, position=0.0, start=0.0, end=5.0)
    b.add_clip(c, track=1, position=4.0, start=0.0, end=4.0)
    fade = shutil.copy(os.path.join(SRC, "transitions", "common", "fade.svg"), tmp_path / "fade.svg")
    wipe = shutil.copy(os.path.join(SRC, "transitions", "common", "wipe_left_to_right.svg"), tmp_path / "wipe.svg")
    b.add_transition(track=1, position=4.0, duration=1.0, mask=str(fade))
    b.add_transition(track=1, position=7.0, duration=1.0, mask=str(wipe))
    gold = shutil.copy(os.path.join(SRC, "titles", "Gold_1.svg"), tmp_path / "Gold_1.svg")
    b.add_clip(b.add_title(str(gold)), track=2, position=1.0, start=0.0, end=3.0)
    result = X.export_after_effects(b.snapshot(), str(tmp_path / "out"), created="test", generator="Zenvi test")
    assert QtGui.QImage(str(tmp_path / "out" / "titles" / "Gold_1.png")).width() == 1920
    assert QtGui.QImage(str(tmp_path / "out" / "masks" / "wipe.png")).width() == 1920
    assert result.stats["transitions_fade"] == 1 and result.stats["transitions_wipe"] == 1
    assert [t["mode"] for t in result.titles] == ["png"]
