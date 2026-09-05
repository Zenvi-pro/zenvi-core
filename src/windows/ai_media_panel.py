"""
 @file
 @brief Scene Descriptions panel — Pegasus audiovisual summary + indexing progress
 @author Zenvi Development Team

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 This file is part of OpenShot Video Editor (http://www.openshot.org)
"""

import os
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout,
    QPushButton, QLabel, QProgressBar, QTextEdit,
)

from classes.logger import log
from classes.app import get_app
from classes.indexing_status import FAILED, RUNNING, derive_indexing_status

_PANEL_QSS = """
QWidget#AIMediaPanelContents { background: #0d0d0d; }
QLabel#clipNameLabel { color: #d4d4d4; font-size: 12px; font-weight: bold; }
QLabel#statusLabel { color: #6a9fd8; font-size: 11px; }
QLabel#statusLabel[failed="true"] { color: #ef4444; }
QTextEdit#descriptionView {
    background: #0d0d0d;
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
    color: #d4d4d4;
    padding: 8px;
    font-size: 12px;
}
QProgressBar#indexingProgress {
    background: #252525;
    border: none;
    border-radius: 1px;
}
QProgressBar#indexingProgress::chunk { background: #4d9cf6; border-radius: 1px; }
QPushButton#refreshBtn {
    background: #252525;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 4px;
    color: #d4d4d4;
    padding: 6px;
    font-size: 11px;
}
QPushButton#refreshBtn:hover { background: #2e2e2e; border-color: #4d9cf6; }
"""


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

    def __init__(self, parent=None):
        super().__init__("Scene Descriptions", parent)
        self.setObjectName("AIMediaPanel")
        self._display_file_id = ""

        self.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )

        # Timer must exist before the first refresh — that path stops it.
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(2000)
        self.update_timer.timeout.connect(self.update_selected_clip_description)

        self._build_ui()
        self._wire_selection_signals()
        self.update_selected_clip_description()

        self.setMinimumWidth(300)
        self.setMinimumHeight(400)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("AIMediaPanelContents")
        root.setStyleSheet(_PANEL_QSS)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.setWidget(root)

        self.selected_clip_label = QLabel("Select a clip to view scene descriptions")
        self.selected_clip_label.setObjectName("clipNameLabel")
        self.selected_clip_label.setWordWrap(True)
        layout.addWidget(self.selected_clip_label)

        self.indexing_status_label = QLabel("")
        self.indexing_status_label.setObjectName("statusLabel")
        self.indexing_status_label.setWordWrap(True)
        self.indexing_status_label.hide()
        layout.addWidget(self.indexing_status_label)

        self.indexing_progress = QProgressBar()
        self.indexing_progress.setObjectName("indexingProgress")
        self.indexing_progress.setRange(0, 0)
        self.indexing_progress.setFixedHeight(3)
        self.indexing_progress.setTextVisible(False)
        self.indexing_progress.hide()
        layout.addWidget(self.indexing_progress)

        self.description_view = QTextEdit()
        self.description_view.setObjectName("descriptionView")
        self.description_view.setReadOnly(True)
        self.description_view.setAcceptRichText(False)
        self.description_view.setPlaceholderText("Select a clip to view its description")
        layout.addWidget(self.description_view, stretch=1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setObjectName("refreshBtn")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

    def refresh(self):
        """Refresh the description view (based on current selection)."""
        prefer_files = None
        try:
            window = get_app().window
            if hasattr(window, "filesView") and window.filesView.hasFocus():
                prefer_files = True
        except Exception:
            pass
        self.update_selected_clip_description(prefer_files=prefer_files)

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
        self.update_selected_clip_description()

    def _start_progress_timer(self):
        if not self.update_timer.isActive():
            self.update_timer.start()

    def _stop_progress_timer(self):
        if self.update_timer.isActive():
            self.update_timer.stop()

    def _resolve_display_target(self, prefer_files=None):
        from classes.query import Clip

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

    def _show_status(self, status, percent=None):
        """Render the shared indexing status on the label + thin progress bar."""
        if status.state == RUNNING:
            self.indexing_status_label.setProperty("failed", "false")
            self.indexing_status_label.setText(status.label)
            self.indexing_status_label.show()
            self.indexing_progress.show()
            if percent is not None and percent >= 0:
                self.indexing_progress.setRange(0, 100)
                self.indexing_progress.setValue(min(100, max(0, percent)))
            else:
                self.indexing_progress.setRange(0, 0)
        elif status.state == FAILED:
            self.indexing_status_label.setProperty("failed", "true")
            self.indexing_status_label.setText(status.tooltip or status.label)
            self.indexing_status_label.show()
            self.indexing_progress.hide()
        else:
            self.indexing_status_label.hide()
            self.indexing_progress.hide()

        # Re-polish so the [failed] property selector takes effect
        style = self.indexing_status_label.style()
        style.unpolish(self.indexing_status_label)
        style.polish(self.indexing_status_label)

    def update_selected_clip_description(self, prefer_files=None, *args, **kwargs):
        """Update the selected-clip description when selection or metadata changes."""
        try:
            window = get_app().window
            files_model = getattr(window, "files_model", None)

            timeline_clip, file_obj, file_id = self._resolve_display_target(prefer_files)
            self._display_file_id = file_id or ""

            if not timeline_clip and not file_obj:
                self.selected_clip_label.setText("Select a clip to view scene descriptions")
                self.description_view.setPlainText("")
                self._show_status(derive_indexing_status(None))
                self._stop_progress_timer()
                return

            ai_meta, name = self._load_ai_metadata(timeline_clip, file_obj)

            progress = files_model.get_indexing_progress(file_id) if files_model and file_id else None
            is_active = files_model.is_file_indexing(file_id) if files_model and file_id else False
            status = derive_indexing_status(ai_meta, progress=progress, is_active=is_active)

            if status.state == RUNNING:
                self._start_progress_timer()
            else:
                self._stop_progress_timer()

            self.selected_clip_label.setText(name)
            self._show_status(status, (progress or {}).get("percent"))

            if ai_meta.get("analyzed"):
                self.description_view.setPlainText(
                    _format_description_text(ai_meta) or "No description available"
                )
            elif status.state == RUNNING:
                self.description_view.setPlainText(status.label)
            elif status.tooltip:
                self.description_view.setPlainText(status.tooltip)
            else:
                self.description_view.setPlainText("Not yet analyzed")

        except Exception as e:
            log.error(f"Failed to update selected clip description: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
