"""Animated 3D titles rendered by Blender (Title > Animated Title). Workstream: titles-text."""

from __future__ import annotations

import os
import shutil

from classes import blender_titles, title_svg
from classes.editor_tools._base import (
    ToolError, get_app, integer, mapping, number, obj, ok, project, project_fps,
    snap_seconds, string,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.titles_text_common import (
    CommitTimeout, commit_on_main, precheck_on_main,
    color_hex_alpha, create_track, place_clip, plan_overlay_track, track_info,
)
from classes.logger import log

HIDDEN = ("start_frame", "end_frame", "file_name", "length_multiplier")


def _blender_command() -> str:
    try:
        return str(get_app().get_settings().get("blender_command") or "").strip()
    except Exception:
        return ""


def _executable(command: str) -> str:
    if not command:
        return ""
    if os.path.isabs(os.path.expanduser(command)):
        path = os.path.expanduser(command)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    return shutil.which(command) or ""


def _param_row(p: dict) -> dict:
    row = {"name": p.get("name"), "type": p.get("type"), "title": p.get("title", ""), "default": p.get("default", "")}
    for key in ("min", "max"):
        if key in p:
            row[key] = p[key]
    if p.get("values"):
        row["values"] = sorted(p["values"].values()) if "project_files" not in str(p.get("name")) else \
            "a Project Files id (image or video)"
    return row


def animated_catalog(query: str = ""):
    """([template rows], blender status) for list_title_templates_tool."""
    rows = []
    for path in blender_titles.template_paths():
        try:
            summary = blender_titles.template_summary(path)
            details = blender_titles.animation_details(path)
        except Exception as exc:
            log.debug("animated title %s unreadable: %s", path, exc)
            continue
        params = [_param_row(p) for p in details["params"] if p.get("name") not in HIDDEN]
        frames = next((int(float(p.get("default") or 0)) for p in details["params"]
                       if p.get("name") == "end_frame"), 0)
        row = {"name": summary["name"], "title": summary["title"], "frames_at_25fps": frames,
               "seconds": round(frames / blender_titles.TITLE_FPS, 2), "params": params}
        hay = (summary["name"] + " " + summary["title"]).lower().replace("_", " ")
        if all(w in hay for w in str(query or "").lower().replace("_", " ").split()):
            rows.append(row)
    command = _blender_command()
    exe = _executable(command)
    status = {"command": command, "found": bool(exe), "path": exe}
    if not exe:
        status["how_to_enable"] = ("Install Blender (blender.org, version %s or newer) and set Preferences > "
                                   "General > Blender Command to its executable" % _min_version())
    return rows, status


def _min_version() -> str:
    from classes import info
    return str(getattr(info, "BLENDER_MIN_VERSION", "5.0"))


def _find_template(name: str):
    raw = str(name or "").strip()
    if not raw:
        raise ToolError("template is required (list_title_templates_tool kind=animated lists them)")
    key = raw.lower().replace(" ", "_").replace(".xml", "").replace(".blend", "")
    for path in blender_titles.template_paths():
        summary = blender_titles.template_summary(path)
        if key in (summary["name"].lower(), summary["title"].lower().replace(" ", "_"),
                   summary["service"].lower().replace(".blend", "")):
            return path, summary
    names = [blender_titles.template_summary(p) for p in blender_titles.template_paths()]
    raise ToolError(f"unknown animated title {raw!r}; available: "
                    + ", ".join(f"{s['name']} ({s['title']})" for s in names))


def _apply_params(details: dict, values: dict, text: str, overrides: dict, file_choices: dict) -> dict:
    params_by_name = {p.get("name"): p for p in details["params"]}
    editable = [p for p in details["params"] if p.get("name") not in HIDDEN]
    if text:
        main = next((p for p in editable if p.get("type") in ("text", "multiline")), None)
        if main is None:
            raise ToolError("this animated title has no text; drop text")
        values[main["name"]] = text
    for name, value in (overrides or {}).items():
        p = params_by_name.get(name)
        if p is None or name in HIDDEN:
            raise ToolError(f"unknown parameter {name!r}; this template takes: "
                            + ", ".join(q.get("name") for q in editable))
        kind = p.get("type")
        if kind == "spinner":
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise ToolError(f"{name} must be a number") from None
            lo, hi = float(p.get("min", v)), float(p.get("max", v))
            if not lo <= v <= hi:
                raise ToolError(f"{name} must be between {lo:g} and {hi:g}")
            values[name] = v
        elif kind in ("text", "multiline"):
            values[name] = str(value)
        elif kind == "color":
            hex_color, alpha = color_hex_alpha(value, name)
            values[name] = blender_titles.color_value(hex_color + ("%02x" % round(alpha * 255)), name)
        elif kind == "dropdown":
            if "project_files" in name:
                if str(value) not in file_choices:
                    raise ToolError(f"{name} takes the id of an image or video in Project Files")
                values[name] = file_choices[str(value)]
                continue
            options = p.get("values") or {}
            by_label = {k.lower(): v for k, v in options.items()}
            v = str(value)
            if v in options.values():
                values[name] = v
            elif v.lower() in by_label:
                values[name] = by_label[v.lower()]
            else:
                raise ToolError(f"{name} must be one of {sorted(options.values())}")
    return values


def _project_file_choices() -> dict:
    from classes.query import File
    out = {}
    for f in File.filter():
        d = f.data if isinstance(f.data, dict) else {}
        if d.get("media_type") in ("image", "video") and not str(d.get("path", "")).lower().endswith(".svg"):
            out[f.id] = blender_titles.project_file_choice(d)
    return out


@editor_tool(
    "add_animated_title_tool",
    label="Add animated title",
    schema=obj({
        "template": string("Animated title from list_title_templates_tool kind=animated, by name or title, e.g. "
                           "'fly_by_1' (Fly Towards Camera), 'rotate_360', 'glare', 'neon_curves', "
                           "'dissolve', 'explode', 'spacemovie_intro'.", ""),
        "text": string("The title text (the template's first text field; '\\n' for a second line).", ""),
        "params": mapping("Other template parameters by name, e.g. {'diffuse_color': '#ff3b30', 'text_size': "
                          "1.5, 'spacemode': 'LEFT', 'sub_title': 'Day 1'}. Colours as '#RRGGBB'; project_files* "
                          "take a Project Files id."),
        "length_multiplier": integer("Animation length: 1 = as designed (about 3-5 s), 2 = twice as long, up to 5.",
                                     1, minimum=1, maximum=5),
        "position_seconds": number("Timeline second where the animation starts.", 0.0, minimum=0),
        "track": string("UI track number (1 = bottom), name or layer. Empty = the lowest free track above the "
                        "video.", ""),
        "file_name": string("Base name of the rendered frames. Empty = from the text.", ""),
        "timeout_seconds": integer("Give up (and stop Blender) after this many seconds.", 900,
                                   minimum=30, maximum=7200),
    }),
    background_safe=True,
    covers=("title.animated",),
)
def add_animated_title(template="", text="", params=None, length_multiplier=1, position_seconds=0.0, track="",
                       file_name="", timeout_seconds=900):
    """Render a 3D animated title with Blender and place it on the timeline (Title > Animated Title).

    Use for "animated title", "3D flying title", "neon title", "space movie intro".
    Needs Blender (Preferences > General > Blender Command); without it the call is
    refused with setup steps -- offer add_title_tool (static SVG titles with fades)
    instead. Rendering runs in the background and takes from tens of seconds to
    several minutes; the result is a transparent image-sequence clip on the lowest
    free track above the video. list_title_templates_tool kind=animated lists the
    templates and their parameters. One undo step removes the clip and file.

    Example: {"template": "fly_by_1", "text": "Tokyo Day 1", "params": {"diffuse_color": "#ffcc00"},
    "position_seconds": 0}.
    """
    from classes import info
    xml_path, summary = _find_template(template)
    details = blender_titles.animation_details(xml_path)
    fps_dict = project().get("fps") or {"num": 30, "den": 1}
    diff = blender_titles.project_fps_diff(fps_dict)
    values = blender_titles.default_params(details, diff)
    needs_files = any("project_files" in str(p.get("name")) for p in details["params"])
    choices = precheck_on_main(_project_file_choices) if needs_files else {}
    values = _apply_params(details, values, str(text or ""), dict(params or {}), choices)
    values["length_multiplier"] = float(length_multiplier) * max(1, diff)
    base = title_svg.safe_base_name(file_name or text or summary["name"], fallback="AnimatedTitle", limit=40)
    values["file_name"] = base.replace(" ", "_")

    command = _blender_command()
    exe = _executable(command)
    if not exe:
        raise ToolError(f"Blender is not available (Blender Command = {command or 'not set'!r}). Install Blender "
                        f"{_min_version()}+ from blender.org and set Preferences > General > Blender Command to "
                        "its executable; or use add_title_tool for a static title with fades")
    try:
        version = blender_titles.blender_version(exe)
    except Exception as exc:
        raise ToolError(f"Blender at {exe} did not run: {exc}") from None
    if not blender_titles.version_newer_or_equal(version, _min_version()):
        raise ToolError(f"Blender {version} is too old; animated titles need {_min_version()} or newer")

    frames = int(values.get("end_frame", 1)) * max(1, round(values["length_multiplier"]))
    fps = project_fps()
    position = snap_seconds(position_seconds)
    estimate = max(1.0 / fps, frames / fps)
    precheck_on_main(plan_overlay_track, position, position + estimate, track)

    folder = os.path.join(info.BLENDER_PATH, str(project().generate_id()))
    os.makedirs(folder, exist_ok=True)
    try:
        blend, source, target = blender_titles.script_paths(summary["service"], folder)
        gpu = bool(get_app().get_settings().get("blender_gpu_enabled"))
        values.update(blender_titles.project_params(project(), os.path.join(folder, values["file_name"])))
        with open(target, "w", encoding="UTF-8") as fh:
            fh.write(blender_titles.build_script(source, values, gpu))
        saved, output = blender_titles.render(exe, blend, target, float(timeout_seconds))
        if saved < 1:
            tail = "\n".join(output.splitlines()[-6:])
            raise ToolError(f"Blender rendered no frames ({summary['title']}). Last output: {tail}")
        seq = blender_titles.image_sequence_details(folder, values["file_name"], fps_dict)
    except ToolError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise ToolError(f"Blender render failed: {exc}") from None

    def _commit():
        from classes.query import File
        win = get_app().window
        win.files_model.add_files(seq["path"], seq, prevent_recent_folder=True, skip_indexing=True)
        f = File.get(path=seq["path"])
        if not f:
            raise ToolError("the rendered frames could not be imported")
        duration = float(f.data.get("duration") or estimate)
        layer, created = plan_overlay_track(position, position + duration, track)
        if created:
            create_track(layer, "Titles")
        clip = place_clip(f.id, position, duration, layer)
        return f.id, clip, layer, created, duration

    try:
        file_id, clip, layer, created, duration = commit_on_main(_commit)
    except CommitTimeout:
        raise                    # it may still finish: keep the rendered frames
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    t = track_info(layer)
    shown = {k: v for k, v in values.items() if k not in ("output_path", "horizon_color", "quality",
                                                          "file_format", "color_mode", "alpha_mode", "animation")}
    return ok(f"Rendered animated title {summary['title']!r} ({saved} frames with Blender {version}) on track "
              f"{t['track']} at {position:.2f}-{position + duration:.2f}s.",
              timeline_clip_id=clip.get("id"), file_id=file_id, template=summary["name"], frames=saved,
              folder=folder, position=round(position, 3), duration=round(duration, 3), new_track=created,
              blender_version=version, params=shown, **t)
