"""Clipboard shortcuts in the assistant chat vs timeline copyAll."""

import pytest

pytest.importorskip("PyQt5.QtGui")
from PyQt5.QtGui import QKeySequence  # noqa: E402

from windows.chat_web_view import (
    chat_owns_clipboard_keys,
    dispatch_chat_edit_action,
    edit_shortcut_name,
    is_edit_shortcut,
    trigger_web_edit_action,
    web_edit_action,
)


class _FakeEvent:
    def __init__(self, hit=None):
        self._hit = hit

    def matches(self, seq):
        return seq == self._hit


class _FakeWidget:
    def __init__(self, parent=None):
        self._parent = parent

    def parentWidget(self):
        return self._parent


class _FakeChat:
    def __init__(self, visible=True, view=None, chat_box=None):
        self._visible = visible
        self._chat_view = view
        self.chat_box = chat_box

    def isVisible(self):
        return self._visible


class _FakePage:
    Copy = "COPY"
    Cut = "CUT"
    Paste = "PASTE"
    SelectAll = "SELECT_ALL"

    def __init__(self):
        self.actions = []

    def triggerAction(self, action):
        self.actions.append(action)


class _FakeView:
    def __init__(self):
        self._page = _FakePage()

    def page(self):
        return self._page


class _FakeBox:
    def __init__(self):
        self.copied = 0
        self.selected = 0

    def copy(self):
        self.copied += 1

    def selectAll(self):
        self.selected += 1


def test_edit_shortcut_name_maps_standard_keys():
    assert edit_shortcut_name(_FakeEvent(QKeySequence.Copy)) == "copy"
    assert edit_shortcut_name(_FakeEvent(QKeySequence.Cut)) == "cut"
    assert edit_shortcut_name(_FakeEvent(QKeySequence.Paste)) == "paste"
    assert edit_shortcut_name(_FakeEvent(QKeySequence.SelectAll)) == "selectAll"
    assert edit_shortcut_name(_FakeEvent(QKeySequence.Undo)) is None
    assert is_edit_shortcut(_FakeEvent(QKeySequence.Copy)) is True
    assert is_edit_shortcut(_FakeEvent(QKeySequence.Undo)) is False
    assert is_edit_shortcut(None) is False
    assert is_edit_shortcut(object()) is False


def test_chat_owns_clipboard_keys_when_focus_is_in_chat():
    chat = _FakeChat()
    child = _FakeWidget(parent=chat)
    assert chat_owns_clipboard_keys(chat, child) is True
    view = _FakeWidget()
    chat_with_view = _FakeChat(view=view)
    assert chat_owns_clipboard_keys(chat_with_view, view) is True
    outsider = _FakeWidget()
    assert chat_owns_clipboard_keys(chat, outsider) is False
    assert chat_owns_clipboard_keys(None, child) is False
    hidden = _FakeChat(visible=False)
    assert chat_owns_clipboard_keys(hidden, _FakeWidget(parent=hidden)) is False


def test_chat_owns_clipboard_keys_under_mouse_when_unfocused():
    view = object()
    chat = _FakeChat(view=view)
    assert chat_owns_clipboard_keys(chat, None, under_mouse=True) is True
    assert chat_owns_clipboard_keys(chat, None, under_mouse=False) is False
    other = _FakeWidget()
    assert chat_owns_clipboard_keys(chat, other, under_mouse=True) is False


def test_web_edit_action_reads_class_enum():
    page = _FakePage()
    assert web_edit_action(page, "copy") == "COPY"
    assert web_edit_action(page, "cut") == "CUT"
    assert web_edit_action(page, "paste") == "PASTE"
    assert web_edit_action(page, "selectAll") == "SELECT_ALL"
    assert web_edit_action(page, "nope") is None
    assert web_edit_action(None, "copy") is None


def test_trigger_web_edit_action_dispatches_page_action():
    view = _FakeView()
    assert trigger_web_edit_action(view, "copy") is True
    assert view._page.actions == [_FakePage.Copy]
    assert trigger_web_edit_action(view, "selectAll") is True
    assert view._page.actions[-1] == _FakePage.SelectAll
    assert trigger_web_edit_action(None, "copy") is False
    assert trigger_web_edit_action(view, "undo") is False


def test_dispatch_chat_edit_action_uses_webview():
    view = _FakeView()
    chat = _FakeChat(view=view)
    child = _FakeWidget(parent=chat)
    assert dispatch_chat_edit_action(chat, "copy", child) is True
    assert view._page.actions == [_FakePage.Copy]
    outsider = _FakeWidget()
    assert dispatch_chat_edit_action(chat, "copy", outsider) is False
    assert view._page.actions == [_FakePage.Copy]


def test_dispatch_widget_fallback_without_webview():
    box = _FakeBox()
    chat = _FakeChat(view=None, chat_box=box)
    child = _FakeWidget(parent=chat)
    assert dispatch_chat_edit_action(chat, "copy", child) is True
    assert box.copied == 1
    assert dispatch_chat_edit_action(chat, "selectAll", child) is True
    assert box.selected == 1
    assert dispatch_chat_edit_action(chat, "copy", _FakeWidget()) is False
    assert box.copied == 1
