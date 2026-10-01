"""The optional local ComfyUI integration (AI Tools menu) as tools: templates, Create/Enhance with AI, jobs.

Workstream: ai-generation. Everything goes through the editor's own code:

* the template catalogue and its per-file filter are ``generation_service.template_registry``
  (``templates_for_context`` -- what the AI Tools / Project Files menus list);
* queueing is ``generation_service._enqueue_generation_for_file`` with the payload the
  Generate dialog builds (``GenerateMediaDialog.get_payload``), so the workflow graph,
  source/reference binding, SAM2 seeds and output naming are identical to the UI;
* jobs are the in-memory ``window.generation_queue``; cancel is the Cancel Job action;
* finished jobs import their outputs into Project Files (``on_generation_job_finished``).

ComfyUI is optional and ships unconfigured (blank URL = off). A blank URL or an
unreachable server is a clean ``Error:`` before anything is queued. The reachability
ping runs on the calling worker thread (``background_safe``), never on the GUI thread.
Queueing changes the generation queue, not the project: these tools add no undo step.
"""

from __future__ import annotations

import os
import re
import time
import types
from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    enum,
    get_app,
    integer,
    number,
    obj,
    ok,
    on_main,
    parse_color,
    string,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

CREATE_KINDS = {"image": "txt2img-basic", "video": "txt2video-svd", "sound": "txt2audio-stable-open",
                "music": "txt2music-ace-step"}
ENHANCE_ACTIONS = {
    "image": {"upscale": "upscale-realesrgan-x4", "restyle": "img2img-basic", "image_to_video": "img2video-wan",
              "blur_object": "image-blur-anything-sam2", "highlight_object": "image-highlight-anything-sam2",
              "mask_object": "image-mask-anything-sam2", "depth": "image-extract-depth",
              "lines": "image-extract-lines"},
    "video": {"upscale": "video-upscale-gan", "smooth_motion": "video-frame-interpolation-rife2x",
              "split_scenes": "video-segment-scenes-transnet", "captions": "video-whisper-srt",
              "restyle": "video2video-basic", "blur_object": "video-blur-anything-sam2",
              "highlight_object": "video-highlight-anything-sam2", "mask_object": "video-mask-anything-sam2",
              "depth": "video-extract-depth", "lines": "video-extract-lines"},
    "audio": {"speech_clarity": "audio-clarity-speech", "reduce_noise": "audio-noise-reduce",
              "remove_noise": "audio-noise-remove"},
}
ACTION_NAMES = ("upscale", "restyle", "image_to_video", "blur_object", "highlight_object", "mask_object", "depth",
                "lines", "smooth_motion", "split_scenes", "captions", "speech_clarity", "reduce_noise",
                "remove_noise")
_ACTION_OF_TEMPLATE = {tid: action for table in ENHANCE_ACTIONS.values() for action, tid in table.items()}
_KIND_OF_TEMPLATE = {tid: kind for kind, tid in CREATE_KINDS.items()}
# The Generate dialog's object-tracking and highlight templates (GenerateMediaDialog._is_*_template).
TRACKING_TEMPLATES = frozenset({"video-blur-anything-sam2", "video-mask-anything-sam2",
                                "video-highlight-anything-sam2", "image-blur-anything-sam2",
                                "image-mask-anything-sam2", "image-highlight-anything-sam2"})
HIGHLIGHT_TEMPLATES = frozenset({"video-highlight-anything-sam2", "image-highlight-anything-sam2"})
_PROMPT_PLACEHOLDERS = ("__openshot_prompt__", "{{openshot_prompt}}", "$openshot_prompt")
_MENU_PARENTS = {"track_object": "Track an Object", "extract": "Extract"}
# The Generate dialog's Highlight tab defaults (QColor.name(HexArgb): #AARRGGBB).
DEFAULT_HIGHLIGHT_COLOR = "#b32ea6ff"
DEFAULT_BORDER_COLOR = "#ffffffff"
ACTIVE_STATES = ("queued", "running", "canceling")
FINISHED_STATES = ("completed", "failed", "canceled")
PING_TIMEOUT = 2.0
MAX_WAIT_SECONDS = 900

NOT_CONFIGURED = ("ComfyUI is not configured. It is an optional local AI server: start ComfyUI (for example "
                  "on this computer at http://127.0.0.1:8188) and enter its URL in Preferences > AI > ComfyUI URL. "
                  "Nothing was queued.")


# ---------------------------------------------------------------------------
# Service, status, templates
# ---------------------------------------------------------------------------

def _window():
    return get_app().window


def _service():
    svc = getattr(_window(), "generation_service", None)
    if svc is None:
        raise ToolError("the AI Tools (ComfyUI) service is not ready yet; try again in a moment")
    return svc


def _queue():
    queue = getattr(_window(), "generation_queue", None)
    if queue is None:
        raise ToolError("the AI generation queue is not ready yet; try again in a moment")
    return queue


def comfy_url() -> str:
    """The configured ComfyUI URL ('' = not configured), read on the GUI thread."""
    return str(on_main(lambda: _service().comfy_ui_url()) or "")


def comfy_status(ping: bool = True) -> dict:
    """{status: not_configured|unreachable|ready|configured, url, error?, how_to_enable?} (worker thread)."""
    url = comfy_url()
    if not url:
        return {"status": "not_configured", "url": "", "how_to_enable": NOT_CONFIGURED.rsplit(" Nothing", 1)[0]}
    if not ping:
        return {"status": "configured", "url": url}
    from classes.comfy_client import ComfyClient
    try:
        alive = ComfyClient(url).ping(timeout=PING_TIMEOUT)
    except Exception as exc:
        return {"status": "unreachable", "url": url, "error": str(exc)[:300],
                "how_to_enable": "Start ComfyUI, or fix the URL in Preferences > AI > ComfyUI URL."}
    if not alive:
        return {"status": "unreachable", "url": url, "error": "ComfyUI answered with an error status",
                "how_to_enable": "Start ComfyUI, or fix the URL in Preferences > AI > ComfyUI URL."}
    _refresh_menu_state()
    return {"status": "ready", "url": url}


def _refresh_menu_state() -> None:
    """Let the AI Tools menu pick up a server that just came up (best effort; menu cache only)."""
    try:
        on_main(lambda: _service().refresh_comfy_availability_async(timeout=PING_TIMEOUT))
    except Exception:
        log.debug("ComfyUI menu availability refresh skipped", exc_info=True)


def _require_configured() -> str:
    url = comfy_url()
    if not url:
        raise ToolError(NOT_CONFIGURED)
    return url


def _require_reachable() -> dict:
    status = comfy_status(ping=True)
    if status["status"] != "ready":
        raise ToolError(f"ComfyUI is not reachable at {status['url']} ({status.get('error', 'no answer')}). "
                        "Start ComfyUI or fix Preferences > AI > ComfyUI URL. Nothing was queued.")
    return status


def _registry():
    return _service().template_registry


def _media_context(media_type: str):
    """What templates_for_context expects for 'enhance a file of this kind'."""
    return types.SimpleNamespace(data={"media_type": media_type}, id="")


def _templates_for(source_file) -> list:
    return list(_registry().templates_for_context(source_file=source_file) or [])


def _needs_prompt(template: dict) -> bool:
    if str(template.get("category")) == "create":
        return True
    for node in (template.get("workflow") or {}).values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in ("text", "prompt", "tags"):
            value = str(inputs.get(key) or "").strip().lower()
            if value in _PROMPT_PLACEHOLDERS:
                return True
    return False


def _menu_path(template: dict) -> str:
    name = str(template.get("display_name") or template.get("id"))
    if str(template.get("category")) == "create":
        return "AI Tools > Create with AI > " + name
    parent = str(template.get("menu_parent") or "").strip().lower()
    parent_label = _MENU_PARENTS.get(parent, parent.replace("_", " ").title()) if parent else ""
    inner = (parent_label + " > " if parent_label else "") + name
    if str(template.get("category")) == "enhance":
        return "Project Files > right-click a file > Enhance with AI > " + inner
    return "AI Tools > Unknown AI > " + inner


def _output_kind(template: dict, base: str) -> str:
    kind = str(template.get("primary_output") or "unknown")
    if kind == "unknown" and _KIND_OF_TEMPLATE.get(base) in ("music", "sound"):
        return "audio"      # the music template declares output_type "music", which the registry drops
    return kind


def template_row(template: dict) -> dict:
    tid = str(template.get("id") or "")
    base = str(template.get("template_id") or tid)
    inputs = list(template.get("input_types") or [])
    return {
        "template": tid,
        "name": str(template.get("display_name") or tid),
        "menu": _menu_path(template),
        "category": str(template.get("category") or "unknown"),
        "kind": _KIND_OF_TEMPLATE.get(base, ""),
        "action": _ACTION_OF_TEMPLATE.get(base, ""),
        "input": inputs[0] if inputs else "",
        "output": _output_kind(template, base),
        "needs_prompt": _needs_prompt(template),
        "needs_reference_image": bool(template.get("needs_reference_image")),
        "needs_object_selection": base in TRACKING_TEMPLATES,
        "user_template": bool(template.get("is_user")),
    }


def find_template(text: str, templates: list, what: str) -> dict:
    """A template by id, base id or menu name ('Image...', 'Increase Resolution (4x)', '(User) Foo').

    An exact id wins; a name shared by several templates is refused with their ids.
    """
    want = str(text or "").strip().lower()
    for t in templates:
        if want and want == str(t.get("id") or "").lower():
            return t
    want = want.rstrip(".").strip()
    hits = {}
    for t in templates:
        display = str(t.get("display_name") or "").strip().lower()
        keys = {str(t.get("template_id") or "").lower(), display.rstrip(".").strip(),
                display.replace("(user)", "").strip().rstrip(".").strip()}
        if want and want in keys:
            hits[str(t.get("id"))] = t
    if len(hits) == 1:
        return next(iter(hits.values()))
    listing = ", ".join(f"{t.get('id')} ({t.get('display_name')})" for t in (hits.values() or templates))
    if hits:
        raise ToolError(f"several {what} templates are named {text!r}: {listing}; pass one of the ids")
    raise ToolError(f"no {what} template matches {text!r}; choose one of: {listing or 'none installed'}")


# ---------------------------------------------------------------------------
# Files and payload
# ---------------------------------------------------------------------------

def _file(file_id: str, what: str = "file_id"):
    from classes.query import File
    fid = str(file_id or "").strip()
    if not fid:
        raise ToolError(f"{what} is required (a Project Files id from list_files_tool)")
    f = File.get(id=fid)
    if not f:
        raise ToolError(f"no project file with {what}={fid!r}; list_files_tool lists them")
    return f


def _media_type(file_obj) -> str:
    return str((file_obj.data or {}).get("media_type") or "").strip().lower()


def _reference_image(reference_image_file_id: str, template: dict) -> str:
    rid = str(reference_image_file_id or "").strip()
    if not rid:
        if template.get("needs_reference_image"):
            raise ToolError(f"'{template.get('display_name')}' needs a reference image: pass "
                            "reference_image_file_id (an image in Project Files) for the look to copy")
        return ""
    ref = _file(rid, "reference_image_file_id")
    if _media_type(ref) != "image":
        raise ToolError(f"reference_image_file_id={rid!r} is a {_media_type(ref) or 'non-image'} file; "
                        "pass an image from Project Files")
    return rid


def _argb(value: str, what: str) -> str:
    """#RRGGBB / color name -> #AARRGGBB (the dialog's QColor HexArgb form); 8 hex digits pass through."""
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{8}", text):
        return text.lower()
    try:
        r, g, b, a = parse_color(text)
    except ToolError:
        raise ToolError(f"{what} {value!r} is not a color; use #RRGGBB, #AARRGGBB or a basic color name") from None
    return "#%02x%02x%02x%02x" % (a, r, g, b)


def _source_size(file_obj) -> tuple:
    try:
        return int(file_obj.data.get("width") or 0), int(file_obj.data.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _frame_count(file_obj) -> int:
    try:
        return max(1, int(float(file_obj.data.get("video_length") or 1)))
    except (TypeError, ValueError):
        return 1


def _check_points(points: list, what: str, width: int, height: int) -> list:
    out = []
    for p in points or []:
        x, y = float(p["x"]), float(p["y"])
        if width and height and not (0 <= x < width and 0 <= y < height):
            raise ToolError(f"point ({x:g}, {y:g}) in {what} is outside the {width}x{height} source frame")
        out.append({"x": int(round(x)), "y": int(round(y))})
    return out


def _check_boxes(boxes: list, what: str, width: int, height: int) -> list:
    out = []
    for b in boxes or []:
        x1, y1, x2, y2 = (float(b[k]) for k in ("x1", "y1", "x2", "y2"))
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        if width and height and not (0 <= x1 < width and 0 <= y1 < height and x2 <= width and y2 <= height):
            raise ToolError(f"box ({x1:g},{y1:g})-({x2:g},{y2:g}) in {what} is outside the {width}x{height} "
                            "source frame")
        if x2 - x1 < 1 or y2 - y1 < 1:
            raise ToolError(f"box ({x1:g},{y1:g})-({x2:g},{y2:g}) in {what} has no area")
        out.append({"x1": int(round(x1)), "y1": int(round(y1)), "x2": int(round(x2)), "y2": int(round(y2))})
    return out


def _empty_payload(name: str, template_id: str, prompt: str) -> dict:
    """GenerateMediaDialog.get_payload() shape with no tracking / highlight input."""
    return {
        "name": name, "template_id": template_id, "prompt": prompt, "reference_image_file_id": "",
        "coordinates_positive": "", "coordinates_negative": "", "rectangles_positive": "",
        "rectangles_negative": "", "auto_mode": False, "tracking_selection": {},
        "highlight_color": "", "highlight_opacity": 0.0, "border_color": "", "border_width": 0,
        "mask_brightness": 1.0, "background_brightness": 1.0,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def job_row(job: dict) -> dict:
    from classes.comfy_client import ComfyClient
    error = str(job.get("error") or "")
    row = {
        "job_id": str(job.get("id") or ""),
        "name": str(job.get("name") or ""),
        "template": str(job.get("template_id") or ""),
        "status": str(job.get("status") or ""),
        "progress": int(job.get("progress") or 0),
        "detail": str(job.get("progress_detail") or ""),
        "source_file_id": str(job.get("source_file_id") or ""),
        "prompt": str(job.get("prompt") or "")[:200],
        "origin": str(job.get("origin") or "ui"),
        "imported_file_ids": list(job.get("imported_file_ids") or []),
    }
    if error:
        row["error"] = ComfyClient.summarize_error_text(error, max_chars=400)
    if job.get("result"):
        row["result"] = dict(job["result"])
    return row


def _snapshot() -> list:
    """Every job, newest first (GUI thread: the queue is only mutated there)."""
    jobs = list(getattr(_queue(), "jobs", {}).values())
    return [job_row(j) for j in reversed(jobs) if isinstance(j, dict)]


def _enqueue(source_file, payload: dict) -> dict:
    """GUI thread: the Generate dialog's enqueue path; returns the new job's row."""
    svc, queue = _service(), _queue()
    before = set(queue.jobs)
    queued, error = svc._enqueue_generation_for_file(source_file, payload)
    if not queued:
        active = queue.get_active_job_for_file(getattr(source_file, "id", "")) if source_file else None
        if active:
            raise ToolError(f"{error} Job {active.get('id')} for this file is {active.get('status')}; wait for it "
                            "(list_comfyui_jobs_tool) or cancel it (cancel_comfyui_job_tool).")
        raise ToolError(error or "the generation job could not be queued")
    new_ids = [jid for jid in queue.jobs if jid not in before]
    if not new_ids:
        raise ToolError("the generation job was not added to the queue")
    job = queue.jobs[new_ids[-1]]
    job["origin"] = "agent"   # on_generation_job_finished: report failures without a modal dialog
    try:
        _window().statusBar.showMessage("Queued generation job", 3000)
    except Exception:
        log.debug("status bar message skipped", exc_info=True)
    return job_row(job)


def _queued_summary(row: dict, what: str, url: str) -> str:
    return (f"Queued ComfyUI job {row['job_id']} ({what}) at {url}; status {row['status']}. The result is imported "
            f"into Project Files when it finishes: call list_comfyui_jobs_tool(job_id='{row['job_id']}', "
            "wait_seconds=120) to wait for it and get the new file ids.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@editor_tool(
    "list_comfyui_templates_tool",
    label="List ComfyUI templates",
    schema=obj({
        "file_id": string("A Project Files id: list what Enhance with AI offers for that file (its media type "
                          "decides). Empty = the whole catalogue.", ""),
        "media_type": enum(["", "image", "video", "audio"], "Without file_id: only the Enhance templates for this "
                           "kind of media.", ""),
    }),
    read_only=True,
    covers=("ai.comfyui_create", "ai.comfyui_enhance"),
)
def list_comfyui_templates(file_id="", media_type=""):
    """List the local ComfyUI generation templates (AI Tools menu) and whether ComfyUI is set up.

    Use before create_media_with_comfyui_tool / enhance_file_with_comfyui_tool, or to answer
    "what AI tools can I use on this clip?". Each row gives the template id, its menu name and
    path, the kind (create) or action (enhance) to pass, input/output media and what it needs
    (prompt, reference image, object selection). comfyui.status is not_configured, unreachable
    or ready: ComfyUI is optional; when it is not ready say how to enable it (how_to_enable)
    instead of trying to generate. With file_id: exactly what the Enhance with AI menu offers
    for that file.
    """
    status = comfy_status(ping=True)
    rows, for_file = [], None
    if file_id:
        f = _file(file_id)
        mt = _media_type(f)
        for_file = {"file_id": f.id, "media_type": mt, "name": str(f.data.get("name") or
                                                                   os.path.basename(f.data.get("path", "")))}
        rows = [template_row(t) for t in _templates_for(f)]
    else:
        kinds = [media_type] if media_type else []
        if not media_type:
            rows = [template_row(t) for t in _templates_for(None)]
            kinds = ["image", "video", "audio"]
        for mt in kinds:
            for t in _templates_for(_media_context(mt)):
                row = template_row(t)
                if row["category"] == "enhance" or media_type:
                    rows.append(row)
    creates = sum(1 for r in rows if r["category"] == "create")
    enhances = len(rows) - creates
    what = f"{creates} Create and {enhances} Enhance template(s)" if not file_id else \
        f"{len(rows)} Enhance template(s) for {for_file['media_type'] or 'this'} file {for_file['file_id']}"
    if status["status"] == "not_configured":
        head = f"ComfyUI is not configured (Preferences > AI > ComfyUI URL), so these {what} cannot run yet."
    elif status["status"] == "unreachable":
        head = f"ComfyUI at {status['url']} is not reachable ({status.get('error', '')}); {what} are listed."
    else:
        head = f"ComfyUI is ready at {status['url']}: {what}."
    receipt = {"comfyui": status, "templates": rows}
    if for_file:
        receipt["for_file"] = for_file
    return ok(head, **receipt)


@editor_tool(
    "create_media_with_comfyui_tool",
    label="Create with ComfyUI",
    schema=obj({
        "prompt": string("What to generate, in plain words (subject, style, mood). For music: style tags, then "
                         "optionally a line 'Lyrics:' followed by the lyrics."),
        "kind": enum(["", "image", "video", "sound", "music"], "What to make with the built-in templates: image, "
                     "short video, sound effect or music. Ignored when template is given.", ""),
        "template": string("A template id or menu name from list_comfyui_templates_tool (for user templates); "
                           "overrides kind.", ""),
        "name": string("Name for the new Project Files item (default: generation, generation_gen2, ...).", ""),
        "reference_image_file_id": string("An image in Project Files, for templates that need a reference image.",
                                          ""),
    }, required=["prompt"]),
    background_safe=True,
    covers=("ai.comfyui_create",),
)
def create_media_with_comfyui(prompt, kind="", template="", name="", reference_image_file_id=""):
    """Generate a new image, video clip, sound effect or music track with the local ComfyUI server (AI Tools > Create with AI).

    Use only when the user wants local/ComfyUI generation and list_comfyui_templates_tool
    reports comfyui.status=ready; when ComfyUI is not configured this refuses with how to set
    it up (cloud video generation is generate_video_and_add_to_timeline_tool). Queues a job
    and returns its job_id at once; the result lands in Project Files when it finishes -- wait
    with list_comfyui_jobs_tool(job_id, wait_seconds), then place it with
    add_clip_to_timeline_tool. Nothing changes on the timeline and there is no undo step.
    Example: {"kind": "image", "prompt": "misty pine forest at dawn, cinematic"}.
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ToolError("prompt is empty; describe what to generate")
    url = _require_configured()
    creates = _templates_for(None)
    if template:
        tmpl = find_template(template, creates, "Create")
    else:
        if not kind:
            raise ToolError("say what to make: kind (image, video, sound, music) or a template from "
                            "list_comfyui_templates_tool")
        tmpl = find_template(CREATE_KINDS[kind], creates, "Create")
    ref_id = _reference_image(reference_image_file_id, tmpl)
    _require_reachable()

    def _queue_it():
        svc = _service()
        payload = _empty_payload(str(name or "").strip() or svc._default_generation_name(None), tmpl["id"], prompt)
        payload["reference_image_file_id"] = ref_id
        return _enqueue(None, payload)

    row = on_main(_queue_it)
    return ok(_queued_summary(row, str(tmpl.get("display_name") or tmpl["id"]).rstrip("."), url),
              job=row, template=template_row(tmpl))


_POINT = {"type": "object", "properties": {"x": {"type": "number", "description": "Source pixel column."},
                                           "y": {"type": "number", "description": "Source pixel row."}},
          "required": ["x", "y"], "additionalProperties": False}
_BOX = {"type": "object", "properties": {k: {"type": "number", "description": d} for k, d in (
    ("x1", "Left edge, source pixels."), ("y1", "Top edge."), ("x2", "Right edge."), ("y2", "Bottom edge."))},
    "required": ["x1", "y1", "x2", "y2"], "additionalProperties": False}


@editor_tool(
    "enhance_file_with_comfyui_tool",
    label="Enhance with ComfyUI",
    schema=obj({
        "file_id": string("The Project Files item to process (list_files_tool). The result is a NEW file; the "
                          "original is untouched."),
        "action": enum([""] + list(ACTION_NAMES), "What to do. Images: upscale, restyle, image_to_video, "
                       "blur_object, highlight_object, mask_object, depth, lines. Videos: upscale, smooth_motion "
                       "(2x frame rate), split_scenes, captions (from speech), restyle, blur_object, "
                       "highlight_object, mask_object, depth, lines. Audio: speech_clarity, reduce_noise, "
                       "remove_noise.", ""),
        "template": string("A template id or menu name from list_comfyui_templates_tool(file_id) instead of "
                           "action (user templates).", ""),
        "prompt": string("For restyle / image_to_video: the look or motion to create. For blur/highlight/mask: "
                         "the object to find, in words ('the red car'), instead of or with points/boxes.", ""),
        "name": string("Name for the new Project Files item (default: '<file>_genN').", ""),
        "reference_image_file_id": string("An image in Project Files whose look to copy (video restyle needs "
                                          "one).", ""),
        "points": array(_POINT, "Object tracking: clicks ON the object, in source pixels of the file "
                        "(x from the left, y from the top) on seed_frame."),
        "negative_points": array(_POINT, "Object tracking: clicks on things that are NOT the object."),
        "boxes": array(_BOX, "Object tracking: rectangles around the object (source pixels)."),
        "negative_boxes": array(_BOX, "Object tracking: rectangles to exclude."),
        "auto_detect": boolean("Object tracking: let SAM2 pick the main objects itself (no points needed).",
                               False),
        "seed_frame": integer("Object tracking in video: the 1-based source frame the points/boxes were chosen "
                              "on.", 1, minimum=1),
        "highlight_color": string("highlight_object: fill color, #RRGGBB or a color name (#AARRGGBB keeps its "
                                  "alpha).", DEFAULT_HIGHLIGHT_COLOR),
        "highlight_opacity": number("highlight_object: fill opacity 0-1.", 0.28, minimum=0, maximum=1),
        "border_color": string("highlight_object: outline color.", DEFAULT_BORDER_COLOR),
        "border_width": integer("highlight_object: outline width in pixels (0-64).", 2, minimum=0, maximum=64),
        "mask_brightness": number("highlight_object: brightness of the object (1 = unchanged, 0-3).", 1.15,
                                  minimum=0, maximum=3),
        "background_brightness": number("highlight_object: brightness of everything else (0-3).", 0.75,
                                        minimum=0, maximum=3),
    }, required=["file_id"]),
    background_safe=True,
    covers=("ai.comfyui_enhance",),
)
def enhance_file_with_comfyui(file_id, action="", template="", prompt="", name="", reference_image_file_id="",
                              points=None, negative_points=None, boxes=None, negative_boxes=None,
                              auto_detect=False, seed_frame=1, highlight_color=DEFAULT_HIGHLIGHT_COLOR,
                              highlight_opacity=0.28, border_color=DEFAULT_BORDER_COLOR, border_width=2,
                              mask_brightness=1.15, background_brightness=0.75):
    """Process a Project Files image, video or audio file with a local ComfyUI template (Project Files > Enhance with AI).

    For "upscale this video 4x", "make the motion smoother", "split this into scenes", "caption
    it from the speech", "blur the license plate", "highlight the player", "cut out the dog
    (mask)", "clean up the noise / make the speech clearer", "turn this photo into a video",
    "restyle it like a watercolor". Needs comfyui.status=ready (list_comfyui_templates_tool);
    otherwise refuses with setup steps. Queues a job and returns its job_id at once; the output
    becomes a NEW Project Files item (captions are stored on the file, split_scenes creates one
    file per scene). Wait with list_comfyui_jobs_tool(job_id, wait_seconds). No undo step.
    Object tracking (blur/highlight/mask) needs points, boxes, a prompt naming the object, or
    auto_detect. Example: {"file_id": "F12", "action": "blur_object", "prompt": "license plate"}.
    """
    _require_configured()
    f = _file(file_id)
    mt = _media_type(f)
    offered = _templates_for(f)
    if template:
        tmpl = find_template(template, offered, f"Enhance ({mt or 'unknown media'})")
    else:
        if not action:
            raise ToolError("say what to do: action (e.g. upscale, restyle, blur_object, captions, "
                            "reduce_noise) or a template from list_comfyui_templates_tool(file_id)")
        table = ENHANCE_ACTIONS.get(mt, {})
        if action not in table:
            valid = ", ".join(table) or "none (this media type has no Enhance templates)"
            raise ToolError(f"action '{action}' does not apply to {mt or 'unknown-type'} files; "
                            f"for {mt or 'this file'} use: {valid}")
        tmpl = find_template(table[action], offered, f"Enhance ({mt})")
    base = str(tmpl.get("template_id") or tmpl.get("id"))
    prompt = str(prompt or "").strip()
    if _needs_prompt(tmpl) and base not in TRACKING_TEMPLATES and not prompt:
        raise ToolError(f"'{tmpl.get('display_name')}' needs a prompt describing the result "
                        "(e.g. 'watercolor painting, soft light')")
    ref_id = _reference_image(reference_image_file_id, tmpl)

    width, height = _source_size(f)
    pos = _check_points(points, "points", width, height)
    neg = _check_points(negative_points, "negative_points", width, height)
    rects = _check_boxes(boxes, "boxes", width, height)
    neg_rects = _check_boxes(negative_boxes, "negative_boxes", width, height)
    tracking = base in TRACKING_TEMPLATES
    if not tracking and (pos or neg or rects or neg_rects or auto_detect):
        raise ToolError(f"points, boxes and auto_detect are for object tracking (blur_object, highlight_object, "
                        f"mask_object); '{tmpl.get('display_name')}' does not use them")
    if tracking and not (pos or rects or auto_detect or prompt):
        raise ToolError("object tracking needs a seed: points or boxes on the object (source pixels), a prompt "
                        "naming it ('the red car'), or auto_detect=true")
    frames = _frame_count(f) if mt == "video" else 1
    if int(seed_frame) > frames:
        raise ToolError(f"seed_frame {seed_frame} is beyond the file's {frames} frame(s)")
    if base in HIGHLIGHT_TEMPLATES:
        fill, outline = _argb(highlight_color, "highlight_color"), _argb(border_color, "border_color")
    else:
        fill, outline = "", ""
    _require_reachable()

    def _queue_it():
        svc = _service()
        payload = _empty_payload(str(name or "").strip() or svc._default_generation_name(f), tmpl["id"], prompt)
        payload["reference_image_file_id"] = ref_id
        if tracking:
            import json
            seed = int(seed_frame)
            payload.update({
                "coordinates_positive": json.dumps(pos) if pos else "",
                "coordinates_negative": json.dumps(neg) if neg else "",
                "rectangles_positive": json.dumps(rects) if rects else "",
                "rectangles_negative": json.dumps(neg_rects) if neg_rects else "",
                "auto_mode": bool(auto_detect),
                "tracking_selection": {"version": 1, "seed_frame": seed, "frames": {str(seed): {
                    "positive_points": pos, "negative_points": neg,
                    "positive_rects": rects, "negative_rects": neg_rects}}} if (pos or neg or rects or neg_rects)
                else {},
            })
        if fill:
            payload.update({"highlight_color": fill, "highlight_opacity": float(highlight_opacity),
                            "border_color": outline, "border_width": int(border_width),
                            "mask_brightness": float(mask_brightness),
                            "background_brightness": float(background_brightness)})
        return _enqueue(f, payload)

    row = on_main(_queue_it)
    label = str(tmpl.get("display_name") or tmpl["id"]).rstrip(".")
    return ok(_queued_summary(row, f"{label} on file {f.id}", comfy_url()), job=row, template=template_row(tmpl))


@editor_tool(
    "list_comfyui_jobs_tool",
    label="List ComfyUI jobs",
    schema=obj({
        "job_id": string("One job (from create_media_with_comfyui_tool / enhance_file_with_comfyui_tool). Empty = "
                         "every job this session.", ""),
        "active_only": boolean("Only queued, running or canceling jobs.", False),
        "wait_seconds": number("With job_id: wait up to this many seconds for the job to finish (0-900) before "
                               "answering.", 0, minimum=0, maximum=MAX_WAIT_SECONDS),
    }),
    read_only=True,
    covers=("ai.jobs",),
)
def list_comfyui_jobs(job_id="", active_only=False, wait_seconds=0):
    """List ComfyUI generation jobs: status (queued, running, canceling, completed, failed, canceled), progress, errors and the Project Files ids they produced.

    Use after create_media_with_comfyui_tool / enhance_file_with_comfyui_tool to wait for a
    result (job_id + wait_seconds) and get imported_file_ids to place with
    add_clip_to_timeline_tool, or to answer "is my generation done?". Jobs live for this
    editor session only. A failed job's error says why (e.g. a missing ComfyUI node/model).
    """
    wanted = str(job_id or "").strip()
    if wait_seconds and not wanted:
        raise ToolError("wait_seconds needs a job_id")
    rows = on_main(_snapshot)
    if wanted:
        match = [r for r in rows if r["job_id"] == wanted]
        if not match:
            known = ", ".join(r["job_id"] for r in rows[:10]) or "none this session"
            raise ToolError(f"no generation job {wanted!r}; jobs: {known}")
        deadline = time.monotonic() + float(wait_seconds or 0)
        row = match[0]
        while row["status"] not in FINISHED_STATES and time.monotonic() < deadline:
            time.sleep(0.5)
            row = next((r for r in on_main(_snapshot) if r["job_id"] == wanted), row)
        rows = [row]
    if active_only:
        rows = [r for r in rows if r["status"] in ACTIVE_STATES]
    status = comfy_status(ping=False)
    if wanted:
        r = rows[0] if rows else {}
        if not rows:
            return ok(f"Job {wanted} is not active.", jobs=[], comfyui=status)
        tail = ""
        if r["status"] == "completed":
            tail = f"; imported file ids: {', '.join(r['imported_file_ids']) or 'none'}"
        elif r["status"] == "failed":
            tail = f"; error: {r.get('error', 'unknown')}"
        elif r["status"] in ACTIVE_STATES:
            tail = f" ({r['progress']}%)" + (f"; still {r['status']} after waiting {wait_seconds:g} s" if wait_seconds
                                               else "")
        return ok(f"Job {wanted} ({r['name']}, {r['template']}) is {r['status']}{tail}.", jobs=rows, comfyui=status)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    what = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"
    return ok(f"{len(rows)} generation job(s): {what}.", jobs=rows, comfyui=status)


@editor_tool(
    "cancel_comfyui_job_tool",
    label="Cancel ComfyUI job",
    schema=obj({
        "job_id": string("The job to cancel (list_comfyui_jobs_tool).", ""),
        "file_id": string("Instead: cancel the active job processing this Project Files item (Project Files > "
                          "Cancel Job).", ""),
        "all_jobs": boolean("Instead: cancel every queued or running job.", False),
    }),
    covers=("ai.jobs",),
)
def cancel_comfyui_job(job_id="", file_id="", all_jobs=False):
    """Cancel a queued or running ComfyUI generation job (Project Files > Cancel Job).

    Pass exactly one of job_id, file_id (the job working on that file) or all_jobs=true. A
    running job first reports 'canceling' while ComfyUI stops it. Never deletes files or clips
    and adds no undo step; a finished job cannot be canceled.
    """
    chosen = [bool(str(job_id or "").strip()), bool(str(file_id or "").strip()), bool(all_jobs)]
    if sum(chosen) != 1:
        raise ToolError("pass exactly one of job_id, file_id or all_jobs=true")
    queue = _queue()
    if job_id:
        job = queue.get_job(str(job_id).strip())
        if not job:
            raise ToolError(f"no generation job {job_id!r}; list_comfyui_jobs_tool lists them")
        if job.get("status") not in ("queued", "running"):
            raise ToolError(f"job {job_id} is {job.get('status')}; only queued or running jobs can be canceled")
        targets = [job]
    elif file_id:
        job = queue.get_active_job_for_file(str(file_id).strip())
        if not job or job.get("status") not in ("queued", "running"):
            raise ToolError(f"no queued or running generation job for file {file_id!r}")
        targets = [job]
    else:
        targets = [j for j in queue.jobs.values() if j.get("status") in ("queued", "running")]
        if not targets:
            raise ToolError("no queued or running generation jobs to cancel")
    win = _window()
    for job in targets:
        win.cancel_generation_job(job.get("id"))
    rows = [job_row(queue.get_job(j.get("id")) or j) for j in targets]
    states = ", ".join(f"{r['job_id']} {r['status']}" for r in rows)
    return ok(f"Canceled {len(rows)} generation job(s): {states}.", jobs=rows)
