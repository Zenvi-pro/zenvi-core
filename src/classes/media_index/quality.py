"""
 @file
 @brief Which shots are worth using: sharpness, shake, exposure, audio trouble, take groups and scenes.

 Everything here is derived from layers already on the shelf (structure, look, audio, watch, vectors),
 so it costs nothing to compute and needs no new storage: ``library`` calls it when a file is loaded.
 Each field says whether it was *measured* from the file or *inferred* by the model, and the measured
 score never depends on the inferred one, so a model's opinion cannot hide a real defect.

 Known limit: very noisy footage reads sharper than it is (noise looks like detail). The model's
 ``usable`` flag, when present, covers that case.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from classes.media_index import schema as S

# --- sharpness: the strong edges of the lightly smoothed frame, relative to its own contrast -------
SHARP_PERCENTILE = 95.0
SOFT_REL = 0.6              # a shot this much softer than its file's median is "soft"
BLURRY_REL = 0.35
# A file with one shot has no siblings to compare with, so there is also an absolute floor: a real scene at
# 320 px blurred by about 6 px measures 0.24 and the same scene sharp 0.6 (see frame_sharpness).
ABS_BLURRY = 0.22

# --- shake: the fast jitter left after removing pans and zooms (px at the analysis frame) ----------
SHAKE_FULL_PX = 1.4         # jitter at or above this reads as fully shaky (4x the handheld threshold)
SHAKY_AT = 0.5
MIN_MOTION_PAIRS = 5

# --- exposure -------------------------------------------------------------------------------------
DARK_LUMA = 0.15
BRIGHT_LUMA = 0.85
CLIPPED_HIGHLIGHTS = 0.05   # share of pixels at pure white
CRUSHED_SHADOWS = 0.30      # share of pixels at pure black in an otherwise dark shot
FLAT_CONTRAST = 0.20

# --- audio trouble per shot -----------------------------------------------------------------------
AUDIO_CLIPPED = 0.01        # share of analysis frames at full scale
AUDIO_RUMBLE = 0.55         # share of spectral magnitude below 100 Hz (wind, handling noise)

# --- grouping -------------------------------------------------------------------------------------
TAKE_SIMILARITY = 0.90      # two shots whose mean picture vectors are this alike are takes of one thing
SCENE_SIMILARITY = 0.78     # neighbouring shots below this start a new scene


# ============================ sharpness ============================
def _box3(g: np.ndarray) -> np.ndarray:
    p = np.pad(g, 1, mode="edge")
    h, w = g.shape
    total = np.zeros_like(g)
    for i in range(3):
        for j in range(3):
            total += p[i:i + h, j:j + w]
    return total / 9.0


def frame_sharpness(gray: np.ndarray) -> float:
    """Edge strength of a grey frame relative to its contrast; lower = softer. 0 for a flat frame.

    Measured on 0-255 grey. The frame is smoothed 3x3 first and only the strong edges count (95th
    percentile of the gradient), so ordinary sensor noise does not read as detail. Calibrated on a real
    scene blurred 0 to 6 px at 320 px wide: 0.65, 0.59, 0.48, 0.36, 0.24 (and 0.66-0.35 with mild noise).
    """
    if gray.ndim != 2 or gray.shape[0] < 8 or gray.shape[1] < 8:
        return 0.0
    g = _box3(gray.astype(np.float32))
    spread = float(g.std())
    if spread < 1.0:
        return 0.0
    mag = np.hypot(np.diff(g, axis=1)[:-1, :], np.diff(g, axis=0)[:, :-1])
    return float(np.percentile(mag, SHARP_PERCENTILE)) / (spread + 1.0)


# ============================ per-shot measurements ============================
def shake_score(motion: Optional[Dict[str, Any]]) -> Optional[float]:
    """0 (steady) to 1 (very shaky), or None when too few frame pairs were measurable."""
    if not motion or int(motion.get("valid_pairs") or 0) < MIN_MOTION_PAIRS:
        return None
    return round(float(min(1.0, max(0.0, float(motion.get("jitter_px") or 0.0)) / SHAKE_FULL_PX)), 3)


def exposure_of(profile: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The exposure problems a look profile shows: dark, bright, clipped highlights, crushed shadows, flat."""
    if not profile or not profile.get("present"):
        return None
    raw_luma = profile.get("avg_luma")
    luma = 0.5 if raw_luma is None else float(raw_luma)
    hi = float(profile.get("clipped_highlights") or 0.0)
    lo = float(profile.get("clipped_shadows") or 0.0)
    contrast = profile.get("contrast_span")
    problems: List[str] = []
    if luma < DARK_LUMA:
        problems.append("dark")
    if luma > BRIGHT_LUMA:
        problems.append("bright")
    if hi > CLIPPED_HIGHLIGHTS:
        problems.append("clipped_highlights")
    if lo > CRUSHED_SHADOWS and luma < 0.35:
        problems.append("crushed_shadows")
    if contrast is not None and float(contrast) < FLAT_CONTRAST:
        problems.append("flat")
    return {"problems": problems, "avg_luma": round(luma, 3), "clipped_highlights": round(hi, 4), "clipped_shadows": round(lo, 4)}


def audio_trouble(windows: Sequence[Dict[str, Any]], start: float, end: float, noise_floor_db: Optional[float] = None
                  ) -> Optional[Dict[str, Any]]:
    """Clipping, rumble and level of the audio windows overlapping [start, end); None without audio facts."""
    mine = [w for w in windows if w["end"] > start and w["start"] < end]
    if not mine:
        return None
    clipped = max(float(w.get("clipped_ratio") or 0.0) for w in mine)
    rumble = float(np.mean([float(w.get("rumble_ratio") or 0.0) for w in mine]))
    level = float(np.mean([float(w["rms_db"]) for w in mine]))
    problems: List[str] = []
    if clipped >= AUDIO_CLIPPED:
        problems.append("clipping")
    if rumble >= AUDIO_RUMBLE:
        problems.append("rumble")
    return {"problems": problems, "clipped_ratio": round(clipped, 4), "rumble_ratio": round(rumble, 3),
            "rms_db": round(level, 1), "noise_floor_db": noise_floor_db}


def shot_quality(shot: Dict[str, Any], *, median_sharpness: Optional[float], audio: Optional[Dict[str, Any]],
                 watch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The quality block of one shot: measured facts, a measured score, and the model's reading beside them."""
    sharp = shot.get("sharpness")
    rel = None
    if sharp is not None and median_sharpness:
        rel = round(float(min(2.0, float(sharp) / median_sharpness)), 3)
    shake = shake_score(shot.get("motion"))
    exposure = exposure_of(shot.get("look"))
    flags: List[str] = []
    if shot.get("black"):
        flags.append("black")
    very_soft = sharp is not None and float(sharp) < ABS_BLURRY
    if very_soft or (rel is not None and rel < BLURRY_REL):
        flags.append("blurry")
    elif rel is not None and rel < SOFT_REL:
        flags.append("soft")
    if shake is not None and shake >= SHAKY_AT:
        flags.append("shaky")
    for p in (exposure or {}).get("problems", []):
        flags.append(p)
    for p in (audio or {}).get("problems", []):
        flags.append("audio_" + p)

    score = 0.0 if shot.get("black") else 1.0
    if score:
        softness = min(rel, 0.3) if (very_soft and rel is not None) else (0.3 if very_soft else rel)
        if softness is not None and softness < SOFT_REL:
            score -= 0.35 * (SOFT_REL - softness) / SOFT_REL
        if shake is not None:
            score -= 0.35 * shake
        probs = (exposure or {}).get("problems", [])
        score -= 0.25 * sum(1 for p in probs if p in ("dark", "bright")) + 0.15 * sum(
            1 for p in probs if p in ("clipped_highlights", "crushed_shadows", "flat"))
        score -= 0.10 * len((audio or {}).get("problems", []))
    score = round(float(min(1.0, max(0.0, score))), 3)

    block: Dict[str, Any] = {"sharpness": sharp, "sharp_rel": rel, "shake": shake, "exposure": exposure, "audio": audio,
                             "flags": flags, "score": score, "kind": S.MEASURED}
    inferred: Dict[str, Any] = {}
    for key in ("interest", "highlight_reason", "usable", "usable_reason", "people_count", "on_camera_speaker", "emotion",
                "hook_potential"):
        if watch and watch.get(key) not in (None, ""):
            inferred[key] = watch[key]
    if inferred:
        block["inferred"] = inferred
    interest = inferred.get("interest")
    highlight = score if interest is None else round(0.6 * float(interest) + 0.4 * score, 3)
    if inferred.get("usable") is False:
        highlight = min(highlight, 0.1)
    block["highlight"] = highlight
    return block


# ============================ vectors: takes and scenes ============================
def shot_vectors(fi: Any) -> Dict[int, np.ndarray]:
    """Unit mean picture vector per shot id (empty when the file has no picture vectors)."""
    if getattr(fi, "image_matrix", None) is None:
        return {}
    by_shot: Dict[int, List[int]] = {}
    for i, row in enumerate(fi.image_rows):
        by_shot.setdefault(int(row["shot"]), []).append(i)
    out: Dict[int, np.ndarray] = {}
    for sid, rows in by_shot.items():
        v = fi.image_matrix[rows].mean(axis=0)
        n = float(np.linalg.norm(v))
        if n > 0:
            out[sid] = v / n
    return out


def take_groups(files: Sequence[Any], threshold: float = TAKE_SIMILARITY) -> List[Dict[str, Any]]:
    """Groups of shots (across files) that show the same thing, best-quality first.

    Greedy and order-independent in effect: shots are visited best-first and join the first group whose
    leader they resemble, so the leader is always the best take. Only groups of two or more are returned.
    """
    items = []
    seen_sha: set = set()
    for fi in files:
        if fi.sha in seen_sha:          # the same footage imported twice is one set of shots
            continue
        seen_sha.add(fi.sha)
        vecs = shot_vectors(fi)
        for shot in fi.shots:
            v = vecs.get(int(shot["id"]))
            if v is None or shot.get("black"):
                continue
            q = shot.get("quality") or {}
            items.append((float(q.get("highlight", 0.0)), fi, shot, v))
    items.sort(key=lambda it: -it[0])
    groups: List[Dict[str, Any]] = []
    for score, fi, shot, v in items:
        for g in groups:
            if float(np.dot(g["_lead"], v)) >= threshold:
                g["members"].append({"file_id": fi.file_id, "shot_id": int(shot["id"]), "highlight": score})
                break
        else:
            groups.append({"_lead": v, "members": [{"file_id": fi.file_id, "shot_id": int(shot["id"]), "highlight": score}]})
    out = []
    for g in groups:
        if len(g["members"]) >= 2:
            out.append({"best": g["members"][0], "members": g["members"]})
    return out


def scenes_of(fi: Any, threshold: float = SCENE_SIMILARITY) -> List[Dict[str, Any]]:
    """Runs of neighbouring shots that look alike (same place, same setup), as scene cards.

    Uses picture vectors when the file has them; without vectors each shot is its own scene.
    """
    vecs = shot_vectors(fi)
    scenes: List[List[Dict[str, Any]]] = []
    prev = None
    for shot in fi.shots:
        v = vecs.get(int(shot["id"]))
        joins = bool(scenes) and v is not None and prev is not None and float(np.dot(prev, v)) >= threshold
        if joins:
            scenes[-1].append(shot)
        else:
            scenes.append([shot])
        prev = v if v is not None else None
    cards = []
    for i, group in enumerate(scenes):
        watches = [s.get("watch") or {} for s in group]
        moods = [w["mood"] for w in watches if w.get("mood")]
        labels: List[str] = []
        for w in watches:
            for o in w.get("objects") or []:
                if o.get("label") and o["label"] not in labels:
                    labels.append(o["label"])
        best = max(group, key=lambda s: float((s.get("quality") or {}).get("highlight", 0.0)))
        cards.append({
            "id": i, "start": group[0]["start"], "end": group[-1]["end"], "shot_ids": [int(s["id"]) for s in group],
            "summary": next((w["description"] for w in watches if w.get("description")), ""),
            "mood": max(set(moods), key=moods.count) if moods else "",
            "objects": labels[:8], "speech": any(float((s.get("speech") or {}).get("speech_ratio") or 0.0) >= 0.2 for s in group),
            "best_shot": int(best["id"]),
        })
    return cards
