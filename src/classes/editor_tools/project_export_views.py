"""The editor layout: built-in views, docks, scopes, toolbar, fullscreen and saved custom views (View menu).

Workstream: project-export. Every change goes through the View menu's own
handlers on ``MainWindow`` (``actionSimple_View_trigger``,
``actionColor_Grade_View_trigger``, ``apply_custom_view``, the docks'
toggle actions, ``show_all_scope_docks``...). Layout is an editor preference,
not project data: no undo step.
"""

from __future__ import annotations

import uuid

from classes.editor_tools._base import ToolError, array, boolean, enum, get_app, obj, ok, on_main, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.project_export import window

_BUILTIN_VIEWS = {"simple": "actionSimple_View_trigger", "color": "actionColor_Grade_View_trigger",
                  "recording": "actionAudio_Recording_View_trigger"}
_VIEW_ALIASES = {"default": "simple", "editing": "simple", "edit": "simple", "color grade": "color",
                 "color grading": "color", "colour": "color", "grading": "color", "record": "recording",
                 "recording view": "recording", "color view": "color", "simple view": "simple"}
_DOCK_ALIASES = {"files": "dockFiles", "project files": "dockFiles", "media": "dockFiles",
                 "preview": "dockVideo", "video": "dockVideo", "video preview": "dockVideo", "viewer": "dockVideo",
                 "timeline": "dockTimeline", "transitions": "dockTransitions", "effects": "dockEffects",
                 "emojis": "dockEmojis", "emoji": "dockEmojis", "properties": "dockProperties",
                 "captions": "dockCaptionEditor", "caption editor": "dockCaptionEditor",
                 "chat": "dockAIChat", "assistant": "dockAIChat", "agents": "dockAIChat", "ai chat": "dockAIChat",
                 "scene descriptions": "dockAIMedia", "plan": "dockPlan", "recording": "dockAudioRecording",
                 "histogram": "dockHistogram", "waveform": "dockLumaWaveform", "luma waveform": "dockLumaWaveform",
                 "vectorscope": "dockVectorscope", "audio levels": "dockAudio", "audio meter": "dockAudio"}


def _docks(win) -> list:
    docks = list(win.getDocks())
    color = getattr(getattr(win, "propertyTableView", None), "color_grade_wheels_dock", None)
    if color is not None and color not in docks:
        docks.append(color)
    hidden = getattr(win, "HIDDEN_DOCK_OBJECT_NAMES", set())
    return [d for d in docks if d.objectName() not in hidden and d.objectName() != "dockTutorial"]


def _find_dock(win, name: str):
    wanted = str(name or "").strip().lower()
    docks = _docks(win)
    target = _DOCK_ALIASES.get(wanted)
    for d in docks:
        if d.objectName() == target or d.objectName().lower() == wanted or d.windowTitle().strip().lower() == wanted:
            return d
    if wanted in ("color wheels", "wheels", "color wheel"):
        color = getattr(getattr(win, "propertyTableView", None), "color_grade_wheels_dock", None)
        if color is not None:
            return color
    hits = [d for d in docks if wanted and wanted in d.windowTitle().lower()]
    if len(hits) == 1:
        return hits[0]
    names = sorted({d.windowTitle() for d in docks if d.windowTitle()})
    raise ToolError(f"no panel named '{name}'; panels: {', '.join(names)}")


def layout_report(win) -> dict:
    s = get_app().get_settings()
    open_docks, closed = [], []
    for d in _docks(win):
        (open_docks if win._dock_is_open(d) else closed).append(d.windowTitle() or d.objectName())
    views = [{"name": v.get("name"), "id": v.get("id")} for v in win._custom_views()]
    active_custom = win._active_custom_view()
    return {
        "active_view": (active_custom or {}).get("name") or s.get("active_builtin_view") or "",
        "custom_views": views,
        "open_panels": sorted(open_docks),
        "closed_panels": sorted(closed),
        "toolbar_visible": bool(win.toolBar.isVisible()),
        "fullscreen": bool(win.isFullScreen()),
    }


def _set_dock(win, dock, visible: bool) -> None:
    if visible:
        if win._dock_is_open(dock):
            dock.raise_()
            return
        if dock in win._scope_docks():
            win._anchor_and_show_scope_dock(dock)
            return
        if dock is getattr(win, "dockProperties", None):
            win._anchor_and_show_properties_dock()
            return
        from qt_api import Qt
        if win.dockWidgetArea(dock) == Qt.NoDockWidgetArea:
            win.addDockWidget(Qt.RightDockWidgetArea, dock)
        win.showDocks([dock])
        dock.raise_()
    elif win._dock_is_open(dock):
        dock.hide()


@editor_tool(
    "get_editor_layout_tool",
    label="Read editor layout",
    schema=obj({}),
    read_only=True,
    covers=("view.layout",),
)
def get_editor_layout():
    """Report the editor layout: active view, open and closed panels, saved custom views, toolbar and fullscreen.

    Panel names in the report are what set_editor_layout_tool's show_panels /
    hide_panels accept.
    """
    report = on_main(lambda: layout_report(window()))
    return ok(f"View '{report['active_view'] or 'custom'}' with {len(report['open_panels'])} open panel(s).", **report)


@editor_tool(
    "set_editor_layout_tool",
    label="Change editor layout",
    schema=obj({
        "view": string("Switch view: 'simple' (default editing), 'color' (Properties, Color Wheels and scopes), "
                       "'recording' (Recording dock), or the name of a saved custom view.", ""),
        "show_panels": array({"type": "string"}, "Panels to open, e.g. ['Properties', 'Effects', 'Histogram', "
                             "'Color Wheels', 'Captions'] (default none)."),
        "hide_panels": array({"type": "string"}, "Panels to close (default none)."),
        "scopes": enum(["", "show_all", "hide_all"], "Open or close every scope (Histogram, Luma Waveform, "
                       "Vectorscope, Audio Levels).", ""),
        "toolbar": enum(["", "show", "hide"], "Show or hide the main toolbar.", ""),
        "fullscreen": enum(["", "on", "off"], "Enter or leave fullscreen.", ""),
        "dock_all": boolean("Dock every floating panel back into the window.", False),
        "save_view_as": string("After the changes, save the layout as a new custom view with this name.", ""),
        "update_active_view": boolean("After the changes, overwrite the active custom view with the layout.", False),
        "delete_view": string("Delete this saved custom view (by name). Never touches the project.", ""),
    }),
    covers=("view.layout",),
)
def set_editor_layout(view="", show_panels=None, hide_panels=None, scopes="", toolbar="", fullscreen="",
                      dock_all=False, save_view_as="", update_active_view=False, delete_view=""):
    """Rearrange the editor window: switch view, open/close panels and scopes, toolbar, fullscreen, custom views.

    Use for "switch to the color view", "show the scopes", "open the
    properties panel", "go fullscreen", "save this layout as Review". Uses the
    View menu's own actions. Not an undo step (layout is an editor preference).
    Example: {"view": "color"}; {"show_panels": ["Histogram"], "save_view_as": "Grade"}
    """
    win = window()
    show_panels, hide_panels = list(show_panels or []), list(hide_panels or [])
    if not any([view, show_panels, hide_panels, scopes, toolbar, fullscreen, dock_all, save_view_as,
                update_active_view, delete_view]):
        raise ToolError("say what to change (view, show_panels, hide_panels, scopes, toolbar, fullscreen, "
                        "dock_all, save_view_as, update_active_view, delete_view); get_editor_layout_tool reads it")
    views = {v.get("name", "").strip().lower(): v for v in win._custom_views()}
    target_custom = None
    builtin = None
    if view:
        key = _VIEW_ALIASES.get(view.strip().lower(), view.strip().lower())
        if key in _BUILTIN_VIEWS:
            builtin = key
        elif key in views:
            target_custom = views[key]
        else:
            raise ToolError(f"no view named '{view}'; views: simple, color, recording"
                            + (", " + ", ".join(v.get("name") for v in views.values()) if views else ""))
    show = [_find_dock(win, n) for n in show_panels]
    hide = [_find_dock(win, n) for n in hide_panels]
    if delete_view and delete_view.strip().lower() not in views:
        raise ToolError(f"no saved custom view named '{delete_view}'")
    if save_view_as and save_view_as.strip().lower() in views:
        raise ToolError(f"a custom view named '{save_view_as}' already exists; use update_active_view or another name")
    if update_active_view and not win._active_custom_view() and not target_custom:
        raise ToolError("no custom view is active; switch to one (view=<name>) or use save_view_as")

    done = []
    if builtin:
        getattr(win, _BUILTIN_VIEWS[builtin])()
        done.append(f"{builtin} view")
    elif target_custom:
        win.apply_custom_view(target_custom.get("id"))
        done.append(f"custom view '{target_custom.get('name')}'")
    if dock_all:
        win.actionDock_All_trigger()
        done.append("docked all panels")
    if scopes == "show_all":
        win.show_all_scope_docks()
        done.append("scopes shown")
    elif scopes == "hide_all":
        win.closeDocks(win._scope_docks())
        done.append("scopes hidden")
    for d in show:
        _set_dock(win, d, True)
    for d in hide:
        _set_dock(win, d, False)
    if show:
        done.append("opened " + ", ".join(d.windowTitle() for d in show))
    if hide:
        done.append("closed " + ", ".join(d.windowTitle() for d in hide))
    if toolbar:
        win.toolBar.setVisible(toolbar == "show")
        try:
            win.actionView_Toolbar.setChecked(toolbar == "show")
        except Exception:
            pass
        done.append(f"toolbar {'shown' if toolbar == 'show' else 'hidden'}")
    if fullscreen and (fullscreen == "on") != bool(win.isFullScreen()):
        win.actionFullscreen_trigger()
        done.append("fullscreen " + fullscreen)
    if delete_view:
        doomed = views[delete_view.strip().lower()]
        win._set_custom_views([v for v in win._custom_views() if v.get("id") != doomed.get("id")])
        if win._active_custom_view_id() == doomed.get("id"):
            win._set_active_custom_view_id("")
        done.append(f"deleted custom view '{doomed.get('name')}'")
    if save_view_as:
        view_id = str(uuid.uuid4())
        saved = win._custom_views() + [win._current_custom_view_data(view_id, save_view_as.strip())]
        win._set_custom_views(saved)
        win._set_active_custom_view_id(view_id)
        done.append(f"saved view '{save_view_as.strip()}'")
    if update_active_view:
        win.update_active_custom_view()
        done.append(f"updated view '{(win._active_custom_view() or {}).get('name')}'")
    report = layout_report(win)
    return ok("Layout: " + "; ".join(done) + ".", **report)
