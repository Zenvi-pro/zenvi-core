"""Headless title / lower-third creation (no TitleEditor dialog)."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from xml.dom import minidom


def _bundled_titles_dir() -> str:
    from classes import info
    return os.path.join(info.PATH, "titles")


def _default_template() -> str:
    titles = _bundled_titles_dir()
    for name in ("Bar_1.svg", "Standard_1.svg", "Footer_1.svg"):
        path = os.path.join(titles, name)
        if os.path.isfile(path):
            return path
    try:
        for entry in sorted(os.listdir(titles)):
            if entry.lower().endswith(".svg"):
                return os.path.join(titles, entry)
    except OSError:
        pass
    return ""


def _set_svg_text(xmldoc, text: str) -> None:
    """Replace the first text/tspan node contents with *text* (TitleEditor style)."""
    text_nodes = list(xmldoc.getElementsByTagName("text"))
    tspan_nodes = list(xmldoc.getElementsByTagName("tspan"))
    targets = tspan_nodes or text_nodes
    if not targets:
        return
    node = targets[0]
    # Clear existing children and set one text node.
    while node.firstChild:
        node.removeChild(node.firstChild)
    node.appendChild(xmldoc.createTextNode(text))
    # Blank remaining text nodes so template placeholders disappear.
    for extra in targets[1:]:
        while extra.firstChild:
            extra.removeChild(extra.firstChild)
        extra.appendChild(xmldoc.createTextNode(""))


def add_title(
    text: str = "",
    template: str = "",
    position_seconds="",
    track: str = "",
    duration_seconds="",
    file_name: str = "",
    **_kw,
) -> str:
    """Create an SVG title from a bundled template and place it on the timeline."""
    from classes import info
    from classes.agent_tools.receipt import ToolReceipt
    from classes.tool_handlers import (
        QThread,
        _get_app,
        _run_on_main_thread,
        add_clip_to_timeline,
        parse_seconds_arg,
    )

    body = str(text or "").strip()
    if not body:
        return ToolReceipt.refused(
            "add_title_tool",
            "Error: add_title_tool needs text.",
        ).to_json()

    tmpl = str(template or "").strip()
    if tmpl:
        if not tmpl.lower().endswith(".svg"):
            tmpl_path = os.path.join(_bundled_titles_dir(), f"{tmpl}.svg")
        elif os.path.isabs(tmpl):
            tmpl_path = tmpl
        else:
            tmpl_path = os.path.join(_bundled_titles_dir(), os.path.basename(tmpl))
    else:
        tmpl_path = _default_template()

    if not tmpl_path or not os.path.isfile(tmpl_path):
        return ToolReceipt.error(
            "add_title_tool",
            f"Error: title template not found ({tmpl or 'default'}).",
        ).to_json()

    try:
        pos = parse_seconds_arg(position_seconds, default=0.0, field="position_seconds")
        dur = parse_seconds_arg(duration_seconds, default=5.0, field="duration_seconds")
    except ValueError as exc:
        return ToolReceipt.refused("add_title_tool", f"Error: {exc}").to_json()
    if dur is None or dur <= 0:
        dur = 5.0

    safe_name = str(file_name or "").strip()
    if not safe_name:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", body)[:40].strip("_") or "title"
        safe_name = f"{slug}_{uuid.uuid4().hex[:8]}.svg"
    if not safe_name.lower().endswith(".svg"):
        safe_name += ".svg"

    os.makedirs(info.TITLE_PATH, exist_ok=True)
    dest = os.path.join(info.TITLE_PATH, safe_name)

    try:
        shutil.copyfile(tmpl_path, dest)
        xmldoc = minidom.parse(dest)
        _set_svg_text(xmldoc, body)
        with open(dest, "w", encoding="utf-8") as fh:
            xmldoc.writexml(fh)
    except Exception as exc:
        return ToolReceipt.error("add_title_tool", f"Error: failed to write title SVG: {exc}").to_json()

    app = _get_app()
    file_id_box = [None]
    error_box = [None]

    def _import():
        try:
            from classes.query import File
            existing = File.get(path=dest)
            if existing:
                file_id_box[0] = existing.id
                return
            win = app.window
            win.files_model.add_files([dest], quiet=True, prevent_image_seq=True)
            added = File.get(path=dest)
            file_id_box[0] = added.id if added else None
        except Exception as exc:
            error_box[0] = str(exc)

    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_import)
    else:
        _import()

    if error_box[0]:
        return ToolReceipt.error("add_title_tool", error_box[0]).to_json()
    if not file_id_box[0]:
        return ToolReceipt.error(
            "add_title_tool",
            f"Error: could not import title into media bin: {dest}",
        ).to_json()

    place = add_clip_to_timeline(
        file_id=file_id_box[0],
        position_seconds=str(pos),
        track=str(track or ""),
        duration_seconds=str(dur),
        full_file="true",
        **{k: v for k, v in _kw.items() if k in ("chat_session_id", "transaction_id")},
    )
    if isinstance(place, str) and place.startswith("Error"):
        return ToolReceipt.error("add_title_tool", place).to_json()

    return ToolReceipt.applied(
        "add_title_tool",
        f"Added title {body!r} from template {os.path.basename(tmpl_path)} "
        f"at {pos:.3f}s for {dur:.3f}s. {place}",
        data={
            "text": body,
            "path": dest,
            "file_id": file_id_box[0],
            "position_seconds": pos,
            "duration_seconds": dur,
            "template": os.path.basename(tmpl_path),
        },
        notes=[place] if place else [],
    ).to_json()
