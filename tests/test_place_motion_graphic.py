"""Unit tests for place_motion_graphic mode enforcement (mocked Qt / add_clip)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

import classes.tool_handlers as th  # noqa: E402


def _file(fid="F1", *, transparent=False, duration=3.0, path="/tmp/out.mp4"):
    ai = {"transparent": True} if transparent else {}
    data = {"path": path, "duration": duration, "ai_metadata": ai}
    if transparent:
        data["path"] = "/tmp/out.webm"
        data["tags"] = ["transparent_overlay"]
    return SimpleNamespace(data=data, save=MagicMock())


def _clip(cid, layer, position, start=0.0, end=5.0):
    return SimpleNamespace(
        data={"id": cid, "layer": layer, "position": position, "start": start, "end": end}
    )


def _patch_place(file_obj, clips, app, add_clip_return="OK placed"):
    query_mod = MagicMock()
    query_mod.File.get.return_value = file_obj
    query_mod.Clip.filter.return_value = clips

    track_mod = MagicMock()
    track_mod.normalize_track_or_layer_arg.side_effect = lambda raw, layers: (int(raw), None)
    track_mod.format_track_label_for_llm.side_effect = lambda n, layers: f"layer {n}"

    return (
        patch.object(th, "_get_app", return_value=app),
        patch.dict(sys.modules, {"classes.query": query_mod, "classes.track_display": track_mod}),
        patch.object(th, "add_clip_to_timeline", return_value=add_clip_return),
        patch.object(th, "_run_on_main_thread", side_effect=lambda fn: fn()),
    )


def _layers_app():
    app = MagicMock()
    app.project.get.return_value = [
        {"number": 1000000},
        {"number": 2000000},
        {"number": 3000000},
    ]
    return app


def test_overlay_refuses_opaque():
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="1.0",
            duration_seconds="2.0",
            mode="overlay",
        )
    assert out.startswith("Error:")
    assert "transparent" in out.lower()
    add_clip.assert_not_called()


def test_gap_refuses_transparent():
    f = _file(transparent=True)
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="2.0",
            duration_seconds="2.0",
            mode="gap",
        )
    assert out.startswith("Error:")
    assert "opaque" in out.lower() or "overlay" in out.lower()
    add_clip.assert_not_called()


def test_gap_refuses_primary_overlap():
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    patches = _patch_place(f, [_clip("C1", 1000000, 0.0, end=10.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="2.0",
            duration_seconds="2.0",
            mode="gap",
        )
    assert out.startswith("Error:")
    assert "gap mode refused" in out.lower() or "overlapping" in out.lower()
    add_clip.assert_not_called()


def test_gap_places_when_clear():
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    patches = _patch_place(f, [_clip("C1", 1000000, 0.0, end=2.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="5.0",
            duration_seconds="2.0",
            mode="gap",
        )
    assert not out.startswith("Error:")
    assert "mg_place mode=gap" in out
    add_clip.assert_called_once()
    kwargs = add_clip.call_args.kwargs
    assert kwargs["file_id"] == "F1"
    assert kwargs["position_seconds"] == "5.0"
    assert kwargs["track"] == "2000000"
    assert kwargs.get("query")


def test_cut_in_refuses_transparent():
    f = _file(transparent=True)
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="1.0",
            duration_seconds="2.0",
            mode="cut_in",
        )
    assert out.startswith("Error:")
    add_clip.assert_not_called()


def test_cut_in_ripples_and_places():
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    patches = _patch_place(
        f,
        [
            _clip("C1", 1000000, 0.0, end=4.0),
            _clip("C2", 1000000, 4.0, end=4.0),
        ],
        app,
    )
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="4.0",
            duration_seconds="2.0",
            mode="cut_in",
            layout_region="mid_plate",
        )
    assert not out.startswith("Error:")
    assert "mg_place mode=cut_in" in out
    assert "layout_region=mid_plate" in out
    app.updates.update.assert_any_call(["clips", {"id": "C2"}], {"position": 6.0})
    add_clip.assert_called_once()
    assert add_clip.call_args.kwargs["track"] == "1000000"


def test_overlay_places_transparent_on_high_track():
    f = _file(transparent=True)
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(
            file_id="F1",
            position_seconds="1.5",
            duration_seconds="2.0",
            mode="overlay",
            layout_region="lower_third",
        )
    assert not out.startswith("Error:")
    assert "mg_place mode=overlay" in out
    assert add_clip.call_args.kwargs["track"] == "3000000"
    assert "lower" in add_clip.call_args.kwargs["query"]


def test_overlay_goes_above_footage_on_the_top_track():
    """Footage on the top track during the window: the overlay gets a new track above it, not the same one."""
    f = _file(transparent=True)
    app = _layers_app()
    patches = _patch_place(f, [_clip("C1", 3000000, 0.0, end=15.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
        track_cls = sys.modules["classes.query"].Track
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "4000000"
    assert track_cls.return_value.data["number"] == 4000000
    track_cls.return_value.save.assert_called_once()


def test_overlay_reuses_a_free_track_above_the_footage():
    f = _file(transparent=True)
    app = _layers_app()
    clips = [_clip("C1", 2000000, 0.0, end=15.0), _clip("C2", 3000000, 40.0, end=5.0)]
    patches = _patch_place(f, clips, app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
        track_cls = sys.modules["classes.query"].Track
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "3000000"
    track_cls.return_value.save.assert_not_called()


def test_overlay_asked_onto_the_footage_track_still_goes_above_it():
    """The assistant names the top track by habit; that must not put the overlay in the footage's lane."""
    f = _file(transparent=True)
    app = _layers_app()
    patches = _patch_place(f, [_clip("C1", 3000000, 0.0, end=15.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="5.0",
                                      mode="overlay", track="3000000")
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "4000000"


def test_music_above_the_picture_neither_forces_a_new_track_nor_shares_its_lane():
    f = _file(transparent=True)
    app = _layers_app()
    music = SimpleNamespace(data={"id": "A1", "layer": 3000000, "position": 0.0, "start": 0.0, "end": 60.0,
                                  "reader": {"has_video": False, "has_audio": True}})
    patches = _patch_place(f, [_clip("C1", 1000000, 0.0, end=15.0), music], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
        track_cls = sys.modules["classes.query"].Track
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "2000000"       # free, above the picture, not the music's lane
    track_cls.return_value.save.assert_not_called()


def test_overlay_skips_a_locked_track():
    f = _file(transparent=True)
    app = MagicMock()
    app.project.get.return_value = [{"number": 1000000}, {"number": 2000000},
                                    {"number": 3000000, "lock": True}]
    patches = _patch_place(f, [_clip("C1", 2000000, 0.0, end=15.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "4000000"       # a new lane above the locked one


def test_a_locked_track_with_no_number_does_not_break_placement():
    f = _file(transparent=True)
    app = MagicMock()
    app.project.get.return_value = [{"number": 1000000}, {"number": 2000000}, {"lock": True},
                                    {"number": None, "lock": True}]
    patches = _patch_place(f, [_clip("C1", 1000000, 0.0, end=15.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
    assert not out.startswith("Error:") and add_clip.call_args.kwargs["track"] == "2000000"


def test_a_failed_placement_removes_the_track_it_created():
    f = _file(transparent=True)
    app = _layers_app()
    app.updates.transaction_id = None
    patches = _patch_place(f, [_clip("C1", 3000000, 0.0, end=15.0)], app, add_clip_return="Error: no such file")
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        deleted_in = []
        sys.modules["classes.query"].Track.return_value.delete.side_effect = \
            lambda: deleted_in.append(app.updates.transaction_id)
        out = th.place_motion_graphic(file_id="F1", position_seconds="1.0", duration_seconds="4.0",
                                      mode="overlay")
    assert out.startswith("Error")
    # in the same undo step as the insert, or undo replays a track that was never wanted
    assert deleted_in == [add_clip.call_args.kwargs["transaction_id"]]


def test_placement_joins_the_undo_step_of_the_tool_that_called_it():
    """add_captions places two overlays; each minting its own transaction made that several undo steps."""
    f = _file(transparent=True)
    app = _layers_app()
    app.updates.transaction_id = "caller-step"
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="240", mode="overlay",
                                max_duration_seconds=240)
    assert add_clip.call_args.kwargs["transaction_id"] == "caller-step"
    assert add_clip.call_args.kwargs["duration_seconds"] == "240.0"        # not cut to the 60 s default


def test_a_graphic_is_still_capped_at_a_minute_by_default():
    f = _file(transparent=True)
    app = _layers_app()
    app.updates.transaction_id = None
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="240", mode="overlay")
    assert add_clip.call_args.kwargs["duration_seconds"] == "60.0"
    assert add_clip.call_args.kwargs["transaction_id"]


@pytest.mark.parametrize("limit", [-5, 0, "nan", "inf", "soon"])
def test_a_nonsense_duration_limit_falls_back_to_a_minute(limit):
    f = _file(transparent=True)
    app = _layers_app()
    app.updates.transaction_id = None
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="240", mode="overlay",
                                max_duration_seconds=limit)
    assert add_clip.call_args.kwargs["duration_seconds"] == "60.0"


def test_full_file_places_the_whole_render_without_a_watch():
    # Captions must land frame-exact: the visual watch moved a 20 s render to its 2-18 s.
    f = _file(transparent=True, duration=20.0)
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="20", mode="overlay",
                                max_duration_seconds=20.0, full_file="true")
    kwargs = add_clip.call_args.kwargs
    assert kwargs["full_file"] == "true" and float(kwargs["end_seconds"]) == 20.0


def test_cut_in_cuts_the_top_track_holding_footage_not_the_lowest():
    # Footage is on the top track and the lowest track is empty: the picture the viewer sees is the top one.
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    app.project.get.return_value = [{"number": n} for n in (1000000, 2000000, 3000000, 4000000)]
    patches = _patch_place(f, [_clip("C1", 4000000, 0.0, end=10.0), _clip("C2", 4000000, 10.0, end=4.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="5.0", duration_seconds="4.0", mode="cut_in")
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "4000000"
    app.updates.update.assert_any_call(["clips", {"id": "C2"}], {"position": 14.0})   # ripple and cut share a track


def test_a_rendered_motion_graphic_is_placed_whole_without_asking():
    # Files stamped by _stamp_motion_graphics_file_metadata: a visual watch trimmed a 20 s demo to 2-18 s.
    f = _file(transparent=False, path="/tmp/demo.mp4", duration=20.0)
    f.data["ai_metadata"]["source"] = "hyperframes_motion_graphics"
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="20", mode="cut_in")
    assert not out.startswith("Error:")
    kwargs = add_clip.call_args.kwargs
    assert kwargs["full_file"] == "true" and float(kwargs["end_seconds"]) == 20.0
    assert kwargs["track"] == "1000000"                                 # no footage: the lowest track, as before


def test_gap_is_refused_when_footage_sits_above_the_mid_track():
    # Gap puts the graphic on the mid track; footage above it would hide the graphic.
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    patches = _patch_place(f, [_clip("C1", 3000000, 0.0, end=10.0)], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        out = th.place_motion_graphic(file_id="F1", position_seconds="2.0", duration_seconds="2.0", mode="gap")
    assert out.startswith("Error:")
    add_clip.assert_not_called()


def test_a_transparent_overlay_above_the_footage_is_not_the_cut_in_picture():
    # Captions sit above the footage on a transparent overlay: the cut ripples the footage, not the captions.
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    app.project.get.return_value = [{"number": n} for n in (1000000, 2000000, 3000000, 4000000)]
    footage, captions = _clip("FOOT", 1000000, 0.0, end=20.0), _clip("CAP", 4000000, 6.0, end=6.0)
    footage.data["file_id"], captions.data["file_id"] = "FOOT_FILE", "CAP_FILE"
    overlay = SimpleNamespace(id="CAP_FILE", data={"path": "/tmp/captions.webm", "ai_metadata": {"transparent": True}})
    patches = _patch_place(f, [footage, captions], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        sys.modules["classes.query"].File.filter.return_value = [overlay]
        out = th.place_motion_graphic(file_id="F1", position_seconds="5.0", duration_seconds="2.0", mode="cut_in")
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "1000000"
    app.updates.update.assert_not_called()


def test_a_title_above_the_footage_is_not_the_cut_in_picture():
    # Titles are SVG images: visual, but not tagged transparent. The cut still belongs on the footage.
    f = _file(transparent=False, path="/tmp/plate.mp4")
    app = _layers_app()
    app.project.get.return_value = [{"number": n} for n in (1000000, 2000000, 3000000, 4000000)]
    footage, title = _clip("FOOT", 1000000, 0.0, end=20.0), _clip("TITLE", 4000000, 4.0, end=5.0)
    footage.data["file_id"], title.data["file_id"] = "FOOT_FILE", "TITLE_FILE"
    svg = SimpleNamespace(id="TITLE_FILE", data={"path": "/project/title/Lower_third.svg", "ai_metadata": {}})
    patches = _patch_place(f, [footage, title], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        sys.modules["classes.query"].File.filter.return_value = [svg]
        out = th.place_motion_graphic(file_id="F1", position_seconds="5.0", duration_seconds="2.0", mode="cut_in")
    assert not out.startswith("Error:")
    assert add_clip.call_args.kwargs["track"] == "1000000"


def test_a_rendered_motion_graphic_longer_than_a_minute_is_placed_whole():
    f = _file(transparent=False, path="/tmp/explainer.mp4", duration=90.0)
    f.data["ai_metadata"]["source"] = "hyperframes_motion_graphics"
    app = _layers_app()
    patches = _patch_place(f, [], app)
    with patches[0], patches[1], patches[2] as add_clip, patches[3]:
        th.place_motion_graphic(file_id="F1", position_seconds="0", duration_seconds="", mode="gap")
    kwargs = add_clip.call_args.kwargs
    assert float(kwargs["duration_seconds"]) == 90.0 and float(kwargs["end_seconds"]) == 90.0
