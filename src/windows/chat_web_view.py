"""Chat webview that refuses to navigate to dropped media files."""

from __future__ import annotations

import json
import os

from qt_api import QEvent, QUrl, pyqtSignal
from qt_api import QAction, QColor, QKeySequence

from classes.chat_navigation import is_allowed_chat_navigation
from classes.file_drop import accept_os_file_drag, local_path_from_url, urls_from_mime
from classes.logger import log

try:
    from qt_api import QWebEnginePage, QWebEngineView
except ImportError:
    QWebEnginePage = None
    QWebEngineView = object

try:
    from qt_api import QWebView, QWebPage
except ImportError:
    QWebView = None
    QWebPage = None


# Standard edit keys the main window also binds as WindowShortcut timeline ops.
# getattr: headless Qt stubs (and some Qt builds) lack these StandardKey attrs.
_EDIT_SHORTCUTS = tuple(
    (name, seq)
    for name, seq in (
        ("copy", getattr(QKeySequence, "Copy", None)),
        ("cut", getattr(QKeySequence, "Cut", None)),
        ("paste", getattr(QKeySequence, "Paste", None)),
        ("selectAll", getattr(QKeySequence, "SelectAll", None)),
        ("undo", getattr(QKeySequence, "Undo", None)),
        ("redo", getattr(QKeySequence, "Redo", None)),
    )
    if seq is not None
)
_WEB_EDIT_ACTIONS = {
    "copy": "Copy",
    "cut": "Cut",
    "paste": "Paste",
    "selectAll": "SelectAll",
    "undo": "Undo",
    "redo": "Redo",
}


def is_edit_shortcut(event) -> bool:
    """True when *event* is Copy / Cut / Paste / Select All."""
    return edit_shortcut_name(event) is not None


def edit_shortcut_name(event):
    """Return 'copy' / 'cut' / 'paste' / 'selectAll', or None."""
    matches = getattr(event, "matches", None)
    if not callable(matches):
        return None
    for name, seq in _EDIT_SHORTCUTS:
        try:
            if matches(seq):
                return name
        except Exception:
            continue
    return None


def web_edit_action(page, name: str):
    """Map copy/cut/paste/selectAll to a QWebEnginePage / QWebPage WebAction."""
    attr = _WEB_EDIT_ACTIONS.get(name)
    if not attr or page is None:
        return None
    return getattr(type(page), attr, None) or getattr(page, attr, None)


def trigger_web_edit_action(view, name: str) -> bool:
    """Run Copy/Cut/Paste/SelectAll on a chat web view. Returns True if dispatched."""
    if view is None or not name:
        return False
    page_fn = getattr(view, "page", None)
    page = page_fn() if callable(page_fn) else None
    action = web_edit_action(page, name)
    trigger = getattr(page, "triggerAction", None) if page is not None else None
    if action is None or not callable(trigger):
        return False
    trigger(action)
    return True


def local_paths_from_urls(urls) -> list:
    """Existing local file paths from a list of QUrl-like objects."""
    paths = []
    seen = set()
    for url in urls or []:
        is_local = getattr(url, "isLocalFile", None)
        to_local = getattr(url, "toLocalFile", None)
        if not callable(is_local) or not callable(to_local) or not is_local():
            continue
        path = to_local() or ""
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            paths.append(path)
    return paths


def attach_chat_media_urls(chat, urls) -> bool:
    """Attach local file URLs on the chat composer. True if any chip was added."""
    paths = local_paths_from_urls(urls)
    attach = getattr(chat, "attach_paths", None) if chat is not None else None
    if not paths or not callable(attach):
        return False
    return attach(paths) > 0


def try_attach_clipboard_media(chat) -> bool:
    """Persist clipboard stills/files and attach chips. True if any chip was added.

    Needed for Windows Snipping Tool / unsaved screenshots that only exist as
    clipboard image bytes (no file URL). The chat webview steals Ctrl+V, so this
    must run from the chat key path — not only from the main-window Paste action.
    """
    if chat is None:
        return False
    try:
        from classes.app import get_app
        app = get_app()
        win = getattr(app, "window", None) if app else None
        if win is None:
            return False
        clipboard = app.clipboard()
        mime = clipboard.mimeData() if clipboard else None
        if not mime:
            return False
        contains = getattr(win, "clipboard_contains_media", None)
        collect = getattr(win, "_collect_clipboard_media_urls", None)
        if not callable(contains) or not callable(collect):
            return False
        if not contains(mime):
            return False
        urls, _ = collect(mime, create_files=True)
        return attach_chat_media_urls(chat, urls)
    except Exception:
        log.debug("try_attach_clipboard_media failed", exc_info=1)
        return False


def chat_owns_clipboard_keys(chat, focus_widget=None, under_mouse=False) -> bool:
    """True when clipboard shortcuts should go to the assistant chat, not the timeline.

    Keyboard focus decides. A *focus_widget* inside the chat dock or its web
    view means the chat owns the key; a focused widget anywhere else never
    does, even with the chat under the mouse. WebEngine keeps focus on a
    focus-proxy child of the view, so the proxy counts only while it has
    focus: it is always a descendant of the view, so ancestry proves nothing.
    *under_mouse* is a fallback only when no widget has focus at all.
    """
    if chat is None:
        return False
    is_visible = getattr(chat, "isVisible", None)
    if callable(is_visible) and not is_visible():
        return False
    view = getattr(chat, "_chat_view", None)

    def _widget_in_chat(widget):
        while widget is not None:
            if widget is chat or (view is not None and widget is view):
                return True
            parent_fn = getattr(widget, "parentWidget", None)
            widget = parent_fn() if callable(parent_fn) else None
        return False

    if focus_widget is not None:
        return _widget_in_chat(focus_widget)

    if view is not None:
        try:
            if view.hasFocus():
                return True
        except Exception:
            pass
        try:
            # WebEngine's focus proxy is always a child of the view, so only a
            # proxy that actually has focus means the chat owns the keys.
            focus_proxy = view.focusProxy() if callable(getattr(view, "focusProxy", None)) else None
            if focus_proxy is not None and hasattr(focus_proxy, "hasFocus") and focus_proxy.hasFocus():
                return True
        except Exception:
            pass

    # The pointer decides only when no other widget holds keyboard focus.
    return bool(view and under_mouse and focus_widget is None)


def dispatch_chat_edit_action(chat, name: str, focus_widget=None, under_mouse=False) -> bool:
    """Send an edit action to the focused chat surface. Returns True if handled."""
    if not name or not chat_owns_clipboard_keys(chat, focus_widget, under_mouse):
        return False
    if name == "paste" and try_attach_clipboard_media(chat):
        return True
    if name == "undo":
        undo_fn = getattr(chat, "undo_chat_attachments", None)
        if callable(undo_fn) and undo_fn():
            return True
    view = getattr(chat, "_chat_view", None)
    if view is not None:
        return trigger_web_edit_action(view, name)
    method = name
    if focus_widget is not None:
        fn = getattr(focus_widget, method, None)
        if callable(fn):
            fn()
            return True
    box = getattr(chat, "chat_box", None)
    if name in ("copy", "selectAll") and box is not None:
        fn = getattr(box, method, None)
        if callable(fn):
            fn()
            return True
    return False


def _chat_window_for_view(view):
    """Walk parents to find AIChatWindow (has attach_paths / undo_chat_attachments)."""
    widget = view
    while widget is not None:
        if callable(getattr(widget, "attach_paths", None)) or callable(
            getattr(widget, "undo_chat_attachments", None)
        ):
            return widget
        parent_fn = getattr(widget, "parentWidget", None)
        widget = parent_fn() if callable(parent_fn) else None
    return None


class ChatEditShortcutMixin:
    """Claim edit keys so main-window timeline shortcuts do not steal them.

    ShortcutOverride is accepted on the view and its children, so while the
    chat has keyboard focus the main window's Undo / Copy / Paste shortcuts
    never fire; the key press is handled here instead. QtWebKit delivers it to
    the view (keyPressEvent). QtWebEngine delivers it to its focus-proxy child,
    which would hand it straight to Chromium, so the child filter handles it.
    """

    def _enable_edit_shortcuts(self):
        self.installEventFilter(self)
        self._watch_child_edit_filters(self)

    def _watch_child_edit_filters(self, widget):
        children_fn = getattr(widget, "children", None)
        children = children_fn() if callable(children_fn) else ()
        for child in children:
            if hasattr(child, "installEventFilter"):
                child.installEventFilter(self)
                self._watch_child_edit_filters(child)

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self._edit_key_name(event):
            event.accept()
            return True
        if event.type() == QEvent.ChildAdded:
            child = event.child()
            if hasattr(child, "installEventFilter"):
                child.installEventFilter(self)
        return super().event(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.ShortcutOverride and self._edit_key_name(event):
            event.accept()
            return True
        if event.type() == QEvent.KeyPress and obj is not self and self._handle_edit_key(event):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if self._handle_edit_key(event):
            return
        super().keyPressEvent(event)

    def _edit_key_name(self, event):
        """edit_shortcut_name, plus the user's own Undo / Redo keys.

        Preferences > Keyboard can give those actions any key sequence. The
        actions always change the project, so a focused chat has to claim the
        configured keys too, not only the standard ones.
        """
        name = edit_shortcut_name(event)
        if name or not callable(getattr(event, "key", None)):
            return name
        try:
            combo = (event.keyCombination() if hasattr(event, "keyCombination")
                     else int(event.modifiers()) | event.key())
            pressed = QKeySequence(combo)
            for name in ("undo", "redo"):
                action = self.window().findChild(QAction, "action" + name.title())
                if action is not None and pressed in action.shortcuts():
                    return name
        except Exception:
            log.warning("Could not read the configured Undo/Redo shortcuts", exc_info=True)
        return None

    def _handle_edit_key(self, event) -> bool:
        """Run an edit shortcut on the chat. True when the key press was consumed."""
        name = self._edit_key_name(event)
        if not name:
            return False
        chat = _chat_window_for_view(self)
        if name == "paste" and try_attach_clipboard_media(chat):
            event.accept()
            return True
        if name == "undo":
            undo_fn = getattr(chat, "undo_chat_attachments", None) if chat else None
            if callable(undo_fn) and undo_fn():
                event.accept()
                return True
        if trigger_web_edit_action(self, name) or name in ("undo", "redo"):
            # Undo/Redo are consumed even with nothing to undo, so a focused
            # chat never falls through to the timeline's Undo.
            event.accept()
            return True
        return False


def _dropped_paths(event):
    paths = []
    seen = set()
    for url in urls_from_mime(event.mimeData() if event else None):
        path = local_path_from_url(url)
        if path and os.path.isfile(path) and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _project_file_ids_from_event(event):
    mime = event.mimeData() if event else None
    if mime is None or not hasattr(mime, "text"):
        return []
    try:
        data = json.loads(mime.text() or "")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if x]


def _accept_chat_file_drag(event) -> bool:
    if accept_os_file_drag(event):
        return True
    if _project_file_ids_from_event(event):
        event.accept()
        return True
    return False


if QWebEnginePage is not None:

    class ChatWebEnginePage(QWebEnginePage):
        """Block main-frame navigations to videos/images dropped on the chat."""

        rejectedFileUrl = pyqtSignal(str)

        def __init__(self, chat_ui_dir: str, parent=None):
            super().__init__(parent)
            self._chat_ui_dir = chat_ui_dir

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if is_allowed_chat_navigation(url, self._chat_ui_dir):
                return True
            if is_main_frame:
                path = ""
                try:
                    if url.isLocalFile():
                        path = url.toLocalFile() or ""
                except Exception:
                    path = ""
                if path and os.path.isfile(path):
                    log.info("Chat webview blocked navigation to %s", path)
                    self.rejectedFileUrl.emit(path)
            return False

    class ChatWebEngineView(ChatEditShortcutMixin, QWebEngineView):
        filesDropped = pyqtSignal(list)
        fileIdsDropped = pyqtSignal(list)

        def __init__(self, chat_ui_dir: str, parent=None):
            super().__init__(parent)
            self._chat_ui_dir = chat_ui_dir
            page = ChatWebEnginePage(chat_ui_dir, self)
            page.setBackgroundColor(QColor(13, 13, 13))
            page.rejectedFileUrl.connect(self._on_rejected_file)
            self.setPage(page)
            self.setAcceptDrops(True)
            self._enable_edit_shortcuts()

        def _on_rejected_file(self, path: str):
            if path:
                self.filesDropped.emit([path])

        def dragEnterEvent(self, event):
            if _accept_chat_file_drag(event):
                return
            super().dragEnterEvent(event)

        def dragMoveEvent(self, event):
            if _accept_chat_file_drag(event):
                return
            super().dragMoveEvent(event)

        def dropEvent(self, event):
            paths = _dropped_paths(event)
            ids = _project_file_ids_from_event(event)
            if paths:
                event.accept()
                self.filesDropped.emit(paths)
                return
            if ids:
                event.accept()
                self.fileIdsDropped.emit(ids)
                return
            event.ignore()


if QWebView is not None and QWebPage is not None:

    class ChatWebKitPage(QWebPage):
        rejectedFileUrl = pyqtSignal(str)

        def __init__(self, chat_ui_dir: str, parent=None):
            super().__init__(parent)
            self._chat_ui_dir = chat_ui_dir

        def acceptNavigationRequest(self, frame, request, nav_type):
            url = request.url() if request is not None else QUrl()
            if is_allowed_chat_navigation(url, self._chat_ui_dir):
                return True
            path = ""
            try:
                if url.isLocalFile():
                    path = url.toLocalFile() or ""
            except Exception:
                path = ""
            if path and os.path.isfile(path):
                self.rejectedFileUrl.emit(path)
            return False

    class ChatWebKitView(ChatEditShortcutMixin, QWebView):
        filesDropped = pyqtSignal(list)
        fileIdsDropped = pyqtSignal(list)

        def __init__(self, chat_ui_dir: str, parent=None):
            super().__init__(parent)
            page = ChatWebKitPage(chat_ui_dir, self)
            page.rejectedFileUrl.connect(self._on_rejected_file)
            self.setPage(page)
            self.setAcceptDrops(True)
            self._enable_edit_shortcuts()

        def _on_rejected_file(self, path: str):
            if path:
                self.filesDropped.emit([path])

        def dragEnterEvent(self, event):
            if _accept_chat_file_drag(event):
                return
            super().dragEnterEvent(event)

        def dragMoveEvent(self, event):
            if _accept_chat_file_drag(event):
                return
            super().dragMoveEvent(event)

        def dropEvent(self, event):
            paths = _dropped_paths(event)
            ids = _project_file_ids_from_event(event)
            if paths:
                event.accept()
                self.filesDropped.emit(paths)
                return
            if ids:
                event.accept()
                self.fileIdsDropped.emit(ids)
                return
            event.ignore()
