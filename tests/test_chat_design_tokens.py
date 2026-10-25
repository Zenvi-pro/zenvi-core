"""The chat dock takes its look from the cosmic tokens (themes/cosmic/tokens.py).

The dock is a web page (src/chat_ui) painted by Qt WebKit or WebEngine, plus a few
Qt widgets. CSS cannot import Python, so chat.css mirrors the tokens in :root and
the Python side reads TOKENS directly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from themes.cosmic.tokens import TOKENS

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
CHAT_UI = SRC / "chat_ui"
CHAT_CSS = CHAT_UI / "chat.css"
WEBKIT_CSS = CHAT_UI / "chat-webkit.css"
PY_FILES = [SRC / "windows" / name for name in (
    "ai_chat_ui.py", "agent_panel.py", "agent_selector_button.py",
    "chat_web_view.py", "update_status_button.py")]

# tokens.py describes the cosmic theme only. The other app themes keep their own
# palettes in CHAT_THEME_COLORS, so those three entries are the one exemption.
OTHER_THEMES = {"Bloomberg Light", "Humanity: Dark", "Retro"}
COLOR_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(")

# Tokens chat.css declares in :root (CSS name = token name with "-" for "_").
MIRRORED = ["surface", "sunken", "raised", "hover", "line", "line_strong",
            "text", "text_muted", "text_faint", "accent", "accent_soft",
            "primary", "primary_text", "danger", "success"]
ROOT_DECL = re.compile(r"--([a-z][a-z0-9-]*)\s*:\s*([^;]+);")

# Qt WebKit 602 (Safari 10) drops these. chat-webkit.css must not use them.
UNSUPPORTED_ON_WEBKIT_602 = re.compile(
    r"(?<![\w-])(?:gap|row-gap|column-gap|inset)\s*:"
    r"|\b(?:min|max|clamp|color-mix)\("
    r"|:is\(|:where\(|::placeholder\b|\bgrid\b")


def _hard_coded_colors(path):
    """Line numbers of color literals and QColor(r, g, b) calls outside the other themes' palettes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in OTHER_THEMES:
                    exempt.update(range(value.lineno, value.end_lineno + 1))
        elif isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            # A docstring's "(#147)" is an issue number, not a color.
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                exempt.update(range(first.lineno, first.end_lineno + 1))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if COLOR_LITERAL.search(node.value):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "QColor":
            if len(node.args) >= 3 and all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in node.args[:3]):
                lines.append(node.lineno)
    return sorted(line for line in lines if line not in exempt)


def _rule_body(css, selector):
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else ""


def test_python_colors_come_from_the_tokens():
    offenders = [f"{path.name}:{line}" for path in PY_FILES for line in _hard_coded_colors(path)]
    assert offenders == [], "hard-coded colors (use TOKENS): " + ", ".join(offenders)


def test_chat_css_root_mirrors_the_tokens():
    css = CHAT_CSS.read_text(encoding="utf-8")
    declared = dict(ROOT_DECL.findall(re.search(r":root\s*\{([^}]*)\}", css).group(1)))
    for key in MIRRORED:
        name = key.replace("_", "-")
        assert name in declared, f"--{name} is not declared in :root"
        assert declared[name].strip().lower() == TOKENS[key].lower(), f"--{name} != tokens.{key}"


def test_dock_font_is_inter_then_the_existing_fallbacks():
    checks = [
        (CHAT_CSS, "body"), (CHAT_CSS, ".font-sans"),
        (WEBKIT_CSS, 'html[data-zenvi-webkit="1"] body'),
        (WEBKIT_CSS, 'html[data-zenvi-webkit="1"] .font-sans'),
    ]
    for path, selector in checks:
        family = re.search(r"font-family:\s*([^;]+);", _rule_body(path.read_text(encoding="utf-8"), selector))
        assert family and family.group(1).strip().startswith("Inter"), f"{path.name}: {selector}"


def test_webkit_css_uses_only_what_webkit_602_has():
    css = re.sub(r"/\*.*?\*/", "", WEBKIT_CSS.read_text(encoding="utf-8"), flags=re.S)
    assert UNSUPPORTED_ON_WEBKIT_602.search(css) is None, UNSUPPORTED_ON_WEBKIT_602.search(css)


def test_every_chat_theme_sets_every_chat_slot():
    # Light themes left --chat-surface on the dark token: dark text on a dark surface
    import re as _re
    from pathlib import Path as _Path

    from windows.ai_chat_ui import CHAT_THEME_COLORS

    css = (_Path(__file__).resolve().parents[1] / "src" / "chat_ui" / "chat.css").read_text(encoding="utf-8")
    root = css[css.index(":root"):css.index("}", css.index(":root"))]
    slots = set(_re.findall(r"--(chat-[a-z-]+):", root)) | {"accent-soft"}
    for name, palette in CHAT_THEME_COLORS.items():
        assert slots - set(palette) == set(), name
