"""
Tool handlers — execute OpenShot tool calls received from the backend.

When the backend's LangChain agent calls a tool that needs the running Qt
application (project state, playback, timeline manipulation), the backend
delegates the call to the frontend via WebSocket.  This module maps tool
names to the actual functions that interact with the live Qt application.

Usage (from ai_chat_ui.py):
    from classes.tool_handlers import execute_tool
    result = execute_tool(tool_name, tool_args)
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid as uuid_module
from typing import Optional

from classes.logger import log
from classes.clip_placement import compute_clip_trim_bounds, default_underlay_layer_number
from classes.track_display import (
    format_track_label_for_llm,
    layer_number_to_display_index,
    layers_sorted_by_number,
    normalize_track_or_layer_arg,
)

try:
    from PyQt5.QtCore import (
        QObject, QThread, pyqtSignal, pyqtSlot,
        QEventLoop, QPointF, QTimer,
    )
except ImportError:
    QObject = object
    QThread = None
    pyqtSignal = None
    pyqtSlot = lambda x: x
    QEventLoop = None
    QPointF = None
    QTimer = None

try:
    from PyQt5.QtWidgets import QApplication
except ImportError:
    QApplication = None


# ---------------------------------------------------------------------------
# Main-thread dispatcher (signal-based)
# ---------------------------------------------------------------------------
# QTimer.singleShot(0, fn) called from a *background* thread creates the
# timer on that thread's event loop — if the thread is blocked (as the AI-
# chat WebSocket loop is), the callback never fires and the caller times
# out.  Instead we use a QObject that lives on the main thread and deliver
# the callable via a cross-thread signal which Qt routes through the main
# event loop.

if pyqtSignal is not None:

    class _MainThreadDispatcher(QObject):
        """Singleton helper that runs callables on the Qt main (GUI) thread."""

        _dispatch = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._dispatch.connect(self._on_dispatch)

        @pyqtSlot(object)
        def _on_dispatch(self, payload):
            func, args, result_box, error_box, done = payload
            try:
                result_box[0] = func(*args)
            except Exception as exc:
                error_box[0] = exc
            finally:
                done.set()

else:

    class _MainThreadDispatcher:
        """Headless fallback when PyQt5 is unavailable."""

        def run(self, fn):
            return fn()


_dispatcher = None
_dispatcher_lock = threading.Lock()


def _get_dispatcher():
    """Return (and lazily create) the singleton main-thread dispatcher."""
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher
    with _dispatcher_lock:
        if _dispatcher is not None:
            return _dispatcher
        d = _MainThreadDispatcher()
        # Ensure the dispatcher lives on the main thread so that signals
        # emitted from background threads are delivered via QueuedConnection.
        app = QApplication.instance() if QApplication is not None else None
        if app is not None:
            d.moveToThread(app.thread())
        _dispatcher = d
        return d


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_app():
    from classes.app import get_app
    return get_app()


def _pause_player():
    import time as _time
    try:
        app = _get_app()
        player = app.window.preview_thread.player
        import openshot
        was_playing = player.Mode() == openshot.PLAYBACK_PLAY
        player.Pause()
        _time.sleep(0.05)
        return was_playing
    except Exception:
        return False


def _resume_player(was_playing):
    try:
        if was_playing:
            _get_app().window.preview_thread.player.Play()
    except Exception:
        pass


def _run_on_main_thread(func, *args, timeout=30):
    """Schedule *func(*args)* on the Qt main thread and block until it
    finishes.  Returns the value returned by *func*.

    Slice_Triggered (and other timeline-mutating code) relies on Qt signals
    being delivered **synchronously** (direct connection) — specifically the
    IgnoreUpdates signal that prevents partial UI refreshes mid-transaction.
    When those signals are emitted from a background thread they become
    **queued** connections and arrive too late, leading to stale cached
    frames and visual glitches.  By routing the work through the main
    thread's event loop we get the same behaviour as a manual keyboard /
    mouse-driven slice.
    """
    if QThread is None:
        # Fallback: no Qt — just call directly (unit-test scenario)
        return func(*args)

    # If we are already on the main thread, run directly
    app = _get_app()
    if QThread.currentThread() is app.thread():
        return func(*args)

    result_box = [None]
    error_box = [None]
    done = threading.Event()

    dispatcher = _get_dispatcher()
    dispatcher._dispatch.emit((func, args, result_box, error_box, done))

    if not done.wait(timeout=timeout):
        raise TimeoutError(
            f"Main-thread operation did not complete within {timeout}s"
        )

    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


def _resolve_timeline_clip_for_tool(**kwargs):
    """Resolve target clip from timeline_clip_id, clip_query, or single-clip shortcut."""
    from classes.clip_resolver import resolve_timeline_clip

    def _do_resolve():
        pos_near = kwargs.get("position_near")
        if pos_near is None:
            pos_near = kwargs.get("prefer_position_near")
        occ = kwargs.get("occurrence", 0)
        try:
            occ = int(float(str(occ).strip() or 0))
        except (TypeError, ValueError):
            occ = 0
        return resolve_timeline_clip(
            timeline_clip_id=str(kwargs.get("timeline_clip_id") or "").strip(),
            clip_query=str(kwargs.get("clip_query") or "").strip(),
            prefer_track=str(kwargs.get("prefer_track") or kwargs.get("track") or "").strip(),
            track=str(kwargs.get("track") or kwargs.get("prefer_track") or "").strip(),
            position_near=pos_near,
            occurrence=occ,
        )

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            return _run_on_main_thread(_do_resolve)
    return _do_resolve()


def _resolve_clip_pair_for_tool(**kwargs):
    """Resolve adjacent clip pair with Qt main thread marshalling."""
    from classes.clip_resolver import resolve_clip_pair

    def _do_resolve():
        return resolve_clip_pair(
            clip_a_id=str(kwargs.get("clip_a_id") or "").strip(),
            clip_b_id=str(kwargs.get("clip_b_id") or "").strip(),
            clip_a_query=str(kwargs.get("clip_a_query") or "").strip(),
            clip_b_query=str(kwargs.get("clip_b_query") or "").strip(),
        )

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            return _run_on_main_thread(_do_resolve)
    return _do_resolve()


def _get_source_file_for_clip(clip_obj):
    try:
        from classes.query import File
        data = clip_obj.data if hasattr(clip_obj, "data") and isinstance(clip_obj.data, dict) else {}
        file_id = data.get("file_id")
        if file_id:
            f = File.get(id=str(file_id))
            if f:
                return f
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        path = reader.get("path")
        if path:
            return File.get(path=path)
    except Exception:
        return None
    return None


def _fmt_mmss(seconds: float) -> str:
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 0.0
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def _ffmpeg_run(args):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "ffmpeg failed")
        return True, ""
    except FileNotFoundError:
        return False, "ffmpeg not found."
    except Exception as e:
        return False, str(e)


def _ffprobe_video_duration(path) -> float:
    """Return the video duration in seconds, or 0.0 on error."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        val = (p.stdout or "").strip()
        if val and val != "N/A":
            return float(val)
        # Fallback: use format duration
        p2 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        val2 = (p2.stdout or "").strip()
        return float(val2) if val2 and val2 != "N/A" else 0.0
    except Exception:
        return 0.0


def _ffprobe_has_audio(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        return bool((p.stdout or "").strip())
    except Exception:
        return False


def _is_extreme_for_4_seconds(prompt):
    text = (prompt or "").strip().lower()
    if len(text) < 2:
        return True, "Prompt is too short."
    multi_markers = [
        "then ", "after that", "afterwards", "meanwhile", "next ",
        "cut to", "scene change", "montage", "several", "multiple",
        "a series of", "over the course of", "gradually",
        "time-lapse", "timelapse",
    ]
    if sum(1 for m in multi_markers if m in text) >= 2:
        return True, "Multiple steps/scenes."
    extreme_markers = [
        "explode", "nuke", "earthquake", "tsunami", "apocalypse",
        "destroy the city", "teleport", "time travel", "turn into",
        "transform into", "grow wings", "summon", "giant",
        "entire crowd", "army", "hundreds of", "thousands of",
    ]
    if any(m in text for m in extreme_markers):
        return True, "Too extreme for 4s."
    if len(text) > 240:
        return True, "Prompt too detailed for 4s."
    return False, ""


def _twelvelabs_search_in_window(index_id, query_text, *, page_limit=30, video_id=""):
    try:
        from classes.api_client import get_backend_client
        if not str(index_id or "").strip():
            return [], "TwelveLabs index_id is missing for this file."
        client = get_backend_client()
        resp = client.search(
            query=query_text,
            index_id=index_id,
            video_id=video_id,
            page_limit=page_limit,
            top_k=page_limit,
        )
        if isinstance(resp, dict) and resp.get("error"):
            return [], resp["error"]
        results = resp.get("results", []) if isinstance(resp, dict) else []
        items = [type("SearchItem", (), r)() for r in results]
        if video_id:
            items = [it for it in items if str(getattr(it, "video_id", "")) == str(video_id)]
        return items, None
    except Exception as e:
        return [], str(e)


def _output_path_for_generated_video(ext=".mp4"):
    """Return an absolute path for a new generated video (preview-safe)."""
    ext = ext if str(ext).startswith(".") else f".{ext}"
    if ext.lower() not in (".mp4", ".webm", ".mov", ".mkv"):
        ext = ".mp4"
    app = _get_app()
    project_path = getattr(app.project, "current_filepath", None) or ""
    if project_path and os.path.isabs(os.path.expanduser(str(project_path))):
        out_dir = os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(project_path))), "Generated")
        try:
            os.makedirs(out_dir, exist_ok=True)
            return os.path.join(out_dir, f"generated_{uuid_module.uuid4().hex[:12]}{ext}")
        except OSError:
            pass
    try:
        from classes import info
        out_dir = os.path.join(info.USER_PATH, "Generated")
        os.makedirs(out_dir, exist_ok=True)
        return os.path.join(out_dir, f"generated_{uuid_module.uuid4().hex[:12]}{ext}")
    except Exception:
        pass
    return os.path.join(tempfile.gettempdir(), f"zenvi_generated_{uuid_module.uuid4().hex[:12]}{ext}")


def _canonical_media_path(path):
    """Normalize to an absolute, expanded path for libopenshot and preview."""
    if not path:
        return path
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def _download_video_url_to_path(video_url: str, dest_path: str, timeout: int = 180) -> Optional[str]:
    """Download a remote generated video to dest_path. Returns None on success, else an error message."""
    if not video_url:
        return "No video URL returned from generation."
    try:
        import requests

        resp = requests.get(
            video_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Zenvi/1.0"},
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    fh.write(chunk)
        if os.path.getsize(dest_path) > 0:
            return None
        return "Downloaded file is empty"
    except Exception as exc:
        return f"Download failed: {exc}"


def _upload_generation_assets(client, seed_path=None, frame_specs=None):
    """Upload seed/frame files for remote generation; returns (seed_file_id, frame_images_paths, error)."""
    seed_file_id = None
    if seed_path and os.path.isfile(seed_path):
        uid = f"gen_seed_{uuid_module.uuid4().hex[:8]}"
        up = client.upload_media_file(seed_path, file_id=uid)
        if not up.get("success"):
            return None, None, up.get("error", "Failed to upload seed video")
        seed_file_id = uid

    frame_images_paths = []
    for idx, spec in enumerate(frame_specs or []):
        path = spec.get("path") or ""
        frame = spec.get("frame", "first")
        if not path or not os.path.isfile(path):
            continue
        uid = f"gen_frame_{idx}_{uuid_module.uuid4().hex[:6]}"
        up = client.upload_media_file(path, file_id=uid)
        if not up.get("success"):
            return None, None, up.get("error", f"Failed to upload frame: {path}")
        frame_images_paths.append({"file_id": uid, "frame": frame})

    return seed_file_id, frame_images_paths or None, None


# Kling O1 Pro via Runware — desktop-side constraints (mirror backend constants).
_KLING_O1_ALLOWED_DURATIONS = [5, 10]
_KLING_O1_MIN_DIM = 720
_KLING_O1_MAX_DIM = 2160


def _snap_kling_o1_duration(duration):
    """Snap to Kling O1 Pro duration: default 5s; use 10s only when clearly requested (>= 8)."""
    try:
        val = float(duration)
    except (TypeError, ValueError):
        return 5
    val = max(1, min(10, val))
    if val >= 8:
        return 10
    return 5


def _kling_o1_output_dims(width, height):
    """Snap arbitrary dimensions to Kling O1 Pro supported output or video-edit range."""
    w = int(width or 1920)
    h = int(height or 1080)
    if w < _KLING_O1_MIN_DIM or h < _KLING_O1_MIN_DIM:
        scale_f = max(_KLING_O1_MIN_DIM / max(w, 1), _KLING_O1_MIN_DIM / max(h, 1))
        w = int(w * scale_f)
        h = int(h * scale_f)
    w += w % 2
    h += h % 2
    if w > _KLING_O1_MAX_DIM or h > _KLING_O1_MAX_DIM:
        scale_d = min(_KLING_O1_MAX_DIM / max(w, 1), _KLING_O1_MAX_DIM / max(h, 1))
        w = int(w * scale_d)
        h = int(h * scale_d)
        w += w % 2
        h += h % 2
    return w, h


def _project_kling_o1_t2v_dims():
    """Resolve T2V width/height from project settings, snapped for Kling O1 Pro."""
    try:
        proj = _get_app().project
        w = int(proj.get("width") or 1920)
        h = int(proj.get("height") or 1080)
    except Exception:
        w, h = 1920, 1080
    aspect = w / max(h, 1)
    if aspect > 1.2:
        return 1920, 1080
    if aspect < 0.8:
        return 1080, 1920
    return 1440, 1440


def _kling_o1_scale_vf(width, height):
    """FFmpeg scale+pad filter for Kling O1 video-edit dimension range."""
    w, h = _kling_o1_output_dims(width, height)
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    ), w, h


# Context for add_clip_to_timeline (remembers last split file id per chat session)
_last_split_file_id_by_chat_session = {}


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------

def get_project_info(**_kw) -> str:
    try:
        app = _get_app()
        proj = app.project
        profile = proj.get("profile") or "unknown"
        fps = proj.get("fps") or {}
        fps_str = "{}/{}".format(fps.get("num", ""), fps.get("den", 1))
        duration = proj.get("duration") or 0
        scale = proj.get("scale") or 0
        return f"Project: profile={profile}, fps={fps_str}, duration={duration}, scale={scale}"
    except Exception as e:
        return f"Error: {e}"


def list_files(**_kw) -> str:
    try:
        import os
        from classes.query import File
        from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

        files = File.filter()
        if not files:
            return "No files in project."
        lines = []
        visible = 0
        for f in files:
            d = f.data if isinstance(f.data, dict) else {}
            if d.get("zenvi_subclip"):
                continue
            visible += 1
            name = d.get("name") or os.path.basename(str(d.get("path") or "")) or "?"
            dur = float(d.get("duration", 0) or 0)
            ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
            analyzed = bool(ai.get("analyzed"))
            indexed = twelvelabs_is_indexed(get_index_block(ai))
            preview = _summary_preview_for_file_data(d)
            lines.append(
                f"  media_bin_file_id={f.id} name={name!r} duration={dur:.2f}s "
                f"analyzed={analyzed} indexed={indexed} "
                f"summary_preview={preview!r} "
                f"path={os.path.basename(d.get('path', ''))}"
            )
        if not lines:
            return "No files in project."
        return f"Media bin files ({visible}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


_TAGS_PREVIEW_MAX = 80


def _summary_preview_for_file_data(file_data: dict, clip_data: dict | None = None) -> str:
    """Top objects/scenes from effective metadata for compact clip listing."""
    from classes.ai_metadata_utils import build_summary_preview, get_effective_ai_metadata

    if not isinstance(file_data, dict):
        return ""
    effective = get_effective_ai_metadata(file_data, clip_data=clip_data, rebased=True)
    return build_summary_preview(effective)


def _file_is_analyzed(file_data: dict) -> bool:
    if not isinstance(file_data, dict):
        return False
    ai = file_data.get("ai_metadata")
    if not isinstance(ai, dict):
        return False
    return bool(ai.get("analyzed"))


def list_clips(layer="", **_kw) -> str:
    try:
        from classes.query import Clip

        app = _get_app()
        layers_raw = app.project.get("layers") or []
        kwargs = {}
        if layer and str(layer).strip():
            resolved, err = normalize_track_or_layer_arg(str(layer).strip(), layers_raw)
            if err:
                return err
            kwargs["layer"] = resolved
        clips = Clip.filter(**kwargs)
        if not clips:
            return "No clips in project."
        from classes.timeline_clip_context import build_timeline_clip_context, clear_metadata_lookup_cache

        clear_metadata_lookup_cache()
        file_cache: dict[str, dict] = {}

        file_dupes: dict[str, list] = {}
        for c in clips:
            fid = str(c.data.get("file_id") or "")
            layer = c.data.get("layer")
            if fid:
                file_dupes.setdefault(f"{fid}:{layer}", []).append(c)

        lines = []
        for c in clips:
            d = c.data
            lid = d.get("layer", "")
            try:
                lid_int = int(lid) if lid != "" and lid is not None else None
            except (TypeError, ValueError):
                lid_int = None
            ui = layer_number_to_display_index(lid_int, layers_raw) if lid_int is not None else None
            ui_part = f" ui_track={ui}" if ui is not None else ""
            tids = (
                [
                    str(L.get("id", ""))
                    for L in layers_raw
                    if int(L.get("number") or 0) == lid_int
                ]
                if lid_int is not None
                else []
            )
            tid_part = f" track_id={tids[0]}" if tids and tids[0] else ""
            title = d.get("title") or d.get("label") or ""
            fid = d.get("file_id", "")
            fname = ""
            summary_preview = ""
            parent_file_id = ""
            source_start = d.get("start", 0)
            source_end = d.get("end", 0)
            timeline_end = float(d.get("position", 0) or 0)
            if fid:
                try:
                    from classes.query import File as _File
                    if fid not in file_cache:
                        fobj = _File.get(id=str(fid))
                        file_cache[fid] = fobj.data if fobj and isinstance(fobj.data, dict) else None
                    fdata = file_cache.get(fid)
                    if fdata:
                        import os
                        fname = (
                            fdata.get("name")
                            or os.path.basename(str(fdata.get("path") or ""))
                        )
                        ctx = build_timeline_clip_context(c, d, fdata, layers=layers_raw)
                        summary_preview = ctx.summary_preview
                        parent_file_id = ctx.parent_file_id
                        source_start = ctx.source_start
                        source_end = ctx.source_end
                        timeline_end = ctx.timeline_end
                except Exception:
                    pass
            summary_part = f" summary_preview={summary_preview!r}" if summary_preview else ""
            occ_hint = ""
            dup_key = f"{fid}:{lid_int}"
            dupes = file_dupes.get(dup_key, [])
            if len(dupes) > 1:
                ranked = sorted(dupes, key=lambda x: float(x.data.get("position", 0) or 0))
                for idx, dc in enumerate(ranked, 1):
                    if dc.id == c.id:
                        occ_hint = f" occurrence_hint={idx}"
                        break
            parent_part = f" parent_file_id={parent_file_id}" if parent_file_id and parent_file_id != str(fid) else ""
            lines.append(
                f"  timeline_clip_id={c.id} media_bin_file_id={fid}{parent_part} "
                f"title={title!r} file={fname!r}{summary_part}{occ_hint} "
                f"layer_number={lid_int if lid_int is not None else lid}{ui_part}{tid_part} "
                f"position={d.get('position',0)} timeline_end={timeline_end:.2f} "
                f"source_start={source_start} source_end={source_end}"
            )
        return f"Timeline clips ({len(clips)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def list_layers(**_kw) -> str:
    try:
        from classes.track_display import build_track_stack, track_stack_json

        layers = _get_app().project.get("layers") or []
        if not layers:
            return "No layers in project."
        stack = build_track_stack(layers)
        lock_by_num = {
            int(L.get("number") or 0): bool(L.get("lock", False)) for L in layers
        }
        n = len(stack)
        bottom = stack[0] if stack else {}
        top = stack[-1] if stack else {}
        lines = [
            f"Layers ({n}). Z-ORDER uses layer_number only (higher covers lower). "
            "Track labels/names are cosmetic — they can be anything and do NOT imply priority.",
            f"BOTTOM (drawn under): layer_number={bottom.get('layer_number')} "
            f"label={bottom.get('label')!r}",
            f"TOP (covers all below): layer_number={top.get('layer_number')} "
            f"label={top.get('label')!r}",
            "Stack bottom→top:",
        ]
        for e in stack:
            lines.append(
                f"  layer_number={e['layer_number']} ui_track={e['ui_track']} "
                f"z_from_bottom={e['z_from_bottom']} label={e.get('label')!r} "
                f"track_id={e.get('track_id')!r} "
                f"lock={lock_by_num.get(e['layer_number'], False)}"
            )
        lines.append(f"TRACK_STACK_JSON={track_stack_json(layers)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def list_markers(**_kw) -> str:
    try:
        from classes.query import Marker
        markers = Marker.filter()
        if not markers:
            return "No markers in project."
        lines = [f"  id={m.data.get('id','')} position={m.data.get('position',0)} name={m.data.get('name','')}" for m in markers]
        return f"Markers ({len(markers)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def new_project(**_kw) -> str:
    try:
        app = _get_app()
        app.project.new()
        app.updates.load(app.project._data, reset_history=True)
        return "New project created."
    except Exception as e:
        return f"Error: {e}"


def save_project(file_path="", **_kw) -> str:
    from classes import info
    if not file_path or not isinstance(file_path, str):
        return "Error: file_path is required."
    file_path = file_path.strip()
    if not file_path.endswith(info.ALL_PROJECT_EXTS):
        file_path += info.PROJECT_EXT
    try:
        _get_app().window.save_project(file_path)
        return f"Project saved to {file_path}."
    except Exception as e:
        return f"Error: {e}"


def open_project(file_path="", **_kw) -> str:
    if not file_path:
        return "Error: file_path is required."
    try:
        _get_app().window.OpenProjectSignal.emit(file_path.strip())
        return f"Open project requested: {file_path}."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Playback & history
# ---------------------------------------------------------------------------

_WATCH_CLIP_DEFAULT_PATH = os.path.expanduser(
    "~/Downloads/Feral - Concept Trailer.mp4"
)


def watch_clip_and_play(file_path: str = "", **_kw) -> str:
    """Import a video file into the media bin, add it to the timeline, and start playback.

    If file_path is empty, defaults to the Feral concept trailer test video.
    This is the handler for natural-language commands like 'watch clip',
    'play the feral trailer', 'show me the clip', etc.
    """
    try:
        resolved_path = (file_path or _WATCH_CLIP_DEFAULT_PATH).strip()
        if not os.path.isfile(resolved_path):
            return f"Error: File not found: {resolved_path}"

        from classes.query import File as _File
        from PyQt5.QtCore import QUrl as _QUrl

        app = _get_app()
        win = app.window

        # Step 1: Import into media bin (must run on main thread — uses libopenshot)
        def _do_import():
            existing = _File.get(path=resolved_path)
            if existing:
                return existing.id
            win.files_model.add_files([resolved_path], quiet=True, prevent_image_seq=True)
            added = _File.get(path=resolved_path)
            return added.id if added else None

        file_id = _run_on_main_thread(_do_import)
        if not file_id:
            return f"Error: Could not import file into media bin: {resolved_path}"

        # Step 2: Add to timeline (position 0, top video track)
        add_result = add_clip_to_timeline(file_id=file_id, position_seconds="0", **_kw)
        if add_result.startswith("Error"):
            return add_result

        # Step 3: Seek to start + play
        def _do_play():
            win.actionJumpStart_trigger()
            # Ensure player is playing (actionPlay_trigger toggles, so check mode)
            try:
                import openshot
                player = win.preview_thread.player
                if player.Mode() != openshot.PLAYBACK_PLAY:
                    win.actionPlay_trigger()
            except Exception:
                win.actionPlay_trigger()

        _run_on_main_thread(_do_play)
        return f"Loaded and playing: {os.path.basename(resolved_path)}"
    except Exception as e:
        log.error("watch_clip_and_play failed: %s", e, exc_info=True)
        return f"Error: {e}"


def play(**_kw) -> str:
    try:
        _get_app().window.actionPlay_trigger()
        return "Playback toggled."
    except Exception as e:
        return f"Error: {e}"


def go_to_start(**_kw) -> str:
    try:
        _get_app().window.actionJumpStart_trigger()
        return "Seeked to start."
    except Exception as e:
        return f"Error: {e}"


def go_to_end(**_kw) -> str:
    try:
        _get_app().window.actionJumpEnd_trigger()
        return "Seeked to end."
    except Exception as e:
        return f"Error: {e}"


def undo(**_kw) -> str:
    try:
        _get_app().updates.undo()
        return "Undo performed."
    except Exception as e:
        return f"Error: {e}"


def redo(**_kw) -> str:
    try:
        _get_app().updates.redo()
        return "Redo performed."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Timeline / view
# ---------------------------------------------------------------------------

def add_track(**_kw) -> str:
    try:
        _get_app().window.actionAddTrackBelow_trigger()
        return "Track added."
    except Exception as e:
        return f"Error: {e}"


def add_marker(**_kw) -> str:
    try:
        _get_app().window.actionAddMarker_trigger()
        return "Marker added."
    except Exception as e:
        return f"Error: {e}"


def remove_clip(**_kw) -> str:
    try:
        _get_app().window.actionRemoveClip_trigger()
        return "Selected clip(s) removed."
    except Exception as e:
        return f"Error: {e}"


def delete_clips_on_track(track: str = "", include_transitions: bool = True, **_kw) -> str:
    """
    Delete all clips on a UI track (Track 1..N bottom=1) or storage layer_number.
    Optionally also deletes timeline transitions/effects that sit on the same layer.

    Important: this is implemented as ONE atomic UpdateManager transaction so that
    a single undo restores the entire operation.
    """
    try:
        from classes.query import Clip, Transition

        app = _get_app()
        win = app.window

        layers = app.project.get("layers") or []
        if track is None or (isinstance(track, str) and not track.strip()):
            return "Error: track is required."

        layer_num, err = normalize_track_or_layer_arg(str(track).strip(), layers)
        if err:
            return err
        if layer_num is None:
            return "Error: Unknown track or layer."

        layer_num = int(layer_num)
        layers_out = app.project.get("layers") or []
        track_lbl = format_track_label_for_llm(layer_num, layers_out)

        # Respect locked tracks.
        for L in layers_out:
            try:
                if int(L.get("number") or 0) == layer_num and bool(L.get("lock", False)):
                    return f"Error: Track {track_lbl} is locked."
            except Exception:
                continue

        # One shared transaction id makes undo/redo atomic.
        tid = str(uuid_module.uuid4())
        app.updates.transaction_id = tid
        try:
            # Avoid stale selections pointing at soon-to-be-deleted objects.
            if hasattr(win, "clearSelections"):
                win.clearSelections()

            clips = Clip.filter(layer=layer_num)
            transitions = Transition.filter(layer=layer_num) if include_transitions else []

            # Delete transitions first (they may reference clip time ranges).
            for t in transitions:
                # Clear selection to reduce UI churn (doesn't affect history).
                try:
                    if hasattr(win, "removeSelection"):
                        win.removeSelection(t.id, "transition")
                except Exception:
                    pass
                t.delete()

            for c in clips:
                try:
                    if hasattr(win, "removeSelection"):
                        win.removeSelection(c.id, "clip")
                except Exception:
                    pass
                c.delete()

        finally:
            app.updates.transaction_id = None

        # Refresh preview frame to reflect the new timeline immediately.
        try:
            app.window.refreshFrameSignal.emit()
        except Exception:
            pass

        return (
            f"Deleted {len(clips)} clips and {len(transitions)} transitions on track {track_lbl} "
            f"(atomic undo)."
        )
    except Exception as e:
        return f"Error: {e}"


def zoom_in(**_kw) -> str:
    try:
        _get_app().window.actionTimelineZoomIn_trigger()
        return "Timeline zoomed in."
    except Exception as e:
        return f"Error: {e}"


def zoom_out(**_kw) -> str:
    try:
        _get_app().window.actionTimelineZoomOut_trigger()
        return "Timeline zoomed out."
    except Exception as e:
        return f"Error: {e}"


def center_on_playhead(**_kw) -> str:
    try:
        _get_app().window.actionCenterOnPlayhead_trigger()
        return "Centered on playhead."
    except Exception as e:
        return f"Error: {e}"


def import_files(**_kw) -> str:
    try:
        _get_app().window.actionImportFiles_trigger()
        return "Import files dialog opened."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_video(show_dialog="true", output_path="", **_kw) -> str:
    """Export the project. Opens the dialog by default; set show_dialog=false to render immediately."""
    try:
        open_ui = str(show_dialog).lower().strip() not in ("0", "false", "no")
        path = (output_path or "").strip()
        if open_ui and not path:
            _get_app().window.actionExportVideo_trigger()
            return "Export video dialog opened."
        from windows.export import export_video_headless, get_default_export_settings
        _, _, _, default_path = get_default_export_settings()
        err = export_video_headless(path or None, None, None, None)
        if err:
            return f"Export failed: {err}"
        return f"Exported to {path or default_path}."
    except Exception as e:
        return f"Error: {e}"


def get_export_settings(**_kw) -> str:
    try:
        from windows.export import get_default_export_settings
        app = _get_app()
        video_settings, audio_settings, export_type, default_path = get_default_export_settings()
        lines = [
            f"Export type: {export_type}",
            f"Default path: {default_path}",
            "Video: {}x{}, {}/{} fps, codec {}, format {}, bitrate {}".format(
                video_settings.get("width"), video_settings.get("height"),
                video_settings.get("fps", {}).get("num"), video_settings.get("fps", {}).get("den"),
                video_settings.get("vcodec"), video_settings.get("vformat"),
                video_settings.get("video_bitrate")),
            "Audio: codec {}, {} Hz, {} channels, bitrate {}".format(
                audio_settings.get("acodec"), audio_settings.get("sample_rate"),
                audio_settings.get("channels"), audio_settings.get("audio_bitrate")),
            "Frame range: {} - {}".format(video_settings.get("start_frame"), video_settings.get("end_frame")),
        ]
        overrides = app.project.get("export_overrides") or {}
        if overrides:
            lines.append(f"Overrides: {overrides}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def set_export_setting(key="", value="", **_kw) -> str:
    try:
        app = _get_app()
        overrides = dict(app.project.get("export_overrides") or {})
        kl = key.lower().strip()
        if kl in ("width", "height", "fps_num", "fps_den", "start_frame", "end_frame", "sample_rate", "channels"):
            overrides[kl] = int(value.strip())
        elif kl in ("video_codec", "vcodec"):
            overrides["video_codec"] = value.strip()
        elif kl in ("audio_codec", "acodec"):
            overrides["audio_codec"] = value.strip()
        elif kl in ("output_path", "path"):
            overrides["output_path"] = value.strip()
        elif kl in ("vformat", "format"):
            overrides["vformat"] = value.strip()
        else:
            overrides[kl] = value.strip()
        from classes.app import get_app
        get_app().updates.ignore_history = True
        app.updates.update(["export_overrides"], overrides)
        get_app().updates.ignore_history = False
        return f"Set {kl} = {value}."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Clipping (split, slice, add to timeline)
# ---------------------------------------------------------------------------

def get_file_info(file_id="", **_kw) -> str:
    try:
        from classes.query import File
        if not file_id:
            return "Error: file_id is required."
        f = File.get(id=file_id.strip())
        if not f:
            return f"Error: File not found for id={file_id}."
        fps_data = f.data.get("fps") or {}
        fps_num = int(fps_data.get("num", 30))
        fps_den = int(fps_data.get("den", 1))
        video_length = int(f.data.get("video_length", 0))
        return f"file_id={file_id} path={f.data.get('path','')} fps={fps_num}/{fps_den} video_length={video_length}"
    except Exception as e:
        return f"Error: {e}"


def split_file_add_clip(file_id="", start_frame=0, end_frame=0, name="", **_kw) -> str:
    try:
        from classes.query import File
        from classes import time_parts
        from classes.ai_metadata_utils import get_effective_ai_metadata, filter_tags_string_for_window

        chat_session_id = str(_kw.get("chat_session_id", "") or "default")

        if not file_id:
            return "Error: file_id is required."
        file_id = str(file_id).strip()
        start_frame = int(start_frame)
        end_frame = int(end_frame)
        f = File.get(id=file_id)
        if not f:
            return f"Error: File not found for id={file_id}."
        fps_data = f.data.get("fps") or {}
        fps_num = int(fps_data.get("num", 30))
        fps_den = int(fps_data.get("den", 1))
        fps = float(fps_num) / float(fps_den) if fps_den else 0.0
        if fps <= 0:
            return "Error: Invalid fps."
        video_length = int(f.data.get("video_length", 0))
        if start_frame < 1 or end_frame < 1:
            return "Error: Frames are 1-based."
        if start_frame >= end_frame:
            return "Error: start_frame must be < end_frame."
        if end_frame > video_length:
            return f"Error: end_frame {end_frame} > video_length {video_length}."

        previous_start = float(f.data.get("start", 0.0))
        start_sec = previous_start + (start_frame - 1) / fps
        end_sec = previous_start + end_frame / fps
        new_file = File()
        new_file.data = copy.deepcopy(f.data)
        new_file.data.pop("name", None)
        new_file.id = None
        new_file.key = None
        new_file.type = "insert"
        new_file.data["start"] = start_sec
        new_file.data["end"] = end_sec
        new_file.data["parent_file_id"] = file_id

        if "ai_metadata" in new_file.data and new_file.data["ai_metadata"].get("analyzed"):
            from classes.timeline_clip_context import resolve_root_ai_metadata
            from classes.ai_metadata_utils import materialize_clip_ai_metadata

            root_ai, _ = resolve_root_ai_metadata(f.data, file_id=file_id)
            if root_ai:
                effective = materialize_clip_ai_metadata(
                    root_ai, start_sec, end_sec, rebased=True,
                )
            else:
                effective = get_effective_ai_metadata(
                    f.data,
                    clip_data={"start": start_sec, "end": end_sec},
                    rebased=True,
                )
            new_file.data["ai_metadata"] = effective
            if new_file.data.get("tags"):
                new_file.data["tags"] = filter_tags_string_for_window(
                    str(new_file.data.get("tags") or ""),
                    effective,
                )

        if name and isinstance(name, str) and name.strip():
            new_file.data["name"] = name.strip()
        else:
            global_frame = round(previous_start * fps) + start_frame
            t = time_parts.secondsToTime((global_frame - 1) / fps, fps_num, fps_den)
            timestamp = "{}:{}:{}:{}".format(t["hour"], t["min"], t["sec"], t["frame"])
            base = os.path.splitext(os.path.basename(f.data.get("path") or f.data.get("name", "clip")))[0]
            new_file.data["name"] = f"{base} ({timestamp})"
        # Mark as agent-created subclip so it's hidden from the project files panel
        new_file.data["zenvi_subclip"] = True
        new_file.save()
        _last_split_file_id_by_chat_session[chat_session_id] = new_file.id
        clip_name = new_file.data.get("name", "")
        return (
            f'Subclip created: "{clip_name}" (file_id={new_file.id}) '
            f'from frames {start_frame}–{end_frame}. '
            f'Call add_clip_to_timeline_tool(file_id="{new_file.id}") to place it on the timeline.'
        )
    except Exception as e:
        return f"Error: {e}"


def add_clip_to_timeline(
    file_id="",
    position_seconds="",
    track="",
    duration_seconds="",
    start_seconds="",
    **_kw,
) -> str:
    try:
        from classes.query import File, Track, Clip

        chat_session_id = str(_kw.get("chat_session_id", "") or "default")

        if not file_id or (isinstance(file_id, str) and not file_id.strip()):
            file_id = _last_split_file_id_by_chat_session.get(chat_session_id)
            if not file_id:
                return (
                    "Error: No clip was just created. "
                    "Pass tool_args.file_id with a media_bin file id, or run "
                    "split_file_add_clip_tool / import_stock_media_tool immediately before this "
                    "step (empty file_id only works right after those tools in the same session)."
                )
        else:
            file_id = str(file_id).strip()
        f = File.get(id=file_id)
        if not f:
            return f"Error: File not found for id={file_id}."
        app = _get_app()
        win = app.window
        fps = app.project.get("fps") or {}
        fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)

        # Detect audio-only files (mp3, wav, ogg, etc. or media_type=="audio")
        file_data = f.data
        _ext = (file_data.get("path") or "").rsplit(".", 1)[-1].lower()
        _audio_exts = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"}
        _is_audio_only = (
            file_data.get("media_type", "") == "audio"
            or _ext in _audio_exts
            or (not file_data.get("has_video", True) and file_data.get("has_audio", False))
        )

        # Optional trim window (stock / beat placement)
        trim_dur = None
        trim_start = 0.0
        if str(start_seconds or "").strip():
            try:
                trim_start = max(0.0, float(start_seconds))
            except (TypeError, ValueError):
                trim_start = 0.0
        if str(duration_seconds or "").strip():
            try:
                trim_dur = max(0.0, float(duration_seconds))
            except (TypeError, ValueError):
                trim_dur = None

        # Determine track FIRST so we can compute position relative to that layer
        if not track or (isinstance(track, str) and not track.strip()):
            layers = app.project.get("layers") or []
            if _is_audio_only:
                track_num = default_underlay_layer_number(layers, audio=True)
            else:
                selected = getattr(win, "selected_tracks", []) or []
                if selected:
                    t = Track.get(id=selected[0])
                    track_num = int(t.data.get("number", 1)) if t else 1
                else:
                    # Video underlay default: lowest layer (bottom) so stock/B-roll
                    # does not cover main footage on higher layers.
                    track_num = default_underlay_layer_number(layers)
        else:
            layers_for_track = app.project.get("layers") or []
            resolved, err = normalize_track_or_layer_arg(str(track).strip(), layers_for_track)
            if err:
                return err
            track_num = resolved

        if not position_seconds or (isinstance(position_seconds, str) and not position_seconds.strip()):
            if _is_audio_only:
                # Audio: always start at position 0 so music covers the whole timeline
                pos_sec = 0.0
            else:
                # Video: append after the last clip on THIS SAME LAYER to avoid cross-track interference
                same_layer = [c for c in Clip.filter() if c.data.get("layer", 0) == track_num]
                # 1-frame buffer to prevent adjacent clips from touching (snap-to-grid rounding
                # can otherwise cause the new clip to slightly overlap the previous one)
                _one_frame = 1.0 / max(fps_float, 1.0)
                if same_layer:
                    last_end = max(
                        c.data.get("position", 0) + (c.data.get("end", 0) - c.data.get("start", 0))
                        for c in same_layer
                    )
                    pos_sec = last_end + _one_frame
                else:
                    pos_sec = 0.0
        else:
            pos_sec = float(position_seconds)

        if QPointF is None:
            from PyQt5.QtCore import QPointF as _QPointF
            pos = _QPointF(pos_sec, 0.0)
        else:
            pos = QPointF(pos_sec, 0.0)

        result_box = [None]

        def _do_add():
            new_clip = win.timeline.addClip(file_id, pos, track_num)
            if new_clip and trim_dur is not None and trim_dur > 0:
                # Clamp trim to source length
                try:
                    from classes.ai_metadata_utils import get_source_window
                    src_start, src_end = get_source_window({}, file_data)
                    source_len = max(0.0, float(src_end) - float(src_start))
                except Exception:
                    try:
                        source_len = float(file_data.get("duration") or 0)
                    except (TypeError, ValueError):
                        source_len = 0.0
                if source_len <= 0:
                    try:
                        source_len = float((file_data.get("reader") or {}).get("duration") or 0)
                    except (TypeError, ValueError):
                        source_len = 0.0

                file_start = float(file_data.get("start") or 0.0)
                start_sec, end_sec = compute_clip_trim_bounds(
                    source_len,
                    trim_start=trim_start,
                    trim_dur=trim_dur,
                    file_start=file_start,
                    min_duration=1.0 / max(fps_float, 1.0),
                )

                new_clip["start"] = start_sec
                new_clip["end"] = end_sec
                new_clip["duration"] = max(0.0, end_sec - start_sec)
                win.timeline.update_clip_data(
                    new_clip, only_basic_props=False, ignore_refresh=False
                )
            result_box[0] = new_clip

        _run_on_main_thread(_do_add)

        _last_split_file_id_by_chat_session.pop(chat_session_id, None)
        layers_out = app.project.get("layers") or []
        track_lbl = format_track_label_for_llm(int(track_num), layers_out)
        placed = result_box[0] or {}
        eff_dur = None
        try:
            if placed:
                eff_dur = float(placed.get("end", 0)) - float(placed.get("start", 0))
        except (TypeError, ValueError):
            eff_dur = None
        dur_part = f" duration={eff_dur:.2f}s" if eff_dur is not None and eff_dur > 0 else ""
        clip_id = placed.get("id", "") if isinstance(placed, dict) else ""
        id_part = f" timeline_clip_id={clip_id}" if clip_id else ""
        return (
            f"Added clip to timeline at position {pos_sec}s on track {track_lbl}"
            f"{dur_part}{id_part}."
        )
    except Exception as e:
        return f"Error: {e}"


def slice_clip_at_playhead(**_kw) -> str:
    try:
        from windows.views.timeline_backend.enums import MenuSlice

        # Read state and perform the slice entirely on the main thread
        result_box = [None]

        def _do_slice():
            from classes.query import Clip, Transition
            app = _get_app()
            win = app.window
            fps = app.project.get("fps") or {}
            fps_float = float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)
            playhead_position = float(win.preview_thread.current_frame - 1) / fps_float
            intersecting_clips = Clip.filter(intersect=playhead_position)
            intersecting_trans = Transition.filter(intersect=playhead_position)
            if not intersecting_clips and not intersecting_trans:
                result_box[0] = "No clip or transition at the playhead."
                return
            win.slice_clips(MenuSlice.KEEP_BOTH)
            n = len(intersecting_clips) + len(intersecting_trans)
            result_box[0] = f"Sliced {n} item(s) at the playhead; both sides kept."

        _run_on_main_thread(_do_slice)

        return result_box[0] or "Slice completed."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Search (project-wide video index + in-clip scenes)
# ---------------------------------------------------------------------------

def get_project_catalog(**_kw) -> str:
    """Orientation pass: list short summaries for all indexed project media."""
    try:
        from classes.api_client import get_backend_client
        from classes.app import get_app

        project_id = ""
        try:
            project_id = str(get_app().project.get("id") or "")
        except Exception:
            pass
        if not project_id:
            return "Error: project_id unavailable."
        client = get_backend_client()
        data = client.get_project_catalog(project_id)
        if data.get("error"):
            return f"Error: {data['error']}"
        items = data.get("items") or []
        if not items:
            return (
                "Catalog is empty — index/summarize project videos, images, and audio first, "
                "then call get_project_catalog_tool again."
            )
        lines = [f"Project catalog ({len(items)} media items):"]
        for it in items:
            name = it.get("filename") or it.get("file_id") or it.get("video_id")
            summary = (it.get("short_summary") or "").strip() or "(no summary)"
            mt = str(it.get("media_type") or "video")
            dur = it.get("duration_sec")
            dur_s = f" [{dur:.1f}s]" if isinstance(dur, (int, float)) and mt != "image" else ""
            lines.append(
                f"- [{mt}] {name}{dur_s} file_id={it.get('file_id')}: {summary}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


_ORDINAL_MAP = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
}


def _detect_ordinal(query: str) -> int:
    words = (query or "").lower().split()
    for word in words:
        if word in _ORDINAL_MAP:
            return _ORDINAL_MAP[word]
    return 0


def search_clips(query="", top_k="5", **_kw) -> str:
    """Project-wide video index search on this project's shared index.

    Returns media_bin_file_id + timestamp (deeper than Gemini tags).
    """
    q = str(query or "").strip()
    if not q:
        return "Error: query is required."
    try:
        k = int(float(top_k)) if str(top_k).strip() else 5
    except Exception:
        k = 5
    k = max(1, min(k, 20))

    try:
        from collections import defaultdict

        from classes.api_client import get_backend_client
        from classes.project_tl_index import (
            collect_project_twelvelabs_index,
            map_search_hit_to_file,
        )
        from classes.twelvelabs_match import compute_cut_timestamp

        info = collect_project_twelvelabs_index()
        if info.get("error") and not info.get("index_id"):
            return (
                f"Error: {info['error']} "
                "Index/summarize project videos first, then search again."
            )
        index_id = str(info.get("index_id") or "").strip()
        if not index_id:
            return (
                "Error: No project video index_id on project files. "
                "Reindex clips so they share the project index, then retry."
            )
        video_map = info.get("video_map") or {}
        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Error: Video indexing is not configured on the backend."

        page_limit = max(30, k * 10)
        resp = client.search(
            q,
            top_k=page_limit,
            index_id=index_id,
            page_limit=page_limit,
        )
        if resp.get("error"):
            return f"Error: {resp['error']}"
        results = resp.get("results") or []
        if not results:
            return (
                f"No index matches for '{q}' in this project's index "
                f"({info.get('index_name') or index_id}, "
                f"{info.get('indexed_count', 0)} indexed media item(s)). "
                "Try a more specific description, or check indexing finished."
            )

        requested_nth = _detect_ordinal(q)
        grouped: dict = defaultdict(list)
        for r in results:
            if not isinstance(r, dict):
                continue
            vid = str(r.get("video_id") or "").strip()
            fid, fname = map_search_hit_to_file(r, video_map)
            key = fid or vid or fname or "unknown"
            grouped[key].append({**r, "_file_id": fid, "_fname": fname, "_vid": vid})

        lines = [
            f"Found {len(results)} match(es) across {len(grouped)} project media item(s) "
            f"(index_id={index_id}, index_name={info.get('index_name') or ''}):",
        ]
        shown = 0
        for key, hits in grouped.items():
            if shown >= k and requested_nth == 0:
                break
            fid = hits[0].get("_file_id") or ""
            fname = hits[0].get("_fname") or key
            vid = hits[0].get("_vid") or ""
            mt = str(hits[0].get("media_type") or "video")
            id_part = f" media_bin_file_id={fid}" if fid else " media_bin_file_id=(unmapped)"
            vid_part = f" twelvelabs_video_id={vid}" if vid else ""
            type_part = f" media_type={mt}"

            hits_sorted = sorted(hits, key=lambda x: float(x.get("start") or 0))
            if len(hits_sorted) == 1 and requested_nth == 0:
                r = hits_sorted[0]
                cut = compute_cut_timestamp(
                    float(r.get("start") or 0),
                    float(r.get("end") or 0),
                    mode="start",
                )
                lines.append(
                    f"  • {fname}{id_part}{vid_part}{type_part} — timestamp {_fmt_mmss(cut)} "
                    f"(segment {_fmt_mmss(float(r.get('start') or 0))}-"
                    f"{_fmt_mmss(float(r.get('end') or 0))}, rank={r.get('rank')})"
                )
                shown += 1
                continue

            if requested_nth > 0:
                idx = min(requested_nth - 1, len(hits_sorted) - 1)
                r = hits_sorted[idx]
                cut = compute_cut_timestamp(
                    float(r.get("start") or 0),
                    float(r.get("end") or 0),
                    mode="start",
                )
                lines.append(
                    f"  • {fname}{id_part}{vid_part} — occurrence #{requested_nth} "
                    f"at timestamp {_fmt_mmss(cut)} "
                    f"(segment {_fmt_mmss(float(r.get('start') or 0))}-"
                    f"{_fmt_mmss(float(r.get('end') or 0))})"
                )
                shown += 1
            else:
                lines.append(
                    f"  • {fname}{id_part}{vid_part} — {len(hits_sorted)} occurrences:"
                )
                for i, r in enumerate(hits_sorted[:8], 1):
                    cut = compute_cut_timestamp(
                        float(r.get("start") or 0),
                        float(r.get("end") or 0),
                        mode="start",
                    )
                    lines.append(
                        f"      {i}. timestamp {_fmt_mmss(cut)} "
                        f"(segment {_fmt_mmss(float(r.get('start') or 0))}-"
                        f"{_fmt_mmss(float(r.get('end') or 0))}, rank={r.get('rank')})"
                    )
                if len(hits_sorted) > 1:
                    lines.append(
                        "      Multiple matches — specify which occurrence "
                        "(e.g. 'the 1st time', 'the 2nd time')."
                    )
                shown += 1

        unmapped = sum(1 for hits in grouped.values() if not hits[0].get("_file_id"))
        if unmapped:
            lines.append(
                f"Note: {unmapped} hit group(s) had no media_bin_file_id mapping — "
                "reindex those files into this project index."
            )
        return "\n".join(lines)
    except Exception as e:
        log.error("search_clips: %s", e, exc_info=True)
        return f"Error: {e}"


def search_clip_scenes(
    query="",
    top_k="5",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    try:
        k = int(float(top_k)) if str(top_k).strip() else 5
    except Exception:
        k = 5

    try:
        from classes.ai_metadata_utils import get_effective_ai_metadata
        from classes.api_client import get_backend_client
        from classes.timeline_clip_context import build_timeline_clip_context, resolve_parent_file_data
        from classes.twelvelabs_match import select_hits_for_display, get_index_block

        resolved = _resolve_timeline_clip_for_tool(
            clip_query=clip_query,
            timeline_clip_id=timeline_clip_id,
            **_kw,
        )
        if not resolved.ok or not resolved.clip:
            return resolved.error or "Error: Could not resolve timeline clip."

        clip_obj = resolved.clip
        clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        source_file = _get_source_file_for_clip(clip_obj)
        file_data = source_file.data if source_file and isinstance(source_file.data, dict) else None
        ctx = build_timeline_clip_context(clip_obj, clip_data, file_data)
        clip_start = ctx.source_start
        clip_end = ctx.source_end
        clip_name = ctx.title or "Timeline clip"

        per_clip_ai = clip_data.get("ai_metadata") if isinstance(clip_data.get("ai_metadata"), dict) else None
        parent_data = resolve_parent_file_data(file_data, file_id=ctx.file_id)
        source_ai = None
        if parent_data:
            source_ai = parent_data.get("ai_metadata") if isinstance(parent_data.get("ai_metadata"), dict) else None

        client = get_backend_client()
        nth = _parse_occurrence(str(_kw.get("occurrence", "0")), query)

        # TwelveLabs search (parent index + trim window)
        if client.is_indexing_configured():
            tw = get_index_block(source_ai or {})
            status = (tw.get("status") or "").lower()
            index_id = tw.get("index_id") or ""
            video_id = tw.get("video_id") or ""

            if status == "ready" and index_id and video_id:
                search_query = _semantic_search_query(query)
                items, err = _tl_search_items_in_window(
                    str(index_id), search_query, page_limit=max(30, k * 10), video_id=str(video_id),
                )
                if not err and items:
                    matches = select_hits_for_display(
                        items,
                        clip_start=clip_start,
                        clip_end=clip_end,
                        occurrence=nth,
                        top_k=k,
                    )
                    if matches:
                        lines = [
                            f"Index matches in '{clip_name}' "
                            f"({_fmt_mmss(clip_start)} - {_fmt_mmss(clip_end)}):"
                        ]
                        for m in matches:
                            rel_cut = m["cut_source"] - clip_start
                            rel_seg_start = m["start"] - clip_start
                            rel_seg_end = m["end"] - clip_start
                            lines.append(
                                f"- timestamp {_fmt_mmss(rel_cut)}"
                                f" (segment {_fmt_mmss(rel_seg_start)}-{_fmt_mmss(rel_seg_end)},"
                                f" rank={m.get('rank')}, overlap={m['overlap_ratio']:.2f})"
                            )
                            if m.get("transcription"):
                                lines.append(
                                    f"  transcript: {str(m['transcription']).strip()[:180]}"
                                )
                        return "\n".join(lines)

                # Broader project search filtered to this video before tag fallback
                search_query = _semantic_search_query(query)
                broad_items, broad_err = _tl_search_items_in_window(
                    str(index_id), search_query, page_limit=max(50, k * 15), video_id="",
                )
                if not broad_err and broad_items:
                    filtered = [
                        it for it in broad_items
                        if str(it.get("video_id") or it.get("twelvelabs_video_id") or "") == str(video_id)
                    ]
                    if filtered:
                        matches = select_hits_for_display(
                            filtered,
                            clip_start=clip_start,
                            clip_end=clip_end,
                            occurrence=nth,
                            top_k=k,
                        )
                        if matches:
                            lines = [
                                f"Index matches in '{clip_name}' "
                                f"({_fmt_mmss(clip_start)} - {_fmt_mmss(clip_end)}):"
                            ]
                            for m in matches:
                                rel_cut = m["cut_source"] - clip_start
                                lines.append(f"- timestamp {_fmt_mmss(rel_cut)} (project search)")
                            return "\n".join(lines)

        # Local chapter / description fallback (Pegasus chapters or legacy scenes)
        local_ai = per_clip_ai
        if local_ai is None:
            local_ai = get_effective_ai_metadata(
                parent_data or file_data,
                clip_data=clip_data,
                rebased=True,
            )

        candidates = []
        for ch in (local_ai or {}).get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            summary = (ch.get("summary") or ch.get("title") or "").strip()
            if not summary:
                continue
            candidates.append({
                "time": float(ch.get("start", 0.0) or 0.0),
                "description": summary,
            })
        for s in (local_ai or {}).get("scene_descriptions") or []:
            if not isinstance(s, dict):
                continue
            desc = (s.get("description") or "").strip()
            if not desc:
                continue
            candidates.append({
                "time": float(s.get("time", 0.0) or 0.0),
                "description": desc,
            })
        # Also score transcript / sounds as whole-clip hints (time=0)
        for key in ("transcript", "sounds", "description", "short_summary"):
            text = str((local_ai or {}).get(key) or "").strip()
            if text:
                candidates.append({"time": float(clip_start or 0.0), "description": text[:500]})

        if not candidates:
            return "No matches found."
        scored = []
        q_lower = query.lower()
        for s in candidates:
            desc = (s.get("description") or "").strip()
            if not desc:
                continue
            score = 0.0
            if q_lower in desc.lower():
                score = 10.0
            else:
                q_words = set(q_lower.split())
                d_words = set(desc.lower().split())
                overlap = len(q_words & d_words)
                if overlap:
                    score = overlap / (len(q_words) ** 0.5 * len(d_words) ** 0.5)
            if score > 0:
                scored.append({"time": float(s.get("time", 0.0)), "description": desc, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:k]
        if not results:
            return "No matches found."
        lines = [f"Description matches in '{clip_name}':"]
        for r in results:
            lines.append(f"- [{_fmt_mmss(r['time'])}] score={r['score']:.3f}: {r['description'][:200]}")
        return "\n".join(lines)
    except Exception as e:
        log.error("search_clip_scenes: %s", e, exc_info=True)
        return f"Error: {e}"


_ORDINAL_MAP = {
    "first": 1, "1st": 1, "one": 1,
    "second": 2, "2nd": 2, "two": 2,
    "third": 3, "3rd": 3, "three": 3,
    "fourth": 4, "4th": 4, "four": 4,
    "fifth": 5, "5th": 5, "five": 5,
}


def _parse_occurrence(occurrence_str: str, query: str) -> int:
    """Return 1-based occurrence index (0 = best overlap+rank match)."""
    try:
        n = int(float(str(occurrence_str).strip()))
        if n > 0:
            return n
    except Exception:
        pass
    q_lower = (query or "").lower()
    for word, n in _ORDINAL_MAP.items():
        if word in q_lower.split():
            return n
    return 0


def _semantic_search_query(query: str) -> str:
    """Strip ordinal words so TwelveLabs search uses semantic content only."""
    if not query or not str(query).strip():
        return ""
    kept = []
    for word in str(query).split():
        bare = word.lower().strip(".,;:!?\"'")
        if bare in _ORDINAL_MAP:
            continue
        kept.append(word)
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else str(query).strip()


def _audio_biased_tl_query(query: str, source_ai=None) -> str:
    """Build an audio/dialogue-oriented TwelveLabs query."""
    q = _semantic_search_query(query)
    if not q:
        return "spoken dialogue"
    import re
    if re.search(r"[^\x00-\x7F]", q):
        return f"spoken words: {q}"
    return f"spoken dialogue about {q}"


def _tl_search_items_in_window(index_id, query_text, *, page_limit=30, video_id=""):
    """Run TL search; on zero hits retry with audio-biased query."""
    items, err = _twelvelabs_search_in_window(
        index_id, query_text, page_limit=page_limit, video_id=video_id,
    )
    if err or items:
        return items, err
    audio_q = _audio_biased_tl_query(query_text)
    if audio_q != query_text:
        return _twelvelabs_search_in_window(
            index_id, audio_q, page_limit=page_limit, video_id=video_id,
        )
    return items, err


def _scene_description_cut_source(
    clip_start: float,
    clip_end: float,
    source_ai,
    query: str,
    occurrence: int,
    per_clip_ai=None,
):
    """Return a source-file cut time from Pegasus chapters or legacy scenes, or None."""
    from classes.ai_metadata_utils import adjust_scene_descriptions_for_subclip

    local_ai = per_clip_ai
    if local_ai is None and source_ai is not None:
        local_ai = adjust_scene_descriptions_for_subclip(source_ai, clip_start, clip_end)

    candidates = []
    for ch in (local_ai or {}).get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        summary = (ch.get("summary") or ch.get("title") or "").strip()
        if not summary:
            continue
        candidates.append({"time": float(ch.get("start", 0.0) or 0.0), "description": summary})
    for s in (local_ai or {}).get("scene_descriptions") or []:
        if not isinstance(s, dict):
            continue
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        candidates.append({"time": float(s.get("time", 0.0) or 0.0), "description": desc})

    if not candidates:
        return None
    q_lower = (query or "").lower()
    scored = []
    for s in candidates:
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        t = float(s.get("time", 0.0) or 0.0)
        if t < clip_start - 1e-3 or t > clip_end + 1e-3:
            continue
        score = 0.0
        if q_lower and q_lower in desc.lower():
            score = 10.0
        elif q_lower:
            qw = set(q_lower.split())
            dw = set(desc.lower().split())
            overlap = len(qw & dw)
            if overlap:
                score = overlap / (len(qw) ** 0.5 * len(dw) ** 0.5)
        else:
            score = 0.01
        if score > 0:
            scored.append((t, score))
    if not scored:
        return None
    scored.sort(key=lambda x: x[1], reverse=True)
    if occurrence and occurrence > 0:
        idx = min(occurrence, len(scored)) - 1
        return scored[idx][0]
    return scored[0][0]


def _slice_at_source_cut(
    clip_id_str: str,
    clip_start: float,
    clip_end: float,
    clip_pos: float,
    cut_source: float,
    *,
    label: str = "match",
) -> str:
    from classes.twelvelabs_match import snap_source_time_to_frame, snap_timeline_position
    from windows.views.timeline_backend.enums import MenuSlice

    fps = _get_app().project.get("fps") or {}
    fps_num = float(fps.get("num", 30))
    fps_den = float(fps.get("den", 1)) or 1.0
    cut_source = snap_source_time_to_frame(float(cut_source), fps_num, fps_den)
    slice_pos = snap_timeline_position(
        clip_pos + (cut_source - clip_start), fps_num, fps_den,
    )
    clip_timeline_end = clip_pos + (clip_end - clip_start)
    if slice_pos <= clip_pos or slice_pos >= clip_timeline_end:
        return (
            f"Error: Computed slice position ({slice_pos:.3f}s) is outside "
            f"the clip range [{clip_pos:.3f}s – {clip_timeline_end:.3f}s]."
        )
    slice_error_box = [None]

    def _do_slice():
        try:
            _get_app().window.timeline.Slice_Triggered(
                MenuSlice.KEEP_BOTH, [clip_id_str], [], slice_pos,
            )
        except Exception as exc:
            slice_error_box[0] = str(exc)

    _run_on_main_thread(_do_slice)
    if slice_error_box[0]:
        return f"Error during slice: {slice_error_box[0]}"
    return f"Sliced at {_fmt_mmss(cut_source - clip_start)} ({label})."


def _parse_mmss_or_hhmmss_token(tok: str):
    """Return seconds for 'SS', 'M:SS', or 'H:M:SS' tokens, else None."""
    if not tok or not isinstance(tok, str):
        return None
    tok = tok.strip()
    if not tok:
        return None
    if ":" not in tok:
        try:
            return float(tok)
        except ValueError:
            return None
    parts = tok.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return nums[0] * 60.0 + nums[1]
    return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]


def _parse_explicit_source_time_range_sec(query: str):
    """If *query* names a concrete time range in source seconds, return (t0, t1).

    t0/t1 are absolute times in the same frame as timeline clip start/end (source media).
    Returns None when no explicit numeric range is detected (caller uses semantic search).
    """
    if not query or not isinstance(query, str):
        return None
    s = query.strip()
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\s+to\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*s\b\s+to\s+(\d+(?:\.\d+)?)\s*s\b",
        s,
        re.IGNORECASE,
    )
    if m:
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 > t0:
            return (t0, t1)
    m = re.search(
        r"(\d+:\d{2}(?::\d{2})?)\s+to\s+(\d+:\d{2}(?::\d{2})?)",
        s,
        re.IGNORECASE,
    )
    if m:
        t0 = _parse_mmss_or_hhmmss_token(m.group(1))
        t1 = _parse_mmss_or_hhmmss_token(m.group(2))
        if t0 is not None and t1 is not None and t1 > t0:
            return (t0, t1)
    if re.search(r"\bsec", s, re.IGNORECASE):
        m = re.search(
            r"from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\b",
            s,
            re.IGNORECASE,
        )
        if m:
            t0, t1 = float(m.group(1)), float(m.group(2))
            if t1 > t0:
                return (t0, t1)
    return None


def _slice_timeline_clip_at_source_times(
    clip_id_str: str,
    clip_start: float,
    clip_end: float,
    clip_pos: float,
    layer_num: int,
    file_id_str: str,
    t0: float,
    t1: float,
) -> str:
    """Slice once or twice so the middle segment is approximately source [t0, t1]."""
    from windows.views.timeline_backend.enums import MenuSlice
    from classes.query import Clip

    app = _get_app()
    win = app.window
    fps = app.project.get("fps") or {}
    fps_num = float(fps.get("num", 30))
    fps_den = float(fps.get("den", 1)) or 1.0

    def snap(pos: float) -> float:
        return float(round((float(pos) * fps_num) / fps_den) * fps_den) / fps_num

    eps = 1e-3
    clip_tl_end = clip_pos + (clip_end - clip_start)

    def _find_right_segment(after_pos: float, src_start: float) -> Optional[str]:
        best_id, best_score = None, 1e9
        for c in Clip.filter():
            try:
                c_ly = int(float(c.data.get("layer", 0) or 0))
            except (TypeError, ValueError):
                continue
            if c_ly != int(layer_num):
                continue
            try:
                p = float(c.data.get("position", 0))
                st = float(c.data.get("start", 0))
            except (TypeError, ValueError):
                continue
            if file_id_str and str(c.data.get("file_id") or "") != file_id_str:
                continue
            score = abs(p - after_pos) + abs(st - src_start)
            if score < best_score:
                best_score = score
                best_id = str(c.id)
        if best_id is not None and best_score < 0.25:
            return best_id
        return None

    # Degenerate: full clip
    if t0 <= clip_start + eps and t1 >= clip_end - eps:
        return "Nothing to slice: the requested range spans the whole clip."

    # Only upper boundary inside clip
    if t0 <= clip_start + eps:
        pos2 = snap(clip_pos + (t1 - clip_start))
        if pos2 <= clip_pos + eps or pos2 >= clip_tl_end - eps:
            return (
                f"Error: End time maps outside the clip "
                f"(clip source {clip_start:.3f}s–{clip_end:.3f}s on timeline)."
            )
        win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos2)
        return f"Sliced at {_fmt_mmss(t1 - clip_start)} from clip start; middle+right kept."

    # Only lower boundary inside clip
    if t1 >= clip_end - eps:
        pos1 = snap(clip_pos + (t0 - clip_start))
        if pos1 <= clip_pos + eps or pos1 >= clip_tl_end - eps:
            return (
                f"Error: Start time maps outside the clip "
                f"(clip source {clip_start:.3f}s–{clip_end:.3f}s on timeline)."
            )
        win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos1)
        return f"Sliced at {_fmt_mmss(t0 - clip_start)} from clip start; left+middle kept."

    pos1 = snap(clip_pos + (t0 - clip_start))
    pos2_abs = snap(clip_pos + (t1 - clip_start))
    if pos1 <= clip_pos + eps or pos1 >= clip_tl_end - eps:
        return "Error: First slice position is outside the clip on the timeline."
    if pos2_abs <= pos1 + eps or pos2_abs >= clip_tl_end - eps:
        return "Error: Second slice position is outside the clip on the timeline."

    win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [clip_id_str], [], pos1)
    right_id = _find_right_segment(pos1, t0)
    if not right_id:
        return (
            "First slice succeeded but the app could not find the new segment "
            "for the second cut. Try slicing once at the playhead, then again."
        )

    pos2 = snap(pos1 + (t1 - t0))
    right_tl_end = pos1 + (clip_end - t0)
    if pos2 <= pos1 + eps or pos2 >= right_tl_end - eps:
        return "Error: Second slice maps outside the trimmed segment."

    win.timeline.Slice_Triggered(MenuSlice.KEEP_BOTH, [right_id], [], pos2)
    return (
        f"Sliced at {_fmt_mmss(t0 - clip_start)} and {_fmt_mmss(t1 - clip_start)} "
        f"(source). Three segments: before, selected range, after."
    )


def slice_clip_at_best_match(
    query="",
    occurrence="0",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    try:
        from classes.api_client import get_backend_client

        clip_info_box = [None]
        error_box_pre = [None]

        def _read_clip_info():
            try:
                from classes.clip_resolver import resolve_timeline_clip
                from classes.ai_metadata_utils import get_source_window
                from classes.timeline_clip_context import resolve_parent_file_data

                occ = _parse_occurrence(str(occurrence or _kw.get("occurrence", "0")), query)
                resolved = resolve_timeline_clip(
                    timeline_clip_id=str(timeline_clip_id or "").strip(),
                    clip_query=str(clip_query or "").strip(),
                    track=str(_kw.get("track") or _kw.get("prefer_track") or "").strip(),
                    occurrence=occ,
                )
                if not resolved.ok or not resolved.clip:
                    error_box_pre[0] = resolved.error or "Error: Could not resolve timeline clip."
                    return
                obj = resolved.clip
                d = obj.data if isinstance(obj.data, dict) else {}
                sf = _get_source_file_for_clip(obj)
                fd = sf.data if sf and isinstance(sf.data, dict) else None
                cs, ce = get_source_window(d, fd)
                cp = float(d.get("position", 0.0) or 0.0)
                ly = d.get("layer", 1)
                try:
                    layer_num = int(ly) if ly is not None else 1
                except (TypeError, ValueError):
                    layer_num = 1
                fid = str(d.get("file_id") or "")
                parent_data = resolve_parent_file_data(fd, file_id=fid)
                sa = (
                    parent_data.get("ai_metadata")
                    if parent_data and isinstance(parent_data.get("ai_metadata"), dict)
                    else None
                )
                # Extract TwelveLabs info (may be absent for old imports)
                tw = get_index_block(sa or {})
                tw_status = (tw.get("status") or "").lower()
                iid = tw.get("index_id") or ""
                vid = tw.get("video_id") or ""
                if not iid:
                    log.warning(
                        "TwelveLabs metadata missing for source file; "
                        "falling back to default index lookup."
                    )
                tw_err = str(tw.get("error") or "").strip()
                clip_info_box[0] = (
                    str(obj.id), cs, ce, cp, str(iid), str(vid), layer_num, fid, tw_status, tw_err
                )
            except Exception as exc:
                error_box_pre[0] = f"Error: {exc}"

        _run_on_main_thread(_read_clip_info)

        if error_box_pre[0]:
            return error_box_pre[0]
        if not clip_info_box[0]:
            return "Error: Could not read clip metadata."

        clip_id_str, clip_start, clip_end, clip_pos, index_id, video_id, layer_num, file_id_str, tw_status, tw_error = clip_info_box[0]

        time_rng = _parse_explicit_source_time_range_sec(query or "")
        if time_rng is not None:
            t0, t1 = time_rng
            eps = 1e-3
            if t0 < clip_start - eps or t1 > clip_end + eps:
                return (
                    f"Error: Requested range [{t0:.2f}s–{t1:.2f}s] is outside this clip's "
                    f"source window [{clip_start:.2f}s–{clip_end:.2f}s]."
                )

            def _do_time_slice():
                return _slice_timeline_clip_at_source_times(
                    clip_id_str,
                    clip_start,
                    clip_end,
                    clip_pos,
                    layer_num,
                    file_id_str,
                    t0,
                    t1,
                )

            try:
                return _run_on_main_thread(_do_time_slice)
            except Exception as exc:
                log.error("time-based slice failed: %s", exc, exc_info=True)
                return f"Error: {exc}"

        if tw_status == "failed":
            detail = f" ({tw_error})" if tw_error else ""
            return (
                "TwelveLabs indexing failed for this video"
                + detail
                + ". Re-import the file or run reindex_project_file_tool to upload and index again. "
                "You can still slice by explicit times, e.g. 'from 4 seconds to 10 seconds'."
            )
        if tw_status == "indexing":
            return (
                "TwelveLabs is still indexing this video. "
                "Please wait for indexing to finish and try again."
            )

        # ── 2. Check backend connectivity (can run on any thread)
        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Error: TwelveLabs is not configured."

        if not index_id:
            return (
                "Error: This clip's source file has no TwelveLabs index_id. "
                "Re-import or re-index the file."
            )
        if not video_id:
            return (
                "Error: This clip's source file has no TwelveLabs video_id. "
                "Re-import or re-index the file so searches target the correct video."
            )

        # ── 3. TwelveLabs search (REST call – fine from background thread)
        from classes.twelvelabs_match import (
            select_twelvelabs_match,
            snap_source_time_to_frame,
            snap_timeline_position,
        )

        search_query = _semantic_search_query(query)
        if not search_query:
            return "Error: Empty search query."

        items, err = _twelvelabs_search_in_window(
            index_id, search_query, page_limit=30, video_id=video_id,
        )
        if err:
            return f"Error: {err}"
        if not items:
            sa_fb = None
            try:
                from classes.query import File
                fobj = File.get(id=file_id_str)
                if fobj and isinstance(fobj.data, dict):
                    raw = fobj.data.get("ai_metadata")
                    sa_fb = raw if isinstance(raw, dict) else None
            except Exception:
                pass
            cut_from_scenes = _scene_description_cut_source(
                clip_start, clip_end, sa_fb, query,
                _parse_occurrence(occurrence, query),
            )
            if cut_from_scenes is not None:
                return _slice_at_source_cut(
                    clip_id_str, clip_start, clip_end, clip_pos, cut_from_scenes,
                    label="scene description match",
                )
            return "No matches found."

        nth = _parse_occurrence(occurrence, query)
        chosen = select_twelvelabs_match(
            items,
            clip_start=clip_start,
            clip_end=clip_end,
            occurrence=nth,
            cut_mode="start",
        )
        if not chosen:
            sa_fb = None
            try:
                from classes.query import File
                fobj = File.get(id=file_id_str)
                if fobj and isinstance(fobj.data, dict):
                    raw = fobj.data.get("ai_metadata")
                    sa_fb = raw if isinstance(raw, dict) else None
            except Exception:
                pass
            cut_from_scenes = _scene_description_cut_source(
                clip_start, clip_end, sa_fb, query, nth,
            )
            if cut_from_scenes is not None:
                return _slice_at_source_cut(
                    clip_id_str, clip_start, clip_end, clip_pos, cut_from_scenes,
                    label="scene description match",
                )
            return "No matches overlapped the clip window."

        ordinal_label = f"occurrence #{nth}" if nth > 0 else "best match"

        fps = _get_app().project.get("fps") or {}
        fps_num = float(fps.get("num", 30))
        fps_den = float(fps.get("den", 1)) or 1.0
        cut_source = snap_source_time_to_frame(chosen["cut_source"], fps_num, fps_den)
        slice_pos = snap_timeline_position(
            clip_pos + (cut_source - clip_start), fps_num, fps_den,
        )

        log.info(
            "slice_clip_at_best_match: clip_id=%s rank=%s overlap=%.3f "
            "cut_source=%.3f slice_pos=%.3f (%s)",
            clip_id_str,
            chosen.get("rank"),
            chosen.get("overlap_ratio", 0.0),
            cut_source,
            slice_pos,
            ordinal_label,
        )

        # ── 4. Validate: slice_pos must fall inside the clip on the timeline
        clip_timeline_end = clip_pos + (clip_end - clip_start)
        if slice_pos <= clip_pos or slice_pos >= clip_timeline_end:
            return (
                f"Error: Computed slice position ({slice_pos:.3f}s) is outside "
                f"the clip range [{clip_pos:.3f}s – {clip_timeline_end:.3f}s]."
            )

        # ── 5. Perform the slice on the Qt main thread
        from windows.views.timeline_backend.enums import MenuSlice

        slice_error_box = [None]

        def _do_slice():
            try:
                _get_app().window.timeline.Slice_Triggered(
                    MenuSlice.KEEP_BOTH, [clip_id_str], [], slice_pos,
                )
            except Exception as exc:
                slice_error_box[0] = str(exc)

        _run_on_main_thread(_do_slice)

        if slice_error_box[0]:
            return f"Error during slice: {slice_error_box[0]}"

        return f"Sliced at {_fmt_mmss(cut_source - clip_start)} ({ordinal_label})."
    except Exception as e:
        log.error("slice_clip_at_best_match failed: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

def _pause_auto_save():
    """Pause auto-save timer to prevent backup interference during generation."""
    result_box = [False]

    def _do():
        try:
            app = _get_app()
            app._generation_in_progress = True
            timer = getattr(app.window, "auto_save_timer", None)
            if timer and timer.isActive():
                timer.stop()
                result_box[0] = True
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_do, timeout=10)
        else:
            _do()
    else:
        _do()
    return result_box[0]


def _resume_auto_save(was_active):
    """Resume auto-save timer if it was previously active."""

    def _clear_flag():
        try:
            _get_app()._generation_in_progress = False
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_clear_flag, timeout=10)
        else:
            _clear_flag()
    else:
        _clear_flag()

    if not was_active:
        return

    def _restart():
        try:
            app = _get_app()
            timer = getattr(app.window, "auto_save_timer", None)
            if timer:
                timer.start()
        except Exception:
            pass

    if QThread is not None:
        app = _get_app()
        if QThread.currentThread() is not app.thread():
            _run_on_main_thread(_restart, timeout=10)
        else:
            _restart()
    else:
        _restart()


def _reencode_for_openshot(input_path, output_path=None, width=1920, height=1080):
    """Re-encode a video with a clean container so libopenshot can read it.

    AI-generated downloads often have missing/corrupt moov atoms, wrong
    timebases, or missing audio streams.  A quick re-encode with libx264
    + aac fixes all of that.

    Returns (output_path, None) on success, (None, error) on failure.
    """
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_clean{ext}"

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    ok, err = _ffmpeg_run(cmd)
    if not ok:
        # Try without audio (source may have no audio stream)
        cmd_no_audio = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        ok, err = _ffmpeg_run(cmd_no_audio)
        if not ok:
            return None, f"Re-encode failed: {err}"
    return output_path, None


def _ffprobe_pix_fmt(path) -> str:
    """Return primary video pix_fmt or empty string."""
    try:
        p = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        return (p.stdout or "").strip().lower()
    except Exception:
        return ""


def _ffprobe_alpha_mode(path) -> str:
    """Return stream alpha_mode / ALPHA_MODE tag (HyperFrames VP9 WebM) or empty."""
    try:
        p = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream_tags=alpha_mode,ALPHA_MODE",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        for line in (p.stdout or "").splitlines():
            val = line.strip().lower()
            if val:
                return val
        return ""
    except Exception:
        return ""


def _ffprobe_has_alpha(path) -> bool:
    """True when stream has alpha pix_fmt OR VP9 WebM ALPHA_MODE=1 (sidecar alpha)."""
    pix = _ffprobe_pix_fmt(path)
    if pix and (
        ("yuva" in pix)
        or pix.startswith("rgba")
        or pix.startswith("bgra")
        or pix.startswith("argb")
        or pix.startswith("abgr")
        or pix.startswith("gbra")
    ):
        return True
    # HyperFrames / libvpx WebM: ffprobe often reports yuv420p + ALPHA_MODE=1
    mode = _ffprobe_alpha_mode(path)
    return mode in ("1", "true", "yes")


def _ffprobe_has_explicit_yuva(path) -> bool:
    """True when primary pix_fmt is already yuva* (rare for libvpx WebM)."""
    pix = _ffprobe_pix_fmt(path)
    return bool(pix and "yuva" in pix)


def _verify_decoded_alpha_pixels(path, *, force_libvpx=None) -> bool:
    """Decode one frame and confirm some pixels are actually transparent.

    VP9 WebM: must force libvpx before -i (native VP9 decode drops alpha → solid
    black). qtrle/png/prores MOV: native decode preserves alpha — match OpenShot.
    Fail closed on ffmpeg errors or fully opaque frames.
    """
    import tempfile

    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if force_libvpx is None:
        force_libvpx = ext in (".webm", ".mkv")
    tmp_png = None
    try:
        fd, tmp_png = tempfile.mkstemp(suffix=".png", prefix="zenvi_alpha_")
        os.close(fd)
        cmd = ["ffmpeg", "-y"]
        if force_libvpx:
            cmd += ["-c:v", "libvpx-vp9"]
        cmd += [
            "-i", path,
            "-frames:v", "1",
            "-update", "1",
            "-pix_fmt", "rgba",
            tmp_png,
        ]
        ok, _err = _ffmpeg_run(cmd)
        if not ok or not os.path.isfile(tmp_png) or os.path.getsize(tmp_png) < 32:
            return False
        try:
            from PIL import Image

            im = Image.open(tmp_png).convert("RGBA")
            w, h = im.size
            if w < 1 or h < 1:
                return False
            samples = [
                im.getpixel((0, 0)),
                im.getpixel((w - 1, 0)),
                im.getpixel((0, h - 1)),
                im.getpixel((w - 1, h - 1)),
                im.getpixel((w // 2, h // 2)),
            ]
            return any(len(px) >= 4 and px[3] < 250 for px in samples)
        except Exception:
            try:
                from PyQt5.QtGui import QImage

                img = QImage(tmp_png)
                if img.isNull():
                    return False
                img = img.convertToFormat(QImage.Format_RGBA8888)
                w, h = img.width(), img.height()
                if w < 1 or h < 1:
                    return False
                pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, h // 2)]
                for x, y in pts:
                    c = img.pixelColor(x, y)
                    if c.alpha() < 250:
                        return True
                return False
            except Exception:
                return False
    finally:
        if tmp_png and os.path.isfile(tmp_png):
            try:
                os.remove(tmp_png)
            except OSError:
                pass


def _openshot_transparent_ok(path) -> bool:
    """True when OpenShot's native decoder will composite with real alpha.

    Requires alpha in the container pix_fmt (argb/rgba/yuva*) — typically qtrle
    MOV from `_reencode_alpha_for_openshot`. VP9 WebM with ALPHA_MODE=1 alone is
    NOT ok: libopenshot uses native VP9 which drops alpha to opaque black.
    """
    if not path:
        return False
    pix = _ffprobe_pix_fmt(path)
    if not pix:
        return False
    if not (
        ("yuva" in pix)
        or pix.startswith("rgba")
        or pix.startswith("bgra")
        or pix.startswith("argb")
        or pix.startswith("abgr")
        or pix.startswith("gbra")
    ):
        return False
    # Native decode path OpenShot uses — do not force libvpx
    return _verify_decoded_alpha_pixels(path, force_libvpx=False)


def _looks_like_alpha_video(path) -> bool:
    """HyperFrames transparent overlays are WebM (VP9+alpha); confirm others via ffprobe."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext == ".webm":
        probed = _ffprobe_has_alpha(path)
        if probed:
            return True
        # If probe fails (ffprobe missing), still treat .webm as alpha-intent for MG.
        return True
    return _ffprobe_has_alpha(path)


def _reencode_alpha_for_openshot(input_path, output_path=None, width=1920, height=1080):
    """Re-encode HyperFrames VP9 WebM into qtrle MOV so OpenShot keeps alpha.

    Must decode with libvpx-vp9 BEFORE -i (native VP9 drops alpha → solid black).
    Encode QuickTime Animation (qtrle + argb): OpenShot/libopenshot native decode
    preserves alpha. Do NOT leave as VP9 WebM — that looks transparent to libvpx
    probes but composites as opaque black in the editor.

    Returns (output_path, None) on success, (None, error) on failure.
    """
    if output_path is None:
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}_alpha.mov"
    # Always deliver .mov for OpenShot alpha overlays
    if not str(output_path).lower().endswith(".mov"):
        output_path = os.path.splitext(output_path)[0] + ".mov"

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,setsar=1,format=rgba"
    )
    # libvpx-vp9 before -i = VP9+alpha decoder; qtrle = OpenShot-safe alpha encoder
    cmd = [
        "ffmpeg", "-y",
        "-c:v", "libvpx-vp9",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "qtrle", "-pix_fmt", "argb",
        "-an",
        output_path,
    ]
    ok, err = _ffmpeg_run(cmd)
    if not ok:
        return None, f"Alpha re-encode failed: {err}"
    # Verify with NATIVE decode (what OpenShot does) — not libvpx
    if not _verify_decoded_alpha_pixels(output_path, force_libvpx=False):
        return None, (
            "Alpha re-encode produced opaque plate "
            "(no transparent pixels after native decode)"
        )
    return output_path, None


def _normalize_imported_file_path(file_obj, final_path):
    """Store an absolute path on imported File metadata (panel + thumbnails)."""
    if not file_obj:
        return
    final_path = _canonical_media_path(final_path)
    changed = False
    if file_obj.data.get("path") != final_path:
        file_obj.data["path"] = final_path
        changed = True
    reader = file_obj.data.get("reader")
    if isinstance(reader, dict) and reader.get("path") != final_path:
        reader["path"] = final_path
        changed = True
    if changed:
        file_obj.save()


def _refresh_imported_file_thumbnail(file_id, file_path):
    """Pre-generate and refresh the files-panel thumbnail for an imported video."""
    from classes import info
    from classes.thumbnail import GenerateThumbnail

    file_path = _canonical_media_path(file_path)
    if not file_id or not file_path or not os.path.isfile(file_path):
        return

    mask_path = os.path.join(info.IMAGES_PATH, "mask.png")
    overlay_path = os.path.join(info.IMAGES_PATH, "overlay.png")
    thumb_path = os.path.join(info.THUMBNAIL_PATH, file_id, "1.png")
    GenerateThumbnail(file_path, thumb_path, 1, 98, 64, mask_path, overlay_path)

    try:
        _get_app().window.FileUpdated.emit(str(file_id))
    except Exception as exc:
        log.warning("_refresh_imported_file_thumbnail: could not refresh UI: %s", exc)


def _merge_baked_transition_metadata(
    file_a,
    start_a,
    end_a,
    file_b,
    start_b,
    end_b,
    seg_a_duration,
    morph_duration,
    prompt_hint="",
):
    """Merge tags/metadata for baked clip A + morph + B with segment-correct scene times."""
    from classes.ai_metadata_utils import collect_scene_descriptions_for_baked_segment

    tag_tokens = []
    seen_tags = set()
    list_keys = ("objects", "scenes", "activities", "mood")
    ai_merged = {
        "analyzed": True,
        "tags": {key: [] for key in list_keys},
    }
    seen_lists = {key: set() for key in list_keys}
    descriptions = []
    scene_descriptions = []

    def _absorb_file_tags(file_obj):
        if not file_obj or not isinstance(getattr(file_obj, "data", None), dict):
            return
        for part in str(file_obj.data.get("tags") or "").split(","):
            token = part.strip()
            if not token:
                continue
            norm = token.lower()
            if norm in seen_tags:
                continue
            seen_tags.add(norm)
            tag_tokens.append(token)

    def _absorb_ai_lists(ai):
        if not isinstance(ai, dict):
            return
        tags = ai.get("tags")
        if not isinstance(tags, dict):
            return
        for key in list_keys:
            vals = tags.get(key) or []
            if not isinstance(vals, list):
                continue
            for val in vals:
                text = str(val).strip()
                if not text:
                    continue
                norm = text.lower()
                if norm in seen_lists[key]:
                    continue
                seen_lists[key].add(norm)
                ai_merged["tags"][key].append(text)
        desc = ai.get("description")
        if desc and str(desc).strip():
            descriptions.append(str(desc).strip())

    _absorb_file_tags(file_a)
    _absorb_file_tags(file_b)

    ai_a = file_a.data.get("ai_metadata") if file_a and isinstance(file_a.data, dict) else None
    ai_b = file_b.data.get("ai_metadata") if file_b and isinstance(file_b.data, dict) else None

    if isinstance(ai_a, dict):
        _absorb_ai_lists(ai_a)
        scene_descriptions.extend(
            collect_scene_descriptions_for_baked_segment(
                ai_a, start_a, end_a, 0.0,
            )
        )

    clip_b_offset = max(0.0, float(seg_a_duration)) + max(0.0, float(morph_duration))
    if isinstance(ai_b, dict):
        _absorb_ai_lists(ai_b)
        scene_descriptions.extend(
            collect_scene_descriptions_for_baked_segment(
                ai_b, start_b, end_b, clip_b_offset,
            )
        )

    hint = str(prompt_hint or "").strip()
    if hint:
        scene_descriptions.append({
            "time": max(0.0, float(seg_a_duration)) + max(0.1, float(morph_duration) * 0.5),
            "description": hint[:500],
        })

    scene_descriptions.sort(key=lambda item: float(item.get("time", 0) or 0))

    if descriptions:
        ai_merged["description"] = " | ".join(descriptions[:3])
    elif scene_descriptions:
        ai_merged["description"] = " ".join(
            str(s.get("description", "")).strip()
            for s in scene_descriptions[:6]
            if str(s.get("description", "")).strip()
        )

    if scene_descriptions:
        ai_merged["scene_descriptions"] = scene_descriptions[:24]

    return ", ".join(tag_tokens), ai_merged


def _merge_file_tags_and_metadata(*file_objs):
    """Merge comma-separated tags and ai_metadata tag lists from File objects."""
    tag_tokens = []
    seen_tags = set()
    list_keys = ("objects", "scenes", "activities", "mood")
    ai_merged = {
        "analyzed": True,
        "tags": {key: [] for key in list_keys},
    }
    seen_lists = {key: set() for key in list_keys}
    descriptions = []
    scene_descriptions = []

    for file_obj in file_objs:
        if not file_obj or not isinstance(getattr(file_obj, "data", None), dict):
            continue
        data = file_obj.data
        for part in str(data.get("tags") or "").split(","):
            token = part.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen_tags:
                continue
            seen_tags.add(key)
            tag_tokens.append(token)

        ai = data.get("ai_metadata")
        if not isinstance(ai, dict):
            continue
        tags = ai.get("tags")
        if isinstance(tags, dict):
            for key in list_keys:
                vals = tags.get(key) or []
                if not isinstance(vals, list):
                    continue
                for val in vals:
                    text = str(val).strip()
                    if not text:
                        continue
                    norm = text.lower()
                    if norm in seen_lists[key]:
                        continue
                    seen_lists[key].add(norm)
                    ai_merged["tags"][key].append(text)
        desc = ai.get("description")
        if desc and str(desc).strip():
            descriptions.append(str(desc).strip())
        for scene in ai.get("scene_descriptions") or []:
            if isinstance(scene, dict) and scene.get("description"):
                scene_descriptions.append(scene)

    if descriptions:
        ai_merged["description"] = " | ".join(descriptions[:3])
    if scene_descriptions:
        ai_merged["scene_descriptions"] = scene_descriptions[:24]

    return ", ".join(tag_tokens), ai_merged


def _clip_source_range(clip_data, file_data, fallback_duration=0.0):
    """Return (start, end) source trim range for a timeline clip."""
    start = float(clip_data.get("start", 0) or 0)
    end = float(clip_data.get("end", 0) or 0)
    if end <= start:
        file_dur = float((file_data or {}).get("duration", 0) or 0)
        end = file_dur if file_dur > start else start + max(0.1, float(fallback_duration or 0.1))
    return start, end


def _bake_transition_video(
    path_a,
    start_a,
    end_a,
    morph_path,
    path_b,
    start_b,
    end_b,
    width,
    height,
    output_path,
):
    """Concatenate clip A + AI morph + clip B into one MP4."""
    dur_a = max(0.01, end_a - start_a)
    dur_b = max(0.01, end_b - start_b)
    morph_dur = _ffprobe_video_duration(morph_path)
    if morph_dur < 0.1:
        morph_dur = 5.0

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps=24"
    )
    video_filters = (
        f"[0:v]trim=start={start_a}:end={end_a},setpts=PTS-STARTPTS,{vf}[va];"
        f"[1:v]setpts=PTS-STARTPTS,{vf}[vb];"
        f"[2:v]trim=start={start_b}:end={end_b},setpts=PTS-STARTPTS,{vf}[vc];"
        f"[va][vb][vc]concat=n=3:v=1:a=0[vout]"
    )

    has_audio_a = _ffprobe_has_audio(path_a)
    has_audio_m = _ffprobe_has_audio(morph_path)
    has_audio_b = _ffprobe_has_audio(path_b)
    want_audio = has_audio_a or has_audio_m or has_audio_b

    if want_audio:
        def _audio_filter(input_idx, has_audio, trim_start=None, trim_end=None, null_dur=0.0):
            if has_audio and trim_start is not None and trim_end is not None:
                return (
                    f"[{input_idx}:a]atrim=start={trim_start}:end={trim_end},"
                    f"asetpts=PTS-STARTPTS"
                )
            if has_audio:
                return f"[{input_idx}:a]asetpts=PTS-STARTPTS"
            return (
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=start=0:end={null_dur},asetpts=PTS-STARTPTS"
            )

        audio_filters = (
            f"{_audio_filter(0, has_audio_a, start_a, end_a, dur_a)}[aa];"
            f"{_audio_filter(1, has_audio_m, null_dur=morph_dur)}[ab];"
            f"{_audio_filter(2, has_audio_b, start_b, end_b, dur_b)}[ac];"
            f"[aa][ab][ac]concat=n=3:v=0:a=1[aout]"
        )
        filter_complex = f"{video_filters};{audio_filters}"
        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", morph_path, "-i", path_b,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", path_a, "-i", morph_path, "-i", path_b,
            "-filter_complex", video_filters,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    return _ffmpeg_run(cmd)


def _replace_timeline_clips_with_baked(clip_a_id, clip_b_id, baked_file_id, position, layer):
    """Remove the two source clips and place the baked transition clip on the timeline."""
    from classes.query import Clip
    from PyQt5.QtCore import QPointF

    def _do():
        app = _get_app()
        win = app.window
        layer_num = int(layer) if layer is not None else 0
        for cid in (clip_b_id, clip_a_id):
            if not cid or not Clip.get(id=cid):
                continue
            if hasattr(win, "removeSelection"):
                try:
                    win.removeSelection(cid, "clip")
                except Exception as exc:
                    log.warning("Could not remove clip %s: %s", cid, exc)
        win.timeline.addClip(baked_file_id, QPointF(float(position), 0.0), layer_num)

    _run_on_main_thread(_do, timeout=30)


def _replace_timeline_clip_with_baked(clip_id, baked_file_id, position, layer):
    """Remove one source clip and place the baked replacement on the timeline."""
    from classes.query import Clip
    from PyQt5.QtCore import QPointF

    def _do():
        app = _get_app()
        win = app.window
        layer_num = int(layer) if layer is not None else 0
        if clip_id and Clip.get(id=clip_id):
            if hasattr(win, "removeSelection"):
                try:
                    win.removeSelection(clip_id, "clip")
                except Exception as exc:
                    log.warning("Could not remove clip %s: %s", clip_id, exc)
        win.timeline.addClip(baked_file_id, QPointF(float(position), 0.0), layer_num)

    _run_on_main_thread(_do, timeout=30)


def _import_generated_video(video_path, *, preserve_alpha=None):
    """Import a generated video into the project with clean metadata.

    Re-encodes the video first to a permanent location (via
    _output_path_for_generated_video), then adds it using skip_indexing=True
    to avoid nested event loops and metadata corruption.

    When preserve_alpha is True (or auto-detected for WebM), re-encodes to
    qtrle MOV (argb) so OpenShot's native decoder keeps transparency.
    VP9 WebM is never imported as-is — native VP9 drops alpha to solid black.
    Alpha failure is fail-closed — never silent yuv420p/MP4 fallback for overlays.

    Returns (File object, None) on success, (None, error_string) on failure.
    """
    from classes.query import File

    want_alpha = bool(preserve_alpha) if preserve_alpha is not None else _looks_like_alpha_video(video_path)

    if want_alpha:
        perm_path = _canonical_media_path(_output_path_for_generated_video(ext=".mov"))
        # Always re-encode through libvpx→qtrle. Even "good" WebM composites black
        # in OpenShot because FFmpegReader uses the native VP9 decoder.
        clean_path, err = _reencode_alpha_for_openshot(video_path, output_path=perm_path)
        if err:
            return None, f"alpha import failed (no opaque fallback): {err}"
    else:
        perm_path = _canonical_media_path(_output_path_for_generated_video(ext=".mp4"))
        clean_path, err = _reencode_for_openshot(video_path, output_path=perm_path)
        if err:
            log.warning("Re-encode failed, using original: %s", err)
            clean_path = video_path

    final_path = _canonical_media_path(clean_path)

    # Import into project on the main thread
    def _do_import():
        _get_app().window.files_model.add_files([final_path], skip_indexing=True)
    _run_on_main_thread(_do_import, timeout=30)

    # Look up the File object
    f = File.get(path=final_path)
    if not f:
        f = File.get(path=os.path.normpath(final_path))
    if not f:
        f = File.get(path=os.path.realpath(final_path))
    if not f:
        for candidate in File.filter():
            try:
                if getattr(candidate, "absolute_path", None) and candidate.absolute_path() == final_path:
                    f = candidate
                    break
            except Exception:
                continue
    if f:
        _normalize_imported_file_path(f, final_path)

        def _refresh_thumb():
            _refresh_imported_file_thumbnail(f.id, final_path)

        _run_on_main_thread(_refresh_thumb, timeout=30)
    return f, None


def _download_motion_graphics_file(url, default_name="motion_segment.mp4"):
    """Download a Supabase video to a fresh temp path. Returns (dest_path, size_mb)."""
    import tempfile
    import urllib.request
    from urllib.parse import unquote

    url_path = url.split("?")[0].rstrip("/")
    raw_name = unquote(url_path.split("/")[-1] or default_name)
    root, ext = os.path.splitext(raw_name)
    if ext.lower() not in (".mp4", ".webm", ".mov", ".mkv", ".avi"):
        # Infer from URL path fragments
        lower = url_path.lower()
        if lower.endswith(".webm") or "/output.webm" in lower:
            ext = ".webm"
        else:
            ext = ".mp4"
        raw_name = f"{root or 'motion_segment'}{ext}"

    tmp_dir = tempfile.mkdtemp(prefix="zenvi_hyperframes_")
    dest_path = os.path.join(tmp_dir, raw_name)

    log.info("Downloading HyperFrames video from Supabase: %s → %s", url, dest_path)
    req = urllib.request.Request(url, headers={"User-Agent": "ZenviApp/1.0"})
    with urllib.request.urlopen(req, timeout=300) as response, open(dest_path, "wb") as out:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            out.write(chunk)

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    size_bytes = os.path.getsize(dest_path)
    if size_bytes <= 0:
        raise ValueError(f"Downloaded file is empty (0 bytes): {dest_path}")
    if size_mb < 0.1:
        log.info("Download complete: %s (%.0f KB)", dest_path, size_bytes / 1024.0)
    else:
        log.info("Download complete: %s (%.1f MB)", dest_path, size_mb)
    return dest_path, size_mb


def _stamp_motion_graphics_file_metadata(file_obj, label="", transparent=None):
    """Agent-facing metadata only — does not enqueue Gemini indexing."""
    if not file_obj:
        return
    summary = (label or "").strip()
    if not summary:
        log.warning("MG stamp refused empty summary — using generic placeholder")
        summary = "HyperFrames motion graphic"
    try:
        tags = file_obj.data.get("tags") if isinstance(file_obj.data, dict) else None
        if isinstance(tags, str):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tag_list = [str(t).strip() for t in tags if str(t).strip()]
        else:
            tag_list = []
        if "motion_graphics" not in tag_list:
            tag_list.append("motion_graphics")
        if transparent and "transparent_overlay" not in tag_list:
            tag_list.append("transparent_overlay")
        file_obj.data["tags"] = ", ".join(tag_list)

        ai = file_obj.data.get("ai_metadata")
        if not isinstance(ai, dict):
            ai = {}
        ai["short_summary"] = summary
        # Mirror AI-gen style: description carries the same human text for panels/search
        ai["description"] = summary
        # Must be True so get_effective_ai_metadata / Scene panel show the summary
        # (skip_indexing still avoids Gemini — agent authored this text).
        ai["analyzed"] = True
        ai["source"] = "hyperframes_motion_graphics"
        if transparent is not None:
            ai["transparent"] = bool(transparent)
        file_obj.data["ai_metadata"] = ai
        # Prefer a readable title in the media bin (like generated clips)
        if summary and summary != "HyperFrames motion graphic":
            short_title = summary.split("(")[0].strip()
            if short_title and len(short_title) <= 120:
                file_obj.data["name"] = short_title[:120]
        elif not file_obj.data.get("name"):
            file_obj.data["name"] = "HyperFrames motion graphic"
        file_obj.save()
        try:
            _get_app().window.FileUpdated.emit(str(file_obj.id))
        except Exception:
            pass
    except Exception as exc:
        log.warning("Could not stamp motion-graphics metadata: %s", exc)


def _resolve_motion_graphics_label_from_job(render_job_id="", fallback=""):
    """When the agent omits label=, recover summary from HyperFrames job status.

    Returns (label, job_meta_dict).
    """
    job_id = (render_job_id or "").strip()
    if not job_id:
        return (fallback or "").strip(), {}
    import json
    import urllib.request

    api = os.environ.get(
        "HYPERFRAMES_URL",
        os.environ.get("REMOTION_URL", "http://localhost:4500/api/v1"),
    ).rstrip("/")
    for kind in ("motion", "demo"):
        try:
            req = urllib.request.Request(
                f"{api}/{kind}/jobs/{job_id}",
                headers={"User-Agent": "ZenviApp/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode() or "{}")
            summary = str(data.get("summary") or "").strip()
            if summary:
                return summary, data
            titles = data.get("segment_titles") or []
            block_id = data.get("block_id") or ""
            if titles or block_id:
                title0 = titles[0] if titles else ""
                transparent = data.get("transparent")
                bits = []
                if block_id:
                    bits.append(f"HyperFrames {block_id}")
                else:
                    bits.append("HyperFrames motion graphic")
                if title0 and title0 not in ("motion", block_id):
                    bits.append(str(title0))
                if transparent:
                    bits.append("transparent overlay")
                label = ": ".join(bits[:2]) + (f" ({bits[2]})" if len(bits) > 2 else "")
                return label, data
            return (fallback or "").strip(), data
        except Exception as exc:
            log.debug("MG label lookup %s/%s failed: %s", kind, job_id, exc)
    return (fallback or "").strip(), {}


def _download_and_import_one(url, label="", job_transparent=None):
    """Download one Supabase video and import it as a project file (skip_indexing).

    Returns (file_id, size_mb, error, transparent_ok, pix_fmt).
    job_transparent: when True, force alpha-preserving import and fail closed (no opaque MP4).
    """
    try:
        last_err = None
        dest_path = None
        size_mb = 0.0
        for attempt in (1, 2):
            try:
                dest_path, size_mb = _download_motion_graphics_file(url)
                break
            except Exception as e:
                last_err = e
                log.warning("Download attempt %d failed for %s: %s", attempt, url, e)
        if dest_path is None:
            return "", 0.0, f"download failed: {last_err}", False, ""

        if job_transparent is True:
            preserve_alpha = True
        elif job_transparent is False:
            preserve_alpha = False
        else:
            preserve_alpha = _looks_like_alpha_video(dest_path)

        f, err = _import_generated_video(dest_path, preserve_alpha=preserve_alpha)
        if err:
            return "", size_mb, f"import failed: {err}", False, ""

        imported_path = None
        try:
            imported_path = f.absolute_path() if f and hasattr(f, "absolute_path") else None
        except Exception:
            imported_path = None
        if not imported_path and f and isinstance(getattr(f, "data", None), dict):
            imported_path = f.data.get("path")

        pix_fmt = _ffprobe_pix_fmt(imported_path) if imported_path else ""
        alpha_mode = _ffprobe_alpha_mode(imported_path) if imported_path else ""
        # libvpx VP9 WebM probes as yuv420p + ALPHA_MODE=1 (never yuva*). Accept that
        # when decoded pixels actually have transparency.
        transparent_ok = bool(imported_path and _openshot_transparent_ok(imported_path))
        stamp_transparent = (
            bool(job_transparent) if job_transparent is not None else transparent_ok
        )
        probe_note = f"pix_fmt={pix_fmt or 'unknown'} alpha_mode={alpha_mode or 'none'}"
        if job_transparent and not transparent_ok:
            return (
                "",
                size_mb,
                (
                    f"transparent job imported without usable VP9 alpha ({probe_note}) "
                    "— re-compose as WebM; refusing solid plate"
                ),
                False,
                pix_fmt,
            )

        _stamp_motion_graphics_file_metadata(f, label=label, transparent=stamp_transparent)
        # Encode probe bits into pix_fmt field for fetch messaging: "yuva420p;alpha_mode=1"
        probe_field = pix_fmt or "unknown"
        if alpha_mode:
            probe_field = f"{probe_field};alpha_mode={alpha_mode}"
        return (f.id if f else ""), size_mb, None, transparent_ok or stamp_transparent, probe_field
    except Exception as e:
        log.error("download/import failed for %s: %s", url, e, exc_info=True)
        return "", 0.0, str(e), False, ""


def _motion_graphics_cleanup_storage(supabase_path="", render_job_id=""):
    """Best-effort DELETE {HYPERFRAMES_URL}/cleanup. Non-critical — failures are logged only."""
    import json
    import urllib.request

    if not (supabase_path or render_job_id):
        return
    api = os.environ.get(
        "HYPERFRAMES_URL",
        os.environ.get("REMOTION_URL", "http://localhost:4500/api/v1"),
    ).rstrip("/")
    try:
        payload = {}
        if supabase_path:
            payload["supabase_path"] = supabase_path
        if render_job_id:
            payload["job_id"] = render_job_id
        body = json.dumps(payload).encode()
        cleanup_req = urllib.request.Request(
            f"{api}/cleanup",
            data=body,
            method="DELETE",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(cleanup_req, timeout=60) as cleanup_resp:
            log.info("Supabase cleanup after import: %s", cleanup_resp.read().decode()[:500])
    except Exception as cleanup_err:
        log.warning("Supabase cleanup failed (non-critical): %s", cleanup_err)


# Session-scoped URL → file_id so package/hand re-fetch is idempotent.
_MG_IMPORTED_URLS: dict = {}


def fetch_motion_graphics_video(
    segment_urls=None,
    supabase_url="",
    supabase_path="",
    render_job_id="",
    label="",
    **_kw,
) -> str:
    """Import rendered HyperFrames motion/demo segments into the project files panel.

    Preferred: pass segment_urls — the ordered list of per-segment Supabase URLs.
    Each segment is imported as its own clip; storage is cleaned up once after success.
    Imports use skip_indexing=True (no Gemini summarize). Optional label stamps short_summary.

    Legacy: pass a single supabase_url to import one video.
    """
    import json
    import re

    global _MG_IMPORTED_URLS

    _GENERIC_LABELS = (
        "hyperframes motion graphic",
        "motion graphic",
        "motion",
    )

    def _norm_url(u: str) -> str:
        return (u or "").strip().split("?")[0].rstrip("/")

    def _already_imported(urls: list):
        ids = []
        for u in urls:
            fid = _MG_IMPORTED_URLS.get(_norm_url(u))
            if not fid:
                return None
            ids.append(fid)
        if not ids:
            return None
        return (
            f"Already imported file_id={ids[0]} (file_ids: {ids}) — "
            "place only if missing. Do NOT re-download these URLs."
        )

    def _is_generic(text: str) -> bool:
        return (text or "").strip().lower() in _GENERIC_LABELS

    job_meta = {}
    stamp_label = (label or "").strip()
    recovered = ""
    if render_job_id:
        recovered, job_meta = _resolve_motion_graphics_label_from_job(
            render_job_id, fallback=stamp_label
        )
        # Prefer explicit agent label; else job.summary; never keep generic when job has better text
        if not stamp_label:
            stamp_label = recovered
        elif _is_generic(stamp_label) and recovered and not _is_generic(recovered):
            stamp_label = recovered
    if not stamp_label:
        stamp_label = "HyperFrames motion graphic"

    job_transparent = job_meta.get("transparent")
    if job_transparent is not None:
        job_transparent = bool(job_transparent)

    generic = _is_generic(stamp_label)
    generic_warn = ""
    if generic:
        if render_job_id and job_meta.get("summary"):
            # Job had summary but we somehow still stamped generic — surface loudly
            generic_warn = (
                " ⚠️ short_summary is still generic despite job.summary — "
                "pass label= from compose suggested_label."
            )
        else:
            generic_warn = (
                " ⚠️ short_summary is generic — pass label= from compose suggested_label "
                "(or ensure render_job_id is set so job.summary can be recovered)."
            )

    # The LLM may pass segment_urls as a JSON-encoded string.
    if isinstance(segment_urls, str):
        try:
            segment_urls = json.loads(segment_urls)
        except Exception:
            segment_urls = [segment_urls]
    segment_urls = [u.strip() for u in (segment_urls or []) if isinstance(u, str) and u.strip()]

    # Prefer WebM when job is transparent but URLs still point at .mp4
    if job_transparent and segment_urls:
        fixed = []
        for u in segment_urls:
            if u.split("?")[0].lower().endswith(".mp4"):
                webm = re.sub(r"\.mp4(\?|$)", r".webm\1", u, count=1, flags=re.I)
                log.warning(
                    "Job %s is transparent but URL is MP4 — trying WebM: %s",
                    render_job_id,
                    webm,
                )
                fixed.append(webm)
            else:
                fixed.append(u)
        segment_urls = fixed

    if segment_urls:
        cached = _already_imported(segment_urls)
        if cached:
            return cached

    # ---- Multi-segment import (preferred) ----
    if segment_urls:
        # Deterministic timeline order: sort by the numeric index in segment_NN.mp4,
        # independent of the order the agent passed the URLs in.
        def _seg_index(u):
            name = u.split("?")[0].rsplit("/", 1)[-1]
            m = re.search(r"segment[_-]?(\d+)", name, re.IGNORECASE)
            return int(m.group(1)) if m else 1_000_000

        ordered = sorted(enumerate(segment_urls), key=lambda iu: (_seg_index(iu[1]), iu[0]))

        file_ids = []
        failures = []  # (original_index, url, error)
        total_mb = 0.0
        any_transparent_ok = False
        last_pix_fmt = ""
        for orig_i, url in ordered:
            path_base = url.lower().split("?")[0]
            if job_transparent and path_base.endswith(".mp4"):
                failures.append(
                    (
                        orig_i,
                        url,
                        "transparent job URL is .mp4 and WebM rewrite failed — "
                        "re-compose as WebM; refusing opaque MP4 import",
                    )
                )
                continue

            file_id, size_mb, err, transparent_ok, pix_fmt = _download_and_import_one(
                url, label=stamp_label, job_transparent=job_transparent
            )
            # Fail-closed: never import opaque MP4 as success for transparent jobs
            if err and job_transparent and path_base.endswith(".webm"):
                failures.append(
                    (
                        orig_i,
                        url,
                        f"{err} — re-compose WebM (no opaque MP4 fallback)",
                    )
                )
                log.warning("Segment %d transparent WebM import failed (no MP4 fallback): %s", orig_i, err)
                continue
            if err:
                failures.append((orig_i, url, err))
                log.warning("Segment %d import failed: %s", orig_i, err)
            else:
                file_ids.append(file_id)
                total_mb += size_mb
                any_transparent_ok = any_transparent_ok or bool(transparent_ok)
                if pix_fmt:
                    last_pix_fmt = pix_fmt
                _MG_IMPORTED_URLS[_norm_url(url)] = file_id

        n = len(segment_urls)
        if failures:
            # Leave storage intact so the failed segments can be re-fetched without a re-render.
            failed_lines = "\n".join(f"  [{i}] {u} ({e})" for i, u, e in failures)
            return (
                f"⚠️ Imported {len(file_ids)}/{n} motion segments; {len(failures)} failed. "
                f"Storage was NOT cleaned up so you can retry the failed ones. "
                f"file_ids: {file_ids}\nFailed segments:\n{failed_lines}{generic_warn}"
            )

        # All segments imported — clean up Supabase storage once.
        _motion_graphics_cleanup_storage(render_job_id=render_job_id, supabase_path=supabase_path)
        alpha_note = (
            "Transparent WebM alpha preserved — place as overlay on a HIGHER track than footage."
            if any_transparent_ok or job_transparent
            else "Opaque MP4 — prefer standalone/mid-layer placement away from hero peaks."
        )
        probe_bits = (
            f" transparent_ok={str(any_transparent_ok).lower()}"
            f" pix_fmt={last_pix_fmt or 'unknown'}"
        )
        return (
            f"✅ Imported {len(file_ids)}/{n} HyperFrames segments as separate clips "
            f"(file_id={file_ids[0]}, file_ids: {file_ids}, total {total_mb:.1f} MB). "
            f"Indexing skipped (motion_graphics tag).{probe_bits}. {alpha_note}\n"
            "MUST call add_clip_to_timeline_tool for each file_id "
            "(transparent overlays: layer_number 3000000+; opaque title cards: standalone cut / mid layer)."
            f"{generic_warn}"
        )

    # ---- Legacy single-video import (back-compat) ----
    supabase_url = (supabase_url or "").strip()
    if not supabase_url:
        return "Error: segment_urls or supabase_url is required."

    if job_transparent and supabase_url.split("?")[0].lower().endswith(".mp4"):
        supabase_url = re.sub(r"\.mp4(\?|$)", r".webm\1", supabase_url, count=1, flags=re.I)

    if job_transparent and supabase_url.split("?")[0].lower().endswith(".mp4"):
        return (
            "Error: transparent job URL is still .mp4 after WebM rewrite — "
            "re-compose as WebM; refusing opaque MP4 import."
            f"{generic_warn}"
        )

    cached_one = _already_imported([supabase_url])
    if cached_one:
        return cached_one

    file_id, size_mb, err, transparent_ok, pix_fmt = _download_and_import_one(
        supabase_url, label=stamp_label, job_transparent=job_transparent
    )
    if err:
        return f"Error importing video: {err}{generic_warn}"
    _MG_IMPORTED_URLS[_norm_url(supabase_url)] = file_id
    _motion_graphics_cleanup_storage(supabase_path=supabase_path, render_job_id=render_job_id)
    alpha_note = (
        "Transparent WebM alpha preserved — overlay on a HIGHER track than footage."
        if transparent_ok or job_transparent
        else "Opaque MP4 — prefer standalone/mid-layer placement away from hero peaks."
    )
    return (
        f"✅ HyperFrames motion graphic imported into project files "
        f"(file_id={file_id}, size: {size_mb:.1f} MB). Indexing skipped (motion_graphics tag). "
        f"transparent_ok={str(bool(transparent_ok)).lower()} pix_fmt={pix_fmt or 'unknown'}. "
        f"{alpha_note}\n"
        "MUST call add_clip_to_timeline_tool(file_id=...) with the placement mode above."
        f"{generic_warn}"
    )


# Hard-cut alias kept only so any stale backend tool name still resolves during one deploy.
fetch_remotion_video_from_supabase = fetch_motion_graphics_video
_download_remotion_file = _download_motion_graphics_file
_remotion_cleanup_storage = _motion_graphics_cleanup_storage


_KLING_O1_DEFAULT_T2V_DURATION = 5


def import_video_url_and_add_to_timeline(video_url="", track="", position_seconds="", **_kw) -> str:
    """Download a video from a public URL, import it into project files, and place it on the timeline.

    Used for server-rendered clips (e.g. Manim) delivered as a public URL. One shot:
    download → re-encode/import (via _import_generated_video) → add_clip_to_timeline.
    """
    import tempfile
    import urllib.request

    video_url = (video_url or "").strip()
    if not video_url:
        return "Error: video_url is required."

    url_path = video_url.split("?")[0].rstrip("/")
    lower_path = url_path.lower()
    _audio_exts = (".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma")
    if "freesound.org" in lower_path or any(lower_path.endswith(ext) for ext in _audio_exts):
        return (
            "Error: import_video_url_and_add_to_timeline_tool is for video URLs only. "
            "For Freesound / music / SFX use stock_music(query=..., track=...)."
        )

    try:
        # Derive a clean .mp4 filename from the URL path.
        raw_name = url_path.split("/")[-1] or "video.mp4"
        root, ext = os.path.splitext(raw_name)
        if ext.lower() not in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
            raw_name = f"{root or 'video'}.mp4"

        tmp_dir = tempfile.mkdtemp(prefix="zenvi_url_import_")
        dest_path = os.path.join(tmp_dir, raw_name)

        log.info("Downloading video from URL: %s → %s", video_url, dest_path)
        req = urllib.request.Request(video_url, headers={"User-Agent": "ZenviApp/1.0"})
        with urllib.request.urlopen(req, timeout=300) as response, open(dest_path, "wb") as out:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)

        size_mb = os.path.getsize(dest_path) / (1024 * 1024)
        log.info("Download complete: %s (%.1f MB)", dest_path, size_mb)

        # Import into project files (re-encodes for libopenshot compatibility).
        f, err = _import_generated_video(dest_path)
        if err:
            return f"Error importing video: {err}"
        file_id = f.id if f else ""
        if not file_id:
            return "Error: video imported but its file_id could not be resolved."

        # Place it on the timeline.
        placement = add_clip_to_timeline(
            file_id=file_id, position_seconds=position_seconds, track=track, **_kw
        )
        return (
            f"✅ Video imported (file_id: {file_id}, {size_mb:.1f} MB) and added to the timeline.\n"
            f"{placement}"
        )
    except Exception as e:
        log.error("import_video_url_and_add_to_timeline failed: %s", e, exc_info=True)
        return f"Error importing video from URL: {e}"


def generate_video_and_add_to_timeline(prompt="", duration_seconds="", position_seconds="", track="", **_kw) -> str:
    if QThread is None or QEventLoop is None:
        return "Error: Requires PyQt5."
    app = _get_app()
    prompt = (prompt or "").strip()
    if len(prompt) < 2:
        return "Error: Prompt must be at least 2 characters."

    explicit_dur = str(duration_seconds or "").strip()
    if explicit_dur:
        try:
            duration = _snap_kling_o1_duration(int(float(explicit_dur)))
        except (TypeError, ValueError):
            duration = _KLING_O1_DEFAULT_T2V_DURATION
    else:
        # Default 5s unless user explicitly requests 10s in chat (passed via duration_seconds).
        duration = _KLING_O1_DEFAULT_T2V_DURATION
    t2v_w, t2v_h = _project_kling_o1_t2v_dims()

    output_path = _canonical_media_path(_output_path_for_generated_video())

    # Pause auto-save during generation to prevent backup interference
    auto_save_was_active = _pause_auto_save()
    try:
        from classes.credits_client import check_operation, credits

        _, _, blocked = check_operation("video_generation", "video generation")
        if blocked:
            return blocked
        from classes.api_client import get_backend_client
        client = get_backend_client()
        result = client.generate_video(
            prompt,
            duration_seconds=duration,
            width=t2v_w,
            height=t2v_h,
            mode="t2v",
        )
        video_url = result.get("video_url", "")
        err = result.get("error", "")
        if err:
            return f"Error: {err}"

        dl_err = _download_video_url_to_path(video_url, output_path)
        if dl_err:
            return f"Error: {dl_err}"

        from classes.credits_client import charge_operation_on_success, credits

        charge_operation_on_success(
            True,
            "video_generation",
            provider="runware",
            note=f"txt2v: {prompt[:60]}",
        )
        credits.award_bonus("first_export")   # idempotent — only fires once ever

        try:
            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Video generated but failed to import into project files"
                    + (f": {import_err}" if import_err else ".")
                )

            # When inserting at a specific position, ripple downstream clips
            # forward so the generated clip doesn't overlap them.
            _pos = None
            if position_seconds and str(position_seconds).strip():
                try:
                    _pos = float(position_seconds)
                except Exception:
                    _pos = None

            if _pos is not None:
                # Compute the generated clip's duration from the file metadata
                _gen_dur = float(f.data.get("duration") or duration)

                from classes.query import Clip as _Clip
                _app_ref = app
                _snap_tol = 0.001

                # Determine which layer to ripple: prefer the explicit track arg,
                # then detect from which clips are actually sitting at position >= _pos.
                # Using max(layers) was unreliable — the highest-numbered layer may not
                # be the one the plan agent placed clips on.
                _ripple_layer = None
                if track and str(track).strip():
                    _layers_ripple = _app_ref.project.get("layers") or []
                    _resolved_r, _err_r = normalize_track_or_layer_arg(
                        str(track).strip(), _layers_ripple
                    )
                    if not _err_r and _resolved_r is not None:
                        _ripple_layer = _resolved_r
                if _ripple_layer is None:
                    _clips_at_pos = [
                        c for c in list(_Clip.filter())
                        if float(c.data.get("position", 0)) >= _pos - _snap_tol
                        and c.data.get("id")
                    ]
                    if _clips_at_pos:
                        closest = min(_clips_at_pos,
                                      key=lambda c: float(c.data.get("position", 0)))
                        _ripple_layer = closest.data.get("layer", None)

                def _do_ripple_insert():
                    for c in list(_Clip.filter()):
                        c_pos = float(c.data.get("position", 0))
                        c_layer = c.data.get("layer", 0)
                        cid = c.data.get("id")
                        if (cid
                                and c_pos >= _pos - _snap_tol
                                and (_ripple_layer is None or c_layer == _ripple_layer)):
                            _app_ref.updates.update(
                                ["clips", {"id": cid}], {"position": c_pos + _gen_dur}
                            )

                _run_on_main_thread(_do_ripple_insert)

            was_playing = _pause_player()
            try:
                msg = add_clip_to_timeline(file_id=f.id, position_seconds=position_seconds or "", track=track or "")
            finally:
                _resume_player(was_playing)
            if not msg or str(msg).lower().startswith("error"):
                return (
                    f"Error: Video imported (file_id={f.id}) but timeline placement failed: "
                    f"{msg or 'unknown'}. "
                    f"Do NOT regenerate — call add_clip_to_timeline_tool(file_id='{f.id}', "
                    f"track=<layer_number from list_layers_tool>, position_seconds=...)."
                )
            return msg
        except Exception as e:
            return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def insert_v2v_into_clip(
    query="",
    fade_ms="400",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Find best match in resolved clip, generate a V2V insert via Kling O1 Pro."""
    if QThread is None or QEventLoop is None:
        return "Error: Requires PyQt5."

    resolved = _resolve_timeline_clip_for_tool(
        clip_query=clip_query, timeline_clip_id=timeline_clip_id, **_kw,
    )
    if not resolved.ok or not resolved.clip:
        return resolved.error or "Error: Could not resolve timeline clip."
    clip_obj = resolved.clip

    query = (query or "").strip()
    too_extreme, reason = _is_extreme_for_4_seconds(query)
    if too_extreme:
        return f"Error: {reason}"

    try:
        fm = int(float(fade_ms)) if str(fade_ms).strip() else 400
    except Exception:
        fm = 400
    fade_s = max(0.05, min(0.49, float(fm) / 1000.0))

    clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
    clip_start = float(clip_data.get("start", 0.0) or 0.0)
    clip_end = float(clip_data.get("end", 0.0) or 0.0)

    source_file = _get_source_file_for_clip(clip_obj)
    if not source_file or not getattr(source_file, "absolute_path", None) or not source_file.absolute_path():
        return "Error: Could not find source video."
    source_path = source_file.absolute_path()

    source_ai = (
        source_file.data.get("ai_metadata")
        if isinstance(source_file.data, dict) and isinstance(source_file.data.get("ai_metadata"), dict)
        else None
    )

    from classes.twelvelabs_match import select_twelvelabs_match

    best_mid = None

    # Strategy 1: TwelveLabs overlap-aware match; cut after the scene ends.
    if source_ai:
        tw = source_ai.get("twelvelabs") if isinstance(source_ai.get("twelvelabs"), dict) else {}
        status = (tw.get("status") or "").lower()
        index_id = tw.get("index_id") or ""
        video_id = tw.get("video_id") or ""
        if (status == "ready" and index_id and video_id):
            search_query = _semantic_search_query(query)
            items, err = _twelvelabs_search_in_window(
                str(index_id), search_query, page_limit=30, video_id=str(video_id),
            )
            if not err and items:
                chosen = select_twelvelabs_match(
                    items,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    cut_mode="end",
                )
                if chosen:
                    insertion = chosen["cut_source"]
                    if insertion > clip_end - 1.0:
                        insertion = max(clip_start, clip_end - 1.0)
                    best_mid = insertion
                    log.info(
                        "insert_v2v: rank=%s segment [%.2f, %.2f] → insertion at %.2f",
                        chosen.get("rank"),
                        chosen["start"],
                        chosen["end"],
                        best_mid,
                    )

    # Strategy 2: Scene descriptions
    if best_mid is None and source_ai:
        scenes = source_ai.get("scene_descriptions", [])
        q_lower = query.lower()
        for sc in (scenes or []):
            if not isinstance(sc, dict):
                continue
            desc = (sc.get("description") or "").lower()
            t = float(sc.get("time", 0.0) or 0.0)
            if q_lower in desc and clip_start <= t <= clip_end:
                best_mid = t
                break

    # Strategy 3: fallback — 80% through the clip (biased toward the end)
    if best_mid is None:
        best_mid = clip_start + (clip_end - clip_start) * 0.8
        log.info("insert_v2v: no search results, using 80%% fallback point %.2fs", best_mid)

    # Get video dimensions — clamp to Kling O1 Pro video-edit range [720, 2160]
    vid_width = int(source_file.data.get("width", 1920))
    vid_height = int(source_file.data.get("height", 1080))
    vf, vid_width, vid_height = _kling_o1_scale_vf(vid_width, vid_height)
    log.info("insert_v2v: target dims %dx%d", vid_width, vid_height)

    # Pause auto-save during the generation pipeline
    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_v2v_")
        try:
            # ---- Step 1: Extract 3 s of footage before the insertion point as V2V seed ----
            # Sending Kling a reference video keeps the generated insert visually consistent
            # with the original clip (same scene, lighting, style).
            seed_mp4 = os.path.join(tmpdir, "seed.mp4")
            insert_mp4 = os.path.join(tmpdir, "insert.mp4")

            ref_dur = min(3.0, best_mid - clip_start)
            ref_start = max(0.0, best_mid - ref_dur)
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(ref_start), "-i", source_path,
                "-t", str(ref_dur), "-vf", vf, "-r", "24", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", seed_mp4,
            ])
            if not ok:
                return f"Error: Failed to extract seed video: {err}"

            # ---- Step 2: Generate V2V insert (video-edit only; no frame constraints) ----
            prompt = (
                f"{query}\n\n"
                "Continue the scene naturally from the source footage. "
                "Preserve camera motion, lighting, and visual style."
            )
            from classes.credits_client import check_operation

            _, _, blocked = check_operation("video_generation", "video generation")
            if blocked:
                return blocked
            from classes.api_client import get_backend_client
            client = get_backend_client()
            seed_fid, _, up_err = _upload_generation_assets(client, seed_path=seed_mp4)
            if up_err:
                return f"Error: {up_err}"
            result = client.generate_video(
                prompt,
                seed_video_file_id=seed_fid,
                mode="v2v_edit",
                keep_original_sound=_ffprobe_has_audio(source_path),
            )
            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            dl_err = _download_video_url_to_path(video_url, insert_mp4)
            if dl_err:
                return f"Error: {dl_err}"

            # ---- Step 4: Bake updated clip with crossfades ----
            output_path = _canonical_media_path(_output_path_for_generated_video())
            dur_a = max(0.0, best_mid - clip_start)
            dur_c = max(0.0, clip_end - best_mid)
            # Probe the actual duration of the generated clip — do NOT assume it equals
            # gen_duration.  Even a 1-second mismatch makes xfade offsets wrong → corruption.
            insert_dur = _ffprobe_video_duration(insert_mp4)
            if insert_dur < 0.5:
                insert_dur = 3.0
                log.warning("insert_v2v: could not probe insert duration, using %s", insert_dur)

            # Clamp fade so xfade offsets are valid
            fade = float(fade_s)
            fade = min(fade, 0.49)
            fade = min(fade, max(0.01, dur_a / 2.0) if dur_a > 0 else 0.01)
            fade = min(fade, max(0.01, dur_c / 2.0) if dur_c > 0 else 0.01)
            fade = min(fade, max(0.01, insert_dur / 2.0) if insert_dur > 0 else 0.01)
            fade = max(0.01, fade)

            vf_bake = (
                f"scale={vid_width}:{vid_height}:force_original_aspect_ratio=decrease,"
                f"pad={vid_width}:{vid_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps=24"
            )
            off1 = max(0.0, dur_a - fade)
            off2 = max(0.0, dur_a + insert_dur - (2.0 * fade))

            has_audio = _ffprobe_has_audio(source_path)
            if has_audio:
                filter_complex = (
                    f"[0:v]trim=start={clip_start}:end={best_mid},setpts=PTS-STARTPTS,{vf_bake}[va];"
                    f"[1:v]setpts=PTS-STARTPTS,{vf_bake}[vb];"
                    f"[0:v]trim=start={best_mid}:end={clip_end},setpts=PTS-STARTPTS,{vf_bake}[vc];"
                    f"[va][vb]xfade=transition=fade:duration={fade}:offset={off1}[vab];"
                    f"[vab][vc]xfade=transition=fade:duration={fade}:offset={off2}[vout];"
                    f"[0:a]atrim=start={clip_start}:end={best_mid},asetpts=PTS-STARTPTS,"
                    f"afade=t=out:st={max(0.0, dur_a - fade)}:d={fade}[aa];"
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=start=0:end={insert_dur}[ab];"
                    f"[0:a]atrim=start={best_mid}:end={clip_end},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d={fade}[ac];"
                    f"[aa][ab][ac]concat=n=3:v=0:a=1[aout]"
                )
                bake_cmd = [
                    "ffmpeg", "-y", "-i", source_path, "-i", insert_mp4,
                    "-filter_complex", filter_complex,
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    output_path,
                ]
            else:
                filter_complex = (
                    f"[0:v]trim=start={clip_start}:end={best_mid},setpts=PTS-STARTPTS,{vf_bake}[va];"
                    f"[1:v]setpts=PTS-STARTPTS,{vf_bake}[vb];"
                    f"[0:v]trim=start={best_mid}:end={clip_end},setpts=PTS-STARTPTS,{vf_bake}[vc];"
                    f"[va][vb]xfade=transition=fade:duration={fade}:offset={off1}[vab];"
                    f"[vab][vc]xfade=transition=fade:duration={fade}:offset={off2}[vout]"
                )
                bake_cmd = [
                    "ffmpeg", "-y", "-i", source_path, "-i", insert_mp4,
                    "-filter_complex", filter_complex,
                    "-map", "[vout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-an",
                    "-movflags", "+faststart",
                    output_path,
                ]

            ok, bake_err = _ffmpeg_run(bake_cmd)
            if not ok:
                return f"Error: Failed to bake updated clip: {bake_err}"

            from classes.credits_client import charge_operation_on_success

            charge_operation_on_success(
                True,
                "video_generation",
                provider="runware",
                note=f"v2v insert: {query[:60]}",
            )

            # ---- Step 5: Import the baked clip and place on timeline ----
            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Failed to import baked clip into project files"
                    + (f": {import_err}" if import_err else ".")
                )
            clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
            clip_pos = float(clip_data.get("position", 0.0) or 0.0)
            clip_layer = clip_data.get("layer", 0)
            _replace_timeline_clip_with_baked(clip_obj.id, f.id, clip_pos, clip_layer)
            return (
                f"The combined clip (with a {insert_dur:.1f}s AI insert at "
                f"{_fmt_mmss(best_mid - clip_start)}, baked with {int(fade * 1000)}ms "
                f"crossfades) replaced the original clip on the timeline at {clip_pos:.2f}s."
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("insert_v2v_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def replace_object_in_clip(
    description="",
    duration_seconds="",
    clip_query="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """Replace or update an object/visual element in a timeline clip using Kling O1 Pro V2V edit."""
    if QThread is None or QEventLoop is None:
        return "Error: Requires PyQt5."

    resolved = _resolve_timeline_clip_for_tool(
        clip_query=clip_query, timeline_clip_id=timeline_clip_id, **_kw,
    )
    if not resolved.ok or not resolved.clip:
        return resolved.error or "Error: Could not resolve timeline clip."
    clip_obj = resolved.clip

    description = (description or "").strip()
    if not description:
        return "Error: A description of what to replace/update is required."

    clip_data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
    clip_start = float(clip_data.get("start", 0.0) or 0.0)
    clip_end = float(clip_data.get("end", 0.0) or 0.0)
    clip_duration = max(0.1, clip_end - clip_start)

    source_file = _get_source_file_for_clip(clip_obj)
    if not source_file or not getattr(source_file, "absolute_path", None) or not source_file.absolute_path():
        return "Error: Could not find source video for selected clip."
    source_path = source_file.absolute_path()

    # Default 5s segment for V2V edit; honor duration_seconds when set (max 10s).
    if str(duration_seconds).strip():
        try:
            extract_dur = min(float(duration_seconds), 10.0, clip_duration)
        except (TypeError, ValueError):
            extract_dur = min(5.0, clip_duration)
    else:
        extract_dur = min(5.0, clip_duration)

    vid_width = int(source_file.data.get("width", 1920))
    vid_height = int(source_file.data.get("height", 1080))
    vf, vid_width, vid_height = _kling_o1_scale_vf(vid_width, vid_height)

    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_replace_")
        try:
            ref_mp4 = os.path.join(tmpdir, "ref.mp4")

            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(clip_start), "-i", source_path,
                "-t", str(extract_dur), "-vf", vf, "-r", "24",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", ref_mp4,
            ])
            if not ok:
                # Retry without audio if mux fails
                ok, err = _ffmpeg_run([
                    "ffmpeg", "-y", "-ss", str(clip_start), "-i", source_path,
                    "-t", str(extract_dur), "-vf", vf, "-r", "24", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", ref_mp4,
                ])
            if not ok:
                return f"Error: Failed to extract reference clip: {err}"

            prompt = (
                f"{description}\n\n"
                "Apply the change throughout the entire video while preserving the original "
                "camera motion, scene composition, and lighting."
            )
            from classes.credits_client import check_operation

            _, _, blocked = check_operation("video_generation", "video generation")
            if blocked:
                return blocked

            from classes.api_client import get_backend_client
            client = get_backend_client()
            seed_fid, _, up_err = _upload_generation_assets(client, seed_path=ref_mp4)
            if up_err:
                return f"Error: {up_err}"
            has_audio = _ffprobe_has_audio(ref_mp4)
            result = client.generate_video(
                prompt,
                seed_video_file_id=seed_fid,
                mode="v2v_edit",
                keep_original_sound=has_audio,
            )
            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            output_path = _canonical_media_path(_output_path_for_generated_video())
            dl_err = _download_video_url_to_path(video_url, output_path)
            if dl_err:
                return f"Error: {dl_err}"

            from classes.credits_client import charge_operation_on_success

            charge_operation_on_success(
                True,
                "video_generation",
                provider="runware",
                note=f"replace object: {description[:60]}",
            )

            gen_duration = _ffprobe_video_duration(output_path)
            if gen_duration < 0.5:
                gen_duration = extract_dur

            f, import_err = _import_generated_video(output_path)
            if not f:
                return (
                    "Error: Failed to import generated video into project files"
                    + (f": {import_err}" if import_err else ".")
                )
            clip_pos = float(clip_data.get("position", 0.0) or 0.0)
            clip_layer = clip_data.get("layer", 0)
            _replace_timeline_clip_with_baked(clip_obj.id, f.id, clip_pos, clip_layer)
            return (
                f"Object replacement complete. A {gen_duration:.1f}s AI video with '{description}' "
                f"applied replaced the original clip on the timeline at {clip_pos:.2f}s."
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("replace_object_in_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


def generate_transition_clip(
    clip_a_id="",
    clip_b_id="",
    clip_a_query="",
    clip_b_query="",
    prompt_hint="",
    **_kw,
) -> str:
    """Generate a baked clip A + AI morph + clip B for two timeline clips (Kling O1 Pro)."""
    from classes.query import Clip
    _get_app()

    pair = _resolve_clip_pair_for_tool(
        clip_a_id=clip_a_id,
        clip_b_id=clip_b_id,
        clip_a_query=clip_a_query,
        clip_b_query=clip_b_query,
    )
    if not pair.ok or not pair.clip_a or not pair.clip_b:
        return pair.error or "Error: Could not resolve transition clip pair."
    clip_a = pair.clip_a
    clip_b = pair.clip_b

    file_a = _get_source_file_for_clip(clip_a)
    file_b = _get_source_file_for_clip(clip_b)
    if not file_a or not file_b:
        return "Error: Could not find source files for the clips."

    path_a = file_a.absolute_path() if hasattr(file_a, 'absolute_path') else file_a.data.get('path', '')
    path_b = file_b.absolute_path() if hasattr(file_b, 'absolute_path') else file_b.data.get('path', '')
    if not path_a or not os.path.isfile(path_a):
        return f"Error: Source video for clip A not found: {path_a}"
    if not path_b or not os.path.isfile(path_b):
        return f"Error: Source video for clip B not found: {path_b}"

    # Timeline positions and source trim ranges
    pos_a = float(clip_a.data.get("position", 0))
    start_a, end_a = _clip_source_range(clip_a.data, file_a.data)
    start_b, end_b = _clip_source_range(clip_b.data, file_b.data)
    duration_a = max(0.01, end_a - start_a)
    dur_b = max(0.01, end_b - start_b)

    log.info(
        "generate_transition: clip_a id=%s source=%.3f-%.3fs file=%s | "
        "clip_b id=%s source=%.3f-%.3fs file=%s",
        clip_a.id, start_a, end_a, os.path.basename(path_a),
        clip_b.id, start_b, end_b, os.path.basename(path_b),
    )

    layer = clip_a.data.get("layer")

    prompt = (prompt_hint or "").strip()
    if not prompt:
        prompt = (
            "Gradually evolve the opening scene into the closing scene through a fluid, "
            "continuous motion. Begin on the first frame composition and evolve smoothly "
            "toward the last frame image. Preserve the appearance and identity of all people "
            "and key objects while naturally transitioning pose, setting, and lighting. "
            "The movement should feel organic and cinematic, with no abrupt cuts."
        )

    morph_duration = _snap_kling_o1_duration(5)

    # Scale extracted frames to project dimensions for consistent morph output
    t2v_w, t2v_h = _project_kling_o1_t2v_dims()
    frame_vf = (
        f"scale={t2v_w}:{t2v_h}:force_original_aspect_ratio=decrease,"
        f"pad={t2v_w}:{t2v_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )

    # Pause auto-save during the generation pipeline
    auto_save_was_active = _pause_auto_save()
    try:
        tmpdir = tempfile.mkdtemp(prefix="zenvi_morph_")
        try:
            frame_a_path = os.path.join(tmpdir, "frame_a.jpg")
            frame_b_path = os.path.join(tmpdir, "frame_b.jpg")

            # Last frame of clip A → morph start (first constraint)
            time_a = max(start_a, end_a - 0.1) if end_a > start_a else start_a
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(time_a), "-i", path_a,
                "-frames:v", "1", "-vf", frame_vf, "-q:v", "2", frame_a_path,
            ])
            if not ok:
                return f"Error: Failed to extract last frame from clip A at {time_a:.3f}s: {err}"
            if not os.path.isfile(frame_a_path) or os.path.getsize(frame_a_path) < 512:
                return f"Error: Extracted frame A is empty (time={time_a:.3f}s, path={path_a})"

            # First frame of clip B → morph end (last constraint)
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-ss", str(start_b), "-i", path_b,
                "-frames:v", "1", "-vf", frame_vf, "-q:v", "2", frame_b_path,
            ])
            if not ok:
                return f"Error: Failed to extract first frame from clip B at {start_b:.3f}s: {err}"
            if not os.path.isfile(frame_b_path) or os.path.getsize(frame_b_path) < 512:
                return f"Error: Extracted frame B is empty (time={start_b:.3f}s, path={path_b})"

            log.info(
                "generate_transition: extracted frames A@%ss (%d bytes) B@%ss (%d bytes)",
                f"{time_a:.3f}",
                os.path.getsize(frame_a_path),
                f"{start_b:.3f}",
                os.path.getsize(frame_b_path),
            )

            from classes.credits_client import check_operation

            _, _, blocked = check_operation("morph_generation", "morph generation")
            if blocked:
                return blocked

            from classes.api_client import get_backend_client
            client = get_backend_client()

            log.info("generate_transition: Kling O1 Pro frame morph (last frame A → first frame B)")
            _, frame_images_paths, up_err = _upload_generation_assets(
                client,
                frame_specs=[
                    {"path": frame_a_path, "frame": "first"},
                    {"path": frame_b_path, "frame": "last"},
                ],
            )
            if up_err:
                return f"Error: {up_err}"
            result = client.generate_video(
                prompt,
                duration_seconds=int(morph_duration),
                frame_images_paths=frame_images_paths,
                mode="frame_morph",
            )

            video_url = result.get("video_url", "")
            gen_err = result.get("error", "")
            if gen_err:
                return f"Error: {gen_err}"

            morph_path = os.path.join(tmpdir, "morph_video.mp4")
            dl_err = _download_video_url_to_path(video_url, morph_path)
            if dl_err:
                return f"Error: {dl_err}"

            baked_path = _canonical_media_path(_output_path_for_generated_video())
            ok, bake_err = _bake_transition_video(
                path_a, start_a, end_a,
                morph_path,
                path_b, start_b, end_b,
                t2v_w, t2v_h,
                baked_path,
            )
            if not ok:
                return f"Error: Failed to bake transition clip: {bake_err}"

            morph_dur_actual = _ffprobe_video_duration(morph_path)
            if morph_dur_actual < 0.1:
                morph_dur_actual = float(morph_duration)

            f, import_err = _import_generated_video(baked_path)
            if not f:
                return "Error: Baked transition clip could not be added to project."

            merged_tags, merged_ai = _merge_baked_transition_metadata(
                file_a,
                start_a,
                end_a,
                file_b,
                start_b,
                end_b,
                duration_a,
                morph_dur_actual,
                prompt_hint=prompt,
            )
            if merged_tags:
                f.data["tags"] = merged_tags
            existing_ai = f.data.get("ai_metadata")
            if not isinstance(existing_ai, dict):
                existing_ai = {}
            existing_ai.update(merged_ai)
            f.data["ai_metadata"] = existing_ai
            _normalize_imported_file_path(f, f.absolute_path() if hasattr(f, "absolute_path") else baked_path)
            try:
                f.save()
                _get_app().window.FileUpdated.emit(str(f.id))
            except Exception as exc:
                log.warning("generate_transition: could not save merged tags: %s", exc)

            from classes.credits_client import charge_operation_on_success

            charge_operation_on_success(
                True,
                "morph_generation",
                provider="runware",
                note="transition/morph generation",
            )

            baked_duration = _ffprobe_video_duration(
                f.absolute_path() if hasattr(f, "absolute_path") else baked_path
            )
            if baked_duration < 0.5:
                baked_duration = duration_a + morph_dur_actual + dur_b
                log.warning(
                    "generate_transition: could not probe baked duration, using %.3fs",
                    baked_duration,
                )
            else:
                log.info("generate_transition: probed baked duration=%.3fs", baked_duration)

            _clip_a_id = clip_a.id
            _clip_b_id = clip_b.id
            _clip_a_layer = clip_a.data.get("layer", 0)

            _replace_timeline_clips_with_baked(
                _clip_a_id,
                _clip_b_id,
                f.id,
                pos_a,
                _clip_a_layer,
            )

            return (
                f"Transition baked! A {baked_duration:.2f}s clip (clip A + "
                f"{morph_dur_actual:.1f}s AI morph + clip B) was added to the project files "
                f"with merged tags and placed on the timeline at {pos_a:.2f}s."
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        log.error("generate_transition_clip: %s", e, exc_info=True)
        return f"Error: {e}"
    finally:
        _resume_auto_save(auto_save_was_active)


# ---------------------------------------------------------------------------
# Transitions tools
# ---------------------------------------------------------------------------

def list_transitions(category="all", **_kw) -> str:
    """List all available transitions in OpenShot."""
    try:
        from classes import info
        transitions_dir = os.path.join(info.PATH, "transitions")
        common_dir = os.path.join(transitions_dir, "common")
        extra_dir = os.path.join(transitions_dir, "extra")

        transitions = []

        def process_dir(dir_path, category_name):
            if not os.path.exists(dir_path):
                return
            for filename in sorted(os.listdir(dir_path)):
                if filename.startswith(".") or "thumbs.db" in filename.lower():
                    continue
                path = os.path.join(dir_path, filename)
                file_base_name = os.path.splitext(filename)[0]
                trans_name = file_base_name.replace("_", " ").capitalize()
                transitions.append({
                    "name": trans_name, "filename": filename,
                    "category": category_name, "path": path,
                })

        if category in ("all", "common"):
            process_dir(common_dir, "common")
        if category in ("all", "extra"):
            process_dir(extra_dir, "extra")

        if not transitions:
            return "No transitions found."

        data = {
            "total": len(transitions),
            "transitions": transitions[:50] if len(transitions) > 50 else transitions,
        }
        if len(transitions) > 50:
            data["note"] = f"Showing first 50 of {len(transitions)} transitions."
        return json.dumps(data, indent=2)
    except Exception as e:
        log.error("list_transitions: %s", e, exc_info=True)
        return f"Error: {e}"


def search_transitions(query="", **_kw) -> str:
    """Search for transitions by name."""
    try:
        from classes import info
        transitions_dir = os.path.join(info.PATH, "transitions")
        common_dir = os.path.join(transitions_dir, "common")
        extra_dir = os.path.join(transitions_dir, "extra")

        query_lower = (query or "").lower()
        matches = []

        def search_dir(dir_path, category_name):
            if not os.path.exists(dir_path):
                return
            for filename in os.listdir(dir_path):
                if filename.startswith(".") or "thumbs.db" in filename.lower():
                    continue
                file_base = os.path.splitext(filename)[0]
                trans_name = file_base.replace("_", " ").capitalize()
                if query_lower in trans_name.lower() or query_lower in file_base.lower():
                    matches.append({
                        "name": trans_name, "filename": filename,
                        "category": category_name,
                        "path": os.path.join(dir_path, filename),
                    })

        search_dir(common_dir, "common")
        search_dir(extra_dir, "extra")

        if not matches:
            return f"No transitions found matching '{query}'."

        return json.dumps({"query": query, "matches": len(matches), "transitions": matches}, indent=2)
    except Exception as e:
        log.error("search_transitions: %s", e, exc_info=True)
        return f"Error: {e}"


def apply_transition(
    clip1_id="",
    clip2_id="",
    transition_name="",
    duration="1.0",
    placement="between",
    **_kw,
) -> str:
    """Apply an OpenShot transition: placement='between' (two clips) or 'start'/'end' (one clip)."""
    place = (placement or "between").lower().strip()
    if place == "between":
        if not clip2_id:
            return "Error: clip2_id is required when placement='between'."
        return add_transition_between_clips(
            clip1_id, clip2_id, transition_name, duration, **_kw
        )
    if place in ("start", "end"):
        return add_transition_to_clip(
            clip1_id, transition_name, position=place, duration=duration, **_kw
        )
    return "Error: placement must be 'between', 'start', or 'end'."


def add_transition_between_clips(clip1_id="", clip2_id="", transition_name="", duration="1.0", **_kw) -> str:
    """Add a transition between two clips."""
    try:
        from classes.query import Clip
        from classes import info

        app = _get_app()
        win = app.window
        clip1 = Clip.get(id=clip1_id)
        clip2 = Clip.get(id=clip2_id)
        if not clip1:
            return f"Error: Clip '{clip1_id}' not found."
        if not clip2:
            return f"Error: Clip '{clip2_id}' not found."

        # Find transition file
        transitions_dir = os.path.join(info.PATH, "transitions")
        transition_path = None
        search_name = (transition_name or "").lower().replace(" ", "_")
        for cat in ["common", "extra"]:
            cat_dir = os.path.join(transitions_dir, cat)
            if os.path.exists(cat_dir):
                for fn in os.listdir(cat_dir):
                    fb = os.path.splitext(fn)[0]
                    if search_name in fb.lower() or fb.lower() in search_name:
                        transition_path = os.path.join(cat_dir, fn)
                        break
            if transition_path:
                break
        if not transition_path:
            return f"Error: Transition '{transition_name}' not found. Use search_transitions_tool."

        clip1_end = clip1.data.get("position", 0) + (clip1.data.get("end", 0) - clip1.data.get("start", 0))
        try:
            dur = float(duration)
        except ValueError:
            dur = 1.0

        layer1 = clip1.data.get("layer", 0)
        layer2 = clip2.data.get("layer", 0)
        target_layer_for_trans = max(layer1, layer2)
        transition_title = os.path.splitext(os.path.basename(transition_path))[0]

        # OpenShot Mask transitions require clips to OVERLAP to cross-dissolve.
        # Move clip2 backward by `dur` to create the overlap region, then place
        # the Mask at the overlap start.  Only clip2 is moved — downstream clips
        # are NOT rippled.
        clip2_pos = float(clip2.data.get("position", 0))
        clip2_id_val = clip2.data.get("id", clip2_id)
        new_clip2_pos = max(clip2_pos - dur, 0)

        # ALL Qt/openshot calls MUST run on the Qt main thread
        result_box = [None]

        def _do_transition():
            import openshot as _os

            # Build proper keyframe objects (required by OpenShot's Mask renderer)
            fps_data = app.project.get("fps") or {}
            fps_f = float(fps_data.get("num", 30)) / float(fps_data.get("den", 1) or 1)
            snap = lambda t: round(t * fps_f) / fps_f
            snapped_dur = snap(dur)
            snapped_c2_pos = snap(new_clip2_pos)

            # Step 1: Move clip2 backward to create overlap with clip1
            app.updates.update(["clips", {"id": clip2_id_val}], {"position": snapped_c2_pos})

            # Step 2: Place Mask transition in the overlap region
            brightness = _os.Keyframe()
            brightness.AddPoint(1, 1.0, _os.BEZIER)
            brightness.AddPoint(round(snapped_dur * fps_f) + 1, -1.0, _os.BEZIER)
            contrast = _os.Keyframe(3.0)
            trans_reader = _os.QtImageReader(transition_path)

            tid = str(uuid_module.uuid4())
            transition_data = {
                "id": tid,
                "layer": target_layer_for_trans,
                "position": snapped_c2_pos,   # start of the overlap region
                "start": 0.0,
                "end": snapped_dur,
                "brightness": json.loads(brightness.Json()),
                "contrast": json.loads(contrast.Json()),
                "reader": json.loads(trans_reader.Json()),
                "replace_image": False,
                "type": "Mask",
                "title": transition_title,
            }
            win.timeline.update_transition_data(transition_data, only_basic_props=False)
            result_box[0] = (tid, snapped_dur, snapped_c2_pos)

        _run_on_main_thread(_do_transition)
        tid, actual_dur, actual_pos = result_box[0] if result_box[0] else ("?", dur, new_clip2_pos)
        return (
            f"Added '{transition_name}' transition between clips (overlap: {actual_dur:.2f}s).\n"
            f"Clip2 moved to {actual_pos:.2f}s. Transition ID: {tid}"
        )
    except Exception as e:
        log.error("add_transition_between_clips: %s", e, exc_info=True)
        return f"Error: {e}"


def add_transition_to_clip(clip_id="", transition_name="", position="start", duration="1.0", **_kw) -> str:
    """Add a transition (fade in/out) to a single clip."""
    try:
        from classes.query import Clip
        from classes import info

        app = _get_app()
        clip = Clip.get(id=clip_id)
        if not clip:
            return f"Error: Clip '{clip_id}' not found."

        transitions_dir = os.path.join(info.PATH, "transitions")
        transition_path = None
        search_name = (transition_name or "").lower().replace(" ", "_")
        for cat in ["common", "extra"]:
            cat_dir = os.path.join(transitions_dir, cat)
            if os.path.exists(cat_dir):
                for fn in os.listdir(cat_dir):
                    fb = os.path.splitext(fn)[0]
                    if search_name in fb.lower() or fb.lower() in search_name:
                        transition_path = os.path.join(cat_dir, fn)
                        break
            if transition_path:
                break
        if not transition_path:
            return f"Error: Transition '{transition_name}' not found. Use search_transitions_tool."

        clip_position = clip.data.get("position", 0)
        clip_start = clip.data.get("start", 0)
        clip_end = clip.data.get("end", 0)
        clip_duration = clip_end - clip_start
        clip_layer = clip.data.get("layer", 0)
        win = app.window
        transition_title = os.path.splitext(os.path.basename(transition_path))[0]

        try:
            dur = float(duration)
        except ValueError:
            dur = 1.0

        if (position or "").lower() == "end":
            trans_position = clip_position + clip_duration - dur
        else:
            trans_position = clip_position

        result_box = [None]

        # Must run on Qt main thread — openshot.QtImageReader and update_transition_data
        # both touch Qt objects and dispatch to Qt listeners (properties_model, timeline, etc.)
        _is_fade_out = (position or "").lower() == "end"

        def _do_insert():
            import openshot as _os
            fps_data = app.project.get("fps") or {}
            fps_f = float(fps_data.get("num", 30)) / float(fps_data.get("den", 1) or 1)
            snap = lambda t: round(t * fps_f) / fps_f
            snapped_dur = snap(dur)

            brightness = _os.Keyframe()
            if _is_fade_out:
                # Fade OUT: clip starts visible (-1.0) and goes hidden (1.0)
                brightness.AddPoint(1, -1.0, _os.BEZIER)
                brightness.AddPoint(round(snapped_dur * fps_f) + 1, 1.0, _os.BEZIER)
            else:
                # Fade IN: clip starts hidden (1.0) and becomes visible (-1.0)
                brightness.AddPoint(1, 1.0, _os.BEZIER)
                brightness.AddPoint(round(snapped_dur * fps_f) + 1, -1.0, _os.BEZIER)
            contrast = _os.Keyframe(3.0)
            trans_reader = _os.QtImageReader(transition_path)

            tid = str(uuid_module.uuid4())
            transition_data = {
                "id": tid,
                "layer": clip_layer,
                "position": snap(trans_position),
                "start": 0.0,
                "end": snapped_dur,
                "brightness": json.loads(brightness.Json()),
                "contrast": json.loads(contrast.Json()),
                "reader": json.loads(trans_reader.Json()),
                "replace_image": False,
                "type": "Mask",
                "title": transition_title,
            }
            win.timeline.update_transition_data(transition_data, only_basic_props=False)
            result_box[0] = (tid, snapped_dur, snap(trans_position))

        _run_on_main_thread(_do_insert)
        tid, actual_dur, actual_pos = result_box[0] if result_box[0] else ("?", dur, trans_position)
        return (
            f"Added '{transition_name}' transition at {position} of clip.\n"
            f"ID: {tid}, Duration: {actual_dur:.2f}s, Position: {actual_pos:.2f}s"
        )
    except Exception as e:
        log.error("add_transition_to_clip: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# TTS tools (frontend-delegated: timeline insertion for generated speech)
# ---------------------------------------------------------------------------


def generate_tts_and_add_to_timeline(
    text="",
    voice="alloy",
    model="tts-1",
    speed=1.0,
    track=0,
    position=0.0,
    **kwargs,
) -> str:
    """Generate narration via backend TTS API and add MP3 to the timeline."""
    try:
        narration = (text or "").strip()
        if not narration:
            return "Error: No text provided for narration."

        from classes.api_client import get_backend_client

        client = get_backend_client()
        resp = client.generate_tts(
            text=narration,
            voice=(voice or "alloy"),
            model=(model or "tts-1"),
            speed=float(speed or 1.0),
        )
        if not resp.get("success"):
            return f"Error: {resp.get('error', 'TTS generation failed')}"

        import base64
        import tempfile

        raw = base64.b64decode(resp.get("audio_base64") or "")
        if not raw:
            return "Error: TTS returned empty audio."

        out_path = os.path.join(
            tempfile.gettempdir(),
            f"zenvi_tts_{uuid_module.uuid4().hex}.mp3",
        )
        with open(out_path, "wb") as f:
            f.write(raw)

        return add_tts_audio_to_timeline(
            audio_path=out_path,
            track=track,
            position=position,
            **kwargs,
        )
    except Exception as e:
        log.error("generate_tts_and_add_to_timeline: %s", e, exc_info=True)
        return f"Error: {e}"


def add_tts_audio_to_timeline(audio_path="", track=0, position=0.0, **kwargs) -> str:
    """Add a generated TTS audio file to the timeline (internal; prefer generate_tts_and_add_to_timeline_tool)."""
    try:
        from classes.query import File, Clip
        app = _get_app()

        if not audio_path or not os.path.isfile(audio_path):
            return f"Error: Audio file not found: {audio_path}"

        file_data = {
            "path": audio_path,
            "id": str(uuid_module.uuid4()),
        }
        clip_data = {
            "id": str(uuid_module.uuid4()),
            "file_id": file_data["id"],
            "layer": int(track),
            "position": float(position),
            "start": 0,
            "end": 0,
            "reader": {"path": audio_path, "has_audio": True, "has_video": False},
        }

        # Must run on Qt main thread — app.updates dispatches to Qt listeners
        def _do_insert():
            app.updates.insert(["files"], file_data)
            app.updates.insert(["clips"], clip_data)

        _run_on_main_thread(_do_insert)

        return f"Added TTS audio to timeline at position {position}s on track {track}."
    except Exception as e:
        log.error("add_tts_audio_to_timeline: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Stock media / resummarize / reindex / planning handlers
# ---------------------------------------------------------------------------

def import_stock_media(
    source="",
    video_id="",
    link="",
    sound_id="",
    preview_url="",
    filename="",
    local_path="",
    **kwargs,
) -> str:
    """Download stock media (pexels|freesound) and import into Project Files, or import local_path."""
    path = (local_path or "").strip()
    if path:
        return add_stock_media_to_project(local_path=path, **kwargs)

    src = (source or "").lower().strip()
    if src == "pexels":
        dl = download_pexels_video_tool(
            video_id=video_id, link=link, filename=filename, **kwargs
        )
    elif src == "freesound":
        dl = download_freesound_music_tool(
            sound_id=sound_id, preview_url=preview_url, filename=filename, **kwargs
        )
    else:
        return "Error: source must be 'pexels' or 'freesound' (or pass local_path)."

    if dl.startswith("Error"):
        return dl
    if "Downloaded to:" in dl:
        path = dl.split("Downloaded to:", 1)[1].strip()
        return add_stock_media_to_project(local_path=path, **kwargs)
    return dl


def modify_clip(
    mode="replace",
    description="",
    query="",
    fade_ms="400",
    duration_seconds="",
    clip_query="",
    timeline_clip_id="",
    **kwargs,
) -> str:
    """AI-edit a timeline clip resolved by tags/query: replace or insert footage."""
    m = (mode or "replace").lower().strip()
    text = (description or query or "").strip()
    if m == "insert":
        return insert_v2v_into_clip(
            query=text,
            fade_ms=fade_ms,
            clip_query=clip_query,
            timeline_clip_id=timeline_clip_id,
            **kwargs,
        )
    return replace_object_in_clip(
        description=text,
        duration_seconds=duration_seconds,
        clip_query=clip_query,
        timeline_clip_id=timeline_clip_id,
        **kwargs,
    )


def download_pexels_video_tool(video_id: str = "", link: str = "", filename: str = "", **kwargs) -> str:
    """Download a Pexels MP4 from the search-result link to the local machine."""
    try:
        if not link:
            return "Error: link is required (use the MP4 URL from search_pexels_videos_tool)."
        from classes.credits_client import charge_operation_on_success, check_operation

        _, _, blocked = check_operation("stock_add", "stock media download")
        if blocked:
            return blocked

        from classes.api_client import get_backend_client
        vid = int(video_id) if str(video_id).strip().isdigit() else 0
        result = get_backend_client().pexels_download(vid, link, filename=filename or "")
        err = result.get("error", "")
        path = result.get("local_path", "")
        if err or not path:
            return f"Pexels download error: {err or 'no file path'}"
        charge_operation_on_success(
            True, "stock_add", "stock_add", provider="pexels", note=f"video {vid}"
        )
        return f"Downloaded to: {path}"
    except Exception as exc:
        return f"Error downloading Pexels video: {exc}"


def download_freesound_music_tool(sound_id: str = "", preview_url: str = "", filename: str = "", **kwargs) -> str:
    """Download a Freesound HQ MP3 preview to the local machine."""
    try:
        if not preview_url:
            return "Error: preview_url is required (from search_freesound_music_tool)."
        try:
            sid = int(sound_id)
        except (ValueError, TypeError):
            return f"Error: sound_id must be numeric Freesound ID, not '{sound_id}'."

        from classes.credits_client import charge_operation_on_success, check_operation

        _, _, blocked = check_operation("stock_add", "stock media download")
        if blocked:
            return blocked

        from classes.api_client import get_backend_client
        result = get_backend_client().freesound_download(sid, preview_url, filename=filename or "")
        err = result.get("error", "")
        path = result.get("local_path", "")
        if err or not path:
            return f"Freesound download error: {err or 'no file path'}"
        charge_operation_on_success(
            True, "stock_add", "stock_add", provider="freesound", note=f"sound {sid}"
        )
        return f"Downloaded to: {path}"
    except Exception as exc:
        return f"Error downloading Freesound audio: {exc}"


def add_stock_media_to_project(local_path: str = "", **kwargs) -> str:
    """Import a downloaded stock file into Project Files."""
    try:
        if not local_path:
            return "Error: local_path is required."
        import os
        if not os.path.isfile(local_path):
            return f"Error: File not found: {local_path}"
        app = _get_app()
        files_model = app.window.files_model
        from classes.query import File

        def _resolve_imported_file(path: str):
            """Match project File after add_files (path keys often differ on Windows)."""
            variants = []
            for candidate in (path, os.path.normpath(path), os.path.realpath(path)):
                if candidate and candidate not in variants:
                    variants.append(candidate)
            for p in variants:
                f = File.get(path=p)
                if f:
                    return f
            base = os.path.basename(path).lower()
            best = None
            for candidate in File.filter():
                try:
                    cpath = candidate.data.get("path") or ""
                    abs_path = ""
                    try:
                        abs_path = candidate.absolute_path() or ""
                    except Exception:
                        abs_path = ""
                    names = {
                        os.path.basename(cpath).lower(),
                        os.path.basename(abs_path).lower(),
                    }
                    if base not in names:
                        continue
                    # Prefer exact absolute-path match when several share a basename.
                    if abs_path and os.path.normcase(os.path.normpath(abs_path)) in {
                        os.path.normcase(os.path.normpath(v)) for v in variants
                    }:
                        return candidate
                    if best is None:
                        best = candidate
                except Exception:
                    continue
            return best

        existing = _resolve_imported_file(local_path)
        if existing:
            chat_session_id = str(kwargs.get("chat_session_id", "") or "default")
            _last_split_file_id_by_chat_session[chat_session_id] = existing.id
            return (
                f"File already in project (file_id={existing.id}): {local_path}. "
                f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{existing.id}' "
                f"(or empty file_id to use this just-imported file) to place it on the timeline."
            )

        # MUST run on main thread — files_model.add_files touches Qt objects
        def _do_add():
            files_model.add_files([local_path])

        _run_on_main_thread(_do_add, timeout=30)

        f = _resolve_imported_file(local_path)
        if f:
            chat_session_id = str(kwargs.get("chat_session_id", "") or "default")
            _last_split_file_id_by_chat_session[chat_session_id] = f.id
            # Stock imports must finish Gemini indexing before the agent continues.
            wait_err = _wait_for_file_indexing(f.id, files_model, timeout_sec=1800)
            summary = ""
            try:
                ai = f.data.get("ai_metadata") if isinstance(f.data, dict) else {}
                if isinstance(ai, dict):
                    summary = str(ai.get("short_summary") or "").strip()
                    # Reload file in case worker updated metadata during wait
                    refreshed = File.get(id=f.id)
                    if refreshed and isinstance(refreshed.data, dict):
                        ai2 = refreshed.data.get("ai_metadata") or {}
                        if isinstance(ai2, dict) and ai2.get("short_summary"):
                            summary = str(ai2.get("short_summary") or "").strip()
                            f = refreshed
            except Exception:
                pass
            if wait_err:
                log.warning("Stock media indexing wait: %s", wait_err)
                return (
                    f"Added to project: {local_path} (file_id={f.id}) but indexing "
                    f"did not finish: {wait_err}. "
                    f"Call reindex_project_file_tool before relying on search. "
                    f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{f.id}' "
                    f"(or empty file_id to use this just-imported file) to place it on the timeline."
                )
            log.info("Stock media added to project: %s (id=%s)", local_path, f.id)
            summary_bit = f" Summary: {summary}" if summary else ""
            return (
                f"Added to project and indexed: {local_path} (file_id={f.id})."
                f"{summary_bit} "
                f"IMPORTANT: Call add_clip_to_timeline_tool with file_id='{f.id}' "
                f"(or empty file_id to use this just-imported file) to place it on the timeline."
            )
        log.error("Stock media import produced no File record: %s", local_path)
        return (
            f"Error: Failed to import into project files (no file_id): {local_path}. "
            f"The download may be corrupt or unsupported by the media engine."
        )
    except Exception as e:
        log.error("add_stock_media_to_project: %s", e, exc_info=True)
        return f"Error: {e}"


def _wait_for_file_indexing(file_id: str, files_model, timeout_sec: int = 1800) -> str:
    """Block until Gemini indexing for file_id finishes. Returns error string or ''."""
    import time
    from classes.query import File
    from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

    fid = str(file_id or "")
    if not fid:
        return "missing file_id"
    deadline = time.time() + max(30, int(timeout_sec))
    # Give the queue a moment to start the worker
    time.sleep(0.5)
    while time.time() < deadline:
        try:
            if hasattr(files_model, "is_file_indexing") and files_model.is_file_indexing(fid):
                time.sleep(1.0)
                continue
            f = File.get(id=fid)
            if not f or not isinstance(f.data, dict):
                time.sleep(0.5)
                continue
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            idx = get_index_block(ai)
            if twelvelabs_is_indexed(idx):
                return ""
            status = str((idx or {}).get("status") or "").lower()
            if status in ("failed", "skipped"):
                return str((idx or {}).get("error") or status)
            if status in ("", "ready") and ai.get("analyzed"):
                return ""
            # Still queued / not started — keep waiting while queue may drain
            if hasattr(files_model, "_indexing_queue"):
                queued = any(str(qid) == fid for qid, _ in (files_model._indexing_queue or []))
                active = any(
                    str(getattr(w, "file_data", {}).get("id", "")) == fid
                    for w in (getattr(files_model, "_active_indexers", None) or [])
                )
                if not queued and not active and status not in ("indexing", "uploading"):
                    # No worker and not ready — treat as finished-or-never-started
                    if twelvelabs_is_indexed(idx) or ai.get("analyzed"):
                        return ""
                    if status in ("failed", "skipped"):
                        return str((idx or {}).get("error") or status)
                    # Media type may not have been queued yet; small grace then fail soft
                    time.sleep(1.0)
                    continue
        except Exception as exc:
            log.debug("wait indexing poll: %s", exc)
        time.sleep(1.0)
    return f"timed out after {timeout_sec}s"


def resummarize_project_file(file_id: str = "", **kwargs) -> str:
    """Re-run Pegasus audiovisual summary for an already-indexed project file.

    Requires an existing TwelveLabs video_id. Runs on a worker thread.
    """
    try:
        if not file_id:
            return "Error: file_id is required."

        def _read_file_meta():
            from classes.query import File
            from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block
            f = File.get(id=file_id)
            if not f:
                return None
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
            return {
                "path": f.data.get("path", ""),
                "duration": f.data.get("duration", 0) or 0,
                "indexed": twelvelabs_is_indexed(tl),
                "video_id": tl.get("video_id") or "",
            }

        meta = _run_on_main_thread(_read_file_meta, timeout=10)
        if meta is None:
            return f"Error: File not found (id={file_id})."

        MAX_SECONDS = 30 * 60
        if meta["duration"] > MAX_SECONDS:
            return (
                f"Error: Clip is {meta['duration'] / 60:.1f} min — exceeds the "
                f"30-minute summarize limit."
            )
        if not meta.get("indexed") or not meta.get("video_id"):
            return (
                f"Error: File {file_id} is not indexed yet. "
                "Call reindex_project_file_tool first, then resummarize."
            )

        def _kick_off_summarize():
            try:
                files_model = _get_app().window.files_model
                files_model._index_file_async(file_id, summarize_only=True)
            except Exception as exc:
                log.warning("resummarize_project_file: failed to start summarize: %s", exc)

        try:
            dispatcher = _get_dispatcher()
            dispatcher._dispatch.emit(
                (_kick_off_summarize, (), [None], [None], threading.Event())
            )
        except Exception:
            _kick_off_summarize()

        return (
            f"Pegasus summarize started for file {file_id} "
            f"(video_id={meta.get('video_id')}, {meta['path']})."
        )
    except Exception as e:
        log.error("resummarize_project_file: %s", e, exc_info=True)
        return f"Error: {e}"


def reindex_project_file(file_id: str = "", force: str = "false", **kwargs) -> str:
    """Re-index an existing project file in TwelveLabs.

    Skips when the file is already indexed unless force=true.
    Runs entirely on a worker thread.  Only the brief project-data reads
    (file path, duration, project id) are marshalled to the Qt main
    thread; the long-running reindex upload runs off the GUI thread.
    """
    try:
        if not file_id:
            return "Error: file_id is required."

        force_reindex = str(force or kwargs.get("force", "false")).strip().lower() in (
            "1", "true", "yes", "force",
        )

        def _read_project_state():
            from classes.query import File
            from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block
            f = File.get(id=file_id)
            if not f:
                return None
            project_id = ""
            try:
                project_id = _get_app().project.get("id") or ""
            except Exception:
                pass
            ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
            tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
            return {
                "path": f.data.get("path", ""),
                "duration": f.data.get("duration", 0) or 0,
                "project_id": project_id,
                "existing_index_id": tl.get("index_id") or "",
                "twelvelabs": tl,
                "already_indexed": twelvelabs_is_indexed(tl),
            }

        state = _run_on_main_thread(_read_project_state, timeout=10)
        if state is None:
            return f"Error: File not found (id={file_id})."

        if state.get("already_indexed") and not force_reindex:
            tl = state.get("twelvelabs") or {}
            return (
                f"File {file_id} is already indexed "
                f"(index_id={tl.get('index_id', '')}, video_id={tl.get('video_id', '')}). "
                "Pass force=true only when the file was replaced or indexing failed."
            )

        MAX_SECONDS = 30 * 60
        if state["duration"] > MAX_SECONDS:
            return (
                f"Error: Clip is {state['duration'] / 60:.1f} min — exceeds the "
                f"30-minute re-indexing limit."
            )

        from classes.api_client import get_backend_client
        from classes.credits_client import charge_operation_on_success, check_operation

        client = get_backend_client()
        if not client.is_indexing_configured():
            return "Gemini indexing is not configured — re-indexing unavailable."

        duration = float(state["duration"])
        _, _, blocked = check_operation(
            "indexing_per_minute",
            "video re-indexing",
            duration_seconds=duration,
        )
        if blocked:
            return blocked

        from classes.project_tl_index import build_project_index_name

        index_name = build_project_index_name(state["project_id"])

        result = client.reindex_video(
            file_id,
            state["path"],
            index_name=index_name,
            existing_index_id=state.get("existing_index_id") or "",
            force=force_reindex,
        )
        if isinstance(result, dict) and result.get("success"):
            charge_operation_on_success(
                True,
                "indexing_per_minute",
                provider="gemini",
                note=f"reindex {file_id}",
                duration_seconds=duration,
            )

            def _persist_index_metadata():
                from classes.query import File
                f = File.get(id=file_id)
                if not f:
                    return
                ai = f.data.get("ai_metadata") if isinstance(f.data.get("ai_metadata"), dict) else {}
                index_block = {
                    "status": "ready",
                    "index_id": result.get("index_id", ""),
                    "video_id": result.get("video_id", ""),
                    "index_name": index_name,
                    "provider": "gemini",
                }
                ai["index"] = index_block
                ai["twelvelabs"] = dict(index_block)  # legacy key for older readers
                f.data["ai_metadata"] = ai
                f.save()

            _run_on_main_thread(_persist_index_metadata, timeout=10)

            video_id = str(result.get("video_id") or "")
            summarize_note = ""
            if video_id:
                summarized = client.summarize_indexed_video(
                    video_id,
                    file_id=file_id,
                    index_id=str(result.get("index_id") or ""),
                    index_name=index_name,
                )
                if isinstance(summarized, dict) and summarized.get("analyzed"):
                    def _persist_summary():
                        from classes.query import File
                        f = File.get(id=file_id)
                        if not f:
                            return
                        tl = {
                            "status": "ready",
                            "index_id": result.get("index_id", ""),
                            "video_id": video_id,
                            "index_name": index_name,
                            "provider": "gemini",
                        }
                        summarized["index"] = {
                            **(summarized.get("index") or {}),
                            **tl,
                        }
                        summarized["twelvelabs"] = {
                            **(summarized.get("twelvelabs") or {}),
                            **tl,
                        }
                        f.data["ai_metadata"] = summarized
                        f.save()

                    _run_on_main_thread(_persist_summary, timeout=10)
                    summarize_note = " Gemini Flash summary updated."
                else:
                    summarize_note = (
                        f" Summarize failed: "
                        f"{(summarized or {}).get('error', 'unknown')}"
                    )

            return (
                f"Re-indexing complete for file {file_id}. "
                f"index_id={result.get('index_id', '')}  video_id={video_id}."
                f"{summarize_note}"
            )
        return f"Re-indexing failed: {result.get('error', result.get('message', 'unknown'))}"
    except Exception as e:
        log.error("reindex_project_file: %s", e, exc_info=True)
        return f"Error: {e}"


def get_clips_with_full_metadata(detail_level="summary", **kwargs) -> str:
    """Return project files with AI metadata for planning (excludes hidden subclips by default)."""
    try:
        from classes.query import File
        from classes.timeline_clip_context import resolve_parent_file_id

        level = str(detail_level or kwargs.get("detail_level") or "summary").lower().strip()
        files = File.filter()
        if not files:
            return "No files in project."
        lines = ["Project files with full metadata:"]
        for f in files:
            d = f.data
            if d.get("zenvi_subclip") and level != "full":
                continue
            dur = d.get("duration", 0) or 0
            m, s = divmod(int(dur), 60)
            media_type = d.get("media_type", "?")
            name = d.get("name") or d.get("path", "?").split("/")[-1]
            ai = d.get("ai_metadata") or {}
            analyzed = ai.get("analyzed", False)
            tl = ai.get("twelvelabs", {}) or {}
            from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block
            from classes.tl_search_strategy import infer_tl_search_hint
            indexed = twelvelabs_is_indexed(tl)
            hint = infer_tl_search_hint(ai, name)
            scene_count = len(ai.get("scene_descriptions") or [])
            chapter_count = len(ai.get("chapters") or [])
            short = (ai.get("short_summary") or "")[:160]
            desc = (ai.get("description") or "")[:400]
            sounds = (ai.get("sounds") or "")[:160]
            transcript = (ai.get("transcript") or "")[:200]
            parent_id = resolve_parent_file_id(d, file_id=str(f.id or ""))
            alias_part = ""
            if d.get("zenvi_subclip") and parent_id and parent_id != str(f.id):
                alias_part = f"  alias_of={parent_id}\n"
            lines.append(
                f"\n  media_bin_file_id={f.id}  name={name}  type={media_type}  "
                f"duration={m}:{s:02d}\n"
                f"{alias_part}"
                f"    analyzed={analyzed}  indexed={indexed}  "
                f"chapter_count={chapter_count}  scene_count={scene_count}\n"
                f"    tl_search_hint={hint}\n"
                f"    twelvelabs_index_id={tl.get('index_id', '')}\n"
                f"    twelvelabs_index_name={tl.get('index_name', '')}\n"
                f"    twelvelabs_video_id={tl.get('video_id', '')}\n"
                f"    short_summary={short}\n"
                f"    description={desc}"
            )
            # Pre-Pegasus projects (or failed summarize) may only have Gemini tags.
            if not short and not desc:
                tags = ai.get("tags") if isinstance(ai.get("tags"), dict) else {}
                legacy_bits = []
                for key in ("objects", "scenes", "activities", "mood"):
                    vals = tags.get(key) or []
                    if isinstance(vals, list) and vals:
                        legacy_bits.append(
                            f"{key}=[{', '.join(str(v) for v in vals[:8] if v)}]"
                        )
                if legacy_bits:
                    lines.append(f"    legacy_tags={' '.join(legacy_bits)}")
                elif sounds or transcript:
                    if sounds:
                        lines.append(f"    sounds={sounds}")
                    if transcript:
                        lines.append(f"    transcript={transcript}")
            if level == "full":
                if sounds:
                    lines.append(f"    sounds={sounds}")
                if transcript:
                    lines.append(f"    transcript={transcript}")
            chapters = ai.get("chapters") or []
            if chapters:
                limit = len(chapters) if level == "full" else min(3, len(chapters))
                lines.append("    chapter_snippets:")
                for ch in chapters[:limit]:
                    if isinstance(ch, dict) and (ch.get("summary") or ch.get("title")):
                        t0 = float(ch.get("start", 0) or 0)
                        t1 = float(ch.get("end", t0) or t0)
                        title = str(ch.get("title") or "")
                        summary = str(ch.get("summary") or "")[:160]
                        lines.append(
                            f"      [{_fmt_mmss(t0)}-{_fmt_mmss(t1)}] {title}: {summary}"
                        )
            snippets = ai.get("scene_descriptions") or []
            if snippets and not chapters:
                limit = len(snippets) if level == "full" else min(3, len(snippets))
                lines.append("    scene_snippets:")
                for sc in snippets[:limit]:
                    if isinstance(sc, dict) and sc.get("description"):
                        t = float(sc.get("time", 0) or 0)
                        lines.append(f"      [{_fmt_mmss(t)}] {str(sc['description'])[:160]}")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_clips_with_full_metadata: %s", e, exc_info=True)
        return f"Error: {e}"


def _overlay_index_boosts(contexts) -> list:
    """Optional TwelveLabs search boosts mapped onto timeline times.

    Falls back to [] when indexing is unavailable (lexical scoring still applies).
    """
    boosts = []
    try:
        from classes.api_client import get_backend_client
        from classes.project_tl_index import (
            collect_project_twelvelabs_index,
            map_search_hit_to_file,
        )
        from classes.mg_placement import AVOID_QUERY, PREFER_QUERY

        info = collect_project_twelvelabs_index()
        index_id = str((info or {}).get("index_id") or "").strip()
        if not index_id:
            return []
        client = get_backend_client()
        if not client.is_indexing_configured():
            return []
        video_map = (info or {}).get("video_map") or {}
        queries = (
            (AVOID_QUERY, -3, True, False),
            (PREFER_QUERY, 3, False, True),
        )
        by_file = {}
        for ctx in contexts or []:
            fid = str(getattr(ctx, "file_id", "") or "")
            pfid = str(getattr(ctx, "parent_file_id", "") or "")
            for key in {fid, pfid}:
                if key:
                    by_file.setdefault(key, []).append(ctx)

        for query, delta, is_avoid, is_prefer in queries:
            resp = client.search(query, top_k=12, index_id=index_id, page_limit=24)
            if resp.get("error"):
                continue
            for r in resp.get("results") or []:
                if not isinstance(r, dict):
                    continue
                fid, _fname = map_search_hit_to_file(r, video_map)
                fid = str(fid or "").strip()
                if not fid or fid not in by_file:
                    continue
                try:
                    hit_start = float(r.get("start") or 0)
                except (TypeError, ValueError):
                    continue
                for ctx in by_file[fid]:
                    pos = float(getattr(ctx, "timeline_position", 0) or 0)
                    end = float(getattr(ctx, "timeline_end", pos + 1) or (pos + 1))
                    src_start = float(getattr(ctx, "source_start", 0) or 0)
                    clip_dur = max(0.1, end - pos)
                    local = hit_start - src_start
                    if 0 <= local <= clip_dur:
                        boosts.append(
                            {
                                "t": pos + local,
                                "score_delta": delta,
                                "avoid": is_avoid,
                                "prefer": is_prefer,
                            }
                        )
    except Exception as exc:
        log.debug("propose_overlay_windows: index boost skipped: %s", exc)
    return boosts


def propose_overlay_windows(beat_count="4", prefer_transparent="true", **_kw) -> str:
    """Propose safe timeline windows for MG overlays/plates.

    Uses scene metadata + lexical/embedding-index scoring. Returns JSON with
    windows[{t, score, avoid, prefer, scene_label, track_hint, confidence,
    suggest_transparent, layout_region}].

    Agent must: bake layout_region into session/draft.html; publish with matching
    transparent flag; place via place_motion_graphic_tool (overlay|gap|cut_in).
    """
    import json

    from classes.mg_placement import (
        apply_embedding_time_boosts,
        layout_region_for,
        score_scene_blob,
    )

    try:
        n = int(float(str(beat_count or "4").strip() or "4"))
    except Exception:
        n = 4
    n = max(1, min(8, n))
    prefer_tr = str(prefer_transparent or "true").strip().lower() not in ("false", "0", "no")

    try:
        from classes.timeline_clip_context import enumerate_timeline_contexts

        contexts = enumerate_timeline_contexts() or []
    except Exception as exc:
        log.warning("propose_overlay_windows: timeline read failed: %s", exc)
        contexts = []

    overlay_layer = 3000000
    mid_layer = 2000000
    try:
        app = _get_app()
        layers = app.project.get("layers") or []
        nums = sorted(int(L.get("number", 0)) for L in layers if isinstance(L, dict))
        if nums:
            overlay_layer = nums[-1] + 1000000 if nums[-1] < 9000000 else nums[-1]
            mid_layer = nums[len(nums) // 2] if len(nums) > 1 else nums[0]
    except Exception:
        pass

    segments = []
    windows = []
    timeline_end = 0.0
    for ctx in contexts:
        pos = float(ctx.timeline_position or 0)
        end = float(ctx.timeline_end or (pos + 1.0))
        timeline_end = max(timeline_end, end)
        ai = ctx.effective_metadata or {}
        src_start = float(getattr(ctx, "source_start", 0) or 0)
        clip_dur = max(0.1, end - pos)

        text_bits = [
            str(ctx.summary_preview or ""),
            str(ai.get("short_summary") or ""),
            str(ai.get("description") or ""),
        ]
        for ch in ai.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            text_bits.append(str(ch.get("title") or ""))
            text_bits.append(str(ch.get("summary") or ""))
            ch_start = ch.get("start")
            try:
                if ch_start is not None:
                    local = float(ch_start) - src_start
                    if 0 <= local <= clip_dur:
                        t = pos + local
                        sc, avoid, prefer = score_scene_blob(
                            f"{ch.get('title') or ''} {ch.get('summary') or ''}"
                        )
                        windows.append(
                            {
                                "t": t,
                                "score": sc,
                                "avoid": avoid,
                                "prefer": prefer,
                                "label": str(ch.get("title") or ch.get("summary") or "")[:80],
                            }
                        )
            except (TypeError, ValueError):
                pass
        for scn in ai.get("scene_descriptions") or []:
            if not isinstance(scn, dict):
                continue
            desc = str(scn.get("description") or "")
            text_bits.append(desc)
            try:
                st = scn.get("time")
                if st is None:
                    st = scn.get("start")
                if st is not None:
                    local = float(st) - src_start
                    if 0 <= local <= clip_dur:
                        t = pos + local
                        sc, avoid, prefer = score_scene_blob(desc)
                        windows.append(
                            {
                                "t": t,
                                "score": sc + 1,
                                "avoid": avoid,
                                "prefer": prefer,
                                "label": desc[:80],
                            }
                        )
            except (TypeError, ValueError):
                pass

        blob = " ".join(text_bits)
        sc, avoid, prefer = score_scene_blob(blob)
        segments.append(
            {
                "position": pos,
                "end": end,
                "avoid": avoid,
                "prefer": prefer,
                "score": sc,
            }
        )
        for frac, bonus in ((0.08, 0), (0.5, 0), (0.88, 0)):
            t = pos + clip_dur * frac
            windows.append(
                {
                    "t": t,
                    "score": sc + bonus,
                    "avoid": avoid,
                    "prefer": prefer,
                    "label": (ctx.title or blob)[:80],
                }
            )

    # Index / embedding search boosts (best-effort)
    apply_embedding_time_boosts(windows, _overlay_index_boosts(contexts))

    segments.sort(key=lambda s: s["position"])
    gaps = []
    if segments:
        if segments[0]["position"] > 0.4:
            gaps.append(max(0.0, segments[0]["position"] * 0.15))
        for a, b in zip(segments, segments[1:]):
            if b["position"] - a["end"] >= 0.4:
                gaps.append((a["end"] + b["position"]) / 2.0)
                windows.append(
                    {
                        "t": a["end"] - 0.15,
                        "score": 2,
                        "avoid": False,
                        "prefer": True,
                        "label": "near cut",
                    }
                )
    else:
        timeline_end = 24.0
        for t in (0.0, 6.0, 12.0, 18.0, 22.0):
            windows.append({"t": t, "score": 1, "avoid": False, "prefer": True, "label": ""})

    if timeline_end <= 0:
        timeline_end = 24.0

    if n == 1:
        anchors = [0.0 if timeline_end < 1 else min(1.0, timeline_end * 0.1)]
    else:
        anchors = [timeline_end * (i / (n - 1)) for i in range(n)]

    def _snap(target, used):
        target = max(0.0, min(float(timeline_end), float(target)))
        cands = sorted(windows, key=lambda w: (abs(w["t"] - target), -w["score"]))
        for w in cands:
            t = max(0.0, min(timeline_end, float(w["t"])))
            if prefer_tr and w.get("avoid") and w["score"] < 0:
                continue
            if all(abs(t - u) >= 1.2 for u in used):
                conf = (
                    "high"
                    if w.get("prefer") or w["score"] >= 2
                    else ("low" if w.get("avoid") else "medium")
                )
                return (
                    t,
                    w.get("label") or "",
                    conf,
                    bool(w.get("avoid")),
                    bool(w.get("prefer")),
                    int(w["score"]),
                )
        return target, "", "medium", False, False, 0

    used_positions = []
    out_windows = []
    for i in range(n):
        use_gap = (not prefer_tr) or (i == max(1, n // 2) and gaps)
        pos = None
        scene_label = ""
        conf = "medium"
        avoid = False
        prefer = False
        score = 0
        track_hint = overlay_layer
        is_gap = False
        if use_gap and gaps:
            for g in gaps:
                g = max(0.0, min(timeline_end, float(g)))
                if all(abs(g - u) >= 1.0 for u in used_positions):
                    pos = g
                    scene_label = "gap — opaque plate candidate"
                    conf = "high"
                    track_hint = mid_layer
                    is_gap = True
                    prefer = True
                    break
        if pos is None:
            pos, scene_label, conf, avoid, prefer, score = _snap(anchors[i], used_positions)
            track_hint = overlay_layer
            is_gap = False
        pos = round(max(0.0, min(timeline_end, float(pos))), 2)
        used_positions.append(pos)
        region = layout_region_for(
            avoid=avoid, prefer=prefer, is_gap=is_gap, score=score
        )
        suggest_tr = bool(prefer_tr and track_hint == overlay_layer and not is_gap)
        out_windows.append(
            {
                "t": pos,
                "score": score,
                "avoid": avoid,
                "prefer": prefer,
                "scene_label": scene_label[:80],
                "track_hint": int(track_hint),
                "confidence": conf,
                "suggest_transparent": suggest_tr,
                "layout_region": region,
                "place_mode": (
                    "gap" if is_gap else ("overlay" if suggest_tr else "cut_in")
                ),
            }
        )

    payload = {
        "timeline_end": round(timeline_end, 2),
        "clip_count": len(segments),
        "windows": out_windows,
        "guidance": (
            "For each beat: decide transparent vs opaque first. "
            "layout_region → bake into session/draft.html "
            "(lower_third/corner_* = non-blocking overlay HTML; full_frame = sting; "
            "mid_plate = opaque plate). "
            "publish_session_draft_tool(transparent=true|false) matching suggest_transparent. "
            "Then place_motion_graphic_tool(mode=overlay|gap|cut_in) — never stack opaque "
            "plates over hero footage with add_clip on the top overlay track."
        ),
    }
    return json.dumps(payload, indent=2)


def place_motion_graphic(
    file_id="",
    position_seconds="",
    duration_seconds="",
    mode="overlay",
    track="",
    layout_region="",
    **_kw,
) -> str:
    """Place a HyperFrames render with overlay/gap/cut_in enforcement.

    mode=overlay → transparent file on a HIGH layer (refuses opaque).
    mode=gap → opaque only when primary track is clear at [t,t+dur).
    mode=cut_in → opaque: ripple primary-track clips at/after t, then place as a cut.
    """
    from classes.mg_placement import (
        file_looks_transparent,
        primary_track_overlaps,
        ripple_positions,
    )
    from classes.query import Clip, File
    from classes.track_display import (
        format_track_label_for_llm,
        normalize_track_or_layer_arg,
    )

    try:
        fid = str(file_id or "").strip()
        if not fid:
            return "Error: file_id is required for place_motion_graphic_tool"
        f = File.get(id=fid)
        if not f:
            return f"Error: File not found for id={fid}."
        file_data = dict(f.data or {})
        is_transparent = file_looks_transparent(file_data)
        mode_s = str(mode or "overlay").strip().lower() or "overlay"
        if mode_s not in ("overlay", "gap", "cut_in"):
            return "Error: mode must be overlay|gap|cut_in"

        try:
            t = float(str(position_seconds).strip() or "0")
        except (TypeError, ValueError):
            return "Error: position_seconds must be a number"
        t = max(0.0, t)
        try:
            dur = float(str(duration_seconds).strip() or "0")
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            try:
                dur = float(file_data.get("duration") or 0) or float(
                    (file_data.get("reader") or {}).get("duration") or 0
                )
            except (TypeError, ValueError):
                dur = 3.0
        dur = max(0.5, min(dur, 60.0))

        app = _get_app()
        layers = app.project.get("layers") or []
        nums = sorted(
            int(L.get("number", 0))
            for L in layers
            if isinstance(L, dict) and L.get("number") is not None
        )
        if not nums:
            nums = [1000000, 2000000, 3000000]
        primary_layer = nums[0]
        mid_layer = nums[len(nums) // 2] if len(nums) > 1 else nums[0]
        overlay_layer = nums[-1]

        if str(track or "").strip():
            resolved, err = normalize_track_or_layer_arg(str(track).strip(), layers)
            if err:
                return err
            track_num = int(resolved)
        elif mode_s == "overlay":
            track_num = int(overlay_layer)
        elif mode_s == "gap":
            track_num = int(mid_layer)
        else:
            track_num = int(primary_layer)

        clips_raw = [
            dict(c.data or {})
            for c in Clip.filter()
            if isinstance(getattr(c, "data", None), dict)
        ]

        if mode_s == "overlay":
            if not is_transparent:
                return (
                    "Error: mode=overlay requires a transparent HyperFrames import "
                    "(ai_metadata.transparent / transparent_overlay tag / webm). "
                    "Use mode=gap or mode=cut_in for opaque plates — never stack opaque "
                    "over hero footage."
                )
            if track_num < mid_layer:
                track_num = int(overlay_layer)
        else:
            # gap / cut_in → opaque path
            if is_transparent:
                return (
                    "Error: mode=%s is for opaque plates. Transparent overlays must use "
                    "mode=overlay on a high track." % mode_s
                )
            if mode_s == "gap":
                if primary_track_overlaps(
                    clips_raw, layer=int(primary_layer), t0=t, t1=t + dur
                ):
                    return (
                        "Error: gap mode refused — primary track has footage overlapping "
                        f"[{t:.2f}, {t + dur:.2f}). Use mode=cut_in to ripple clips, or "
                        "pick a true gap from propose_overlay_windows_tool."
                    )

        region = str(layout_region or "").strip()

        def _ripple_and_stamp():
            if mode_s == "cut_in":
                shifts = ripple_positions(
                    clips_raw, layer=int(primary_layer), t=t, delta=dur
                )
                for cid, new_pos in shifts:
                    app.updates.update(
                        ["clips", {"id": cid}],
                        {"position": float(new_pos)},
                    )
            try:
                ai = dict(file_data.get("ai_metadata") or {})
                ai["mg_placement"] = {
                    "mode": mode_s,
                    "layout_region": region,
                    "transparent": bool(is_transparent),
                    "position_seconds": t,
                    "duration_seconds": dur,
                    "track": track_num,
                }
                if not is_transparent:
                    ai["transparent"] = False
                f.data["ai_metadata"] = ai
                if hasattr(f, "save"):
                    f.save()
                else:
                    app.updates.update(
                        ["files", {"id": fid}],
                        {"ai_metadata": ai},
                    )
            except Exception as stamp_exc:
                log.debug("mg_placement stamp failed: %s", stamp_exc)
            return True

        _run_on_main_thread(_ripple_and_stamp)
        # add_clip marshals Qt mutations itself
        result = add_clip_to_timeline(
            file_id=fid,
            position_seconds=str(t),
            track=str(track_num),
            duration_seconds=str(dur),
            chat_session_id=_kw.get("chat_session_id", ""),
        )

        track_lbl = format_track_label_for_llm(int(track_num), layers)
        if isinstance(result, str) and result.startswith("Error"):
            return result
        return (
            f"{result} [mg_place mode={mode_s} layout_region={region or 'n/a'} "
            f"transparent={is_transparent} track={track_lbl}]. "
            "NEXT: get_timeline_state_tool once to verify, then continue next beat."
        )
    except Exception as e:
        log.error("place_motion_graphic: %s", e, exc_info=True)
        return f"Error: {e}"


def suggest_motion_graphics_placements(brief="", beat_count="4", **_kw) -> str:
    """Deprecated — use propose_overlay_windows_tool (timing) + agent-authored beats_json."""
    return (
        "Error: suggest_motion_graphics_placements_tool is removed. "
        "Call propose_overlay_windows_tool(beat_count=...) for timing only, then "
        "search_motion_blocks_tool / sandbox_compose_motion_tool, and "
        "motion_graphics_package(beats_json=[{title, block_query|block_id, position_seconds, ...}]). "
        "Never put the creative brief into title=."
    )


def get_timeline_placements_metadata(detail_level="summary", **_kw) -> str:
    """Return one row per timeline clip with trim-aware effective metadata."""
    try:
        from classes.timeline_clip_context import enumerate_timeline_contexts

        level = str(detail_level or _kw.get("detail_level") or "summary").lower().strip()
        contexts = enumerate_timeline_contexts()
        if not contexts:
            return "No clips on timeline."

        dupe_groups: dict[str, list] = {}
        for ctx in contexts:
            key = f"{ctx.file_id}:{ctx.layer}"
            dupe_groups.setdefault(key, []).append(ctx)

        lines = [f"Timeline placements ({len(contexts)}):"]
        for ctx in sorted(contexts, key=lambda c: (int(c.layer or 0), c.timeline_position)):
            dup_key = f"{ctx.file_id}:{ctx.layer}"
            dupes = dupe_groups.get(dup_key, [])
            occ_hint = ""
            if len(dupes) > 1:
                ranked = sorted(dupes, key=lambda c: c.timeline_position)
                for idx, dc in enumerate(ranked, 1):
                    if dc.timeline_clip_id == ctx.timeline_clip_id:
                        occ_hint = f" occurrence_hint={idx}"
                        break
            group_id = dup_key if len(dupes) > 1 else ""
            ai = ctx.effective_metadata or {}
            scenes = ai.get("scene_descriptions") or []
            scene_limit = len(scenes) if level == "full" else min(3, len(scenes))
            lines.append(
                f"\n  timeline_clip_id={ctx.timeline_clip_id} file_id={ctx.file_id} "
                f"parent_file_id={ctx.parent_file_id}{occ_hint}\n"
                f"    title={ctx.title!r} track={ctx.layer} ui_track={ctx.ui_track} "
                f"position={ctx.timeline_position:.2f}s timeline_end={ctx.timeline_end:.2f}s\n"
                f"    source_window={ctx.source_start:.2f}-{ctx.source_end:.2f}s "
                f"index_status={ctx.index_status!r} duplicate_group={group_id!r}\n"
                f"    summary_preview={ctx.summary_preview!r}"
            )
            if scenes and scene_limit:
                lines.append("    effective_scenes:")
                for sc in scenes[:scene_limit]:
                    if isinstance(sc, dict) and sc.get("description"):
                        t = float(sc.get("time", 0) or 0)
                        lines.append(f"      [{_fmt_mmss(t)}] {str(sc['description'])[:160]}")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_timeline_placements_metadata: %s", e, exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool name → handler mapping
# ---------------------------------------------------------------------------

def get_timeline_state(**_kw) -> str:
    """Return a structured snapshot of the current timeline: tracks, clips with positions, and effects.

    Always lists every project layer (including empty tracks) so planners can pick ui_track values
    even when the timeline has no clips yet.
    """
    try:
        from classes.query import Clip
        from classes.timeline_clip_context import build_timeline_clip_context

        app = _get_app()

        layers = app.project.get("layers") or []

        clips = Clip.filter()
        effects_raw = app.project.get("effects") or []

        # Group clips by layer (store tuple of clip object and data)
        by_layer = {}
        for c in clips:
            d = c.data
            layer = d.get("layer", 0)
            by_layer.setdefault(layer, []).append((c, d))

        def _track_heading(layer_num, layer_obj=None):
            ui = layer_number_to_display_index(int(layer_num), layers)
            tid = ""
            label = ""
            if layer_obj is not None:
                tid = str(layer_obj.get("id", ""))
                label = (layer_obj.get("label") or layer_obj.get("name") or "").strip()
            else:
                for L in layers:
                    if int(L.get("number") or 0) == int(layer_num):
                        tid = str(L.get("id", ""))
                        label = (L.get("label") or L.get("name") or "").strip()
                        break
            z_from_bottom = (ui - 1) if ui is not None else "?"
            parts = [f"layer_number={layer_num}", f"z_from_bottom={z_from_bottom}"]
            if ui is not None:
                parts.append(f"ui_track={ui}")
            if tid:
                parts.append(f"track_id={tid}")
            if label:
                parts.append(f"label={label!r}")
            return " | ".join(parts)

        lines = ["=== TIMELINE STATE ==="]
        lines.append(
            "Z-ORDER: higher layer_number covers lower. "
            "Track labels are cosmetic names only — never infer priority from the label text. "
            "Call list_layers_tool for TRACK_STACK_JSON before multi-track placement. "
            "Hero/foreground → highest layer_number; backgrounds → lowest."
        )
        if not clips and not effects_raw:
            lines.append("Timeline is empty — no clips or effects have been added yet.")

        # Always emit every project track (high layer number first = top of stack).
        from classes.track_display import track_stack_json

        asc = layers_sorted_by_number(layers)
        emitted_layer_nums = set()
        if asc:
            for L in reversed(asc):
                layer_num = int(L.get("number") or 0)
                emitted_layer_nums.add(layer_num)
                lines.append(f"\n{_track_heading(layer_num, L)}:")
                layer_clips = by_layer.get(layer_num) or []
                if not layer_clips:
                    lines.append("  (empty)")
                    continue
                for c, d in sorted(layer_clips, key=lambda x: x[1].get("position", 0)):
                    clip_dur = float(d.get("end", 0) or 0) - float(d.get("start", 0) or 0)
                    if clip_dur <= 0:
                        try:
                            from classes.query import File as _FileDur
                            _fo = _FileDur.get(id=d.get("file_id", ""))
                            if _fo:
                                from classes.ai_metadata_utils import get_source_window
                                ss, se = get_source_window(d, _fo.data)
                                clip_dur = se - ss
                        except Exception:
                            clip_dur = 0
                    clip_end = d.get("position", 0) + clip_dur
                    summary_preview = ""
                    analyzed_part = ""
                    source_part = ""
                    try:
                        from classes.query import File as _File
                        fobj = _File.get(id=d.get("file_id", ""))
                        if fobj and isinstance(fobj.data, dict):
                            fname = (
                                fobj.data.get("name")
                                or os.path.basename(str(fobj.data.get("path") or ""))
                                or d.get("file_id", "?")
                            )
                            ctx = build_timeline_clip_context(c, d, fobj.data, layers=layers)
                            summary_preview = ctx.summary_preview
                            source_part = (
                                f" source={ctx.source_start:.1f}-{ctx.source_end:.1f}s"
                            )
                            if not _file_is_analyzed(fobj.data):
                                analyzed_part = " analyzed=False"
                        else:
                            fname = d.get("file_id", "?")
                    except Exception:
                        fname = d.get("file_id", "?")
                    summary_part = f" summary_preview={summary_preview!r}" if summary_preview else ""
                    lines.append(
                        f"  timeline_clip_id={c.id} media_bin_file_id={d.get('file_id','')} "
                        f"file={fname!r} title={(d.get('title') or d.get('label') or '')!r}"
                        f"{summary_part}{analyzed_part}{source_part}"
                        f" @ {d.get('position',0):.2f}s–{clip_end:.2f}s (dur={clip_dur:.2f}s)"
                    )
        else:
            lines.append("\n(No layers/tracks in project.)")

        # Orphan clips on layer numbers not in project.layers
        for layer_num in sorted(by_layer.keys(), reverse=True):
            if int(layer_num) in emitted_layer_nums:
                continue
            lines.append(f"\n{_track_heading(layer_num)}:")
            for c, d in sorted(by_layer[layer_num], key=lambda x: x[1].get("position", 0)):
                clip_dur = float(d.get("end", 0) or 0) - float(d.get("start", 0) or 0)
                clip_end = d.get("position", 0) + clip_dur
                lines.append(
                    f"  timeline_clip_id={c.id} media_bin_file_id={d.get('file_id','')} "
                    f"file={d.get('file_id','')!r} title={(d.get('title') or d.get('label') or '')!r}"
                    f" @ {d.get('position',0):.2f}s–{clip_end:.2f}s (dur={clip_dur:.2f}s)"
                )

        # Summarise effects/transitions
        if effects_raw:
            lines.append(f"\nEffects/Transitions ({len(effects_raw)}):")
            for e in effects_raw:
                lines.append(
                    f"  id={e.get('id','')} title={e.get('title','?')!r}"
                    f" layer={e.get('layer','')} @ {e.get('position',0):.2f}s end={e.get('end',0):.2f}s"
                )

        # Total timeline duration
        if not clips:
            lines.append("\nTotal timeline duration: 0.00s")
        else:
            all_ends = [
                d.get("position", 0) + (d.get("end", 0) - d.get("start", 0))
                for c in clips for d in [c.data]
            ]
            if all_ends:
                lines.append(f"\nTotal timeline duration: {max(all_ends):.2f}s")

        lines.append(f"\nTRACK_STACK_JSON={track_stack_json(layers)}")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_timeline_state: %s", e, exc_info=True)
        return f"Error: {e}"


def build_editor_snapshot_for_chat(max_chars: int = 5500) -> str:
    """Compact media-bin + timeline for LLM grounding. Call from Qt GUI thread.

    Empty timeline must NOT look like an empty project — always list media-bin
    files (with summary_preview when available) so the agent can plan from them.
    """
    try:
        import os
        from classes.query import File
        from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

        files = File.filter() or []
        media_lines: list[str] = []
        for f in files:
            d = f.data if isinstance(getattr(f, "data", None), dict) else {}
            if d.get("zenvi_subclip"):
                continue
            name = d.get("name") or os.path.basename(str(d.get("path") or "")) or "?"
            dur = float(d.get("duration", 0) or 0)
            ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
            analyzed = bool(ai.get("analyzed"))
            indexed = twelvelabs_is_indexed(get_index_block(ai))
            preview = _summary_preview_for_file_data(d)
            media_lines.append(
                f"  media_bin_file_id={f.id} name={name!r} duration={dur:.2f}s "
                f"analyzed={analyzed} indexed={indexed} "
                f"summary_preview={preview!r}"
            )

        n_visible = len(media_lines)
        head = (
            f"[Editor snapshot]\n"
            f"Project files count: {n_visible}\n"
            f"NOTE: media-bin files exist independently of the timeline; "
            f"an empty timeline does NOT mean no project files.\n"
        )
        if media_lines:
            # Cap listing so snapshot stays within max_chars with timeline.
            shown = media_lines[:20]
            head += "MEDIA_BIN:\n" + "\n".join(shown)
            if n_visible > len(shown):
                head += f"\n  ... and {n_visible - len(shown)} more (use list_files_tool / get_clips_with_full_metadata_tool)\n"
            else:
                head += "\n"
        else:
            head += "MEDIA_BIN: (empty)\n"

        tl = get_timeline_state()
        body = (tl or "").strip()
        out = f"{head}TIMELINE:\n{body}\n" if body else f"{head}TIMELINE: (empty or unavailable)\n"
        if len(out) > max_chars:
            out = out[: max(0, max_chars - 24)].rstrip() + "\n... (truncated)\n"
        return out + "[/Editor snapshot]\n"
    except Exception as e:
        log.debug("build_editor_snapshot_for_chat: %s", e)
        return ""


# Tools exposed to the main chat / video / transitions agents.
AGENT_TOOL_HANDLERS = {
    # Project
    "get_project_info_tool": get_project_info,
    "list_files_tool": list_files,
    "list_clips_tool": list_clips,
    "list_layers_tool": list_layers,
    "list_markers_tool": list_markers,
    "new_project_tool": new_project,
    "save_project_tool": save_project,
    "open_project_tool": open_project,
    # Playback
    "watch_clip_tool": watch_clip_and_play,
    "play_tool": play,
    "go_to_start_tool": go_to_start,
    "go_to_end_tool": go_to_end,
    "undo_tool": undo,
    "redo_tool": redo,
    # Timeline
    "add_track_tool": add_track,
    "add_marker_tool": add_marker,
    "remove_clip_tool": remove_clip,
    "delete_clips_on_track_tool": delete_clips_on_track,
    "zoom_in_tool": zoom_in,
    "zoom_out_tool": zoom_out,
    "center_on_playhead_tool": center_on_playhead,
    "import_files_tool": import_files,
    # Export
    "export_video_tool": export_video,
    "get_export_settings_tool": get_export_settings,
    "set_export_setting_tool": set_export_setting,
    # Clips
    "get_file_info_tool": get_file_info,
    "split_file_add_clip_tool": split_file_add_clip,
    "add_clip_to_timeline_tool": add_clip_to_timeline,
    "import_video_url_and_add_to_timeline_tool": import_video_url_and_add_to_timeline,
    "slice_clip_at_playhead_tool": slice_clip_at_playhead,
    # Search / slice / modify (tag-query resolved)
    "search_clips_tool": search_clips,
    "search_clip_scenes_tool": search_clip_scenes,
    "get_project_catalog_tool": get_project_catalog,
    "slice_clip_at_best_match_tool": slice_clip_at_best_match,
    "suggest_motion_graphics_placements_tool": suggest_motion_graphics_placements,
    "propose_overlay_windows_tool": propose_overlay_windows,
    "place_motion_graphic_tool": place_motion_graphic,
    # Remotion / HyperFrames
    "fetch_motion_graphics_video_tool": fetch_motion_graphics_video,
    "fetch_remotion_video_from_supabase_tool": fetch_motion_graphics_video,
    # Video generation / AI edit
    "generate_video_and_add_to_timeline_tool": generate_video_and_add_to_timeline,
    "modify_clip_tool": modify_clip,
    "generate_transition_clip_tool": generate_transition_clip,
    # OpenShot transitions (mask/dissolve)
    "list_transitions_tool": list_transitions,
    "search_transitions_tool": search_transitions,
    "apply_transition_tool": apply_transition,
    # TTS
    "generate_tts_and_add_to_timeline_tool": generate_tts_and_add_to_timeline,
    # Stock / planning
    "import_stock_media_tool": import_stock_media,
    "resummarize_project_file_tool": resummarize_project_file,
    "reindex_project_file_tool": reindex_project_file,
    "get_clips_with_full_metadata_tool": get_clips_with_full_metadata,
    "get_timeline_placements_metadata_tool": get_timeline_placements_metadata,
    "get_timeline_state_tool": get_timeline_state,
}

TOOL_HANDLERS = dict(AGENT_TOOL_HANDLERS)

# Humanized titles for chat tool-block headers (main agent tools only).
TOOL_DISPLAY_LABELS = {
    "get_project_info_tool": "Read project info",
    "list_files_tool": "List files",
    "list_clips_tool": "List clips",
    "list_layers_tool": "List tracks",
    "list_markers_tool": "List markers",
    "new_project_tool": "New project",
    "save_project_tool": "Save project",
    "open_project_tool": "Open project",
    "watch_clip_tool": "Load and play clip",
    "play_tool": "Toggle playback",
    "go_to_start_tool": "Seek to start",
    "go_to_end_tool": "Seek to end",
    "undo_tool": "Undo",
    "redo_tool": "Redo",
    "add_track_tool": "Add track",
    "add_marker_tool": "Add marker",
    "remove_clip_tool": "Remove clip",
    "delete_clips_on_track_tool": "Delete clips on track",
    "zoom_in_tool": "Zoom in",
    "zoom_out_tool": "Zoom out",
    "center_on_playhead_tool": "Center on playhead",
    "import_files_tool": "Import files",
    "export_video_tool": "Export video",
    "get_export_settings_tool": "Read export settings",
    "set_export_setting_tool": "Update export setting",
    "get_file_info_tool": "Read file info",
    "split_file_add_clip_tool": "Split clip and add to timeline",
    "add_clip_to_timeline_tool": "Add clip to timeline",
    "import_video_url_and_add_to_timeline_tool": "Import video to timeline",
    "slice_clip_at_playhead_tool": "Slice clip at playhead",
    "search_clips_tool": "Search project index",
    "search_clip_scenes_tool": "Search clip scenes",
    "get_project_catalog_tool": "Read project catalog",
    "slice_clip_at_best_match_tool": "Slice clip at best match",
    "suggest_motion_graphics_placements_tool": "Suggest MG placements (deprecated)",
    "propose_overlay_windows_tool": "Propose overlay windows",
    "place_motion_graphic_tool": "Place motion graphic",
    "fetch_motion_graphics_video_tool": "Fetch HyperFrames video",
    "fetch_remotion_video_from_supabase_tool": "Fetch HyperFrames video",
    "generate_video_and_add_to_timeline_tool": "Generate video",
    "modify_clip_tool": "AI edit clip",
    "generate_transition_clip_tool": "Bake A + morph + B",
    "list_transitions_tool": "List transitions",
    "search_transitions_tool": "Search transitions",
    "apply_transition_tool": "Apply transition",
    "generate_tts_and_add_to_timeline_tool": "Add narration (TTS)",
    "import_stock_media_tool": "Import stock media",
    "resummarize_project_file_tool": "Resummarize file",
    "reindex_project_file_tool": "Reindex file",
    "get_clips_with_full_metadata_tool": "Read clips metadata",
    "get_timeline_placements_metadata_tool": "Read timeline placements",
    "get_timeline_state_tool": "Read timeline state",
}

assert set(TOOL_DISPLAY_LABELS) == set(AGENT_TOOL_HANDLERS), (
    "TOOL_DISPLAY_LABELS keys must match AGENT_TOOL_HANDLERS"
)

# Server-side / subagent names that never hit AGENT_TOOL_HANDLERS but still
# appear as tool_started titles over the WebSocket.
_EXTRA_TOOL_DISPLAY_LABELS = {
    "motion-graphics-agent": "Motion graphics",
    "publish_session_draft_tool": "Publish motion graphic",
    "lint_session_draft_tool": "Lint draft",
    "product_demo": "Product demo",
    "plan_product_demo_tool": "Plan product demo",
    "render_product_demo_tool": "Render product demo",
    "check_motion_graphics_health_tool": "Motion graphics health",
    "get_motion_graphics_job_status_tool": "Motion job status",
}


def humanize_tool_name(tool_name: str) -> str:
    """Return a short human-readable title for a tool name."""
    if tool_name in TOOL_DISPLAY_LABELS:
        return TOOL_DISPLAY_LABELS[tool_name]
    if tool_name in _EXTRA_TOOL_DISPLAY_LABELS:
        return _EXTRA_TOOL_DISPLAY_LABELS[tool_name]
    base = tool_name[:-5] if tool_name.endswith("_tool") else tool_name
    return base.replace("_", " ").strip().capitalize() or "Run tool"


# Tools that only READ project / timeline data and don't mutate Qt widgets.
# Safe to invoke from worker threads, which lets the agent run several of them
# concurrently without serializing through the Qt main-thread dispatcher.
READ_ONLY_TOOLS = frozenset({
    "list_files_tool",
    "list_clips_tool",
    "list_layers_tool",
    "list_markers_tool",
    "get_timeline_state_tool",
    "get_project_info_tool",
    "get_file_info_tool",
    "get_export_settings_tool",
    "list_transitions_tool",
    "search_transitions_tool",
    "get_clips_with_full_metadata_tool",
    "get_timeline_placements_metadata_tool",
    "propose_overlay_windows_tool",
})

# Tools that perform long-running network/IO work and only briefly touch Qt
# state.  They marshal those brief reads onto the main thread internally, so
# the dispatcher must NOT wrap the entire call in ``_run_on_main_thread`` —
# doing so would block the GUI for the duration of the network call (up to
# 30 minutes for TwelveLabs indexing) and serialize parallel agent calls.
BACKGROUND_SAFE_TOOLS = frozenset({
    "reindex_project_file_tool",
    # Downloads + re-encodes off the GUI thread; its timeline mutations
    # marshal to the main thread internally.
    "import_video_url_and_add_to_timeline_tool",
    "resummarize_project_file_tool",
    "import_stock_media_tool",
    # Long-running Runware/ffmpeg work; Qt timeline touches are marshalled internally.
    "generate_video_and_add_to_timeline_tool",
    "modify_clip_tool",
    "generate_transition_clip_tool",
    # Network search against project TwelveLabs index (File reads are read-only).
    "search_clips_tool",
    "search_clip_scenes_tool",
    "propose_overlay_windows_tool",
    # HyperFrames download + alpha re-encode can take a while.
    "fetch_motion_graphics_video_tool",
    "fetch_remotion_video_from_supabase_tool",
})


def execute_tool(tool_name: str, tool_args: dict) -> str:
    """Execute a tool by name with the given arguments. Returns the result string."""
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return f"Error: Unknown tool '{tool_name}'."

    # chat_session_id is used for tool state isolation (e.g. split/import → add clip chains).
    # Only pass it through to the relevant handlers.
    if isinstance(tool_args, dict) and "chat_session_id" in tool_args:
        if tool_name not in (
            "split_file_add_clip_tool",
            "add_clip_to_timeline_tool",
            "place_motion_graphic_tool",
            "import_stock_media_tool",
        ):
            tool_args = dict(tool_args)
            tool_args.pop("chat_session_id", None)

    def _invoke():
        try:
            return handler(**tool_args)
        except Exception as e:
            log.error("Tool %s execution failed: %s", tool_name, e, exc_info=True)
            return f"Error: {e}"

    try:
        if QThread is None:
            return _invoke()
        app = _get_app()
        if QThread.currentThread() is app.thread():
            return _invoke()
        if tool_name in READ_ONLY_TOOLS or tool_name in BACKGROUND_SAFE_TOOLS:
            return _invoke()
        return _run_on_main_thread(_invoke)
    except Exception as e:
        log.error("Tool %s dispatch failed: %s", tool_name, e, exc_info=True)
        return f"Error: {e}"
