"""
 @file
 @brief Optimize Preview submenu helpers for project file context menus.
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import os

from qt_api import QIcon

from classes import info
from classes.app import get_app
from .menu import StyledContextMenu


_OPTIMIZE_PREVIEW_READY_ICON = "tool-optimize-preview.svg"
_OPTIMIZE_PREVIEW_MISSING_ICON = "tool-optimize-preview-missing.svg"


def optimized_preview_icon(state="ready"):
     icon_name = _OPTIMIZE_PREVIEW_MISSING_ICON if state == "missing" else _OPTIMIZE_PREVIEW_READY_ICON
     icon_path = os.path.join(info.PATH, "themes", "cosmic", "images", icon_name)
     if os.path.exists(icon_path):
         return QIcon(icon_path)
     return QIcon()


def _optimizable_files(win):
     files = [f for f in (win.selected_files() or []) if f]
     eligible = []
     for file_obj in files:
         data = getattr(file_obj, "data", {}) or {}
         media_type = str(data.get("media_type", "") or "").strip().lower()
         if media_type == "video":
             eligible.append(file_obj)
     return eligible


def populate_optimized_preview_menu(win, optimized_menu):
     """Populate an Optimize Preview submenu for the current file selection."""
     optimized_menu.clear()
     selected_files = _optimizable_files(win)
     win._optimized_preview_target_file_ids = [str(getattr(f, "id", "") or "") for f in selected_files if getattr(f, "id", None)]
     if not selected_files:
         return None

     _ = get_app()._tr
     service = getattr(win, "proxy_service", None)
     states = [service.get_proxy_state(file_obj) for file_obj in selected_files] if service else []
     has_active = any(state in ("queued", "running", "canceling") for state in states)
     has_proxy = bool(service and any(service.has_proxy_reader(file_obj) for file_obj in selected_files))
     has_missing = "missing" in states
     use_existing_enabled = bool(selected_files) and not has_active
     win.actionOptimizedPreviewCreate.setText(_("Optimize Video"))
     win.actionOptimizedPreviewCreate.setEnabled(not has_active)
     win.actionOptimizedPreviewUseExisting.setEnabled(use_existing_enabled)
     win.actionOptimizedPreviewRemove.setEnabled(has_proxy)
     win.actionOptimizedPreviewCancel.setEnabled(has_active)
     win.actionOptimizedPreviewDeleteAndUnlink.setEnabled(has_proxy and not has_active)

     optimized_menu.setIcon(optimized_preview_icon("missing" if has_missing and not has_active else "ready"))
     if has_active:
         optimized_menu.addAction(win.actionOptimizedPreviewCancel)
         return optimized_menu
     if not has_active:
         optimized_menu.addAction(win.actionOptimizedPreviewCreate)
     optimized_menu.addAction(win.actionOptimizedPreviewUseExisting)
     if has_proxy:
         optimized_menu.addAction(win.actionOptimizedPreviewRemove)
         optimized_menu.addAction(win.actionOptimizedPreviewDeleteAndUnlink)
     return optimized_menu


def add_optimized_preview_menu(win, menu):
     """Add the Optimize Preview submenu for the current file selection."""
     selected_files = _optimizable_files(win)
     if not selected_files:
         return None

     optimized_menu = StyledContextMenu(title=get_app()._tr("Optimize"), parent=menu)
     populate_optimized_preview_menu(win, optimized_menu)
     menu.addMenu(optimized_menu)
     return optimized_menu
