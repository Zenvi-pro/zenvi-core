"""
 @file
 @brief The cloud layers of the media index: what happens in each shot, and search vectors.

 Runs after the local facts. One low-resolution proxy of the file is uploaded; Gemini describes
 every shot once (the shot list, per-shot frame rates and the local transcript are given to it,
 so it neither guesses cuts nor transcribes). Descriptions and transcript sentences become text
 vectors, and a picture every couple of seconds becomes picture vectors, all kept on the shelf.

 Video is watched; audio-only files and stills get picture/text vectors only for now (the watch
 pass is video-only). Nothing here raises for a backend problem: it returns an ``error`` so the
 caller can fall back to the original indexing.
"""

from __future__ import annotations

import base64
import math
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from classes.ffmpeg_cli import run_ffmpeg
from classes.media_index import schema as S
from classes.media_index.store import Shelf

POLL_SECONDS = 2.0
UNREACHABLE_SECONDS = 180.0
MAX_WAIT_SECONDS = 30 * 60
EMBED_BATCH = 48
READY = "ready"


class Cancelled(Exception):
    """The caller asked to stop."""


# ============================ planning (pure) ============================
def plan_shot_fps(shot: Dict[str, Any], speech: Optional[Dict[str, Any]] = None) -> float:
    """How often Gemini should look inside a shot: rarely when nothing moves, often when much does."""
    motion = shot.get("motion") or {}
    klass = motion.get("class")
    level = float(motion.get("level") or 0.0)
    talky = float((speech or {}).get("speech_ratio") or 0.0) >= 0.5
    if klass == "static" and level < 0.2:
        return 0.5
    if klass == "static" and talky:
        return 0.5
    if klass == "busy" or level >= 0.6:
        return 2.0
    return 1.0


def watch_request_shots(structure: Dict[str, Any], speech: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    per_shot = (speech or {}).get("per_shot") or {}
    return [{"id": int(s["id"]), "start": float(s["start"]), "end": float(s["end"]),
             "fps": plan_shot_fps(s, per_shot.get(str(s["id"])))}
            for s in structure.get("shots") or [] if float(s["end"]) > float(s["start"])]


def transcript_rows(speech: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text") or ""),
             "speaker": s.get("speaker")} for s in (speech or {}).get("sentences") or []]


def shot_document(watch: Dict[str, Any]) -> str:
    """The text that stands for a shot in search: what happens, what is seen, read and heard."""
    parts = [str(watch.get("description") or "").strip()]
    if watch.get("actions"):
        parts.append("Actions: " + "; ".join(watch["actions"]) + ".")
    labels = [o["label"] for o in watch.get("objects") or [] if o.get("label")]
    if labels:
        parts.append("Seen: " + ", ".join(dict.fromkeys(labels)) + ".")
    texts = [o["text"] for o in watch.get("on_screen_text") or [] if o.get("text")]
    if texts:
        parts.append("Text on screen: " + "; ".join(dict.fromkeys(texts)) + ".")
    sounds = [o["label"] for o in watch.get("sound_events") or [] if o.get("label")]
    if sounds:
        parts.append("Sounds: " + ", ".join(dict.fromkeys(sounds)) + ".")
    tail = ", ".join(x for x in (watch.get("mood"), watch.get("shot_type")) if x)
    if tail:
        parts.append(tail + ".")
    return " ".join(p for p in parts if p)


def keyframe_plan(shots: List[Dict[str, Any]], every: float = S.KEYFRAME_EVERY_SECONDS,
                  cap: int = S.MAX_KEYFRAMES) -> List[Dict[str, Any]]:
    """Picture times: at least one per shot and one per ``every`` seconds inside long shots."""
    rows: List[Dict[str, Any]] = []
    for s in shots:
        start, end = float(s["start"]), float(s["end"])
        n = max(1, int(math.ceil((end - start) / every)))
        for i in range(n):
            rows.append({"shot": int(s["id"]), "t": round(start + (i + 0.5) * (end - start) / n, 3)})
    if len(rows) > cap:  # thin evenly, never dropping a shot's only picture
        step = len(rows) / cap
        rows = [rows[int(i * step)] for i in range(cap)]
    return rows


def v1_metadata_from_v2(watch: Dict[str, Any], speech: Optional[Dict[str, Any]], structure: Dict[str, Any],
                        media_type: str = "video") -> Dict[str, Any]:
    """The v1 ``ai_metadata`` shape (chapters, scene descriptions, cues...) from v2 data, so the
    Scene Descriptions dock, badges and audio tools keep working while v2 is on."""
    shots = watch.get("shots") or []
    chapters, scenes, moments = [], [], []
    objects: List[str] = []
    seen = set()
    for s in shots:
        desc = s.get("description") or ""
        title = (desc.split(".")[0] or desc)[:70]
        sounds = ", ".join(o["label"] for o in s.get("sound_events") or [])
        labels = [o["label"] for o in s.get("objects") or []]
        chapters.append({"start": s["start"], "end": s["end"], "title": title, "summary": desc, "visual": desc,
                         "sounds": sounds, "shot_type": s.get("shot_type") or "", "objects": labels})
        if desc:
            scenes.append({"description": f"{title} — {desc}" if title and title != desc else desc,
                           "source_time": s["start"], "time": s["start"]})
        for action in s.get("actions") or []:
            moments.append({"start": s["start"], "end": s["end"], "label": action, "detail": desc})
        for lab in labels:
            if lab.lower() not in seen:
                seen.add(lab.lower())
                objects.append(lab)
    cues = [{"start": r["start"], "end": r["end"], "text": r["text"], "text_en": r["text"]}
            for r in transcript_rows(speech)]
    first = [s.get("description") for s in shots if s.get("description")]
    return {
        "analyzed": bool(first or cues), "provider": "gemini-v2", "media_type": media_type,
        "short_summary": " ".join(first[:2])[:400], "description": " ".join(first)[:4000],
        "sounds": ", ".join(dict.fromkeys(o["label"] for s in shots for o in s.get("sound_events") or [])),
        "transcript": " ".join(r["text"] for r in cues).strip(),
        "chapters": chapters, "transcript_cues": cues, "moments": moments, "scene_descriptions": scenes,
        "tags": {"objects": objects, "scenes": sorted({s.get("shot_type") for s in shots if s.get("shot_type")}),
                 "activities": [], "mood": sorted({s.get("mood") for s in shots if s.get("mood")}), "quality": {}},
    }


# ============================ proxy and keyframes (ffmpeg) ============================
def make_proxy(path: str, probe: Dict[str, Any], out_path: str) -> Tuple[bool, str]:
    """A small copy of a video for the watch pass: 480 px high, 15 fps, mono 48k audio."""
    video = (probe or {}).get("video") or {}
    height = int(video.get("height") or 0)
    scale = f"scale=-2:{S.PROXY_HEIGHT}," if height > S.PROXY_HEIGHT or height <= 0 else ""
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
           "-map_metadata", "-1", "-map_chapters", "-1",     # never upload the clip's GPS, device or date tags
           "-vf", f"{scale}fps=15,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", out_path]
    try:
        proc = run_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=3 * 3600)
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        return False, (proc.stderr or b"proxy encode failed").decode("utf-8", "replace")[-300:]
    return True, ""


def extract_keyframe(path: str, t: float, out: str, long_edge: int = S.KEYFRAME_LONG_EDGE) -> bool:
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
           "-map_metadata", "-1", "-frames:v", "1", "-vf", f"scale='if(gt(iw,ih),{long_edge},-2)':'if(gt(iw,ih),-2,{long_edge})':flags=area",
           "-q:v", "4", out]
    try:
        proc = run_ffmpeg(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=120)
    except Exception:
        return False
    return proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0


# ============================ talking to the backend ============================
def _wait_for_job(client: Any, session: Any, job_id: str, should_cancel: Optional[Callable[[], bool]],
                  on_tick: Optional[Callable[[float], None]] = None, what: str = "describing shots") -> Dict[str, Any]:
    deadline = time.time() + MAX_WAIT_SECONDS
    unreachable_since: Optional[float] = None
    started = time.time()
    while time.time() < deadline:
        if should_cancel and should_cancel():
            raise Cancelled()
        reply = client.v2_job(job_id, session=session)
        if reply.get("error") and not reply.get("status"):
            unreachable_since = unreachable_since or time.time()
            if time.time() - unreachable_since >= UNREACHABLE_SECONDS:
                return {"error": f"backend unreachable while {what}: {reply['error']}"}
        else:
            unreachable_since = None
            status = reply.get("status")
            if status == "done":
                return reply.get("result") or {"error": "the backend returned an empty result"}
            if status == "failed":
                return {"error": str((reply.get("result") or {}).get("error") or f"{what} failed")}
            if status == "not_found":
                return {"error": "the backend lost the job"}
        if on_tick:
            on_tick(min(0.95, (time.time() - started) / 120.0))
        time.sleep(POLL_SECONDS)
    return {"error": f"{what} timed out"}


def run_watch(client: Any, path: str, probe: Dict[str, Any], file_id: str, structure: Dict[str, Any],
              speech: Optional[Dict[str, Any]], *, should_cancel: Optional[Callable[[], bool]] = None,
              on_progress: Optional[Callable[[float], None]] = None, uploader: Optional[Callable[..., Any]] = None
              ) -> Dict[str, Any]:
    """Describe every shot of a video. Returns ``{"shots", "usage", ...}`` or ``{"error": ..., "unsupported"?}``."""
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable

    uploader = uploader or upload_file_to_gemini_resumable
    shots = watch_request_shots(structure, speech)
    if not shots:
        return {"error": "no shots to describe"}
    session = client._new_http_session()
    with tempfile.TemporaryDirectory(prefix="zenvi_watch_") as tmp:
        proxy = os.path.join(tmp, "proxy.mp4")
        ok, err = make_proxy(path, probe, proxy)
        if not ok:
            return {"error": f"could not make the proxy: {err}"}
        if should_cancel and should_cancel():
            raise Cancelled()
        if on_progress:
            on_progress(0.15)
        sess = client.v2_upload_session(file_id, os.path.basename(path) or "proxy.mp4", os.path.getsize(proxy), "video/mp4", session=session)
        if sess.get("error") or not sess.get("upload_url"):
            return {k: v for k, v in sess.items() if k in ("error", "unsupported", "auth")} or {"error": "no upload url"}
        info, up_err = uploader(proxy, sess["upload_url"], mime_type="video/mp4")
        if up_err:
            return {"error": f"upload failed: {up_err}"}
        if on_progress:
            on_progress(0.4)
    started = client.v2_understand(info.get("name", ""), info.get("uri", ""), shots, transcript_rows(speech), session=session)
    if started.get("error") or not started.get("job_id"):
        return {k: v for k, v in started.items() if k in ("error", "unsupported", "auth")} or {"error": "the backend did not start the job"}
    result = _wait_for_job(client, session, started["job_id"], should_cancel,
                           on_tick=(lambda f: on_progress(0.4 + 0.6 * f)) if on_progress else None)
    if on_progress:
        on_progress(1.0)
    return result


def embed_in_batches(client: Any, items: List[Dict[str, Any]], *, dims: int = S.EMBED_DIMS, task_type: Optional[str] = None,
                     should_cancel: Optional[Callable[[], bool]] = None,
                     on_progress: Optional[Callable[[float], None]] = None) -> Tuple[List[Optional[np.ndarray]], List[str]]:
    """Vectors for *items* (None where one failed) and any error strings."""
    session = client._new_http_session()
    out: List[Optional[np.ndarray]] = [None] * len(items)
    errors: List[str] = []
    for lo in range(0, len(items), EMBED_BATCH):
        if should_cancel and should_cancel():
            raise Cancelled()
        chunk = items[lo:lo + EMBED_BATCH]
        reply = client.v2_embed(chunk, dims=dims, task_type=task_type, session=session)
        if reply.get("error") and not reply.get("vectors"):
            errors.append(str(reply["error"]))
            if reply.get("unsupported") or reply.get("auth"):
                break
            continue
        for i, b64 in enumerate(reply.get("vectors") or []):
            if b64:
                out[lo + i] = np.frombuffer(base64.b64decode(b64), dtype="<f2").astype(np.float32)
        errors.extend(f"item {lo + int(k)}: {v}" for k, v in (reply.get("errors") or {}).items())
        if on_progress:
            on_progress(min(1.0, (lo + len(chunk)) / max(1, len(items))))
    return out, errors


# ============================ the layers ============================
def _pack(rows: List[Dict[str, Any]], vectors: List[Optional[np.ndarray]]) -> Tuple[List[Dict[str, Any]], bytes]:
    kept = [(r, v) for r, v in zip(rows, vectors) if v is not None]
    if not kept:
        return [], b""
    return [r for r, _ in kept], np.stack([v for _, v in kept]).astype("<f2").tobytes()


def compute_cloud(client: Any, path: str, probe: Dict[str, Any], sha: str, shelf: Shelf, *, file_id: str = "",
                  media_type: str = "video", should_cancel: Optional[Callable[[], bool]] = None,
                  on_progress: Optional[Callable[[float], None]] = None, uploader: Optional[Callable[..., Any]] = None
                  ) -> Dict[str, Any]:
    """Run the watch and vector layers that are missing. Needs the local facts on the shelf.

    Returns ``{"layers": {...}, "error"?, "unsupported"?, "auth"?, "usage"?}``; never raises for backend problems.
    """
    structure = shelf.read_json(sha, "structure.json") or {}
    speech = shelf.read_json(sha, "speech.json") if shelf.layer_ready(sha, S.LAYER_SPEECH, version=S.LAYER_VERSIONS[S.LAYER_SPEECH]) else None
    layers: Dict[str, str] = {}
    usage: Dict[str, Any] = {}

    def report(lo: float, hi: float) -> Callable[[float], None]:
        return lambda f: on_progress(round(lo + (hi - lo) * max(0.0, min(1.0, f)), 4)) if on_progress else None

    def current(layer: str) -> bool:
        # Cloud layers cost money: one saved by an older version is kept, never re-run just for being old.
        return shelf.layer_ready(sha, layer)

    # ---- watch -------------------------------------------------------------------------
    watch: Optional[Dict[str, Any]] = None
    if media_type != "video" or not structure.get("shots"):
        layers[S.LAYER_WATCH] = "skipped"
    elif current(S.LAYER_WATCH):
        layers[S.LAYER_WATCH] = "cached"
        watch = shelf.read_json(sha, "watch.json")
    else:
        result = run_watch(client, path, probe, file_id or sha[:12], structure, speech, should_cancel=should_cancel,
                           on_progress=report(0.0, 0.6), uploader=uploader)
        if result.get("error") or not result.get("shots"):
            fail = {k: result[k] for k in ("error", "unsupported", "auth") if k in result}
            fail["layers"] = layers
            fail.setdefault("error", "no shot descriptions came back")
            return fail
        watch = {"version": S.LAYER_VERSIONS[S.LAYER_WATCH], "kind": S.INFERRED, "model": (result.get("usage") or {}).get("model"),
                 "usage": result.get("usage"), "missing": result.get("missing", 0), "shots": result["shots"]}
        shelf.write_json(sha, "watch.json", watch)
        shelf.set_layer(sha, S.LAYER_WATCH, version=S.LAYER_VERSIONS[S.LAYER_WATCH], status=READY,
                        model=watch["model"], shots=len(watch["shots"]), missing=watch["missing"])
        layers[S.LAYER_WATCH] = READY
        usage = watch.get("usage") or {}
    if should_cancel and should_cancel():
        raise Cancelled()

    # ---- vectors -----------------------------------------------------------------------
    if current(S.LAYER_VECTORS):
        layers[S.LAYER_VECTORS] = "cached"
        return {"layers": layers, "usage": usage, "watch": watch}
    text_rows: List[Dict[str, Any]] = []
    text_items: List[Dict[str, Any]] = []
    for s in (watch or {}).get("shots") or []:
        doc = shot_document(s)
        if doc and not s.get("missing"):
            text_rows.append({"kind": "shot", "shot": s["id"], "start": s["start"], "end": s["end"], "text": doc})
    for r in transcript_rows(speech):
        if r["text"].strip():
            text_rows.append({"kind": "speech", "shot": None, "start": r["start"], "end": r["end"], "text": r["text"]})
    text_items = [{"kind": "text", "text": r["text"]} for r in text_rows]

    image_rows: List[Dict[str, Any]] = []
    image_items: List[Dict[str, Any]] = []
    if probe.get("has_video") and structure.get("shots"):
        with tempfile.TemporaryDirectory(prefix="zenvi_kf_") as tmp:
            plan = keyframe_plan(structure["shots"])
            files = [os.path.join(tmp, f"{i}.jpg") for i in range(len(plan))]
            with ThreadPoolExecutor(max_workers=4) as pool:
                ok = list(pool.map(lambda iv: extract_keyframe(path, iv[1]["t"], files[iv[0]]), enumerate(plan)))
            for row, f, good in zip(plan, files, ok):
                if good:
                    with open(f, "rb") as fh:
                        image_items.append({"kind": "image", "mime": "image/jpeg", "data": base64.b64encode(fh.read()).decode("ascii")})
                    image_rows.append(row)
    if should_cancel and should_cancel():
        raise Cancelled()

    text_vecs, text_errors = embed_in_batches(client, text_items, task_type="RETRIEVAL_DOCUMENT", should_cancel=should_cancel,
                                              on_progress=report(0.6, 0.8))
    image_vecs, image_errors = embed_in_batches(client, image_items, task_type=None, should_cancel=should_cancel,
                                                on_progress=report(0.8, 1.0))
    t_rows, t_blob = _pack(text_rows, text_vecs)
    i_rows, i_blob = _pack(image_rows, image_vecs)
    errors = text_errors + image_errors
    if not t_rows and not i_rows:
        return {"error": errors[0] if errors else "no vectors were produced", "layers": layers, "usage": usage,
                **({"unsupported": True} if any("no media index v2" in e for e in errors) else {})}
    shelf.write_bytes(sha, "vectors_text.f16", t_blob)
    shelf.write_bytes(sha, "vectors_image.f16", i_blob)
    shelf.write_json(sha, "vectors_index.json", {"version": S.LAYER_VERSIONS[S.LAYER_VECTORS], "dims": S.EMBED_DIMS,
                                                  "text": t_rows, "image": i_rows, "errors": errors[:20]})
    shelf.set_layer(sha, S.LAYER_VECTORS, version=S.LAYER_VERSIONS[S.LAYER_VECTORS], status=READY,
                    text=len(t_rows), image=len(i_rows), failed=len(text_rows) + len(image_rows) - len(t_rows) - len(i_rows))
    layers[S.LAYER_VECTORS] = READY
    return {"layers": layers, "usage": usage, "watch": watch, "errors": errors}
