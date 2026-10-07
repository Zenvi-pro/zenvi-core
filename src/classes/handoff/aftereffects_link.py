"""Linked clips rendered by After Effects (``zenvi_link.kind == "aftereffects"``).

An After Effects comp comes into Zenvi as a linked clip (AE renders it with
Zenvi Link's ``ae_render_for_zenvi``; ``import_linked_media_tool`` adds the
file). This provider lets Zenvi check and re-render it later:

* ``fingerprint``: the saved ``.aep`` (size + first/last MB, like
  ``media_fingerprint``) + comp name/id + props. AE changes count once the
  project is saved in AE.
* ``render``: asks the connected After Effects to render the comp into
  Zenvi's staging folder (needs the Zenvi Link panel); the argument names
  are taken from the host's own tool schema, so the call follows the
  extension's catalog.
* ``open_source``: opens the ``.aep`` with the system (After Effects).

Loaded by ``classes.handoff.plugins``.
"""

from __future__ import annotations

import os
from fractions import Fraction
from typing import Any, Dict, Optional

from classes.handoff import adobe_link
from classes.handoff.linked_media import (
    LinkError, RenderResult, SourceMissing, fingerprint_value, link_props, register_provider,
)

APP = "aftereffects"
RENDER_TOOL = "ae_render_for_zenvi"
RENDER_TIMEOUT = 60 * 60
_schema_cache: Dict[str, Any] = {}


def _render_arg_names() -> Dict[str, Optional[str]]:
    """Which argument names the connected AE's ``ae_render_for_zenvi`` takes for comp / output / project."""
    host = adobe_link.get_host(APP, probe=False)
    key = "%s:%s" % (host.pid, host.started_at)
    schema = _schema_cache.get(key)
    if schema is None:
        schema = {}
        for tool in adobe_link.list_host_tools(APP):
            if tool.get("name") == RENDER_TOOL:
                schema = tool.get("inputSchema") or {}
                break
        _schema_cache.clear()
        _schema_cache[key] = schema
    props = set((schema.get("properties") or {}).keys()) if isinstance(schema, dict) else set()

    def pick(*names: str) -> Optional[str]:
        return next((n for n in names if n in props), None)

    return {"comp": pick("comp", "comp_id", "comp_name", "composition"),
            "output_dir": pick("output_dir", "folder", "target_dir"),
            "output_path": pick("output_path", "output", "target", "path"),
            "project": pick("project", "aep", "project_path")}


def _fps(value: Any) -> Fraction:
    if isinstance(value, dict):
        return Fraction(int(value.get("num") or 30), int(value.get("den") or 1))
    try:
        return Fraction(float(value or 30)).limit_denominator(1001)
    except (TypeError, ValueError):
        return Fraction(30)


class AfterEffectsProvider:
    kind = "aftereffects"
    label = "After Effects"
    supports_studio = False

    def fingerprint(self, link: dict) -> str:
        from classes.media_fingerprint import fingerprint
        source = link.get("source") or {}
        aep = source.get("aep")
        if aep and not os.path.isfile(aep):
            raise SourceMissing(f"the After Effects project {aep} no longer exists")
        return fingerprint_value({"aep": fingerprint(aep) if aep else None,
                                  "composition": source.get("composition"),
                                  "composition_key": source.get("composition_key"),
                                  "props": link_props(link)})

    def render(self, link: dict, out_dir: str, *, on_progress, should_cancel) -> RenderResult:
        source = link.get("source") or {}
        comp = source.get("composition_key") or source.get("composition")
        if comp is None:
            raise LinkError("this After Effects link names no comp; re-import it from After Effects")
        try:
            names = _render_arg_names()
        except adobe_link.LinkHostError as exc:
            raise LinkError(f"After Effects could not render: {exc}") from None
        args: Dict[str, Any] = {}
        if names["comp"]:
            args[names["comp"]] = comp
        if names["output_dir"]:
            args[names["output_dir"]] = out_dir
        elif names["output_path"]:
            args[names["output_path"]] = os.path.join(out_dir, "render.mov")
        if names["project"] and source.get("aep"):
            args[names["project"]] = source["aep"]
        on_progress(None, "After Effects is rendering %s" % (source.get("composition") or comp))
        try:
            result = adobe_link.call_host_tool(APP, RENDER_TOOL, args, timeout=RENDER_TIMEOUT)
        except adobe_link.LinkHostError as exc:
            raise LinkError(f"After Effects could not render: {exc}") from None
        if result.is_error:
            raise LinkError(result.summary or "After Effects could not render the comp")
        data = result.receipt.get("data") or {}
        path = str(data.get("path") or data.get("output") or "")
        if not path or not os.path.isfile(path):
            raise LinkError("After Effects reported no rendered file")
        codec = str(data.get("codec") or "").lower().replace(" ", "").replace("_", "")
        if "prores" in codec:
            # AE before 2023 may only have a ProRes 422 output module: opaque, no alpha
            codec = "prores4444" if "4444" in codec else "prores422"
        elif "264" in codec:
            codec = "h264"
        elif codec != "qtrle":
            codec = "h264" if path.lower().endswith(".mp4") else "prores4444"
        fps = _fps(data.get("fps"))
        duration = float(data.get("duration") or 0.0)
        updates = {}
        if data.get("comp_id") is not None:
            updates["composition_key"] = data.get("comp_id")
        if data.get("comp_name"):
            updates["composition"] = data.get("comp_name")
        if data.get("project"):
            updates["aep"] = data.get("project")
        return RenderResult(path=path, codec=codec, width=int(data.get("width") or 0),
                            height=int(data.get("height") or 0), fps=fps,
                            duration_frames=int(round(duration * float(fps))),
                            warnings=list(result.receipt.get("warnings") or []), source_updates=updates)

    def open_source(self, link: dict) -> None:
        from classes.handoff.open_source import open_in_editor
        aep = (link.get("source") or {}).get("aep")
        if not aep:
            raise LinkError("this After Effects link has no saved project (.aep) to open")
        open_in_editor(aep, None, setting="system")

    def open_studio(self, link: dict) -> None:
        self.open_source(link)

    def editable_props(self, link: dict) -> dict:
        return link_props(link)


register_provider(AfterEffectsProvider())
