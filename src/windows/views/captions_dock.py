"""The Captions dock: pick a HyperFrames caption style for the selected clip.

The dock is a front for the same ``add_captions_tool`` / ``remove_captions_tool``
the assistant uses, so there is one captioning path. The words shown are the
clip's own transcript; editing them fixes what the recogniser got wrong.
"""

from __future__ import annotations

import threading

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from classes import hyperframes_captions as cap
from classes.app import get_app
from classes.logger import log


class CaptionsDock(QWidget):
    """Style picker, editable transcript and Generate / Remove for the selected clip."""

    finished = pyqtSignal(str)                 # status line of a finished tool call
    wordsLoaded = pyqtSignal(str, str, str)    # clip id, words, status line

    def __init__(self, parent=None):
        super().__init__(parent)
        _ = get_app()._tr
        self._clip_id = ""
        self._busy = False

        self.clipLabel = QLabel(_("Select a clip with speech on the timeline."))
        self.clipLabel.setWordWrap(True)
        self.styleCombo = QComboBox()
        for key, style in cap.STYLES.items():
            self.styleCombo.addItem(_(style["label"]), key)
        self.styleCombo.setCurrentIndex(self.styleCombo.findData(cap.DEFAULT_STYLE))
        self.styleHint = QLabel()
        self.styleHint.setWordWrap(True)
        self.behindSubject = QCheckBox(_("Behind the speaker (slower)"))
        self.behindSubject.setToolTip(_("Cuts the speaker out and places them on a track above the captions."))
        self.wordsEdit = QPlainTextEdit()
        self.wordsEdit.setPlaceholderText(_("The clip's words appear here. Edit them to fix the captions."))
        self.loadButton = QPushButton(_("Load words"))
        self.generateButton = QPushButton(_("Generate captions"))
        self.removeButton = QPushButton(_("Remove"))
        self.statusLabel = QLabel()
        self.statusLabel.setWordWrap(True)

        buttons = QHBoxLayout()
        for button in (self.loadButton, self.generateButton, self.removeButton):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.clipLabel, self.styleCombo, self.styleHint, self.behindSubject, self.wordsEdit):
            layout.addWidget(widget)
        layout.addLayout(buttons)
        layout.addWidget(self.statusLabel)

        self.styleCombo.currentIndexChanged.connect(self._show_style_hint)
        self.loadButton.clicked.connect(self.load_words)
        self.generateButton.clicked.connect(self.generate)
        self.removeButton.clicked.connect(self.remove)
        self.finished.connect(self._on_finished)
        self.wordsLoaded.connect(self._on_words_loaded)
        get_app().window.SelectionChanged.connect(self.selection_changed)
        self._show_style_hint()
        self.selection_changed()

    # -- selection ----------------------------------------------------------

    def selection_changed(self):
        from classes.query import Clip

        selected = list(getattr(get_app().window, "selected_clips", None) or [])
        clip = Clip.get(id=selected[0]) if len(selected) == 1 else None
        clip_id = str(clip.id) if clip else ""
        if clip_id != self._clip_id:
            self._clip_id = clip_id
            self.wordsEdit.clear()
            _ = get_app()._tr
            self.clipLabel.setText(_("Captions for: %s") % (clip.data.get("title") or clip_id) if clip
                                   else _("Select a clip with speech on the timeline."))
        self._refresh_enabled()

    def _refresh_enabled(self):
        ready = bool(self._clip_id) and not self._busy
        for widget in (self.loadButton, self.generateButton, self.removeButton):
            widget.setEnabled(ready)

    def _show_style_hint(self):
        style = cap.STYLES.get(self.styleCombo.currentData() or "", {})
        self.styleHint.setText(get_app()._tr(style.get("description", "")))

    # -- actions (the tools do the work, off the GUI thread) ------------------

    def _run(self, status, tool_name, args):
        if self._busy or not self._clip_id:
            return
        self._busy = True
        self._refresh_enabled()
        self.statusLabel.setText(status)

        def work():
            from classes.tool_handlers import execute_tool
            try:
                out = str(execute_tool(tool_name, args))
            except Exception as exc:
                log.error("Captions dock: %s failed", tool_name, exc_info=True)
                out = "Error: %s" % exc
            self.finished.emit(out.split("\n", 1)[0])

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def load_words(self):
        if self._busy or not self._clip_id:
            return
        self._busy = True
        self._refresh_enabled()
        _ = get_app()._tr
        self.statusLabel.setText(_("Reading the clip's speech..."))
        clip_id = self._clip_id

        def work():
            from classes.editor_tools import titles_text_captions as tool
            from classes.query import Clip
            try:
                words = tool.clip_caption_words(Clip.get(id=clip_id))
                text = " ".join(w["text"] for w in words)
                message = _("%d words loaded.") % len(words) if words else _("No speech was found in this clip.")
            except Exception as exc:
                text, message = "", "Error: %s" % exc
            self.wordsLoaded.emit(clip_id, text, message)

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def _on_words_loaded(self, clip_id, text, message):
        self._busy = False
        if clip_id == self._clip_id:                    # the selection may have moved on meanwhile
            if text:
                self.wordsEdit.setPlainText(text)
            self.statusLabel.setText(message)
        self._refresh_enabled()

    def generate(self):
        _ = get_app()._tr
        self._run(_("Rendering captions on this computer..."), "add_captions_tool", {
            "timeline_clip_id": self._clip_id,
            "style": self.styleCombo.currentData(),
            "behind_subject": self.behindSubject.isChecked(),
            "text": self.wordsEdit.toPlainText().strip(),
        })

    def remove(self):
        _ = get_app()._tr
        self._run(_("Removing captions..."), "remove_captions_tool", {"timeline_clip_id": self._clip_id})

    def _on_finished(self, message):
        self._busy = False
        self.statusLabel.setText(message)
        self._refresh_enabled()
