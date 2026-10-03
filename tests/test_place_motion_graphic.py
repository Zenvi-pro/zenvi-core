"""Unit tests for place_motion_graphic mode enforcement (mocked Qt / add_clip)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
