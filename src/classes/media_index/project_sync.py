"""
 @file
 @brief Move the media index between the shelf and a project.

 Collect Media copies each file's index next to the project (``<Project>_assets/index/<sha>``),
 so the project works on another machine without re-indexing. Opening a project brings any
 such entries into the shelf (layers the shelf already has, or has newer, are left alone).

 Both run off the GUI thread: the callers are the Collect worker and a short daemon thread
 started after a project loads.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Iterable, List

from classes.logger import log
from classes.media_index.store import default_shelf, sha_of

INDEX_DIRNAME = "index"


def project_index_dir(project_file_path: str, *, create: bool = False) -> str:
    from classes.assets import get_assets_path

    if not project_file_path:
        return ""
    assets = get_assets_path(project_file_path, create_paths=create)
    return os.path.join(assets, INDEX_DIRNAME) if assets else ""


def export_index_for_files(files: Iterable[Any], project_file_path: str) -> List[str]:
    """Copy the shelf entries of *files* next to the project; returns the keys copied.

    Never raises: a failure here must not fail the media collect it rides along with.
    """
    try:
        keys = [sha_of(f.get("fingerprint")) for f in files or [] if isinstance(f, dict)]
        keys = sorted({k for k in keys if k})
        dest = project_index_dir(project_file_path, create=True)
        if not keys or not dest:
            return []
        return default_shelf().export_entries(keys, dest)
    except Exception:
        log.warning("Could not copy the media index into the project", exc_info=True)
        return []


def import_project_index(project_file_path: str) -> List[str]:
    """Bring a project's saved index entries into the shelf; returns the keys imported."""
    try:
        src = project_index_dir(project_file_path, create=False)
        if not src or not os.path.isdir(src):
            return []
        imported = default_shelf().import_entries(src)
        if imported:
            log.info("Media index: restored %d file(s) from the project", len(imported))
        return imported
    except Exception:
        log.warning("Could not read the media index from the project", exc_info=True)
        return []


def import_project_index_in_background(project_file_path: str) -> None:
    """Import on a daemon worker, so opening a project never waits on the disk."""
    if not project_file_path:
        return
    try:
        src = project_index_dir(project_file_path, create=False)
        if not src or not os.path.isdir(src):
            return  # nothing to do: no thread for the common case
    except Exception:
        return
    threading.Thread(
        target=import_project_index, args=(project_file_path,),
        name="media-index-import", daemon=True,
    ).start()
