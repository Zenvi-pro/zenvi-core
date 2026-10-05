"""Real Qt parts of the Premiere handoff: titles rendered to transparent PNG stills
(final_cut_pro.render_still_png) and the export options dialog.

Run with ZENVI_REAL_QT=1 QT_QPA_PLATFORM=offscreen (skipped under the headless stub).
"""

import os

import pytest

pytest.importorskip("PyQt5.QtSvg")
from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QApplication.instance() or QApplication([])


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080">'
       '<rect x="760" y="440" width="400" height="200" fill="#ff5a36"/>'
       '<text x="100" y="900" font-size="72" fill="#ffffff">Launch day</text></svg>')


def test_svg_title_renders_to_a_transparent_png(qapp, tmp_path):
    from classes.exporters.final_cut_pro import render_still_png
    svg = tmp_path / "Title.svg"
    svg.write_text(SVG)
    out = tmp_path / "Edit_media" / "titles" / "Title-T1.png"
    render_still_png(str(svg), str(out), 1920, 1080, "svg")
    image = QImage(str(out))
    assert (image.width(), image.height()) == (1920, 1080) and image.hasAlphaChannel()
    assert image.pixelColor(10, 10).alpha() == 0                       # transparent around the title
    c = image.pixelColor(960, 540)
    assert (c.red(), c.green(), c.blue(), c.alpha()) == (255, 90, 54, 255)
    assert not list(out.parent.glob("*.partial"))


def test_a_broken_svg_raises_and_leaves_no_file(qapp, tmp_path):
    from classes.exporters.final_cut_pro import ExportError, render_still_png
    bad = tmp_path / "Broken.svg"
    bad.write_text("<svg")
    out = tmp_path / "titles" / "Broken.png"
    with pytest.raises(ExportError, match="not a valid SVG"):
        render_still_png(str(bad), str(out), 1920, 1080, "svg")
    assert not out.exists() and not list((tmp_path / "titles").glob("*"))


def test_other_stills_are_converted_to_png(qapp, tmp_path):
    from classes.exporters.final_cut_pro import render_still_png
    src = QImage(64, 32, QImage.Format_ARGB32)
    src.fill(QColor(0, 128, 255, 128))
    jpeg_like = tmp_path / "frame.bmp"
    assert src.save(str(jpeg_like), "BMP")
    out = tmp_path / "stills" / "frame.png"
    render_still_png(str(jpeg_like), str(out), 64, 32, "image")
    image = QImage(str(out))
    assert (image.width(), image.height()) == (64, 32)
    assert image.pixelColor(5, 5).blue() > 200


def test_export_dialog_offers_the_options(qapp, monkeypatch, tmp_path):
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff import premiere
    monkeypatch.setattr(fcp, "default_export_path", lambda ext=".xml", suffix="": str(tmp_path / f"Trip{suffix}{ext}"))
    dialog, path_edit, collect, open_folder = premiere.build_export_dialog(None)
    assert dialog.objectName() == "premiereExportDialog"
    assert path_edit.text() == str(tmp_path / "Trip (Premiere).xml")
    assert not collect.isChecked() and not open_folder.isChecked()
    assert "Premiere" in dialog.windowTitle()
    dialog.deleteLater()
