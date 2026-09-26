"""Composer attachments for the Agents chat (Cursor-style file references)."""

from __future__ import annotations

import copy
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


def snapshot_attachments(attachments) -> list:
    return copy.deepcopy(list(attachments or []))


def append_path_attachment(attachments: list, path: str, file_id: str = "", name: str = ""):
    """Append a path attachment if new. Returns the item, or None if skipped."""
    if not path or not os.path.isfile(path):
        return None
    abs_path = os.path.abspath(path)
    for existing in attachments:
        if os.path.abspath(existing.get("path") or "") == abs_path:
            return None
    att = make_attachment(abs_path, file_id=file_id, name=name)
    attachments.append(att)
    return att


def attach_paths_batch(attachments: list, paths, file_id_for_path=None) -> tuple:
    """Attach many paths as one batch.

    Returns ``(snapshot_before, added_count)``. ``snapshot_before`` is None when
    nothing was added (caller should not push an undo step).
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return None, 0
    before = snapshot_attachments(attachments)
    added = 0
    for path in paths:
        file_id = ""
        if callable(file_id_for_path):
            try:
                file_id = file_id_for_path(path) or ""
            except Exception:
                file_id = ""
        if append_path_attachment(attachments, path, file_id=file_id) is not None:
            added += 1
    if added == 0:
        return None, 0
    return before, added


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


# Vision payload for hosted Zenvi assistant (Claude Code / Codex use paths only).
_CHAT_VISION_MAX_IMAGES = 4
_CHAT_VISION_LONG_EDGE = 1280
_CHAT_VISION_MAX_BYTES = 1_500_000
_CHAT_VISION_JPEG_QUALITY = 85


def _scale_pil(img, long_edge: int):
    w, h = img.size
    m = max(w, h)
    if m <= long_edge or m <= 0:
        return img
    scale = long_edge / float(m)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))))


def _encode_image_file(path: str, long_edge: int, max_bytes: int, quality: int):
    """Return (mime_type, base64_ascii) or None."""
    import base64
    from io import BytesIO

    try:
        from PIL import Image
    except ImportError:
        Image = None

    if Image is not None:
        try:
            with Image.open(path) as im:
                rgb = _scale_pil(im.convert("RGB"), long_edge)
                for q in (quality, 70, 55):
                    buf = BytesIO()
                    rgb.save(buf, format="JPEG", quality=q, optimize=True)
                    data = buf.getvalue()
                    if data and len(data) <= max_bytes:
                        return "image/jpeg", base64.b64encode(data).decode("ascii")
        except Exception:
            pass

    try:
        from PyQt5.QtGui import QImage
        from PyQt5.QtCore import QBuffer, QIODevice
    except Exception:
        QImage = None

    if QImage is not None:
        try:
            img = QImage(path)
            if img.isNull():
                return None
            w, h = img.width(), img.height()
            m = max(w, h)
            if m > long_edge > 0:
                scale = long_edge / float(m)
                img = img.scaled(max(1, int(w * scale)), max(1, int(h * scale)))
            if img.hasAlphaChannel():
                img = img.convertToFormat(QImage.Format_RGB32)
            for q in (quality, 70, 55):
                buf = QBuffer()
                buf.open(QIODevice.WriteOnly)
                if not img.save(buf, "JPEG", q):
                    continue
                data = bytes(buf.data())
                if data and len(data) <= max_bytes:
                    return "image/jpeg", base64.b64encode(data).decode("ascii")
        except Exception:
            pass

    try:
        size = os.path.getsize(path)
        if size <= 0 or size > max_bytes:
            return None
        ext = os.path.splitext(path)[1].lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext)
        if not mime:
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
        return mime, base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def encode_chat_images(
    attachments,
    *,
    max_images: int = _CHAT_VISION_MAX_IMAGES,
    long_edge: int = _CHAT_VISION_LONG_EDGE,
    max_bytes: int = _CHAT_VISION_MAX_BYTES,
) -> list:
    """Build ``images[]`` for the hosted Zenvi chat WebSocket (still images only)."""
    out = []
    for item in attachments or []:
        if (item.get("kind") or "") != "image":
            continue
        path = item.get("path") or ""
        if not path or not os.path.isfile(path):
            continue
        encoded = _encode_image_file(path, long_edge, max_bytes, _CHAT_VISION_JPEG_QUALITY)
        if not encoded:
            continue
        mime, b64 = encoded
        out.append({
            "name": item.get("name") or os.path.basename(path),
            "mime_type": mime,
            "image_base64": b64,
            "kind": "image",
            "source": "attachment",
            "file_id": str(item.get("file_id") or ""),
        })
        if len(out) >= max_images:
            break
    return out
