"""Captions are placed as PNG renders of their title SVG, never cloud-indexed.

libopenshot draws SVG titles on its own (non-Qt) threads. The first time such
a thread lays out a font, Qt warns from inside its font-database lock; PyQt's
Python message handler then waits for the GIL, while the GUI thread (holding
the GIL in add_captions) waits for that lock: add_captions froze the app.
A PNG needs no fonts when libopenshot draws it.
"""

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


# ---------------------------------------------------------------------------
# Captions go on one track above the captioned clip and anything covering it.
# ---------------------------------------------------------------------------

def _clip(cid, layer, position, start=0.0, end=5.0):
    return MagicMock(id=cid, data={"layer": layer, "position": position, "start": start, "end": end})


def _caption_track_for(layers, clips, clip_id="speech"):
    from classes.agent_tools.speech_extra import _caption_track

    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"layers": [{"number": n} for n in layers]}.get(k, d)
    by_id = {c.id: c for c in clips}
    cues = [{"startSec": 1.0, "endSec": 2.0}, {"startSec": 2.0, "endSec": 4.0}]
    with patch("classes.query.Clip.get", side_effect=lambda id=None: by_id.get(id)), \
         patch("classes.query.Clip.filter", return_value=clips):
        track = _caption_track(app, clip_id, cues)
    return track, app.window.ensure_tracks_for_layers


def test_caption_track_reuses_the_free_track_above():
    track, ensure = _caption_track_for([1000000, 2000000, 3000000], [_clip("speech", 1000000, 0.0)])
    assert track == "2000000"
    ensure.assert_not_called()


def test_caption_track_goes_above_overlapping_broll():
    clips = [_clip("speech", 1000000, 0.0), _clip("broll", 2000000, 1.5, end=2.0)]
    track, ensure = _caption_track_for([1000000, 2000000, 3000000], clips)
    assert track == "3000000"
    ensure.assert_not_called()


def test_caption_track_ignores_clips_outside_the_span():
    clips = [_clip("speech", 1000000, 0.0), _clip("late", 2000000, 30.0)]
    track, _ensure = _caption_track_for([1000000, 2000000], clips)
    assert track == "2000000"


def test_caption_track_creates_a_top_track_when_none_is_free():
    clips = [_clip("speech", 1000000, 0.0), _clip("broll", 2000000, 0.0)]
    track, ensure = _caption_track_for([1000000, 2000000], clips)
    assert track == "3000000"
    ensure.assert_called_once_with([3000000])


def test_add_captions_puts_every_cue_on_the_same_track():
    from classes.agent_tools.receipt import ToolReceipt

    words = [{"index": i, "text": w, "startSec": i * 0.5, "endSec": i * 0.5 + 0.4,
              "startFrame": i * 15, "endFrame": i * 15 + 12,
              "timelineStartSec": i * 0.5, "timelineEndSec": i * 0.5 + 0.4}
             for i, w in enumerate("one two three four five six seven eight nine ten eleven".split())]
    transcript = ToolReceipt.applied(
        "get_transcript_tool", "ok", undo_steps=0,
        data={"transcriptionSource": "local", "transcriptGeneration": 1,
              "clips": [{"clipId": "c1", "words": words}]},
    ).to_json()
    title = ToolReceipt.applied("add_title_tool", "placed", data={"file_id": "t1"}).to_json()
    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.agent_tools.transcript.get_transcript", return_value=transcript), \
         patch("classes.agent_tools.speech_extra._caption_track", return_value="2000000"), \
         patch("classes.agent_tools.titles.add_title", return_value=title) as add_title:
        from classes.agent_tools.speech_extra import add_captions
        receipt = parse_receipt(add_captions(clipId="c1", maxWords=4))
    assert receipt["data"]["count"] >= 2
    assert {c.kwargs["track"] for c in add_title.call_args_list} == {"2000000"}


def test_long_caption_text_shrinks_to_fit_the_bar():
    from xml.dom import minidom
    from classes.agent_tools import titles

    doc = minidom.parse(titles._default_template())
    cue = "basically FlowCut makes cutting fast, thanks for"   # ~48 chars
    titles._set_svg_text(doc, cue)
    titles._fit_text_to_width(doc, cue)
    sizes = {float(titles._FONT_SIZE_RE.search(n.getAttribute("style")).group(1))
             for n in doc.getElementsByTagName("tspan") if titles._FONT_SIZE_RE.search(n.getAttribute("style") or "")}
    assert sizes and max(sizes) < 131.25
    for size in sizes:
        assert len(cue) * size * titles._CAPTION_EM_PER_CHAR <= 1920 * titles._CAPTION_WIDTH_FRACTION + 0.01

    short = minidom.parse(titles._default_template())
    titles._set_svg_text(short, "Hi")
    titles._fit_text_to_width(short, "Hi")
    assert "font-size:131.25064087px" in short.toxml()


def test_failed_captions_leave_no_track_or_undo_step():
    """If no cue lands, the track and imports made for them are reverted."""
    from classes.agent_tools.receipt import ToolReceipt

    words = [{"index": 0, "text": "Hi", "startSec": 0.0, "endSec": 0.3,
              "startFrame": 0, "endFrame": 9, "timelineStartSec": 0.0, "timelineEndSec": 0.3}]
    transcript = ToolReceipt.applied(
        "get_transcript_tool", "ok", undo_steps=0,
        data={"transcriptionSource": "local", "transcriptGeneration": 1,
              "clips": [{"clipId": "c1", "words": words}]},
    ).to_json()
    failed = ToolReceipt.error("add_title_tool", "Error: could not import title").to_json()
    app = MagicMock()
    app.updates.transaction_id = "tid-1"
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.agent_tools.transcript.get_transcript", return_value=transcript), \
         patch("classes.agent_tools.speech_extra._caption_track", return_value="6000000"), \
         patch("classes.agent_tools.speech_extra._discard_group") as discard, \
         patch("classes.agent_tools.titles.add_title", return_value=failed):
        from classes.agent_tools.speech_extra import add_captions
        receipt = parse_receipt(add_captions(clipId="c1"))
    assert receipt["status"] == "error"
    discard.assert_called_once_with(app, "tid-1")


class _Updates:
    def __init__(self, history):
        self.actionHistory = list(history)
        self.redoHistory = ["older redo"]
        self.undone = []

    def undo(self):
        tid = self.actionHistory[-1].transaction
        group = [a for a in self.actionHistory if a.transaction == tid]
        self.actionHistory = [a for a in self.actionHistory if a.transaction != tid]
        self.redoHistory.extend(group)
        self.undone.extend(group)


def test_discard_group_reverts_and_forgets_the_failed_request():
    from types import SimpleNamespace
    from classes.agent_tools.speech_extra import _discard_group

    user_edit = SimpleNamespace(transaction="user")
    track = SimpleNamespace(transaction="cap")
    image = SimpleNamespace(transaction="cap")
    app = SimpleNamespace(updates=_Updates([user_edit, track, image]))
    assert _discard_group(app, "cap") is True
    assert app.updates.undone == [track, image]
    assert app.updates.actionHistory == [user_edit]
    assert app.updates.redoHistory == ["older redo"]


def test_discard_group_leaves_history_alone_when_another_edit_followed():
    from types import SimpleNamespace
    from classes.agent_tools.speech_extra import _discard_group

    track = SimpleNamespace(transaction="cap")
    later = SimpleNamespace(transaction="user")
    app = SimpleNamespace(updates=_Updates([track, later]))
    assert _discard_group(app, "cap") is False
    assert app.updates.undone == []
    assert _discard_group(SimpleNamespace(updates=_Updates([track])), None) is False
