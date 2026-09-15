"""
 @file
 @brief This file generates the path for a project's assets
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
import shutil
from classes import info
from classes.logger import log


def get_assets_path(file_path=None, create_paths=True):
    """Get and/or create the current assets path. This path is used for thumbnail and blender files,
    and is unique to each project. For example: `Project1.zvn` would use `Project1_assets` folder."""
    if not file_path:
        return info.USER_PATH

    try:
        # Generate asset folder name filename + "_assets"
        file_path = file_path
        asset_filename = os.path.splitext(os.path.basename(file_path))[0]
        asset_folder_name = asset_filename[:248] + "_assets" #Windows max name size is 255. 248 = 255 - len("_assets")
        asset_path = os.path.join(os.path.dirname(file_path), asset_folder_name)

        # Previous Assets File Name Convention.
        # We can remove the 30_char variables after 05/27/2022
        asset_folder_name_30_char = asset_filename[:30] + "_assets"
        asset_path_30_char = os.path.join(os.path.dirname(file_path), asset_folder_name_30_char)

        # Create asset folder, if necessary
        if create_paths:
            if not os.path.exists(asset_path):
                if os.path.exists(asset_path_30_char):
                    # Copy assets folder, if it follows the previous naming convention
                    # must leave a copy for possible projects that shared the folder.
                    try:
                        shutil.copytree(asset_path_30_char, asset_path)
                        log.info("Copying shortened asset folder. {}".format(asset_path))
                    except:
                        log.error("Could not make a copy of assets folder")
                else:
                    os.mkdir(asset_path)
                    log.info("Asset dir created as {}".format(asset_path))
            else:
                log.debug("Using existing asset folder {}".format(asset_path))

            # Create asset thumbnails folder
            asset_thumbnails_folder = os.path.join(asset_path, "thumbnail")
            if not os.path.exists(asset_thumbnails_folder):
                os.mkdir(asset_thumbnails_folder)
                log.info("New thumbnails folder: {}".format(asset_thumbnails_folder))

            # Create asset title folder
            asset_titles_folder = os.path.join(asset_path, "title")
            if not os.path.exists(asset_titles_folder):
                os.mkdir(asset_titles_folder)
                log.info("New titles folder: {}".format(asset_titles_folder))

            # Create asset blender folder
            asset_blender_folder = os.path.join(asset_path, "blender")
            if not os.path.exists(asset_blender_folder):
                os.mkdir(asset_blender_folder)
                log.info("New blender folder: {}".format(asset_blender_folder))

            # Create asset clipboard folder
            asset_clipboard_folder = os.path.join(asset_path, "clipboard")
            if not os.path.exists(asset_clipboard_folder):
                os.mkdir(asset_clipboard_folder)
                log.info("New clipboard folder: {}".format(asset_clipboard_folder))

            asset_media_folder = os.path.join(asset_path, "media")
            if not os.path.exists(asset_media_folder):
                os.mkdir(asset_media_folder)
                log.info("New media folder: {}".format(asset_media_folder))

        return asset_path

    except Exception as ex:
        log.error("Error while getting/creating asset folder {}: {}".format(asset_path, ex))


def path_is_under(path, root):
    """True if *path* is inside *root* (or is *root*). Cross-drive paths are not."""
    try:
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(root)
        return os.path.commonpath([abs_path, abs_root]) == abs_root
    except (ValueError, TypeError):
        return False


def copy_imported_media(files, clips, project_file_path, app_root=None):
    """Copy imported/stock media into ``ProjectName_assets/media``.

    Mutates *files* and *clips* so their paths point at the copies. Bundled
    app resources and files already inside this project's assets folder are
    left in place. Missing files and image-sequence paths (``%``) are skipped.
    """
    if not project_file_path or not files:
        return

    asset_path = get_assets_path(project_file_path, create_paths=True)
    if not asset_path:
        return
    media_dir = os.path.join(asset_path, "media")
    try:
        os.makedirs(media_dir, exist_ok=True)
    except OSError:
        log.error("Could not create media folder %s", media_dir, exc_info=1)
        return

    id_to_new = {}
    src_to_new = {}

    for file in files:
        src = file.get("path") or ""
        if not src or "%" in src:
            continue
        if not os.path.isfile(src):
            continue
        abs_src = os.path.abspath(src)
        if path_is_under(abs_src, asset_path):
            continue
        if app_root and path_is_under(abs_src, app_root):
            continue

        dest_name = os.path.basename(abs_src)
        dest = os.path.join(media_dir, dest_name)
        if os.path.isfile(dest):
            try:
                same = os.path.samefile(abs_src, dest)
            except OSError:
                same = False
            if not same:
                stem, ext = os.path.splitext(dest_name)
                dest_name = "%s_%s%s" % (stem, file.get("id") or "file", ext)
                dest = os.path.join(media_dir, dest_name)

        if not os.path.isfile(dest):
            try:
                shutil.copy2(abs_src, dest)
            except Exception:
                log.error("Could not copy imported media %s to %s", abs_src, dest, exc_info=1)
                continue
            log.info("Copied imported media %s to %s", abs_src, dest)

        file["path"] = dest
        file_id = file.get("id")
        if file_id:
            id_to_new[file_id] = dest
        src_to_new[abs_src] = dest

    if not id_to_new and not src_to_new:
        return

    for clip in clips or []:
        reader = clip.get("reader")
        if not isinstance(reader, dict):
            reader = {}
            clip["reader"] = reader
        file_id = clip.get("file_id")
        rpath = reader.get("path") or ""
        if file_id and file_id in id_to_new:
            reader["path"] = id_to_new[file_id]
        elif rpath:
            abs_rpath = os.path.abspath(rpath)
            if abs_rpath in src_to_new:
                reader["path"] = src_to_new[abs_rpath]


def snapshot_media_paths(files, clips):
    """Record file and clip reader paths so a failed save can roll them back."""
    file_paths = [(item, item.get("path")) for item in files or []]
    clip_paths = []
    for clip in clips or []:
        reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else None
        clip_paths.append((clip, None if reader is None else reader.get("path")))
    return file_paths, clip_paths


def restore_media_paths(snapshot):
    """Undo in-memory path mutations from ``copy_imported_media``."""
    if not snapshot:
        return
    file_paths, clip_paths = snapshot
    for item, path in file_paths:
        item["path"] = path
    for clip, path in clip_paths:
        reader = clip.get("reader")
        if not isinstance(reader, dict):
            continue
        if path is None:
            reader.pop("path", None)
        else:
            reader["path"] = path
