"""
Caption control panel — PIL-based caption renderer UI.
Lives in dockCaptionEditor, tabified with the left nav docks.
"""
import logging

from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# (preset_key, display_name, accent_hex)
_PRESET_INFO = [
    ("karaoke",   "Karaoke",   "#FFE600"),
    ("word",      "Word",      "#FFFFFF"),
    ("fire",      "Fire",      "#FF7800"),
    ("clean",     "Clean",     "#FFFFFF"),
    ("neon",      "Neon",      "#00DCFF"),
    ("block",     "Block",     "#AAAAAA"),
    ("netflix",   "Netflix",   "#E50914"),
    ("tiktok",    "TikTok",    "#69C9D0"),
    ("cinematic", "Cinematic", "#FFFAD2"),
    ("bold",      "Bold",      "#FFE600"),
    ("minimal",   "Minimal",   "#FFFFFF"),
    ("subtitle",  "Subtitle",  "#CCCCCC"),
]

_LANGUAGES = [
    ("",   "Auto-detect"),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("it", "Italiano"),
    ("pt", "Português"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("zh", "中文"),
]

_POSITIONS = [
    ("top",    "Top"),
    ("center", "Center"),
    ("bottom", "Bottom"),
]

_ANIM_OPTIONS = [
    ("preset",   "From Preset"),
    ("standard", "Standard"),
    ("karaoke",  "Karaoke"),
    ("word",     "Word-by-Word"),
]

_FONTS = [
    ("", "Default"),
    ("Arial", "Arial"),
    ("Arial Black", "Arial Black"),
    ("Impact", "Impact"),
    ("Georgia", "Georgia"),
    ("Helvetica Neue", "Helvetica Neue"),
    ("Times New Roman", "Times New Roman"),
    ("Courier New", "Courier New"),
]


# ── QSS tokens ────────────────────────────────────────────────────────────────
_QSS_PANEL = """
QWidget#caption_panel_root { background: #0d0d0d; }
"""

_QSS_TILE_IDLE = """
QPushButton {{
    background: #141414;
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 5px;
    color: #d4d4d4;
    font-size: 10px;
    font-weight: 400;
    padding: 5px 2px;
}}
QPushButton:hover {{
    background: #1e1e1e;
    border-color: rgba(255,255,255,0.14);
}}
"""

_QSS_TILE_SELECTED = """
QPushButton {{
    background: #1a1a1a;
    border: 1.5px solid {accent};
    border-radius: 5px;
    color: {accent};
    font-size: 10px;
    font-weight: 600;
    padding: 5px 2px;
}}
"""

_QSS_BTN_PRIMARY = """
QPushButton {
    background: #4d9cf6;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    padding: 7px 14px;
}
QPushButton:hover  { background: #3b8fe8; }
QPushButton:pressed { background: #2d7fd6; }
QPushButton:disabled { background: #1e1e1e; color: #444444; }
"""

_QSS_BTN_SECONDARY = """
QPushButton {
    background: #1a1a1a;
    color: #d4d4d4;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 6px;
    font-size: 12px;
    padding: 5px 10px;
}
QPushButton:hover  { background: #202020; border-color: rgba(255,255,255,0.16); }
QPushButton:pressed { background: #2a2a2a; }
QPushButton:disabled { background: #141414; color: #444444; }
"""

_QSS_COMBO = """
QComboBox {
    background: #141414;
    color: #d4d4d4;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 5px;
    padding: 3px 6px;
    font-size: 12px;
}
QComboBox:hover { border-color: rgba(255,255,255,0.18); }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid rgba(255,255,255,0.1);
    selection-background-color: #252525;
    outline: none;
}
"""

_QSS_SLIDER = """
QSlider::groove:horizontal {
    height: 3px;
    background: #2a2a2a;
    border-radius: 1px;
}
QSlider::handle:horizontal {
    background: #4d9cf6;
    width: 12px; height: 12px;
    border-radius: 6px;
    margin: -5px 0;
}
QSlider::sub-page:horizontal {
    background: #4d9cf6;
    border-radius: 1px;
}
"""

_QSS_RADIO = """
QRadioButton {
    color: #d4d4d4;
    font-size: 11px;
    spacing: 4px;
}
QRadioButton::indicator {
    width: 12px; height: 12px;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.25);
    background: #141414;
}
QRadioButton::indicator:checked {
    background: #4d9cf6;
    border-color: #4d9cf6;
}
"""

_QSS_SCROLL = """
QScrollArea { background: #0d0d0d; border: none; }
QScrollBar:vertical {
    background: #0d0d0d; width: 5px; border: none; margin: 0;
}
QScrollBar::handle:vertical {
    background: #303030; border-radius: 2px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


# ── Worker ────────────────────────────────────────────────────────────────────

class _CaptionWorker(QObject):
    status = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, fn, kwargs):
        super().__init__()
        self._fn = fn
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(**self._kwargs)
            self.status.emit(str(result).strip() if result else "Done")
        except Exception as exc:
            log.exception("CaptionWorker error")
            self.status.emit(f"Error: {exc}")
        finally:
            self.done.emit()


# ── Preset tile ───────────────────────────────────────────────────────────────

class _PresetTile(QPushButton):
    def __init__(self, key: str, name: str, accent: str, parent=None):
        super().__init__(parent)
        self._key = key
        self._accent = accent
        self.setCheckable(True)
        self.setText(name)
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._refresh()

    @property
    def preset_key(self) -> str:
        return self._key

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        self._refresh()

    def _refresh(self):
        if self.isChecked():
            self.setStyleSheet(_QSS_TILE_SELECTED.format(accent=self._accent))
        else:
            self.setStyleSheet(_QSS_TILE_IDLE)


# ── Main panel ────────────────────────────────────────────────────────────────

class CaptionPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("caption_panel_root")

        # State
        self._selected_preset = "karaoke"
        self._custom_text_color = ""
        self._thread = None
        self._worker = None

        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet("background: #0d0d0d; color: #d4d4d4;")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(_QSS_SCROLL)

        inner = QWidget()
        inner.setStyleSheet("background: #0d0d0d;")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(7)

        # ── Preset grid ────────────────────────────────────────────
        layout.addWidget(self._section_header("STYLES"))

        grid_wrap = QWidget()
        grid_wrap.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)

        self._tiles: dict[str, _PresetTile] = {}
        self._tile_group = QButtonGroup(self)
        self._tile_group.setExclusive(True)

        for idx, (key, name, accent) in enumerate(_PRESET_INFO):
            tile = _PresetTile(key, name, accent, parent=grid_wrap)
            self._tile_group.addButton(tile)
            self._tiles[key] = tile
            row, col = divmod(idx, 4)
            grid.addWidget(tile, row, col)
            tile.clicked.connect(lambda _checked, k=key: self._on_preset_clicked(k))

        self._tiles[self._selected_preset].setChecked(True)
        layout.addWidget(grid_wrap)

        # ── Customise section ──────────────────────────────────────
        layout.addWidget(self._divider())
        layout.addWidget(self._section_header("CUSTOMIZE"))

        # Font
        font_row = QHBoxLayout()
        font_row.addWidget(self._label("Font"))
        self._font_combo = QComboBox()
        for val, display in _FONTS:
            self._font_combo.addItem(display, val)
        self._font_combo.setStyleSheet(_QSS_COMBO)
        font_row.addWidget(self._font_combo, 1)
        layout.addLayout(font_row)

        # Font size
        size_row = QHBoxLayout()
        size_row.addWidget(self._label("Size"))
        self._size_val_lbl = QLabel("Default")
        self._size_val_lbl.setFixedWidth(44)
        self._size_val_lbl.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._size_slider = QSlider(Qt.Horizontal)
        self._size_slider.setRange(0, 100)
        self._size_slider.setValue(0)
        self._size_slider.setStyleSheet(_QSS_SLIDER)
        self._size_slider.valueChanged.connect(self._on_size_changed)
        size_row.addWidget(self._size_slider, 1)
        size_row.addWidget(self._size_val_lbl)
        layout.addLayout(size_row)

        # Text colour + BG opacity
        colour_row = QHBoxLayout()
        colour_row.addWidget(self._label("Text"))
        self._text_colour_btn = self._colour_swatch("#FFFFFF")
        self._text_colour_btn.clicked.connect(self._pick_text_colour)
        colour_row.addWidget(self._text_colour_btn)
        colour_row.addSpacing(6)
        colour_row.addWidget(self._label("BG%"))
        self._bg_opacity_slider = QSlider(Qt.Horizontal)
        self._bg_opacity_slider.setRange(0, 100)
        self._bg_opacity_slider.setValue(0)
        self._bg_opacity_slider.setStyleSheet(_QSS_SLIDER)
        self._bg_opacity_lbl = QLabel("0%")
        self._bg_opacity_lbl.setFixedWidth(28)
        self._bg_opacity_lbl.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._bg_opacity_slider.valueChanged.connect(self._on_bg_opacity_changed)
        colour_row.addWidget(self._bg_opacity_slider, 1)
        colour_row.addWidget(self._bg_opacity_lbl)
        layout.addLayout(colour_row)

        # Position
        pos_row = QHBoxLayout()
        pos_row.addWidget(self._label("Pos"))
        self._pos_group = QButtonGroup(self)
        for val, display in _POSITIONS:
            rb = QRadioButton(display)
            rb.setStyleSheet(_QSS_RADIO)
            rb.setProperty("pos_val", val)
            self._pos_group.addButton(rb)
            pos_row.addWidget(rb)
        # default: bottom
        self._pos_group.buttons()[-1].setChecked(True)
        layout.addLayout(pos_row)

        # Animation override
        anim_row = QHBoxLayout()
        anim_row.addWidget(self._label("Anim"))
        self._anim_combo = QComboBox()
        for val, display in _ANIM_OPTIONS:
            self._anim_combo.addItem(display, val)
        self._anim_combo.setStyleSheet(_QSS_COMBO)
        anim_row.addWidget(self._anim_combo, 1)
        layout.addLayout(anim_row)

        # Language
        lang_row = QHBoxLayout()
        lang_row.addWidget(self._label("Lang"))
        self._lang_combo = QComboBox()
        for code, display in _LANGUAGES:
            self._lang_combo.addItem(display, code)
        self._lang_combo.setStyleSheet(_QSS_COMBO)
        lang_row.addWidget(self._lang_combo, 1)
        layout.addLayout(lang_row)

        # ── Action buttons ─────────────────────────────────────────
        layout.addWidget(self._divider())

        self._add_btn = QPushButton("Add Captions")
        self._add_btn.setMinimumHeight(34)
        self._add_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._add_btn.setStyleSheet(_QSS_BTN_PRIMARY)
        self._add_btn.clicked.connect(self._on_add)
        layout.addWidget(self._add_btn)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        self._restyle_btn = QPushButton("Restyle")
        self._remove_btn = QPushButton("Remove")
        for btn in (self._restyle_btn, self._remove_btn):
            btn.setMinimumHeight(30)
            btn.setStyleSheet(_QSS_BTN_SECONDARY)
        self._restyle_btn.clicked.connect(self._on_restyle)
        self._remove_btn.clicked.connect(self._on_remove)
        sub_row.addWidget(self._restyle_btn)
        sub_row.addWidget(self._remove_btn)
        layout.addLayout(sub_row)

        # Status
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setStyleSheet("color: #6b7280; font-size: 11px; padding: 2px 0;")
        layout.addWidget(self._status_lbl)

        layout.addStretch()

        scroll.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll)

    # ── Widget helpers ─────────────────────────────────────────────────

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #555555; font-size: 10px; font-weight: 700; "
            "letter-spacing: 1.5px; padding: 0; background: transparent;"
        )
        return lbl

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setMaximumHeight(1)
        line.setStyleSheet("background: rgba(255,255,255,0.06);")
        return line

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFixedWidth(32)
        lbl.setStyleSheet("color: #6b7280; font-size: 11px; background: transparent;")
        return lbl

    def _colour_swatch(self, hex_color: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(22, 22)
        btn.setProperty("colour_hex", hex_color)
        self._apply_swatch_colour(btn, hex_color)
        return btn

    def _apply_swatch_colour(self, btn: QPushButton, hex_color: str):
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {hex_color};
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 4px;
            }}
            QPushButton:hover {{ border-color: rgba(255,255,255,0.30); }}
        """)

    # ── Slot: preset tile clicked ──────────────────────────────────────

    def _on_preset_clicked(self, key: str):
        self._selected_preset = key

    # ── Slot: size slider ──────────────────────────────────────────────

    def _on_size_changed(self, val: int):
        self._size_val_lbl.setText("Default" if val == 0 else str(val))

    # ── Slot: BG opacity ──────────────────────────────────────────────

    def _on_bg_opacity_changed(self, val: int):
        self._bg_opacity_lbl.setText(f"{val}%")

    # ── Slot: colour picker ────────────────────────────────────────────

    def _pick_text_colour(self):
        current = QColor(self._text_colour_btn.property("colour_hex") or "#ffffff")
        colour = QColorDialog.getColor(current, self, "Text Colour")
        if colour.isValid():
            hex_c = colour.name()
            self._custom_text_color = hex_c
            self._text_colour_btn.setProperty("colour_hex", hex_c)
            self._apply_swatch_colour(self._text_colour_btn, hex_c)

    # ── Helpers: read UI state ─────────────────────────────────────────

    def _selected_clip_id(self) -> str | None:
        try:
            from classes.app import get_app
            clips = getattr(get_app().window, "selected_clips", [])
            return clips[0] if clips else None
        except Exception:
            return None

    def _style_kwargs(self) -> dict:
        kwargs = {}
        font_val = self._font_combo.currentData()
        if font_val:
            kwargs["font_name"] = font_val
        size = self._size_slider.value()
        if size > 0:
            kwargs["font_size"] = size
        if self._custom_text_color:
            kwargs["font_color"] = self._custom_text_color
        bg_pct = self._bg_opacity_slider.value()
        if bg_pct > 0:
            kwargs["bg_opacity"] = bg_pct / 100.0
        for btn in self._pos_group.buttons():
            if btn.isChecked():
                pos = btn.property("pos_val")
                if pos != "bottom":
                    kwargs["position"] = pos
                break
        return kwargs

    def _effective_style(self) -> str:
        """Return the preset key, potentially overridden by the animation combo."""
        anim = self._anim_combo.currentData()
        if anim == "karaoke":
            return "karaoke"
        if anim == "word":
            return "word"
        return self._selected_preset

    # ── Helpers: busy state ────────────────────────────────────────────

    def _set_busy(self, busy: bool, msg: str = ""):
        self._add_btn.setEnabled(not busy)
        self._restyle_btn.setEnabled(not busy)
        self._remove_btn.setEnabled(not busy)
        if msg:
            self._status_lbl.setText(msg)

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)

    # ── Background thread runner ───────────────────────────────────────

    def _run_bg(self, fn, kwargs: dict, busy_msg: str):
        # Clean up any previous thread
        if self._thread and self._thread.isRunning():
            return  # don't stack calls
        self._set_busy(True, busy_msg)
        self._thread = QThread(self)
        self._worker = _CaptionWorker(fn, kwargs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status.connect(self._set_status)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(lambda: self._set_busy(False))
        self._thread.start()

    # ── Action handlers ────────────────────────────────────────────────

    def _on_add(self):
        clip_id = self._selected_clip_id()
        if not clip_id:
            self._status_lbl.setText("Select a clip first")
            return
        from classes.tool_handlers import add_captions_to_timeline
        self._run_bg(
            add_captions_to_timeline,
            {
                "clip_id": clip_id,
                "language": self._lang_combo.currentData(),
                "style": self._effective_style(),
            },
            "Transcribing…",
        )

    def _on_restyle(self):
        clip_id = self._selected_clip_id()
        if not clip_id:
            self._status_lbl.setText("Select a clip first")
            return
        from classes.tool_handlers import style_captions
        kwargs = self._style_kwargs()
        kwargs["clip_id"] = clip_id
        kwargs["preset"] = self._effective_style()
        self._run_bg(style_captions, kwargs, "Restyling…")

    def _on_remove(self):
        clip_id = self._selected_clip_id()
        if not clip_id:
            self._status_lbl.setText("Select a clip first")
            return
        try:
            from classes.app import get_app
            from classes.query import Clip
            from classes.tool_handlers import _remove_old_pil_captions
            clip = Clip.get(id=clip_id)
            if clip:
                _remove_old_pil_captions(clip, get_app())
                self._status_lbl.setText("Captions removed")
            else:
                self._status_lbl.setText("Clip not found")
        except Exception as exc:
            log.exception("Remove captions error")
            self._status_lbl.setText(f"Error: {exc}")
