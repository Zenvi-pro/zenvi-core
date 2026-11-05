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
    copied, skipped, errors, moves = copy_media_into_project(files, project_file_path, app_root)
    repoint_media(files, clips, moves, keep_original=True)
    return copied, skipped, errors


def copy_media_into_project(files, project_file_path, app_root=None):
    """The file-copy half of collect; reads *files*, never changes them.

    Returns ``(copied, skipped, errors, moves)``; *moves* are
    ``(file_id, old_path, new_path)`` for :func:`repoint_media`.
    """
    copied = []
    skipped = []
    errors = []
    moves = []
    if not project_file_path or not files:
        return copied, skipped, errors, moves

    asset_path = get_assets_path(project_file_path, create_paths=True)
    if not asset_path:
        return copied, skipped, errors, moves
    media_dir = os.path.join(asset_path, "media")
    try:
        os.makedirs(media_dir, exist_ok=True)
    except OSError as exc:
        errors.append(str(exc))
        return copied, skipped, errors, moves

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
        moves.append((file.get("id"), abs_src, dest))

    # The media index goes with the media, so the collected project needs no re-indexing.
    from classes.media_index.project_sync import export_index_for_files
    export_index_for_files(files, project_file_path)

    return copied, skipped, errors, moves


def repoint_media(files, clips, moves, keep_original=False):
    """Point files and clip readers from each move's old path to its new one.

    Cheap and in-memory: the half of collect/reclaim that touches project data.
    With *keep_original* a file remembers the path it was collected from.
    """
    by_id = {fid: (old, new) for fid, old, new in moves if fid}
    by_path = {os.path.abspath(old): new for _fid, old, new in moves}
    for file in files or []:
        path = file.get("path") or ""
        hit = by_id.get(file.get("id"))
        new = hit[1] if hit else (by_path.get(os.path.abspath(path)) if path else None)
        if new is None:
            continue
        if keep_original and not file.get("original_path"):
            file["original_path"] = os.path.abspath(path)
        file["path"] = new
    for clip in clips or []:
        reader = clip.get("reader")
        if not isinstance(reader, dict):
            continue
        file_id = clip.get("file_id")
        rpath = reader.get("path") or ""
        if file_id and file_id in by_id:
            reader["path"] = by_id[file_id][1]
        elif rpath and os.path.abspath(rpath) in by_path:
            reader["path"] = by_path[os.path.abspath(rpath)]


def find_reclaimable_media(files, project_file_path):
    """Asset copies whose recorded original still exists byte-for-byte.

    The slow (full-file hash) half of reclaim; reads *files*, never changes
    them or the disk. Returns ``(moves, kept, errors)`` with moves
    ``(file_id, copy_path, original_path)``.
    """
    moves = []
    kept = []
    errors = []
    if not project_file_path:
        return moves, kept, errors

    asset_path = get_assets_path(project_file_path, create_paths=False)
    if not asset_path:
        return moves, kept, errors
    media_dir = os.path.join(asset_path, "media")
    if not os.path.isdir(media_dir):
        return moves, kept, errors

    for file in files or []:
        dest = file.get("path") or ""
        original = file.get("original_path") or ""
        if not dest or not original or not path_is_under(dest, media_dir):
            continue
        if not os.path.isfile(dest) or not os.path.isfile(original):
            # Without a surviving original the copy may be the only version.
            kept.append(os.path.abspath(dest))
            continue
        try:
            if files_identical(dest, original):
                moves.append((file.get("id"), os.path.abspath(dest), original))
            else:
                kept.append(os.path.abspath(dest))
        except Exception as exc:
            errors.append("%s: %s" % (dest, exc))
    return moves, kept, errors


def commit_reclaim(moves, repoint, save_project=None):
    """Point the project at the originals, save it, and only then delete the copies.

    *repoint(moves)* applies path moves to the live project. If *save_project*
    raises, the project is pointed back at the copies and nothing is deleted,
    so the saved project never references a removed file.
    Returns ``(removed, errors)``.
    """
    if not moves:
        return [], []
    repoint(moves)
    if save_project is not None:
        try:
            save_project()
        except Exception as exc:
            log.error("Reclaim: project save failed, keeping the copies", exc_info=1)
            repoint([(fid, original, dest) for fid, dest, original in moves])
            return [], ["Project not saved, nothing removed: %s" % exc]
    removed = []
    errors = []
    for _fid, dest, original in moves:
        try:
            os.remove(dest)
            removed.append(dest)
            log.info("Reclaimed duplicate media %s (kept %s)", dest, original)
        except OSError as exc:
            errors.append("%s: %s" % (dest, exc))
    return removed, errors


# DEAD CODE (PR #216 review): no app caller left (File > Reclaim and consolidate_project_media_tool
# use find_reclaimable_media + commit_reclaim directly); only tests/test_media_cache.py
# calls it. Delete it and point those tests at commit_reclaim.
def reclaim_unused_asset_media(files, clips, project_file_path, save_project=None):
    """Delete ``_assets/media`` copies whose original still exists and matches.

    Never deletes a copy whose original is missing — that copy may be the only
    surviving version. Returns ``(removed, kept, errors)``.

    Collect + Reclaim is intentionally reversible: reclaim undoes collect when
    the external original still exists and is byte-identical.
    """
    moves, kept, errors = find_reclaimable_media(files, project_file_path)
    removed, commit_errors = commit_reclaim(
        moves, lambda m: repoint_media(files, clips, m), save_project
    )
    if commit_errors and not removed:
        kept.extend(dest for _fid, dest, _orig in moves)
    return removed, kept, errors + commit_errors
