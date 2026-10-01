"""Video profiles (``src/profiles`` + the user's profiles folder) and switching the project to one.

Profile files are MLT-style ``key=value`` text, so they are read here without
libopenshot: the agent tools list and match them off the GUI thread, and the
Choose Profile dialog and the tools apply a profile through the same
:func:`apply_profile_values` (one undo step, joining the caller's transaction).
"""

from __future__ import annotations

import os
from fractions import Fraction
from math import gcd
from typing import Dict, List, Optional, Tuple

from classes.logger import log

# fps values people say -> the exact fractions the built-in profiles use.
_NTSC = {23.976: (24000, 1001), 23.98: (24000, 1001), 29.97: (30000, 1001),
         47.95: (48000, 1001), 59.94: (60000, 1001), 119.88: (120000, 1001)}


def fps_fraction(fps) -> Tuple[int, int]:
    """24 -> (24, 1); 29.97 -> (30000, 1001); "25/1" -> (25, 1)."""
    if isinstance(fps, str) and "/" in fps:
        num, den = fps.split("/", 1)
        return int(num), int(den)
    rate = float(fps)
    if rate <= 0:
        raise ValueError("fps must be positive")
    for approx, frac in _NTSC.items():
        if abs(rate - approx) < 0.011:
            return frac
    if abs(rate - round(rate)) < 1e-6:
        return int(round(rate)), 1
    frac = Fraction(rate).limit_denominator(1001)
    return frac.numerator, frac.denominator


def aspect_text(width: int, height: int) -> str:
    g = gcd(int(width), int(height)) or 1
    return f"{int(width) // g}:{int(height) // g}"


def orientation_of(width, height) -> str:
    width, height = int(width or 0), int(height or 0)
    if width == height:
        return "square"
    return "portrait" if height > width else "landscape"


def read_profile_file(path: str) -> Optional[dict]:
    """Parse one profile file; None when it is not a profile."""
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    except OSError:
        return None
    try:
        width, height = int(values["width"]), int(values["height"])
        fps_num, fps_den = int(values["frame_rate_num"]), int(values["frame_rate_den"] or 1)
        description = values["description"]
    except (KeyError, ValueError):
        return None
    if width <= 0 or height <= 0 or fps_num <= 0 or fps_den <= 0 or not description:
        return None
    sar_num = int(values.get("sample_aspect_num") or 1)
    sar_den = int(values.get("sample_aspect_den") or 1)
    dar_num = int(values.get("display_aspect_num") or width * sar_num)
    dar_den = int(values.get("display_aspect_den") or height * sar_den)
    return {
        "description": description,
        "key": os.path.basename(path),
        "path": path,
        "width": width,
        "height": height,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "display_ratio": {"num": dar_num, "den": dar_den},
        "pixel_ratio": {"num": sar_num, "den": sar_den},
        "interlaced": values.get("progressive", "1") == "0",
        "spherical": values.get("spherical", "0") == "1",
    }


_CACHE: dict = {"signature": None, "profiles": []}


def _folders() -> List[Tuple[str, bool]]:
    from classes import info
    return [(info.USER_PROFILES_PATH, True), (info.PROFILES_PATH, False)]


def profile_catalog(folders: Optional[List[Tuple[str, bool]]] = None) -> List[dict]:
    """User profiles, then built-in ones, in the order ``ProjectDataStore.get_profile`` searches.

    Cached on the folders' modification times (425 files; call it off the GUI thread).
    """
    folders = folders if folders is not None else _folders()
    signature = []
    for folder, _user in folders:
        try:
            signature.append((folder, os.stat(folder).st_mtime_ns))
        except OSError:
            signature.append((folder, None))
    if _CACHE["signature"] == signature:
        return list(_CACHE["profiles"])
    profiles = []
    for folder, user in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in reversed(sorted(os.listdir(folder))):
            path = os.path.join(folder, name)
            if os.path.isdir(path):
                continue
            record = read_profile_file(path)
            if record is None:
                log.debug("Skipping unreadable profile %s", path)
                continue
            record["user"] = user
            profiles.append(record)
    _CACHE.update(signature=signature, profiles=profiles)
    return list(profiles)


def invalidate_catalog() -> None:
    _CACHE.update(signature=None, profiles=[])


def describe_profile(record: dict) -> dict:
    """Compact receipt form of a profile record."""
    fps = record["fps_num"] / float(record["fps_den"])
    return {
        "name": record["description"],
        "key": record["key"],
        "width": record["width"],
        "height": record["height"],
        "fps": round(fps, 3),
        "fps_num": record["fps_num"],
        "fps_den": record["fps_den"],
        "aspect": aspect_text(record["display_ratio"]["num"], record["display_ratio"]["den"]),
        "orientation": orientation_of(record["width"], record["height"]),
        "interlaced": record["interlaced"],
        "spherical": record["spherical"],
        "user": bool(record.get("user")),
    }


def project_profile_values(project) -> dict:
    """The project's current profile keys, shaped like a profile record."""
    fps = project.get("fps") or {"num": 30, "den": 1}
    return {
        "description": project.get("profile") or "",
        "width": int(project.get("width") or 0),
        "height": int(project.get("height") or 0),
        "fps_num": int(fps.get("num") or 30),
        "fps_den": int(fps.get("den") or 1),
        "display_ratio": dict(project.get("display_ratio") or {"num": 16, "den": 9}),
        "pixel_ratio": dict(project.get("pixel_ratio") or {"num": 1, "den": 1}),
    }


def profile_differs(project, record: dict) -> bool:
    cur = project_profile_values(project)
    return any([
        cur["description"] != record["description"],
        cur["width"] != record["width"],
        cur["height"] != record["height"],
        cur["fps_num"] != record["fps_num"],
        cur["fps_den"] != record["fps_den"],
        cur["display_ratio"] != record["display_ratio"],
        cur["pixel_ratio"] != record["pixel_ratio"],
    ])


def record_from_openshot_profile(profile) -> dict:
    """The Choose Profile dialog hands over an openshot.Profile; turn it into a record."""
    i = profile.info
    return {
        "description": i.description,
        "key": getattr(profile, "path", "") and os.path.basename(profile.path) or profile.Key(),
        "width": int(i.width),
        "height": int(i.height),
        "fps_num": int(i.fps.num),
        "fps_den": int(i.fps.den),
        "display_ratio": {"num": int(i.display_ratio.num), "den": int(i.display_ratio.den)},
        "pixel_ratio": {"num": int(i.pixel_ratio.num), "den": int(i.pixel_ratio.den)},
        "interlaced": bool(i.interlaced_frame),
        "spherical": bool(getattr(i, "spherical", False)),
    }


def apply_profile_values(record: dict, updates=None, project=None) -> bool:
    """Switch the project to *record* (Choose Profile dialog core). Returns whether it changed.

    One undo step: joins the caller's transaction (or opens one). Export dialog
    settings depend on the profile, so they are reset when it changes -- the
    same rule the dialog applied.
    """
    from classes.app import get_app
    from classes.updates import nested_transaction

    app = get_app()
    updates = updates or app.updates
    project = project or app.project
    changed = profile_differs(project, record)
    with nested_transaction(updates):
        updates.update(["profile"], record["description"])
        updates.update(["width"], record["width"])
        updates.update(["height"], record["height"])
        updates.update(["display_ratio"], dict(record["display_ratio"]))
        updates.update(["pixel_ratio"], dict(record["pixel_ratio"]))
        updates.update(["fps"], {"num": record["fps_num"], "den": record["fps_den"]})
        if changed:
            updates.update(["export_settings"], None)
    return changed


def write_user_profile(record: dict, folder: str) -> str:
    """Save a user profile file (the profile editor's format); returns its path."""
    os.makedirs(folder, exist_ok=True)
    path = record.get("path") or os.path.join(folder, profile_key(record))
    lines = [
        f"description={record['description']}",
        f"frame_rate_num={record['fps_num']}",
        f"frame_rate_den={record['fps_den']}",
        f"width={record['width']}",
        f"height={record['height']}",
        f"progressive={0 if record.get('interlaced') else 1}",
        f"sample_aspect_num={record['pixel_ratio']['num']}",
        f"sample_aspect_den={record['pixel_ratio']['den']}",
        f"display_aspect_num={record['display_ratio']['num']}",
        f"display_aspect_den={record['display_ratio']['den']}",
        f"spherical={1 if record.get('spherical') else 0}",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    invalidate_catalog()
    return path


def profile_key(record: dict) -> str:
    """libopenshot's Profile::Key() format: 01080x1920p0030_09-16."""
    fps = record["fps_num"] / float(record["fps_den"])
    fps_code = f"{int(round(fps * 100)):04d}" if abs(fps - round(fps)) > 1e-6 else f"{int(round(fps)):04d}"
    dar = record["display_ratio"]
    return "%05dx%04d%s%s_%02d-%02d%s" % (
        record["width"], record["height"], "i" if record.get("interlaced") else "p", fps_code,
        dar["num"], dar["den"], "_360" if record.get("spherical") else "")


def reduced_display_ratio(width: int, height: int, pixel_num: int = 1, pixel_den: int = 1) -> dict:
    num, den = int(width) * int(pixel_num), int(height) * int(pixel_den)
    g = gcd(num, den) or 1
    return {"num": num // g, "den": den // g}
