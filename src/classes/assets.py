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


def durable_media_path(ext=".mp4", project_file_path=None):
    """Return a durable absolute path for a new generated media file.

    Saved projects write into ``{Project}_assets/media/``. Unsaved projects
    write into ``{USER_PATH}/generated/``. Never returns an OS temp path.
    """
    import uuid as _uuid

    ext = ext if str(ext).startswith(".") else f".{ext}"
    if not ext:
        ext = ".mp4"
    name = "generated_%s%s" % (_uuid.uuid4().hex[:12], ext)

    if not project_file_path:
        try:
            from classes.app import get_app
            app = get_app()
            if app and getattr(app, "project", None):
                project_file_path = getattr(app.project, "current_filepath", None)
        except Exception:
            project_file_path = None

    if project_file_path:
        asset_path = get_assets_path(project_file_path, create_paths=True)
        if asset_path:
            media_dir = os.path.join(asset_path, "media")
            try:
                os.makedirs(media_dir, exist_ok=True)
                return os.path.join(media_dir, name)
            except OSError:
                log.error("Could not create media folder %s", media_dir, exc_info=1)

    out_dir = os.path.join(info.USER_PATH, "generated")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        log.error("Could not create generated folder %s", out_dir, exc_info=1)
        # Last resort: still avoid OS temp by writing under USER_PATH.
        return os.path.join(info.USER_PATH, name)
    return os.path.join(out_dir, name)


def cleanup_scratch_parent(path, prefix):
    """Remove a tempfile.mkdtemp parent when *path* sits under a matching prefix."""
    try:
        parent = os.path.dirname(os.path.abspath(path or ""))
        if parent and os.path.basename(parent).startswith(prefix) and os.path.isdir(parent):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        log.debug("Scratch cleanup failed for %s", path, exc_info=1)


def _generated_roots():
    """Directories that hold generated media awaiting project ownership."""
    return [
        os.path.join(info.USER_PATH, "generated"),
        os.path.join(info.USER_PATH, "Generated"),  # legacy sibling name
    ]


def relocate_generated_media(files, clips, project_file_path):
    """Move unsaved generated media into ``{Project}_assets/media``.

    Only relocates files that live under ``USER_PATH/generated`` (or the
    legacy ``Generated`` folder). Imported user footage is left in place.
    Returns a move ledger ``[(src, dest), ...]`` for rollback on save failure.
    """
    moves = []
    if not project_file_path or not files:
        return moves

    asset_path = get_assets_path(project_file_path, create_paths=True)
    if not asset_path:
        return moves
    media_dir = os.path.join(asset_path, "media")
    try:
        os.makedirs(media_dir, exist_ok=True)
    except OSError:
        log.error("Could not create media folder %s", media_dir, exc_info=1)
        return moves

    roots = [os.path.abspath(r) for r in _generated_roots()]
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
        if not any(path_is_under(abs_src, root) for root in roots):
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

        if abs_src != os.path.abspath(dest):
            try:
                shutil.move(abs_src, dest)
            except Exception:
                log.error("Could not relocate generated media %s to %s", abs_src, dest, exc_info=1)
                # Reverse any moves already done in this batch.
                reverse_media_moves(moves)
                moves[:] = []
                raise
            moves.append((abs_src, dest))
            log.info("Relocated generated media %s to %s", abs_src, dest)

        file["path"] = dest
        file_id = file.get("id")
        if file_id:
            id_to_new[file_id] = dest
        src_to_new[abs_src] = dest

    if not id_to_new and not src_to_new:
        return moves

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

    return moves


def reverse_media_moves(moves):
    """Undo filesystem moves from ``relocate_generated_media`` (dest -> src)."""
    if not moves:
        return
    for src, dest in reversed(list(moves)):
        try:
            if os.path.isfile(dest):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dest, src)
        except Exception:
            log.error("Could not reverse media move %s -> %s", dest, src, exc_info=1)


def snapshot_media_paths(files, clips):
    """Record file and clip reader paths so a failed save can roll them back."""
    file_paths = [(item, item.get("path")) for item in files or []]
    clip_paths = []
    for clip in clips or []:
        reader = clip.get("reader") if isinstance(clip.get("reader"), dict) else None
        clip_paths.append((clip, None if reader is None else reader.get("path")))
    return file_paths, clip_paths


def restore_media_paths(snapshot):
    """Undo in-memory path mutations from ``relocate_generated_media``."""
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
