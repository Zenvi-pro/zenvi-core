"""Blender animated titles without the dialog (no Qt).

The Animated Title dialog (``windows/views/blender_listview.py``) and the
``add_animated_title_tool`` share these steps: read a template's parameters
from ``src/blender/<name>.xml``, build the dialog's parameter values, add the
project settings, inject them into ``src/blender/scripts/<name>.py.in`` (after
``base.py.in``, plus GPU code when enabled), run ``blender -b <blend> -P
<script> -a`` and import the ``<file_name>%04d.png`` image sequence it writes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from classes import info
from classes.logger import log

try:  # the dialog prefers the security-patched parser
    from defusedxml import minidom as xml
except ImportError:  # pragma: no cover
    from xml.dom import minidom as xml

VERSION_RE = re.compile(r"Blender ([0-9a-z\.]*)", flags=re.MULTILINE)
SAVED_RE = re.compile(r"Saved: '(.*\.png)")
TITLE_FPS = 25.0   # every animated title template is authored at 25 fps


def blender_dir() -> str:
    return os.path.join(info.PATH, "blender")


def template_paths() -> List[str]:
    folder = blender_dir()
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if os.path.isfile(os.path.join(folder, f)) and ".xml" in f]


def _text(doc, tag: str) -> str:
    nodes = doc.getElementsByTagName(tag)
    if nodes and nodes[0].childNodes:
        return nodes[0].childNodes[0].data
    return ""


def template_summary(xml_path: str) -> dict:
    """Title, icon and .blend 'service' of a template (the dialog's list columns)."""
    doc = xml.parse(xml_path)
    try:
        return {"name": os.path.splitext(os.path.basename(xml_path))[0], "title": _text(doc, "title"),
                "description": _text(doc, "description"), "icon": _text(doc, "icon"),
                "service": _text(doc, "service"), "path": xml_path}
    finally:
        doc.unlink()


def animation_details(xml_path: str) -> dict:
    """{title, path, service, params: [...]} -- BlenderListView.get_animation_details."""
    doc = xml.parse(xml_path)
    try:
        animation = {"title": _text(doc, "title"), "path": xml_path, "service": _text(doc, "service"),
                     "params": []}
        for param in doc.getElementsByTagName("param"):
            item = {"default": ""}
            for att in ("title", "description", "name", "type"):
                if param.attributes.get(att) is not None:
                    item[att] = param.attributes[att].value
            for tag in ("min", "max", "step", "digits", "default"):
                for p in param.getElementsByTagName(tag):
                    if p.childNodes:
                        item[tag] = p.firstChild.data
            try:
                item["values"] = dict(
                    (p.attributes["name"].value, p.attributes["num"].value)
                    for p in param.getElementsByTagName("value")
                    if "name" in p.attributes and "num" in p.attributes)
            except (TypeError, AttributeError) as ex:
                log.warning("XML parser: %s", ex)
            animation["params"].append(item)
        return animation
    finally:
        doc.unlink()


def project_fps_diff(fps: dict) -> int:
    """Blender remaps frames by whole multiples only (1X, 2X ...): project fps / 25, rounded."""
    fps_float = float(fps.get("num", 25)) / float(fps.get("den", 1) or 1)
    return round(fps_float / TITLE_FPS)


def color_value(hex_color: str, name: str) -> list:
    """'#RRGGBB[AA]' -> [r, g, b] floats (+ alpha for *diffuse_color* params), as the colour button stores."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
    value = [r, g, b]
    if "diffuse_color" in name:
        value.append(int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0)
    return value


def project_file_choice(file_data: dict) -> str:
    """A 'project_files' dropdown value: path|height|width|media_type|fps (Picture Frames)."""
    fps = file_data.get("fps") or {"num": 25, "den": 1}
    return "|".join((str(file_data.get("path")), str(file_data.get("height")), str(file_data.get("width")),
                     str(file_data.get("media_type")),
                     str(float(fps.get("num", 25)) / float(fps.get("den", 1) or 1))))


def default_params(details: dict, fps_diff: int = 1, tr: Optional[Callable] = None) -> dict:
    """The parameter values the dialog holds right after a template is selected."""
    tr = tr or (lambda s: s)
    params = {}
    for param in details.get("params", []):
        name, kind, default = param.get("name"), param.get("type"), param.get("default", "")
        if name in ("start_frame", "end_frame"):
            params[name] = int(float(default))
        elif kind == "spinner":
            params[name] = float(default)
        elif kind in ("text", "multiline"):
            params[name] = tr(default)
        elif kind == "dropdown":
            values = param.get("values") or {}
            value = default if default in values.values() else next(iter(sorted(values.items())), ("", ""))[1]
            if name == "length_multiplier":
                params[name] = float(value or 1) * fps_diff
            else:
                params[name] = value
        elif kind == "color":
            params[name] = color_value(default or "#ffffff", name)
    return params


def project_params(project, output_path: str, is_preview: bool = False) -> dict:
    """Project settings the scripts need (BlenderListView.get_project_params)."""
    fps = project.get("fps")
    params = {"fps": fps["num"]}
    if fps["den"] != 1:
        params["fps_base"] = fps["den"]
    params.update({
        "resolution_x": project.get("width"),
        "resolution_y": project.get("height"),
        "resolution_percentage": 50 if is_preview else 100,
        "quality": 100,
        "file_format": "PNG",
        "color_mode": "RGBA",
        "alpha_mode": 1,
        "horizon_color": (0.57, 0.57, 0.57),
        "animation": True,
        "output_path": output_path,
    })
    return params


def build_script(source_path: str, params: dict, gpu_enabled: bool = False) -> str:
    """base.py.in + the template script with *params* injected (BlenderListView.inject_params)."""
    user_params = "\n#BEGIN INJECTING PARAMS\n"
    user_params += 'params_json = r' + '"""{}"""'.format(json.dumps(params))
    user_params += "\n#END INJECTING PARAMS\n"
    if gpu_enabled:
        gpu_path = os.path.join(blender_dir(), "scripts", "gpu_enable.py.in")
        try:
            with open(gpu_path, "r") as f:
                gpu_code = f.read()
            if gpu_code:
                user_params += "\n#ENABLE GPU RENDERING\n" + gpu_code + "\n#END ENABLE GPU RENDERING\n"
        except IOError as e:
            log.error("Could not load GPU enable code! %s", e)
    with open(source_path, "r") as f:
        script_body = f.read()
    base_path = os.path.join(blender_dir(), "scripts", "base.py.in")
    try:
        with open(base_path, "r") as f:
            script_body = f.read() + "\n\n" + script_body
    except IOError:
        log.error("Could not load base Blender helper script at %s", base_path)
    return script_body.replace("# INJECT_PARAMS_HERE", user_params)


def script_paths(service: str, folder: str) -> Tuple[str, str, str]:
    """(.blend file, source .py.in, target .py in *folder*) for a template's service name."""
    blend = os.path.join(blender_dir(), "blend", service)
    source = os.path.join(blender_dir(), "scripts", service.replace(".blend", ".py.in"))
    target = os.path.join(folder, service.replace(".blend", ".py"))
    return blend, source, target


def image_sequence_details(folder: str, base_name: str, fps: dict) -> dict:
    """What render_finished hands to files_model.add_files for the rendered frames."""
    pattern = "{}%04d.png".format(base_name)
    return {
        "folder_path": folder,
        "base_name": base_name,
        "fixlen": True,
        "digits": 4,
        "extension": "png",
        "fps": {"num": fps.get("num", 25), "den": fps.get("den", 1)},
        "pattern": pattern,
        "path": os.path.join(folder, pattern),
    }


# ---------------------------------------------------------------------------
# Running Blender
# ---------------------------------------------------------------------------

def _env() -> dict:
    env = dict(os.environ)
    if sys.platform == "linux":
        env.pop("LD_LIBRARY_PATH", None)   # do not hand our bundled libraries to Blender
    return env


def version_newer_or_equal(version: str, minimum: str) -> bool:
    def parts(v):
        return [int(p) for p in re.findall(r"\d+", v)[:3]] or [0]
    return parts(version) >= parts(minimum)


def blender_version(command: str, timeout: float = 90.0) -> str:
    """'4.5.1' from ``blender --factory-startup -v``; raises OSError/RuntimeError when it cannot run."""
    if not command:
        raise FileNotFoundError("no Blender command is configured")
    proc = subprocess.run([command, "--factory-startup", "-v"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, env=_env(), cwd=info.HOME_PATH)
    text = proc.stdout.decode("utf-8", errors="ignore")
    m = VERSION_RE.search(text)
    if not m:
        raise RuntimeError("no Blender version in the output of %s -v" % command)
    return m.group(1)


def render(command: str, blend_path: str, script_path: str, timeout: float,
           preview_frame: int = 0, cancelled: Optional[Callable[[], bool]] = None) -> Tuple[int, str]:
    """Run Blender in the background; returns (frames saved, output). Kills it on timeout/cancel."""
    cmd = [command, "--factory-startup", "-b", blend_path, "-y", "-P", script_path]
    cmd += ["-f", str(preview_frame)] if preview_frame > 0 else ["-a"]
    log.debug("Running Blender: %s", " ".join(shlex.quote(x) for x in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(),
                            cwd=info.HOME_PATH, start_new_session=(sys.platform != "win32"))
    saved = 0
    lines = []
    deadline = time.monotonic() + float(timeout)
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="ignore").rstrip()
            if line:
                lines.append(line)
                if len(lines) > 400:
                    del lines[:100]
            if SAVED_RE.search(line):
                saved += 1
            if time.monotonic() > deadline or (cancelled and cancelled()):
                _stop(proc)
                raise TimeoutError("Blender did not finish within %ss" % int(timeout))
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise TimeoutError("Blender did not finish within %ss" % int(timeout)) from None
    finally:
        if proc.poll() is None:
            _stop(proc)
    return saved, "\n".join(lines)


def _stop(proc) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
