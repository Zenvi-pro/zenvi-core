"""
 @file
 @brief From a located object to the arguments of the masking tools (SAM2 blur, highlight and mask).

 ``locate_in_footage_tool`` finds where something is as a rough box in 0-1 fractions. The object-tracking templates want
 source pixels, a 1-based seed frame and points or boxes on the object. This does that conversion, so "blur the licence
 plate" is locate, then one call.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

ROUGH_PAD = 0.05             # the index's boxes are rough: grow each side by this share of the box so the object is not clipped
MAX_HANDOFFS = 5
ACTIONS = ("blur_object", "highlight_object", "mask_object")
TOOL = "enhance_file_with_comfyui_tool"


def mask_handoff(hit: Dict[str, Any], width: int, height: int, fps: float, frames: Optional[int] = None, action: str = "") -> Optional[Dict[str, Any]]:
    """The call to make for one located hit, or None when the hit has no box or the frame size is unknown.

    ``args`` is ready for the masking tool: a padded pixel box and a centre point (both on the seed frame), the seed frame (1-based,
    the frame at the hit's time) and the label as a prompt. ``action`` is added when given.
    """
    box = hit.get("box")
    if not box or len(box) != 4 or width <= 0 or height <= 0:
        return None
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    pad_x, pad_y = w * ROUGH_PAD, h * ROUGH_PAD
    x1 = int(round(max(0.0, x - pad_x) * width))
    y1 = int(round(max(0.0, y - pad_y) * height))
    x2 = int(round(min(1.0, x + w + pad_x) * width))
    y2 = int(round(min(1.0, y + h + pad_y) * height))
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    cx = min(width - 1, max(0, int(round((x + w / 2.0) * width))))
    cy = min(height - 1, max(0, int(round((y + h / 2.0) * height))))
    seed = max(1, int(round(float(hit.get("t") or 0.0) * fps)) + 1) if fps > 0 else 1
    if frames:
        seed = min(seed, int(frames))
    args: Dict[str, Any] = {"file_id": str(hit.get("file_id") or ""), "boxes": [{"x1": x1, "y1": y1, "x2": x2, "y2": y2}], "points": [{"x": cx, "y": cy}],
                            "seed_frame": seed, "prompt": str(hit.get("label") or "")}
    if action:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        args["action"] = action
    return {"tool": TOOL, "args": args, "frame_size": [int(width), int(height)], "box_is": f"rough, grown {ROUGH_PAD:.0%} per side; check it on a frame first"}


BODY_WIDTH_FACES = 2.6       # a standing or sitting person is about this many face-widths across at the shoulders
BODY_ABOVE_FACES = 0.4       # the top of the head is a little above the face box
BODY_HEIGHT_FACES = 6.0      # from the head down to about mid-thigh


def body_box_from_face(box: Sequence[float]) -> Optional[list]:
    """A rough box around the whole person from a face box ([x, y, w, h] as 0-1 fractions), kept inside the frame.

    It is an estimate from proportions (a face is not a body): good for seeding a mask, to be checked on a frame first.
    """
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    cx = x + w / 2.0
    bw, top = w * BODY_WIDTH_FACES, y - h * BODY_ABOVE_FACES
    x1, x2 = max(0.0, cx - bw / 2.0), min(1.0, cx + bw / 2.0)
    y1, y2 = max(0.0, top), min(1.0, top + h * BODY_HEIGHT_FACES)
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return [round(x1, 4), round(y1, 4), round(x2 - x1, 4), round(y2 - y1, 4)]
