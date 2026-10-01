"""zenvi-core #141 — the Windows (Qt WebKit) Zenvi Assistant dock must not clip.

``tests/fixtures/webkit_chat_layout_probe.py`` loads ``src/chat_ui`` in a real
Qt WebKit view the way ``AIChatWindow`` embeds it and reports where things
landed. Skipped where Qt WebKit is not installed (it is on the Windows build,
which is the only place this path runs).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_WEBKIT_CSS = _REPO / "src" / "chat_ui" / "chat-webkit.css"
_PROBE = _REPO / "tests" / "fixtures" / "webkit_chat_layout_probe.py"

_DARK = {"chat-bg": "#161616", "chat-surface": "#1e1e1e", "chat-text": "#d4d4d4",
         "chat-border": "#2a2a2a", "chat-input-bg": "#1a1a1a", "chat-accent": "#4d9cf6"}
_LIGHT = {"chat-bg": "#f0f0f0", "chat-text": "#333333", "chat-border": "#ccc",
          "chat-input-bg": "#ffffff", "chat-accent": "#217dd4"}


def _probe(width, height, theme=_DARK):
    proc = subprocess.run(
        [sys.executable, str(_PROBE), str(width), str(height), json.dumps(theme)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode == 3:
        pytest.skip("Qt WebKit is not installed")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_webkit_css_never_forces_display_over_inline_none():
    """`display: flex !important` on a utility class re-shows every element the
    page hides with an inline `display:none` — that was the #141 clip."""
    css = _WEBKIT_CSS.read_text(encoding="utf-8")
    assert not re.search(r"display:[^;]*!important", css)


@pytest.mark.parametrize("width,height", [(420, 620), (300, 340)])
def test_messages_fill_the_dock_and_nothing_is_cut_off(width, height):
    m = _probe(width, height)

    # The hidden "CLI not installed" panel must stay hidden, so the message
    # list runs all the way down to the composer.
    assert m["emptyStateDisplay"] == "none"
    assert m["inputRow"]["top"] - m["messages"]["bottom"] < 16

    # A tall reply appended in one go is scrolled fully into view.
    assert m["lastLine"]["bottom"] <= m["messages"]["bottom"] + 1

    # Composer, send button and the new-chat button stay inside the dock.
    assert m["inputRow"]["bottom"] <= m["viewport"]["height"]
    assert m["send"]["right"] <= m["inputRow"]["right"]
    assert m["send"]["bottom"] <= m["inputRow"]["bottom"]
    assert m["newChatButton"]["right"] <= m["viewport"]["width"]


def test_history_overlay_covers_the_dock():
    """`inset: 0` and `min()` do not exist on this WebKit; the overlay collapsed."""
    m = _probe(420, 620)
    overlay, panel = m["historyOverlay"], m["historyPanel"]
    assert overlay["right"] - overlay["left"] == m["viewport"]["width"]
    assert overlay["bottom"] - overlay["top"] == m["viewport"]["height"]
    assert 200 < panel["right"] - panel["left"] <= 360


def test_clicks_at_a_buttons_edge_are_not_dropped():
    """Previous-chats and the tab × shrank on press, so a click near their edge
    released outside the button and did nothing; users had to click again."""
    m = _probe(420, 620)
    assert m["historyOpensOnEdgeClick"]
    assert m["tabCloseFiresOnEdgeClick"]


def test_light_theme_is_not_painted_dark():
    m = _probe(420, 620, _LIGHT)
    assert m["bodyBackground"] == "rgb(240, 240, 240)"
    assert m["messagesBackground"] in ("rgba(0, 0, 0, 0)", "rgb(240, 240, 240)")


def test_windows_fonts_replace_generic_fallbacks():
    """Tailwind's ui-sans-serif / ui-monospace resolve to Arial / Courier New here."""
    m = _probe(420, 620)
    assert m["inputFont"].lstrip("'\"").startswith("Segoe UI")
    assert "Consolas" in m["codeFont"]
