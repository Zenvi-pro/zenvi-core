"""
 @file
 @brief Helpers for resolving media paths to absolute/relative forms
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
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

from classes import info
from classes.app import get_app
from classes.assets import get_assets_path


def _project_file_path(project_file=None):
    """Return the active project file path (if any)."""
    if project_file:
        return project_file
    app = get_app()
    if app and hasattr(app, "project"):
        return getattr(app.project, "current_filepath", None)
    return None


def _project_folder(project_file=None):
    project_file = _project_file_path(project_file)
    if project_file:
        return os.path.normpath(os.path.expanduser(os.path.dirname(project_file)))
    return os.path.normpath(os.path.expanduser(info.HOME_PATH))


def _token_suffix(path_value):
    parts = path_value.split("/", 1)
    if len(parts) == 2:
        return parts[1]
    return ""


def absolute_media_path(path_value, project_file=None):
    """Resolve OpenShot-specific tokens and relative paths into absolute paths."""
    if not path_value:
        return ""

    normalized = path_value.replace("\\", "/")

    # Repair legacy paths like ~/~/.openshot_qt/... from tilde + relative join.
    while normalized.startswith("~/~/"):
        normalized = normalized[2:]

    if normalized.startswith("@emojis"):
        suffix = _token_suffix(normalized)
        return os.path.normpath(os.path.join(info.PATH, "emojis", "color", "svg", suffix))

    if normalized.startswith("@transitions"):
        suffix = _token_suffix(normalized)
        return os.path.normpath(os.path.join(info.PATH, "transitions", suffix))

    if normalized.startswith("@colors"):
        suffix = _token_suffix(normalized)
        return os.path.normpath(os.path.join(info.COLORS_PATH, suffix))

    if normalized.startswith("@assets"):
        project_file = _project_file_path(project_file)
        assets_root = get_assets_path(project_file, create_paths=False)
        suffix = _token_suffix(normalized)
        return os.path.normpath(os.path.join(assets_root, suffix))

    if normalized.startswith("thumbnail/"):
        project_file = _project_file_path(project_file)
        assets_root = get_assets_path(project_file, create_paths=False)
        return os.path.normpath(os.path.join(assets_root, normalized.replace("thumbnail/", "thumbnail" + os.sep)))

    if normalized.startswith("~/") or normalized == "~":
        return os.path.normpath(os.path.expanduser(normalized))

    if os.path.isabs(normalized):
        return os.path.normpath(normalized)

    base_folder = _project_folder(project_file)
    return os.path.normpath(os.path.join(base_folder, normalized))


def _media_roots():
    """Remembered folders used to silently relink missing media."""
    try:
        app = get_app()
        if app:
            settings = app.get_settings()
            roots = settings.get("media-roots") if settings else None
            if isinstance(roots, list):
                return [r for r in roots if isinstance(r, str) and r]
    except Exception:
        pass
    return []


def remember_media_root(folder):
    """Persist *folder* in settings so later opens can relink silently."""
    if not folder or not os.path.isdir(folder):
        return
    try:
        app = get_app()
        if not app:
            return
        settings = app.get_settings()
        if not settings:
            return
        roots = settings.get("media-roots") or []
        if not isinstance(roots, list):
            roots = []
        abs_folder = os.path.abspath(folder)
        if abs_folder not in roots:
            roots = [abs_folder] + [r for r in roots if r != abs_folder]
            settings.set("media-roots", roots[:20])
            settings.save()
    except Exception:
        pass


def resolve_media_path(path_value, fingerprint=None, project_file=None, fingerprint_index=None):
    """Resolve a media path, falling back to remembered roots and fingerprints.

    Order: existing absolute/relative path, basename under media roots
    (validated against fingerprint when available), then fingerprint match
    against *fingerprint_index* ``{sha256: path}``.
    """
    resolved = absolute_media_path(path_value, project_file=project_file)
    if resolved and os.path.exists(resolved):
        return resolved

    fp_digest = None
    if isinstance(fingerprint, dict):
        fp_digest = fingerprint.get("sha256") or None

    def _candidate_matches(candidate):
        if not candidate or not os.path.isfile(candidate):
            return False
        if not fp_digest:
            return True
        from classes.media_fingerprint import fingerprint as fingerprint_file
        cand_fp = fingerprint_file(candidate)
        return bool(cand_fp and cand_fp.get("sha256") == fp_digest)

    basename = os.path.basename(path_value or "")
    if basename and "%" not in basename:
        for root in _media_roots():
            candidate = os.path.join(root, basename)
            if _candidate_matches(candidate):
                return os.path.normpath(candidate)
            # Also search one level of subdirs for common layouts.
            try:
                for name in os.listdir(root):
                    sub = os.path.join(root, name, basename)
                    if _candidate_matches(sub):
                        return os.path.normpath(sub)
            except OSError:
                continue

    if fp_digest and fingerprint_index:
        hit = fingerprint_index.get(fp_digest)
        if hit and os.path.isfile(hit):
            return os.path.normpath(hit)

    return resolved


def relative_export_path(abs_path, export_folder):
    """Return path relative to export folder when possible."""
    if not abs_path:
        return ""
    try:
        abs_norm = os.path.normpath(abs_path)
        if not export_folder:
            return abs_norm.replace("\\", "/")
        export_norm = os.path.normpath(export_folder)
        if os.name == "nt":
            src_drive = os.path.splitdrive(abs_norm)[0].lower()
            dst_drive = os.path.splitdrive(export_norm)[0].lower()
            if src_drive and dst_drive and src_drive != dst_drive:
                return abs_norm.replace("\\", "/")
        rel_path = os.path.relpath(abs_norm, export_norm)
        return rel_path.replace("\\", "/")
    except Exception:
        return abs_path.replace("\\", "/")


def absolute_path_from_export(path_value, base_folder, project_file=None):
    """Resolve a relative path stored in an export back into an absolute path."""
    if not path_value:
        return ""

    normalized = path_value.replace("\\", "/")

    if normalized.startswith("@"):
        return absolute_media_path(normalized, project_file)

    if os.path.isabs(normalized):
        return os.path.normpath(normalized)

    if not base_folder:
        base_folder = _project_folder(project_file)

    return os.path.normpath(os.path.join(base_folder, normalized))


def normalize_path(path_value):
    """Return a path string with POSIX separators (useful for XML)."""
    if not path_value:
        return ""
    return path_value.replace("\\", "/")
