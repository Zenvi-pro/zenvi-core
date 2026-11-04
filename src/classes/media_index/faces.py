"""
 @file
 @brief Finding faces in a picture and turning each into a short number list, with plain numpy around two ONNX models.

 Detection is YuNet (decoded here, no OpenCV), recognition is SFace on a face aligned to the standard five-point template.
 Every function takes RGB uint8 arrays. The numbers a face becomes are biometric data: nothing here logs, prints or returns
 them except to the caller that asked for them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

DET_SIZE = 640
STRIDES = (8, 16, 32)
SCORE_MIN = 0.6
NMS_IOU = 0.3
RECOG_SIZE = 112
EMBED_DIMS = 128
MIN_RECOGNISE_PX = 40         # a face narrower than this (in the picture it was found in) is counted but not recognised
# where the five landmarks (face's right eye, left eye, nose, right and left mouth corner) go in a 112x112 aligned face
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)


# ============================ detection ============================
def letterbox(rgb: np.ndarray, size: int = DET_SIZE) -> Tuple[np.ndarray, float]:
    """The picture scaled to fit a size x size square (kept in proportion, padded with black), as a BGR float blob (1,3,size,size)."""
    h, w = rgb.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    ys = np.minimum(h - 1, (np.arange(nh) / scale).astype(int))
    xs = np.minimum(w - 1, (np.arange(nw) / scale).astype(int))
    small = rgb[ys][:, xs]
    canvas = np.zeros((size, size, 3), dtype=np.float32)
    canvas[:nh, :nw] = small[:, :, ::-1]
    return canvas.transpose(2, 0, 1)[None], scale


def decode_yunet(outputs: Sequence[np.ndarray], size: int = DET_SIZE, score_min: float = SCORE_MIN) -> List[Dict[str, Any]]:
    """Faces from YuNet's twelve raw outputs (cls, obj, bbox, kps at strides 8, 16, 32), in the letterboxed square's pixels."""
    cls, obj, box, kps = outputs[0:3], outputs[3:6], outputs[6:9], outputs[9:12]
    found: List[Dict[str, Any]] = []
    for i, stride in enumerate(STRIDES):
        cols = size // stride
        score = np.sqrt(np.clip(cls[i][0, :, 0], 0, 1) * np.clip(obj[i][0, :, 0], 0, 1))
        for idx in np.nonzero(score >= score_min)[0]:
            r, c = divmod(int(idx), cols)
            b, k = box[i][0, idx], kps[i][0, idx]
            cx, cy = (c + b[0]) * stride, (r + b[1]) * stride
            w, h = float(np.exp(b[2]) * stride), float(np.exp(b[3]) * stride)
            pts = np.array([[(k[2 * n] + c) * stride, (k[2 * n + 1] + r) * stride] for n in range(5)])
            found.append({"box": [cx - w / 2, cy - h / 2, w, h], "score": float(score[idx]), "kps": pts})
    return found


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def nms(faces: List[Dict[str, Any]], iou_max: float = NMS_IOU) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for f in sorted(faces, key=lambda f: -f["score"]):
        if all(_iou(f["box"], k["box"]) <= iou_max for k in kept):
            kept.append(f)
    return kept


def detect(session: Any, rgb: np.ndarray, score_min: float = SCORE_MIN) -> List[Dict[str, Any]]:
    """Faces in a picture: box [x, y, w, h] and the five landmarks in its own pixels, plus a 0-1 score, best first."""
    blob, scale = letterbox(rgb)
    outputs = session.run(None, {session.get_inputs()[0].name: blob})
    h, w = rgb.shape[:2]
    faces = []
    for f in nms(decode_yunet(outputs, score_min=score_min)):
        x, y, bw, bh = (v / scale for v in f["box"])
        x1, y1 = max(0.0, x), max(0.0, y)
        x2, y2 = min(float(w), x + bw), min(float(h), y + bh)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        faces.append({"box": [x1, y1, x2 - x1, y2 - y1], "score": round(f["score"], 4), "kps": f["kps"] / scale})
    return faces


# ============================ recognition ============================
def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The 2x3 matrix (rotation, uniform scale, shift) that best maps src points onto dst (least squares, Umeyama)."""
    n = src.shape[0]
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s0, d0 = src - mu_s, dst - mu_d
    cov = d0.T @ s0 / n
    u, sv, vt = np.linalg.svd(cov)
    sign = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[-1] = -1
    rot = u @ np.diag(sign) @ vt
    var = (s0 ** 2).sum() / n
    scale = (sv * sign).sum() / var if var > 0 else 1.0
    shift = mu_d - scale * rot @ mu_s
    return np.hstack([scale * rot, shift[:, None]])


def align(rgb: np.ndarray, kps: np.ndarray, size: int = RECOG_SIZE) -> np.ndarray:
    """The face warped onto the standard template: (size, size, 3) uint8, bilinear, black outside the picture."""
    m = similarity_transform(np.asarray(kps, dtype=np.float64), TEMPLATE * (size / RECOG_SIZE))
    inv = np.linalg.inv(np.vstack([m, [0, 0, 1]]))[:2]
    ys, xs = np.mgrid[0:size, 0:size]
    pts = inv @ np.stack([xs.ravel(), ys.ravel(), np.ones(size * size)])
    sx, sy = pts[0].reshape(size, size), pts[1].reshape(size, size)
    h, w = rgb.shape[:2]
    x0, y0 = np.floor(sx).astype(int), np.floor(sy).astype(int)
    fx, fy = (sx - x0)[..., None], (sy - y0)[..., None]
    inside = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)
    x0c, y0c = np.clip(x0, 0, w - 2), np.clip(y0, 0, h - 2)
    img = rgb.astype(np.float32)
    top = img[y0c, x0c] * (1 - fx) + img[y0c, x0c + 1] * fx
    bottom = img[y0c + 1, x0c] * (1 - fx) + img[y0c + 1, x0c + 1] * fx
    out = top * (1 - fy) + bottom * fy
    out[~inside] = 0
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def embed(session: Any, aligned_rgb: np.ndarray) -> np.ndarray:
    """The 128 numbers for an aligned face, scaled to length 1 (so a dot product is the cosine similarity)."""
    blob = aligned_rgb.astype(np.float32).transpose(2, 0, 1)[None]
    vec = np.asarray(session.run(None, {session.get_inputs()[0].name: blob})[0], dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))
