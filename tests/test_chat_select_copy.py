"""Highlighting chat text and copying it, without undoing #256's rules for timeline Undo/Copy/Paste."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from windows.chat_web_view import chat_has_text_selection, chat_owns_clipboard_keys, dispatch_chat_edit_action  # noqa: E402

CHAT_UI = SRC / "chat_ui"


class Page:
    Copy, Cut, Paste, SelectAll = "COPY", "CUT", "PASTE", "SELECT_ALL"

    def __init__(self):
        self.actions = []

    def triggerAction(self, action):
        self.actions.append(action)


class View:
    def __init__(self, selected=False, has_selection_api=True):
        self._page, self._selected = Page(), selected
        if has_selection_api:
            self.hasSelection = lambda: self._selected
        else:
            self.selectedText = lambda: "hi" if self._selected else ""

    def page(self):
        return self._page


def chat(view, visible=True):
    return SimpleNamespace(_chat_view=view, isVisible=lambda: visible)


class Elsewhere:
    """A widget outside the chat (the timeline) that holds Qt focus."""

    def parentWidget(self):
        return None


# ============================ copy follows the highlight ============================
def test_highlighted_chat_text_is_copied_even_when_the_timeline_has_focus():
    v = View(selected=True)
    assert dispatch_chat_edit_action(chat(v), "copy", Elsewhere(), under_mouse=False) is True
    assert v.page().actions == [Page.Copy]


def test_with_nothing_highlighted_copy_stays_with_the_timeline():
    v = View(selected=False)
    assert dispatch_chat_edit_action(chat(v), "copy", Elsewhere(), under_mouse=True) is False
    assert v.page().actions == []


def test_a_highlight_left_in_the_chat_does_not_beat_clips_the_user_selected_since():
    v = View(selected=True)
    assert dispatch_chat_edit_action(chat(v), "copy", Elsewhere(), under_mouse=False, timeline_has_selection=True) is False
    assert v.page().actions == []
    assert dispatch_chat_edit_action(chat(v), "copy", Elsewhere(), under_mouse=True, timeline_has_selection=True) is True, "the pointer is on the chat: copy that"


def test_only_copy_follows_the_highlight_cut_paste_undo_and_select_all_stay_with_the_timeline():
    v = View(selected=True)
    for name in ("cut", "paste", "undo", "redo", "selectAll"):
        assert dispatch_chat_edit_action(chat(v), name, Elsewhere(), under_mouse=False) is False, name
    assert v.page().actions == []


def test_a_hidden_chat_or_a_missing_view_never_claims_a_highlight():
    assert chat_has_text_selection(chat(View(selected=True), visible=False)) is False
    assert chat_has_text_selection(chat(None)) is False and chat_has_text_selection(None) is False
    assert dispatch_chat_edit_action(chat(View(selected=True), visible=False), "copy", Elsewhere()) is False


def test_either_selection_api_is_understood_and_a_failure_means_no_selection():
    assert chat_has_text_selection(chat(View(selected=True, has_selection_api=False))) is True
    assert chat_has_text_selection(chat(View(selected=False, has_selection_api=False))) is False
    boom = SimpleNamespace(hasSelection=lambda: (_ for _ in ()).throw(RuntimeError("page gone")))
    assert chat_has_text_selection(chat(boom)) is False


def test_a_focused_chat_still_copies_as_before():
    v = View(selected=False)
    child = SimpleNamespace(parentWidget=lambda: c)
    c = chat(v)
    assert dispatch_chat_edit_action(c, "copy", child) is True and v.page().actions == [Page.Copy]


# ============================ #256 stays: ownership is not widened ============================
def test_a_highlight_does_not_make_the_chat_own_the_keys():
    v = View(selected=True)
    assert chat_owns_clipboard_keys(chat(v), Elsewhere(), under_mouse=True) is False, "a focused timeline widget is never overruled by the pointer"
    assert chat_owns_clipboard_keys(chat(v), None, under_mouse=False) is False


# ============================ the page lets text be selected ============================
def css():
    return (CHAT_UI / "chat.css").read_text(encoding="utf-8")


def test_no_message_is_left_with_a_transform_after_it_appears():
    text = css()
    assert re.search(r"@keyframes chat-message-enter\s*\{\s*from\s*\{[^}]*\}\s*\}", text), "only the starting state is keyframed"
    for selector in (".chat-message", ".chat-thought-badge", ".chat-tool-block"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*chat-message-enter[^}]*)\}", text)
        assert block, selector
        assert "forwards" not in block.group(1) and "both" not in block.group(1), f"{selector} keeps its final transform"
        assert not re.search(r"(?<!-)transform\s*:", block.group(1)) and "opacity: 0" not in block.group(1), f"{selector} rests transformed or hidden"
    webkit = (CHAT_UI / "chat-webkit.css").read_text(encoding="utf-8")
    assert "forwards" not in re.search(r'\.chat-message \{[^}]*\}', webkit).group(0)


def test_the_whole_transcript_is_selectable_and_only_controls_are_not():
    text = css()
    assert re.search(r"#chat-messages,\s*#chat-messages \*\s*\{[^}]*user-select:\s*text", text)
    assert re.search(r"#chat-messages button[^{]*\{[^}]*user-select:\s*none", text)


def test_the_highlight_is_visible_on_the_dark_theme():
    m = re.search(r"#chat-messages ::selection\s*\{([^}]*)\}", css())
    assert m and "background" in m.group(1) and "rgba(77, 156, 246" in m.group(1) and "color" in m.group(1)


def test_the_chat_is_a_region_not_a_custom_application_shell():
    html = (CHAT_UI / "index.html").read_text(encoding="utf-8")
    assert 'role="application"' not in html and 'role="region" aria-label="Zenvi Assistant chat"' in html


def test_dragging_text_over_the_chat_is_never_taken_as_a_file_drop():
    from windows import chat_web_view as W
    calls = []
    mime = SimpleNamespace(text=lambda: "some highlighted words", hasUrls=lambda: False, urls=lambda: [], hasFormat=lambda f: False, formats=lambda: ["text/plain"])
    text_drag = SimpleNamespace(mimeData=lambda: mime, accept=lambda: calls.append("accept"), ignore=lambda: calls.append("ignore"),
                                setDropAction=lambda a: calls.append("action"))
    assert W._accept_chat_file_drag(text_drag) is False and "accept" not in calls, "a text drag is left to the page"
    ids = SimpleNamespace(mimeData=lambda: SimpleNamespace(text=lambda: '["F1"]', hasUrls=lambda: False, urls=lambda: [], hasFormat=lambda f: False,
                                                           formats=lambda: ["text/plain"]), accept=lambda: calls.append("accept"), ignore=lambda: None, setDropAction=lambda a: None)
    assert W._accept_chat_file_drag(ids) is True and calls[-1] == "accept", "project file ids are still accepted"


def test_cut_copies_the_timeline_selection_directly_not_through_chat_copy_routing():
    import ast

    src = (Path(__file__).resolve().parents[1] / "src" / "windows" / "main_window.py").read_text(encoding="utf-8")
    cut = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "cutAll")
    calls = {ast.unparse(n.func) for n in ast.walk(cut) if isinstance(n, ast.Call)}
    assert "self.copyAll" not in calls
    assert "self.timeline.Copy_Triggered" in calls
    assert "self.deleteItem" in calls
