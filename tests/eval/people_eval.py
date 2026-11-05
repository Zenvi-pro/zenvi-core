"""Measure face matching on LFW (development only: nothing from it is kept in the repository).

    ZENVI_PEOPLE_MODELS=<folder with the two .onnx files> ZENVI_LFW_NPZ=<npz with images (n,250,250,3), labels> python tests/eval/people_eval.py

The npz can be made with scikit-learn's fetch_lfw_people(min_faces_per_person=20, color=True, slice_=(slice(0, 250), slice(0, 250))).
Prints the numbers recorded in results/people_calibration.json.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def load_sessions(models_dir: str):
    import onnxruntime as ort
    from classes.media_index import people_models as pm
    out = []
    for name in ("detector", "recognizer"):
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        out.append(ort.InferenceSession(os.path.join(models_dir, pm.MODELS[name]["file"]), opts, providers=["CPUExecutionProvider"]))
    return out


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    order = np.argsort(np.concatenate([neg, pos]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[len(neg):].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def run(models_dir: str, npz: str, sample: int = 900, seed: int = 0) -> dict:
    from classes.media_index import faces
    det, rec = load_sessions(models_dir)
    data = np.load(npz)
    imgs, labels = data["images"], data["labels"]
    idx = np.sort(np.random.default_rng(seed).choice(len(imgs), min(sample, len(imgs)), replace=False))
    embs, kept, started = [], [], time.time()
    for i in idx:
        found = faces.detect(det, imgs[i])
        if not found:
            continue
        best = max(found, key=lambda f: f["box"][2] * f["box"][3])
        embs.append(faces.embed(rec, faces.align(imgs[i], best["kps"])))
        kept.append(i)
    ms = (time.time() - started) / len(idx) * 1000
    emb, lab = np.stack(embs), labels[kept]
    iu = np.triu_indices(len(emb), 1)
    same = (lab[:, None] == lab[None, :])[iu]
    sims = (emb @ emb.T)[iu]
    pos, neg = sims[same], sims[~same]
    return {"detected": f"{len(kept)}/{len(idx)}", "ms_per_photo": round(ms), "auc": round(auc(pos, neg), 4),
            "same_p5": round(float(np.percentile(pos, 5)), 3), "different_p99": round(float(np.percentile(neg, 99)), 3),
            "by_threshold": {f"{t:g}": {"same_accepted": round(float((pos >= t).mean()), 3), "false_merge": round(float((neg >= t).mean()), 5)}
                             for t in (0.30, 0.363, 0.40, 0.45, 0.50)}}


if __name__ == "__main__":
    print(run(os.environ["ZENVI_PEOPLE_MODELS"], os.environ["ZENVI_LFW_NPZ"]))
