"""Zenvi design tokens: the one place the dark theme's colors and sizes live.

The Qt stylesheet (``{name}`` placeholders, see ``resolve``) and the widgets that
paint themselves (timeline, properties sliders, scopes) read these, so a change
here restyles the whole app. True-black base, hairline structure, one accent.
"""

TOKENS = {
    # Surfaces, darkest to lightest
    "bg": "#000000",          # window floor, viewer stage
    "surface": "#0a0a0a",     # panels
    "sunken": "#121212",      # inputs, tables, lists
    "raised": "#171717",      # buttons, menus, popovers
    "hover": "#1f1f1f",
    "active": "#262626",
    # Lines
    "line": "#1c1c1c",        # hairlines between panels and rows
    "line_strong": "#2a2a2a", # control borders
    # Text
    "text": "#ededed",
    "text_muted": "#a1a1a1",
    "text_faint": "#6b6b6b",
    "text_off": "#4a4a4a",
    "white": "#ffffff",
    # Accent (Zenvi brand blue) — focus, selection, progress only
    "accent": "#3a72f5",
    "accent_hover": "#5b8cff",
    "accent_soft": "rgba(58, 114, 245, 0.16)",
    # Primary action (Cursor convention: light button on black)
    "primary": "#ededed",
    "primary_text": "#0a0a0a",
    "primary_hover": "#ffffff",
    # Status
    "danger": "#f04438",
    "success": "#3ccf6e",
    "playhead": "#ff4a3d",
    # Timeline items
    "clip": "#1a1a1a",
    "clip_border": "#2e2e2e",
}

FONT_FAMILY = "Inter"
FONT_POINT_SIZE = 9.75  # 13px at 96 dpi


def resolve(text):
    """Replace ``{name}`` token placeholders in a stylesheet."""
    for name, value in TOKENS.items():
        text = text.replace("{%s}" % name, value)
    return text
