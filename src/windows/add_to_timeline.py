"""
 @file
 @brief This file loads the Addtotimeline dialog (i.e add several clips in the timeline)
 @author Jonathan Thomas <jonathan@openshot.org>
 @author Olivier Girard <olivier@openshot.org>

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
from operator import itemgetter
from random import shuffle

from qt_api import QDialog
from qt_api import QIcon

from classes import info, ui_util, time_parts
from classes.logger import log
from classes.app import get_app
from classes.metrics import track_metric_screen
from classes.timeline_ops import place_files
from classes.updates import nested_transaction
from windows.views.add_to_timeline_treeview import TimelineTreeView


class AddToTimeline(QDialog):
    """ Add To timeline Dialog """

    ui_path = os.path.join(info.PATH, 'windows', 'ui', 'add-to-timeline.ui')

    def _select_added_items(self, win, added_clip_ids):
        """Select only the newly added clips, matching timeline drag/drop behavior."""
        if not win or not added_clip_ids:
            return

        timeline_view = getattr(win, "timeline", None)
        for idx, clip_id in enumerate(added_clip_ids):
            if not clip_id:
                continue
            if timeline_view and hasattr(timeline_view, "AddSelectionJS"):
                timeline_view.AddSelectionJS(str(clip_id), "clip", idx == 0)
            else:
                win.addSelection(str(clip_id), "clip", clear_existing=(idx == 0))

        files_model = getattr(win, "files_model", None)
        if files_model:
            selection_model = getattr(files_model, "selection_model", None)
            if selection_model:
                selection_model.clearSelection()
            list_selection_model = getattr(files_model, "list_selection_model", None)
            if list_selection_model:
                list_selection_model.clearSelection()

        if timeline_view and hasattr(timeline_view, "setFocus"):
            timeline_view.setFocus()
        if timeline_view and hasattr(timeline_view, "geometry"):
            timeline_geometry = getattr(timeline_view, "geometry", None)
            if hasattr(timeline_geometry, "mark_dirty"):
                timeline_geometry.mark_dirty()
        if timeline_view and hasattr(timeline_view, "update"):
            timeline_view.update()

    def btnMoveUpClicked(self, checked):
        """Callback for move up button click"""
        log.info("btnMoveUpClicked")

        # Get selected file
        files = self.treeFiles.timeline_model.files

        selected_index = None
        if self.treeFiles.selected:
            selected_index = self.treeFiles.selected.row()

        # Ignore if empty files or no selection
        if not files or selected_index is None:
            return

        # Check if selected_index is within valid range
        if 0 <= selected_index < len(files):
            # New index
            new_index = max(selected_index - 1, 0)

            # Remove item and move it
            files.insert(new_index, files.pop(selected_index))
        else:
            log.warning(f"Invalid selected_index: {selected_index}, list length: {len(files)}")
            return

        # Refresh tree
        self.treeFiles.refresh_view()

        # Select new position
        idx = self.treeFiles.timeline_model.model.index(new_index, 0)
        self.treeFiles.setCurrentIndex(idx)

    def btnMoveDownClicked(self, checked):
        """Callback for move up button click"""
        log.info("btnMoveDownClicked")

        # Get selected file
        files = self.treeFiles.timeline_model.files

        selected_index = None
        if self.treeFiles.selected:
            selected_index = self.treeFiles.selected.row()

        # Ignore if empty files or no selection
        if not files or selected_index is None:
            return

        # Check if selected_index is within valid range
        if 0 <= selected_index < len(files):
            # New index
            new_index = min(selected_index + 1, len(files) - 1)

            # Remove item and move it
            files.insert(new_index, files.pop(selected_index))
        else:
            log.warning(f"Invalid selected_index: {selected_index}, list length: {len(files)}")
            return

        # Refresh tree
        self.treeFiles.refresh_view()

        # Select new position
        idx = self.treeFiles.timeline_model.model.index(new_index, 0)
        self.treeFiles.setCurrentIndex(idx)

    def btnShuffleClicked(self, checked):
        """Callback for move up button click"""
        log.info("btnShuffleClicked")

        # Shuffle files
        shuffle(self.treeFiles.timeline_model.files)

        # Refresh tree
        self.treeFiles.refresh_view()

    def btnRemoveClicked(self, checked):
        """Callback for move up button click"""
        log.info("btnRemoveClicked")

        # Get selected file
        files = self.treeFiles.timeline_model.files

        selected_index = None
        if self.treeFiles.selected:
            selected_index = self.treeFiles.selected.row()

        # Ignore if empty files
        if not files or selected_index is None:
            return

        # Remove item
        files.pop(selected_index)

        # Refresh tree
        self.treeFiles.refresh_view()

        # Select next item (if any)
        new_index = max(len(files) - 1, 0)

        # Select new position
        idx = self.treeFiles.timeline_model.model.index(new_index, 0)
        self.treeFiles.setCurrentIndex(idx)

        # Update total
        self.updateTotal()

    def accept(self):
        """ Ok button clicked """
        log.info('accept')

        # Get settings from form
        transition_path = self.cmbTransition.currentData()

        # Place every file (in the current order) in one undo step
        with nested_transaction(get_app().updates):
            added_clip_ids, _transition_ids = place_files(
                [{"file": file} for file in self.treeFiles.timeline_model.files],
                self.txtStartTime.value(),
                self.cmbTrack.currentData(),
                fade=self.cmbFade.currentData(),
                fade_length=self.txtFadeLength.value(),
                transition_path=None if transition_path == "random" else transition_path,
                random_transitions=self.transitions if transition_path == "random" else None,
                transition_length=self.txtTransitionLength.value(),
                image_length=self.txtImageLength.value(),
                zoom=self.cmbZoom.currentData(),
            )

        # Ensure timeline extension behavior matches all other timeline add/move paths.
        timeline_view = getattr(get_app().window, "timeline", None)
        extend_timeline = getattr(timeline_view, "_extend_timeline_to_fit_items", None)
        if callable(extend_timeline):
            extend_timeline()

        # Auto-select newly added clips, like timeline drag/drop does.
        self._select_added_items(get_app().window, added_clip_ids)

        # Accept dialog
        super(AddToTimeline, self).accept()

    def ImageLengthChanged(self, value):
        """Handle callback for image length being changed"""
        self.updateTotal()

    def updateTotal(self):
        """Calculate the total length of what's about to be added to the timeline"""
        fade_value = self.cmbFade.currentData()
        fade_length = self.txtFadeLength.value()
        transition_path = self.cmbTransition.currentData()
        transition_length = self.txtTransitionLength.value()

        total = 0.0
        for file in self.treeFiles.timeline_model.files:
            # Adjust clip duration, start, and end
            duration = file.data["duration"]
            if file.data["media_type"] == "image":
                duration = self.txtImageLength.value()

            if total != 0.0:
                # Don't subtract time from initial clip
                if not transition_path:
                    # No transitions
                    if fade_value is not None:
                        # Fade clip - subtract the fade length
                        duration -= fade_length
                else:
                    # Transition
                    duration -= transition_length

            # Append duration to total
            total += duration

        # Get frames per second
        fps = get_app().project.get("fps")

        # Update label
        total_parts = time_parts.secondsToTime(total, fps["num"], fps["den"])
        timestamp = "%s:%s:%s:%s" % (total_parts["hour"], total_parts["min"], total_parts["sec"], total_parts["frame"])
        self.lblTotalLengthValue.setText(timestamp)

    def reject(self):
        """ Cancel button clicked """
        log.info('reject')

        # Accept dialog
        super(AddToTimeline, self).reject()

    def __init__(self, files=None, position=0.0):
        # Create dialog class
        super().__init__()

        # Load UI from Designer
        ui_util.load_ui(self, self.ui_path)

        # Init UI
        ui_util.init_ui(self)

        # Get translation object
        self.app = get_app()
        _ = self.app._tr

        # Get settings
        self.settings = self.app.get_settings()

        # Track metrics
        track_metric_screen("add-to-timeline-screen")

        # Add custom treeview to window
        self.treeFiles = TimelineTreeView(self)
        self.vboxTreeParent.insertWidget(0, self.treeFiles)

        # Update data in model
        self.treeFiles.timeline_model.update_model(files)

        # Init start position
        self.txtStartTime.setValue(position)

        # Init default image length
        self.txtImageLength.setValue(self.settings.get("default-image-length"))
        self.txtImageLength.valueChanged.connect(self.updateTotal)
        self.cmbTransition.currentIndexChanged.connect(self.updateTotal)
        self.cmbFade.currentIndexChanged.connect(self.updateTotal)
        self.txtFadeLength.valueChanged.connect(self.updateTotal)
        self.txtTransitionLength.valueChanged.connect(self.updateTotal)

        # Find display track number
        all_tracks = get_app().project.get("layers")
        display_count = len(all_tracks)
        for track in reversed(sorted(all_tracks, key=itemgetter('number'))):
            # Add to dropdown
            track_name = track.get('label') or _("Track %s") % display_count
            self.cmbTrack.addItem(track_name, track.get('number'))
            display_count -= 1

        # Add all fade options
        self.cmbFade.addItem(_('None'), None)
        self.cmbFade.addItem(_('Fade In'), 'Fade In')
        self.cmbFade.addItem(_('Fade Out'), 'Fade Out')
        self.cmbFade.addItem(_('Fade In & Out'), 'Fade In & Out')

        # Add all zoom options
        self.cmbZoom.addItem(_('None'), None)
        self.cmbZoom.addItem(_('Random'), 'Random')
        self.cmbZoom.addItem(_('Zoom In'), 'Zoom In')
        self.cmbZoom.addItem(_('Zoom Out'), 'Zoom Out')

        # Add all transitions
        transitions_dir = os.path.join(info.PATH, "transitions")
        common_dir = os.path.join(transitions_dir, "common")
        extra_dir = os.path.join(transitions_dir, "extra")
        transition_groups = [{"type": "common", "dir": common_dir, "files": os.listdir(common_dir)},
                             {"type": "extra", "dir": extra_dir, "files": os.listdir(extra_dir)}]

        self.cmbTransition.addItem(_('None'), None)
        self.cmbTransition.addItem(_('Random'), 'random')
        self.transitions = []
        for group in transition_groups:
            dir = group["dir"]
            files = group["files"]

            for filename in sorted(files):
                path = os.path.join(dir, filename)
                fileBaseName = os.path.splitext(filename)[0]

                # Skip hidden files (such as .DS_Store, etc...)
                if filename[0] == "." or "thumbs.db" in filename.lower():
                    continue

                # split the name into parts (looking for a number)
                suffix_number = None
                name_parts = fileBaseName.split("_")
                if name_parts[-1].isdigit():
                    suffix_number = name_parts[-1]

                # get name of transition
                trans_name = fileBaseName.replace("_", " ").capitalize()

                # replace suffix number with placeholder (if any)
                if suffix_number:
                    trans_name = trans_name.replace(suffix_number, "%s")
                    trans_name = _(trans_name) % suffix_number
                else:
                    trans_name = _(trans_name)

                # Check for thumbnail path (in build-in cache)
                thumb_path = os.path.join(info.IMAGES_PATH, "cache",  "{}.png".format(fileBaseName))

                # Check built-in cache (if not found)
                if not os.path.exists(thumb_path):
                    # Check user folder cache
                    thumb_path = os.path.join(info.CACHE_PATH, "{}.png".format(fileBaseName))

                # Add item
                self.transitions.append(path)
                self.cmbTransition.addItem(QIcon(thumb_path), _(trans_name), path)

        # Connections
        self.btnMoveUp.clicked.connect(self.btnMoveUpClicked)
        self.btnMoveDown.clicked.connect(self.btnMoveDownClicked)
        self.btnShuffle.clicked.connect(self.btnShuffleClicked)
        self.btnRemove.clicked.connect(self.btnRemoveClicked)
        self.btnBox.accepted.connect(self.accept)
        self.btnBox.rejected.connect(self.reject)

        # Update total
        self.updateTotal()
