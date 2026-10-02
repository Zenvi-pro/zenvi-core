"""Editor preferences (Edit > Preferences) as typed, validated reads and writes.

Workstream: project-export. The catalog is ``settings/_default.settings`` (what the
Preferences dialog builds its tabs from): hidden settings are never listed or
changed, and the backend URL (it routes the user's credentials) is read-only.
A change is stored like the dialog stores it (``settings.set`` + save) and
takes effect through ``classes.preference_effects`` -- the same function the
dialog now calls. Preferences are not project data, so no undo step.
"""

from __future__ import annotations

import json
import platform
import re

from classes.editor_tools._base import ToolError, enum, get_app, obj, ok, on_main, string
from classes.editor_tools._registry import editor_tool

TABS = ("General", "Preview", "Timeline", "Autosave", "Cache", "Debug", "Keyboard", "Performance", "Location", "AI")

# Visible in the dialog but not something the assistant may change.
READ_ONLY_SETTINGS = {
    "zenvi-backend-url": "it decides where your sign-in token is sent; change it yourself in Preferences > AI",
}
_HW_DECODERS_BY_OS = {"Darwin": ("0", "2", "5"), "Windows": ("0", "3", "4"), "Linux": ("0", "1", "2", "6")}


def _defaults() -> dict:
    s = get_app().get_settings()
    try:
        with open(s.defaults_path, encoding="utf-8") as fh:
            return {d["setting"]: d.get("value") for d in json.load(fh) if d.get("setting")}
    except (OSError, ValueError, AttributeError):
        return {}


def _visible_settings() -> list:
    items = get_app().get_settings().get_all_settings() or []
    return [dict(d) for d in items if d.get("setting") and d.get("type") != "hidden" and d.get("category") in TABS]


def _choices(item: dict):
    """[{value, name}] for a dropdown, or a string explaining where the values come from."""
    setting = item["setting"]
    if setting == "default-profile":
        return "any profile name from list_project_profiles_tool"
    if setting == "theme":
        from themes.manager import ThemeName
        return [{"value": n, "name": n} for n in ThemeName.get_sorted_theme_names()]
    if setting == "default-language":
        try:
            from classes.language import get_all_languages
            langs = [{"value": loc, "name": f"{lang} ({loc})"} for loc, lang, _c in get_all_languages() if lang]
        except Exception:
            langs = []
        return [{"value": "Default", "name": "Default"}] + sorted(langs, key=lambda d: d["name"])
    if setting == "playback-audio-device":
        return "'' (default device) or a device from the Preferences > Preview list"
    if setting in ("graca_number_de", "graca_number_en"):
        return [{"value": i, "name": f"Graphics Card {i}"} for i in range(3)]
    values = [{"value": v.get("value"), "name": v.get("name")} for v in item.get("values") or []]
    if setting == "hw-decoder":
        allowed = _HW_DECODERS_BY_OS.get(platform.system())
        if allowed:
            values = [v for v in values if v["value"] in allowed]
    return values


def _describe(item: dict, defaults: dict) -> dict:
    out = {"key": item["setting"], "title": item.get("title") or item["setting"], "tab": item.get("category"),
           "type": item.get("type"), "value": item.get("value"), "default": defaults.get(item["setting"]),
           "restart_required": bool(item.get("restart"))}
    for k in ("min", "max", "step"):
        if k in item:
            out[k] = item[k]
    if item.get("type") == "dropdown":
        out["choices"] = _choices(item)
    if item["setting"] in READ_ONLY_SETTINGS:
        out["read_only"] = True
    return out


def _find(key: str) -> dict:
    wanted = str(key or "").strip().lower()
    items = _visible_settings()
    for d in items:
        if d["setting"].lower() == wanted:
            return d
    for d in items:
        if (d.get("title") or "").strip().lower() == wanted:
            return d
    hidden = {d.get("setting", "").lower() for d in get_app().get_settings().get_all_settings() or []}
    if wanted in hidden:
        raise ToolError(f"'{key}' is an internal setting that Preferences does not show; it cannot be changed here")
    close = [d["setting"] for d in items if wanted and (wanted in d["setting"].lower()
                                                        or wanted in (d.get("title") or "").lower())]
    hint = f" Did you mean: {', '.join(close[:6])}?" if close else " Use get_preferences_tool to list them."
    raise ToolError(f"no preference named '{key}'.{hint}")


def _shortcut_conflicts(key: str, sequences: list) -> list:
    clashes = []
    for d in _visible_settings():
        if d.get("category") != "Keyboard" or d["setting"] == key:
            continue
        theirs = {s.strip().lower() for s in str(d.get("value") or "").split("|") if s.strip()}
        for seq in sequences:
            if seq.lower() in theirs:
                clashes.append(f"{seq} is already {d.get('title') or d['setting']}")
    return clashes


def _parse_value(item: dict, value):
    kind = item.get("type")
    key = item["setting"]
    text = "" if value is None else str(value).strip()
    if kind == "bool":
        low = text.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ToolError(f"{key} is on/off: give true or false")
    if kind in ("spinner", "spinner-int"):
        try:
            num = float(text)
        except ValueError:
            raise ToolError(f"{key} needs a number, got {value!r}") from None
        if kind == "spinner-int":
            if num != int(num):
                raise ToolError(f"{key} needs a whole number, got {value!r}")
            num = int(num)
        lo, hi = item.get("min"), item.get("max")
        if key in ("omp_threads_number", "ff_threads_number"):
            from classes.preference_effects import thread_limits
            lo, hi = thread_limits(key)
        if lo is not None and num < lo or hi is not None and num > hi:
            raise ToolError(f"{key} must be between {lo} and {hi}, got {value!r}")
        return num
    if kind == "dropdown":
        if key == "default-profile":
            from classes.editor_tools.project_export_profiles import resolve_profile_request
            return resolve_profile_request(text)["description"]
        choices = _choices(item)
        if isinstance(choices, str):
            return text
        for c in choices:
            if str(c["value"]).lower() == text.lower() or str(c["name"] or "").lower() == text.lower():
                return c["value"]
        raise ToolError(f"{key} must be one of: " + ", ".join(f"{c['value']} ({c['name']})" for c in choices))
    if item.get("category") == "Keyboard":
        seqs = [s.strip() for s in text.split("|") if s.strip()]
        for s in seqs:
            if not re.match(r"^((Ctrl|Shift|Alt|Meta|Cmd)\+)*([A-Za-z0-9]|F\d{1,2}|[^\s+]|Left|Right|Up|Down|Home|End|"
                            r"PgUp|PgDown|Space|Tab|Return|Enter|Esc|Escape|Del|Delete|Backspace|Ins|Insert)$", s):
                raise ToolError(f"'{s}' is not a key sequence like 'Ctrl+Shift+K' or 'F5'")
        clashes = _shortcut_conflicts(key, seqs)
        if clashes:
            raise ToolError("shortcut conflict: " + "; ".join(clashes))
        return " | ".join(seqs)
    if kind in ("text", "browse"):
        return str(value if value is not None else "")
    raise ToolError(f"{key} ({kind}) cannot be changed here")


@editor_tool(
    "get_preferences_tool",
    label="Read preferences",
    schema=obj({
        "tab": enum(["", *TABS], "Only this Preferences tab.", ""),
        "key": string("One preference by key (e.g. 'autosave-interval') or its title.", ""),
        "query": string("Words in the key or title, e.g. 'cache', 'audio'.", ""),
    }),
    read_only=True,
    covers=("prefs.get",),
)
def get_preferences(tab="", key="", query=""):
    """Read editor preferences (Edit > Preferences) with their allowed values.

    Each entry has key (pass it to set_preference_tool), title, tab, type
    (bool / spinner / spinner-int / dropdown / text / browse), value, default,
    min/max, choices for dropdowns, and restart_required. Keyboard shortcuts
    are listed only for tab='Keyboard' or a query. Internal settings are not shown.
    """
    defaults = _defaults()
    if (key or "").strip():
        item = _describe(_find(key), defaults)
        return ok(f"{item['title']} = {item['value']!r} (default {item['default']!r}).", preference=item)
    tokens = [t for t in re.split(r"[\s,]+", (query or "").lower()) if t]
    rows = []
    for d in _visible_settings():
        if tab and d.get("category") != tab:
            continue
        if d.get("category") == "Keyboard" and not (tab == "Keyboard" or tokens):
            continue
        text = f"{d['setting']} {d.get('title') or ''}".lower()
        if tokens and not all(t in text for t in tokens):
            continue
        rows.append(_describe(d, defaults))
    return ok(f"{len(rows)} preference(s)" + (f" on {tab}" if tab else "") + ".", preferences=rows)


@editor_tool(
    "set_preference_tool",
    label="Change preference",
    schema=obj({
        "key": string("Preference key (or its title) from get_preferences_tool, e.g. 'autosave-interval'.", ""),
        "value": string("New value: true/false, a number within min/max, a dropdown value or name, text, or a "
                        "shortcut like 'Ctrl+Shift+K' (several: 'Ctrl+K | F5').", ""),
        "restore_tab": enum(["", *TABS], "Instead of key/value: restore every preference on this tab to its default "
                            "(Preferences > Restore Defaults).", ""),
    }),
    background_safe=True,
    covers=("prefs.set",),
)
def set_preference(key="", value="", restore_tab=""):
    """Change one editor preference, validated against its type and allowed values, or restore a tab's defaults.

    Takes effect like the Preferences dialog (autosave timer, cache, theme,
    decoder, shortcuts...); restart_required in the receipt says when a
    restart is needed. Refuses unknown keys (with suggestions), out-of-range
    numbers, invalid choices, conflicting shortcuts and internal or protected
    settings. Not an undo step: call again with the previous value to revert.
    """
    from classes.preference_effects import apply_preference_side_effects

    s = get_app().get_settings()
    if restore_tab:
        if (key or "").strip():
            raise ToolError("give either key/value or restore_tab, not both")
        restart = bool(s.restore(category_filter=restore_tab))

        def _after_restore():
            win = getattr(get_app(), "window", None)
            if restore_tab in ("Performance", "Cache"):
                for k in ("omp_threads_number", "ff_threads_number", "cache-limit-mb"):
                    try:
                        apply_preference_side_effects(k, s.get(k))
                    except Exception:
                        pass
            if restore_tab == "Timeline":
                apply_preference_side_effects("timeline-thumbnail-style", s.get("timeline-thumbnail-style"))
            if restore_tab == "Keyboard" and win is not None:
                win.initShortcuts()

        on_main(_after_restore)
        return ok(f"Restored the {restore_tab} preferences to their defaults"
                  + ("; restart Zenvi for all of them to take effect." if restart else "."),
                  tab=restore_tab, restart_required=restart)
    if not (key or "").strip():
        raise ToolError("give key and value (see get_preferences_tool), or restore_tab")
    item = _find(key)
    name = item["setting"]
    if name in READ_ONLY_SETTINGS:
        raise ToolError(f"'{name}' is protected: {READ_ONLY_SETTINGS[name]}")
    new = _parse_value(item, value)
    old = item.get("value")
    if new == old:
        return ok(f"{item.get('title') or name} is already {new!r}.", key=name, value=new, changed=False)
    s.set(name, new)
    try:
        def _effects():
            apply_preference_side_effects(name, new)
            if item.get("category") == "Keyboard":
                get_app().window.initShortcuts()
        on_main(_effects)
    except Exception as exc:
        s.set(name, old)
        raise ToolError(f"could not apply {name}: {exc}") from exc
    s.save()
    restart = bool(item.get("restart"))
    return ok(f"Set {item.get('title') or name} to {new!r} (was {old!r})"
              + ("; restart Zenvi for it to take effect." if restart else "."),
              key=name, value=new, previous=old, changed=True, restart_required=restart)
