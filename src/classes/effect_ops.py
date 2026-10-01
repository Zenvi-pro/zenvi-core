"""
 @file
 @brief Clip effect helpers shared by the timeline UI and the agent effect tools

 * create_effect_json -- a fresh libopenshot effect as project JSON (new id,
   badge color), the way dropping an effect on a clip creates one.
 * merge_effects_by_class -- Paste of copied effects onto a clip: an effect of
   the same class is overwritten, anything else is appended with a new id.
 * list_luts / resolve_lut -- the LUT files offered for ``lut_path`` (built-in
   ``src/colors`` categories plus the user LUT folder). These walk folders, so
   call them off the Qt GUI thread.
"""

import copy
import json
import os

from classes import info
from classes.logger import log


def _new_id():
    from classes.app import get_app
    return get_app().project.generate_id()


def _badge_color(effect_json):
    try:
        from windows.views.timeline_backend.colors import effect_color_hex
        return effect_color_hex(effect_json)
    except Exception:  # cosmetic only: the timeline falls back to its own palette
        log.debug("effect badge color unavailable", exc_info=True)
        return None


def create_effect_json(class_name):
    """A new *class_name* effect as project JSON with a fresh id.

    Raises RuntimeError when this libopenshot build cannot create the effect.
    """
    import openshot

    effect = openshot.EffectInfo().CreateEffect(class_name)
    if effect is None:
        raise RuntimeError("Unable to create {} effect".format(class_name))
    effect.Id(_new_id())
    effect_json = json.loads(effect.Json())
    color = _badge_color(effect_json)
    if color:
        effect_json.setdefault("ui", {}).setdefault("icon_color", color)
    return effect_json


def assign_new_effect_ids(effects):
    """Give every effect dict in *effects* a new unique id (in place)."""
    for effect in effects or []:
        if isinstance(effect, dict):
            effect["id"] = _new_id()


def merge_effects_by_class(existing_effects, copied_effects):
    """Paste *copied_effects* onto a clip's *existing_effects* (Copy > Effects, Paste).

    Each copied effect gets a new id. When the clip already has an effect of the
    same ``class_name`` that effect is overwritten with the copy; otherwise the
    copy is appended. Returns the new list; the inputs are not modified.
    """
    merged = [copy.deepcopy(e) for e in (existing_effects or [])]
    effect_map = {
        effect.get("class_name"): effect
        for effect in merged
        if isinstance(effect, dict) and effect.get("class_name")
    }
    for effect in copied_effects or []:
        if not isinstance(effect, dict):
            continue
        effect_copy = copy.deepcopy(effect)
        assign_new_effect_ids([effect_copy])
        effect_type = effect_copy.get("class_name")
        if effect_type in effect_map:
            effect_map[effect_type].update(effect_copy)
        else:
            merged.append(effect_copy)
    return merged


# ---------------------------------------------------------------------------
# LUTs
# ---------------------------------------------------------------------------

def _pretty(name):
    return os.path.splitext(name)[0].replace("_", " ").title()


def _gather_luts(root, builtin):
    out = []
    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return out
    for name in names:
        full = os.path.join(root, name)
        if os.path.isdir(full):
            try:
                files = sorted(os.listdir(full), key=str.lower)
            except OSError:
                continue
            for fn in files:
                if fn.lower().endswith(".cube"):
                    out.append({"name": _pretty(fn), "id": "%s/%s" % (name, os.path.splitext(fn)[0]),
                                "category": _pretty(name), "path": os.path.join(full, fn),
                                "builtin": builtin})
        elif name.lower().endswith(".cube"):
            out.append({"name": _pretty(name), "id": os.path.splitext(name)[0],
                        "category": "User-Defined" if not builtin else "",
                        "path": full, "builtin": builtin})
    return out


def list_luts():
    """Every LUT the lut_path dropdown offers: user folder first, then built-in categories."""
    return _gather_luts(info.USER_COLORS_PATH, False) + _gather_luts(info.COLORS_PATH, True)


def _norm(text):
    return "".join(ch for ch in str(text).lower().replace("&", "and") if ch.isalnum())


def resolve_lut(value, luts=None):
    """A LUT name, id ('category/file'), file name or path -> absolute .cube path.

    Returns "" for "", "none" or "off". Raises ValueError (with suggestions)
    when nothing matches or a given path is not a .cube file.
    """
    text = str(value or "").strip()
    if text.lower() in ("", "none", "off", "no"):
        return ""
    if text.startswith("@colors/"):
        text = os.path.join(info.COLORS_PATH, text[len("@colors/"):])
    if os.path.isabs(text) or (os.sep in text and os.path.exists(text)):
        if not text.lower().endswith(".cube") or not os.path.isfile(text):
            raise ValueError("LUT file not found or not a .cube file: {}".format(text))
        return text
    luts = list_luts() if luts is None else luts
    key = _norm(text[:-5] if text.lower().endswith(".cube") else text)
    for field in ("id", "name"):
        for lut in luts:
            if _norm(lut[field]) == key or _norm(os.path.basename(lut["path"])[:-5]) == key:
                return lut["path"]
    partial = [lut for lut in luts if key and key in _norm(lut["id"])]
    if len(partial) == 1:
        return partial[0]["path"]
    import difflib
    names = [lut["id"] for lut in luts]
    close = difflib.get_close_matches(text, names, n=5, cutoff=0.3) or [lut["id"] for lut in partial[:5]]
    hint = "; closest: " + ", ".join(close) if close else ""
    raise ValueError("no LUT named {!r}{} (list_luts_tool lists them)".format(text, hint))
