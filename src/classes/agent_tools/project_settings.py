"""Direct project fps / resolution / sample-rate settings for the agent."""

from __future__ import annotations


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
        notes=notes,
    ).to_json()
