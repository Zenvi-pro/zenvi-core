"""Direct project fps / resolution / sample-rate settings for the agent."""

from __future__ import annotations

import os
from math import gcd


def _fps_label(num: int, den: int) -> str:
    rate = num / den
    return ("%.2f" % rate).rstrip("0").rstrip(".")


def _profile_fields(description, width, height, num, den, dar, par) -> dict:
    return {
        "profile": description,
        "width": width,
        "height": height,
        "fps": {"num": num, "den": den},
        "display_ratio": {"num": dar[0], "den": dar[1]},
        "pixel_ratio": {"num": par[0], "den": par[1]},
    }


def resolve_profile(width: int, height: int, num: int, den: int) -> dict:
    """Return the project fields of a named profile with exactly these values.

    Opening a project re-applies its *named* profile (ProjectDataStore.load ->
    get_profile -> apply_profile), so writing width/height/fps alone reverts
    on reopen. Like the Profile dialog, switch the project to a stock or user
    profile with these values; when none exists, save a custom user profile
    the way the profile editor does.
    """
    import openshot
    from classes import info

    fallback = None
    for folder in (info.USER_PROFILES_PATH, info.PROFILES_PATH):
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isdir(path):
                continue
            try:
                profile = openshot.Profile(path)
            except RuntimeError:
                continue
            pi = profile.info
            if (pi.width, pi.height, pi.fps.num, pi.fps.den) != (width, height, num, den):
                continue
            fields = _profile_fields(
                pi.description, width, height, num, den,
                (pi.display_ratio.num, pi.display_ratio.den),
                (pi.pixel_ratio.num, pi.pixel_ratio.den),
            )
            if pi.pixel_ratio.num == pi.pixel_ratio.den and not pi.interlaced_frame:
                return fields
            fallback = fallback or fields
    if fallback:
        return fallback

    div = gcd(width, height) or 1
    dar = (width // div, height // div)
    description = "Custom %dx%d %s fps" % (width, height, _fps_label(num, den))
    profile = openshot.Profile()
    pi = profile.info
    pi.description = description
    pi.width = width
    pi.height = height
    pi.fps.num = num
    pi.fps.den = den
    pi.pixel_ratio.num = 1
    pi.pixel_ratio.den = 1
    pi.display_ratio.num = dar[0]
    pi.display_ratio.den = dar[1]
    pi.interlaced_frame = False
    os.makedirs(info.USER_PROFILES_PATH, exist_ok=True)
    profile.Save(os.path.join(info.USER_PROFILES_PATH, profile.Key()))
    return _profile_fields(description, width, height, num, den, dar, (1, 1))


def set_project_setting(
    fps=None,
    fps_num=None,
    fps_den=None,
    width=None,
    height=None,
    sample_rate=None,
    **_kw,
) -> str:
    """Change project fps, width, height, and/or sample_rate.

    No-op when the requested values already match (status=unchanged, undoSteps=0).
    """
    from classes.agent_tools.receipt import ToolReceipt
    from classes.tool_handlers import QThread, _get_app, _run_on_main_thread

    app = _get_app()
    project = app.project

    updates: dict = {}
    notes: list[str] = []

    # FPS
    num = den = None
    if fps_num is not None or fps_den is not None:
        try:
            num = int(fps_num if fps_num is not None else 30)
            den = int(fps_den if fps_den is not None else 1)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: fps_num/fps_den must be integers.",
            ).to_json()
    elif fps is not None and str(fps).strip() != "":
        try:
            rate = float(fps)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_project_setting_tool",
                f"Error: fps must be a number (got {fps!r}).",
            ).to_json()
        if rate <= 0:
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: fps must be positive.",
            ).to_json()
        # Prefer exact NTSC-style fractions when close.
        if abs(rate - 23.976) < 0.01:
            num, den = 24000, 1001
        elif abs(rate - 29.97) < 0.01:
            num, den = 30000, 1001
        elif abs(rate - 59.94) < 0.01:
            num, den = 60000, 1001
        elif rate == int(rate):
            num, den = int(rate), 1
        else:
            from fractions import Fraction
            frac = Fraction(rate).limit_denominator(1001)
            num, den = frac.numerator, frac.denominator

    if num is not None and den is not None:
        if den <= 0 or num <= 0:
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: fps_num and fps_den must be positive.",
            ).to_json()
        cur = project.get("fps") or {}
        if int(cur.get("num") or 0) != num or int(cur.get("den") or 0) != den:
            updates["fps"] = {"num": num, "den": den}
            notes.append(f"fps={num}/{den}")

    if width is not None:
        try:
            w = int(width)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_project_setting_tool",
                f"Error: width must be an integer (got {width!r}).",
            ).to_json()
        if w < 16 or w > 8192:
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: width out of range (16..8192).",
            ).to_json()
        if int(project.get("width") or 0) != w:
            updates["width"] = w
            notes.append(f"width={w}")

    if height is not None:
        try:
            h = int(height)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_project_setting_tool",
                f"Error: height must be an integer (got {height!r}).",
            ).to_json()
        if h < 16 or h > 8192:
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: height out of range (16..8192).",
            ).to_json()
        if int(project.get("height") or 0) != h:
            updates["height"] = h
            notes.append(f"height={h}")

    if sample_rate is not None:
        try:
            sr = int(sample_rate)
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_project_setting_tool",
                f"Error: sample_rate must be an integer (got {sample_rate!r}).",
            ).to_json()
        if sr < 8000 or sr > 192000:
            return ToolReceipt.refused(
                "set_project_setting_tool",
                "Error: sample_rate out of range (8000..192000).",
            ).to_json()
        if int(project.get("sample_rate") or 0) != sr:
            updates["sample_rate"] = sr
            notes.append(f"sample_rate={sr}")

    if not updates:
        return ToolReceipt.unchanged(
            "set_project_setting_tool",
            "Settings already matched.",
            data={"changed": False},
        ).to_json()

    if {"fps", "width", "height"} & set(updates):
        target_fps = updates.get("fps") or project.get("fps") or {}
        try:
            fields = resolve_profile(
                int(updates.get("width", project.get("width"))),
                int(updates.get("height", project.get("height"))),
                int(target_fps.get("num")),
                int(target_fps.get("den")),
            )
        except Exception as exc:
            # Writing the raw values would look applied and revert on reopen.
            return ToolReceipt.error(
                "set_project_setting_tool",
                f"Error: could not find or create a project profile for these settings: {exc}",
            ).to_json()
        for key in ("width", "height", "fps"):
            updates.pop(key, None)
        profile_updates = dict(fields)
        profile_updates.update(updates)
        updates = profile_updates
        if fields["profile"] != project.get("profile"):
            # Export settings are profile-dependent (same reset as the Profile dialog).
            updates["export_settings"] = None
        notes.append(f"profile={fields['profile']}")
    caveats = []
    if "sample_rate" in updates:
        caveats.append(
            "sample_rate applies to this session; reopening a project uses the "
            "Preferences default sample rate"
        )

    error_box = [None]

    def _do():
        try:
            for key, value in updates.items():
                app.updates.update([key], value)
        except Exception as exc:
            error_box[0] = str(exc)

    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_do)
    else:
        _do()

    if error_box[0]:
        return ToolReceipt.error("set_project_setting_tool", error_box[0]).to_json()

    return ToolReceipt.applied(
        "set_project_setting_tool",
        "Updated project settings: " + ", ".join(notes) + ".",
        data={"changed": True, "updates": updates},
        notes=notes + caveats,
    ).to_json()
