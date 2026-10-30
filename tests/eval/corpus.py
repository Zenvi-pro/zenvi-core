"""A deterministic corpus with known answers, built with ffmpeg (and numpy for sound). No network, no licensing.

Every builder returns the path and the truth: where the cuts are, where the voice is, where the beats and sections are.
Files are cached in ``EVAL_CACHE`` (default: a folder beside the pytest temp root) so a run builds each clip once.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

SIZE = "640x360"
FPS = 25
SR = 32000


def ffmpeg() -> str:
    folder = os.environ.get("FFMPEG_BIN_DIR")
    for candidate in ((os.path.join(folder, "ffmpeg"),) if folder else ()) + (shutil.which("ffmpeg") or "",):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError("ffmpeg is needed for the eval corpus (set FFMPEG_BIN_DIR)")


def cache_dir() -> Path:
    root = Path(os.environ.get("EVAL_CACHE") or Path(tempfile.gettempdir()) / "zenvi_eval_corpus")
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(*args: str) -> None:
    proc = subprocess.run([ffmpeg(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-500:])


def cached(name: str, build) -> Path:
    path = cache_dir() / name
    if not path.exists() or path.stat().st_size == 0:
        tmp = path.with_name("tmp_" + path.name)
        build(str(tmp))
        os.replace(tmp, path)
    return path


# ============================ sources ============================
SOURCES = {
    "testsrc2": f"testsrc2=size={SIZE}:rate={FPS}",
    "bars": f"smptebars=size={SIZE}:rate={FPS}",
    "mandel": f"mandelbrot=size={SIZE}:rate={FPS}",
    "gradient": f"gradients=size={SIZE}:rate={FPS}:seed=7:speed=0.01",
    "rgb": f"rgbtestsrc=size={SIZE}:rate={FPS}",
    "testsrc": f"testsrc=size={SIZE}:rate={FPS}",
}


def segment(source: str, seconds: float, out: str, vf: str = "") -> None:
    filt = ",".join(x for x in (vf, "format=yuv420p") if x)
    run("-f", "lavfi", "-i", SOURCES[source], "-t", f"{seconds}", "-vf", filt, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-g", "25", out)


def concat(parts: Sequence[str], out: str) -> None:
    listing = out + ".txt"
    with open(listing, "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    run("-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", out)
    os.remove(listing)


# ============================ shots: known cuts ============================
def hard_cuts(seconds: float = 4.0, kinds: Sequence[str] = ("testsrc2", "bars", "mandel", "gradient", "rgb", "testsrc")) -> Tuple[Path, Dict[str, Any]]:
    def build(out):
        parts = []
        for i, k in enumerate(kinds):
            p = out + f".{i}.mp4"
            segment(k, seconds, p)
            parts.append(p)
        concat(parts, out)
        for p in parts:
            os.remove(p)
    path = cached(f"hard_{seconds}_{len(kinds)}.mp4", build)
    return path, {"hard": [round(seconds * i, 3) for i in range(1, len(kinds))], "soft": [], "none": 0}


def near_identical(seconds: float = 4.0) -> Tuple[Path, Dict[str, Any]]:
    """The same quiet scene with a small brightness step: a cut a person sees, with little pixel change."""
    def build(out):
        a, b = out + ".a.mp4", out + ".b.mp4"
        segment("gradient", seconds, a)
        segment("gradient", seconds, b, "eq=brightness=0.06:saturation=1.1")
        concat([a, b], out)
        os.remove(a)
        os.remove(b)
    path = cached(f"near_{seconds}.mp4", build)
    return path, {"hard": [seconds], "soft": [], "none": 0}


def dissolves(seconds: float = 4.0, fade: float = 1.0, kinds: Sequence[str] = ("testsrc2", "mandel", "bars", "rgb")) -> Tuple[Path, Dict[str, Any]]:
    def build(out):
        parts = []
        for i, k in enumerate(kinds):
            p = out + f".{i}.mp4"
            segment(k, seconds + fade, p)
            parts.append(p)
        args, label, offset = [], "[0:v]", 0.0
        filters = []
        for i in range(1, len(parts)):
            offset += seconds
            filters.append(f"{label}[{i}:v]xfade=transition=fade:duration={fade}:offset={offset}[v{i}]")
            label = f"[v{i}]"
        for p in parts:
            args += ["-i", p]
        run(*args, "-filter_complex", ";".join(filters), "-map", label, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", out)
        for p in parts:
            os.remove(p)
    path = cached(f"dissolve_{seconds}_{fade}_{len(kinds)}.mp4", build)
    return path, {"hard": [], "soft": [round(seconds * i + fade / 2, 3) for i in range(1, len(kinds))], "none": 0}


def fades_through_black(seconds: float = 4.0, kinds: Sequence[str] = ("testsrc2", "mandel", "bars")) -> Tuple[Path, Dict[str, Any]]:
    def build(out):
        parts = []
        for i, k in enumerate(kinds):
            p = out + f".{i}.mp4"
            vf = []
            if i > 0:
                vf.append("fade=t=in:st=0:d=0.5")
            if i < len(kinds) - 1:
                vf.append(f"fade=t=out:st={seconds - 0.5}:d=0.5")
            segment(k, seconds, p, ",".join(vf))
            parts.append(p)
        concat(parts, out)
        for p in parts:
            os.remove(p)
    path = cached(f"fade_{seconds}_{len(kinds)}.mp4", build)
    return path, {"hard": [], "soft": [round(seconds * i, 3) for i in range(1, len(kinds))], "none": 0}


def no_cuts_busy(seconds: float = 12.0) -> Tuple[Path, Dict[str, Any]]:
    """One continuous, fast-moving scene: every cut reported here is false."""
    path = cached(f"busy_{seconds}.mp4", lambda out: segment("mandel", seconds, out))
    return path, {"hard": [], "soft": [], "none": 1}


def flash_inside(seconds: float = 10.0) -> Tuple[Path, Dict[str, Any]]:
    """One continuous scene with a two-frame white flash: a flash is not a cut."""
    path = cached(f"flash_{seconds}.mp4", lambda out: segment("testsrc2", seconds, out, "eq=brightness=0.9:enable='between(n,100,101)'"))
    return path, {"hard": [], "soft": [], "none": 1}


# ============================ sound ============================
def write_wav(path: str, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SR)
        fh.writeframes((pcm * 32767).astype("<i2").tobytes())


def click_track(bpm: float = 120.0, seconds: float = 48.0, sections: Sequence[Tuple[float, float, float]] = ((0.0, 12.0, 0.15), (12.0, 36.0, 0.7), (36.0, 48.0, 0.15))
                ) -> Tuple[Path, Dict[str, Any]]:
    """A beat with a tone bed. *sections* are (start, end, loudness); the truth is the beats and the section edges."""
    key = hashlib.sha1(json.dumps([bpm, seconds, sections]).encode()).hexdigest()[:10]

    def build(out):
        n = int(seconds * SR)
        t = np.arange(n) / SR
        audio = np.zeros(n, np.float32)
        step = 60.0 / bpm
        beats = np.arange(0.5, seconds - 0.2, step)
        for b in beats:
            i = int(b * SR)
            length = int(0.06 * SR)
            env = np.exp(-np.arange(length) / (0.012 * SR))
            level = next((lv for a, z, lv in sections if a <= b < z), sections[-1][2])
            audio[i:i + length] += (np.sin(2 * np.pi * 900 * np.arange(length) / SR) * env * level).astype(np.float32)[: n - i]
        for a, z, lv in sections:
            lo, hi = int(a * SR), min(n, int(z * SR))
            audio[lo:hi] += (0.25 * lv * np.sin(2 * np.pi * 220 * t[lo:hi])).astype(np.float32)
        write_wav(out, audio * 0.8)

    path = cached(f"click_{key}.wav", build)
    return path, {"bpm": bpm, "beats": [round(float(b), 3) for b in np.arange(0.5, seconds - 0.2, 60.0 / bpm)],
                  "section_edges": [a for a, _, _ in sections[1:]], "seconds": seconds}


def voice_bursts(spans: Sequence[Tuple[float, float]] = ((1.0, 3.0), (4.5, 6.0), (7.5, 10.0)), seconds: float = 11.0) -> Tuple[Path, Dict[str, Any]]:
    """Speech-like bursts (a harmonic tone with a syllable rhythm) in silence: known voice starts and ends."""
    key = hashlib.sha1(json.dumps([spans, seconds]).encode()).hexdigest()[:10]

    def build(out):
        n = int(seconds * SR)
        t = np.arange(n) / SR
        audio = np.zeros(n, np.float32)
        rng = np.random.default_rng(3)
        for a, z in spans:
            lo, hi = int(a * SR), int(z * SR)
            tt = t[lo:hi]
            f0 = 140 + 25 * np.sin(2 * np.pi * 0.7 * tt)
            phase = 2 * np.pi * np.cumsum(f0) / SR
            voiced = sum(np.sin(h * phase) / h for h in range(1, 8))
            syll = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * tt) ** 2
            edge = np.minimum(1.0, np.minimum(tt - tt[0], tt[-1] - tt) / 0.03)
            audio[lo:hi] = (0.35 * voiced * syll * edge + 0.01 * rng.standard_normal(hi - lo)).astype(np.float32)
        audio += (0.002 * rng.standard_normal(n)).astype(np.float32)
        write_wav(out, audio)

    path = cached(f"voice_{key}.wav", build)
    return path, {"spans": [list(s) for s in spans], "seconds": seconds}


def song(bpm: float = 100.0, seconds: float = 60.0, sections: Sequence[Dict[str, Any]] = (), seed: int = 5) -> Tuple[Path, Dict[str, Any]]:
    """A harder synthetic song. Each section dict: start, end, level (0..1, can be a (from, to) ramp), bed (Hz), click (Hz), hat (bool).

    Sections can differ in loudness, in which instruments play (a low bed, a bright click, a hi-hat), or both, and a level can
    ramp so a build has no sharp edge. The truth is the section edges and the beats.
    """
    key = hashlib.sha1(json.dumps([bpm, seconds, list(sections), seed], default=str).encode()).hexdigest()[:10]

    def build(out):
        n = int(seconds * SR)
        t = np.arange(n) / SR
        rng = np.random.default_rng(seed)
        audio = np.zeros(n, np.float32)
        step = 60.0 / bpm
        for sec in sections:
            lo, hi = int(sec["start"] * SR), min(n, int(sec["end"] * SR))
            level = sec.get("level", 0.5)
            ramp = np.linspace(level[0], level[1], hi - lo) if isinstance(level, (tuple, list)) else np.full(hi - lo, float(level))
            audio[lo:hi] += (0.3 * ramp * np.sin(2 * np.pi * sec.get("bed", 220) * t[lo:hi])).astype(np.float32)
            for b in np.arange(sec["start"] + 0.25, sec["end"] - 0.1, step):
                i = int(b * SR)
                length = int(0.05 * SR)
                gain = float(ramp[min(len(ramp) - 1, max(0, i - lo))])
                env = np.exp(-np.arange(length) / (0.01 * SR))
                audio[i:i + length] += (np.sin(2 * np.pi * sec.get("click", 900) * np.arange(length) / SR) * env * gain).astype(np.float32)[: n - i]
                if sec.get("hat"):
                    j = int((b + step / 2) * SR)
                    hl = int(0.02 * SR)
                    audio[j:j + hl] += (rng.standard_normal(hl) * np.exp(-np.arange(hl) / (0.004 * SR)) * 0.35 * gain).astype(np.float32)[: max(0, n - j)]
        write_wav(out, audio * 0.8)

    path = cached(f"song_{key}.wav", build)
    return path, {"bpm": bpm, "seconds": seconds, "section_edges": [s["start"] for s in sections[1:]],
                  "beats": [round(float(b), 3) for sec in sections for b in np.arange(sec["start"] + 0.25, sec["end"] - 0.1, 60.0 / bpm)]}
