"""Local dubbing worker: lip sync, face swap, Ming TTS, Ming export and the backend probe.

Runs only in the downloaded pack env (numpy, opencv-python-headless, onnxruntime, onnx, onnxslim; torch only inside the
Ming functions). It must not import zenvi or openshot modules. CLI: --command <name> --args <json file>. stdout carries
JSON lines only: {"progress": 0..1, "message"}, then {"result": {...}} or {"error": "..."}.
"""

import argparse
import functools
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

import numpy as np

CPU = ["CPUExecutionProvider"]
MODEL_FILES = {
    "edtalk": "edtalk_slim.onnx",
    "wav2lip": "wav2lip_gan_96.onnx",
    "hyperswap": "hyperswap_1a_256.onnx",
    "yoloface": "yoloface_8n.onnx",
    "arcface": "arcface_w600k_r50.onnx",
    "fan68": "fan_68_5.onnx",
    "ming_backbone": "ming_onnx/ming_backbone.onnx",
    "ming_flow": "ming_onnx/ming_flow.onnx",
    "ming_decoder": "ming_onnx/ming_decoder.onnx",
}
PROBE_THRESHOLD = {"ming_backbone": 0.99999, "ming_flow": 0.9999}  # autoregressive and flow parts: errors compound
DIM_DEFAULTS = {"seq": 3, "past": 150, "frames": 61}  # symbolic input dims for the probe; past = KV length
SHIFT = 2  # frames: the audio window for frame i is the one for i - SHIFT (measured 0 ms lip offset)
LOWER_FACE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 35, 34, 33, 32, 31]
WARP_TEMPLATES = {
    "arcface_112_v2": np.array([[0.34191607, 0.46157411], [0.65653393, 0.45983393], [0.50022500, 0.64050536],
                                [0.37097589, 0.82469196], [0.63151696, 0.82325089]]),
    "arcface_128": np.array([[0.36167656, 0.40387734], [0.63696719, 0.40235469], [0.50019687, 0.56044219],
                             [0.38710391, 0.72160547], [0.61507734, 0.72034453]]),
    "ffhq_512": np.array([[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
                          [0.39308822, 0.72541100], [0.61150205, 0.72490465]]),
}


def emit(obj, stream):
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


def progress_reporter(stream):
    last = [float("-inf")]

    def report(fraction, message):
        now = time.monotonic()
        if fraction >= 1 or now - last[0] >= 0.5:
            last[0] = now
            emit({"progress": round(float(fraction), 3), "message": message}, stream)

    return report


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="dubbing_worker.py")
    parser.add_argument("--command", required=True, choices=sorted(COMMANDS))
    parser.add_argument("--args", required=True)
    ns = parser.parse_args(argv)
    with open(ns.args, encoding="utf-8") as fh:
        return ns.command, json.load(fh)


def main(argv):
    command, args = parse_args(argv)
    stream, sys.stdout = sys.stdout, sys.stderr  # stdout is the protocol; stray prints go to stderr
    try:
        result = COMMANDS[command](args, progress_reporter(stream))
    except Exception as exc:
        traceback.print_exc()
        emit({"error": str(exc) or type(exc).__name__}, stream)
        return 1
    emit({"result": result}, stream)
    return 0


_openvino_libs_added = False


def _ort():
    global _openvino_libs_added
    import onnxruntime as ort

    if sys.platform == "win32" and not _openvino_libs_added:
        _openvino_libs_added = True
        try:
            from onnxruntime.tools import add_openvino_win_libs
        except ImportError:
            pass
        else:
            add_openvino_win_libs.add_openvino_libs_to_path()
    return ort


def session(path, providers=CPU):
    ort = _ort()
    opts = ort.SessionOptions()
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    providers = [tuple(p) if isinstance(p, list) else p for p in providers]
    if providers == CPU:
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    return ort.InferenceSession(path, opts, providers=providers)


def model_path(home, key):
    return os.path.join(home, "models", MODEL_FILES[key])


def _picks(args):
    picks = args.get("picks") or {}
    return picks.get("picks", picks)


def _providers(picks, key):
    return picks[key]["providers"] if key in picks else CPU


def candidate_providers(home):
    have = _ort().get_available_providers()
    candidates = []
    if "OpenVINOExecutionProvider" in have:
        cache = os.path.join(home, "ovcache")
        os.makedirs(cache, exist_ok=True)
        for label, device, extra in (("openvino_gpu", "GPU", {}), ("openvino_gpu_fp32", "GPU", {"precision": "FP32"}),
                                     ("openvino_cpu", "CPU", {})):
            ov = ("OpenVINOExecutionProvider", {"device_type": device, "cache_dir": cache, **extra})
            candidates.append((label, [ov, "CPUExecutionProvider"]))
    for label, ep, options in (("webgpu", "WebGpuExecutionProvider", {}), ("directml", "DmlExecutionProvider", {}),
                               ("coreml", "CoreMLExecutionProvider", {"ModelFormat": "MLProgram"}),
                               ("cuda", "CUDAExecutionProvider", {})):
        if ep in have:
            candidates.append((label, [(ep, options) if options else ep, "CPUExecutionProvider"]))
    candidates.append(("cpu", CPU))
    return candidates


def reference_feed(sess, seed=0):
    rng = np.random.default_rng(seed)
    feed = {}
    for item in sess.get_inputs():
        shape = [d if isinstance(d, int) else DIM_DEFAULTS.get(d, 1) for d in item.shape]
        if item.type == "tensor(int64)":
            feed[item.name] = rng.integers(0, 64, shape)
        else:
            feed[item.name] = rng.uniform(-1, 1, shape).astype(np.float16 if item.type == "tensor(float16)" else np.float32)
    return feed


def cosine(a, b):
    a, b = a.ravel().astype(np.float64), b.ravel().astype(np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def pick_backend(rows, threshold):
    """Fastest candidate whose output matches CPU (cosine >= threshold); the CPU row when none does."""
    ok = [r for r in rows if "cosine" in r and r["cosine"] >= threshold]
    return min(ok, key=lambda r: r["ms"]) if ok else next(r for r in rows if r["backend"] == "cpu")


def probe_model(home, key, candidates):
    path = model_path(home, key)
    cpu = session(path)
    feed = reference_feed(cpu)
    want = cpu.run(None, feed)[0]
    rows = []
    for label, providers in candidates:
        try:
            sess = session(path, providers)
            got = sess.run(None, feed)[0]
            times = []
            for _ in range(5):
                start = time.perf_counter()
                sess.run(None, feed)
                times.append((time.perf_counter() - start) * 1000)
            rows.append({"backend": label, "providers": providers, "ms": round(sorted(times)[2], 1),
                         "cosine": round(cosine(got, want), 6)})
            del sess
        except Exception as exc:  # a failing backend is a candidate result, not a probe failure
            rows.append({"backend": label, "providers": providers, "error": str(exc)[:120]})
    return rows


def template_matrix(points, template, size):
    import cv2

    dst = WARP_TEMPLATES[template] * np.asarray(size, np.float64)
    return cv2.estimateAffinePartial2D(np.asarray(points, np.float32), dst, method=cv2.RANSAC, ransacReprojThreshold=100)[0]


def warp_by_template(frame, points, template, size):
    import cv2

    matrix = template_matrix(points, template, size)
    return cv2.warpAffine(frame, matrix, size, borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA), matrix


def warp_by_box(frame, box, size):
    import cv2

    x1, y1, x2, y2 = box
    matrix = cv2.getAffineTransform(np.array([[x1, y1], [x2, y1], [x1, y2]], np.float32),
                                    np.array([[0, 0], [size[0], 0], [0, size[1]]], np.float32))
    flags = cv2.INTER_AREA if x2 - x1 > size[0] or y2 - y1 > size[1] else cv2.INTER_LINEAR
    return cv2.warpAffine(frame, matrix, size, flags=flags), matrix


def paste_back(frame, crop, mask, matrix):
    import cv2

    h, w = frame.shape[:2]
    ch, cw = crop.shape[:2]
    inverse = cv2.invertAffineTransform(matrix)
    corners = cv2.transform(np.array([[0, 0], [cw, 0], [cw, ch], [0, ch]]).reshape(-1, 1, 2), inverse).reshape(-1, 2)
    lo = np.clip(np.floor(corners.min(0)), 0, [w, h]).astype(int)
    hi = np.clip(np.ceil(corners.max(0)), 0, [w, h]).astype(int)
    x1, y1 = int(lo[0]), int(lo[1])
    size = (int(hi[0]) - x1, int(hi[1]) - y1)
    paste = inverse.copy()
    paste[0, 2] -= x1
    paste[1, 2] -= y1
    inv_mask = cv2.warpAffine(mask, paste, size).clip(0, 1)[:, :, None]
    inv_crop = cv2.warpAffine(crop, paste, size, borderMode=cv2.BORDER_REPLICATE)
    out = frame.copy()
    region = out[y1:y1 + size[1], x1:x1 + size[0]]
    out[y1:y1 + size[1], x1:x1 + size[0]] = (region * (1 - inv_mask) + inv_crop * inv_mask).astype(out.dtype)
    return out


def box_mask(crop, blur):
    import cv2

    h, w = crop.shape[:2]
    amount = int(w * 0.5 * blur)
    edge = max(amount // 2, 1)
    mask = np.ones((h, w), np.float32)
    mask[:edge] = mask[-edge:] = 0
    mask[:, :edge] = mask[:, -edge:] = 0
    return cv2.GaussianBlur(mask, (0, 0), amount * 0.25) if amount > 0 else mask


def area_mask(crop, landmarks, indices):
    import cv2

    hull = cv2.convexHull(landmarks[indices].astype(np.int32))
    mask = np.zeros(crop.shape[:2], np.float32)
    cv2.fillConvexPoly(mask, hull, 1.0)
    return (cv2.GaussianBlur(mask.clip(0, 1), (0, 0), 5).clip(0.5, 1) - 0.5) * 2


def landmarks68(landmarker, points):
    """fan_68_5 from the 5 detected points; 68 landmarks back in frame pixels (float32 cv2.transform, as the prototype)."""
    import cv2

    points = np.asarray(points, np.float32)
    norm = template_matrix(points, "ffhq_512", (1, 1))
    five = cv2.transform(points.reshape(1, -1, 2), norm).reshape(-1, 2)
    lm = landmarker.run(None, {landmarker.get_inputs()[0].name: five[None].astype(np.float32)})[0][0]
    return cv2.transform(lm.reshape(1, -1, 2), cv2.invertAffineTransform(norm)).reshape(-1, 2)


def face_crop(frame, points, landmarker):
    import cv2

    crop, matrix = warp_by_template(frame, points, "ffhq_512", (512, 512))
    return crop, matrix, cv2.transform(landmarks68(landmarker, points).reshape(1, -1, 2), matrix).reshape(-1, 2)


def detect_face(detector, frame):
    """YOLO face on a 640 canvas: 5 landmarks in frame pixels, or None."""
    import cv2

    h, w = frame.shape[:2]
    scale = min(1.0, 640 / max(h, w))
    small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
    canvas = np.zeros((640, 640, 3), np.float32)
    canvas[:small.shape[0], :small.shape[1]] = small
    raw = np.squeeze(detector.run(None, {detector.get_inputs()[0].name: canvas.transpose(2, 0, 1)[None] / 255.0})[0]).T
    best = raw[np.argmax(raw[:, 4])]
    if best[4] < 0.5:
        return None
    pts = best[5:].reshape(-1, 3)[:, :2].copy()
    pts[:, 0] *= w / small.shape[1]
    pts[:, 1] *= h / small.shape[0]
    return pts.astype(np.float32)


def frame_histogram(frame):
    import cv2

    small = cv2.resize(frame, (160, 90))
    return cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3).ravel()


def histogram_correlation(a, b):
    a, b = a - a.mean(), b - b.mean()
    return float(a @ b / (np.sqrt((a @ a) * (b @ b)) + 1e-12))


def is_scene_cut(prev, hist):
    return histogram_correlation(prev, hist) < 0.6


class FaceTracker:
    """One face per frame: YOLO every `every` frames, optical flow in between, re-detect on cuts, smoothed points."""

    def __init__(self, detector, every):
        self.detector, self.every = detector, every
        self.points = self.gray = self.hist = self.smooth = None

    def step(self, frame, index):
        import cv2

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hist = frame_histogram(frame)
        cut = self.hist is not None and is_scene_cut(self.hist, hist)
        self.hist = hist
        tracked = None
        if self.points is not None and self.gray is not None and index % self.every and not cut:
            tracked, status, _ = cv2.calcOpticalFlowPyrLK(self.gray, gray, self.points.reshape(-1, 1, 2), None,
                                                          winSize=(31, 31), maxLevel=3)
            if status.sum() < 5:
                tracked = None
        self.points = tracked.reshape(-1, 2) if tracked is not None else detect_face(self.detector, frame)
        self.gray = gray
        if self.points is None or cut:
            self.smooth = None
        if self.points is None:
            return None
        self.smooth = self.points if self.smooth is None else self.smooth * 0.4 + self.points * 0.6
        return self.smooth.astype(np.float32)


def read_image(path):
    import cv2

    image = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read image {path}")
    return image


def _quiet():
    """No console window per ffmpeg run in a Windows GUI build."""
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def encode_cmd(audio_in, out, width, height, fps, start):
    """Frames on stdin plus the audio; audio shorter than the video is padded, so ffmpeg reads every frame."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
           "-r", str(fps), "-i", "-"]
    if start:
        cmd += ["-ss", str(start)]
    return cmd + ["-i", audio_in, "-map", "0:v", "-map", "1:a?", "-af", "apad", "-c:v", "libx264", "-preset",
                  "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out]


def read_audio_48k(path):
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
                          capture_output=True, **_quiet())
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or f"ffmpeg could not read {path}")
    return proc.stdout


def resample(x, num):
    """scipy.signal.resample (FFT method) for a 1-D real signal."""
    n = len(x)
    spectrum = np.fft.rfft(x)
    keep = min(num, n)
    out = np.zeros(num // 2 + 1, complex)
    out[: keep // 2 + 1] = spectrum[: keep // 2 + 1]
    if keep % 2 == 0:
        out[keep // 2] *= 2.0 if num < n else (0.5 if n < num else 1.0)
    return np.fft.irfft(out, num) * (num / n)


def prepare_audio(x):
    """Peak-normalise, then the 0.97 pre-emphasis FaceFusion applies before the spectrogram."""
    peak = np.max(np.abs(x))
    x = x / peak if peak else x
    y = x.copy()
    y[1:] -= 0.97 * x[:-1]
    return y


def prepare_voice(pcm):
    """48 kHz stereo int16 (or mono) -> 16 kHz mono, as FaceFusion's voice path. Skipping the resample makes lips 3x slow."""
    mono = pcm.mean(axis=1) if pcm.ndim > 1 else pcm.astype(np.float64)
    mono = np.pad(mono, (0, max(0, 24000 - len(mono))))        # under 0.5 s: pad, so a short line still has windows
    return prepare_audio(resample(mono, round(len(mono) * 16000 / 48000)))


def triang(m):
    """Symmetric triangular window, scipy.signal.windows.triang."""
    if m <= 1:
        return np.ones(max(m, 0))
    n = np.arange(1, (m + 1) // 2 + 1)
    if m % 2 == 0:
        w = (2 * n - 1.0) / m
        return np.r_[w, w[::-1]]
    w = 2 * n / (m + 1.0)
    return np.r_[w, w[-2::-1]]


@functools.lru_cache(maxsize=None)
def mel_filter_bank():
    def to_mel(hz):
        return 2595 * np.log10(1 + hz / 700)

    def to_hz(mel):
        return 700 * (10 ** (mel / 2595) - 1)

    bank = np.zeros((80, 401))
    edges = np.linspace(to_mel(55.0), to_mel(7600.0), 82)
    idx = np.floor(801 * to_hz(edges) / 16000).astype(np.int64)
    for i in range(80):
        bank[i, idx[i]:idx[i + 1]] = triang(idx[i + 1] - idx[i])
    return bank


def create_spectrogram(audio):
    """80-band mel spectrogram of 16 kHz audio: 800-point Hann STFT, hop 200, zero boundaries, scipy's scaling."""
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(800) / 800)
    x = np.pad(np.asarray(audio, np.float64), 400)
    x = np.pad(x, (0, (-(len(x) - 800)) % 200))
    segments = np.lib.stride_tricks.sliding_window_view(x, 800)[::200]
    mag = np.concatenate([np.abs(np.fft.rfft(segments[i:i + 4096] * win, axis=1))
                          for i in range(0, len(segments), 4096)])
    return mel_filter_bank() @ (mag / win.sum()).T


def extract_audio_frames(spectrogram, fps):
    """Entry j is the 80 x 16 mel window that starts at mel step j * 80 / fps."""
    ends = np.arange(0, spectrogram.shape[1], 80 / fps).astype(np.int64)
    return [spectrogram[:, end - 16:end] for end in ends[ends >= 16]]


def mel_window(windows, index):
    return windows[min(max(0, index - SHIFT), len(windows) - 1)]


def mel_input(window):
    return np.clip(np.log10(np.maximum(1e-5, window)) * 1.6 + 3.2, -4, 4)[None, None].astype(np.float32)


def face_embedding(arcface, frame, points):
    crop, _ = warp_by_template(frame, points, "arcface_112_v2", (112, 112))
    x = (crop / 127.5 - 1)[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32)
    return arcface.run(None, {arcface.get_inputs()[0].name: x})[0].ravel()


def balance_embedding(source, target):
    """Hyperswap identity balance at weight 1.0: the source embedding minus 0.35 of the target's."""
    s = source / np.linalg.norm(source)
    t = target / np.linalg.norm(target)
    return (s * 1.35 - t * 0.35).reshape(1, -1).astype(np.float32)


class _LipEdtalk:
    """edtalk on every second frame; the frames between take the blend of their two neighbours."""

    def __init__(self, lip, landmarker, windows):
        self.lip, self.landmarker, self.windows = lip, landmarker, windows
        self.held = self.last = None

    def frame(self, frame, points, index):
        if points is None:
            return self.flush() + [frame]
        import cv2

        crop, matrix, landmarks = face_crop(frame, points, self.landmarker)
        mask = area_mask(crop, landmarks, LOWER_FACE)
        if index % 2:
            self.held = (frame, matrix, mask)
            return []
        out = self._model(crop, index)
        outs = []
        if self.held is not None:
            held_frame, held_matrix, held_mask = self.held
            mid = cv2.addWeighted(out if self.last is None else self.last, 0.5, out, 0.5, 0)
            outs.append(paste_back(held_frame, mid, held_mask, held_matrix))
            self.held = None
        self.last = out
        outs.append(paste_back(frame, out, mask, matrix))
        return outs

    def _model(self, crop, index):
        import cv2

        small = cv2.resize(crop, (256, 256), interpolation=cv2.INTER_AREA)[:, :, ::-1] / 255.0
        out = self.lip.run(None, {"source": mel_input(mel_window(self.windows, index)),
                                  "target": small.transpose(2, 0, 1)[None].astype(np.float32),
                                  "weight": np.ones(1, np.float32)})[0]
        out = (out[0].transpose(1, 2, 0).clip(0, 1) * 255).astype(np.uint8)[:, :, ::-1]
        return cv2.resize(out, (512, 512), interpolation=cv2.INTER_CUBIC)

    def flush(self):
        if self.held is None:
            return []
        held_frame, held_matrix, held_mask = self.held
        self.held = None
        if self.last is None:
            return [held_frame]
        return [paste_back(held_frame, self.last, held_mask, held_matrix)]


class _LipWav2lip:
    """wav2lip on a 96 px area with its lower half blanked; the mouth is pasted back over the lower face."""

    def __init__(self, lip, landmarker, windows):
        self.lip, self.landmarker, self.windows = lip, landmarker, windows

    def frame(self, frame, points, index):
        if points is None:
            return [frame]
        import cv2

        crop, matrix, landmarks = face_crop(frame, points, self.landmarker)
        mask = area_mask(crop, landmarks, LOWER_FACE)
        area, area_matrix = warp_by_box(crop, np.r_[landmarks.min(0), landmarks.max(0)], (96, 96))
        blank = area[None].copy()
        blank[:, 48:] = 0
        target = np.concatenate((blank, area[None]), axis=3).transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        out = self.lip.run(None, {"source": mel_input(mel_window(self.windows, index)), "target": target})[0]
        out = (out[0].transpose(1, 2, 0).clip(0, 1) * 255).astype(np.uint8)
        pasted = cv2.warpAffine(out, cv2.invertAffineTransform(area_matrix), (512, 512),
                                borderMode=cv2.BORDER_REPLICATE)
        return [paste_back(frame, pasted, mask, matrix)]

    def flush(self):
        return []


class _Swap:
    """hyperswap on every frame (skipping frames makes the swap pulse); the target identity is the first face seen."""

    def __init__(self, swapper, arcface, source):
        self.swapper, self.arcface, self.source, self.vec = swapper, arcface, source, None

    def frame(self, frame, points, index=0):
        if points is None:
            return [frame]
        if self.vec is None:
            self.vec = balance_embedding(self.source, face_embedding(self.arcface, frame, points))
        crop, matrix = warp_by_template(frame, points, "arcface_128", (256, 256))
        mask = box_mask(crop, 0.3)
        x = ((crop[:, :, ::-1] / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None].astype(np.float32)
        out = self.swapper.run(None, {"source": self.vec, "target": x})[0][0]
        out = (out.transpose(1, 2, 0).astype(np.float32) * 0.5 + 0.5).clip(0, 1)[:, :, ::-1] * 255
        return [paste_back(frame, out.astype(np.uint8), mask, matrix)]

    def flush(self):
        return []


def _video_fps(path):
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return fps


def _run_video(args, progress, tracker, mode, audio_in, label):
    import cv2

    cap = cv2.VideoCapture(args["video"])
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {args['video']}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start = float(args.get("start") or 0.0)
    first = int(round(start * fps))
    last = min(total, int(round(float(args["end"]) * fps))) if args.get("end") else total
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    os.makedirs(os.path.dirname(os.path.abspath(args["out"])), exist_ok=True)
    encoder = subprocess.Popen(encode_cmd(audio_in, args["out"], width, height, fps, start), stdin=subprocess.PIPE,
                               **_quiet())
    index, faced = first, 0
    try:
        while index < last:
            ok, frame = cap.read()
            if not ok:
                break
            points = tracker.step(frame, index)
            faced += points is not None
            for out in mode.frame(frame, points, index):
                encoder.stdin.write(out.tobytes())
            index += 1
            progress((index - first) / max(last - first, 1), f"{label}: {index - first}/{last - first} frames")
        for out in mode.flush():
            encoder.stdin.write(out.tobytes())
        encoder.stdin.close()
        if encoder.wait():
            raise RuntimeError(f"ffmpeg could not write {args['out']}")
    finally:
        cap.release()
        if encoder.poll() is None:
            encoder.kill()
    frames = index - first
    return {"out": args["out"], "frames": frames, "frames_with_face": faced, "seconds": frames / fps}


def cmd_prepare(args, progress):
    import onnxslim

    slim = model_path(args["home"], "edtalk")
    onnxslim.slim(os.path.join(args["home"], "models", "edtalk_256.onnx"), slim)
    return {"edtalk_slim": slim}


def cmd_probe(args, progress):
    names = args.get("models") or list(MODEL_FILES)
    candidates = candidate_providers(args["home"])
    rows, picks = {}, {}
    for i, key in enumerate(names):
        progress(i / len(names), f"probing {key}")
        rows[key] = probe_model(args["home"], key, candidates)
        picks[key] = pick_backend(rows[key], PROBE_THRESHOLD.get(key, 0.999))
    return {"picks": picks, "candidates": rows}


def cmd_lipsync(args, progress):
    home, picks, model = args["home"], _picks(args), args["model"]
    fps = _video_fps(args["video"])
    pcm = np.frombuffer(read_audio_48k(args["audio"]), np.int16).reshape(-1, 2)
    windows = extract_audio_frames(create_spectrogram(prepare_voice(pcm)), fps)
    detector = session(model_path(home, "yoloface"), _providers(picks, "yoloface"))
    landmarker = session(model_path(home, "fan68"), _providers(picks, "fan68"))
    lip = session(model_path(home, model), _providers(picks, model))
    mode = _LipEdtalk(lip, landmarker, windows) if model == "edtalk" else _LipWav2lip(lip, landmarker, windows)
    tracker = FaceTracker(detector, int(args.get("detect_every", 10)))
    return _run_video(args, progress, tracker, mode, args["audio"], "lip sync")


def _swap_setup(args):
    home, picks = args["home"], _picks(args)
    detector = session(model_path(home, "yoloface"), _providers(picks, "yoloface"))
    arcface = session(model_path(home, "arcface"), _providers(picks, "arcface"))
    swapper = session(model_path(home, "hyperswap"), _providers(picks, "hyperswap"))
    source = read_image(args["face_image"])
    points = detect_face(detector, source)
    if points is None:
        raise ValueError("no face found in the face image")
    return detector, _Swap(swapper, arcface, face_embedding(arcface, source, points))


def cmd_faceswap(args, progress):
    detector, mode = _swap_setup(args)
    tracker = FaceTracker(detector, int(args.get("detect_every", 10)))
    return _run_video(args, progress, tracker, mode, args["video"], "face swap")


def cmd_swap_frames(args, progress):
    import cv2

    detector, mode = _swap_setup(args)
    os.makedirs(args["out_dir"], exist_ok=True)
    images = []
    for i, path in enumerate(args["images"]):
        target = os.path.join(args["out_dir"], os.path.basename(path))
        frame = read_image(path)
        points = detect_face(detector, frame)
        if points is None:
            shutil.copyfile(path, target)
        else:
            ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", mode.frame(frame, points)[0])
            if not ok:
                raise ValueError(f"cannot write {target}")
            buf.tofile(target)
        images.append(target)
        progress((i + 1) / len(args["images"]), f"swapped {i + 1}/{len(args['images'])} frames")
    return {"images": images}


def _patch_ming_for_cpu(torch, spk_head, cast, dtype_of):
    """Ming's code autocasts on "cuda" only and feeds float speaker embeddings to a bf16 head; on a CPU that
    breaks. Point its autocast at the CPU and cast the head's input to the head's own dtype."""
    real_autocast = torch.autocast

    def autocast(device_type, *args, **kwargs):
        return real_autocast("cpu" if device_type == "cuda" else device_type, *args, **kwargs)

    torch.autocast = autocast
    real_forward = spk_head.forward
    spk_head.forward = lambda x: real_forward(cast(x, dtype_of(spk_head)))


def _ming_model(home):
    import torch

    sys.path.insert(0, os.path.join(home, "ming-code"))
    from ming_tts import MingTTS

    tts = MingTTS(os.path.join(home, "models", "ming"), device="cpu", text_normalization=False)
    _patch_ming_for_cpu(torch, tts.model.spk_head, lambda x, dtype: x.to(dtype),
                        lambda module: next(module.parameters()).dtype)
    return tts


def cmd_export_ming(args, progress):
    import onnxslim
    import torch
    from transformers.cache_utils import DynamicCache

    def rms_norm(x, normalized_shape, weight=None, eps=1e-6):
        x = x / torch.sqrt(x.pow(2).mean(-1, keepdim=True) + eps)
        return x * weight if weight is not None else x

    torch.nn.functional.rms_norm = rms_norm
    tts = _ming_model(args["home"])
    m = tts.model
    m.requires_grad_(False)

    def export(key, module, inputs, input_names, output_names, dynamic_axes):
        progress(0.0, f"exporting {key}")
        raw = os.path.join(args["home"], "models", "ming_onnx", key, key + ".onnx")  # one folder per part
        os.makedirs(os.path.dirname(raw), exist_ok=True)
        with torch.no_grad():
            torch.onnx.export(module, inputs, raw, input_names=input_names, output_names=output_names,
                              dynamic_axes=dynamic_axes, opset_version=17, dynamo=False)
        onnxslim.slim(raw, model_path(args["home"], key))

    backbone = m.model.float().eval()
    cfg = backbone.config
    layers, kv_heads = cfg.num_hidden_layers, cfg.num_key_value_heads
    head_dim = cfg.hidden_size // cfg.num_attention_heads

    class Step(torch.nn.Module):
        def forward(self, inputs_embeds, position_ids, *past):
            cache = DynamicCache.from_legacy_cache(tuple((past[2 * i], past[2 * i + 1]) for i in range(layers)))
            out = backbone(inputs_embeds=inputs_embeds, position_ids=position_ids, past_key_values=cache,
                           use_cache=True, output_hidden_states=True, return_dict=True)
            present = out.past_key_values.to_legacy_cache() if hasattr(out.past_key_values, "to_legacy_cache") \
                else out.past_key_values
            return (out.hidden_states[-1][:, -1:, :], *[t for kv in present for t in kv])

    past_len, new_len = 5, 3
    names_in = ["inputs_embeds", "position_ids"] + [f"past_{i}_{k}" for i in range(layers) for k in ("key", "value")]
    names_out = ["hidden"] + [f"present_{i}_{k}" for i in range(layers) for k in ("key", "value")]
    dynamic = {"inputs_embeds": {1: "seq"}, "position_ids": {1: "seq"}}
    dynamic.update({n: {2: "past"} for n in names_in[2:]})
    dynamic.update({n: {2: "total"} for n in names_out[1:]})
    inputs = (torch.randn(1, new_len, cfg.hidden_size), torch.arange(past_len, past_len + new_len)[None],
              *[torch.randn(1, kv_heads, past_len, head_dim) for _ in range(2 * layers)])
    export("ming_backbone", Step().eval(), inputs, names_in, names_out, dynamic)

    dit = m.flowloss.cfm.model
    calls = []
    real = dit.forward_with_cfg

    def spy(x, t, c, latent_history, patch_size):
        if not calls:
            calls.append((x.clone(), t.clone(), c.clone(), latent_history.clone(), patch_size))
            raise KeyboardInterrupt  # first flow call: keep its real inputs, stop the synthesis
        return real(x, t, c, latent_history, patch_size)

    dit.forward_with_cfg = spy
    try:
        tts.synthesize(task="TTS", language_id="English", text="Hello there.", use_zero_spk_emb=True,
                       output_path=os.path.join(args["home"], "models", "ming_onnx", "capture.wav"))
    except KeyboardInterrupt:
        pass
    dit.forward_with_cfg = real
    if not calls:
        raise RuntimeError("Ming did not reach the flow head")
    x, t, c, hist, patch = calls[0]

    class Wrap(torch.nn.Module):
        def __init__(self, inner, patch_size):
            super().__init__()
            self.inner, self.patch = inner, patch_size

        def forward(self, x, t, c, latent_history):
            return self.inner.forward_with_cfg(x, t, c, latent_history, self.patch)

    flow_inputs = tuple(v.float() for v in (x, t.reshape(1), c, hist))
    export("ming_flow", Wrap(dit.float(), patch).eval(), flow_inputs, ["x", "t", "c", "latent_history"], ["v"], None)

    dec = m.audio.decoder.float().eval()

    class Dec(torch.nn.Module):
        def forward(self, latent):
            h = dec.fc1(latent)
            if dec.patch_size != -1:
                h = dec.upsampling.upsampler(h.transpose(1, 2)).transpose(1, 2)
            h = dec.decoder(inputs_embeds=h).last_hidden_state
            return dec.head.out(h)

    export("ming_decoder", Dec().eval(), (torch.randn(1, 61, dec.latent_dim),), ["latent"], ["spec"],
           {"latent": {1: "frames"}, "spec": {1: "steps"}})
    return {"paths": {key: model_path(args["home"], key) for key in ("ming_backbone", "ming_flow", "ming_decoder")}}


def cmd_tts(args, progress):
    import torch

    home, picks = args["home"], _picks(args)
    progress(0.0, "loading Ming")
    tts = _ming_model(home)
    m = tts.model
    m.requires_grad_(False)

    backbone = session(model_path(home, "ming_backbone"), _providers(picks, "ming_backbone"))
    past_names = [i.name for i in backbone.get_inputs()][2:]
    m.model.model.layers = torch.nn.ModuleList()  # keep embed_tokens for prompt building, drop the 24 torch layers
    m.stop_head.float()
    m.linear_proj_audio.float()

    def step(inputs_embeds=None, position_ids=None, past_key_values=None, **_):
        past = past_key_values or [np.zeros((1, 2, 0, 64), np.float32)] * len(past_names)
        feed = {"inputs_embeds": inputs_embeds.float().numpy(), "position_ids": position_ids.long().numpy(),
                **dict(zip(past_names, past))}
        out = backbone.run(None, feed)
        return SimpleNamespace(past_key_values=out[1:], hidden_states=[torch.from_numpy(out[0])])

    m.model.forward = step

    m.flowloss.float()
    dit = m.flowloss.cfm.model
    flow = session(model_path(home, "ming_flow"), _providers(picks, "ming_flow"))

    def forward_with_cfg(x, t, c, latent_history, patch_size):
        tt = t.reshape(1).float() if t.ndim == 0 else t.float()
        out = flow.run(None, {"x": x.float().numpy(), "t": tt.numpy(), "c": c.float().numpy(),
                              "latent_history": latent_history.float().numpy()})[0]
        return torch.from_numpy(out)

    dit.forward_with_cfg = forward_with_cfg
    for child in list(dit._modules):
        dit._modules[child] = None  # the flow weights now live in ONNX
    real_sample = m.flowloss.sample

    def sample(z, hist, *a, **k):
        with torch.autocast("cpu", enabled=False):
            return real_sample(z.float(), hist.float(), *a, **k).to(z.dtype)

    m.flowloss.sample = sample

    dec = m.audio.decoder
    dec.decoder = None  # the decoder weights now live in ONNX; keep fc and the ISTFT head
    dec.head.float()
    decoder = session(model_path(home, "ming_decoder"), _providers(picks, "ming_decoder"))

    def reconstruct(latent, past_key_values=None, use_cache=False, stream_state=(None, None, None), last_chunk=False):
        spec = torch.from_numpy(decoder.run(None, {"latent": latent.float().numpy()})[0]).transpose(1, 2)
        mag, phase = spec.chunk(2, dim=1)
        mag = torch.clip(torch.exp(mag), max=1e2)
        wav = dec.head.istft(mag * (torch.cos(phase) + 1j * torch.sin(phase)), audio_buffer=None, window_buffer=None,
                             streaming=False, last_chunk=True)[0].unsqueeze(1)
        return wav, stream_state, past_key_values

    dec.low_level_reconstruct = reconstruct

    kwargs = dict(task=args.get("task", "TTS"), language_id="English", text=args["text"], output_path=args["out"],
                  max_decode_steps=min(int(args.get("max_decode_steps", 300)), 300))
    if args.get("instruction"):
        kwargs["instruction"] = args["instruction"]
    if args.get("prompt_wav"):  # zero-shot clone: prompt audio plus its transcript
        kwargs.update(prompt_wav_path=args["prompt_wav"], prompt_text=args["prompt_text"], use_spk_emb=True)
    else:
        kwargs["use_zero_spk_emb"] = True
    progress(0.1, "synthesizing speech")
    tts.synthesize(**kwargs)
    import soundfile

    return {"out": args["out"], "seconds": float(soundfile.info(args["out"]).duration)}


COMMANDS = {
    "prepare": cmd_prepare,
    "export-ming": cmd_export_ming,
    "probe": cmd_probe,
    "lipsync": cmd_lipsync,
    "faceswap": cmd_faceswap,
    "swap-frames": cmd_swap_frames,
    "tts": cmd_tts,
}


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]  # the app's src/classes must not shadow packages
    sys.exit(main(sys.argv[1:]))
