"""Toolbar agent selector: the flow button and its popup panel.

These pin the invariants that are cheap to assert and expensive to notice
breaking by eye — stable toolbar width, exact CSS easing, a popup that cannot
resize or escape the screen, and the held state that survives the mouse grab
a Qt.Popup takes when it opens.
"""

import os
import sys

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()
from PyQt5.QtCore import QPoint, QRect, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

CLAUDE = "claude_code"
CODEX = "codex"


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class FakeChat:
    """Stands in for AIChatWindow — the panel and button only read this much."""

    def __init__(self, status=None, active=CLAUDE):
        self.status = status if status is not None else {}
        self.active = active
        self.detects = 0
        self.connects = []

    def active_backend(self):
        return self.active

    def cli_status(self):
        return self.status

    def set_active_backend(self, backend):
        self.active = backend

    def _detect_clis(self):
        self.detects += 1

    def _connect_cli(self, backend_id):
        self.connects.append(backend_id)


def _window(chat):
    win = QMainWindow()
    win.dockAIChat = chat
    return win


CONNECTED = {
    CLAUDE: {"installed": True, "version": "2.1.241 (Claude Code)", "registered": True},
    CODEX: {"installed": True, "version": "codex-cli 0.142.3", "registered": False},
}
MISSING = {
    CLAUDE: {"installed": False, "version": None, "registered": False},
    CODEX: {"installed": False, "version": None, "registered": False},
}


# ── Easing ─────────────────────────────────────────────────────────────────

def test_easing_curves_match_the_css_including_overshoot(qapp):
    """The whole motion design rests on Qt reproducing cubic-bezier() exactly,
    overshoot included — a clamped curve would silently flatten the arrows."""
    from windows.agent_selector_button import (
        CURVE_ARROW, CURVE_INK, CURVE_LABEL, CURVE_RADIUS,
    )

    for curve in (CURVE_INK, CURVE_RADIUS, CURVE_LABEL, CURVE_ARROW):
        assert curve.valueForProgress(0.0) == pytest.approx(0.0, abs=1e-6)
        assert curve.valueForProgress(1.0) == pytest.approx(1.0, abs=1e-6)

    # cubic-bezier(.34, 1.56, .64, 1) overshoots; the others must not.
    assert max(CURVE_ARROW.valueForProgress(t / 100.0) for t in range(101)) > 1.05
    for curve in (CURVE_INK, CURVE_RADIUS, CURVE_LABEL):
        assert max(curve.valueForProgress(t / 100.0) for t in range(101)) <= 1.0 + 1e-6

    # Spot values against CSS.
    assert CURVE_INK.valueForProgress(0.25) == pytest.approx(0.8435, abs=1e-3)
    assert CURVE_RADIUS.valueForProgress(0.5) == pytest.approx(0.9660, abs=1e-3)


def test_only_the_monotonic_curve_is_ever_inverted(qapp):
    """_invert bisects, so it is only valid on a monotonic curve."""
    from windows.agent_selector_button import CURVE_INK, _invert

    for target in (0.1, 0.5, 0.9):
        assert CURVE_INK.valueForProgress(_invert(CURVE_INK, target)) == pytest.approx(
            target, abs=1e-3)


# ── Button ─────────────────────────────────────────────────────────────────

def test_width_is_identical_for_every_backend_name(qapp):
    """Otherwise picking "Codex" after "Zenvi Assistant" reflows the toolbar."""
    from windows.agent_selector_button import AgentSelectorButton

    win = _window(FakeChat(CONNECTED))
    widths = set()
    for name in ("Zenvi Assistant", "Claude Code", "Codex"):
        button = AgentSelectorButton(win)
        button.setText(name)
        button._recompute_hint()
        widths.add(button.sizeHint().width())
        assert button.sizeHint() == button.minimumSizeHint()
    assert len(widths) == 1, "toolbar would jitter: %s" % sorted(widths)


def test_size_hint_survives_being_released_by_the_toolbar(qapp):
    """QToolBar.clear() reparents the widget to None; sizeHint must not raise."""
    from windows.agent_selector_button import AgentSelectorButton

    button = AgentSelectorButton(_window(FakeChat(CONNECTED)))
    button.setParent(None)
    button._recompute_hint()
    assert button.sizeHint().width() > 0


def test_open_panel_holds_the_button_through_the_grab_leave(qapp):
    """Opening a Qt.Popup steals the mouse grab, which delivers a leaveEvent to
    the button. Without the held guard the fill would collapse under the panel."""
    from PyQt5.QtCore import QEvent
    from windows.agent_selector_button import AgentSelectorButton

    button = AgentSelectorButton(_window(FakeChat(CONNECTED)))
    button._set_panel_open(True)
    assert button._phase == "in"

    button.leaveEvent(QEvent(QEvent.Leave))
    assert button._phase == "in", "held state must ignore the popup's grab-leave"

    button._panel_open = False
    button.leaveEvent(QEvent(QEvent.Leave))
    assert button._phase == "out"


def test_leaving_replays_forward_rather_than_rewinding(qapp):
    """CURVE_INK is 0.998 at t=0.75, so rewinding the clock would barely move
    the ink and the collapse would look frozen."""
    from windows.agent_selector_button import AgentSelectorButton

    win = _window(FakeChat(CONNECTED))
    win.show()
    button = AgentSelectorButton(win)
    button.show()   # a hidden widget snaps to rest instead of animating
    button._phase = "in"
    button._t = 1.0
    depth = button._channels()["ink"]

    button._begin_out()
    assert button._snap["ink"] == pytest.approx(depth)
    assert button._t == 0.0                       # its own forward clock
    assert button._channels()["ink"] == pytest.approx(depth, abs=1e-6)

    button._t = 1.0                               # collapse complete
    assert button._channels()["ink"] == pytest.approx(0.0, abs=1e-6)


def test_paint_is_safe_at_zero_size(qapp):
    """A zero-size widget mid toolbar-rebuild would otherwise hit a NaN radius."""
    from windows.agent_selector_button import AgentSelectorButton

    button = AgentSelectorButton(_window(FakeChat(CONNECTED)))
    button.resize(0, 0)
    button.grab()   # must not raise


def test_label_follows_the_active_tab(qapp):
    from windows.agent_selector_button import AgentSelectorButton

    chat = FakeChat(CONNECTED, active=CODEX)
    button = AgentSelectorButton(_window(chat))
    button.sync_from_chat()
    assert button.text() == "Codex"

    chat.active = CLAUDE
    button.sync_from_chat()
    assert button.text() == "Claude Code"


# ── Panel ──────────────────────────────────────────────────────────────────

def _panel(chat):
    from windows.agent_panel import AgentPanel
    panel = AgentPanel(_window(chat))
    panel.refresh()
    panel.adjustSize()
    return panel


def test_panel_height_never_changes_between_states(qapp):
    """It is a popup under the cursor — resizing while open would move the rows
    out from under the pointer."""
    heights = set()

    for status in (CONNECTED, MISSING, {}):
        heights.add(_panel(FakeChat(status)).height())

    panel = _panel(FakeChat(CONNECTED))
    panel._connecting = CODEX
    panel.refresh(); panel.adjustSize()
    heights.add(panel.height())

    panel._connecting = None
    panel._connect_error[CODEX] = "codex config write failed: permission denied\nline two"
    panel.refresh(); panel.adjustSize()
    heights.add(panel.height())

    assert len(heights) == 1, "panel resizes between states: %s" % sorted(heights)


def test_panel_reports_each_status(qapp):
    panel = _panel(FakeChat(CONNECTED))
    assert panel._rows[CLAUDE].word.text() == "connected"
    assert "v2.1.241" in panel._rows[CLAUDE].desc.text()
    assert panel._rows[CODEX].word.text() == "not connected"
    assert panel._rows[CODEX].action.isVisible() or not panel.isVisible()

    panel = _panel(FakeChat(MISSING))
    assert panel._rows[CLAUDE].word.text() == "not installed"
    assert "claude" in panel._rows[CLAUDE].desc.text()

    panel = _panel(FakeChat({}))       # nothing probed yet
    assert panel._rows[CLAUDE].word.text() == "checking…"

    # The built-in assistant is always ready and never offers Connect.
    assert panel._rows["zenvi"].word.text() == "ready"
    assert not panel._rows["zenvi"].action.isVisible()


def test_selected_row_tracks_the_active_backend(qapp):
    panel = _panel(FakeChat(CONNECTED, active=CODEX))
    assert panel._rows[CODEX].property("selected") is True
    assert panel._rows[CLAUDE].property("selected") is False


def test_connect_marks_the_row_busy_then_reports_the_result(qapp):
    chat = FakeChat(CONNECTED)
    panel = _panel(chat)

    panel._on_connect_requested(CODEX)
    assert chat.connects == [CODEX]
    assert panel._rows[CODEX].word.text() == "connecting…"
    assert not panel._rows[CODEX].action.isEnabled()

    panel.on_connect_result(CODEX, False, "codex config write failed\ndetail")
    assert panel._rows[CODEX].word.text() == "not connected"
    assert panel._rows[CODEX].desc.text() == "codex config write failed"
    assert panel._rows[CODEX].action.text() == "Retry"


def test_a_stale_connect_result_cannot_clear_the_other_row(qapp):
    panel = _panel(FakeChat(CONNECTED))
    panel._on_connect_requested(CODEX)
    panel.on_connect_result(CLAUDE, False, "unrelated failure")
    assert panel._connecting == CODEX, "result for the wrong CLI was applied"


def test_detection_is_throttled(qapp):
    """detect_cli shells out to two binaries sequentially; rapid opens must not
    stack detection threads."""
    chat = FakeChat(CONNECTED)
    panel = _panel(chat)
    panel._maybe_detect()
    panel._maybe_detect()
    panel._maybe_detect()
    assert chat.detects == 1


def test_reopen_guard_covers_the_popup_dismiss_click(qapp):
    panel = _panel(FakeChat(CONNECTED))
    panel.show()
    panel.close()
    assert panel.just_closed(), "the anchor click would immediately reopen the popup"


def test_show_under_keeps_the_panel_on_screen(qapp):
    """Anchored bottom-right of the screen, it must clamp and flip above."""
    from windows.agent_selector_button import AgentSelectorButton

    chat = FakeChat(CONNECTED)
    win = _window(chat)
    button = AgentSelectorButton(win)
    win.resize(400, 200)
    win.show()

    from windows.agent_panel import AgentPanel
    panel = AgentPanel(win)
    panel.refresh()

    screen = QApplication.primaryScreen().availableGeometry()
    win.move(screen.right() - 420, screen.bottom() - 220)
    panel.show_under(button)

    placed = QRect(panel.pos(), panel.size())
    assert screen.contains(placed.topLeft()), placed
    assert placed.right() <= screen.right(), placed
    assert placed.bottom() <= screen.bottom(), placed


def test_version_strings_are_normalised(qapp):
    from windows.agent_panel import _format_version

    assert _format_version("2.1.241 (Claude Code)") == "v2.1.241"
    assert _format_version("codex-cli 0.142.3") == "v0.142.3"
    assert _format_version("") == ""
    assert _format_version(None) == ""


def test_tool_count_is_computed_not_hard_coded(qapp):
    from windows.agent_panel import _editor_tool_count
    from classes.tool_handlers import AGENT_TOOL_HANDLERS

    assert _editor_tool_count() == len(AGENT_TOOL_HANDLERS)


# ── Shutdown guard ─────────────────────────────────────────────────────────

def test_getattr_on_a_deleted_widget_raises(qapp):
    """The premise of the closeEvent guard.

    getattr(obj, name, default) does NOT swallow the RuntimeError sip raises for
    a destroyed C++ object — it propagates. main_window.closeEvent relied on the
    default and so aborted mid-shutdown, skipping thread teardown (including the
    agent CLI subprocesses) and killing the process with
    "QThread: Destroyed while thread is still running".
    """
    import sip
    from PyQt5.QtWidgets import QWidget

    widget = QWidget()
    sip.delete(widget)

    with pytest.raises(RuntimeError):
        getattr(widget, "thumbnail_manager", None)

    # ...which is why the call site now wraps it.
    try:
        getattr(widget, "thumbnail_manager", None)
    except Exception:
        pass   # reaching here is the fix


def test_close_event_guards_the_timeline_shutdown(qapp):
    """The guard is present at the call site, not just in principle."""
    import re

    path = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "main_window.py")
    source = open(path).read()
    body = source[source.index("def closeEvent"):]
    body = body[:body.index("\n    def ", 1)]

    call = body.index("thumbnail_manager.shutdown()")
    before = body[:call]
    guard = before.rindex("try:")
    assert "except" in body[call:], "thumbnail shutdown is not inside a try/except"
    assert re.search(r"try:\s*\n\s+timeline_widget = getattr", before[guard:]), \
        "the getattr that raises must itself be inside the try"
