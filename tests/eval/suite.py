"""The measurements: each function runs one part of the index on the known-answer corpus and returns numbers.

Nothing here asserts. ``run_eval.py`` prints and saves the numbers, and ``thresholds.json`` holds the floors the fast
subset must not fall below (``tests/test_eval_fast.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from eval import corpus, metrics  # noqa: E402

HARD_TOLERANCE = 0.15       # seconds: the analysis runs at 10 frames a second
SOFT_TOLERANCE = 0.6        # a dissolve or fade has no single instant


def _structure(path: Path) -> Dict[str, Any]:
    from classes.media_index import structure
    from classes.media_index.probe import probe_media
    return structure.analyze_structure(str(path), probe_media(str(path)))


# ============================ shots ============================
def shots() -> Dict[str, Any]:
    """Cut detection: hard cuts, near-identical scenes, dissolves, fades through black, and clips with no cut at all."""
    out: Dict[str, Any] = {}
    hard_path, hard_truth = corpus.hard_cuts()
    near_path, near_truth = corpus.near_identical()
    found = {"hard": _structure(hard_path), "near": _structure(near_path)}
    out["hard"] = metrics.prf([b["t"] for b in found["hard"]["boundaries"]], hard_truth["hard"], HARD_TOLERANCE)
    out["near_identical"] = metrics.prf([b["t"] for b in found["near"]["boundaries"]], near_truth["hard"], HARD_TOLERANCE)
    soft: List[Dict[str, Any]] = []
    for name, builder in (("dissolve", corpus.dissolves), ("fade_through_black", corpus.fades_through_black)):
        path, truth = builder()
        res = _structure(path)
        times = [b["t"] for b in res["boundaries"]]
        out[name] = metrics.prf(times, truth["soft"], SOFT_TOLERANCE)
        out[name]["boundaries_per_transition"] = round(len(times) / max(1, len(truth["soft"])), 2)
        soft.append(out[name])
    out["soft_pooled"] = metrics.combine(*soft)
    false_cuts = 0
    for builder in (corpus.no_cuts_busy, corpus.flash_inside):
        path, _ = builder()
        false_cuts += len(_structure(path)["boundaries"])
    out["false_cuts_on_clips_with_none"] = false_cuts
    return out


# ============================ music ============================
TRACKS = {
    "easy": dict(),
    "low_contrast": dict(bpm=90.0, sections=((0.0, 16.0, 0.4), (16.0, 32.0, 0.55), (32.0, 48.0, 0.4))),
    "with_break": dict(bpm=128.0, sections=((0.0, 8.0, 0.3), (8.0, 24.0, 0.8), (24.0, 30.0, 0.05), (30.0, 48.0, 0.8))),
}
# Harder: sections that differ in which instruments play, a build with no sharp edge, a hi-hat entering at a steady level.
SONGS = {
    "timbre_only": dict(bpm=100.0, seconds=60.0, sections=[dict(start=0, end=20, level=0.5, bed=220, click=900),
                                                         dict(start=20, end=40, level=0.5, bed=660, click=1800, hat=True), dict(start=40, end=60, level=0.5, bed=220, click=900)]),
    "gradual_build": dict(bpm=110.0, seconds=60.0, sections=[dict(start=0, end=15, level=0.15), dict(start=15, end=40, level=(0.15, 0.9)), dict(start=40, end=60, level=0.9, hat=True)]),
    "same_loudness_new_instruments": dict(bpm=90.0, seconds=48.0, sections=[dict(start=0, end=16, level=0.6, bed=200, click=700), dict(start=16, end=32, level=0.6, bed=200, click=700, hat=True),
                                                                         dict(start=32, end=48, level=0.6, bed=400, click=1500, hat=True)]),
    "fade_out": dict(bpm=100.0, seconds=60.0, sections=[dict(start=0, end=30, level=0.8), dict(start=30, end=48, level=(0.8, 0.05)), dict(start=48, end=60, level=0.05)]),
}


def music() -> Dict[str, Any]:
    from classes.media_index import audio as au
    from classes.media_index.probe import probe_media
    out: Dict[str, Any] = {}
    pooled_beats: List[Dict[str, Any]] = []
    pooled_sections: List[Dict[str, Any]] = []
    for name, spec in TRACKS.items():
        path, truth = corpus.click_track(**spec)
        res = au.analyze_audio(str(path), probe_media(str(path)))
        tempo = res.get("tempo") or {}
        beats = metrics.prf(tempo.get("beats") or [], truth["beats"], 0.07)
        edges = [s["start"] for s in (res.get("music") or {}).get("sections", [])[1:]]
        sections = metrics.prf(edges, truth["section_edges"], 2.0)
        out[name] = {"bpm_error": round(abs((tempo.get("bpm") or 0.0) - truth["bpm"]), 3), "beat_f1": beats["f1"], "beat_mean_error": beats["mean_error"],
                     "section_f1": sections["f1"], "section_extra": sections["extra"], "section_missed": sections["missed"]}
        pooled_beats.append(beats)
        pooled_sections.append(sections)
    for name, spec in SONGS.items():
        path, truth = corpus.song(**spec)
        res = au.analyze_audio(str(path), probe_media(str(path)))
        edges = [s["start"] for s in (res.get("music") or {}).get("sections", [])[1:]]
        # where a gradual build or fade "starts" is fuzzy by a few seconds (loudness in dB is flat near the top): 3 s there, 2 s elsewhere
        sections = metrics.prf(edges, truth["section_edges"], 3.0 if name in ("gradual_build", "fade_out") else 2.0)
        out[name] = {"section_f1": sections["f1"], "section_extra": sections["extra"], "section_missed": sections["missed"],
                     "bpm_error": round(abs(((res.get("tempo") or {}).get("bpm") or 0.0) - truth["bpm"]), 3)}
        pooled_sections.append(sections)
    out["beat_f1_pooled"] = metrics.combine(*pooled_beats)["f1"]
    out["section_f1_pooled"] = metrics.combine(*pooled_sections)["f1"]
    out["bpm_error_max"] = max(v["bpm_error"] for v in out.values() if isinstance(v, dict))
    return out


# ============================ voice edges ============================
def _complement(silences: List[List[float]], seconds: float) -> List[List[float]]:
    spans, cursor = [], 0.0
    for a, b in sorted(silences):
        if a > cursor:
            spans.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < seconds:
        spans.append([cursor, seconds])
    return spans


def voice_edges() -> Dict[str, Any]:
    """Where speech-like bursts start and stop: from the audio layer's silence, and from the speech module's voice detector."""
    from classes.media_index import audio as au
    from classes.media_index.probe import probe_media
    path, truth = corpus.voice_bursts()
    res = au.analyze_audio(str(path), probe_media(str(path)))
    out = {"audio_layer": metrics.edge_errors(_complement(res.get("silence_ranges") or [], truth["seconds"]), truth["spans"])}
    from classes.media_index import voice
    samples = voice.read_audio(str(path), 0.0, truth["seconds"])
    found = voice.voice_edges(samples)
    out["energy_edges"] = metrics.edge_errors([[a, b] for a, b in found.get("spans", [])], truth["spans"])
    out["energy_edges"]["pauses_found"] = len(found.get("pauses", []))
    try:
        from classes.speech import vad
        from classes.speech.runtime import CancelToken
        windows = vad.detect_speech_windows(str(path), token=CancelToken(), min_speech_sec=0.2, min_silence_sec=0.2)
        out["voice_detector"] = metrics.edge_errors([list(w) for w in windows], truth["spans"])
    except Exception as exc:  # noqa: BLE001
        out["voice_detector"] = {"error": str(exc)[:120]}
    return out


# ============================ quality ============================
def quality(seeds: Tuple[int, ...] = (1, 2, 3, 4)) -> Dict[str, Any]:
    """Blur, shake and exposure flags on textured scenes with the fault added: how many are caught, how many sharp ones are accused."""
    from PIL import ImageFilter
    import media_fixtures as mf
    from classes.media_index import quality as Q

    cache = corpus.cache_dir() / "quality"
    cache.mkdir(exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for seed in seeds:
        base = mf.scene(seed)
        rng = np.random.default_rng(seed)
        jit: Dict[int, Tuple[float, float]] = {}

        def jitter(t, rng=rng, jit=jit, base=base):
            k = int(round(t * 30))
            jit.setdefault(k, (rng.uniform(-10, 10), rng.uniform(-10, 10)))
            return base.width / 2 + jit[k][0], base.height / 2 + jit[k][1], 1.0

        variants = {
            "sharp": (base, None, ()), "blur_light": (base.filter(ImageFilter.GaussianBlur(3)), None, ("soft_or_blurry",)),
            "blur_medium": (base.filter(ImageFilter.GaussianBlur(6)), None, ("soft_or_blurry",)),
            "blur_heavy": (base.filter(ImageFilter.GaussianBlur(12)), None, ("soft_or_blurry",)),
            "shaky": (base, jitter, ("shaky",)), "pan": (base, lambda t, base=base: (base.width / 2 + 40 * t, base.height / 2, 1.0), ()),
            "dark": (base.point(lambda v: int(v * 0.18)), None, ("dark",)), "bright": (base.point(lambda v: min(255, int(v * 0.5 + 150))), None, ("bright",)),
        }
        for name, (img, pose, expected) in variants.items():
            path = cache / f"q{seed}_{name}.mp4"
            if not path.exists():
                mf.render(str(path), img, pose or mf.hold(img), 3.0)
            st = _structure(path)
            shot = st["shots"][0]
            rows.append({"seed": seed, "name": name, "expected": expected, "shot": shot, "profile": None})
    medians = {seed: float(np.median([r["shot"]["sharpness"] for r in rows if r["seed"] == seed and r["name"] == "sharp"])) for seed in seeds}
    for r in rows:
        q = Q.shot_quality({**r["shot"], "look": None}, median_sharpness=medians[r["seed"]], audio=None, watch=None)
        flags = set(q["flags"])
        r["flags"] = flags
        r["got_soft_or_blurry"] = bool(flags & {"soft", "blurry"})
        r["got_blurry"] = "blurry" in flags
        r["got_shaky"] = "shaky" in flags
    out: Dict[str, Any] = {}
    sharp = [r for r in rows if r["name"] in ("sharp", "pan")]
    out["blur"] = metrics.confusion([r["got_soft_or_blurry"] for r in rows], [bool(r["expected"] and "soft_or_blurry" in r["expected"]) for r in rows])
    out["blur_by_amount"] = {n: round(sum(r["got_soft_or_blurry"] for r in rows if r["name"] == n) / len(seeds), 2) for n in ("blur_light", "blur_medium", "blur_heavy")}
    out["heavy_blur_called_blurry"] = round(sum(r["got_blurry"] for r in rows if r["name"] == "blur_heavy") / len(seeds), 2)
    out["light_blur_called_blurry"] = round(sum(r["got_blurry"] for r in rows if r["name"] in ("blur_light", "blur_medium")) / (2 * len(seeds)), 2)
    out["shake"] = metrics.confusion([r["got_shaky"] for r in rows], [bool(r["expected"] and "shaky" in r["expected"]) for r in rows])
    out["sharp_clips_accused"] = sum(1 for r in sharp if r["got_soft_or_blurry"] or r["got_shaky"])
    return out


# ============================ everything ============================
def run_fast() -> Dict[str, Any]:
    return {"shots": shots(), "music": music(), "voice_edges": voice_edges(), "quality": quality()}
