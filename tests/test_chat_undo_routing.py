"""Undo/Redo and clipboard keys go to the chat only when the chat really has the keys.

RC v1.2.0 (#216, blocker B1): with the chat dock visible, which is the default
layout, every timeline Undo/Redo went to the chat. WebEngine's focus proxy is
always a child of the chat view, so ``_widget_in_chat(focus_proxy)`` was always
true; and the pointer over the chat beat a timeline widget holding focus.
"""

from __future__ import annotations

from windows.chat_web_view import chat_owns_clipboard_keys


class _Widget:
    def __init__(self, parent=None, focused=False):
        self._parent = parent
        self._focused = focused

    def parentWidget(self):
        return self._parent

    def hasFocus(self):
        return self._focused


class _View(_Widget):
    def __init__(self, parent, proxy_focused=False):
        super().__init__(parent)
        self._proxy = _Widget(self, focused=proxy_focused)

    def focusProxy(self):
        return self._proxy


class _Chat(_Widget):
    def __init__(self, proxy_focused=False):
        super().__init__()
        self._chat_view = _View(self, proxy_focused=proxy_focused)

    def isVisible(self):
        return True


def test_a_visible_chat_without_focus_does_not_own_undo():
    assert chat_owns_clipboard_keys(_Chat(), None, under_mouse=False) is False


def test_the_web_page_holding_focus_owns_undo():
    assert chat_owns_clipboard_keys(_Chat(proxy_focused=True), None, under_mouse=False) is True


def test_a_focused_widget_inside_the_chat_owns_undo():
    chat = _Chat()
    field = _Widget(chat._chat_view, focused=True)
    assert chat_owns_clipboard_keys(chat, field, under_mouse=False) is True


def test_the_pointer_does_not_beat_a_focused_timeline():
    timeline = _Widget(focused=True)
    assert chat_owns_clipboard_keys(_Chat(), timeline, under_mouse=True) is False


def test_the_pointer_decides_when_nothing_has_focus():
    assert chat_owns_clipboard_keys(_Chat(), None, under_mouse=True) is True
