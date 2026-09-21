"""
 @file
 @brief Explicit media collect / reclaim helpers (user-invoked)
"""

import os
import shutil

from classes.assets import get_assets_path, path_is_under, unique_media_dest
from classes.logger import log
from classes.media_fingerprint import files_identical


def collect_media_into_project(files, clips, project_file_path, app_root=None):
    """Copy referenced external media into ``{Project}_assets/media``.

    User-invoked handoff/archive helper. Returns ``(copied, skipped, errors)``.
    """
    copied = []
    skipped = []
    errors = []
    if not project_file_path or not files:
        return copied, skipped, errors

    asset_path = get_assets_path(project_file_path, create_paths=True)
    if not asset_path:
        return copied, skipped, errors
    media_dir = os.path.join(asset_path, "media")
    try:
        os.makedirs(media_dir, exist_ok=True)
    except OSError as exc:
        errors.append(str(exc))
        return copied, skipped, errors

    id_to_new = {}
    src_to_new = {}

    for file in files:
        src = file.get("path") or ""
        if not src or "%" in src:
            skipped.append(src or "(empty)")
            continue
        if not os.path.isfile(src):
            skipped.append(src)
            continue
        abs_src = os.path.abspath(src)
        if path_is_under(abs_src, asset_path):
            skipped.append(abs_src)
            continue
        if app_root and path_is_under(abs_src, app_root):
            skipped.append(abs_src)
            continue

        dest = unique_media_dest(
            media_dir, os.path.basename(abs_src), file.get("id"), abs_src
        )

        if not os.path.isfile(dest):
            try:
                shutil.copy2(abs_src, dest)
            except Exception as exc:
                log.error("Collect media failed for %s: %s", abs_src, exc, exc_info=1)
                errors.append("%s: %s" % (abs_src, exc))
                continue
            copied.append(dest)
            log.info("Collected media %s -> %s", abs_src, dest)
        else:
            skipped.append(abs_src)

        if not file.get("original_path"):
            file["original_path"] = abs_src
        file["path"] = dest
        file_id = file.get("id")
        if file_id:
            id_to_new[file_id] = dest
        src_to_new[abs_src] = dest

    for clip in clips or []:
        reader = clip.get("reader")
        if not isinstance(reader, dict):
            continue
        file_id = clip.get("file_id")
        rpath = reader.get("path") or ""
        if file_id and file_id in id_to_new:
            reader["path"] = id_to_new[file_id]
        elif rpath and os.path.abspath(rpath) in src_to_new:
            reader["path"] = src_to_new[os.path.abspath(rpath)]

    return copied, skipped, errors


def reclaim_unused_asset_media(files, clips, project_file_path):
    """Delete ``_assets/media`` copies whose original still exists and matches.

    Never deletes a copy whose original is missing — that copy may be the only
    surviving version. Returns ``(removed, kept, errors)``.

    Collect + Reclaim is intentionally reversible: reclaim undoes collect when
    the external original still exists and is byte-identical.
    """
    removed = []
    kept = []
    errors = []
    if not project_file_path:
        return removed, kept, errors

    asset_path = get_assets_path(project_file_path, create_paths=False)
    if not asset_path:
        return removed, kept, errors
    media_dir = os.path.join(asset_path, "media")
    if not os.path.isdir(media_dir):
        return removed, kept, errors

    referenced = set()
    for file in files or []:
        path = file.get("path") or ""
        if path and path_is_under(path, media_dir):
            referenced.add(os.path.abspath(path))

    for name in os.listdir(media_dir):
        dest = os.path.abspath(os.path.join(media_dir, name))
        if not os.path.isfile(dest):
            continue
        if dest in referenced:
            kept.append(dest)
            continue
        # Look for an original that still exists and fingerprint-matches.
        # Without a recorded original path we cannot safely delete.
        kept.append(dest)

    id_to_original = {}
    src_to_original = {}

    # Safer reclaim: only remove copies that are still referenced AND whose
    # full-file hash matches a sibling original path stored as file["original_path"].
    for file in files or []:
        dest = file.get("path") or ""
        original = file.get("original_path") or ""
        if not dest or not original:
            continue
        if not path_is_under(dest, media_dir):
            continue
        if not os.path.isfile(dest) or not os.path.isfile(original):
            continue
        try:
            if files_identical(dest, original):
                os.remove(dest)
                file["path"] = original
                removed.append(dest)
                file_id = file.get("id")
                if file_id:
                    id_to_original[file_id] = original
                src_to_original[os.path.abspath(dest)] = original
                log.info("Reclaimed duplicate media %s (kept %s)", dest, original)
            else:
                kept.append(dest)
        except Exception as exc:
            errors.append("%s: %s" % (dest, exc))

    for clip in clips or []:
        reader = clip.get("reader")
        if not isinstance(reader, dict):
            continue
        file_id = clip.get("file_id")
        rpath = reader.get("path") or ""
        if file_id and file_id in id_to_original:
            reader["path"] = id_to_original[file_id]
        elif rpath and os.path.abspath(rpath) in src_to_original:
            reader["path"] = src_to_original[os.path.abspath(rpath)]

    return removed, kept, errors
