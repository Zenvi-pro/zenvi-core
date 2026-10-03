"""Edit > Undo / Redo and the timeline toolbar buttons undo the project even
while the assistant chat is docked and visible (RC v1.2.0 B1).

The chat claims Ctrl/Cmd+Z itself while it has keyboard focus
(tests/test_chat_edit_keys.py), so the Undo / Redo actions never route to it.
Real-Qt test: importing windows.main_window needs libopenshot.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("openshot")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default


_app = QApplication.instance() or QApplication([])
if not hasattr(_app, "get_settings"):
    _app.get_settings = lambda: _Settings()
if not hasattr(_app, "_tr"):
    _app._tr = lambda s: s

# Same reason as test_save_project_error_dialog.py: undo never touches the
# timeline view, and importing it needs GL setup this session cannot promise.
if "windows.views.timeline" not in sys.modules:
    _stub = types.ModuleType("windows.views.timeline")
    _stub.TimelineView = type("TimelineView", (), {})
    sys.modules["windows.views.timeline"] = _stub

import windows.main_window as main_window  # noqa: E402


class _Widget:
    def __init__(self, parent=None, focused=False):
        self._parent = parent
        self.focused = focused

    def parentWidget(self):
        return self._parent

    def hasFocus(self):
        return self.focused

    def underMouse(self):
        return False


class _ChatView(_Widget):
    def __init__(self, parent):
        super().__init__(parent)
        # QtWebEngine's focus proxy: always a child of the view.
        self.proxy = _Widget(parent=self)

    def focusProxy(self):
        return self.proxy


class _VisibleChat(_Widget):
    def __init__(self, proxy_focused):
        super().__init__()
        self._chat_view = _ChatView(self)
        self._chat_view.proxy.focused = proxy_focused
        self.chat_undos = 0

    def isVisible(self):
        return True

    def undo_chat_attachments(self):
        self.chat_undos += 1
        return True


@pytest.mark.parametrize("proxy_focused", [False, True], ids=["chat-unfocused", "chat-focused"])
@pytest.mark.parametrize("action", ["undo", "redo"])
def test_undo_redo_actions_always_reach_the_project(monkeypatch, action, proxy_focused):
    app = MagicMock()
    monkeypatch.setattr(main_window, "get_app", lambda: app)
    chat = _VisibleChat(proxy_focused)
    win = types.SimpleNamespace(dockAIChat=chat, refreshFrameSignal=MagicMock())

    trigger = getattr(main_window.MainWindow, "action%s_trigger" % action.capitalize())
    trigger(win)

    getattr(app.updates, action).assert_called_once_with()
    win.refreshFrameSignal.emit.assert_called_once_with()
    assert chat.chat_undos == 0
