"""Effects and color: the processing effects (Stabilizer, Tracker, Object Detector, Object Mask).

Workstream: effects-color (see ``effects_color``). The Process Effect dialog
(windows/process_effect.py) collects options and runs
``openshot.ClipProcessingJobs`` on the clip; this module builds the same
processing context from tool arguments and runs the job on the calling worker
thread, on a private one-clip Timeline (same frame mapping as the preview, so
the analysis lines up with playback). Only the final effect insert touches
the GUI thread. Model files come from ``classes.effect_models`` (the dialog's
allow-listed, sha256-verified downloads).
"""

from __future__ import annotations

import copy
import json
import os
import time as _time
from typing import Optional

from classes import effect_models, effect_ops
from classes.editor_tools._base import (
    CLIP_TARGET,
    ToolError,
    array,
    boolean,
    clip_extent,
    clip_frame_at,
    enum,
    get_app,
    integer,
    mapping,
    nullable,
    number,
    obj,
    ok,
    project_fps,
    resolve_clip,
    string,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.effects_color import (
    INTERPOLATION_ARG,
    PROCESSING_EFFECTS,
    PROPERTIES_ARG,
    catalog,
    clip_effects,
    compatibility_problem,
    prepare_properties,
    save_clip_effects,
)
from classes.logger import log

TRACKER_TYPES = ("KCF", "CSRT", "MIL", "BOOSTING", "TLD", "MEDIANFLOW", "MOSSE")
DEVICES = ("CPU", "GPU_AUTO", "GPU_CUDA", "GPU_OPENCL")
OPTION_NAMES = ("smoothing", "region", "region_time", "tracker_type", "model", "device", "download_model",
                "class_filter", "confidence", "points", "negative_points", "boxes", "mask_quality",
                "timeout_seconds", "if_exists")
# Replace or stack when the clip already has one: one stabilization / detection per clip,
# but several trackers or masks (one per object) are normal.
_DEFAULT_IF_EXISTS = {"Stabilizer": "update", "ObjectDetection": "update", "Tracker": "add", "ObjectMask": "add"}


def _yolo_ids():
    try:
        return [m.get("id") for m in effect_models.load_yolo_models_manifest().get("models", []) if m.get("id")]
    except (OSError, ValueError):
        return []


def _cutie_ids():
    try:
        return [m.get("id") for m in effect_models.load_model_manifest(effect_models.CUTIE_MODELS_PATH)
                .get("models", []) if m.get("id")]
    except (OSError, ValueError):
        return []


def opencv_available() -> bool:
    try:
        import openshot
        return bool(openshot.Clip().COMPILED_WITH_CV)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Argument validation -> processing context
# ---------------------------------------------------------------------------

def _rect(value, name) -> dict:
    if not isinstance(value, dict):
        raise ToolError(f'{name} is {{"x": 0.4, "y": 0.3, "width": 0.2, "height": 0.3}} (fractions of the frame)')
    try:
        r = {k: float(value[k]) for k in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"{name} needs x, y, width, height as fractions 0-1 of the frame") from None
    if not (0 <= r["x"] < 1 and 0 <= r["y"] < 1 and 0 < r["width"] <= 1 and 0 < r["height"] <= 1
            and r["x"] + r["width"] <= 1.0001 and r["y"] + r["height"] <= 1.0001):
        raise ToolError(f"{name} must lie inside the frame (fractions 0-1, x+width <= 1, y+height <= 1)")
    if r["width"] < 0.005 or r["height"] < 0.005:
        raise ToolError(f"{name} is too small to track")
    return r


def _point(value, name) -> dict:
    try:
        if isinstance(value, dict):
            p = {"x": float(value["x"]), "y": float(value["y"])}
        else:
            p = {"x": float(value[0]), "y": float(value[1])}
    except (KeyError, IndexError, TypeError, ValueError):
        raise ToolError(f"{name} are points like {{\"x\": 0.5, \"y\": 0.4}} (fractions of the frame)") from None
    if not (0 <= p["x"] <= 1 and 0 <= p["y"] <= 1):
        raise ToolError(f"{name} ({p['x']:g}, {p['y']:g}) is outside the frame (0-1)")
    return p


def _seconds_in_clip(clip_data, t, name) -> float:
    start, end, _d = clip_extent(clip_data)
    if t is None:
        return start
    t = float(t)
    if not start - 1e-6 <= t <= end + 1e-6:
        raise ToolError(f"{name} {t:g}s is outside the clip ({start:g}-{end:g}s on the timeline)")
    return max(start, min(t, end - 1.0 / project_fps()))


def _ensure_models(label, installed, download, size_bytes, do_download):
    if installed():
        return False
    if not download:
        mb = max(1, int(round((size_bytes or 0) / 1e6)))
        raise ToolError(f"the {label} model is not downloaded yet ({mb} MB); call again with download_model=true "
                        "to fetch it (or download it in the Process Effect dialog)")
    try:
        do_download()
    except Exception as exc:
        raise ToolError(f"downloading the {label} model failed: {exc}") from None
    if not installed():
        raise ToolError(f"the {label} model download did not verify; nothing changed")
    return True


def build_context(class_name: str, clip, opts: dict) -> tuple:
    """(context for ClipProcessingJobs, prompts for ObjectMask or None, notes) -- validation + model files."""
    data = clip.data
    notes = []
    if class_name == "Stabilizer":
        smoothing = opts.get("smoothing", 30)
        try:
            smoothing = int(smoothing)
        except (TypeError, ValueError):
            raise ToolError("smoothing must be a whole number 1-100") from None
        if not 1 <= smoothing <= 100:
            raise ToolError("smoothing must be 1-100 (frames averaged; 30 = default, higher = steadier)")
        return {"smoothing-window": float(smoothing)}, None, notes

    if class_name == "Tracker":
        if not opts.get("region"):
            raise ToolError('Tracker needs region: the box around the object on the frame at region_time, e.g. '
                            '{"x": 0.4, "y": 0.3, "width": 0.15, "height": 0.25} (use analyze_frame_colors_tool '
                            'with save_frame_path to look at the frame first)')
        r = _rect(opts["region"], "region")
        t = _seconds_in_clip(data, opts.get("region_time"), "region_time")
        first = int(round((t - float(data.get("position") or 0.0)) * project_fps()))
        tracker = str(opts.get("tracker_type") or "KCF").upper()
        if tracker not in TRACKER_TYPES:
            raise ToolError(f"tracker_type must be one of {', '.join(TRACKER_TYPES)}")
        return {"region": {"button-clicked": True, "x": 0, "y": 0, "width": 0, "height": 0,
                           "normalized_x": r["x"], "normalized_y": r["y"], "normalized_width": r["width"],
                           "normalized_height": r["height"], "first-frame": first},
                "tracker-type": tracker}, None, notes

    device = str(opts.get("device") or "CPU").upper()
    if device not in DEVICES:
        raise ToolError(f"device must be one of {', '.join(DEVICES)}")
    download = bool(opts.get("download_model"))

    if class_name == "ObjectDetection":
        manifest = effect_models.load_yolo_models_manifest()
        model = effect_models.model_by_id(manifest.get("models", []), opts.get("model") or "")
        if not model:
            raise ToolError(f"unknown model {opts.get('model')!r}; choose one of {', '.join(_yolo_ids())}")
        if _ensure_models(model.get("name") or model["id"], lambda: effect_models.yolo_installed_files_match(model),
                          download, model.get("bytes"),
                          lambda: effect_models.download_yolo_model(manifest, model)):
            notes.append(f"downloaded {model.get('name')}")
        ctx = {"model": effect_models.yolo_model_path(model), "classes_file": effect_models.yolo_classes_path(model),
               "processing-device": device}
        if opts.get("confidence") is not None:
            ctx["confidence-threshold"] = float(opts["confidence"])
        return ctx, None, notes

    # ObjectMask
    positives = [_point(p, "points") for p in (opts.get("points") or [])]
    negatives = [_point(p, "negative_points") for p in (opts.get("negative_points") or [])]
    boxes = [_rect(b, "boxes") for b in (opts.get("boxes") or [])]
    if not positives and not boxes:
        raise ToolError('ObjectMask needs at least one point on the object (points=[{"x": 0.5, "y": 0.45}]) '
                        'or a box around it (boxes=[{"x", "y", "width", "height"}]), as fractions of the frame')
    t = _seconds_in_clip(data, opts.get("region_time"), "region_time")
    sam_manifest = effect_models.load_model_manifest(effect_models.EFFICIENT_SAM_MODELS_PATH)
    cutie_manifest = effect_models.load_model_manifest(effect_models.CUTIE_MODELS_PATH)
    sam = effect_models.recommended_model(sam_manifest.get("models", []))
    cutie = effect_models.model_by_id(cutie_manifest.get("models", []), opts.get("mask_quality") or "")
    if not sam or not cutie:
        raise ToolError(f"unknown mask_quality {opts.get('mask_quality')!r}; choose one of {', '.join(_cutie_ids())}")
    sam_path = effect_models.object_mask_efficient_sam_path(sam)
    cutie_paths = effect_models.object_mask_cutie_paths(cutie)
    sam_dir, cutie_dir = effect_models.model_install_dir(sam), effect_models.model_install_dir(cutie)
    cutie_list = [cutie_paths[k] for k in ("encode-key", "encode-value", "memory-readout", "decode")]
    for label, model, manifest, inst_dir, meta, paths in (
            ("EfficientSAM", sam, sam_manifest, sam_dir, effect_models.EFFICIENT_SAM_INSTALL_METADATA, [sam_path]),
            ("Cutie", cutie, cutie_manifest, cutie_dir, effect_models.CUTIE_INSTALL_METADATA, cutie_list)):
        if _ensure_models(
                f"{label} ({model.get('name')})",
                lambda d=inst_dir, m=meta, mo=model, p=paths: effect_models.installed_model_files_match(d, m, mo, p),
                download, model.get("bytes"),
                lambda mf=manifest, mo=model, d=inst_dir, m=meta, p=paths:
                    effect_models.download_manifest_archive(mf, mo, d, m, p)):
            notes.append(f"downloaded {label}")
    ctx = {"efficient_sam_model": sam_path, "cutie_encode_key_model": cutie_paths["encode-key"],
           "cutie_encode_value_model": cutie_paths["encode-value"],
           "cutie_memory_readout_model": cutie_paths["memory-readout"], "cutie_decode_model": cutie_paths["decode"],
           "cutie_model_dir": cutie_dir, "model_size": 1024, "processing-device": device}
    prompts = {"frame": clip_frame_at(data, t), "positive_points": positives, "negative_points": negatives,
               "positive_rects": boxes}
    return ctx, prompts, notes


def object_mask_prompt_context(prompts: dict, width: int, height: int) -> dict:
    """Normalized prompts -> the dialog's ObjectMask context (source pixels, seed frame, legacy keys)."""
    pos = [{"x": p["x"] * width, "y": p["y"] * height} for p in prompts["positive_points"]]
    neg = [{"x": p["x"] * width, "y": p["y"] * height} for p in prompts["negative_points"]]
    rects = [{"x1": r["x"] * width, "y1": r["y"] * height, "x2": (r["x"] + r["width"]) * width,
              "y2": (r["y"] + r["height"]) * height} for r in prompts["positive_rects"]]
    frame = str(int(prompts["frame"]))
    ctx = {"object_mask_selection": {"seed_frame": int(frame), "frames": {frame: {
        "positive_points": pos, "negative_points": neg, "positive_rects": rects, "negative_rects": []}}},
        "positive_points": pos, "negative_points": neg, "positive_rects": rects, "negative_rects": []}
    if rects:
        r = rects[0]
        ctx.update(rect_x1=r["x1"], rect_y1=r["y1"], rect_x2=r["x2"], rect_y2=r["y2"])
        if not pos:
            ctx.update(positive_x=(r["x1"] + r["x2"]) / 2.0, positive_y=(r["y1"] + r["y2"]) / 2.0)
    if pos:
        ctx.update(positive_x=pos[0]["x"], positive_y=pos[0]["y"])
    if neg:
        ctx.update(negative_x=neg[0]["x"], negative_y=neg[0]["y"])
    return ctx


# ---------------------------------------------------------------------------
# Running the job (worker thread)
# ---------------------------------------------------------------------------

def run_job(class_name: str, clip_data: dict, context: dict, prompts: Optional[dict], timeout: float) -> dict:
    """Process the clip on a private timeline and return the new effect's JSON (with its data loaded)."""
    import openshot
    from classes import info
    from classes.editor_tools.effects_color_analysis import project_snapshot
    from classes.export_acceleration.export_pipeline import _clone_timeline

    effect_id = get_app().project.generate_id()
    os.makedirs(info.PROTOBUF_DATA_PATH, exist_ok=True)
    protobuf_path = os.path.join(info.PROTOBUF_DATA_PATH, effect_id + ".data")
    if os.name == "nt":
        protobuf_path = protobuf_path.replace("\\", "/")
    proj = project_snapshot()
    proj["clips"] = [copy.deepcopy(clip_data)]
    proj["effects"] = []
    video = {"fps": proj.get("fps") or {"num": 30, "den": 1}, "width": int(proj.get("width") or 1920),
             "height": int(proj.get("height") or 1080)}
    audio = {"sample_rate": int(proj.get("sample_rate") or 48000), "channels": int(proj.get("channels") or 2),
             "channel_layout": int(proj.get("channel_layout") or 3)}
    timeline, _cache = _clone_timeline(proj, video, audio, 32 * 1024 * 1024)
    clip = None
    try:
        clips = list(timeline.Clips())
        if not clips:
            raise ToolError("the clip could not be loaded for processing (missing media?)")
        clip = clips[0]
        clip.Open()
        ctx = dict(context)
        if prompts is not None:
            frame = clip.GetFrame(max(1, int(prompts["frame"])))
            ctx.update(object_mask_prompt_context(prompts, int(frame.GetWidth()), int(frame.GetHeight())))
        ctx["protobuf_data_path"] = protobuf_path
        payload = json.dumps(ctx)
        started = _time.monotonic()
        job = openshot.ClipProcessingJobs(class_name, payload)
        job.processClip(clip, payload)
        blank_error_since = None
        while not job.IsDone():
            if job.GetError():
                message = (job.GetErrorMessage() or "").strip()
                if message:
                    _cancel(job)
                    raise ToolError(f"{class_name} processing failed: {message}")
                blank_error_since = blank_error_since or _time.monotonic()
                if _time.monotonic() - blank_error_since > 3.0:
                    _cancel(job)
                    raise ToolError(f"{class_name} processing failed (no message from libopenshot)")
            else:
                blank_error_since = None
            if _time.monotonic() - started > timeout:
                _cancel(job)
                raise ToolError(f"{class_name} processing did not finish within {timeout:g}s "
                                f"({job.GetProgress()}% done); nothing changed. Raise timeout_seconds or trim the clip.")
            _time.sleep(0.1)
        if job.GetError():
            raise ToolError(f"{class_name} processing failed: {(job.GetErrorMessage() or '').strip() or 'unknown error'}")
        if not os.path.isfile(protobuf_path):
            raise ToolError(f"{class_name} processing finished without writing its data file")
        effect = openshot.EffectInfo().CreateEffect(class_name)
        effect.SetJson(json.dumps({"protobuf_data_path": protobuf_path}))
        effect.Id(effect_id)
        effect_json = json.loads(effect.Json())
        effect_json["_seconds"] = round(_time.monotonic() - started, 2)
        return effect_json
    finally:
        for o in (clip, timeline):
            try:
                if o is not None:
                    o.Close()
            except Exception:
                log.debug("closing the processing timeline failed", exc_info=True)


def _cancel(job):
    try:
        job.CancelProcessing()
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and not job.IsDone():
            _time.sleep(0.02)
    except RuntimeError:
        pass


def run_processing_tool(timeline_clip_id, clip_query, track, class_name, opts, properties=None,
                        interpolation="bezier", occurrence=0, position_near=None) -> str:
    unknown = sorted(set(opts or {}) - set(OPTION_NAMES))
    if unknown:
        raise ToolError(f"unknown processing option(s) {', '.join(unknown)}; accepted: {', '.join(OPTION_NAMES)}")
    clip = resolve_clip(timeline_clip_id, clip_query, track, occurrence=occurrence, position_near=position_near)
    problem = compatibility_problem(clip, class_name)
    if problem:
        raise ToolError(problem)
    reader = clip.data.get("reader") or {}
    if reader.get("media_type") == "image" or (reader.get("has_single_image") and class_name != "ObjectMask"):
        raise ToolError(f"{class_name} analyses motion over time; clip {clip.id} is a still image")
    if not opencv_available():
        raise ToolError(f"{class_name} needs libopenshot built with OpenCV, which this Zenvi build lacks")
    if_exists = str(opts.get("if_exists") or "auto")
    if if_exists not in ("auto", "update", "add"):
        raise ToolError("if_exists must be auto, update or add")
    if if_exists == "auto":
        if_exists = _DEFAULT_IF_EXISTS[class_name]
    try:
        timeout = float(opts.get("timeout_seconds") or 900)
    except (TypeError, ValueError):
        raise ToolError("timeout_seconds must be a number") from None
    if not 5 <= timeout <= 3600:
        raise ToolError("timeout_seconds must be 5-3600")
    extra = dict(properties or {}) if isinstance(properties, dict) else {}
    if properties not in (None, {}) and not isinstance(properties, dict):
        raise ToolError("properties is an object")
    if class_name == "ObjectDetection":
        if opts.get("class_filter"):
            extra.setdefault("class_filter", str(opts["class_filter"]))
        if opts.get("confidence") is not None:
            extra.setdefault("confidence_threshold", float(opts["confidence"]))
    # Validate the effect properties against a template before the (long) processing run.
    if extra:
        prepare_properties(class_name, extra, {}, clip.data, interpolation)
    context, prompts, notes = build_context(class_name, clip, opts)

    effect_json = run_job(class_name, copy.deepcopy(clip.data), context, prompts, timeout)
    seconds = effect_json.pop("_seconds", None)
    color = effect_ops._badge_color(effect_json)
    if color:
        effect_json.setdefault("ui", {}).setdefault("icon_color", color)
    effect_json.update(prepare_properties(class_name, extra, effect_json, clip.data, interpolation))

    from classes.query import Clip
    current = Clip.get(id=clip.id)
    if not current:
        raise ToolError(f"clip {clip.id} was removed while it was being processed; nothing changed")
    effects = copy.deepcopy(clip_effects(current))
    replaced = None
    if if_exists == "update":
        idxs = [i for i, e in enumerate(effects) if e.get("class_name") == class_name]
        if idxs:
            replaced = effects[idxs[0]].get("id")
            if "order" in effects[idxs[0]]:
                effect_json["order"] = effects[idxs[0]]["order"]
            effects[idxs[0]] = effect_json
            for i in reversed(idxs[1:]):
                del effects[i]
    if replaced is None:
        effects.append(effect_json)
    save_clip_effects([(clip.id, effects)])

    objects = effect_json.get("objects")
    object_ids = sorted(objects) if isinstance(objects, dict) else []
    name = catalog()[class_name]["name"]
    summary = f"{name} processed clip {clip.id}"
    summary += f" in {seconds:g}s" if seconds is not None else ""
    summary += f"; {len(object_ids)} tracked object(s)" if class_name != "Stabilizer" else ""
    summary += f" (replaced effect {replaced})" if replaced else ""
    summary += "." + (f" Note: {'; '.join(notes)}." if notes else "")
    return ok(summary, timeline_clip_id=clip.id, effect=class_name, effect_id=effect_json.get("id"),
              seconds=seconds, protobuf_data_path=effect_json.get("protobuf_data_path"),
              tracked_object_ids=object_ids, replaced_effect_id=replaced, notes=notes)


@editor_tool(
    "process_clip_effect_tool",
    label="Stabilize / track / detect",
    schema=obj({
        **CLIP_TARGET,
        "effect": enum(list(PROCESSING_EFFECTS),
                       "Stabilizer = remove camera shake; Tracker = follow a boxed object; ObjectDetection = "
                       "find people/cars/animals... (YOLO); ObjectMask = cut out an object you point at."),
        "smoothing": integer("Stabilizer: frames averaged, 1-100 (30 default; higher = steadier, more crop).", 30,
                             minimum=1, maximum=100),
        "region": mapping("Tracker: box around the object at region_time, {\"x\", \"y\", \"width\", \"height\"} as "
                          "fractions of the frame (0,0 = top-left)."),
        "region_time": nullable(number("Tracker/ObjectMask: timeline seconds of the frame the region/points refer to "
                                       "(default: the clip's first frame).")),
        "tracker_type": enum(list(TRACKER_TYPES), "Tracker algorithm: KCF fast (default), CSRT most accurate, "
                             "MOSSE fastest.", "KCF"),
        "model": enum([""] + _yolo_ids(), "ObjectDetection model ('' = recommended YOLO26 Nano; yoloe-* know more "
                      "labels).", ""),
        "device": enum(list(DEVICES), "ObjectDetection/ObjectMask processing device.", "CPU"),
        "download_model": boolean("Download the model files if missing (Object Detector ~10-90 MB, Object Mask "
                                  "~175 MB, from the OpenShot model releases, sha256-verified).", False),
        "class_filter": string("ObjectDetection: only show these labels, comma separated ('person,car').", ""),
        "confidence": nullable(number("ObjectDetection: minimum confidence 0-1 (default 0.25).", minimum=0,
                                      maximum=1)),
        "points": array({"type": "object"}, "ObjectMask: points ON the object, [{\"x\": 0.5, \"y\": 0.4}] (fractions)."),
        "negative_points": array({"type": "object"}, "ObjectMask: points NOT on the object (background)."),
        "boxes": array({"type": "object"}, "ObjectMask: box(es) around the object, [{\"x\", \"y\", \"width\", "
                       "\"height\"}] (fractions)."),
        "mask_quality": enum([""] + _cutie_ids(), "ObjectMask Cutie tracking quality ('' = recommended medium).", ""),
        "properties": PROPERTIES_ARG,
        "interpolation": INTERPOLATION_ARG,
        "if_exists": enum(["auto", "update", "add"],
                          "If the clip already has this effect: auto = replace a Stabilizer/Object Detector, add "
                          "another Tracker/Object Mask; update = replace; add = keep both.", "auto"),
        "timeout_seconds": number("Give up (changing nothing) after this long.", 900, minimum=5, maximum=3600),
    }, required=["effect"]),
    background_safe=True,
    covers=("effect.process",),
)
def process_clip_effect(*, effect, timeline_clip_id="", clip_query="", track="", smoothing=30, region=None,
                        region_time=None, tracker_type="KCF", model="", device="CPU", download_model=False,
                        class_filter="", confidence=None, points=None, negative_points=None, boxes=None,
                        mask_quality="", properties=None, interpolation="bezier", if_exists="auto",
                        timeout_seconds=900):
    """Run an analysis effect on a video clip, off the GUI thread, and add the result as an effect:
    "stabilize the shaky shot" (Stabilizer), "track the ball / the car" (Tracker with a region box),
    "detect people" (ObjectDetection, YOLO), "cut out the person / mask the dog" (ObjectMask with points
    or a box). Takes seconds to minutes (about real time for Stabilizer); changes nothing on failure
    or timeout; one undo step.

    The result's tracked objects (Tracker/ObjectDetection/ObjectMask) can drive other effects: add Blur
    or Pixelate with properties {"mask_source_id": "<effect_id>"} to blur only the tracked object
    ("blur the face / licence plate"), plus "mask_invert": 1 to blur everything else ("blur the
    background"). Use analyze_frame_colors_tool(save_frame_path=...) to look at a frame before picking
    a region. Model files are downloaded only with download_model=true. Refused: still images,
    audio-only clips, locked tracks, a missing region/points, builds without OpenCV.
    Example: effect="Tracker", region={"x":0.42,"y":0.30,"width":0.12,"height":0.20}, region_time=3.0.
    """
    opts = {"smoothing": smoothing, "region": region, "region_time": region_time, "tracker_type": tracker_type,
            "model": model, "device": device, "download_model": download_model, "class_filter": class_filter,
            "confidence": confidence, "points": points, "negative_points": negative_points, "boxes": boxes,
            "mask_quality": mask_quality, "timeout_seconds": timeout_seconds, "if_exists": if_exists}
    return run_processing_tool(timeline_clip_id, clip_query, track, effect, opts, properties, interpolation)
