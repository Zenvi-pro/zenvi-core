"""Composer attachments for the Agents chat (Cursor-style file references)."""

from __future__ import annotations

import os
import uuid

from classes.image_types import is_audio_path, is_image

_IMAGE_EXTS = (
    ".bmp", ".gif", ".jpg", ".jpeg", ".png", ".svg", ".tif", ".tiff", ".webp",
)
_VIDEO_EXTS = (
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv",
)
_TEXT_EXTS = (
    ".txt", ".md", ".json", ".csv", ".srt", ".vtt", ".ass", ".log", ".xml",
    ".html", ".htm", ".py", ".js", ".css",
)


def kind_for_path(path: str) -> str:
    """Coarse kind used on chips and in the mention palette."""
    lower = (path or "").lower().split("?")[0]
    ext = os.path.splitext(lower)[1]
    if ext in _IMAGE_EXTS or is_image({"path": path or ""}):
        return "image"
    if is_audio_path(path):
        return "audio"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _VIDEO_EXTS:
        return "video"
    return "file"


def make_attachment(path: str, file_id: str = "", name: str = "") -> dict:
    abs_path = os.path.abspath(path) if path else ""
    return {
        "id": uuid.uuid4().hex[:12],
        "path": abs_path,
        "name": name or (os.path.basename(abs_path) if abs_path else "") or abs_path,
        "kind": kind_for_path(abs_path),
        "file_id": str(file_id or ""),
    }


def format_referenced_files_block(attachments) -> str:
    """Prompt block the model sees; not shown as-is in the chat bubble."""
    if not attachments:
        return ""
    lines = [
        "[Referenced files]",
        "The user attached these local files as references (like Cursor @file).",
        "They are not in the media bin unless media_bin_file_id is set.",
        "To edit them on the timeline, call import_files_tool with the path, then "
        "add_clip_to_timeline_tool with file_id= the returned media_bin_file_id.",
    ]
    for item in attachments:
        path = item.get("path") or ""
        name = item.get("name") or os.path.basename(path)
        kind = item.get("kind") or kind_for_path(path)
        line = f"- @{name} kind={kind}"
        if path:
            line += f" path={path}"
        file_id = item.get("file_id") or ""
        if file_id:
            line += f" media_bin_file_id={file_id}"
        lines.append(line)
    lines.append("[/Referenced files]")
    return "\n".join(lines)


def display_user_text(text: str, attachments) -> str:
    """What the chat bubble shows: the typed message plus @filenames."""
    typed = (text or "").strip()
    names = []
    for item in attachments or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        token = "@" + name
        if typed and token in typed:
            continue
        names.append(token)
    extra = " ".join(names)
    if typed and extra:
        return f"{typed} {extra}"
    return typed or extra
