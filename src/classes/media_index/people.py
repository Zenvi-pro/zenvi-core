"""
 @file
 @brief People identity: who appears where in a file, and the user's own names for them. Local only, opt-in, deletable.

 What this holds is biometric: face and voice vectors. So it is kept apart from the media index shelf (a separate folder, so
 it is never part of a shelf export or a project index folder), written owner-only, never sent anywhere, never logged, and
 removed completely by ``delete_all``. A person has a name only when the user gave one; every match carries a confidence and
 "unsure" is an answer. The numbers are calibrated on LFW (see ``tests/eval/results/people_calibration.json``).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from classes.logger import log
from classes.media_index import faces

VERSION = 1
MATCH = 0.45                # at or above: the same person (LFW: 98.1% of same-person pairs, 0.009% of different-person pairs)
UNSURE_LOW = 0.35           # from here to MATCH: a possible match, said so and never merged
SAMPLE_RATE = 2.0           # frames a second looked at
MAX_PER_SHOT = 8
MAX_SAMPLES = 1500          # per file
FRAME_LONG_EDGE = 960
MAX_EXEMPLARS = 12
EXEMPLAR_MIN_DISTANCE = 0.1  # a new exemplar must differ from the existing ones by at least this (cosine distance)
VOICE_EXEMPLAR_MIN_DISTANCE = 0.05
MIN_LINK_SECONDS = 10.0     # a voice and a face are linked only after this long speaking with that one face on screen
LINK_RATIO = 0.7            # ... and only when that face is alone on screen for at least this share of all the time the voice speaks (so a narrator
                            # speaking over footage of someone is not taken for them)
_lock = threading.RLock()


# ============================ where it lives ============================
def people_root() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(os.path.expanduser("~"), ".openshot_qt")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "media_index_people")


def _private_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_json(path: str, data: Any) -> None:
    _private_dir(os.path.dirname(path))
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def enc(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype="<f2").tobytes()).decode("ascii")


def dec(text: str) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(text), dtype="<f2").astype(np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _scan_path(sha: str) -> str:
    return os.path.join(people_root(), "files", f"{sha}.json")


def _registry_path() -> str:
    return os.path.join(people_root(), "registry.json")


# ============================ the registry ============================
def load_registry() -> Dict[str, Any]:
    with _lock:
        reg = _read_json(_registry_path()) or {}
        reg.setdefault("version", VERSION)
        reg.setdefault("people", [])
        reg.setdefault("pins", {})
        reg.setdefault("voice_pins", {})
        reg.setdefault("next", 1)
        return reg


def save_registry(reg: Dict[str, Any]) -> None:
    with _lock:
        _write_json(_registry_path(), reg)


def _person(reg: Dict[str, Any], pid: str) -> Optional[Dict[str, Any]]:
    return next((p for p in reg["people"] if p["id"] == pid), None)


def _add_exemplar(person: Dict[str, Any], vec: np.ndarray) -> None:
    ex = [dec(e) for e in person["exemplars"]]
    if ex and max(float(np.dot(vec, e)) for e in ex) > 1.0 - EXEMPLAR_MIN_DISTANCE:
        return
    person["exemplars"].append(enc(vec))
    if len(person["exemplars"]) > MAX_EXEMPLARS:
        person["exemplars"].pop(0)


def new_person(reg: Dict[str, Any], vec: Optional[np.ndarray] = None, name: Optional[str] = None, voice: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """A new person with a face (``vec``), a voice (``voice``) or both; at least one is needed."""
    p = {"id": f"P{reg['next']}", "name": name or None, "exemplars": [enc(vec)] if vec is not None else [], "voices": [enc(voice)] if voice is not None else []}
    reg["next"] += 1
    reg["people"].append(p)
    return p


def _add_voice(person: Dict[str, Any], vec: np.ndarray) -> None:
    person.setdefault("voices", [])
    ex = [dec(e) for e in person["voices"]]
    if ex and max(float(np.dot(vec, e)) for e in ex) > 1.0 - VOICE_EXEMPLAR_MIN_DISTANCE:
        return
    person["voices"].append(enc(vec))
    if len(person["voices"]) > MAX_EXEMPLARS:
        person["voices"].pop(0)


def match_voice(reg: Dict[str, Any], vec: np.ndarray) -> Dict[str, Any]:
    """Whose voice this is: like ``match``, with the voice lines (``voiceprint.SAME_VOICE`` and ``UNSURE_VOICE``)."""
    from classes.media_index import voiceprint as vp
    best, best_sim = None, -1.0
    for p in reg["people"]:
        sim = max((float(np.dot(vec, dec(e))) for e in p.get("voices") or []), default=-1.0)
        if sim > best_sim:
            best, best_sim = p, sim
    if best is not None and best_sim >= vp.SAME_VOICE:
        return {"person": best["id"], "confidence": round(best_sim, 3), "status": "match", "candidate": None}
    if best is not None and best_sim >= vp.UNSURE_VOICE:
        return {"person": None, "confidence": round(best_sim, 3), "status": "unsure", "candidate": best["id"]}
    return {"person": None, "confidence": round(max(best_sim, 0.0), 3), "status": "new", "candidate": None}


def match(reg: Dict[str, Any], vec: np.ndarray) -> Dict[str, Any]:
    """Who a face vector is: ``{"person", "confidence", "status": "match" | "unsure" | "new", "candidate"}``."""
    best, best_sim = None, -1.0
    for p in reg["people"]:
        sim = max((float(np.dot(vec, dec(e))) for e in p["exemplars"]), default=-1.0)
        if sim > best_sim:
            best, best_sim = p, sim
    if best is not None and best_sim >= MATCH:
        return {"person": best["id"], "confidence": round(best_sim, 3), "status": "match", "candidate": None}
    if best is not None and best_sim >= UNSURE_LOW:
        return {"person": None, "confidence": round(best_sim, 3), "status": "unsure", "candidate": best["id"]}
    return {"person": None, "confidence": round(max(best_sim, 0.0), 3), "status": "new", "candidate": None}


# ============================ one file ============================
def sample_plan(shots: Sequence[Dict[str, Any]], duration: float) -> List[float]:
    """The times to look at: every half second inside a shot, at most 8 a shot (spread evenly), 1500 a file."""
    times: List[float] = []
    step = 1.0 / SAMPLE_RATE
    for s in shots:
        lo, hi = float(s["start"]) + 0.05, float(s["end"]) - 0.05
        if hi <= lo:
            continue
        n = max(1, int((hi - lo) / step) + 1)
        if n > MAX_PER_SHOT:
            picks = [lo + (hi - lo) * (i + 0.5) / MAX_PER_SHOT for i in range(MAX_PER_SHOT)]
        else:
            picks = [lo + step * i for i in range(n)]
        times.extend(t for t in picks if t < duration)
    if len(times) > MAX_SAMPLES:
        stride = len(times) / MAX_SAMPLES
        times = [times[int(i * stride)] for i in range(MAX_SAMPLES)]
    return times


def shot_of(shots: Sequence[Dict[str, Any]], t: float) -> Optional[int]:
    for s in shots:
        if float(s["start"]) <= t < float(s["end"]):
            return int(s["id"])
    return None


def group_into_tracks(detections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Faces seen in one shot, grouped by who they look like: a track per person with every sighting and a mean vector."""
    tracks: List[Dict[str, Any]] = []
    for d in sorted(detections, key=lambda d: d["t"]):
        vec = d.get("vec")
        if vec is None:
            continue
        for tr in tracks:
            if float(np.dot(vec, tr["_mean"])) >= MATCH:
                tr["samples"].append(d)
                m = np.mean([x["vec"] for x in tr["samples"]], axis=0)
                tr["_mean"] = m / (np.linalg.norm(m) or 1.0)
                break
        else:
            tracks.append({"samples": [d], "_mean": vec})
    return tracks


def _finish_track(tid: str, shot_id: int, tr: Dict[str, Any]) -> Dict[str, Any]:
    s = tr["samples"]
    return {"id": tid, "shot": shot_id, "start": round(min(x["t"] for x in s), 3), "end": round(max(x["t"] for x in s), 3), "count": len(s),
            "samples": [{"t": round(float(x["t"]), 3), "box": [round(float(v), 4) for v in x["box"]], "px": int(x["px"]), "score": float(x["score"])} for x in s],
            "embedding": enc(tr["_mean"]), "best_px": int(max(x["px"] for x in s))}


def build_scan(detections: List[Dict[str, Any]], shots: Sequence[Dict[str, Any]], sampled: int, duration: float) -> Dict[str, Any]:
    """The saved scan of a file from its detections (each has t, box (0-1), px, score, vec or None)."""
    by_shot: Dict[int, List[Dict[str, Any]]] = {}
    unrecognised: Dict[int, int] = {}
    most: Dict[int, int] = {}
    per_time: Dict[Tuple[int, float], int] = {}
    for d in detections:
        sid = shot_of(shots, d["t"])
        if sid is None:
            continue
        per_time[(sid, round(d["t"], 3))] = per_time.get((sid, round(d["t"], 3)), 0) + 1
        if d.get("vec") is None:
            unrecognised[sid] = unrecognised.get(sid, 0) + 1
            continue
        by_shot.setdefault(sid, []).append(d)
    for (sid, _t), n in per_time.items():
        most[sid] = max(most.get(sid, 0), n)
    tracks: List[Dict[str, Any]] = []
    for sid in sorted(by_shot):
        for i, tr in enumerate(group_into_tracks(by_shot[sid])):
            tracks.append(_finish_track(f"s{sid}t{i + 1}", sid, tr))
    return {"version": VERSION, "duration": round(float(duration), 3), "sampled": int(sampled), "faces_most_in_a_frame": {str(k): v for k, v in most.items()},
            "unrecognised": {str(k): v for k, v in unrecognised.items()}, "tracks": tracks}


def stream_frames(path: str, times: Sequence[float], size: Tuple[int, int], start_time: float = 0.0,
                  should_cancel: Optional[Callable[[], bool]] = None) -> Iterator[Tuple[float, np.ndarray]]:
    """(wanted time, RGB frame) for each wanted time, from one ffmpeg pass over the file at the sampling rate.

    Each wanted time gets the frame nearest it (within a quarter of a sampling step). Raises RuntimeError when ffmpeg cannot start.
    """
    import subprocess
    from classes.ffmpeg_cli import popen_ffmpeg
    w, h = size
    wanted = sorted(times)
    if not wanted:
        return
    seek = max(0.0, wanted[0] - 0.5)
    tolerance = 0.5 / SAMPLE_RATE
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seek + start_time:.3f}", "-i", path, "-an",
           "-vf", f"fps={SAMPLE_RATE:g},scale={w}:{h}:flags=area,format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = popen_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        n, nbytes, i = 0, w * h * 3, 0
        while i < len(wanted):
            if should_cancel and should_cancel():
                return
            buf = proc.stdout.read(nbytes) if proc.stdout else b""
            if len(buf) < nbytes:
                break
            t = seek + n / SAMPLE_RATE
            n += 1
            while i < len(wanted) and wanted[i] < t - tolerance:
                i += 1
            if i < len(wanted) and abs(wanted[i] - t) <= tolerance + 1e-6:
                yield wanted[i], np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
                i += 1
    finally:
        proc.kill()
        proc.wait()


def scan_file(path: str, sha: str, shots: Sequence[Dict[str, Any]], duration: float, size: Tuple[int, int], *, start_time: float = 0.0,
              detector: Any = None, recogniser: Any = None, should_cancel: Optional[Callable[[], bool]] = None,
              on_progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
    """Look for faces through a file and save what was found (tracks with vectors) under the people folder.

    Blocks on decode and inference: call it from a worker. Raises ``people_models.PeopleUnavailable`` when the models or runtime are
    missing, and InterruptedError when cancelled (nothing is saved then).
    """
    from classes.media_index import people_models as pm
    det = detector or pm.session("detector")
    rec = recogniser or pm.session("recognizer")
    times = sample_plan(shots, duration)
    detections: List[Dict[str, Any]] = []
    w, h = size
    done = 0
    for t, frame in stream_frames(path, times, size, start_time, should_cancel):
        if should_cancel and should_cancel():
            raise InterruptedError("people scan cancelled")
        for f in faces.detect(det, frame):
            x, y, bw, bh = f["box"]
            vec = None
            if min(bw, bh) >= faces.MIN_RECOGNISE_PX:
                vec = faces.embed(rec, faces.align(frame, f["kps"]))
            detections.append({"t": t, "box": [x / w, y / h, bw / w, bh / h], "px": int(min(bw, bh)), "score": f["score"], "vec": vec})
        done += 1
        if on_progress and done % 10 == 0:
            on_progress(min(1.0, done / max(1, len(times))))
    if should_cancel and should_cancel():          # the frame stream ends quietly on cancel: never save what is only part of a scan
        raise InterruptedError("people scan cancelled")
    scan = build_scan(detections, shots, done, duration)
    with _lock:
        _write_json(_scan_path(sha), scan)
    log.info("scanned %d frame(s) of a file, %d track(s)", done, len(scan["tracks"]))
    return scan


def scan_video(path: str, sha: str, shots: Sequence[Dict[str, Any]], duration: float, *, should_cancel: Optional[Callable[[], bool]] = None,
               on_progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
    """Probe a video, scan it for faces and register the people found: ``{"scan", "assigned"}``.

    Blocks on decode and inference. Raises ``RuntimeError`` when the picture cannot be read, ``people_models.PeopleUnavailable`` when
    the runtime or models are missing, and InterruptedError when cancelled.
    """
    from classes.media_index.probe import analysis_size, probe_media
    pr = probe_media(path)
    v = pr.get("video") or {}
    size = analysis_size(int(v.get("width") or 0), int(v.get("height") or 0), FRAME_LONG_EDGE)
    if not size:
        raise RuntimeError("could not read the picture of this file")
    scan = scan_file(path, sha, shots, duration or float(pr.get("duration") or 0.0), size, start_time=float(pr.get("start_time") or 0.0),
                     should_cancel=should_cancel, on_progress=on_progress)
    return {"scan": scan, "assigned": assign_new_faces(sha, scan)}


def load_scan(sha: str) -> Optional[Dict[str, Any]]:
    scan = _read_json(_scan_path(sha)) if sha else None
    return scan if scan and scan.get("version") == VERSION else None


# ============================ who is who ============================
def resolve_tracks(sha: str, scan: Dict[str, Any], reg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each track of a file with who it is: a pin the user made wins, else the closest known person, else unsure or new."""
    out = []
    for tr in scan.get("tracks", []):
        key = f"{sha[:16]}:{tr['id']}"
        pinned = reg["pins"].get(key)
        if pinned and _person(reg, pinned):
            who = {"person": pinned, "confidence": 1.0, "status": "pinned", "candidate": None}
        else:
            who = match(reg, dec(tr["embedding"]))
        out.append({**{k: v for k, v in tr.items() if k != "embedding"}, "key": key, **who})
    return out


def assign_new_faces(sha: str, scan: Dict[str, Any]) -> Dict[str, int]:
    """Give each recognisable track that matches nobody a new unnamed person (a number to refer to), and grow known people's exemplars.

    Tracks that are only 'unsure' are left alone: a possible match is reported, never merged. Returns counts.
    """
    counts = {"matched": 0, "new": 0, "unsure": 0}
    with _lock:
        reg = load_registry()
        for tr in scan.get("tracks", []):
            key = f"{sha[:16]}:{tr['id']}"
            if key in reg["pins"]:
                continue
            vec = dec(tr["embedding"])
            m = match(reg, vec)
            if m["status"] == "match":
                _add_exemplar(_person(reg, m["person"]), vec)
                counts["matched"] += 1
            elif m["status"] == "unsure":
                counts["unsure"] += 1
            else:
                new_person(reg, vec)
                counts["new"] += 1
        save_registry(reg)
    return counts


def name_person(person_id: str, name: str) -> Dict[str, Any]:
    """Give a person the name the user chose. Raises KeyError for an unknown id, ValueError for an empty name."""
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("a name is needed")
    with _lock:
        reg = load_registry()
        p = _person(reg, person_id)
        if p is None:
            raise KeyError(person_id)
        clash = next((q for q in reg["people"] if q["id"] != person_id and (q.get("name") or "").lower() == name.lower()), None)
        p["name"] = name[:80]
        save_registry(reg)
    return {"id": person_id, "name": p["name"], "same_name_as": clash["id"] if clash else None}


def merge_people(keep: str, drop: str) -> Dict[str, Any]:
    """Fold *drop* into *keep* (same person): its exemplars, pins and a missing name move across."""
    if keep == drop:
        raise ValueError("those are the same person already")
    with _lock:
        reg = load_registry()
        a, b = _person(reg, keep), _person(reg, drop)
        if a is None or b is None:
            raise KeyError(keep if a is None else drop)
        for e in b["exemplars"]:
            _add_exemplar(a, dec(e))
        for e in b.get("voices") or []:
            _add_voice(a, dec(e))
        if not a.get("name") and b.get("name"):
            a["name"] = b["name"]
        reg["pins"] = {k: (keep if v == drop else v) for k, v in reg["pins"].items()}
        reg["voice_pins"] = {k: (keep if v == drop else v) for k, v in reg["voice_pins"].items()}
        reg["people"] = [p for p in reg["people"] if p["id"] != drop]
        save_registry(reg)
    return {"kept": keep, "merged": drop, "name": a.get("name")}


def pin_track(sha: str, track_id: str, person_id: Optional[str]) -> Dict[str, Any]:
    """The user says who a track is (``person_id``), or that it is someone else (None: it becomes a new unnamed person)."""
    scan = load_scan(sha)
    tr = next((t for t in (scan or {}).get("tracks", []) if t["id"] == track_id), None)
    if tr is None:
        raise KeyError(track_id)
    key = f"{sha[:16]}:{track_id}"
    with _lock:
        reg = load_registry()
        vec = dec(tr["embedding"])
        if person_id is None:
            person_id = new_person(reg, vec)["id"]
        else:
            p = _person(reg, person_id)
            if p is None:
                raise KeyError(person_id)
            _add_exemplar(p, vec)
        reg["pins"][key] = person_id
        save_registry(reg)
    return {"track": key, "person": person_id}


def person_summary(reg: Dict[str, Any], scans: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each person with how much they appear: shots, files and seconds on screen (a track's span). No vectors."""
    stats: Dict[str, Dict[str, Any]] = {p["id"]: {"files": set(), "shots": 0, "seconds": 0.0} for p in reg["people"]}
    for sha, scan in scans.items():
        for tr in resolve_tracks(sha, scan, reg):
            if tr["person"] in stats:
                s = stats[tr["person"]]
                s["files"].add(sha)
                s["shots"] += 1
                s["seconds"] += max(0.0, tr["end"] - tr["start"]) + 1.0 / SAMPLE_RATE
    rows = [{"id": p["id"], "name": p.get("name"), "files": len(stats[p["id"]]["files"]), "shots": stats[p["id"]]["shots"],
             "seconds_on_screen": round(stats[p["id"]]["seconds"], 1)} for p in reg["people"]]
    rows.sort(key=lambda r: (-r["seconds_on_screen"], r["id"]))
    return rows


def appearances(sha: str, scan: Dict[str, Any], reg: Dict[str, Any], person_id: str) -> List[Dict[str, Any]]:
    """When a person is on screen in a file: a time range per track, with how sure the match is."""
    return [{"track": t["id"], "shot": t["shot"], "start": t["start"], "end": t["end"], "confidence": t["confidence"], "status": t["status"],
             "box": t["samples"][len(t["samples"]) // 2]["box"]} for t in resolve_tracks(sha, scan, reg) if t["person"] == person_id]


# ============================ voices ============================
def _voice_path(sha: str) -> str:
    return os.path.join(people_root(), "voices", f"{sha}.json")


def load_voice_scan(sha: str) -> Optional[Dict[str, Any]]:
    scan = _read_json(_voice_path(sha)) if sha else None
    return scan if scan and scan.get("version") == VERSION else None


def build_voice_scan(labels: Sequence[int], words: Sequence[Any], speakers: Sequence[Dict[str, Any]], duration: float) -> Dict[str, Any]:
    """The saved voice scan of a file: who spoke (a voiceprint each, seconds heard) and when (a run of words by one speaker)."""
    segments: List[Dict[str, Any]] = []
    for w, lab in zip(words, labels):
        a, b = float(w.startSec), float(w.endSec)
        if segments and segments[-1]["speaker"] == f"S{lab + 1}" and a - segments[-1]["end"] <= 1.5:
            segments[-1]["end"] = round(max(segments[-1]["end"], b), 3)
        else:
            segments.append({"start": round(a, 3), "end": round(b, 3), "speaker": f"S{lab + 1}"})
    return {"version": VERSION, "duration": round(float(duration), 3), "segments": segments,
            "speakers": [{"id": f"S{sp['index'] + 1}", "seconds": sp["seconds"], "embedding": enc(sp["vector"])} for sp in speakers]}


def scan_voices(path: str, sha: str, words: Sequence[Any], duration: float, *, session: Any = None,
                should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Tell the voices in a file apart, from its transcript's word times, and save them. Blocks on decode and inference.

    Raises ``people_models.PeopleUnavailable`` (no runtime or voice model), RuntimeError (audio unreadable) and InterruptedError.
    """
    from classes.media_index import people_models as pm, voiceprint as vp
    if not words:
        scan = build_voice_scan([], [], [], duration)       # nothing said: save that, so the file is not looked at again
    else:
        sess = session or pm.session("voice")
        from classes.speech.audio_extract import extract_mono_16k_wav
        wav, err = extract_mono_16k_wav(path)
        if err:
            raise RuntimeError(err)
        try:
            samples = vp.read_wav16k(wav)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
        got = vp.speakers_of(sess, samples, words, should_cancel=should_cancel)
        scan = build_voice_scan(got["labels"], words, got["speakers"], duration)
    if should_cancel and should_cancel():
        raise InterruptedError("voice scan cancelled")
    with _lock:
        _write_json(_voice_path(sha), scan)
    log.info("heard %d voice(s) in a file", len(scan["speakers"]))
    return scan


def scan_file_voices(path: str, sha: str, duration: float, shelf: Any, *, should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Listen to a file using the word times of its saved transcript, save the voices and register them: ``{"scan", "assigned", "had_speech"}``.

    A file with no transcript words is saved as silent (so it is not looked at again). Blocks on decode and inference.
    """
    from types import SimpleNamespace
    words = [SimpleNamespace(startSec=float(w["startSec"]), endSec=float(w["endSec"])) for w in (shelf.read_json(sha, "speech.json") or {}).get("words") or []]
    scan = scan_voices(path, sha, words, duration, should_cancel=should_cancel)
    return {"scan": scan, "assigned": assign_new_voices(sha, scan) if words else {"matched": 0, "new": 0, "unsure": 0}, "had_speech": bool(words)}


def resolve_speakers(sha: str, vscan: Dict[str, Any], reg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each voice heard in a file with whose it is: a pin the user made wins, else the closest known voice, else unsure or new."""
    out = []
    for sp in vscan.get("speakers", []):
        key = f"{sha[:16]}:{sp['id']}"
        pinned = reg["voice_pins"].get(key)
        if pinned and _person(reg, pinned):
            who = {"person": pinned, "confidence": 1.0, "status": "pinned", "candidate": None}
        else:
            who = match_voice(reg, dec(sp["embedding"]))
        out.append({"speaker": sp["id"], "key": key, "seconds": sp["seconds"], **who})
    return out


def assign_new_voices(sha: str, vscan: Dict[str, Any]) -> Dict[str, int]:
    """Give each voice that matches nobody a new unnamed person and grow known voices (never merging a merely unsure one)."""
    counts = {"matched": 0, "new": 0, "unsure": 0}
    with _lock:
        reg = load_registry()
        for sp in vscan.get("speakers", []):
            key = f"{sha[:16]}:{sp['id']}"
            if key in reg["voice_pins"]:
                continue
            vec = dec(sp["embedding"])
            m = match_voice(reg, vec)
            if m["status"] == "match":
                _add_voice(_person(reg, m["person"]), vec)
                counts["matched"] += 1
            elif m["status"] == "unsure":
                counts["unsure"] += 1
            else:
                new_person(reg, voice=vec)
                counts["new"] += 1
        save_registry(reg)
    return counts


def pin_speaker(sha: str, speaker_id: str, person_id: Optional[str]) -> Dict[str, Any]:
    """The user says whose voice a speaker is (``person_id``), or that it is someone new (None)."""
    vscan = load_voice_scan(sha)
    sp = next((x for x in (vscan or {}).get("speakers", []) if x["id"] == speaker_id), None)
    if sp is None:
        raise KeyError(speaker_id)
    with _lock:
        reg = load_registry()
        vec = dec(sp["embedding"])
        if person_id is None:
            person_id = new_person(reg, voice=vec)["id"]
        else:
            p = _person(reg, person_id)
            if p is None:
                raise KeyError(person_id)
            _add_voice(p, vec)
        reg["voice_pins"][f"{sha[:16]}:{speaker_id}"] = person_id
        save_registry(reg)
    return {"speaker": f"{sha[:16]}:{speaker_id}", "person": person_id}


def voice_at(sha: str, vscan: Dict[str, Any], reg: Dict[str, Any], t: float) -> Optional[Dict[str, Any]]:
    """Whose voice is heard at a time (within a second of a spoken stretch), or None."""
    seg = next((g for g in vscan.get("segments", []) if g["start"] - 0.5 <= t <= g["end"] + 0.5), None)
    if seg is None:
        return None
    who = next((r for r in resolve_speakers(sha, vscan, reg) if r["speaker"] == seg["speaker"]), None)
    return None if who is None else {**who, "start": seg["start"], "end": seg["end"]}


def speaking_shots(sha: str, vscan: Dict[str, Any], reg: Dict[str, Any], person_ids: Sequence[str], shots: Sequence[Dict[str, Any]]) -> Dict[int, float]:
    """{shot id: confidence} for the shots in which one of these people's voice is heard (an unsure voice is not counted)."""
    wanted = set(person_ids)
    who = {r["speaker"]: r for r in resolve_speakers(sha, vscan, reg) if r["person"] in wanted}
    out: Dict[int, float] = {}
    for seg in vscan.get("segments", []):
        r = who.get(seg["speaker"])
        if r is None:
            continue
        for sh in shots:
            if float(sh["end"]) > seg["start"] and float(sh["start"]) < seg["end"]:
                out[int(sh["id"])] = max(out.get(int(sh["id"]), 0.0), float(r["confidence"]))
    return out


def speaking_seconds(reg: Dict[str, Any], vscans: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """Seconds each person is heard speaking, over the voice scans."""
    out: Dict[str, float] = {}
    for sha, vscan in vscans.items():
        who = {r["speaker"]: r["person"] for r in resolve_speakers(sha, vscan, reg) if r["person"]}
        for seg in vscan.get("segments", []):
            if seg["speaker"] in who:
                out[who[seg["speaker"]]] = out.get(who[seg["speaker"]], 0.0) + (seg["end"] - seg["start"])
    return out


def voice_face_links(reg: Dict[str, Any], files: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Which voice goes with which face: ``files`` is {sha: (face scan, voice scan)}.

    While a voice speaks, if exactly one known face is on screen, that is a sighting of the pair. A pair is linked only after
    ``MIN_LINK_SECONDS`` of it and when that face is alone on screen for ``LINK_RATIO`` of all the time the voice speaks. A voice and a face of
    the same person (already merged) are not listed. Returns ``{voice, face, seconds, ratio, confidence}`` best first; the user decides
    whether to merge them.
    """
    seconds: Dict[Tuple[str, str], float] = {}
    totals: Dict[str, float] = {}
    for sha, (fscan, vscan) in files.items():
        tracks = [t for t in resolve_tracks(sha, fscan, reg) if t["person"]]
        voices = {r["speaker"]: r["person"] for r in resolve_speakers(sha, vscan, reg) if r["person"]}
        for seg in vscan.get("segments", []):
            vp = voices.get(seg["speaker"])
            if not vp:
                continue
            totals[vp] = totals.get(vp, 0.0) + (seg["end"] - seg["start"])      # all the time this voice speaks, seen or not
            on = {t["person"] for t in tracks if t["end"] + 0.5 >= seg["start"] and t["start"] - 0.5 <= seg["end"]}
            if len(on) == 1:
                face = next(iter(on))
                seconds[(vp, face)] = seconds.get((vp, face), 0.0) + (seg["end"] - seg["start"])
    links = []
    for (vp, face), sec in seconds.items():
        ratio = sec / totals[vp]
        if vp != face and sec >= MIN_LINK_SECONDS and ratio >= LINK_RATIO:
            links.append({"voice": vp, "face": face, "seconds": round(sec, 1), "ratio": round(ratio, 3), "confidence": round(ratio * min(1.0, sec / (3 * MIN_LINK_SECONDS)), 3)})
    links.sort(key=lambda r: -r["confidence"])
    return links


def find_people(reg: Dict[str, Any], ref: str) -> List[Dict[str, Any]]:
    """The people a name or id refers to (names are matched ignoring case; ``P3`` is an id)."""
    ref = " ".join(str(ref or "").split()).lower()
    if not ref:
        return []
    return [p for p in reg["people"] if p["id"].lower() == ref or (p.get("name") or "").lower() == ref]


def shots_with(reg: Dict[str, Any], person_ids: Sequence[str], scans: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[int, float]]:
    """{sha: {shot id: confidence}} for the shots where one of these people is on screen. A possible (unsure) match is not included."""
    wanted = set(person_ids)
    out: Dict[str, Dict[int, float]] = {}
    for sha, scan in scans.items():
        for tr in resolve_tracks(sha, scan, reg):
            if tr["person"] in wanted:
                shots = out.setdefault(sha, {})
                shots[tr["shot"]] = max(shots.get(tr["shot"], 0.0), float(tr["confidence"]))
    return out


# ============================ delete everything ============================
def delete_all(include_models: bool = False) -> Dict[str, Any]:
    """Remove every face scan and the registry (and optionally the downloaded models). Returns what was removed."""
    removed = {"scans": 0, "registry": False, "models": 0}
    with _lock:
        root = people_root()
        for folder in ("files", "voices"):
            try:
                removed["scans"] += len([n for n in os.listdir(os.path.join(root, folder)) if n.endswith(".json")])
            except OSError:
                pass
        removed["registry"] = os.path.isfile(_registry_path())
        shutil.rmtree(root, ignore_errors=True)
    if include_models:
        from classes.media_index import people_models as pm
        pm.clear_sessions()
        removed["models"] = pm.remove_models()
    return removed


def has_data() -> bool:
    return os.path.isdir(people_root()) and bool(os.listdir(people_root()))
