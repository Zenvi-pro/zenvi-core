"""Keep the Agents chat webview from navigating away on file drops."""

from __future__ import annotations

import os

_CHAT_SCHEMES = frozenset({"data", "about", "qrc", "blob"})


def is_allowed_chat_navigation(url, chat_ui_dir: str) -> bool:
    """True when *url* is the chat page or a resource under ``chat_ui/``."""
    if url is None:
        return False
    scheme = ""
    try:
        scheme = (url.scheme() or "").lower()
    except Exception:
        return False
    if scheme in _CHAT_SCHEMES:
        return True
    try:
        if not url.isLocalFile():
            return False
        path = os.path.abspath(url.toLocalFile() or "")
    except Exception:
        return False
    if not path or not chat_ui_dir:
        return False
    root = os.path.abspath(chat_ui_dir)
    return path == root or path.startswith(root + os.sep)
