"""
Embedded HTML docks: prefer Qt WebEngine on Linux/macOS; on Windows prefer Qt WebKit when
available so frozen installers align with MSYS2 (no Chromium). Falls back to the other engine
or None. Override with env ZENVI_EMBED_BACKEND=webkit|webengine.
"""

import os
import sys

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401

    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False
    QWebEngineView = None  # type: ignore

try:
    from PyQt5.QtWebKitWidgets import QWebView  # noqa: F401

    HAS_WEBKIT = True
except ImportError:
    HAS_WEBKIT = False
    QWebView = None  # type: ignore


def web_embed_backend():
    """Return 'webengine', 'webkit', or None."""
    override = os.environ.get("ZENVI_EMBED_BACKEND", "").strip().lower()
    if override == "webkit" and HAS_WEBKIT:
        return "webkit"
    if override == "webengine" and HAS_WEBENGINE:
        return "webengine"
    # Windows: WebKit-first when both exist (installer / MSYS2 parity; avoids Chromium).
    if sys.platform == "win32":
        if HAS_WEBKIT:
            return "webkit"
        if HAS_WEBENGINE:
            return "webengine"
        return None
    if HAS_WEBENGINE:
        return "webengine"
    if HAS_WEBKIT:
        return "webkit"
    return None


def run_js(view, backend, code):
    """Run JavaScript on QWebEngineView or QWebView."""
    if backend == "webengine":
        view.page().runJavaScript(code)
    elif backend == "webkit":
        frame = view.page().mainFrame()
        if frame:
            frame.evaluateJavaScript(code)
    else:
        raise ValueError("invalid backend for run_js")


def attach_webkit_window_object(web_view, js_object_name, qobject):
    """
    Register a QObject on each document load (QtWebKit pattern).
    Call before load()/setHtml().
    """
    page = web_view.page()

    def _on_cleared():
        page.mainFrame().addToJavaScriptWindowObject(js_object_name, qobject)

    page.mainFrame().javaScriptWindowObjectCleared.connect(_on_cleared)
