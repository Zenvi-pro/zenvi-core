"""The dark theme takes every color from one token set (#330), so docks,
dialogs and self-painted widgets can't drift apart."""

from __future__ import annotations

import re
from pathlib import Path

from themes.cosmic import tokens

SRC = Path(__file__).resolve().parents[1] / "src"
THEME = SRC / "themes" / "cosmic" / "theme.py"
QSS = SRC / "themes" / "cosmic" / "zenvi.qss"


def test_resolve_replaces_placeholders():
    assert tokens.resolve("QWidget { color: {text}; }") == "QWidget { color: %s; }" % tokens.TOKENS["text"]


def test_theme_placeholders_all_exist():
    source = THEME.read_text(encoding="utf-8")
    used = set(re.findall(r"\{([a-z_]+)\}", source))
    python_names = set(re.findall(r"^\s*([a-z_]+) = ", source, re.M))  # f-string variables
    assert used - python_names - set(tokens.TOKENS) == set()


def test_qss_placeholders_all_exist():
    used = set(re.findall(r"\{([a-z_]+)\}", QSS.read_text(encoding="utf-8")))
    assert used - set(tokens.TOKENS) == set()


def test_theme_has_no_colors_outside_the_tokens():
    source = THEME.read_text(encoding="utf-8") + QSS.read_text(encoding="utf-8")
    hexes = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", source)}
    allowed = {v.lower() for v in tokens.TOKENS.values() if v.startswith("#")}
    assert hexes - allowed == set()


def test_inter_is_bundled_with_its_license():
    fonts = SRC / "images" / "fonts"
    for name in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-LICENSE.txt"):
        assert (fonts / name).is_file(), name
    assert tokens.FONT_FAMILY == "Inter"


def test_self_painted_timeline_chrome_uses_tokens():
    # The zoom minimap painted a saturated blue block over the timeline toolbar
    source = (SRC / "windows" / "views" / "zoom_slider.py").read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{6,8}\b", source) == []
    assert "TOKENS[" in source


def test_python_code_never_passes_a_raw_placeholder_to_qcolor():
    # QColor("{surface}") is an invalid color: the whole palette went black
    source = THEME.read_text(encoding="utf-8")
    assert re.findall(r"QColor\(\s*[\"']\{", source) == []


def test_minimap_window_is_translucent_so_clips_show_through():
    # The viewport window sits over the clip lines; an opaque fill hid them
    source = (SRC / "windows" / "views" / "zoom_slider.py").read_text(encoding="utf-8")
    alpha = re.search(r"scroll_color\.setAlpha\((\d+)\)", source)
    assert alpha and 1 <= int(alpha.group(1)) <= 254


def test_dock_border_reset_spares_controls():
    # "QDockWidget QWidget { border: none }" also hit combos, spin boxes and buttons in
    # docks, and their hover/focus rules only change border-color, so they lost borders
    qss = QSS.read_text(encoding="utf-8")
    for rule in re.findall(r"^([^{}\n]*QDockWidget[^{}\n]*)\{([^}]*)\}", qss, re.M):
        selector, body = rule
        if "border: none" not in body:
            continue
        for part in selector.split(","):
            assert not re.fullmatch(r"\s*QDockWidget(#\w+)?\s+QWidget\s*", part), part


def test_webengine_chat_page_follows_the_theme_background():
    # The WebEngine boot script pinned the body to the dark theme's surface color
    source = (SRC / "windows" / "ai_chat_ui.py").read_text(encoding="utf-8")
    assert "document.body.style.background = '%(surface)s'" not in source
    assert "document.body.style.background = 'var(--chat-bg)'" in source


def test_every_placeholder_string_in_the_theme_is_resolved():
    # The Export button's inline sheet kept "{accent}" literally, so Qt dropped its colors
    import ast

    tree = ast.parse(THEME.read_text(encoding="utf-8"))
    resolved = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "resolve":
            for arg in node.args:
                resolved.update(id(n) for n in ast.walk(arg))
        if isinstance(node, ast.JoinedStr):  # the message-box f-string joins self.style_sheet, resolved after
            resolved.update(id(n) for n in ast.walk(node))
    names = "|".join(sorted(tokens.TOKENS))
    leaks = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and re.search(r"\{(%s)\}" % names, n.value) and id(n) not in resolved
             and not n.value.lstrip().startswith(("/*", "QMainWindow"))]
    assert leaks == []


def test_font_hinting_enum_works_on_qt5_and_qt6():
    # PyQt6 keeps PreferNoHinting under QFont.HintingPreference; QFont.PreferNoHinting crashed theming
    assert "QFont.PreferNoHinting)" not in THEME.read_text(encoding="utf-8")


def test_activity_bar_sheets_agree_on_size_and_indicator():
    # The per-tab-bar sheet drew a right accent bar on top of the global left text bar
    source = THEME.read_text(encoding="utf-8")
    sheet = source[source.index('tabbar.setStyleSheet(resolve("""'):]
    sheet = sheet[:sheet.index('"""))')]
    assert "border-right: 2px solid {accent}" not in sheet
    assert "border-left: 2px solid {text}" in sheet
    assert "min-width:  40px" in sheet and "_tab_size = 40" in source


def test_disabled_tracks_look_different_from_enabled_ones():
    source = THEME.read_text(encoding="utf-8")
    track = re.search(r"\.track \{[^}]*background: (\{\w+\})", source).group(1)
    disabled = re.search(r"\.track_disabled \{[^}]*background: (\{\w+\})", source).group(1)
    assert track != disabled


def test_chat_page_loads_the_bundled_inter():
    # Qt WebEngine ignores QFontDatabase app fonts; the page needs @font-face
    css = (SRC / "chat_ui" / "chat.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{[^}]*font-family:\s*[\"']?Inter[^}]*\}", css)
    assert faces
    for url in re.findall(r"url\([\"']?([^)\"']+)", " ".join(faces)):
        assert (SRC / "chat_ui" / url).resolve().is_file(), url


def test_one_shared_parser_turns_tokens_into_qcolors():
    import os as _os

    import pytest as _pytest

    if _os.environ.get("ZENVI_REAL_QT") != "1":  # CI stubs Qt, so QColor would be a mock
        _pytest.skip("builds real QColors: run with ZENVI_REAL_QT=1")
    soft = tokens.qcolor("accent_soft")      # CSS rgba(), which QColor() cannot read
    assert (soft.red(), soft.green(), soft.blue(), soft.alpha()) == (58, 114, 245, 41)
    assert tokens.qcolor("accent").name() == tokens.TOKENS["accent"]
    copies = [p for p in (SRC / "windows").rglob("*.py")
              if 'startswith("rgba(")' in p.read_text(encoding="utf-8")]
    assert copies == []  # views use tokens.qcolor instead of their own parser
