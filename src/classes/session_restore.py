"""Session restore helpers: last project path, untitled backup, draft chat key.

Kept separate from MainWindow so unit tests can cover relaunch behavior
without importing the full Qt window graph.
"""

from __future__ import annotations

import os

from classes import info

# Auto-restore and recovery-zip skip the same cap: 209 MB recovered Marina
# files were usable; 1.4 GB+ hung. A legitimate huge project is File → Open.
AUTO_RESTORE_MAX_BYTES = 400 * 1024 * 1024


def normalize_restore_project_path(file_path) -> str:
    """Absolute path for a named project, or "" for untitled / backup."""
    if not file_path:
        return ""
    path = os.path.abspath(file_path)
    if os.path.abspath(info.BACKUP_FILE) == path:
        return ""
    return path


def normalize_draft_history_key(draft_key) -> str:
    """Only ``draft:…`` keys are valid for untitled chat restore."""
    key = (draft_key or "").strip()
    if key and not key.startswith("draft:"):
        return ""
    return key


def persist_setting(settings, key: str, value: str) -> bool:
    """Write *value* if it changed. Returns True when settings were saved."""
    if (settings.get(key) or "") == (value or ""):
        return False
    settings.set(key, value or "")
    settings.save()
    return True


def set_restore_project_path(settings, file_path) -> bool:
    return persist_setting(
        settings, "restore_project_path", normalize_restore_project_path(file_path)
    )


def set_restore_draft_history_key(settings, draft_key) -> bool:
    return persist_setting(
        settings, "restore_draft_history_key", normalize_draft_history_key(draft_key)
    )


def choose_restore_target(settings, backup_exists: bool):
    """Decide what recover_backup should open.

    Returns one of:
      ("untitled_backup", None)
      ("named", absolute_path)
      ("too_large", absolute_path)  — exists but over AUTO_RESTORE_MAX_BYTES
      ("blank", None)
      ("missing", absolute_path)  — remembered path is gone
    """
    if backup_exists:
        return ("untitled_backup", None)

    restore_path = settings.get("restore_project_path") or ""
    if restore_path and os.path.exists(restore_path):
        try:
            if os.path.getsize(restore_path) > AUTO_RESTORE_MAX_BYTES:
                return ("too_large", restore_path)
        except OSError:
            pass
        return ("named", restore_path)
    if restore_path:
        return ("missing", restore_path)
    return ("blank", None)


def should_flush_after_indexing(needs_save: bool, generation_in_progress: bool) -> bool:
    """True when indexing should overwrite the live .zvn / backup.zvn."""
    return bool(needs_save) and not bool(generation_in_progress)


def flush_target(current_filepath):
    """Where an in-place index flush should write.

    Returns (path, backup_only). Named projects overwrite the .zvn;
    untitled work writes BACKUP_FILE with backup_only=True (no Recovery zip).
    """
    if current_filepath:
        return (current_filepath, False)
    return (info.BACKUP_FILE, True)
