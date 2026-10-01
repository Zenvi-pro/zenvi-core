"""Captions are placed as PNG renders of their title SVG, never cloud-indexed.

libopenshot draws SVG titles on its own (non-Qt) threads. The first time such
a thread lays out a font, Qt warns from inside its font-database lock; PyQt's
Python message handler then waits for the GIL, while the GUI thread (holding
the GIL in add_captions) waits for that lock: add_captions froze the app.
A PNG needs no fonts when libopenshot draws it.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from classes.agent_tools.receipt import parse_receipt


def test_add_captions_asks_for_raster_titles():
    from classes.agent_tools.receipt import ToolReceipt

    transcript = ToolReceipt.applied(
        "get_transcript_tool", "ok", undo_steps=0,
        data={"transcriptionSource": "local", "transcriptGeneration": 1, "clips": [{
            "clipId": "c1",
            "words": [{"index": 0, "text": "Hi", "startSec": 0.0, "endSec": 0.3,
                       "startFrame": 0, "endFrame": 9,
                       "timelineStartSec": 0.0, "timelineEndSec": 0.3}],
        }]},
    ).to_json()
    title = ToolReceipt.applied("add_title_tool", "placed", data={"file_id": "t1"}).to_json()
    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.transcript.get_transcript", return_value=transcript), \
         patch("classes.agent_tools.titles.add_title", return_value=title) as add_title:
        from classes.agent_tools.speech_extra import add_captions
        receipt = parse_receipt(add_captions(clipId="c1"))
    assert receipt["status"] == "applied"
    assert add_title.call_args.kwargs.get("raster") is True


def test_raster_title_imports_the_png_without_indexing(tmp_path, monkeypatch):
    from classes import info
    from classes.agent_tools import titles

    monkeypatch.setattr(info, "TITLE_PATH", str(tmp_path))
    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"width": 1280, "height": 720}.get(k, d)
    imported = {}

    def fake_raster(svg, png, w, h):
        assert os.path.isfile(svg) and (w, h) == (1280, 720)
        with open(png, "wb") as fh:
            fh.write(b"png")

    def add_files(paths, **kw):
        imported["paths"], imported["kw"] = paths, kw

    app.window.files_model.add_files.side_effect = add_files
    fake_file = MagicMock(id="F1")
    with patch("classes.tool_handlers._get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.tool_handlers.add_clip_to_timeline", return_value="placed") as place, \
         patch("classes.query.File.get", side_effect=[None, fake_file]), \
         patch.object(titles, "_rasterize_svg", side_effect=fake_raster):
        receipt = parse_receipt(titles.add_title(
            text="Hello there", position_seconds="1", duration_seconds="2",
            file_name="cap_test_0.svg", raster=True,
        ))
    assert receipt["status"] == "applied", receipt
    png = os.path.join(str(tmp_path), "cap_test_0.png")
    assert imported["paths"] == [png]
    assert imported["kw"].get("skip_indexing") is True
    assert not os.path.exists(os.path.join(str(tmp_path), "cap_test_0.svg"))
    assert receipt["data"]["path"] == png
    assert place.call_args.kwargs["file_id"] == "F1"


def test_plain_titles_skip_indexing_too(tmp_path, monkeypatch):
    from classes import info
    from classes.agent_tools import titles

    monkeypatch.setattr(info, "TITLE_PATH", str(tmp_path))
    app = MagicMock()
    with patch("classes.tool_handlers._get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.tool_handlers.add_clip_to_timeline", return_value="placed"), \
         patch("classes.query.File.get", side_effect=[None, MagicMock(id="F2")]):
        receipt = parse_receipt(titles.add_title(text="Lower third", file_name="lt.svg"))
    assert receipt["status"] == "applied", receipt
    paths = app.window.files_model.add_files.call_args.args[0]
    assert paths == [os.path.join(str(tmp_path), "lt.svg")]
    assert app.window.files_model.add_files.call_args.kwargs.get("skip_indexing") is True


def test_rasterize_svg_draws_the_title(tmp_path):
    """Real Qt only (ZENVI_REAL_QT=1): the PNG has visible title pixels."""
    if os.environ.get("ZENVI_REAL_QT") != "1":
        pytest.skip("needs real Qt (ZENVI_REAL_QT=1)")
    from qt_api import QApplication, QImage
    _app = QApplication.instance() or QApplication([])
    from classes.agent_tools import titles

    svg = os.path.join(str(tmp_path), "t.svg")
    with open(titles._default_template(), encoding="utf-8") as src, open(svg, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    png = os.path.join(str(tmp_path), "t.png")
    titles._rasterize_svg(svg, png, 320, 180)
    image = QImage(png)
    assert (image.width(), image.height()) == (320, 180)
    opaque = sum(1 for x in range(0, 320, 4) for y in range(0, 180, 4) if image.pixelColor(x, y).alpha() > 0)
    assert opaque > 0
