"""
 @file
 @brief The search vectors of a file: keyframes, spoken sentences, sound and descriptions in one space.

 Everything a file can be found by becomes a 768-number unit vector from one model (``embedder``), kept on the
 shelf as one layer, ``vectors_local``:

     vectors_local.f16   N x 768 float16, one row per vector
     vectors_local.json  one row per vector: its kind and the time it stands for, plus the model that made them

 Row kinds: ``image`` (a keyframe: shot, t), ``speech`` (a transcript sentence: start, end, text), ``audio`` (10 s of
 sound: start, end) and ``shot`` (a description of a shot, added when one is asked for).
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import numpy as np

from classes.ffmpeg_cli import popen_ffmpeg, run_ffmpeg
from classes.media_index import embedder as E, schema as S
from classes.media_index.store import Shelf

MATRIX = "vectors_local.f16"
INDEX = "vectors_local.json"
AUDIO_WINDOW = 10.0
AUDIO_MIN = 2.0                  # a shorter tail is not worth a vector
SILENCE_PEAK = 1e-3              # digital silence: nothing to find in it


class Cancelled(Exception):
    """The caller asked to stop."""


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


# ============================ reading and writing the layer ============================
def write_layer(shelf: Shelf, sha: str, rows: Sequence[Dict[str, Any]], vecs: Sequence[Optional[np.ndarray]], *, model: str,
                device: str = "") -> int:
    """Replace the layer with *rows* (a row whose vector is None is left out). Returns how many were kept."""
    kept = [(dict(r), v) for r, v in zip(rows, vecs) if v is not None]
    dims = len(kept[0][1]) if kept else S.EMBED_DIMS
    blob = np.stack([v for _, v in kept]).astype("<f2").tobytes() if kept else b""
    shelf.write_bytes(sha, MATRIX, blob)
    shelf.write_json(sha, INDEX, {"version": S.LAYER_VERSIONS[S.LAYER_VECTORS], "dims": dims, "model": model, "device": device,
                                  "rows": [r for r, _ in kept]})
    shelf.set_layer(sha, S.LAYER_VECTORS, version=S.LAYER_VERSIONS[S.LAYER_VECTORS], status="ready", model=model, device=device,
                    rows=len(kept), dims=dims)
    return len(kept)


def read_layer(shelf: Shelf, sha: str):
    """(rows, matrix, model), or ([], None, "") when the layer is missing or does not match its index."""
    index = shelf.read_json(sha, INDEX) or {}
    rows, dims = index.get("rows") or [], int(index.get("dims") or S.EMBED_DIMS)
    blob = shelf.read_bytes(sha, MATRIX) if rows else None
    if not rows or not blob or len(blob) != len(rows) * dims * 2:
        return [], None, str(index.get("model") or "")
    return rows, np.frombuffer(blob, dtype="<f2").reshape(len(rows), dims).astype(np.float32), str(index.get("model") or "")


def is_current(shelf: Shelf, sha: str, model: str) -> bool:
    """The file's vectors exist and were made by *model* (another model's vectors cannot be searched with its queries)."""
    row = shelf.layer(sha, S.LAYER_VECTORS) or {}
    if row.get("model") != model or int(row.get("version") or 0) != S.LAYER_VERSIONS[S.LAYER_VECTORS]:
        return False
    if row.get("status") == S.NOT_APPLICABLE:
        return True
    try:          # a write cut short leaves a matrix that does not match its rows: that file is embedded again
        size = os.path.getsize(os.path.join(shelf.entry_dir(sha), MATRIX))
    except OSError:
        return False
    return row.get("status") == "ready" and size == int(row.get("rows") or 0) * int(row.get("dims") or S.EMBED_DIMS) * 2


# ============================ cutting the inputs (ffmpeg) ============================
def extract_keyframes(path: str, plan: List[Dict[str, Any]], size, tmp: str) -> List[Optional[str]]:
    """One JPEG per planned time at exactly *size* (the model reads it as is); None where a frame could not be read."""
    w, h = size

    def one(i_row):
        i, row = i_row
        out = os.path.join(tmp, f"{i}.jpg")
        cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, row['t']):.3f}", "-i", path,
               "-map_metadata", "-1", "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=bicubic", "-q:v", "3", out]
        try:
            proc = run_ffmpeg(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=120)
        except Exception:
            return None
        return out if proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0 else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, enumerate(plan)))


def audio_windows(path: str) -> Iterator[E.Window]:
    """The file's sound as 10 s windows of 16 kHz mono, without digital silence and short tails, read as it decodes (a long
    file is never held in memory). Raises ``embedder.EmbedError`` when the sound cannot be read to the end."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a:0", "-ac", "1",
           "-ar", str(E.AUDIO_RATE), "-f", "f32le", "-"]
    step = int(AUDIO_WINDOW * E.AUDIO_RATE)
    proc = popen_ffmpeg(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        at = 0
        while True:
            chunk = proc.stdout.read(step * 4)
            if not chunk:
                break
            w = np.frombuffer(chunk[: len(chunk) // 4 * 4], dtype="<f4")
            if len(w) >= AUDIO_MIN * E.AUDIO_RATE and float(np.max(np.abs(w))) > SILENCE_PEAK:
                yield at / E.AUDIO_RATE, (at + len(w)) / E.AUDIO_RATE, w
            at += len(w)
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise E.EmbedError("the sound could not be read to the end")


# ============================ making the layer ============================
def _shot_at(shots: List[Dict[str, Any]], t: float) -> Optional[int]:
    for s in shots:
        if float(s["start"]) <= t < float(s["end"]):
            return int(s["id"])
    return int(shots[-1]["id"]) if shots and t >= float(shots[-1]["end"]) - 1e-6 else None


def _description_rows(shelf: Shelf, sha: str, shots: Sequence[Dict[str, Any]], have: set) -> List[Dict[str, Any]]:
    from classes.media_index.cloud import shot_document
    rows = []
    for s in shots:
        doc = shot_document(s)
        if doc and not s.get("missing") and int(s["id"]) not in have:
            rows.append({"kind": "shot", "shot": int(s["id"]), "start": float(s["start"]), "end": float(s["end"]), "text": doc})
    return rows


def compute_vectors(path: str, probe: Dict[str, Any], sha: str, shelf: Shelf, embedder: Any, *,
                    should_cancel: Optional[Callable[[], bool]] = None,
                    on_progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
    """Embed a file's keyframes, sentences, sound and any saved descriptions with *embedder*. Needs the local facts.

    Raises ``embedder.EmbedError`` when the model fails and ``Cancelled`` when asked to stop.
    """
    def check():
        if should_cancel and should_cancel():
            raise Cancelled()

    def progress(f):
        if on_progress:
            on_progress(round(min(1.0, f), 3))

    structure = shelf.read_json(sha, "structure.json") or {}
    shots = structure.get("shots") or []
    speech = shelf.read_json(sha, "speech.json") if shelf.layer_ready(sha, S.LAYER_SPEECH) else None
    rows: List[Dict[str, Any]] = []
    vecs: List[Optional[np.ndarray]] = []

    if hasattr(embedder, "start"):
        embedder.start()                  # its device decides how many pictures to plan
    if probe.get("has_video") and shots:
        video = probe.get("video") or {}
        # ponytail: "low power" is simply the CPU path (one picture per shot); add RAM/battery checks if CPU users want more
        every = math.inf if getattr(embedder, "device", "") == "cpu" else S.KEYFRAME_EVERY_SECONDS
        plan = keyframe_plan(shots, every=every)
        with tempfile.TemporaryDirectory(prefix="zenvi_kf_") as tmp:
            files = extract_keyframes(path, plan, E.keyframe_size(video.get("width") or 0, video.get("height") or 0), tmp)
            good = [(row, f) for row, f in zip(plan, files) if f]
            for lo in range(0, len(good), 30):
                check()
                part = good[lo:lo + 30]
                vecs += embedder.images([f for _, f in part])
                rows += [{"kind": "image", "shot": row["shot"], "t": row["t"]} for row, _ in part]
                progress(0.7 * (lo + len(part)) / max(1, len(good)))
    check()
    sentences = [s for s in (speech or {}).get("sentences") or [] if str(s.get("text") or "").strip()]
    if sentences:
        vecs += embedder.texts([str(s["text"]) for s in sentences])
        rows += [{"kind": "speech", "shot": _shot_at(shots, (float(s["start"]) + float(s["end"])) / 2), "start": float(s["start"]),
                  "end": float(s["end"]), "text": str(s["text"])} for s in sentences]
    progress(0.8)
    check()
    batch: List[E.Window] = []
    for window in (audio_windows(path) if probe.get("has_audio") else ()):
        batch.append(window)
        if len(batch) == E.AUDIO_BATCH * 2:
            check()
            vecs += embedder.audio(batch)
            rows += [{"kind": "audio", "start": round(a, 3), "end": round(b, 3)} for a, b, _ in batch]
            batch = []
    if batch:
        vecs += embedder.audio(batch)
        rows += [{"kind": "audio", "start": round(a, 3), "end": round(b, 3)} for a, b, _ in batch]
    check()
    watch = shelf.read_json(sha, "watch.json") if shelf.layer_ready(sha, S.LAYER_WATCH) else None
    described = _description_rows(shelf, sha, (watch or {}).get("shots") or [], set())
    if described:
        vecs += embedder.texts([r["text"] for r in described])
        rows += described
    progress(1.0)
    if not rows:
        # A still with no shots, or a silent file with nothing to see: finished, with nothing to search by meaning.
        shelf.set_layer(sha, S.LAYER_VECTORS, version=S.LAYER_VERSIONS[S.LAYER_VECTORS], status=S.NOT_APPLICABLE,
                        model=embedder.model, note="nothing to embed")
        return {"status": S.NOT_APPLICABLE, "rows": 0}
    kept = write_layer(shelf, sha, rows, vecs, model=embedder.model, device=getattr(embedder, "device", ""))
    return {"status": "ready", "rows": kept}


def add_descriptions(shelf: Shelf, sha: str, embedder: Any, shots: Sequence[Dict[str, Any]]) -> int:
    """Embed newly described shots into the file's layer. Does nothing when the layer was made by another model
    (the next index with that model picks the descriptions up). Returns how many were added."""
    old_rows, matrix, model = read_layer(shelf, sha)
    if model != embedder.model:
        return 0
    have = {int(r["shot"]) for r in old_rows if r.get("kind") == "shot"}
    new = _description_rows(shelf, sha, shots, have)
    if not new:
        return 0
    vecs = embedder.texts([r["text"] for r in new])
    old_vecs = list(matrix) if matrix is not None else []
    row = shelf.layer(sha, S.LAYER_VECTORS) or {}
    write_layer(shelf, sha, list(old_rows) + new, old_vecs + list(vecs), model=model, device=str(row.get("device") or ""))
    return len(new)
