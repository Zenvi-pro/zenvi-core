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


def _node_text(node) -> str:
    return "".join(c.data for c in node.childNodes if c.nodeType == c.TEXT_NODE)


def _set_svg_text(xmldoc, text: str) -> None:
    """Put *text* in the template's title line (TitleEditor style).

    Reflection templates (Bar_1, the default, plus Oval_4, Smoke_3 and
    Standard_2) carry the title twice: a mirrored copy under scale(1,-1) first,
    then the visible line, both with the same placeholder. Every node holding the
    first node's placeholder gets the text; any other placeholder is blanked.
    """
    text_nodes = list(xmldoc.getElementsByTagName("text"))
    tspan_nodes = list(xmldoc.getElementsByTagName("tspan"))
    targets = tspan_nodes or text_nodes
    if not targets:
        return
    placeholder = _node_text(targets[0]).strip()
    for node in targets:
        same_line = node is targets[0] or (
            placeholder and _node_text(node).strip() == placeholder)
        while node.firstChild:
            node.removeChild(node.firstChild)
        node.appendChild(xmldoc.createTextNode(text if same_line else ""))


# Bold sans glyphs average ~0.6 em; a little more so wide words still fit.
_CAPTION_EM_PER_CHAR = 0.66
_CAPTION_WIDTH_FRACTION = 0.83   # the default caption bar spans ~83% of the frame
_FONT_SIZE_RE = re.compile(r"font-size:\s*([0-9.]+)px")


def _fit_text_to_width(xmldoc, text: str) -> None:
    """Shrink the title line's font so *text* fits the caption bar.

    Templates size their line for a short title; a caption cue can be several
    times longer and ran off both ends of the bar. Only ever shrinks.
    """
    svg = xmldoc.documentElement
    try:
        canvas = float(re.sub(r"[^0-9.]", "", svg.getAttribute("width") or "") or 1920)
    except ValueError:
        canvas = 1920.0
    max_width = canvas * _CAPTION_WIDTH_FRACTION
    length = max(1, len(text))
    for node in list(xmldoc.getElementsByTagName("tspan")) or list(xmldoc.getElementsByTagName("text")):
        style = node.getAttribute("style") or ""
        match = _FONT_SIZE_RE.search(style)
        if not match:
            continue
        size = float(match.group(1))
        fitted = min(size, max_width / (length * _CAPTION_EM_PER_CHAR))
        if fitted < size:
            node.setAttribute("style", _FONT_SIZE_RE.sub("font-size:%.2fpx" % fitted, style, count=1))


def _rasterize_svg(svg_path: str, png_path: str, width: int, height: int) -> None:
    """Render a title SVG to a transparent PNG (call on the GUI thread).

    libopenshot renders SVG titles on its own, non-Qt threads. The first time
    one of those threads lays out a new font, Qt warns from inside its font
    database lock, PyQt's Python message handler then waits for the GIL, and
    a GUI thread holding the GIL that needs a font deadlocks the app. A PNG
    needs no fonts when libopenshot draws it.
    """
    from qt_api import QImage, QPainter, QSvgRenderer, Qt

    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        raise ValueError(f"invalid title SVG: {svg_path}")
    image = QImage(int(width), int(height), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    if not image.save(png_path, "PNG"):
        raise OSError(f"could not write {png_path}")


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
        if _kw.get("raster"):
            _fit_text_to_width(xmldoc, body)
        with open(dest, "w", encoding="utf-8") as fh:
            xmldoc.writexml(fh)
    except Exception as exc:
        return ToolReceipt.error("add_title_tool", f"Error: failed to write title SVG: {exc}").to_json()

    app = _get_app()
    file_id_box = [None]
    error_box = [None]
    # Internal (add_captions): place a PNG render instead of the SVG, so
    # libopenshot never lays out caption text on its own threads.
    raster = bool(_kw.get("raster"))
    media_path = os.path.splitext(dest)[0] + ".png" if raster else dest

    def _import():
        try:
            from classes.query import File
            existing = File.get(path=media_path)
            if existing:
                file_id_box[0] = existing.id
                return
            if raster:
                project = app.project
                _rasterize_svg(
                    dest, media_path,
                    int(project.get("width") or 1920), int(project.get("height") or 1080),
                )
                os.remove(dest)
            win = app.window
            # Generated text: nothing for cloud indexing to learn, and it bills.
            win.files_model.add_files(
                [media_path], quiet=True, prevent_image_seq=True, skip_indexing=True,
            )
            added = File.get(path=media_path)
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
            f"Error: could not import title into media bin: {media_path}",
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
            "path": media_path,
            "file_id": file_id_box[0],
            "position_seconds": pos,
            "duration_seconds": dur,
            "template": os.path.basename(tmpl_path),
        },
        notes=[place] if place else [],
    ).to_json()
