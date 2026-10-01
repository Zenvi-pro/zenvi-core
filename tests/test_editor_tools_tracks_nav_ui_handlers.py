"""The Track / Marker menu handlers in main_window.py, run against the headless editor.

main_window.py cannot be imported without Qt and libopenshot, so the handler
methods that now call classes.track_ops are compiled straight from its source
(ast) and bound to a small window object. This exercises the real handler code
-- the menu actions and the tools share one implementation.
"""

import ast
import os
from unittest.mock import MagicMock

import pytest

from classes import track_ops
from tracks_nav_fakes import add_transition, set_layers

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "main_window.py")
HANDLERS = (
    "actionAddTrack_trigger", "_selected_track_layer", "actionAddTrackAbove_trigger",
    "actionAddTrackBelow_trigger", "renumber_all_layers", "deselect_removed_item",
    "actionRemoveTrack_trigger", "actionLockTrack_trigger", "actionUnlockTrack_trigger",
    "actionRenameTrack_trigger", "actionAddMarker_trigger", "actionRemoveMarker_trigger",
    "findAllMarkerPositions", "_seek_to_marker", "actionPreviousMarker_trigger",
    "actionNextMarker_trigger", "step_frames", "handleSeekPreviousFrame", "handleSeekNextFrame",
)


def _compile_handlers(namespace):
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), SRC)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    missing = [h for h in HANDLERS if h not in funcs]
    assert not missing, missing
    module = ast.Module(body=[funcs[h] for h in HANDLERS], type_ignores=[])
    exec(compile(module, SRC, "exec"), namespace)
    return {h: namespace[h] for h in HANDLERS}


@pytest.fixture
def ui(editor):
    from classes.query import Marker, Track

    dialog = MagicMock(name="QInputDialog")
    box = MagicMock(name="QMessageBox")
    namespace = {"get_app": lambda: editor.app, "track_ops": track_ops, "Track": Track,
                 "Marker": Marker, "log": MagicMock(), "Qt": MagicMock(), "QMessageBox": box,
                 "QInputDialog": dialog}
    handlers = _compile_handlers(namespace)

    class Window:
        pass

    for name, func in handlers.items():
        setattr(Window, name, func)
    win = Window()
    win.selected_tracks = []
    win.selected_markers = []
    win.selected_clips, win.selected_transitions, win.selected_effects = [], [], []
    for name in ("removeSelection", "emit_selection_signal", "show_property_timeout", "refreshFrameSignal",
                 "SeekSignal", "PauseSignal", "SpeedSignal", "timeline", "preview_thread"):
        setattr(win, name, MagicMock(name=name))
    editor.app._tr = lambda text: text
    # get_app().window is the harness window; the handlers use both.
    editor.window.timeline_sync.GetLastFrame.return_value = 899
    editor.window.preview_thread.player.Position.return_value = 1
    win.preview_thread = editor.window.preview_thread
    win.preview_thread.current_frame = 1
    win.dialog, win.box, win.editor = dialog, box, editor
    return win


def numbers(editor):
    return sorted(int(t["number"]) for t in editor.get("layers"))


def track_id(editor, number):
    return next(t["id"] for t in editor.get("layers") if int(t["number"]) == number)


def test_add_track_button_and_menu(ui):
    editor = ui.editor
    ui.actionAddTrack_trigger()
    assert numbers(editor)[-1] == 6000000
    ui.selected_tracks = [track_id(editor, 2000000)]
    ui.actionAddTrackAbove_trigger()
    ui.actionAddTrackBelow_trigger()
    assert 2500000 in numbers(editor) and 1500000 in numbers(editor)
    assert editor.undo_steps_since_mark() == 3
    # No track selected: nothing happens (it used to raise IndexError).
    ui.selected_tracks = []
    ui.actionAddTrackAbove_trigger()
    assert len(numbers(editor)) == 8


def test_menu_insert_with_tight_numbers_is_one_undo_step(ui):
    editor = ui.editor
    set_layers(editor, [1, 2, 3])
    f = editor.add_file("video")
    c = editor.add_clip(f, layer=3)
    ui.selected_tracks = [track_id(editor, 2)]
    ui.actionAddTrackAbove_trigger()
    assert numbers(editor) == [1000000, 2000000, 3000000, 4000000]
    assert editor.clip(c)["layer"] == 4000000
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert numbers(editor) == [1, 2, 3] and editor.clip(c)["layer"] == 3


def test_remove_track_menu(ui):
    editor = ui.editor
    f = editor.add_file("video")
    c = editor.add_clip(f, layer=2000000)
    t = add_transition(editor, layer=2000000, position=1.0)
    ui.selected_tracks = [track_id(editor, 2000000)]
    ui.actionRemoveTrack_trigger()
    assert editor.clip(c) is None and 2000000 not in numbers(editor)
    assert not any(e["id"] == t for e in editor.get("effects"))
    removed = [call.args for call in ui.removeSelection.call_args_list]
    assert removed == [(c, "clip"), (t, "transition")]
    assert ui.selected_tracks == []
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(c) is not None and 2000000 in numbers(editor)


def test_remove_the_last_track_is_refused_with_a_message(ui):
    editor = ui.editor
    set_layers(editor, [1000000])
    ui.selected_tracks = [track_id(editor, 1000000)]
    ui.actionRemoveTrack_trigger()
    ui.box.warning.assert_called_once()
    assert numbers(editor) == [1000000] and editor.undo_steps_since_mark() == 0


def test_lock_unlock_and_rename_menu(ui):
    editor = ui.editor
    ui.selected_tracks = [track_id(editor, 3000000)]
    ui.actionLockTrack_trigger()
    assert next(t for t in editor.get("layers") if t["number"] == 3000000)["lock"] is True
    ui.actionUnlockTrack_trigger()
    assert next(t for t in editor.get("layers") if t["number"] == 3000000)["lock"] is False
    ui.dialog.getText.return_value = ("Titles", True)
    ui.actionRenameTrack_trigger()
    assert next(t for t in editor.get("layers") if t["number"] == 3000000)["label"] == "Titles"
    # The dialog offers the default name counted from the bottom.
    assert ui.dialog.getText.call_args.kwargs["text"] == "Track 3"
    assert editor.undo_steps_since_mark() == 3


def test_add_and_remove_marker_menu(ui):
    editor = ui.editor
    editor.window.preview_thread.player.Position.return_value = 61   # 2.0 s at 30 fps
    ui.actionAddMarker_trigger()
    (m,) = editor.get("markers")
    assert m["position"] == 2.0 and m["icon"] == "blue.png" and m["vector"] == "blue"
    ui.selected_markers = [m["id"]]
    ui.actionRemoveMarker_trigger()
    assert editor.get("markers") == []
    assert editor.undo_steps_since_mark() == 2


def test_previous_and_next_marker_menu(ui):
    track_ops.add_marker(5.0)
    track_ops.add_marker(12.0)
    ui.preview_thread.current_frame = 181   # 6 s
    ui.actionNextMarker_trigger()
    ui.SeekSignal.emit.assert_called_with(361)
    ui.actionPreviousMarker_trigger()
    ui.SeekSignal.emit.assert_called_with(151)
    ui.preview_thread.current_frame = 400   # past the last marker: next stop is the end
    ui.actionNextMarker_trigger()
    ui.SeekSignal.emit.assert_called_with(899)


def test_frame_step_keys(ui):
    editor = ui.editor
    editor.window.preview_thread.player.Position.return_value = 10
    ui.handleSeekNextFrame()
    editor.window.previewFrameSignal.emit.assert_called_with(11)
    ui.handleSeekPreviousFrame()
    editor.window.previewFrameSignal.emit.assert_called_with(9)
    assert ui.step_frames(-50) == 1   # never before the first frame


# --- native timeline: Select All (Ctrl+A) and Ripple Select (Alt+A) -----------------

NATIVE_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "timeline_backend",
                          "qwidget", "base.py")


def _native_selection_handlers():
    from classes.query import Clip, Transition
    with open(NATIVE_SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), NATIVE_SRC)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TimelineWidgetBase")
    wanted = ("_timeline_items", "select_all_items", "selectRipple")
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {"Clip": Clip, "Transition": Transition}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), NATIVE_SRC, "exec"), namespace)

    class Widget:
        pass

    for name in wanted:
        setattr(Widget, name, namespace[name])
    return Widget


def test_select_all_and_ripple_cover_items_outside_the_visible_timeline(editor):
    """Ctrl+A / Alt+A used the painted geometry, which only holds items in view."""
    Widget = _native_selection_handlers()
    widget = Widget()
    widget.win = MagicMock()
    picked = []
    widget._select_timeline_item = lambda item_id, kind, clear: picked.append((item_id, kind))
    f = editor.add_file("video", duration=5.0)
    a = editor.add_clip(f, layer=1000000, position=0.0)
    b = editor.add_clip(f, layer=1000000, position=600.0)     # far right of any view
    c = editor.add_clip(f, layer=5000000, position=3.0)       # a track scrolled out of view
    t = add_transition(editor, layer=1000000, position=4.0)

    widget.select_all_items()
    widget.win.clearSelections.assert_called_once()
    assert sorted(picked) == sorted([(a, "clip"), (b, "clip"), (c, "clip"), (t, "transition")])

    picked.clear()
    widget.selectRipple(a, "clip")
    assert sorted(picked) == sorted([(a, "clip"), (b, "clip"), (t, "transition")])
    picked.clear()
    widget.selectRipple("missing", "clip")
    assert picked == []
