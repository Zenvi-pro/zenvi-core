"""
 @file
 @brief This file contains the project file model, used by the project tree
 @author Noah Figg <eggmunkee@hotmail.com>
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
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
import json
import glob
import functools
import uuid

from qt_api import (
    QMimeData, Qt, QUrl, pyqtSignal, pyqtSlot, QEventLoop, QObject, QThread, QTimer,
    QSortFilterProxyModel, QItemSelectionModel, QItemSelection, QPersistentModelIndex, QModelIndex
)
from qt_api import (
    QIcon, QPixmap, QStandardItem, QStandardItemModel
)
from qt_api import QAbstractItemView
from classes import updates
from classes import info
from classes.image_types import get_media_type, is_audio_only_media
from classes.query import File
from classes.logger import log
from classes.app import get_app
from classes.file_drop import local_path_from_url
from classes.api_client import get_backend_client

import openshot


def inspect_media(path, max_width=0, max_height=0):
    """Inspect a media file with libopenshot and return (reader_json, duration).

    libopenshot 1.0 exposes Clip.CreateReader(path, inspect_reader). Its cheap
    first pass can pick the wrong reader (QtImageReader for a .flac), so retry
    with inspect_reader=True and let libopenshot fall through to the next
    candidate (OpenShot #5997). Older builds (Zenvi ships 0.5.x today) have no
    CreateReader: use Clip(path) and, if that fails, an explicit FFmpegReader.
    """
    def _inspect(reader):
        if not reader:
            raise RuntimeError(f"No reader available for path: {path}")
        if max_width > 0 and max_height > 0 and hasattr(reader, "SetMaxDecodeSize"):
            reader.SetMaxDecodeSize(int(max_width), int(max_height))
        reader.Open()
        try:
            return json.loads(reader.Json()), float(reader.info.duration or 0.0)
        finally:
            reader.Close()

    create_reader = getattr(openshot.Clip, "CreateReader", None)
    if callable(create_reader):
        try:
            return _inspect(create_reader(path, False))
        except Exception:
            # Eager inspection rejects a wrong lightweight reader choice during
            # construction and falls back to the next candidate (e.g. FFmpeg).
            return _inspect(create_reader(path, True))

    try:
        clip = openshot.Clip(path)
        return _inspect(clip.Reader())
    except Exception:
        return _inspect(openshot.FFmpegReader(path))


class BackendIndexingWorker(QThread):
    """Background worker: Gemini index + Flash audiovisual summary."""
    completed = pyqtSignal(dict, object, object)  # file_data, metadata, error
    progress = pyqtSignal(str, str, int)  # file_id, phase, percent (-1 = indeterminate)
    intermediate_save = pyqtSignal(str, object)  # file_id, metadata dict

    def __init__(self, file_data, project_id="", summarize_only=False, parent=None):
        super().__init__(parent)
        self.file_data = file_data
        self.project_id = project_id or ""
        self.summarize_only = bool(summarize_only)
        self._job = None


    def run(self):
        # The logic lives in classes.media_index.job (no Qt, so it is tested headlessly);
        # this thread only supplies the backend client and the three signals.
        from classes.media_index.job import IndexingJob

        self._job = IndexingJob(
            self.file_data, self.project_id, self.summarize_only,
            client_factory=get_backend_client,  # looked up now, so a test can patch the module name
            emit_completed=self.completed.emit,
            emit_progress=self.progress.emit,
            emit_intermediate=self.intermediate_save.emit,
        )
        self._job.run()

    def interrupt(self):
        """Stop local analysis and close the active HTTP session to unblock any pending request."""
        job = getattr(self, "_job", None)
        if job is not None:
            job.cancel()
        try:
            client = get_backend_client()
            if client._session is not None:
                client._session.close()
                client._session = None
        except Exception:
            pass


class SingleColumnProxyModel(QSortFilterProxyModel):
    """Proxy that exposes only the first column for ListView accessibility"""

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.DisplayRole):
        """Get text data from the underlying source model (bypassing filter proxy)"""
        if index.column() == 0 and role in (Qt.DisplayRole, Qt.AccessibleTextRole):
            # Get the actual text from the root source model (QStandardItemModel)
            # by traversing through the proxy chain
            source_index = self.mapToSource(index)
            filter_proxy = self.sourceModel()
            if filter_proxy:
                root_index = filter_proxy.mapToSource(source_index)
                root_model = filter_proxy.sourceModel()
                if root_model:
                    return root_model.data(root_index, Qt.DisplayRole)
        return super().data(index, role)


class FileFilterProxyModel(QSortFilterProxyModel):
    """Proxy class used for sorting and filtering model data"""

    def data(self, index, role=Qt.DisplayRole):
        """Hide text in column 0 for TreeView - name is shown in column 1"""
        if index.column() == 0 and role in (Qt.DisplayRole, Qt.AccessibleTextRole):
            return ""
        return super().data(index, role)

    def filterAcceptsRow(self, sourceRow, sourceParent):
        """Filter for text"""
        from qt_api import isdeleted, get_proxy_filter_regex, regex_is_empty, regex_matches
        files_filter = get_app().window.filesFilter
        filter_text = "" if isdeleted(files_filter) else files_filter.text()
        if get_app().window.actionFilesShowVideo.isChecked() \
                or get_app().window.actionFilesShowAudio.isChecked() \
                or get_app().window.actionFilesShowImage.isChecked() \
                or filter_text:
            # Fetch the file name
            index = self.sourceModel().index(sourceRow, 0, sourceParent)
            file_name = self.sourceModel().data(index)  # file name (i.e. MyVideo.mp4)

            # Fetch the media_type
            index = self.sourceModel().index(sourceRow, 3, sourceParent)
            media_type = self.sourceModel().data(index)  # media type (i.e. video, image, audio)

            index = self.sourceModel().index(sourceRow, 2, sourceParent)
            tags = self.sourceModel().data(index)  # tags (i.e. intro, custom, etc...)

            if any([
                get_app().window.actionFilesShowVideo.isChecked() and media_type != "video",
                get_app().window.actionFilesShowAudio.isChecked() and media_type != "audio",
                get_app().window.actionFilesShowImage.isChecked() and media_type != "image",
            ]):
                return False

            # Match against regex pattern
            regex = get_proxy_filter_regex(self)
            if not regex_is_empty(regex):
                tag_text = tags or ""
                return regex_matches(regex, file_name) or regex_matches(regex, tag_text)
            return True

        # Continue running built-in parent filter logic
        return super().filterAcceptsRow(sourceRow, sourceParent)

    def mimeData(self, indexes):
        # Create MimeData for drag operation
        data = QMimeData()

        # Get list of selected file ids from indexes (more reliable across bindings)
        ids = []
        seen_rows = set()
        for idx in indexes:
            row = idx.row()
            if row in seen_rows:
                continue
            seen_rows.add(row)
            id_index = idx.sibling(row, 5)
            file_id = id_index.data()
            if file_id:
                ids.append(file_id)
        if not ids:
            ids = self.model_owner.selected_file_ids()
        data.setText(json.dumps(ids))
        data.setHtml("clip")
        urls = []
        for file_id in ids:
            try:
                file = File.get(id=file_id)
            except Exception:
                file = None
            if not file:
                continue
            try:
                path = file.absolute_path()
            except Exception:
                path = file.data.get("path")
            if path:
                urls.append(QUrl.fromLocalFile(path))
        if urls:
            data.setUrls(urls)

        # Return Mimedata
        return data

    def get_file_index(self, file_id):
        # Find the index in the proxy model based on the file ID
        if file_id in self.model_owner.model_ids:
            return self.mapFromSource(QModelIndex(self.model_owner.model_ids[file_id]))
        return QModelIndex()

    def __init__(self, **kwargs):
        if "parent" in kwargs:
            self.model_owner = kwargs["parent"]
            kwargs.pop("parent")

        # Call base class implementation
        super().__init__(**kwargs)


class FilesModel(QObject, updates.UpdateInterface):
    ModelRefreshed = pyqtSignal()
    indexingProgress = pyqtSignal(str, str, int)  # file_id, phase, percent
    sessionSaved = pyqtSignal()  # a Zenvi session was saved (any thread): resume "sign in to index" files
    PLACEHOLDER_PREFIX = "__genjob__:"
    PROJECT_FILE_THUMB_ATTEMPTS = 3
    _pending_icon = None

    @staticmethod
    def _thumbnail_frame_for_file(file):
        """Frame a file's Project Files thumbnail shows (its trimmed start)."""
        thumbnail_frame = 1
        if 'start' in file.data:
            fps = file.data["fps"]
            fps_float = float(fps["num"]) / float(fps["den"])
            thumbnail_frame = round(float(file.data['start']) * fps_float) + 1
        return thumbnail_frame

    @classmethod
    def _pending_thumbnail_icon(cls):
        """Placeholder shown until a file's thumbnail arrives from the worker."""
        if cls._pending_icon is None:
            cls._pending_icon = QIcon(os.path.join(info.IMAGES_PATH, "ThumbnailPending.svg"))
        return cls._pending_icon

    def _project_file_icon_for_file(self, file):
        """Icon, display name and media type for a new Project Files row.

        Video and image thumbnails are made on the thumbnail worker -- one
        frame of long-GOP media can take a minute to decode -- so the row shows
        a placeholder until _on_thumbnail_ready swaps the real one in.
        """
        path, filename = os.path.split(file.data["path"])
        name = file.data.get("name", filename)
        media_type = file.data.get("media_type")

        if media_type in ["video", "image"]:
            self._request_file_thumbnail(file)
            return self._pending_thumbnail_icon(), name, media_type
        return QIcon(os.path.join(info.PATH, "images", "AudioThumbnail.svg")), name, media_type

    def _request_file_thumbnail(self, file, clear_cache=False):
        """Queue a file's Project Files thumbnail on the thumbnail worker."""
        self.request_thumbnail(file.id, file.id, self._thumbnail_frame_for_file(file),
                               clear_cache=clear_cache)

    def request_thumbnail(self, slot, file_id, frame, on_ready=None, clear_cache=False):
        """Load (or make) one thumbnail on the thumbnail worker, never the GUI thread.

        Call it on the GUI thread. *slot* keys the request: repeat requests
        for the same slot and frame share one job. Project Files rows use the
        file id; other views pass their own key. on_ready(image) runs on the
        GUI thread with a QImage (null when no thumbnail could be made).
        """
        slot, frame = str(slot or ""), int(frame or 0)
        if on_ready is not None:
            self._thumbnail_callbacks.setdefault((slot, frame), []).append(on_ready)
        self.thumbnails.request_thumbnail(slot, file_id, frame, self._thumbnail_generation,
                                          clear_cache=clear_cache)

    @pyqtSlot(str, int, object, int)
    def _on_thumbnail_ready(self, slot, frame, image, generation):
        """A thumbnail finished on the worker (GUI thread): update its row and callers."""
        if generation != self._thumbnail_generation:
            return  # requested for a project that is no longer loaded
        for on_ready in self._thumbnail_callbacks.pop((slot, frame), []):
            try:
                on_ready(image)
            except Exception:
                log.warning("Thumbnail callback failed for %s frame %s", slot, frame, exc_info=1)

        id_index = self.model_ids.get(slot)
        if id_index is None or not id_index.isValid():
            return
        file = File.get(id=slot)
        if not file or frame != self._thumbnail_frame_for_file(file):
            return  # the file's start moved; the thumbnail for the new frame is queued
        if image is None or image.isNull():
            log.warning("No thumbnail for file %s frame %s; keeping the placeholder", slot, frame)
            return
        item = self.model.itemFromIndex(id_index.sibling(id_index.row(), 0))
        if item is None:
            return
        # The icon change is not a name/tags edit: the views save the file and
        # emit FileUpdated on itemChanged unless ignore_updates is set, which
        # would add an undo step and regenerate this thumbnail again.
        was_ignoring = self.ignore_updates
        self.ignore_updates = True
        try:
            # From the worker's fresh read of the file: QIcon(path) caches by
            # file name and would keep showing a regenerated thumbnail's old image.
            item.setIcon(QIcon(QPixmap.fromImage(image)))
        finally:
            self.ignore_updates = was_ignoring

    def thumbnail_icon(self, file_id):
        """The icon Project Files shows for a file (its placeholder until the
        thumbnail is ready), for views that must not wait on the thumbnail
        server. None when the file has no row."""
        id_index = self.model_ids.get(str(file_id or ""))
        if id_index is None or not id_index.isValid():
            return None
        item = self.model.itemFromIndex(id_index.sibling(id_index.row(), 0))
        return item.icon() if item is not None else None

    def _stop_thumbnail_worker(self):
        """Called at window close / app quit: stop the thumbnail worker thread."""
        self._thumbnail_callbacks.clear()
        if self.thumbnails is not None:
            self.thumbnails.shutdown()

    def _proxy_service(self):
        """The window's Optimize Preview service (None before the window creates it)."""
        if self.proxy_service is not None:
            return self.proxy_service
        window = getattr(get_app(), "window", None)
        return getattr(window, "proxy_service", None) if window else None

    def _tooltip_for_file(self, file, name):
        """Tooltip for the thumbnail cell; marks files that have an optimized preview."""
        tooltip = str(name or "")
        proxy_service = self._proxy_service()
        if not proxy_service or not file:
            return tooltip
        if proxy_service.get_proxy_state(file) in ("ready", "missing"):
            return "{} {}".format(tooltip, get_app()._tr("(Optimized)"))
        return tooltip

    def _on_proxy_file_changed(self, file_id):
        """Repaint one row when its Optimize Preview job/state changes."""
        file_id = str(file_id or "")
        id_index = self.model_ids.get(file_id)
        if id_index is None or not id_index.isValid():
            return
        row = id_index.row()
        file_obj = File.get(id=file_id)
        if file_obj:
            path, filename = os.path.split(file_obj.data["path"])
            previous_ignore = self.ignore_updates
            self.ignore_updates = True     # a repaint, not a user edit of the row
            try:
                self.model.item(row, 0).setToolTip(self._tooltip_for_file(file_obj, filename))
            finally:
                self.ignore_updates = previous_ignore
        left = self.model.index(row, 0)
        right = self.model.index(row, self.model.columnCount() - 1)
        self.model.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.ToolTipRole])

    # This method is invoked by the UpdateManager each time a change happens (i.e UpdateInterface)
    def changed(self, action):

        # Something was changed in the 'files' list
        if action and ((len(action.key) >= 1 and action.key[0].lower() == "files") or action.type == "load"):
            # Refresh project files model
            if action.type == "insert":
                # Don't clear the existing items if only inserting new things
                self.update_model(clear=False)
            elif action.type == "delete" and action.key[0].lower() == "files" and len(action.key) == 2:
                # Delete a top-level file row only when the file object itself was deleted.
                self.invalidate_indexing_status(action.key[1].get('id', ''))
                self.update_model(clear=False, delete_file_id=action.key[1].get('id', ''))
            elif action.type in ("update", "delete") and action.key[0].lower() == "files":
                # Update a single file (if found)
                self.invalidate_indexing_status(action.key[1].get('id', ''))
                self.update_model(clear=False, update_file_id=action.key[1].get('id', ''))
            else:
                # Clear existing items. For full project loads, batch updates for faster UI rebuild.
                self.update_model(clear=True, progressive_ui=False)

    def update_model(self, clear=True, delete_file_id=None, update_file_id=None, progressive_ui=True):
        # Programmatic cell changes must never be read back as user edits. A save made
        # while a refresh is running re-enters this method; restoring the caller's flag
        # (rather than clearing it) keeps the outer refresh protected, and an early
        # return can no longer leave the flag stuck on.
        previous_ignore = self.ignore_updates
        try:
            return self._update_model(clear=clear, delete_file_id=delete_file_id,
                                      update_file_id=update_file_id, progressive_ui=progressive_ui)
        finally:
            self.ignore_updates = previous_ignore

    def _update_model(self, clear=True, delete_file_id=None, update_file_id=None, progressive_ui=True):
        log.debug("updating files model.")
        app = get_app()

        # update_model restores the caller's flag on every return path.
        previous_ignore = self.ignore_updates
        self.ignore_updates = True

        # Translations
        _ = app._tr

        # Delete a file (if delete_file_id passed in)
        if delete_file_id in self.model_ids:
            # Use the persistent index we stored to find the row
            id_index = self.model_ids[delete_file_id]

            # sanity check
            if not id_index.isValid() or delete_file_id != id_index.data():
                log.warning("Couldn't remove {} from model!".format(delete_file_id))
                return
            # Delete row from model
            row_num = id_index.row()
            self.model.removeRows(row_num, 1, id_index.parent())
            self.model.submit()
            self.model_ids.pop(delete_file_id)

        # Update a file (if update_file_id passed in)
        if update_file_id in self.model_ids:
            # Use the persistent index we stored to find the row
            id_index = self.model_ids[update_file_id]

            # sanity check
            if not id_index.isValid() or update_file_id != id_index.data():
                log.warning("Couldn't update {} in model!".format(update_file_id))
                return

            # lookup File object
            f = File.get(id=update_file_id)
            if f:
                # Update "tags" in model (if different)
                row_num = id_index.row()
                if f.data.get("tags") != self.model.item(row_num, 2).text():
                    self.model.item(row_num, 2).setText(f.data.get("tags"))
                path, filename = os.path.split(f.data["path"])
                # Keep the shown name current: the rename handler reads it back.
                name = f.data.get("name", filename)
                for col in (0, 1):
                    item = self.model.item(row_num, col)
                    if item.text() != name:
                        item.setText(name)
                        item.setAccessibleText(name)
                self.model.item(row_num, 0).setToolTip(self._tooltip_for_file(f, filename))

        # Clear all items
        if clear:
            self.model_ids = {}
            self.model.clear()
            self._status_cache.clear()
            # Thumbnails still queued for the old rows are no longer wanted
            self._thumbnail_generation += 1
            self._thumbnail_callbacks.clear()

        # Add Headers (all 6 columns - last 3 are hidden but must exist for proper layout)
        self.model.setHorizontalHeaderLabels([
            _("Thumb"), _("Name"), _("Tags"),
            "media_type", "path", "id"
        ])

        # Get list of files in project
        files = File.filter()  # get all files

        # add item for each file
        row_added_count = 0
        for file in files:
            # Skip agent-created subclips — nothing creates them any more, but
            # projects saved before placement was consolidated still carry them.
            if file.data.get("zenvi_subclip"):
                continue

            id = file.data["id"]
            if id in self.model_ids and self.model_ids[id].isValid():
                # Ignore files that already exist in model
                continue

            path, filename = os.path.split(file.data["path"])
            tags = file.data.get("tags", "")
            thumb_icon, name, media_type = self._project_file_icon_for_file(file)

            row = []
            flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled | Qt. ItemNeverHasChildren

            # Append thumbnail
            col = QStandardItem(thumb_icon, name)
            col.setToolTip(self._tooltip_for_file(file, filename))
            col.setFlags(flags)
            col.setAccessibleText(name)
            row.append(col)

            # Append Filename
            col = QStandardItem(name)
            col.setFlags(flags | Qt.ItemIsEditable)
            col.setAccessibleText(name)
            row.append(col)

            # Append Tags
            col = QStandardItem(tags)
            col.setFlags(flags | Qt.ItemIsEditable)
            row.append(col)

            # Append Media Type
            col = QStandardItem(media_type)
            col.setFlags(flags)
            row.append(col)

            # Append Path
            col = QStandardItem(path)
            col.setFlags(flags)
            row.append(col)

            # Append ID
            col = QStandardItem(id)
            col.setFlags(flags | Qt.ItemIsUserCheckable)
            row.append(col)

            # Append ROW to MODEL (if does not already exist in model)
            if id not in self.model_ids:
                self.model.appendRow(row)
                # Link the file ID hash to that column of the table row by persistent index
                self.model_ids[id] = QPersistentModelIndex(row[5].index())

                row_added_count += 1
                if progressive_ui and row_added_count % 25 == 0:
                    # Update every X items
                    get_app().processEvents(QEventLoop.ExcludeUserInputEvents)

            # Refresh view/filtering incrementally during interactive updates (i.e. imports)
            if progressive_ui:
                get_app().window.resize_contents()

        self.ignore_updates = previous_ignore

        # Single refresh after bulk updates (i.e. opening a project)
        if not progressive_ui:
            get_app().window.resize_contents()

        # Emit signal when model is updated
        self.ModelRefreshed.emit()
        self._rebuild_generation_placeholders()

    _MAX_INDEXING_WORKERS = 2

    def _stop_active_indexers(self):
        """Called at app quit — interrupt any pending HTTP requests, then wait briefly."""
        self._indexing_queue.clear()
        for worker in list(self._active_indexers):
            try:
                worker.interrupt()  # close session to unblock requests.post()
                worker.quit()
                worker.wait(3000)
            except Exception:
                pass
        self._active_indexers.clear()

    def _apply_ai_metadata(self, file_obj, ai_metadata):
        """Attach AI metadata to a file object (does not save)."""
        if not ai_metadata or not isinstance(ai_metadata, dict):
            return
        from classes.ai_metadata_utils import is_ai_metadata_usable

        # Don't let a failed / empty tagging result wipe out previously-good
        # analysis. Keep the usable content; only record the new error and any
        # fresh indexing status.
        prev = file_obj.data.get("ai_metadata")
        if (not is_ai_metadata_usable(ai_metadata)
                and isinstance(prev, dict) and is_ai_metadata_usable(prev)):
            merged = dict(prev)
            if ai_metadata.get("error"):
                merged["error"] = ai_metadata["error"]
            if ai_metadata.get("index"):
                merged["index"] = ai_metadata["index"]
            if ai_metadata.get("twelvelabs"):
                merged["twelvelabs"] = ai_metadata["twelvelabs"]
            elif ai_metadata.get("index"):
                merged["twelvelabs"] = ai_metadata["index"]
            file_obj.data["ai_metadata"] = merged
            self._status_cache.pop(str(file_obj.data.get("id", "")), None)
            return

        file_obj.data["ai_metadata"] = ai_metadata
        # Cache bulky transcript/scene payload by fingerprint; keep index handles in project JSON.
        try:
            fp = file_obj.data.get("fingerprint")
            if fp:
                from classes.media_cache import save_ai_metadata
                save_ai_metadata(fp, ai_metadata)
        except Exception:
            log.debug("Could not cache ai_metadata", exc_info=1)
        self._status_cache.pop(str(file_obj.data.get("id", "")), None)
        # Do not auto-fill legacy file.data["tags"] from AI analysis.

    def _save_file_untracked(self, file_obj):
        """Write an indexing result into the project without an undo step.

        Indexing finishes whenever it finishes. A normal save would put an entry in the
        undo history, so a user's next Ctrl+Z would silently undo an index they never asked
        for (and a half-finished "indexing..." marker with it). One user intent is one undo
        step; background analysis is not a user intent.
        """
        get_app().updates.update_untracked(file_obj.key, file_obj.data)

    def _set_indexing_progress(self, file_id, phase, percent):
        self._indexing_progress[str(file_id)] = {"phase": phase, "percent": percent}
        self._status_cache.pop(str(file_id), None)
        self.indexingProgress.emit(str(file_id), phase, percent)

    def file_indexing_status(self, file_id):
        """Badge status for a file id — cached, so views can call it from paint()."""
        from classes.indexing_status import derive_indexing_status

        fid = str(file_id or "")
        if not fid:
            return derive_indexing_status(None)
        cached = self._status_cache.get(fid)
        if cached is not None:
            return cached
        try:
            f = File.get(id=fid)
            ai_meta = f.data.get("ai_metadata") if f else None
        except Exception:
            ai_meta = None
        status = derive_indexing_status(
            ai_meta,
            progress=self._indexing_progress.get(fid),
            is_active=self.is_file_indexing(fid),
            is_queued=self.is_file_queued(fid),
        )
        self._status_cache[fid] = status
        return status

    def invalidate_indexing_status(self, file_id=None):
        """Drop cached status for a file (or all files) after metadata changes."""
        if file_id is None:
            self._status_cache.clear()
        else:
            self._status_cache.pop(str(file_id), None)

    def is_file_indexing(self, file_id):
        fid = str(file_id or "")
        return any(
            str(w.file_data.get("id", "")) == fid for w in self._active_indexers
        )

    def is_file_queued(self, file_id):
        """True while a file waits for one of the bounded indexing worker slots."""
        fid = str(file_id or "")
        return any(qid == fid for qid, _ in self._indexing_queue)

    def has_active_indexing(self):
        """True while any file in the project is indexing or waiting to index."""
        return bool(self._active_indexers or self._indexing_queue)

    def get_indexing_progress(self, file_id):
        return self._indexing_progress.get(str(file_id or ""))

    def _enqueue_index(self, file_id, summarize_only=False):
        """Queue a file for background indexing/summarize with bounded concurrency."""
        fid = str(file_id or "")
        if not fid:
            return
        if self.is_file_indexing(fid):
            return
        if any(qid == fid for qid, _ in self._indexing_queue):
            return
        self._indexing_queue.append((fid, bool(summarize_only)))
        self._status_cache.pop(fid, None)
        self._drain_indexing_queue()

    def _timeline_file_ids(self):
        """Files a timeline clip uses: they are indexed before files nobody has placed yet."""
        try:
            from classes.media_index.queue import timeline_file_ids
            return timeline_file_ids(get_app().project.get("clips"))
        except Exception:
            return set()

    def requeue_signin_skipped(self):
        """Index again the files whose cloud indexing waited for a signed-in user."""
        try:
            from classes.media_index.queue import signin_skipped_ids
            ids = signin_skipped_ids(get_app().project.get("files"))
        except Exception:
            return
        for fid in ids:
            self._enqueue_index(fid)

    def _drain_indexing_queue(self):
        from classes.media_index.queue import next_index
        while len(self._active_indexers) < self._MAX_INDEXING_WORKERS and self._indexing_queue:
            file_id, summarize_only = self._indexing_queue.pop(
                next_index(self._indexing_queue, self._timeline_file_ids()))
            if self.is_file_indexing(file_id):
                continue
            self._start_indexing_worker(file_id, summarize_only=summarize_only)

    def _index_file_async(self, file_id, summarize_only=False):
        """Fire-and-forget background indexing/summarize for an already-saved file."""
        self._enqueue_index(file_id, summarize_only=summarize_only)

    def _start_indexing_worker(self, file_id, summarize_only=False):
        from classes.query import File as _File
        file_obj = _File.get(id=file_id)
        if not file_obj or not isinstance(file_obj.data, dict):
            return
        if file_obj.data.get("media_type") not in ("video", "image", "audio"):
            return
        if self.is_file_indexing(file_id):
            return

        # Pass project ID so the worker can build a per-project index name.
        project_id = ""
        try:
            project_id = get_app().project.get("id") or ""
        except Exception:
            pass
        worker = BackendIndexingWorker(
            dict(file_obj.data), project_id=project_id, summarize_only=summarize_only,
        )
        self._active_indexers.append(worker)
        self._set_indexing_progress(file_id, "uploading", -1)

        def _on_finished():
            try:
                self._active_indexers.remove(worker)
            except ValueError:
                pass
            self._indexing_progress.pop(str(file_id), None)
            self._status_cache.pop(str(file_id), None)
            self._drain_indexing_queue()

        def _on_progress(fid, phase, percent):
            self._set_indexing_progress(fid, phase, percent)

        def _on_intermediate(fid, metadata):
            try:
                if not metadata or not isinstance(metadata, dict):
                    return
                f = _File.get(id=fid)
                if not f:
                    return
                self._apply_ai_metadata(f, metadata)
                self._save_file_untracked(f)
                get_app().window.FileUpdated.emit(str(fid))
                try:
                    get_app().window.schedule_flush_project_to_disk()
                except Exception:
                    pass
            except Exception as exc:
                log.warning(f"Failed to apply intermediate indexing result: {exc}")

        def _on_complete(_file_data, metadata, error):
            try:
                if error:
                    log.warning(f"Background indexing failed for {file_id}: {error}")
                    # Persist it, or the file keeps no status and shows no badge at all.
                    metadata = dict(metadata or {}, error=str(error))
                if not metadata or not isinstance(metadata, dict):
                    return
                f = _File.get(id=file_id)
                if not f:
                    return
                self._apply_ai_metadata(f, metadata)
                self._save_file_untracked(f)
                get_app().window.FileUpdated.emit(str(file_id))
                try:
                    get_app().window.schedule_flush_project_to_disk()
                except Exception:
                    pass
            except Exception as exc:
                log.warning(f"Failed to apply background indexing result: {exc}")

        worker.progress.connect(_on_progress)
        worker.intermediate_save.connect(_on_intermediate)
        worker.completed.connect(_on_complete)
        worker.finished.connect(_on_finished)
        worker.start()

    def add_files(self, files, image_seq_details=None, quiet=False,
                  prevent_image_seq=False, prevent_recent_folder=False,
                  skip_indexing=False):
        # Access translations
        app = get_app()
        settings = app.get_settings()
        _ = app._tr

        # Make sure we're working with a list of files
        if not isinstance(files, (list, tuple)):
            files = [files]
        scroll_to_files = []
        # Collect IDs of video files that need background indexing.  We start
        # the workers *after* add_files returns (via QTimer.singleShot) so that
        # any rapid worker completion doesn't deliver signals back into the model
        # while we're still updating it (reentrancy → model corruption / crash).
        _deferred_index_ids: list = []

        start_count = len(files)
        for count, filepath in enumerate(files):
            (dir_path, filename) = os.path.split(filepath)

            # Check for this path in our existing project data
            new_file = File.get(path=filepath)

            # If this file is already found, exit
            if new_file:
                # Still add the file (to be selected and scrolled to)
                scroll_to_files.append(new_file)
                del new_file
                continue

            try:
                # Inspect with libopenshot (tries multiple readers, retries eagerly on failure)
                file_data, _duration = inspect_media(filepath)

                # Determine media type
                file_data["media_type"] = get_media_type(file_data)

                # Check for audio-only files
                if is_audio_only_media(file_data):
                    # Cover-art MP3s report has_video=True; correct it at the source
                    # so every clip built from this file stays transparent.
                    file_data["has_video"] = False
                    # Audio-only file should match the current project size and FPS
                    project = get_app().project
                    file_data["width"] = project.get("width")
                    file_data["height"] = project.get("height")

                # Save new file to the project data
                new_file = File()
                new_file.data = file_data

                # Is this an image sequence / animation?
                seq_info = None
                if not prevent_image_seq:
                    seq_info = image_seq_details or self.get_image_sequence_details(filepath)

                if seq_info:
                    # Update file with image sequence path & name
                    new_path = seq_info.get("path")

                    # Load image sequence (to determine duration and video_length)
                    clip = openshot.Clip(new_path)
                    new_file.data = json.loads(clip.Reader().Json())
                    if clip and clip.info.duration > 0.0:
                        # Update file details
                        new_file.data["media_type"] = "video"
                        duration = new_file.data["duration"]

                        if seq_info and "fps" in seq_info and "length_multiplier" in seq_info:
                            # Blender Titles specify their fps in seq_info
                            fps_num = seq_info.get("fps", {}).get("num", 25)
                            fps_den = seq_info.get("fps", {}).get("den", 1)
                            log.debug("Image Sequence using specified FPS: %s / %s" % (fps_num, fps_den))
                        else:
                            # Get the project's fps, apply to the image sequence.
                            fps_num = get_app().project.get("fps").get("num", 30)
                            fps_den = get_app().project.get("fps").get("den", 1)
                            log.debug("Image Sequence using project FPS: %s / %s" % (fps_num, fps_den))

                        # Adjust FPS (difference between 25 FPS and actual FPS)
                        duration *= 25.0 / (float(fps_num) / float(fps_den))
                        new_file.data["duration"] = duration
                        new_file.data["fps"] = {"num": fps_num, "den": fps_den}
                        new_file.data["video_timebase"] = {"num": fps_den, "den": fps_num}

                        log.info(f"Imported '{new_path}' as image sequence with '{fps_num}/{fps_den}' FPS "
                                 f"and '{duration}' duration")

                        # Remove any other image sequence files from the list we're processing
                        match_glob = "{}{}.{}".format(seq_info.get("base_name"), '[0-9]*', seq_info.get("extension"))
                        log.debug("Removing files from import list with glob: {}".format(match_glob))
                        for seq_file in glob.iglob(os.path.join(seq_info.get("folder_path"), match_glob)):
                            # Don't remove the current file, or we mess up the for loop
                            if seq_file in files and seq_file != filepath:
                                files.remove(seq_file)
                    else:
                        # Failed to import image sequence
                        log.info(f"Failed to parse image sequence pattern {new_path}, ignoring...")
                        continue

                if not seq_info:
                    # Log our not-an-image-sequence import
                    log.info("Imported media file {}".format(filepath))

                # Stamp a content fingerprint for later relinking (skip sequences).
                try:
                    from classes.media_fingerprint import fingerprint as _media_fp
                    path_for_fp = new_file.data.get("path") or filepath
                    fp = _media_fp(path_for_fp)
                    if fp:
                        new_file.data["fingerprint"] = fp
                except Exception:
                    log.debug("Could not stamp media fingerprint for %s", filepath, exc_info=1)

                # Save file
                new_file.save()
                scroll_to_files.append(new_file)

                # Queue this video for background indexing (started after add_files
                # returns to avoid reentrancy with processEvents below).
                if not skip_indexing and new_file.data.get("media_type") in (
                    "video", "image", "audio",
                ):
                    _deferred_index_ids.append(new_file.id)

                if start_count > 15:
                    message = _("Importing %(count)d / %(total)d") % {
                            "count": count,
                            "total": len(files) - 1
                            }
                    app.window.statusBar.showMessage(message, 15000)

                # Let the event loop run to update the status bar. Restore the
                # active undo transaction afterward — processEvents can run
                # other code that clears updates.transaction_id, which would
                # mint a unique undo step per imported file.
                _tid = get_app().updates.transaction_id
                get_app().processEvents()
                get_app().updates.transaction_id = _tid
                # Update the recent import path
                if not prevent_recent_folder:
                    settings.setDefaultPath(settings.actionType.IMPORT, dir_path)

            except Exception as ex:
                # Log exception
                log.warning("Failed to import {}: {}".format(filepath, ex))

                if not quiet and start_count == 1:
                    # Show message box to user (if importing a single file)
                    app.window.invalidImage(filename)

        # Reset list of ignored paths
        self.ignore_image_sequence_paths = []

        # Select all new files (clear previous selection)
        self.selection_model.clearSelection()
        last_selected_index = QModelIndex()
        for file_object in scroll_to_files:
            # Get the index of the newly added file in the proxy model
            index = self.proxy_model.get_file_index(file_object.id)
            if index.isValid():
                # Select & scroll to selection
                self.selection_model.select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                get_app().window.filesView.scrollTo(index.siblingAtColumn(0), QAbstractItemView.PositionAtCenter)
                last_selected_index = index
        if last_selected_index.isValid():
            # Keep current index aligned with the newly selected file so actions
            # (preview/properties/etc.) resolve to the expected item.
            self.selection_model.setCurrentIndex(last_selected_index, QItemSelectionModel.NoUpdate)

        message = _("Imported %(count)d files") % {"count": len(files) - 1}
        app.window.statusBar.showMessage(message, 3000)

        # Start deferred indexing workers now that add_files has fully returned
        # to a stable state.  singleShot(0) fires on the next event-loop tick,
        # well outside this call frame, so worker callbacks can't re-enter here.
        for _fid in _deferred_index_ids:
            def _start(_fid=_fid):
                try:
                    self._index_file_async(_fid)
                except Exception as _e:
                    log.warning("Failed to start background indexing: %s", _e)
            QTimer.singleShot(0, _start)

        return scroll_to_files

    def get_image_sequence_details(self, file_path):
        """Inspect a file path and determine if this is an image sequence"""
        from classes.project_files import detect_image_sequence

        # Get just the file name
        (dirName, fileName) = os.path.split(file_path)

        # Image sequence imports are one per directory per run
        if dirName in self.ignore_image_sequence_paths:
            return None

        parameters = detect_image_sequence(file_path)
        if not parameters:
            # File name does not match an image sequence (or has no neighbouring frames)
            return None

        # Found a sequence, ignore this path (no matter what the user answers)
        # To avoid issues with overlapping/conflicting sets of files,
        # we only attempt one image sequence match per directory
        log.debug("Ignoring path for image sequence imports: {}".format(dirName))
        self.ignore_image_sequence_paths.append(dirName)

        log.info('Prompt user to import sequence starting from {}'.format(fileName))
        if not get_app().window.promptImageSequence(fileName):
            # User said no, don't import as a sequence
            return None

        # Yes, import image sequence
        return parameters

    def process_urls(self, qurl_list, import_quietly=False, prevent_image_seq=False,
                     transaction_id=None):
        """Recursively process QUrls from a QDropEvent.

        Returns the list of imported (or already-present) File objects, or an
        empty list when nothing was imported. Opening a dropped project file
        emits OpenProjectSignal and returns [].

        Reuses an existing ``updates.transaction_id`` when the caller already
        opened one (e.g. timeline drop that also places clips), so the whole
        gesture undoes as a single step. ``transaction_id`` lets a caller name
        that transaction explicitly; it stays active after this call so the
        caller can group follow-up mutations under it.
        """
        media_paths = []

        from classes.updates import nested_transaction

        if transaction_id:
            active_tid = get_app().updates.transaction_id
            if active_tid and active_tid != transaction_id:
                # Never hijack a caller's in-flight transaction: joining it keeps
                # the caller's later mutations in the undo step they expect.
                log.warning(
                    "process_urls: ignoring transaction_id %s, joining active %s",
                    transaction_id, active_tid,
                )
            else:
                get_app().updates.transaction_id = transaction_id

        with nested_transaction(get_app().updates):
            for uri in qurl_list or []:
                filepath = local_path_from_url(uri)
                if not filepath or not os.path.exists(filepath):
                    continue
                if filepath.endswith(info.ALL_PROJECT_EXTS) and os.path.isfile(filepath):
                    # Auto load project passed as argument
                    get_app().window.OpenProjectSignal.emit(filepath)
                    return []
                if os.path.isdir(filepath):
                    import_quietly = True
                    log.info("Recursively importing {}".format(filepath))
                    try:
                        for r, dirs, f in os.walk(filepath):
                            dirs.sort()
                            media_paths.extend(
                                [os.path.join(r, p) for p in sorted(f)])
                    except OSError:
                        log.warning("Directory recursion failed", exc_info=1)
                elif os.path.isfile(filepath):
                    media_paths.append(filepath)
            if not media_paths:
                return []
            # Import all new media files
            # Preserve the incoming path order (selection/drop order) instead of
            # forcing filename sorting.
            log.debug("Importing file list: {}".format(media_paths))
            return self.add_files(
                media_paths, quiet=import_quietly, prevent_image_seq=prevent_image_seq
            ) or []

    def update_file_thumbnail(self, file_id):
        """Update/re-generate the thumbnail of a specific file"""
        previous_ignore = self.ignore_updates
        try:
            self._update_file_thumbnail(file_id)
        finally:
            self.ignore_updates = previous_ignore

    def _update_file_thumbnail(self, file_id):
        self._status_cache.pop(str(file_id), None)
        file = File.get(id=file_id)
        if not file:
            return
        path, filename = os.path.split(file.data["path"])
        name = file.data.get("name", filename)

        # Refresh thumbnail for updated file
        previous_ignore = self.ignore_updates
        self.ignore_updates = True
        m = self.model

        if file_id in self.model_ids:
            # Look up stored index to ID column
            id_index = self.model_ids[file_id]
            if not id_index.isValid():
                return

            # Update thumb for file: video/image thumbnails are regenerated on
            # the worker and keep their current icon until the new one arrives
            thumb_index = id_index.sibling(id_index.row(), 0)
            item = m.itemFromIndex(thumb_index)
            if file.data.get("media_type") in ["video", "image"]:
                self._request_file_thumbnail(file, clear_cache=True)
            else:
                item.setIcon(QIcon(os.path.join(info.PATH, "images", "AudioThumbnail.svg")))
            item.setText(name)
            item.setToolTip(name)
            item.setAccessibleText(name)
            item.setToolTip(self._tooltip_for_file(file, filename))

            # Update display name
            text_index = id_index.sibling(id_index.row(), 1)
            item = m.itemFromIndex(text_index)
            item.setText(name)

            # Emit signal when model is updated
            self.ModelRefreshed.emit()

        self.ignore_updates = previous_ignore

    def selected_file_ids(self):
        """ Get a list of file IDs for all selected files """
        # Get the indexes for column 5 of all selected rows
        selected = self.selection_model.selectedRows(5)
        ids = []
        for idx in selected:
            file_id = idx.data()
            if not file_id or self._is_generation_placeholder(file_id):
                continue
            ids.append(file_id)
        return ids

    def selected_files(self):
        """ Get a list of File objects representing the current selection """
        files = []
        for id in self.selected_file_ids():
            files.append(File.get(id=id))
        return files

    def current_file_id(self):
        """ Get the file ID of the current files-view item, or the first selection """
        # Prefer selected rows first, since currentIndex can become stale when
        # switching between details/list views with separate selection models.
        selected_rows = self.selection_model.selectedRows(5)
        if selected_rows:
            selected_ids = {row_index.data() for row_index in selected_rows if row_index.data()}
            current = self.selection_model.currentIndex()
            if current and current.isValid():
                current_id = current.sibling(current.row(), 5).data()
                # A stale current index must not win over the real selection.
                if current_id and current_id in selected_ids:
                    return current_id
            for row_index in selected_rows:
                file_id = row_index.data()
                if file_id:
                    return file_id

        cur = self.selection_model.currentIndex()
        if cur and cur.isValid():
            file_id = cur.sibling(cur.row(), 5).data()
            if file_id and not self._is_generation_placeholder(file_id):
                return file_id

    def current_file(self):
        """ Get the File object for the current files-view item, or the first selection """
        cur_id = self.current_file_id()
        if cur_id:
            return File.get(id=cur_id)
        else:
            return None

    def value_updated(self, item):
        """ Table cell change event - when tags are updated on a file"""
        # Only user edits: the model's own refreshes (a file saved by a tool, a tag
        # change, undo/redo) must not be written back -- into whichever file is selected.
        if self.ignore_updates or item.column() != 2:
            return
        id_item = self.model.item(item.row(), 5)
        f = File.get(id=id_item.text()) if id_item else None
        if f:
            tags_value = item.data(0)
            if f.data.get("tags", "") != tags_value:
                # Save tags to the edited row's file
                f.data["tags"] = tags_value
                f.save()

    def _sync_tree_to_list_selection(self, selected, deselected):
        """Sync selection from TreeView (proxy_model) to ListView (list_proxy_model)"""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            # Map selected indexes from proxy_model to list_proxy_model
            list_selection = QItemSelection()
            for index in self.selection_model.selectedRows(0):
                list_index = self.list_proxy_model.mapFromSource(index)
                if list_index.isValid():
                    list_selection.select(list_index, list_index)
            self.list_selection_model.select(
                list_selection,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
            )
        finally:
            self._syncing_selection = False

    def _sync_list_to_tree_selection(self, selected, deselected):
        """Sync selection from ListView (list_proxy_model) to TreeView (proxy_model)"""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            # Map selected indexes from list_proxy_model to proxy_model
            tree_selection = QItemSelection()
            for index in self.list_selection_model.selectedRows(0):
                tree_index = self.list_proxy_model.mapToSource(index)
                if tree_index.isValid():
                    tree_selection.select(tree_index, tree_index)
            self.selection_model.select(
                tree_selection,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
            )
        finally:
            self._syncing_selection = False

    def __init__(self, *args, proxy_service=None, generation_queue=None):
        # Optimize Preview service (badges, tooltips, per-row repaints)
        self.proxy_service = proxy_service
        self.generation_queue = generation_queue

        # Add self as listener to project data updates
        # (undo/redo, as well as normal actions handled within this class all update the model)
        app = get_app()
        app.updates.add_listener(self)

        # Create standard model
        self.model = QStandardItemModel()
        self.model.setColumnCount(6)
        self.model_ids = {}
        self.ignore_updates = False
        self.ignore_image_sequence_paths = []
        self._active_indexers = []  # strong refs to keep QThreads alive until finished
        self._indexing_queue = []  # (file_id, summarize_only) waiting for a worker slot
        self._indexing_progress = {}
        self._status_cache = {}
        self.thumbnails = None  # thumbnail worker, created below
        self._thumbnail_generation = 0  # bumped when the rows are rebuilt
        self._thumbnail_callbacks = {}  # (slot, frame) -> [on_ready(QImage)]

        # Stop any running indexing threads cleanly when the app quits
        try:
            get_app().aboutToQuit.connect(self._stop_active_indexers)
        except Exception:
            pass

        # Files whose cloud indexing waited for a signed-in user resume once a session is
        # saved. The signal hops to the GUI thread, wherever the login finished.
        try:
            from classes.auth_manager import AuthManager
            self.sessionSaved.connect(self.requeue_signin_skipped)
            AuthManager.instance().add_session_listener(self.sessionSaved.emit)
        except Exception:
            log.debug("Could not watch for sign-in", exc_info=1)

        # Create proxy model (for sorting and filtering) - used by TreeView
        self.proxy_model = FileFilterProxyModel(parent=self)
        self.proxy_model.setDynamicSortFilter(True)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setSortCaseSensitivity(Qt.CaseSensitive)
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setSortLocaleAware(True)

        # Create single-column proxy for ListView (wraps proxy_model for accessibility)
        self.list_proxy_model = SingleColumnProxyModel()
        self.list_proxy_model.setSourceModel(self.proxy_model)

        # Connect data changed signal
        self.model.itemChanged.connect(self.value_updated)

        # Create selection models for each view
        self.selection_model = QItemSelectionModel(self.proxy_model)
        self.list_selection_model = QItemSelectionModel(self.list_proxy_model)

        # Sync selections between the two selection models
        self._syncing_selection = False
        self.selection_model.selectionChanged.connect(self._sync_tree_to_list_selection)
        self.list_selection_model.selectionChanged.connect(self._sync_list_to_tree_selection)

        # Connect signal
        app.window.FileUpdated.connect(self.update_file_thumbnail)
        if self.proxy_service is not None:
            self.proxy_service.file_job_changed.connect(self._on_proxy_file_changed)
        app.window.refreshFilesSignal.connect(
            functools.partial(self.update_model, clear=False))
        if self.generation_queue:
            self.generation_queue.file_job_changed.connect(self._refresh_file_generation_display)
            self.generation_queue.queue_changed.connect(self._refresh_all_generation_displays)
            self.generation_queue.job_added.connect(self._on_generation_job_added)
            self.generation_queue.job_updated.connect(self._on_generation_job_updated)
            self.generation_queue.job_finished.connect(self._on_generation_job_finished)
            self.generation_queue.job_removed.connect(self._on_generation_job_removed)

        # Call init for superclass QObject
        super().__init__(*args)

        # Thumbnails are made off the GUI thread by the batched worker the
        # native timeline uses, with its results delivered back queued
        from windows.views.timeline_backend.qwidget.thumbnails import TimelineThumbnailManager
        self.thumbnails = TimelineThumbnailManager(
            self, max_pending=None, attempts=self.PROJECT_FILE_THUMB_ATTEMPTS,
            thread_name="project_files_thumbnail")
        self.thumbnails.thumbnail_ready.connect(self._on_thumbnail_ready, type=Qt.QueuedConnection)
        app.aboutToQuit.connect(self._stop_thumbnail_worker)

        # Attempt to load model testing interface, if requested
        # (will only succeed with Qt 5.11+)
        if info.MODEL_TEST:
            try:
                # Create model tester objects
                from qt_api import QAbstractItemModelTester
                self.model_tests = []
                for m in [self.proxy_model, self.model]:
                    self.model_tests.append(
                        QAbstractItemModelTester(
                            m, QAbstractItemModelTester.FailureReportingMode.Warning)
                    )
                log.info("Enabled {} model tests for emoji data".format(len(self.model_tests)))
            except ImportError:
                pass
    def _is_generation_placeholder(self, file_id):
        return str(file_id or "").startswith(self.PLACEHOLDER_PREFIX)

    def _placeholder_id_for_job(self, job_id):
        return "{}{}".format(self.PLACEHOLDER_PREFIX, str(job_id or ""))

    def _job_id_from_placeholder(self, file_id):
        file_id = str(file_id or "")
        if not self._is_generation_placeholder(file_id):
            return None
        return file_id[len(self.PLACEHOLDER_PREFIX):]

    def _placeholder_row_for_job(self, job_id):
        placeholder_id = self._placeholder_id_for_job(job_id)
        if placeholder_id not in self.model_ids:
            return None
        id_index = self.model_ids[placeholder_id]
        if not id_index.isValid():
            return None
        return id_index.row()

    def _generation_icon_for_job(self, job):
        icon_name = "tool-generate-sparkle.svg"
        try:
            app = get_app()
            window = getattr(app, "window", None)
            generation_service = getattr(window, "generation_service", None)
            if generation_service and isinstance(job, dict):
                template_id = str(job.get("template_id") or "").strip()
                template = generation_service.template_registry.get_template(template_id)
                if template:
                    resolved_icon = generation_service.icon_for_template(template)
                    if resolved_icon:
                        icon_name = resolved_icon
        except Exception:
            pass

        icon_path = os.path.join(info.PATH, "themes", "cosmic", "images", icon_name)
        if os.path.exists(icon_path):
            return QIcon(icon_path)

        emoji_icon_path = os.path.join(info.PATH, "emojis", "color", "svg", "2728.svg")
        if os.path.exists(emoji_icon_path):
            return QIcon(emoji_icon_path)
        return QIcon(":/icons/Humanity/actions/16/media-record.svg")

    def _add_generation_placeholder(self, job_id):
        job = self.generation_queue.get_job(job_id) if self.generation_queue else None
        if not job:
            return
        if job.get("source_file_id"):
            return

        placeholder_id = self._placeholder_id_for_job(job_id)
        if placeholder_id in self.model_ids and self.model_ids[placeholder_id].isValid():
            self._update_generation_placeholder(job_id)
            return

        name = str(job.get("name") or "generation")
        status = str(job.get("status") or "queued")
        progress = int(job.get("progress", 0))
        progress_detail = str(job.get("progress_detail") or "").strip()
        label = name
        if status == "running":
            label = "{} ({}%)".format(name, progress)
            if progress_detail:
                label = "{} [{}]".format(label, progress_detail)
        elif status == "queued":
            label = "{} (Queued)".format(name)
        elif status == "canceling":
            label = "{} (Canceling...)".format(name)

        row = []
        icon = self._generation_icon_for_job(job)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemNeverHasChildren

        col = QStandardItem(icon, label)
        col.setFlags(flags)
        row.append(col)

        col = QStandardItem(label)
        col.setFlags(flags)
        row.append(col)

        col = QStandardItem("generation")
        col.setFlags(flags)
        row.append(col)

        col = QStandardItem("generation_job")
        col.setFlags(flags)
        row.append(col)

        col = QStandardItem("")
        col.setFlags(flags)
        row.append(col)

        col = QStandardItem(placeholder_id)
        col.setFlags(flags)
        row.append(col)

        self.model.appendRow(row)
        self.model_ids[placeholder_id] = QPersistentModelIndex(row[5].index())
        self.ModelRefreshed.emit()

    def _update_generation_placeholder(self, job_id):
        row = self._placeholder_row_for_job(job_id)
        if row is None:
            self._add_generation_placeholder(job_id)
            return
        job = self.generation_queue.get_job(job_id) if self.generation_queue else None
        if not job:
            return

        name = str(job.get("name") or "generation")
        status = str(job.get("status") or "queued")
        progress = int(job.get("progress", 0))
        progress_detail = str(job.get("progress_detail") or "").strip()
        label = name
        if status == "running":
            label = "{} ({}%)".format(name, progress)
            if progress_detail:
                label = "{} [{}]".format(label, progress_detail)
        elif status == "queued":
            label = "{} (Queued)".format(name)
        elif status == "canceling":
            label = "{} (Canceling...)".format(name)

        self.model.item(row, 0).setIcon(self._generation_icon_for_job(job))
        self.model.item(row, 0).setText(label)
        self.model.item(row, 1).setText(label)
        left = self.model.index(row, 0)
        right = self.model.index(row, 1)
        self.model.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.AccessibleTextRole])
        self.ModelRefreshed.emit()

    def _remove_generation_placeholder(self, job_id):
        placeholder_id = self._placeholder_id_for_job(job_id)
        if placeholder_id not in self.model_ids:
            return
        id_index = self.model_ids.get(placeholder_id)
        if not id_index or not id_index.isValid():
            self.model_ids.pop(placeholder_id, None)
            return
        row = id_index.row()
        self.model.removeRows(row, 1, id_index.parent())
        self.model.submit()
        self.model_ids.pop(placeholder_id, None)
        self.ModelRefreshed.emit()

    def _rebuild_generation_placeholders(self):
        if not self.generation_queue:
            return
        for job in list(self.generation_queue.jobs.values()):
            if job.get("source_file_id"):
                continue
            if job.get("status") in ("completed", "failed", "canceled"):
                self._remove_generation_placeholder(job.get("id"))
            else:
                self._add_generation_placeholder(job.get("id"))

    def _on_generation_job_added(self, job_id, source_file_id):
        if source_file_id:
            return
        self._add_generation_placeholder(job_id)

    def _on_generation_job_updated(self, job_id, status, progress):
        job = self.generation_queue.get_job(job_id) if self.generation_queue else None
        if not job or job.get("source_file_id"):
            return
        if status in ("completed", "failed", "canceled"):
            self._remove_generation_placeholder(job_id)
        else:
            self._update_generation_placeholder(job_id)

    def _on_generation_job_finished(self, job_id, status):
        job = self.generation_queue.get_job(job_id) if self.generation_queue else None
        if not job or job.get("source_file_id"):
            return
        self._remove_generation_placeholder(job_id)

    def _on_generation_job_removed(self, job_id):
        self._remove_generation_placeholder(job_id)

    def _refresh_file_generation_display(self, file_id):
        file_id = str(file_id or "")
        if not file_id:
            return
        if file_id not in self.model_ids:
            return
        id_index = self.model_ids[file_id]
        if not id_index.isValid():
            return
        row = id_index.row()
        left = self.model.index(row, 0)
        right = self.model.index(row, 0)
        self.model.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.AccessibleTextRole])
        self.ModelRefreshed.emit()

    def _refresh_all_generation_displays(self):
        if self.model.rowCount() < 1:
            return
        left = self.model.index(0, 0)
        right = self.model.index(self.model.rowCount() - 1, 0)
        self.model.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.AccessibleTextRole])
        self.ModelRefreshed.emit()
