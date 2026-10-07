"""Forcing a saved dock size must not leave the dock fixed-size.

``_force_dock_extent_once`` pins a dock to its saved width/height and queues a
restore of the previous min/max. Startup calls it more than once per dock
before the queued restore runs; the later call used to capture the *pinned*
limits as "previous" and restore those, so the preview dock stayed
fixed-width (min == max) and its splitter could not be dragged.

The methods are compiled from main_window.py's source, so the test runs without
Qt or libopenshot (importing the window module needs both).
"""

import ast
import os
from types import SimpleNamespace

_MAIN_WINDOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "windows", "main_window.py",
)

QWIDGETSIZE_MAX = 16777215
Qt = SimpleNamespace(Horizontal=1, Vertical=2)


class FakeDock:
    """Size limits of a QDockWidget; the layout has not resized it yet."""

    def __init__(self):
        self.min_w, self.max_w = 80, QWIDGETSIZE_MAX
        self.min_h, self.max_h = 60, QWIDGETSIZE_MAX

    def width(self):
        return 400

    def height(self):
        return 300

    def minimumWidth(self):
        return self.min_w

    def maximumWidth(self):
        return self.max_w

    def minimumHeight(self):
        return self.min_h

    def maximumHeight(self):
        return self.max_h

    def setMinimumWidth(self, value):
        self.min_w = value

    def setMaximumWidth(self, value):
        self.max_w = value

    def setMinimumHeight(self, value):
        self.min_h = value

    def setMaximumHeight(self, value):
        self.max_h = value

    def setFixedWidth(self, value):
        self.min_w = self.max_w = value

    def setFixedHeight(self, value):
        self.min_h = self.max_h = value


def _window(queued):
    """The real _force_dock_extent_once, bound to a stand-in window."""
    with open(_MAIN_WINDOW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _MAIN_WINDOW)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    names = ("_positive_int", "_force_dock_extent_once")
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {
        "Qt": Qt,
        "QTimer": SimpleNamespace(singleShot=lambda ms, callback: queued.append(callback)),
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), _MAIN_WINDOW, "exec"), ns)
    attrs = {name: ns[name] for name in names}
    attrs.update(width=lambda self: 1530, height=lambda self: 921)
    return type("DockExtentMethods", (), attrs)()


def _run(queued):
    while queued:
        queued.pop(0)()


def test_forcing_width_twice_before_restore_keeps_original_limits():
    queued = []
    window = _window(queued)
    dock = FakeDock()

    window._force_dock_extent_once(dock, 931, Qt.Horizontal)
    assert (dock.min_w, dock.max_w) == (931, 931)
    window._force_dock_extent_once(dock, 931, Qt.Horizontal)
    _run(queued)

    assert (dock.min_w, dock.max_w) == (80, QWIDGETSIZE_MAX)


def test_forcing_height_twice_before_restore_keeps_original_limits():
    queued = []
    window = _window(queued)
    dock = FakeDock()

    window._force_dock_extent_once(dock, 250, Qt.Vertical)
    window._force_dock_extent_once(dock, 250, Qt.Vertical)
    _run(queued)

    assert (dock.min_h, dock.max_h) == (60, QWIDGETSIZE_MAX)


def test_dock_can_be_forced_again_after_its_restore_ran():
    queued = []
    window = _window(queued)
    dock = FakeDock()

    window._force_dock_extent_once(dock, 931, Qt.Horizontal)
    _run(queued)
    window._force_dock_extent_once(dock, 700, Qt.Horizontal)
    assert (dock.min_w, dock.max_w) == (700, 700)
    _run(queued)

    assert (dock.min_w, dock.max_w) == (80, QWIDGETSIZE_MAX)
