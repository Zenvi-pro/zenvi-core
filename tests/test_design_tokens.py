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
    window = re.search(r"scroll_color = (.+)\n(.*)\n", source)
    assert window and "setAlpha(" in window.group(0)
