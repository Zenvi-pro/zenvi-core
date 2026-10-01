"""Headless clip effect tool — wraps timeline effect application without dialogs."""

from __future__ import annotations

import json


def _dialog_effects() -> set:
    try:
        from classes.effect_init import effect_options
        return set(effect_options.keys()) - {"Example"}
    except Exception:
        return set()


def list_effect_class_names() -> list[str]:
    try:
        import openshot
        raw = json.loads(openshot.EffectInfo.Json())
        return sorted(e["class_name"] for e in raw if e.get("class_name"))
    except Exception:
        return []


def add_effect(
    timeline_clip_id: str = "",
    clip_query: str = "",
    track: str = "",
    effect: str = "",
    effect_name: str = "",
    occurrence: str = "0",
    position_near=None,
    **_kw,
) -> str:
    """Add a libopenshot effect to one timeline clip.

    *effect* (or *effect_name*) is an EffectInfo class_name such as ``Blur``,
    ``Crop``, ``Brightness``. Effects that require the ProcessEffect dialog are
    refused — they cannot run headlessly from the agent.
    """
    from classes.agent_tools.receipt import ToolReceipt
    from classes.tool_handlers import (
        _get_app,
        _resolve_timeline_clip_for_tool,
        _run_on_main_thread,
    )

    name = str(effect or effect_name or "").strip()
    if not name:
        return ToolReceipt.refused(
            "add_effect_tool",
            "Error: add_effect_tool needs effect (EffectInfo class_name).",
        ).to_json()

    dialogish = _dialog_effects()
    if name in dialogish:
        return ToolReceipt.refused(
            "add_effect_tool",
            f"Error: effect {name!r} requires the ProcessEffect dialog and "
            "cannot be applied by the agent. Choose a different effect.",
        ).to_json()

    resolved = _resolve_timeline_clip_for_tool(
        timeline_clip_id=timeline_clip_id,
        clip_query=clip_query,
        track=track,
        occurrence=occurrence,
        position_near=position_near,
    )
    if getattr(resolved, "error", None):
        return ToolReceipt.error("add_effect_tool", resolved.error).to_json()
    if not getattr(resolved, "ok", False) or resolved.clip is None:
        return ToolReceipt.error(
            "add_effect_tool",
            resolved.error or "Error: could not resolve a timeline clip.",
        ).to_json()
    clip = resolved.clip

    available = set(list_effect_class_names())
    if available and name not in available:
        sample = ", ".join(sorted(available)[:12])
        return ToolReceipt.refused(
            "add_effect_tool",
            f"Error: unknown effect {name!r}. Examples: {sample}.",
        ).to_json()

    app = _get_app()
    effect_id_box = [None]
    error_box = [None]

    def _do():
        try:
            import openshot
            effect_obj = openshot.EffectInfo().CreateEffect(name)
            effect_obj.Id(app.project.generate_id())
            effect_json = json.loads(effect_obj.Json())
            data = clip.data if isinstance(clip.data, dict) else {}
            effects = data.get("effects")
            if not isinstance(effects, list):
                effects = list(effects) if effects else []
            effects.append(effect_json)
            clip.data = {"effects": effects}
            clip.save()
            effect_id_box[0] = effect_json.get("id")
        except Exception as exc:
            error_box[0] = str(exc)

    from classes.tool_handlers import QThread
    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_do)
    else:
        _do()

    if error_box[0]:
        return ToolReceipt.error("add_effect_tool", error_box[0]).to_json()

    cid = str(getattr(clip, "id", "") or "")
    return ToolReceipt.applied(
        "add_effect_tool",
        f"Added effect {name!r} to timeline_clip_id={cid} "
        f"(effect_id={effect_id_box[0]}).",
        clips=[{"id": cid, "effect": name, "effectId": effect_id_box[0]}],
        data={"effect": name, "effectId": effect_id_box[0], "timeline_clip_id": cid},
    ).to_json()
