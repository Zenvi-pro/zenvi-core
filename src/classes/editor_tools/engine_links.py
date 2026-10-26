"""Linked clips the Zenvi engine renders, read and re-rendered through the engine bridge.

Zenvi Motion blocks (``zenvi-motion``), Zenvi 3D scenes (``blender``) and
Zenvi FX renders (``zenvi-fx``) are Project Files whose media is a render of a
spec kept on the file record, ``files[].zenvi_link`` (the record of
handoff-core's ``classes/handoff/linked_media.py``: kind, version, source,
props, render, state, error). The engine wrote them, so the engine re-renders
them: ``rerender_linked_clip_tool`` runs the kind's own engine tool (ENGINE_KINDS)
through the bridge, which swaps the render into the same bin file as one undo
step, and answers ``unchanged`` when the new render is the one already there.

These tools only exist while the desktop has no linked-clip tools of its own.
handoff-core (``origin/jashan/handoff-core``, not on main yet) brings native
``get_linked_clip_tool`` / ``rerender_linked_clip_tool`` for Remotion,
HyperFrames and After Effects; once it lands, native wins by name here and its
``rerender_linked`` should hand an engine kind to :func:`rerender` (``is_engine_kind``)
instead of looking for a render provider -- the engine's re-render is a whole
tool call (new file record, clips that follow a longer render), not a file
written into a staging folder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from classes.editor_tools._base import fail, get_app, ok, on_main

LINK_KEY = "zenvi_link"
PROPS_ESCAPE_KEY = "$zenvi_json"


@dataclass(frozen=True)
class EngineKind:
    label: str
    # The engine tool that renders the link again, and whether it takes the file or a clip of it.
    rerender_tool: str
    target: str  # "file_id" | "clip_id"


ENGINE_KINDS = {
    "zenvi-motion": EngineKind("Zenvi Motion", "update_motion_block_tool", "file_id"),
    "blender": EngineKind("Zenvi 3D", "rerender_3d_scene_tool", "file_id"),
    "zenvi-fx": EngineKind("Zenvi FX", "update_fx_tool", "clip_id"),
}

_TARGET = {
    "type": "object",
    "properties": {
        "clip_id": {"type": "string", "description": "A timeline clip of the linked file (timeline_clip_id from "
                    "get_timeline_state_tool). Give this or file_id."},
        "file_id": {"type": "string", "description": "The linked file's Project Files id instead of a clip."},
    },
    "additionalProperties": False,
}

GET_TOOL = {
    "name": "get_linked_clip_tool",
    "title": "Linked clip info",
    "description": (
        "Read a linked clip's spec: a Zenvi Motion block, Zenvi 3D scene or Zenvi FX render (kind, block / scene / "
        "effects and their props, how it was rendered, whether the render file is there) and the timeline clips "
        "that use it. Use before changing or re-rendering one. Read-only."),
    "inputSchema": _TARGET,
    "annotations": {"title": "Linked clip info", "readOnlyHint": True, "destructiveHint": False,
                    "openWorldHint": False, "idempotentHint": True},
    "desktop": True,
}

RERENDER_TOOL = {
    "name": "rerender_linked_clip_tool",
    "title": "Re-render linked clip",
    "description": (
        "Re-render a Zenvi Motion block, Zenvi 3D scene or Zenvi FX clip from its stored spec with this Zenvi "
        "engine (after an engine update, or when its render file is gone) and swap it into every clip that uses "
        "it, as one undo step. To change props instead use update_motion_block_tool, rerender_3d_scene_tool or "
        "update_fx_tool. Answers unchanged when the render would be identical; a failed render changes nothing."),
    "inputSchema": _TARGET,
    "annotations": {"title": "Re-render linked clip", "readOnlyHint": False, "destructiveHint": False,
                    "openWorldHint": False},
    "desktop": True,
}


def is_engine_kind(kind) -> bool:
    return str(kind or "").strip().lower() in ENGINE_KINDS


def desktop_tools(bridged, existing) -> list:
    """(catalog entry, handler) of the linked-clip tools the desktop lacks, when the engine can re-render a kind."""
    if not any(k.rerender_tool in bridged for k in ENGINE_KINDS.values()):
        return []
    tools = [(GET_TOOL, get_linked_clip), (RERENDER_TOOL, rerender_linked_clip)]
    return [(tool, run) for tool, run in tools if tool["name"] not in existing]


def decode_props(stored) -> dict:
    """Props as the spec means them (linked_media.decode_props: path-like keys are stored JSON-encoded)."""
    if isinstance(stored, dict) and set(stored) == {PROPS_ESCAPE_KEY} and isinstance(stored[PROPS_ESCAPE_KEY], str):
        try:
            value = json.loads(stored[PROPS_ESCAPE_KEY])
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return dict(stored) if isinstance(stored, dict) else {}


def _target(clip_id: str, file_id: str) -> tuple:
    """GUI thread: (file record copy, clip ids using it, error) for the clip or file named."""
    project = get_app().project
    clip_id, file_id = str(clip_id or "").strip(), str(file_id or "").strip()
    if not clip_id and not file_id:
        return None, [], "say which linked clip: clip_id (a timeline clip) or file_id (its Project Files id)"
    clips = project.get("clips") or []
    if clip_id:
        clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
        if clip is None:
            return None, [], f"no timeline clip {clip_id}"
        if file_id and str(clip.get("file_id")) != file_id:
            return None, [], f"clip {clip_id} uses file {clip.get('file_id')}, not {file_id}; pass one of them"
        file_id = str(clip.get("file_id") or "")
    record = next((f for f in project.get("files") or [] if str(f.get("id")) == file_id), None)
    if record is None:
        return None, [], f"no Project Files entry {file_id}"
    using = [str(c.get("id")) for c in clips if str(c.get("file_id")) == file_id]
    if clip_id in using:
        # The clip the caller named first: an FX re-render runs on it.
        using.remove(clip_id)
        using.insert(0, clip_id)
    return json.loads(json.dumps(record)), using, None


def _link(record: dict):
    link = record.get(LINK_KEY)
    return link if isinstance(link, dict) else None


def report(record: dict, clip_ids: list) -> dict:
    """What get_linked_clip_tool answers for *record* (worker thread: checks the render file)."""
    from classes.editor_tools import engine_bridge as bridged

    link = _link(record) or {}
    kind = str(link.get("kind") or "")
    engine = ENGINE_KINDS.get(kind)
    render = link.get("render") if isinstance(link.get("render"), dict) else {}
    path = str(record.get("path") or "")
    present = bool(path) and os.path.isfile(path)
    state = str(link.get("state") or "fresh")
    if not present:
        state = "stale"
    return {
        "file_id": str(record.get("id")), "name": record.get("name") or os.path.basename(path), "path": path,
        "kind": kind, "kind_label": engine.label if engine else kind,
        "state": state, "error": link.get("error"), "render_present": present,
        "source": link.get("source"), "props": decode_props(link.get("props")), "render": render,
        "spec": {k: link[k] for k in ("motion", "scenes3d", "fx") if k in link},
        "rerender_tool": engine.rerender_tool if engine else None,
        "rerender_available": bool(engine and engine.rerender_tool in bridged.BRIDGED),
        "clips": clip_ids,
    }


def get_linked_clip(clip_id: str = "", file_id: str = "") -> str:
    record, clip_ids, error = on_main(_target, clip_id, file_id)
    if error:
        return fail(error)
    if _link(record) is None:
        return fail(f"{record.get('id')} is not a linked clip (its file has no {LINK_KEY})")
    info = report(record, clip_ids)
    if info["kind"] not in ENGINE_KINDS:
        info["detail"] = (f"{info['kind']} links are re-rendered by Zenvi's linked-clip support, which this build "
                          "does not have")
    missing = "" if info["render_present"] else "; its render file is missing (rerender_linked_clip_tool)"
    return ok(f"{info['kind_label']} clip {info['name']!r} is {info['state']}{missing}.", **info)


def rerender(file_id: str, clip_ids: list, link: dict):
    """Re-render an engine-kind link through the bridge: the engine tool's receipt, or ``Error: ...``."""
    from classes.editor_tools import engine_bridge as bridged

    kind = str(link.get("kind") or "")
    engine = ENGINE_KINDS.get(kind)
    if engine is None:
        return fail(f"{kind} links are not rendered by the Zenvi engine")
    if engine.rerender_tool not in bridged.BRIDGED:
        return fail(f"this Zenvi engine (zenvi CLI) has no {engine.rerender_tool}, which re-renders {engine.label} "
                    "clips: update the CLI")
    if engine.target == "clip_id":
        if not clip_ids:
            return fail(f"{engine.label} renders are re-rendered on a timeline clip, and no clip uses {file_id}")
        args = {"clip_id": clip_ids[0]}
        effects = decode_props(link.get("props")).get("effects")
        if isinstance(effects, list) and len(effects) > 1:
            # update_fx_tool changes one effect unless given the whole stack: the stored stack, as is.
            args["effects"] = effects
    else:
        args = {"file_id": file_id}
    return bridged.run_tool(engine.rerender_tool, args)


def rerender_linked_clip(clip_id: str = "", file_id: str = ""):
    record, clip_ids, error = on_main(_target, clip_id, file_id)
    if error:
        return fail(error)
    link = _link(record)
    if link is None:
        return fail(f"{record.get('id')} is not a linked clip (its file has no {LINK_KEY})")
    kind = str(link.get("kind") or "")
    if kind not in ENGINE_KINDS:
        return fail(f"{kind} links are re-rendered by Zenvi's linked-clip support, which this build does not have")
    return rerender(str(record.get("id")), clip_ids, link)

