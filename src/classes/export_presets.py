"""Export presets (``src/presets/*.xml`` + the user's presets folder), read without Qt.

The Export dialog fills its Profile / Target / Quality combos from these files;
the agent export tools use the same records to render without a dialog. The
All Formats bits-per-pixel rule lives here too, so the dialog and the tools
compute the same bitrate for a size and frame rate.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional
from xml.parsers.expat import ExpatError

try:
    from defusedxml import minidom
except ImportError:
    from xml.dom import minidom

from classes.logger import log

EXPORT_TYPES = ("Video & Audio", "Video Only", "Audio Only", "Image Sequence")
QUALITIES = ("Low", "Med", "High")

# Bits per pixel per frame for "All Formats" bitrate-mode presets
# (Export.calculate_all_formats_bitrate): midpoints of the recommended ranges.
ALL_FORMATS_BPP = {"Low": 0.055, "Med": 0.08, "High": 0.12}


def all_formats_bitrate(width, height, fps, quality_key) -> Optional[str]:
    """Bitrate text ("3.32 Mb/s") for an All Formats preset at this size and fps."""
    bpp = ALL_FORMATS_BPP.get(quality_key)
    try:
        width, height, fps = float(width), float(height), float(fps)
    except (TypeError, ValueError):
        return None
    if bpp is None or not width or not height or not fps:
        return None
    return f"{width * height * fps * bpp / 1_000_000.0:.2f} Mb/s"


def is_quality_mode_rate(rate_text) -> bool:
    """True for crf / cqp / qp rates (a quality number, not bits per second)."""
    text = (rate_text or "").strip().lower()
    return (" crf" in text) or (" cqp" in text) or (" qp" in text)


def _text(doc, tag) -> str:
    nodes = doc.getElementsByTagName(tag)
    if not nodes or not nodes[0].childNodes:
        return ""
    return (nodes[0].childNodes[0].data or "").strip()


def _rates(doc, tag) -> Dict[str, str]:
    for node in doc.getElementsByTagName(tag):
        return {q: (node.getAttribute(q.lower()) or "").strip() for q in QUALITIES}
    return {q: "" for q in QUALITIES}


def parse_preset(path: str) -> Optional[dict]:
    """One preset file -> dict, or None when it is not a valid preset."""
    try:
        doc = minidom.parse(path)
    except (ExpatError, OSError) as exc:
        log.error("Failed to parse file '%s' as a preset: %s", path, exc)
        return None
    try:
        title = _text(doc, "title")
        if not title:
            return None
        export_to = _text(doc, "export-to") or EXPORT_TYPES[0]
        try:
            channels = int(_text(doc, "audiochannels") or 0)
        except ValueError:
            channels = 0
        try:
            layout = int(_text(doc, "audiochannellayout") or 3)
        except ValueError:
            layout = 3
        try:
            sample_rate = int(_text(doc, "samplerate") or 0)
        except ValueError:
            sample_rate = 0
        return {
            "title": title,
            "category": _text(doc, "type") or "All Formats",
            "file": os.path.basename(path),
            "path": path,
            "export_to": export_to if export_to in EXPORT_TYPES else EXPORT_TYPES[0],
            "vformat": _text(doc, "videoformat"),
            "vcodec": _text(doc, "videocodec"),
            "acodec": _text(doc, "audiocodec"),
            "channels": channels,
            "channel_layout": layout,
            "sample_rate": sample_rate,
            "video_bitrate": _rates(doc, "videobitrate"),
            "audio_bitrate": _rates(doc, "audiobitrate"),
            "profiles": [
                (n.childNodes[0].data or "").strip()
                for n in doc.getElementsByTagName("projectprofile") if n.childNodes
            ],
        }
    finally:
        doc.unlink()


def load_presets(folders: Optional[List[str]] = None) -> List[dict]:
    """Every preset in the built-in and user folders (user files may repeat a title)."""
    if folders is None:
        from classes import info
        folders = [info.EXPORT_PRESETS_PATH, info.USER_PRESETS_PATH]
    presets = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            preset = parse_preset(path)
            if preset:
                preset["user"] = folder != folders[0]
                presets.append(preset)
    return presets


def available_qualities(preset: dict) -> List[str]:
    """Qualities the dialog would list (a rate is defined for video or audio)."""
    return [q for q in QUALITIES
            if preset["video_bitrate"].get(q) or preset["audio_bitrate"].get(q)]
