"""Watch-window frame extract: candidate [hit-2s, hit+2s] JPEGs for vision confirm.

Never watches a whole file. Temp JPEGs are deleted by the caller after confirm.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from classes.ffmpeg_cli import run_ffmpeg

WATCH_PAD_SEC = 2.0
MAX_FRAMES = 12
DENSE_WINDOW_SEC = 16.0
DEFAULT_LONG_EDGE = 512
TEXT_LONG_EDGE = 1024
_TEXT_QUERY_RE = re.compile(
    r"\b(ui|slide|slides|on[- ]screen|onscreen|text|terminal|screenshot|"
    r"caption|title card|lower[- ]third|ocr|menu|hud)\b",
    re.IGNORECASE,
)


def padded_window(
    start: float,
    end: float,
    duration: float = 0.0,
    pad: float = WATCH_PAD_SEC,
) -> Tuple[float, float]:
    s = max(0.0, float(start) - float(pad))
    e = float(end) + float(pad)
    if duration and duration > 0:
        e = min(e, float(duration))
    if e <= s:
        e = s + 0.04
        if duration and duration > 0:
            e = min(e, float(duration))
    return s, e


def is_onscreen_text_query(query: str) -> bool:
    return bool(_TEXT_QUERY_RE.search(query or ""))


def _cue_times(cues: Optional[Iterable[Any]], win_start: float, win_end: float) -> List[float]:
    out: List[float] = []
    for cue in cues or []:
        t = None
        if isinstance(cue, dict):
            try:
                t = float(cue.get("start") if cue.get("start") is not None else cue.get("time"))
            except (TypeError, ValueError):
                t = None
        else:
            try:
                t = float(cue)
            except (TypeError, ValueError):
                t = None
        if t is None:
            continue
        if win_start - 1e-3 <= t <= win_end + 1e-3:
            out.append(max(win_start, min(t, win_end)))
    return out


def plan_sample_times(
    win_start: float,
    win_end: float,
    cues: Optional[Iterable[Any]] = None,
    *,
    max_frames: int = MAX_FRAMES,
    dense_window_sec: float = DENSE_WINDOW_SEC,
    extra_times: Optional[Sequence[float]] = None,
) -> Tuple[List[float], str, bool]:
    """Return (timestamps, warning, sparse)."""
    span = max(0.0, float(win_end) - float(win_start))
    sparse = span > float(dense_window_sec)
    warning = ""
    if sparse:
        warning = (
            f"Window is {span:.1f}s; sparse-scanned (not a dense watch). "
            "Narrow the candidate window if the cut looks off."
        )
        n = max(2, int(max_frames))
        step = span / max(n - 1, 1)
        times = [float(win_start) + i * step for i in range(n)]
    else:
        step = 0.5
        times = []
        t = float(win_start)
        while t <= float(win_end) + 1e-6:
            times.append(t)
            t += step
        times.append(float(win_end))

    must = _cue_times(cues, win_start, win_end)
    times.extend(must)
    for extra in extra_times or []:
        try:
            et = float(extra)
        except (TypeError, ValueError):
            continue
        if win_start - 1e-3 <= et <= win_end + 1e-3:
            times.append(et)

    rounded: List[float] = []
    seen = set()
    for t in sorted(times):
        key = round(t, 2)
        if key in seen:
            continue
        seen.add(key)
        rounded.append(float(t))
    return rounded, warning, sparse


def hamming_near_dup(a: bytes, b: bytes, *, min_similarity: float = 0.92) -> bool:
    if not a or not b or len(a) != len(b):
        return a == b and bool(a)
    diffs = sum(abs(x - y) for x, y in zip(a, b))
    sim = 1.0 - (diffs / (255.0 * len(a)))
    return sim >= min_similarity


def dedupe_frame_records(
    records: List[Dict[str, Any]],
    *,
    max_frames: int = MAX_FRAMES,
) -> List[Dict[str, Any]]:
    """Keep unique frames; always keep must_keep (cue-forced) when possible."""
    kept: List[Dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: float(r.get("timestamp") or 0)):
        fp = rec.get("fingerprint") or b""
        must = bool(rec.get("must_keep"))
        dup = False
        if fp:
            for prev in kept:
                prev_fp = prev.get("fingerprint") or b""
                if prev_fp and hamming_near_dup(fp, prev_fp):
                    dup = True
                    break
        if dup and not must:
            continue
        kept.append(rec)

    if len(kept) <= max_frames:
        return kept
    musts = [r for r in kept if r.get("must_keep")]
    others = [r for r in kept if not r.get("must_keep")]
    budget = max(0, int(max_frames) - len(musts))
    if budget <= 0:
        return musts[:max_frames]
    if len(others) <= budget:
        picked = others
    else:
        if budget == 1:
            picked = [others[len(others) // 2]]
        else:
            step = (len(others) - 1) / max(budget - 1, 1)
            idxs = sorted({int(round(i * step)) for i in range(budget)})
            picked = [others[i] for i in idxs]
    merged = musts + picked
    merged.sort(key=lambda r: float(r.get("timestamp") or 0))
    return merged[:max_frames]


def _probe_duration(path: str) -> float:
    try:
        p = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, check=False,
        )
        val = (p.stdout or "").strip()
        if val and val != "N/A":
            return float(val)
    except Exception:
        pass
    return 0.0


def _extract_one_jpeg(path: str, timestamp: float, dest: str, long_edge: int) -> bool:
    vf = f"scale={int(long_edge)}:{int(long_edge)}:force_original_aspect_ratio=decrease"
    try:
        p = run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{float(timestamp):.3f}", "-i", path,
                "-frames:v", "1", "-vf", vf, "-q:v", "3", "-y", dest,
            ],
            capture_output=True, check=False,
        )
        return p.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 32
    except Exception:
        return False


def _fingerprint_jpeg(path: str) -> bytes:
    try:
        p = run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", path, "-vf", "scale=16:16", "-f", "rawvideo",
                "-pix_fmt", "gray", "pipe:1",
            ],
            capture_output=True, check=False,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).digest()
    except Exception:
        return b""


def _scene_times(path: str, win_start: float, win_end: float) -> List[float]:
    span = max(0.04, float(win_end) - float(win_start))
    try:
        p = run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{float(win_start):.3f}", "-i", path,
                "-t", f"{span:.3f}",
                "-vf", "select='gt(scene,0.25)',showinfo",
                "-an", "-f", "null", "-",
            ],
            capture_output=True, text=True, check=False,
        )
        blob = (p.stderr or "") + (p.stdout or "")
    except Exception:
        return []
    times: List[float] = []
    for m in re.finditer(r"pts_time:([0-9.]+)", blob):
        try:
            local = float(m.group(1))
        except (TypeError, ValueError):
            continue
        abs_t = float(win_start) + local
        if win_start - 1e-3 <= abs_t <= win_end + 1e-3:
            times.append(abs_t)
    return times


def extract_watch_window(
    source_path: str,
    start: float,
    end: float,
    *,
    query: str = "",
    duration: float = 0.0,
    transcript_cues: Optional[Iterable[Any]] = None,
    max_frames: int = MAX_FRAMES,
    work_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract a small JPEG set for [start-2, end+2]. Caller deletes temp files."""
    path = os.path.abspath(source_path or "")
    if not path or not os.path.isfile(path):
        return {
            "ok": False,
            "error": f"Source file not found: {source_path}",
            "frames": [],
            "window_start": float(start or 0),
            "window_end": float(end or 0),
            "warning": "",
            "sparse": False,
        }
    dur = float(duration or 0)
    if dur <= 0:
        dur = _probe_duration(path)
    win_start, win_end = padded_window(start, end, dur)
    long_edge = TEXT_LONG_EDGE if is_onscreen_text_query(query) else DEFAULT_LONG_EDGE
    scene_ts = _scene_times(path, win_start, win_end)
    span = max(0.0, float(win_end) - float(win_start))
    sparse_pre = span > float(DENSE_WINDOW_SEC)
    times, warning, sparse = plan_sample_times(
        win_start, win_end, transcript_cues,
        max_frames=max_frames if sparse_pre else max(max_frames * 3, 24),
        extra_times=scene_ts,
    )
    cue_set = {round(t, 2) for t in _cue_times(transcript_cues, win_start, win_end)}
    tmp = work_dir or tempfile.mkdtemp(prefix="zenvi_watch_")
    os.makedirs(tmp, exist_ok=True)
    records: List[Dict[str, Any]] = []
    for i, ts in enumerate(times):
        dest = os.path.join(tmp, f"f{i:03d}_{ts:.3f}.jpg")
        if not _extract_one_jpeg(path, ts, dest, long_edge):
            continue
        records.append({
            "timestamp": float(ts),
            "path": dest,
            "fingerprint": _fingerprint_jpeg(dest),
            "must_keep": round(ts, 2) in cue_set,
        })
    kept = dedupe_frame_records(records, max_frames=max_frames)
    kept_paths = {r["path"] for r in kept}
    for rec in records:
        if rec["path"] not in kept_paths:
            try:
                os.remove(rec["path"])
            except OSError:
                pass
    frames = [{"timestamp": r["timestamp"], "path": r["path"]} for r in kept]
    return {
        "ok": True,
        "error": "",
        "frames": frames,
        "window_start": win_start,
        "window_end": win_end,
        "warning": warning,
        "sparse": sparse,
        "work_dir": tmp,
        "long_edge": long_edge,
    }


def cleanup_watch_files(extract_result: Optional[Dict[str, Any]]) -> None:
    if not extract_result:
        return
    for fr in extract_result.get("frames") or []:
        p = str(fr.get("path") or "")
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    work = str(extract_result.get("work_dir") or "")
    if work and os.path.isdir(work):
        try:
            os.rmdir(work)
        except OSError:
            pass


def frames_to_payload(frames: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    import base64

    out: List[Dict[str, Any]] = []
    for fr in frames:
        p = str(fr.get("path") or "")
        if not p or not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        out.append({
            "timestamp": float(fr.get("timestamp") or 0),
            "image_base64": b64,
        })
    return out


def confirm_watch_window(
    *,
    source_path: str,
    start: float,
    end: float,
    query: str,
    duration: float = 0.0,
    transcript_cues: Optional[Iterable[Any]] = None,
    fallback_cut: Optional[float] = None,
    fallback_in: Optional[float] = None,
    fallback_out: Optional[float] = None,
) -> Dict[str, Any]:
    """Extract JPEGs, POST to backend vision confirm, delete temps."""
    extracted = extract_watch_window(
        source_path, start, end,
        query=query, duration=duration, transcript_cues=transcript_cues,
    )
    fallback = float(fallback_cut if fallback_cut is not None else start)
    try:
        fb_in = float(fallback_in if fallback_in is not None else start)
    except (TypeError, ValueError):
        fb_in = float(start)
    try:
        fb_out = float(fallback_out if fallback_out is not None else end)
    except (TypeError, ValueError):
        fb_out = float(end)
    win_s = float(extracted.get("window_start") or start)
    win_e = float(extracted.get("window_end") or end)
    warning = str(extracted.get("warning") or "")

    def _soft(reason):
        return {
            "cut_source": fallback,
            "in_source": fb_in,
            "out_source": fb_out,
            "matched": False,
            "used_fallback": True,
            "confidence": 0.0,
            "reason": reason,
            "warning": warning,
            "window_start": win_s,
            "window_end": win_e,
        }

    if not extracted.get("ok") or not extracted.get("frames"):
        cleanup_watch_files(extracted)
        return _soft(extracted.get("error") or "No watch frames extracted")
    try:
        from classes.api_client import get_backend_client

        client = get_backend_client()
        payload_frames = frames_to_payload(extracted["frames"])
        data = client.watch_window(
            query=query,
            window_start=win_s,
            window_end=win_e,
            frames=payload_frames,
            fallback_cut=fallback,
            fallback_in=fb_in,
            fallback_out=fb_out,
        )
    except Exception as exc:  # noqa: BLE001
        data = {"error": str(exc)}
    finally:
        cleanup_watch_files(extracted)

    if not isinstance(data, dict) or data.get("error"):
        return _soft(str((data or {}).get("error") or "watch-window failed"))
    cut = data.get("cut_source")
    try:
        cut_f = float(cut)
    except (TypeError, ValueError):
        cut_f = fallback
    cut_f = max(win_s, min(cut_f, win_e))
    matched = bool(data.get("matched"))
    used_fallback = bool(data.get("used_fallback")) or (not matched)

    def _opt(key, default):
        try:
            if data.get(key) is None:
                return default
            return max(win_s, min(float(data.get(key)), win_e))
        except (TypeError, ValueError):
            return default

    in_s = _opt("in_source", fb_in)
    out_s = _opt("out_source", fb_out)
    if used_fallback and not matched:
        cut_f = max(win_s, min(fallback, win_e))
        in_s, out_s = fb_in, fb_out
    if out_s <= in_s:
        in_s, out_s = win_s, win_e
    extra_warn = str(data.get("warning") or "")
    if extra_warn and extra_warn not in warning:
        warning = (warning + " " + extra_warn).strip()
    return {
        "cut_source": cut_f,
        "in_source": in_s,
        "out_source": out_s,
        "matched": matched,
        "used_fallback": used_fallback,
        "confidence": float(data.get("confidence") or 0),
        "reason": str(data.get("reason") or ""),
        "warning": warning,
        "window_start": win_s,
        "window_end": win_e,
    }
