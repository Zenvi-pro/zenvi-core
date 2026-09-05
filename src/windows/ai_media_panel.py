"""
 @file
 @brief Scene Descriptions panel — Pegasus audiovisual summary + indexing progress
 @author Zenvi Development Team

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 This file is part of OpenShot Video Editor (http://www.openshot.org)
"""

import os
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QTabWidget,
    QPushButton, QLabel, QFrame, QLineEdit,
    QSizePolicy, QProgressBar, QTextEdit,
)
from PyQt5.QtGui import QFont

from classes.logger import log
from classes.app import get_app

_PHASE_LABELS = {
    "uploading": "Uploading for search…",
    "indexing": "Indexing for search…",
    "summarizing": "Generating description…",
    "done": "Indexing complete",
}


def _section_header(text: str) -> QLabel:
    """Return a flat section-header label that replaces QGroupBox titles."""
    lbl = QLabel(text.upper())
    lbl.setObjectName("sectionHeader")
    font = lbl.font()
    font.setPointSizeF(8.5)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
    lbl.setFont(font)
    lbl.setStyleSheet(
        "QLabel#sectionHeader {"
        "  color: #737373;"
        "  padding: 6px 0 2px 0;"
        "  border: none;"
        "}"
    )
    return lbl


def _format_description_text(ai_meta: dict) -> str:
    """Build the panel body from Pegasus (or legacy) ai_metadata."""
    if not isinstance(ai_meta, dict):
        return ""
    short = str(ai_meta.get("short_summary") or "").strip()
    desc = str(ai_meta.get("description") or "").strip()
    if desc:
        if short and short not in desc:
            return f"{short}\n\n{desc}"
        return desc
    parts = []
    if short:
        parts.append(short)
    sounds = str(ai_meta.get("sounds") or "").strip()
    transcript = str(ai_meta.get("transcript") or "").strip()
    if sounds:
        parts.append(f"Sounds:\n{sounds}")
    if transcript:
        parts.append(f"Transcript:\n{transcript}")
    elif parts:
        parts.append("Transcript:\n(no speech)")
    return "\n\n".join(parts).strip()


class AIMediaPanel(QDockWidget):
    """Dock widget for AI media descriptions and indexing status."""

    analysisComplete = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Scene Descriptions", parent)
        self.setObjectName("AIMediaPanel")
        self._display_file_id = ""
        self._full_description = ""

        self.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )

        main = QWidget()
        main.setObjectName("AIMediaPanelContents")
        layout = QVBoxLayout()
        main.setLayout(layout)
        self.setWidget(main)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Timer must exist before _create_description_tab() — that path calls
        # refresh_tags() → update_selected_clip_description() → _stop_progress_timer().
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(2000)
        self.update_timer.timeout.connect(self._on_progress_timer)

        self._create_description_tab()

        self._wire_selection_signals()
        self.update_selected_clip_description()

        self.setMinimumWidth(300)
        self.setMinimumHeight(400)

    def _create_description_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        widget.setLayout(layout)

        self.desc_search = QLineEdit()
        self.desc_search.setPlaceholderText("Filter description…")
        self.desc_search.textChanged.connect(self._filter_description)
        layout.addWidget(self.desc_search)

        self.description_view = QTextEdit()
        self.description_view.setReadOnly(True)
        self.description_view.setAcceptRichText(False)
        self.description_view.setPlaceholderText("Select a clip to view its description")
        self.description_view.setStyleSheet(
            "QTextEdit { background: #141414; color: #d4d4d4; border: none; "
            "padding: 8px; font-size: 12px; line-height: 1.35; }"
        )
        layout.addWidget(self.description_view, stretch=3)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Plain)
        sep.setStyleSheet(
            "background: rgba(255,255,255,0.07); max-height: 1px; border: none; margin: 4px 0;"
        )
        layout.addWidget(sep)

        layout.addWidget(_section_header("Selected Clip"))
        self.selected_clip_label = QLabel("Select a clip to view scene descriptions")
        self.selected_clip_label.setWordWrap(True)
        self.selected_clip_label.setStyleSheet("color: #8a8a8a; font-size: 11px; padding: 2px 0;")
        layout.addWidget(self.selected_clip_label)

        self.indexing_status_label = QLabel("")
        self.indexing_status_label.setWordWrap(True)
        self.indexing_status_label.setStyleSheet("color: #6a9fd8; font-size: 10px; padding: 0;")
        self.indexing_status_label.hide()
        layout.addWidget(self.indexing_status_label)

        self.indexing_progress = QProgressBar()
        self.indexing_progress.setRange(0, 0)
        self.indexing_progress.setFixedHeight(3)
        self.indexing_progress.setTextVisible(False)
        self.indexing_progress.setStyleSheet(
            "QProgressBar { background: #1a1a1a; border: none; border-radius: 1px; }"
            "QProgressBar::chunk { background: #4d9cf6; border-radius: 1px; }"
        )
        self.indexing_progress.hide()
        layout.addWidget(self.indexing_progress)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        refresh_btn.setObjectName("tagsRefreshBtn")
        refresh_btn.clicked.connect(self.refresh_tags)
        layout.addWidget(refresh_btn)

        self.selected_clip_group = None
        # Back-compat aliases for any code still expecting old widgets
        self.tag_search = self.desc_search
        self.selected_tags_list = None
        self.tags_tree = None

        self.tabs.tabBar().setVisible(False)
        self.tabs.addTab(widget, "Description")

        self.refresh_tags()

    def refresh_tags(self):
        """Refresh the description view (based on current selection)."""
        prefer_files = None
        try:
            window = get_app().window
            if hasattr(window, "filesView") and window.filesView.hasFocus():
                prefer_files = True
        except Exception:
            pass
        self.update_selected_clip_description(prefer_files=prefer_files)

    def _filter_description(self, text):
        """Simple case-insensitive filter: hide non-matching content."""
        needle = (text or "").strip().lower()
        if not needle:
            self.description_view.setPlainText(self._full_description)
            return
        if not self._full_description:
            return
        kept = []
        for block in self._full_description.split("\n\n"):
            if needle in block.lower():
                kept.append(block)
        self.description_view.setPlainText("\n\n".join(kept) if kept else "(no matches)")

    def filter_tags(self, text):
        self._filter_description(text)

    def _wire_selection_signals(self):
        try:
            window = get_app().window
            files_model = getattr(window, "files_model", None)
            if files_model and files_model.selection_model:
                files_model.selection_model.selectionChanged.connect(
                    lambda *_a: self.update_selected_clip_description(prefer_files=True)
                )
                files_model.indexingProgress.connect(self._on_indexing_progress)
            window.FileUpdated.connect(self._on_file_updated)
            window.SelectionChanged.connect(
                lambda *_a: self.update_selected_clip_description(prefer_files=False)
            )
        except Exception as e:
            log.warning(f"Failed to connect selection signals for descriptions: {e}")

    def _on_file_updated(self, file_id):
        self.update_selected_clip_description()

    def _on_indexing_progress(self, file_id, phase, percent):
        if str(file_id) == str(self._display_file_id):
            self._update_indexing_ui(phase, percent, None)
            if phase != "done":
                self._start_progress_timer()
            else:
                self._stop_progress_timer()
        self.update_selected_clip_description()

    def _on_progress_timer(self):
        self.update_selected_clip_description()

    def _start_progress_timer(self):
        timer = getattr(self, "update_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()

    def _stop_progress_timer(self):
        timer = getattr(self, "update_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def _resolve_display_target(self, prefer_files=None):
        from classes.query import Clip, File

        window = get_app().window
        files_model = getattr(window, "files_model", None)
        file_obj = files_model.current_file() if files_model else None

        if prefer_files is None:
            try:
                prefer_files = bool(
                    hasattr(window, "filesView") and window.filesView.hasFocus()
                )
            except Exception:
                prefer_files = False

        timeline_clip = None
        try:
            selected_clip_ids = getattr(window, "selected_clips", []) or []
            if selected_clip_ids and not prefer_files:
                timeline_clip = Clip.get(id=selected_clip_ids[0])
        except Exception:
            timeline_clip = None

        if prefer_files and file_obj:
            return None, file_obj, str(file_obj.id)

        if timeline_clip:
            clip_data = timeline_clip.data if isinstance(getattr(timeline_clip, "data", None), dict) else {}
            fid = str(clip_data.get("file_id") or "")
            return timeline_clip, file_obj, fid

        if file_obj:
            return None, file_obj, str(file_obj.id)

        return None, None, ""

    def _load_ai_metadata(self, timeline_clip, file_obj):
        from classes.query import File
        from classes.ai_metadata_utils import adjust_scene_descriptions_for_subclip

        ai_meta = {}
        name = ""

        if timeline_clip and isinstance(getattr(timeline_clip, "data", None), dict):
            clip_data = timeline_clip.data
            name = clip_data.get("title") or clip_data.get("name") or "Timeline Clip"
            ai_meta = clip_data.get("ai_metadata") if isinstance(clip_data.get("ai_metadata"), dict) else {}

            if not ai_meta.get("analyzed"):
                try:
                    file_id = clip_data.get("file_id")
                    source_file = File.get(id=str(file_id)) if file_id else None
                    if source_file:
                        name = name or source_file.data.get("name") or os.path.basename(
                            source_file.data.get("path", "Clip")
                        )
                        candidate = source_file.data.get("ai_metadata")
                        if isinstance(candidate, dict) and candidate.get("analyzed"):
                            clip_start = float(clip_data.get("start", 0.0) or 0.0)
                            clip_end = float(clip_data.get("end", 0.0) or 0.0)
                            ai_meta = adjust_scene_descriptions_for_subclip(
                                candidate, clip_start, clip_end
                            )
                except Exception:
                    pass

        elif file_obj:
            name = file_obj.data.get("name") or os.path.basename(file_obj.data.get("path", "Clip"))
            candidate = file_obj.get_ai_metadata()
            ai_meta = candidate if isinstance(candidate, dict) else {}

        return ai_meta, name

    def _update_indexing_ui(self, phase, percent, twelvelabs):
        show = False
        label = ""

        if phase and phase != "done":
            show = True
            base = _PHASE_LABELS.get(phase, "Processing…")
            if phase == "uploading" and percent is not None and percent >= 0:
                label = f"{base} ({percent}%)"
            else:
                label = base
        elif isinstance(twelvelabs, dict):
            tw_status = str(twelvelabs.get("status") or "").lower()
            if tw_status == "indexing":
                show = True
                label = _PHASE_LABELS["indexing"]
            elif tw_status == "failed":
                err = twelvelabs.get("error") or "Indexing failed"
                self.indexing_status_label.setStyleSheet("color: #c96a6a; font-size: 10px; padding: 0;")
                self.indexing_status_label.setText(str(err))
                self.indexing_status_label.show()
                self.indexing_progress.hide()
                return

        if show:
            self.indexing_status_label.setStyleSheet("color: #6a9fd8; font-size: 10px; padding: 0;")
            self.indexing_status_label.setText(label)
            self.indexing_status_label.show()
            self.indexing_progress.show()
            if phase == "uploading" and percent is not None and percent >= 0:
                self.indexing_progress.setRange(0, 100)
                self.indexing_progress.setValue(min(100, max(0, percent)))
            else:
                self.indexing_progress.setRange(0, 0)
        else:
            self.indexing_status_label.hide()
            self.indexing_progress.hide()

    def _set_description(self, text: str):
        self._full_description = text or ""
        filter_text = self.desc_search.text() if self.desc_search else ""
        if filter_text.strip():
            self._filter_description(filter_text)
        else:
            self.description_view.setPlainText(self._full_description)

    def update_selected_clip_tags(self, prefer_files=None, *args, **kwargs):
        """Back-compat alias."""
        return self.update_selected_clip_description(prefer_files=prefer_files, *args, **kwargs)

    def update_selected_clip_description(self, prefer_files=None, *args, **kwargs):
        """Update the selected-clip description when selection or metadata changes."""
        try:
            window = get_app().window
            files_model = getattr(window, "files_model", None)

            timeline_clip, file_obj, file_id = self._resolve_display_target(prefer_files)
            self._display_file_id = file_id or ""

            if not timeline_clip and not file_obj:
                self.selected_clip_label.setText("Select a clip to view scene descriptions")
                self._set_description("")
                self._update_indexing_ui(None, None, None)
                self._stop_progress_timer()
                return

            ai_meta, name = self._load_ai_metadata(timeline_clip, file_obj)
            twelvelabs = ai_meta.get("twelvelabs") if isinstance(ai_meta.get("twelvelabs"), dict) else {}

            progress = files_model.get_indexing_progress(file_id) if files_model and file_id else None
            is_active = files_model.is_file_indexing(file_id) if files_model and file_id else False
            phase = progress.get("phase") if progress else None
            percent = progress.get("percent") if progress else None

            if is_active or phase:
                self._start_progress_timer()
            elif str(twelvelabs.get("status") or "").lower() != "indexing":
                self._stop_progress_timer()

            if ai_meta.get("analyzed"):
                self.selected_clip_label.setText(name)
                body = _format_description_text(ai_meta)
                self._set_description(body or "No description available")
                if is_active or phase:
                    self._update_indexing_ui(phase, percent, twelvelabs)
                elif str(twelvelabs.get("status") or "").lower() == "indexing":
                    self._update_indexing_ui("indexing", -1, twelvelabs)
                else:
                    self._update_indexing_ui(None, None, twelvelabs)
                return

            if is_active or phase:
                self.selected_clip_label.setText(name)
                self._set_description("Generating description…")
                self._update_indexing_ui(phase or "summarizing", percent, twelvelabs)
                return

            if str(twelvelabs.get("status") or "").lower() == "indexing":
                self.selected_clip_label.setText(name)
                self._set_description("Indexing for search…")
                self._update_indexing_ui("indexing", -1, twelvelabs)
                return

            err = ai_meta.get("error") or twelvelabs.get("error")
            self.selected_clip_label.setText(name)
            self._set_description(str(err) if err else "Not yet analyzed")
            self._update_indexing_ui(None, None, twelvelabs)

        except Exception as e:
            log.error(f"Failed to update selected clip description: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()

    def on_tag_clicked(self, item, column):
        return

    def update_analysis_status(self):
        self.update_selected_clip_description()
