"""
 @file
 @brief The one cloud layer of the media index: Gemini's words for what happens in a shot, made on demand.

 Search does not need it (every file is searchable from its local vectors). Descriptions are made when an agent
 asks for some shots (``describe_shots``), or for a whole file when the user turned on "Describe all footage on
 import" (``compute_cloud``). A low-resolution proxy of the needed range is uploaded with the measured shot list and
 the local transcript, so Gemini neither guesses cuts nor transcribes. Each shot is described once and kept on the
 shelf; a shot already described is never paid for again. Nothing here raises for a backend problem: it returns an
 ``error``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from classes.ffmpeg_cli import run_ffmpeg
from classes.media_index import schema as S
from classes.media_index.store import Shelf

POLL_SECONDS = 2.0
UNREACHABLE_SECONDS = 180.0
MAX_WAIT_SECONDS = 30 * 60
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


def audio_summary(audio: Dict[str, Any], seconds: float) -> str:
    """One factual line about an audio file from its local analysis (tempo, length, loudness, sections)."""
    bits = [f"Audio, {int(round(seconds))} s"]
    tempo = (audio or {}).get("tempo") or {}
    if tempo.get("bpm"):
        bits.append(f"{float(tempo['bpm']):.0f} BPM")
    sections = ((audio or {}).get("music") or {}).get("sections") or []
    if len(sections) > 1:
        bits.append("sections: " + ", ".join(sec["label"] for sec in sections))
    lufs = ((audio or {}).get("loudness") or {}).get("integrated_lufs")
    if lufs is not None:
        bits.append(f"{float(lufs):.0f} LUFS")
    return ", ".join(bits) + "."


def metadata_view(watch: Dict[str, Any], speech: Optional[Dict[str, Any]], structure: Dict[str, Any],
                  media_type: str = "video") -> Dict[str, Any]:
    """The ``ai_metadata`` view (chapters, scene descriptions, cues...) derived from the v2 shelf layers.

    The Scene Descriptions dock, badges and audio tools read this shape. It is a view, not a second store: the shelf
    is the source of truth.
    """
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
        "analyzed": bool(first or cues), "provider": "media-index-v2", "media_type": media_type,
        "short_summary": " ".join(first[:2])[:400], "description": " ".join(first)[:4000],
        "sounds": ", ".join(dict.fromkeys(o["label"] for s in shots for o in s.get("sound_events") or [])),
        "transcript": " ".join(r["text"] for r in cues).strip(),
        "chapters": chapters, "transcript_cues": cues, "moments": moments, "scene_descriptions": scenes,
        "tags": {"objects": objects, "scenes": sorted({s.get("shot_type") for s in shots if s.get("shot_type")}),
                 "activities": [], "mood": sorted({s.get("mood") for s in shots if s.get("mood")}), "quality": {}},
    }


# ============================ proxy and keyframes (ffmpeg) ============================
def make_proxy(path: str, probe: Dict[str, Any], out_path: str, start: Optional[float] = None,
               end: Optional[float] = None) -> Tuple[bool, str]:
    """A small copy of a video for the watch pass: 480 px high, 15 fps, mono 48k audio.

    With *start* and *end* only that stretch is copied (its time starts at 0), for long files done in chunks.
    """
    video = (probe or {}).get("video") or {}
    height = int(video.get("height") or 0)
    scale = f"scale=-2:{S.PROXY_HEIGHT}," if height > S.PROXY_HEIGHT or height <= 0 else ""
    window: List[str] = []
    if start:
        window += ["-ss", f"{max(0.0, float(start)):.3f}"]
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *window, "-i", path]
    if end is not None:
        cmd += ["-t", f"{max(0.1, float(end) - float(start or 0.0)):.3f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0?",
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


PASS_UP = ("error", "unsupported", "auth", "credits", "rate_limited", "forbidden", "retry_after")


def _watch_window(client: Any, path: str, probe: Dict[str, Any], file_id: str, shots: List[Dict[str, Any]],
                  transcript: List[Dict[str, Any]], *, start: Optional[float], end: Optional[float],
                  should_cancel: Optional[Callable[[], bool]], on_progress: Optional[Callable[[float], None]],
                  uploader: Callable[..., Any]) -> Dict[str, Any]:
    """Describe *shots* from a proxy of the whole file, or of [start, end) when given (then times are shifted to match)."""
    offset = float(start or 0.0)
    request = [{**s, "start": s["start"] - offset, "end": s["end"] - offset} for s in shots] if offset else shots
    rows = ([{**r, "start": r["start"] - offset, "end": r["end"] - offset} for r in transcript
             if r["end"] > offset and (end is None or r["start"] < end)] if offset or end is not None else transcript)
    session = client._new_http_session()
    with tempfile.TemporaryDirectory(prefix="zenvi_watch_") as tmp:
        proxy = os.path.join(tmp, "proxy.mp4")
        ok, err = make_proxy(path, probe, proxy, start, end)
        if not ok:
            return {"error": f"could not make the proxy: {err}"}
        if should_cancel and should_cancel():
            raise Cancelled()
        if on_progress:
            on_progress(0.15)
        sess = client.v2_upload_session(file_id, os.path.basename(path) or "proxy.mp4", os.path.getsize(proxy), "video/mp4", session=session)
        if sess.get("error") or not sess.get("upload_url"):
            return {k: v for k, v in sess.items() if k in PASS_UP} or {"error": "no upload url"}
        info, up_err = uploader(proxy, sess["upload_url"], mime_type="video/mp4")
        if up_err:
            return {"error": f"upload failed: {up_err}"}
        if on_progress:
            on_progress(0.4)
    started = client.v2_understand(info.get("name", ""), info.get("uri", ""), request, rows, session=session)
    if started.get("error") or not started.get("job_id"):
        return {k: v for k, v in started.items() if k in PASS_UP} or {"error": "the backend did not start the job"}
    result = _wait_for_job(client, session, started["job_id"], should_cancel,
                           on_tick=(lambda f: on_progress(0.4 + 0.6 * f)) if on_progress else None)
    if on_progress:
        on_progress(1.0)
    if offset and result.get("shots"):
        original = {int(s["id"]): s for s in shots}
        for shot in result["shots"]:
            src = original.get(int(shot.get("id", -1)))
            if src:
                shot["start"], shot["end"] = float(src["start"]), float(src["end"])
    return result


def run_watch(client: Any, path: str, probe: Dict[str, Any], file_id: str, structure: Dict[str, Any],
              speech: Optional[Dict[str, Any]], *, should_cancel: Optional[Callable[[], bool]] = None,
              on_progress: Optional[Callable[[float], None]] = None, uploader: Optional[Callable[..., Any]] = None,
              resume: Optional[Tuple[Shelf, str]] = None) -> Dict[str, Any]:
    """Describe every shot of a video. Returns ``{"shots", "usage", ...}`` or ``{"error": ..., "unsupported"?}``.

    A file longer than ``longfile.LONG_SECONDS`` is described in chunks (see ``run_watch_chunked``); *resume* is the
    shelf and fingerprint its finished chunks are kept under.
    """
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable
    from classes.media_index import longfile

    uploader = uploader or upload_file_to_gemini_resumable
    shots = watch_request_shots(structure, speech)
    if not shots:
        return {"error": "no shots to describe"}
    if longfile.is_long(float((probe or {}).get("duration") or max(float(s["end"]) for s in shots))):
        return run_watch_chunked(client, path, probe, file_id, shots, transcript_rows(speech), should_cancel=should_cancel,
                                 on_progress=on_progress, uploader=uploader, resume=resume)
    return _watch_window(client, path, probe, file_id, shots, transcript_rows(speech), start=None, end=None,
                         should_cancel=should_cancel, on_progress=on_progress, uploader=uploader)


def run_watch_chunked(client: Any, path: str, probe: Dict[str, Any], file_id: str, shots: List[Dict[str, Any]],
                      transcript: List[Dict[str, Any]], *, should_cancel: Optional[Callable[[], bool]] = None,
                      on_progress: Optional[Callable[[float], None]] = None, uploader: Callable[..., Any],
                      resume: Optional[Tuple[Shelf, str]] = None) -> Dict[str, Any]:
    """Describe a long file one chunk at a time, keeping each finished chunk so an interrupted run carries on.

    Chunks are cut between shots. Each chunk is its own proxy, upload and cloud job (and is charged on its own by the
    backend), so a failure at chunk 5 never repeats chunks 1 to 4. The result has the shape of ``run_watch``.
    """
    from classes.media_index import longfile

    chunks = longfile.plan_chunks(shots)
    plan = [[c["index"], round(c["start"], 3), round(c["end"], 3), len(c["shot_ids"])] for c in chunks]
    version = S.LAYER_VERSIONS[S.LAYER_WATCH]
    saved: Dict[str, Any] = {}
    if resume:
        shelf, sha = resume
        found = shelf.read_json(sha, "watch_partial.json") or {}
        if found.get("plan") == plan and found.get("version") == version:        # same footage, same cut: reuse what finished
            saved = found.get("done") or {}
    done: Dict[str, Any] = dict(saved)
    by_id = {int(s["id"]): s for s in shots}
    for chunk in chunks:
        key = str(chunk["index"])
        if key in done:
            continue
        if should_cancel and should_cancel():
            raise Cancelled()
        lo, hi = chunk["index"] / len(chunks), (chunk["index"] + 1) / len(chunks)
        part = _watch_window(
            client, path, probe, file_id, [by_id[i] for i in chunk["shot_ids"]], transcript, start=chunk["start"], end=chunk["end"],
            should_cancel=should_cancel, on_progress=(lambda f, lo=lo, hi=hi: on_progress(lo + (hi - lo) * f)) if on_progress else None,
            uploader=uploader)
        if part.get("error") or not part.get("shots"):
            fail = {k: v for k, v in part.items() if k in PASS_UP}
            fail["error"] = f"chunk {chunk['index'] + 1} of {len(chunks)}: " + str(fail.get("error") or "no descriptions came back")
            return fail
        returned = {int(s["id"]) for s in part["shots"]}
        for sid in chunk["shot_ids"]:          # a shot the backend left out is recorded as missing, never as described
            if sid not in returned:
                part["shots"].append({"id": sid, "start": by_id[sid]["start"], "end": by_id[sid]["end"], "missing": True})
        done[key] = {"shots": part["shots"], "usage": part.get("usage") or {}}
        if resume:
            resume[0].write_json(resume[1], "watch_partial.json", {"plan": plan, "version": version, "done": done})
    merged = [shot for chunk in chunks for shot in sorted(done[str(chunk["index"])]["shots"], key=lambda s: int(s["id"]))]
    usage = {"prompt_tokens": 0, "output_tokens": 0, "batches": 0, "model": None, "chunks": len(chunks)}
    for part in done.values():
        u = part["usage"]
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["batches"] += int(u.get("batches") or 0)
        usage["model"] = u.get("model") or usage["model"]
    if resume:
        resume[0].write_json(resume[1], "watch_partial.json", {})
    if on_progress:
        on_progress(1.0)
    return {"shots": merged, "usage": usage, "missing": sum(1 for s in merged if s.get("missing")), "errors": []}


# ============================ the layer ============================
def _described(shelf: Shelf, sha: str) -> Dict[int, Dict[str, Any]]:
    """{shot id: description} of the shots already described (a shot the cloud left out does not count)."""
    watch = shelf.read_json(sha, "watch.json") if shelf.layer_ready(sha, S.LAYER_WATCH) else None
    return {int(s["id"]): s for s in (watch or {}).get("shots") or [] if not s.get("missing")}


def _merge(shelf: Shelf, sha: str, new: List[Dict[str, Any]], usage: Dict[str, Any]) -> Dict[str, Any]:
    by_id = _described(shelf, sha)
    by_id.update({int(s["id"]): s for s in new})
    watch = {"version": S.LAYER_VERSIONS[S.LAYER_WATCH], "kind": S.INFERRED, "model": usage.get("model"),
             "shots": [by_id[k] for k in sorted(by_id)]}
    shelf.write_json(sha, "watch.json", watch)
    shelf.set_layer(sha, S.LAYER_WATCH, version=S.LAYER_VERSIONS[S.LAYER_WATCH], status=READY, model=watch["model"],
                    shots=len(watch["shots"]))
    return watch


def compute_cloud(client: Any, path: str, probe: Dict[str, Any], sha: str, shelf: Shelf, *, file_id: str = "",
                  media_type: str = "video", should_cancel: Optional[Callable[[], bool]] = None,
                  on_progress: Optional[Callable[[float], None]] = None, uploader: Optional[Callable[..., Any]] = None
                  ) -> Dict[str, Any]:
    """Describe every shot of a video not yet described. Needs the local facts on the shelf.

    Returns ``{"layers": {...}, "watch"?, "usage"?}`` or ``{"error": ..., "unsupported"?, "auth"?, "credits"?...}``.
    """
    structure = shelf.read_json(sha, "structure.json") or {}
    if media_type != "video" or not structure.get("shots"):
        return {"layers": {S.LAYER_WATCH: "skipped"}}
    have = _described(shelf, sha)
    todo = [s for s in structure["shots"] if int(s["id"]) not in have]
    if not todo:
        return {"layers": {S.LAYER_WATCH: "cached"}, "watch": shelf.read_json(sha, "watch.json")}
    speech = shelf.read_json(sha, "speech.json") if shelf.layer_ready(sha, S.LAYER_SPEECH, version=S.LAYER_VERSIONS[S.LAYER_SPEECH]) else None
    result = run_watch(client, path, probe, file_id or sha[:12], {"shots": todo}, speech, should_cancel=should_cancel,
                       on_progress=on_progress, uploader=uploader, resume=(shelf, sha))
    if result.get("error") or not result.get("shots"):
        fail = {k: result[k] for k in PASS_UP if k in result}
        fail.setdefault("error", "no shot descriptions came back")
        return fail
    usage = result.get("usage") or {}
    return {"layers": {S.LAYER_WATCH: READY}, "usage": usage, "watch": _merge(shelf, sha, result["shots"], usage)}


def describe_shots(client: Any, path: str, probe: Dict[str, Any], sha: str, shelf: Shelf, shot_ids: List[int], embedder: Any,
                   *, file_id: str = "", should_cancel: Optional[Callable[[], bool]] = None,
                   uploader: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Describe the asked-for shots that have no description yet, keep them, and add them to the file's search vectors.

    Returns ``{"described": [newly described ids], "shots": [descriptions of every asked-for shot], "usage"}``, or an
    error dict as ``compute_cloud`` does.
    """
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable
    from classes.media_index import vectors

    structure = shelf.read_json(sha, "structure.json") or {}
    wanted = {int(i) for i in shot_ids}
    have = _described(shelf, sha)
    todo = [s for s in structure.get("shots") or [] if int(s["id"]) in wanted and int(s["id"]) not in have]
    usage: Dict[str, Any] = {}
    if todo:
        speech = shelf.read_json(sha, "speech.json") if shelf.layer_ready(sha, S.LAYER_SPEECH) else None
        per_shot = (speech or {}).get("per_shot") or {}
        request = [{"id": int(s["id"]), "start": float(s["start"]), "end": float(s["end"]),
                    "fps": plan_shot_fps(s, per_shot.get(str(s["id"])))} for s in todo]
        # ponytail: one proxy from the first to the last asked-for shot; split into runs if far-apart asks get slow
        start, end = min(r["start"] for r in request), max(r["end"] for r in request)
        result = _watch_window(client, path, probe, file_id or sha[:12], request, transcript_rows(speech),
                               start=start if start > 0 else None, end=end, should_cancel=should_cancel, on_progress=None,
                               uploader=uploader or upload_file_to_gemini_resumable)
        if result.get("error") or not result.get("shots"):
            fail = {k: result[k] for k in PASS_UP if k in result}
            fail.setdefault("error", "no shot descriptions came back")
            return fail
        usage = result.get("usage") or {}
        _merge(shelf, sha, result["shots"], usage)
    described = _described(shelf, sha)
    shots = [described[i] for i in sorted(wanted) if i in described]
    vectors.add_descriptions(shelf, sha, embedder, shots)
    return {"described": sorted(int(s["id"]) for s in todo if int(s["id"]) in described), "shots": shots, "usage": usage}
