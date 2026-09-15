"""Chat webview that refuses to navigate to dropped media files."""

from __future__ import annotations

import json
import os

from PyQt5.QtCore import QEvent, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence

from classes.chat_navigation import is_allowed_chat_navigation
from classes.file_drop import accept_os_file_drag, local_path_from_url, urls_from_mime
from classes.logger import log

try:
    from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
except ImportError:
    QWebEnginePage = None
    QWebEngineView = object

try:
    from PyQt5.QtWebKitWidgets import QWebView, QWebPage
except ImportError:
    QWebView = None
    QWebPage = None


# Standard edit keys the main window also binds as WindowShortcut timeline ops.
_EDIT_SHORTCUTS = (
    ("copy", QKeySequence.Copy),
    ("cut", QKeySequence.Cut),
    ("paste", QKeySequence.Paste),
    ("selectAll", QKeySequence.SelectAll),
)
_WEB_EDIT_ACTIONS = {
    "copy": "Copy",
    "cut": "Cut",
    "paste": "Paste",
    "selectAll": "SelectAll",
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


def chat_owns_clipboard_keys(chat, focus_widget=None, under_mouse=False) -> bool:
    """True when clipboard shortcuts should go to the assistant chat, not the timeline.

    WebEngine often reports ``focusWidget() is None``; in that case *under_mouse*
    (the chat view is under the cursor) is the fallback.
    """
    if chat is None:
        return False
    is_visible = getattr(chat, "isVisible", None)
    if callable(is_visible) and not is_visible():
        return False
    view = getattr(chat, "_chat_view", None)
    if focus_widget is not None:
        widget = focus_widget
        while widget is not None:
            if widget is chat or (view is not None and widget is view):
                return True
            parent_fn = getattr(widget, "parentWidget", None)
            widget = parent_fn() if callable(parent_fn) else None
        return False
    return bool(view and under_mouse)


def dispatch_chat_edit_action(chat, name: str, focus_widget=None, under_mouse=False) -> bool:
    """Send an edit action to the focused chat surface. Returns True if handled."""
    if not name or not chat_owns_clipboard_keys(chat, focus_widget, under_mouse):
        return False
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


class ChatEditShortcutMixin:
    """Claim edit keys so main-window timeline shortcuts do not steal them."""

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
        if event.type() == QEvent.ShortcutOverride and is_edit_shortcut(event):
            event.accept()
            return True
        if event.type() == QEvent.ChildAdded:
            child = event.child()
            if hasattr(child, "installEventFilter"):
                child.installEventFilter(self)
        return super().event(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.ShortcutOverride and is_edit_shortcut(event):
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        name = edit_shortcut_name(event)
        if name and trigger_web_edit_action(self, name):
            event.accept()
            return
        super().keyPressEvent(event)


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
