"""
 @file
 @brief AI Media Management panel for tags, collections, and analysis
 @author Zenvi Development Team

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 This file is part of OpenShot Video Editor (http://www.openshot.org)
"""

import os
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QTabWidget,
    QListWidget, QPushButton, QLabel, QFrame,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QLineEdit,
    QSizePolicy, QProgressBar,
)
from PyQt5.QtGui import QFont

from classes.logger import log
from classes.app import get_app
from classes.ai_metadata_utils import (
    clip_metadata_is_valid,
    get_effective_ai_metadata,
    get_scene_descriptions_formatted,
    get_source_window,
)
from classes.timeline_clip_context import resolve_root_ai_metadata

_PHASE_LABELS = {
    "extracting": "Extracting frames…",
    "tagging": "Generating scene descriptions…",
    "uploading": "Uploading for search…",
    "indexing": "Indexing for search…",
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


class AIMediaPanel(QDockWidget):
    """Dock widget for AI media management features"""

    analysisComplete = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Scene Descriptions", parent)
        self.setObjectName("AIMediaPanel")
        self._display_file_id = ""
        self._meta_cache_key = None
        self._meta_cache: tuple = ()

        # Make it closable and movable
        self.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )

        # Main widget
        main = QWidget()
        layout = QVBoxLayout()
        main.setLayout(layout)
        self.setWidget(main)

        # Create tabs
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Debounced refresh — selection/drag can fire many times per second
        self._tags_debounce = QTimer()
        self._tags_debounce.setSingleShot(True)
        self._tags_debounce.setInterval(120)
        self._tags_debounce.timeout.connect(self._flush_selected_clip_tags)
        self._pending_prefer_files = None

        # Update timer – polls while tagging/indexing is active for selected file
        self.update_timer = QTimer()
        self.update_timer.setInterval(2000)
        self.update_timer.timeout.connect(self._on_progress_timer)

        # Create tab pages (Tags only – Analysis/Collections are backend-internal)
        self._create_tags_tab()

        # Track selection changes for clip tag display
        self._wire_selection_signals()
        self.update_selected_clip_tags()

        self.setMinimumWidth(300)
        self.setMinimumHeight(400)

    def _create_tags_tab(self):
        """Create the tags browser tab"""
        tags_widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        tags_widget.setLayout(layout)

        # Search box
        self.tag_search = QLineEdit()
        self.tag_search.setPlaceholderText("Search scene descriptions...")
        self.tag_search.textChanged.connect(self.filter_tags)
        layout.addWidget(self.tag_search)

        # Scene list tree (takes most of the space)
        self.tags_tree = QTreeWidget()
        self.tags_tree.setHeaderLabels(["Clip time", "Description"])
        self.tags_tree.setColumnWidth(0, 52)
        self.tags_tree.setUniformRowHeights(True)
        self.tags_tree.setWordWrap(True)
        self.tags_tree.setRootIsDecorated(False)
        self.tags_tree.setAlternatingRowColors(False)
        layout.addWidget(self.tags_tree, stretch=3)

        # Subtle separator between the scene list and the selected-clip section
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Plain)
        sep.setStyleSheet("background: rgba(255,255,255,0.07); max-height: 1px; border: none; margin: 4px 0;")
        layout.addWidget(sep)

        # Selected clip section
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

        self.selected_tags_list = QListWidget()
        self.selected_tags_list.setMaximumHeight(110)
        layout.addWidget(self.selected_tags_list, stretch=1)

        # Refresh button – minimal, full-width
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        refresh_btn.setObjectName("tagsRefreshBtn")
        refresh_btn.clicked.connect(self.refresh_tags)
        layout.addWidget(refresh_btn)

        # Keep a reference for backward-compatible code that accesses .selected_clip_group
        self.selected_clip_group = None

        # No tab bar needed when there is only one tab – hide it
        self.tabs.tabBar().setVisible(False)
        self.tabs.addTab(tags_widget, "Tags")

        # Load initial tags
        self.refresh_tags()

    def refresh_tags(self):
        """Refresh the scenes tree (based on current selection)."""
        prefer_files = None
        try:
            window = get_app().window
            if hasattr(window, "filesView") and window.filesView.hasFocus():
                prefer_files = True
        except Exception:
            pass
        self.update_selected_clip_tags(prefer_files=prefer_files)

    def filter_tags(self, text):
        """Filter scene descriptions by search text."""
        iterator = QTreeWidgetItemIterator(self.tags_tree)
        while iterator.value():
            item = iterator.value()
            desc_text = (item.text(1) or "").lower()
            time_text = (item.text(0) or "").lower()
            haystack = f"{time_text} {desc_text}".strip()
            item.setHidden(text.lower() not in haystack if text else False)
            iterator += 1

    def _wire_selection_signals(self):
        """Listen for file selection changes to show per-clip tags."""
        try:
            window = get_app().window
            files_model = getattr(window, "files_model", None)
            if files_model and files_model.selection_model:
                files_model.selection_model.selectionChanged.connect(
                    lambda *_a: self.update_selected_clip_tags(prefer_files=True)
                )
                files_model.taggingProgress.connect(self._on_tagging_progress)
            window.FileUpdated.connect(self._on_file_updated)
            window.SelectionChanged.connect(
                lambda *_a: self._schedule_selected_clip_tags(prefer_files=False)
            )
        except Exception as e:
            log.warning(f"Failed to connect selection signals for tags: {e}")

    def _schedule_selected_clip_tags(self, prefer_files=None):
        self._pending_prefer_files = prefer_files
        self._tags_debounce.start()

    def _flush_selected_clip_tags(self):
        self.update_selected_clip_tags(prefer_files=self._pending_prefer_files)

    def _on_file_updated(self, file_id):
        if str(file_id) == str(self._display_file_id):
            self._meta_cache_key = None
            self._meta_cache = ()
        self.update_selected_clip_tags()

    def _on_tagging_progress(self, file_id, phase, percent):
        if str(file_id) == str(self._display_file_id):
            self._update_indexing_ui(phase, percent, None)
            if phase != "done":
                self._start_progress_timer()
            else:
                self._stop_progress_timer()
        self.update_selected_clip_tags()

    def _on_progress_timer(self):
        self.update_selected_clip_tags()

    def _start_progress_timer(self):
        if not self.update_timer.isActive():
            self.update_timer.start()

    def _stop_progress_timer(self):
        if hasattr(self, "update_timer") and self.update_timer.isActive():
            self.update_timer.stop()

    def _resolve_display_target(self, prefer_files=None):
        """Return (timeline_clip, file_obj, file_id) for the dock display context."""
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
        """Resolve trim-aware ai_metadata for the current display target."""
        ai_meta = {}
        name = ""

        if timeline_clip and isinstance(getattr(timeline_clip, "data", None), dict):
            clip_data = timeline_clip.data
            name = clip_data.get("title") or clip_data.get("name") or "Timeline Clip"
            clip_ai = clip_data.get("ai_metadata") if isinstance(clip_data.get("ai_metadata"), dict) else None
            source_start, source_end = get_source_window(clip_data, None)

            # Fast path: sliced clips keep valid local metadata (track/position changes don't matter)
            if clip_metadata_is_valid(clip_ai, source_start, source_end):
                return clip_ai, name

            fid = str(clip_data.get("file_id") or "")
            file_data = None
            if file_obj and str(getattr(file_obj, "id", "")) == fid:
                file_data = file_obj.data if isinstance(file_obj.data, dict) else None
            if file_data is None and fid:
                try:
                    from classes.query import File
                    source_file = File.get(id=fid)
                    if source_file and isinstance(source_file.data, dict):
                        file_data = source_file.data
                        name = name or source_file.data.get("name") or os.path.basename(
                            source_file.data.get("path", "Clip")
                        )
                except Exception:
                    pass

            source_start, source_end = get_source_window(clip_data, file_data)
            root_ai, parent_data = resolve_root_ai_metadata(file_data, file_id=fid)
            if root_ai:
                ai_meta = get_effective_ai_metadata(
                    parent_data or file_data,
                    clip_data=clip_data,
                    clip_ai_metadata=clip_ai,
                    root_ai_metadata=root_ai,
                    rebased=True,
                )
            elif clip_ai:
                ai_meta = clip_ai

        elif file_obj:
            name = file_obj.data.get("name") or os.path.basename(file_obj.data.get("path", "Clip"))
            candidate = file_obj.get_ai_metadata()
            ai_meta = candidate if isinstance(candidate, dict) else {}

        return ai_meta, name

    def _metadata_cache_key(self, timeline_clip, file_obj, file_id):
        """Key by trim window only — layer/position drags must not force recomputation."""
        if timeline_clip and isinstance(getattr(timeline_clip, "data", None), dict):
            d = timeline_clip.data
            ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
            sw = ai.get("source_window") if isinstance(ai.get("source_window"), dict) else {}
            scenes = ai.get("scene_descriptions") or []
            n_scenes = len(scenes) if isinstance(scenes, list) else 0
            return (
                f"{file_id}|{timeline_clip.id}|{d.get('start')}|{d.get('end')}|"
                f"{sw.get('start')}|{sw.get('end')}|{n_scenes}|{bool(ai.get('analyzed'))}"
            )
        if file_obj and isinstance(file_obj.data, dict):
            ai = file_obj.data.get("ai_metadata") if isinstance(file_obj.data.get("ai_metadata"), dict) else {}
            return f"{file_id}|file|{bool(ai.get('analyzed'))}|{len(ai.get('scene_descriptions') or [])}"
        return str(file_id or "")

    def _update_indexing_ui(self, phase, percent, twelvelabs):
        """Show or hide indexing progress bar and status label."""
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

    def _populate_scene_views(self, ai_meta):
        """Fill tree and list widgets from ai_metadata."""
        scenes = ai_meta.get("scene_descriptions", [])
        if not isinstance(scenes, list) or not scenes:
            self.selected_tags_list.addItem("No scene descriptions found")
            return

        formatted = get_scene_descriptions_formatted(ai_meta, use_source_time=False)
        for line in formatted:
            self.selected_tags_list.addItem(line)

        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            time_sec = float(scene.get("time", 0) or 0)
            desc = scene.get("description", "")
            minutes = int(time_sec // 60)
            seconds = int(time_sec % 60)
            time_str = f"{minutes}:{seconds:02d}"
            row = QTreeWidgetItem(self.tags_tree)
            row.setText(0, time_str)
            row.setText(1, str(desc))
            src = scene.get("source_time")
            if src is not None:
                row.setToolTip(0, f"Source file: {int(float(src) // 60)}:{int(float(src) % 60):02d}")

    def update_selected_clip_tags(self, prefer_files=None, *args, **kwargs):
        """Update the selected-clip scene list when selection or metadata changes."""
        try:
            window = get_app().window
            files_model = getattr(window, "files_model", None)

            timeline_clip, file_obj, file_id = self._resolve_display_target(prefer_files)

            if not timeline_clip and not file_obj:
                self.selected_tags_list.clear()
                self.tags_tree.clear()
                self.selected_clip_label.setText("Select a clip to view scene descriptions")
                self._update_indexing_ui(None, None, None)
                self._stop_progress_timer()
                self._meta_cache_key = None
                self._meta_cache = ()
                self._display_file_id = ""
                return

            progress = files_model.get_tagging_progress(file_id) if files_model and file_id else None
            is_active = files_model.is_file_tagging(file_id) if files_model and file_id else False
            phase = progress.get("phase") if progress else None
            percent = progress.get("percent") if progress else None
            cache_key = self._metadata_cache_key(timeline_clip, file_obj, file_id)
            indexing_busy = bool(is_active or phase)

            if (
                not indexing_busy
                and cache_key == self._meta_cache_key
                and self._meta_cache
            ):
                return

            ai_meta, name = self._load_ai_metadata(timeline_clip, file_obj)
            twelvelabs = ai_meta.get("twelvelabs") if isinstance(ai_meta.get("twelvelabs"), dict) else {}
            self._meta_cache_key = cache_key
            self._meta_cache = (ai_meta, name, twelvelabs, is_active, phase, percent)
            self._display_file_id = file_id or ""

            self.selected_tags_list.clear()
            self.tags_tree.clear()

            if is_active or phase:
                self._start_progress_timer()
            elif str(twelvelabs.get("status") or "").lower() != "indexing":
                self._stop_progress_timer()

            if ai_meta.get("analyzed"):
                self.selected_clip_label.setText(name)
                self._populate_scene_views(ai_meta)
                if is_active or phase:
                    self._update_indexing_ui(phase, percent, twelvelabs)
                elif str(twelvelabs.get("status") or "").lower() == "indexing":
                    self._update_indexing_ui("indexing", -1, twelvelabs)
                else:
                    self._update_indexing_ui(None, None, twelvelabs)
                return

            if is_active or phase:
                self.selected_clip_label.setText(name)
                self.selected_tags_list.addItem("Preparing scene descriptions…")
                self._update_indexing_ui(phase or "extracting", percent, twelvelabs)
                return

            if str(twelvelabs.get("status") or "").lower() == "indexing":
                self.selected_clip_label.setText(name)
                self.selected_tags_list.addItem("Preparing scene descriptions…")
                self._update_indexing_ui("indexing", -1, twelvelabs)
                return

            self.selected_clip_label.setText(name)
            self.selected_tags_list.addItem("Not yet analyzed")
            self._update_indexing_ui(None, None, twelvelabs)

        except Exception as e:
            log.error(f"Failed to update selected clip tags: {e}")

    def showEvent(self, event):
        """Raise to the front of the tab stack when made visible via View > Docks."""
        super().showEvent(event)
        self.raise_()

    def on_tag_clicked(self, item, column):
        """Deprecated: tag click handler kept for backward compatibility."""
        return

    def update_analysis_status(self):
        """Refresh display while background tagging/indexing runs."""
        self.update_selected_clip_tags()
