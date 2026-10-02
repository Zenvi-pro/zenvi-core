"""Edit keys with the assistant chat docked (RC v1.2.0 B1).

Ctrl/Cmd+Z belongs to the chat only while the chat has keyboard focus; with
focus anywhere else the main window's Undo action (the timeline) must fire.
Real widgets and real key events: QTest.keyClick sends ShortcutOverride, tries
the shortcut map, then delivers the key press, as the platform does.

QtWebEngine is not needed. Its view keeps keyboard focus on a focus-proxy child
that hands every key press straight to Chromium; _PageProxy models that.
"""

import os

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("PyQt5.QtTest")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeySequence  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QAction,
    QApplication,
    QDockWidget,
    QMainWindow,
    QWidget,
)

from windows.chat_web_view import ChatEditShortcutMixin, chat_owns_clipboard_keys  # noqa: E402


class _PageProxy(QWidget):
    """Like QtWebEngine's focus proxy: takes every key press for the page."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.page_keys = []

    def keyPressEvent(self, event):
        self.page_keys.append(event.key())
        event.accept()


class _ChatView(ChatEditShortcutMixin, QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self._enable_edit_shortcuts()
        # Created after the filters are set up, as QtWebEngine creates its
        # proxy once the page loads (the ChildAdded path).
        self.proxy = _PageProxy(self)
        self.setFocusProxy(self.proxy)


class _ChatDock(QDockWidget):
    def __init__(self, parent):
        super().__init__("Agents", parent)
        self._chat_view = _ChatView(self)
        self.setWidget(self._chat_view)
        self.attachment_batches = [["a.png"], ["b.png"]]
        self.undo_calls = 0

    def undo_chat_attachments(self):
        self.undo_calls += 1
        if not self.attachment_batches:
            return False
        self.attachment_batches.pop()
        return True


class _Editor:
    def __init__(self):
        self.win = QMainWindow()
        self.timeline = QWidget()
        self.timeline.setFocusPolicy(Qt.StrongFocus)
        self.win.setCentralWidget(self.timeline)
        self.chat = _ChatDock(self.win)
        self.win.addDockWidget(Qt.RightDockWidgetArea, self.chat)
        self.timeline_undos = 0
        undo = QAction("Undo", self.win)
        undo.setObjectName("actionUndo")
        undo.setShortcut(QKeySequence(QKeySequence.Undo))
        self.undo_action = undo
        undo.triggered.connect(self._timeline_undo)
        self.win.addAction(undo)
        self.win.resize(800, 500)
        self.win.show()
        QApplication.setActiveWindow(self.win)
        QApplication.processEvents()

    def _timeline_undo(self):
        self.timeline_undos += 1

    def focus(self, widget):
        widget.setFocus(Qt.OtherFocusReason)
        QApplication.processEvents()

    @property
    def proxy(self):
        return self.chat._chat_view.proxy


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    ed = _Editor()
    yield ed
    ed.win.close()
    ed.win.deleteLater()
    app.processEvents()


def test_focus_on_the_chat_proxy_owns_edit_keys(editor):
    editor.focus(editor.chat._chat_view)
    assert QApplication.focusWidget() is editor.proxy
    assert chat_owns_clipboard_keys(editor.chat, QApplication.focusWidget()) is True


def test_focus_elsewhere_never_routes_edit_keys_to_the_chat(editor):
    editor.focus(editor.timeline)
    focus = QApplication.focusWidget()
    assert focus is editor.timeline
    # The proxy is a child of the chat view whether or not it has focus.
    assert editor.chat._chat_view.focusProxy() is editor.proxy
    assert chat_owns_clipboard_keys(editor.chat, focus, under_mouse=False) is False
    assert chat_owns_clipboard_keys(editor.chat, focus, under_mouse=True) is False


def test_ctrl_z_in_the_focused_chat_removes_the_last_chip_not_a_timeline_step(editor):
    editor.focus(editor.chat._chat_view)
    QTest.keyClick(editor.proxy, Qt.Key_Z, Qt.ControlModifier)
    assert editor.chat.attachment_batches == [["a.png"]]
    assert editor.timeline_undos == 0
    # Handled before the page, so Chromium never runs its own text undo too.
    assert Qt.Key_Z not in editor.proxy.page_keys


def test_ctrl_z_in_the_focused_chat_without_chips_still_stays_in_the_chat(editor):
    editor.chat.attachment_batches = []
    editor.focus(editor.chat._chat_view)
    QTest.keyClick(editor.proxy, Qt.Key_Z, Qt.ControlModifier)
    assert editor.chat.undo_calls == 1
    assert editor.timeline_undos == 0


def test_a_rebound_undo_shortcut_in_the_focused_chat_stays_in_the_chat(editor):
    # Preferences > Keyboard lets the user give Undo another key sequence.
    editor.undo_action.setShortcut(QKeySequence("Ctrl+Alt+U"))
    editor.focus(editor.chat._chat_view)
    QTest.keyClick(editor.proxy, Qt.Key_U, Qt.ControlModifier | Qt.AltModifier)
    assert editor.chat.attachment_batches == [["a.png"]]
    assert editor.timeline_undos == 0

    editor.focus(editor.timeline)
    QTest.keyClick(editor.timeline, Qt.Key_U, Qt.ControlModifier | Qt.AltModifier)
    assert editor.timeline_undos == 1


def test_ctrl_z_with_the_timeline_focused_undoes_the_timeline(editor):
    editor.focus(editor.timeline)
    QTest.keyClick(editor.timeline, Qt.Key_Z, Qt.ControlModifier)
    assert editor.timeline_undos == 1
    assert editor.chat.undo_calls == 0


def test_plain_typing_in_the_chat_still_reaches_the_page(editor):
    editor.focus(editor.chat._chat_view)
    QTest.keyClick(editor.proxy, Qt.Key_A)
    assert editor.proxy.page_keys == [Qt.Key_A]
    assert editor.chat.undo_calls == 0
