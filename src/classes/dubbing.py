"""Dubbing: lip sync, face swap and speech, on the cloud (Grok, credits) or on this computer.

Workstream: ai-generation. This module holds the rules and the orchestration: tag cleanup, the engine
choice, chunk planning, the cloud round trips and the local worker calls. The editor tools
(editor_tools/ai_generation_lipsync.py, ai_generation_tts.py) own consent, the install prompt,
placement and undo. Local work runs in the downloaded worker env through classes.dubbing_runtime.
Nothing here imports Qt or an ML package.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

SUPPORTED_TAGS = ("[Breathing]", "[Cough]", "[Sigh]", "[Laughter]", "[Uhm]", "[Surprise-ah]", "[Surprise-oh]",
                  "[Dissatisfaction-hnn]", "[Confirmation-en]", "[Question-ei]", "[Question-ah]", "[Question-en]",
                  "[Question-oh]", "[Question-huh]")
_TAG_ALIASES = {"chuckle": "Laughter", "laugh": "Laughter", "laughs": "Laughter", "sighs": "Sigh"}
_EXACT = {tag[1:-1].lower(): tag[1:-1] for tag in SUPPORTED_TAGS}
_TAG = re.compile(r"\[([^\[\]]*)\]")
_LEADING_TAG = re.compile(r"\s*(\[[^\[\]]*\])\s*")
_SENTENCE_GAP = re.compile(r"(?<=[.!?…])\s+")
_ENDS_SENTENCE = re.compile(r"[.!?…][\"'”’)\]]*$")
PAUSE_S = 0.3
MAX_S = 15            # Grok takes 1-15 s per request
TARGET_S = 8
WORD_ERROR_MAX = 0.25
MING_STEPS = 300
DEFAULT_VOICE = "leo"   # a Grok preset; the backend needs a voice for every dialogue request
MAX_JOB_S = 600         # one dub at most 10 minutes of clip
MAX_SCRIPT_CHARS = 4096
_LABELS = {"face": "Face models and the Python runtime for them (one time)",
           "ming": "Ming speech model, about 3 GB (one time)"}
_PROMPT_DIALOGUE = "The speaker says the dialogue with natural mouth movement; keep the framing and the light."
_PROMPT_SWAP = "Keep the motion, the camera and the light of the reference frames; the speaker is the reference face."
_JOB_LOCK = threading.Lock()   # one dubbing job at a time


class DubbingError(Exception):
    """A dubbing job that cannot finish; the message is for the person."""


class _Refused(Exception):
    """One cloud attempt that produced no usable video (retried once, then this computer runs the chunk)."""

    def __init__(self, message: str, credits: bool = False):
        super().__init__(message)
        self.credits = credits


# ---------------------------------------------------------------------------
# Tags, engines and words (pure)
# ---------------------------------------------------------------------------

def sanitize_tags(text: str) -> str:
    """Ming's tags: exact case, the known aliases, unknown tags dropped, a tag that opens a sentence moved after its first word."""
    def known(match):
        key = match.group(1).strip().lower()
        if key in _EXACT:
            return "[%s]" % _EXACT[key]
        if key in _TAG_ALIASES:
            return "[%s]" % _TAG_ALIASES[key]
        return ""

    sentences = []
    for sentence in _SENTENCE_GAP.split(_TAG.sub(known, str(text or ""))):
        tags = []
        while True:
            lead = _LEADING_TAG.match(sentence)
            if not lead:
                break
            tags.append(lead.group(1))
            sentence = sentence[lead.end():]
        if tags:
            head, _, tail = sentence.partition(" ")
            sentence = " ".join(part for part in [head, *tags, tail] if part)
        sentences.append(sentence)
    return re.sub(r"[ \t]{2,}", " ", " ".join(s for s in sentences if s)).strip()


def strip_tags(text: str) -> str:
    """The text without any bracket tag, for the cloud voice that cannot read them."""
    return re.sub(r"\s*\[[^\[\]]*\]", "", str(text or "")).strip()


def engine_for(feature: str, setting_value: Optional[str], *, keep_own_audio: bool = False,
               has_speech: bool = False, out_of_credits: bool = False) -> str:
    """"cloud" or "local" for one job. An explicit setting is kept (cloud never falls back silently)."""
    value = (setting_value or "auto").strip().lower()
    if value in ("local", "cloud"):
        return value
    if feature == "lip_sync":
        return "local" if (keep_own_audio or out_of_credits) else "cloud"
    if feature == "face_swap":
        return "local" if (has_speech or out_of_credits) else "cloud"
    return "local"


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def word_error_rate(expected: str, heard_text: str) -> float:
    """Word-level edit distance over the script's words (case and punctuation ignored)."""
    want, got = _words(expected), _words(heard_text)
    if not want:
        return 0.0
    row = list(range(len(got) + 1))
    for i, word in enumerate(want, 1):
        cur = [i]
        for j, other in enumerate(got, 1):
            cur.append(min(row[j] + 1, cur[j - 1] + 1, row[j - 1] + (word != other)))
        row = cur
    return row[-1] / len(want)


def keyframe_times(duration: float) -> list:
    """Two pins inside a window of whole seconds, on the 1/3 s grid the backend takes (inside, 1/3 s apart)."""
    def grid(x):
        return max(1, round(x * 3)) / 3

    first = grid(duration / 3)
    second = grid(2 * duration / 3)
    if second <= first:
        second = first + 1 / 3
    return [first, second]


# ---------------------------------------------------------------------------
# Chunk planning (pure)
# ---------------------------------------------------------------------------

def plan_chunks(cues: list, cut_times: list, max_s: float = MAX_S, target_s: float = TARGET_S,
                total: Optional[float] = None) -> list:
    """Windows that tile the clip from 0 to *total* (default: the last cue's end) with no gaps. A chunk ends at a
    sentence end or a pause of 0.3 s or more, the one nearest target_s within max_s; no window crosses a cut (a
    sentence spanning one is split there, its words shared by time); each duration is whole seconds from 1 to 15."""
    items = sorted((dict(c) for c in cues), key=lambda c: float(c["start"]))
    end_all = float(total) if total is not None else max((float(c["end"]) for c in items), default=0.0)
    cuts = sorted(float(t) for t in cut_times if 0.0 < float(t) < end_all)
    for cut in cuts:
        items = [half for c in items for half in _cut(c, cut)]
    edges = [0.0] + cuts + [end_all]
    chunks = []
    for lo, hi in zip(edges, edges[1:]):
        shot = [c for c in items if lo <= (float(c["start"]) + float(c["end"])) / 2 < hi]
        pieces = [p for c in shot for p in _split(c, max_s)] or [{"start": lo, "end": hi, "text": ""}]
        groups = []
        while pieces:
            count = _take(pieces, max_s, target_s)
            groups.append(pieces[:count])
            pieces = pieces[count:]
        for k, group in enumerate(groups):
            start = lo if k == 0 else float(group[0]["start"])
            end = hi if k == len(groups) - 1 else float(groups[k + 1][0]["start"])
            text = " ".join(p["text"] for p in group if p.get("text"))
            chunks.append({"start": round(start, 3), "end": round(end, 3), "text": text,
                           "duration": min(max_s, max(1, math.ceil(round(end - start, 6))))})
    return chunks


def _cut(item: dict, at: float) -> list:
    """A cue spanning *at* becomes two, its words shared by time; any other cue is returned as is."""
    start, end = float(item["start"]), float(item["end"])
    if not start < at < end:
        return [item]
    words = str(item.get("text") or "").split()
    k = round(len(words) * (at - start) / (end - start))
    return [{"start": start, "end": at, "text": " ".join(words[:k])},
            {"start": at, "end": end, "text": " ".join(words[k:])}]


def _split(item: dict, max_s: float) -> list:
    """An item longer than max_s (speech with no word timings) becomes equal pieces of its words."""
    start, end = float(item["start"]), float(item["end"])
    n = math.ceil(round((end - start) / max_s, 9))
    if n <= 1:
        return [item]
    words = str(item.get("text") or "").split()
    step = (end - start) / n
    # ponytail: no word timings here, so the words are shared out by count; word times would place the cuts exactly.
    return [{"start": start + k * step, "end": start + (k + 1) * step,
             "text": " ".join(words[k * len(words) // n:(k + 1) * len(words) // n])} for k in range(n)]


def _take(pieces: list, max_s: float, target_s: float) -> int:
    """How many leading pieces make the next chunk: the break nearest target_s within max_s, else the word boundary."""
    start = float(pieces[0]["start"])
    fits = []
    for i, piece in enumerate(pieces):
        span = float(piece["end"]) - start
        if span > max_s:
            break
        nxt = pieces[i + 1] if i + 1 < len(pieces) else None
        brk = nxt is None or bool(_ENDS_SENTENCE.search(str(piece.get("text") or "").strip())) \
            or float(nxt["start"]) - float(piece["end"]) >= PAUSE_S
        fits.append((i + 1, span, brk))
    breaks = [f for f in fits if f[2]]
    return min(breaks or fits, key=lambda f: (abs(f[1] - target_s), -f[1]))[0]


# ---------------------------------------------------------------------------
# ffmpeg and the helpers the orchestration calls (tests replace these)
# ---------------------------------------------------------------------------

def stitch_args(paths: list, out: str, fps: float, audio: Optional[str] = None, size: tuple = (1280, 720),
                seconds: Optional[list] = None) -> list:
    """ffmpeg arguments: each clip's first video stream trimmed to its seconds, at fps and padded to size, joined
    with its own audio cut to the same seconds (or the audio given, replacing the clips' own)."""
    seconds = list(seconds) if seconds else [None] * len(paths)
    width, height = size
    args = ["-y"]
    for path, secs in zip(paths, seconds):
        if secs is not None:
            args += ["-t", "%.3f" % secs]
        args += ["-i", path]
    n = len(paths)
    graph = [f"[{i}:v:0]fps={fps:g},scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]" for i in range(n)]
    graph.append("".join(f"[v{i}]" for i in range(n)) + (f"concat=n={n}:v=1:a=0[v]" if n > 1 else "null[v]"))
    if audio:
        args += ["-i", audio]
        audio_map = ["-map", f"{n}:a:0?", "-shortest"]
    else:
        # Audio joins at exactly the video's boundaries: a crossfade would pull every later word ahead of its mouth.
        graph += [f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo"
                  + (f",apad,atrim=0:{secs:g}" if secs is not None else "") + f",asetpts=PTS-STARTPTS[a{i}]"
                  for i, secs in enumerate(seconds)]
        graph[n] = "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
        audio_map = ["-map", "[a]"]
    args += ["-filter_complex", ";".join(graph), "-map", "[v]"] + audio_map
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    return args


def stitch(paths: list, out: str, fps: float, audio: Optional[str] = None, size: tuple = (1280, 720),
           seconds: Optional[list] = None) -> str:
    _ffmpeg(stitch_args(paths, out, fps, audio=audio, size=size, seconds=seconds))
    return out


def _ffmpeg(args: list) -> None:
    from classes.ffmpeg_cli import run_ffmpeg
    try:
        proc = run_ffmpeg(["ffmpeg"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        raise DubbingError("ffmpeg could not run (%s)" % exc) from None
    if proc.returncode != 0:
        raise DubbingError("ffmpeg failed: " + (proc.stderr or "").strip()[-300:])


def scene_times(log: str) -> list:
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", log or "")]


def shot_cuts(source: str, start: float, seconds: float) -> list:
    """Shot cuts inside the clip's range, in clip seconds (ffmpeg scene detection); [] when ffmpeg cannot tell."""
    from classes.ffmpeg_cli import run_ffmpeg
    try:
        proc = run_ffmpeg(["ffmpeg", "-ss", "%.3f" % start, "-t", "%.3f" % seconds, "-i", source, "-an",
                           "-vf", "select='gt(scene,0.4)',showinfo", "-f", "null", "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                          check=False)
    except OSError:
        return []
    return [t for t in scene_times(proc.stderr) if 0.0 < t < seconds]


def _frame(source: str, seconds: float, out: str) -> str:
    _ffmpeg(["-y", "-ss", "%.3f" % max(0.0, seconds), "-i", source, "-frames:v", "1", out])
    return out


def _last_frame(video: str, out: str) -> str:
    _ffmpeg(["-y", "-sseof", "-0.1", "-i", video, "-frames:v", "1", out])
    return out


def _window(source: str, start: float, seconds: float, fps: float, out: str) -> str:
    """The clip's own picture and sound from *start*, at the project fps."""
    _ffmpeg(["-y", "-ss", "%.3f" % start, "-i", source, "-t", "%.3f" % seconds, "-vf", f"fps={fps:g}",
             "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", out])
    return out


def _mux(video: str, audio: str, out: str, seconds: float) -> str:
    _ffmpeg(["-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-t", "%.3f" % seconds,
             "-c:v", "copy", "-c:a", "aac", out])
    return out


def _download(url: str, path: str) -> None:
    from classes.tool_handlers import _download_video_url_to_path
    err = _download_video_url_to_path(url, path)
    if err:
        raise DubbingError(err)


def heard(path: str) -> str:
    """The words a file says, from the core recogniser (cached per file)."""
    from classes.speech.asr import transcribe_file
    return " ".join(w.text for w in transcribe_file(path).words)


def _runtime():
    from classes import dubbing_runtime
    return dubbing_runtime


# ---------------------------------------------------------------------------
# Local runtime (classes.dubbing_runtime) and its worker
# ---------------------------------------------------------------------------

def picks_of(status: dict) -> Optional[dict]:
    """The cached per-model backend picks in a runtime status, or None before the probe has run."""
    return (status or {}).get("probe") or None


def ensure_local(features: set, *, consent: Callable[[list], bool], progress: Any = None) -> dict:
    """The runtime status for these parts; what is missing is installed only after consent(steps) says yes."""
    rt = _runtime()
    st = rt.status()
    need = set()
    if "face" in features and not (st.get("env") and st.get("face_models")):
        need.add("face")
    if "ming" in features and not st.get("ming"):
        need.add("ming")
    if not need:
        if picks_of(st) is None:                 # installed, but the probe's picks never reached the settings
            st = rt.reprobe(set(features), progress=progress)
        return st
    if not consent([{"label": _LABELS[f]} for f in sorted(need)]):
        raise DubbingError("the one-time download was declined, so nothing was installed")
    try:
        return rt.install(need, progress=progress)
    except rt.DubbingRuntimeError as exc:
        raise DubbingError(str(exc)) from None


@contextlib.contextmanager
def _cancel_on(job):
    """A threading.Event for run_worker, set when the job is cancelled."""
    event, done = threading.Event(), threading.Event()
    if job is not None:
        def watch():
            while not done.wait(0.25):
                if job.should_cancel():
                    event.set()
                    return
        threading.Thread(target=watch, name="dubbing-cancel", daemon=True).start()
    try:
        yield event
    finally:
        done.set()


def _worker(command: str, args: dict, job=None) -> dict:
    rt = _runtime()
    progress = (lambda _fraction, message: job.report(None, message)) if job is not None else None
    with _cancel_on(job) as cancel:
        try:
            return rt.run_worker(command, args, progress=progress, cancel=cancel)
        except rt.DubbingRuntimeError as exc:
            raise DubbingError(str(exc)) from None


def _speak(text: str, out: str, picks, job=None, instruction: str = "", prompt_wav: str = "",
           prompt_text: str = "") -> dict:
    args = {"text": text, "out": out, "picks": picks, "task": "FREEFORM" if instruction else "TTS",
            "max_decode_steps": MING_STEPS}
    if instruction:
        args["instruction"] = instruction
    if prompt_wav:
        args.update(prompt_wav=prompt_wav, prompt_text=prompt_text)
    return _worker("tts", args, job)


def tts_local(text: str, out: str, picks, *, instruction: str = "", prompt_wav: str = "",
              prompt_text: str = "") -> dict:
    """Speech on this computer (Ming): plain, FREEFORM with an instruction, or a copied voice from prompt_wav.
    The text is spoken as given: callers sanitize its tags first (sanitize_tags)."""
    with _JOB_LOCK:
        return _speak(text, out, picks, instruction=instruction, prompt_wav=prompt_wav, prompt_text=prompt_text)


def _lipsync(video: str, audio: str, out: str, picks, job=None) -> dict:
    return _worker("lipsync", {"video": video, "audio": audio, "out": out, "model": "edtalk", "picks": picks,
                               "detect_every": 10}, job)


def _swap_window(video: str, face_image: str, out: str, picks, job=None) -> str:
    _worker("faceswap", {"video": video, "face_image": face_image, "out": out, "picks": picks}, job)
    return out


def _swap_frames(images: list, face_image: str, picks, out_dir: str, job=None) -> list:
    os.makedirs(out_dir, exist_ok=True)
    res = _worker("swap-frames", {"images": images, "face_image": face_image, "out_dir": out_dir, "picks": picks}, job)
    swapped = list(res.get("images") or [])
    if len(swapped) != len(images):
        raise DubbingError("the face swap returned %d frames for %d" % (len(swapped), len(images)))
    return swapped


# ---------------------------------------------------------------------------
# Cloud (backend modes "dialogue" and "reference") helpers
# ---------------------------------------------------------------------------

@dataclass
class _Run:
    """One job: the source clip, the engines, the local picks (resolved on first need) and the work folder."""
    source: str
    start: float
    end: float
    fps: float
    client: Any
    job: Any
    work: str
    picks: Any = None
    resolve_picks: Optional[Callable[[], dict]] = None
    voice: str = ""
    fallback: bool = True


def _picks(run: _Run) -> dict:
    if not run.picks and run.resolve_picks is not None:
        run.picks = run.resolve_picks()
    if not run.picks:
        raise DubbingError("this computer has no working backend yet; run the dubbing setup again")
    return run.picks


def _png(run: _Run, name: str, tag: str) -> str:
    return os.path.join(run.work, "%s-%s.png" % (name, tag))


def _upload(client, path: str) -> str:
    file_id = "dub-" + uuid.uuid4().hex[:12]
    res = client.upload_media_file(path, file_id=file_id)
    if not isinstance(res, dict) or not res.get("success"):
        raise _Refused("could not upload a frame (%s)" % ((res or {}).get("error") or "unknown error"))
    return file_id


def _generate(client, prompt: str, seconds: int, **kwargs) -> str:
    res = client.generate_video(prompt, duration_seconds=seconds, **kwargs)
    if not isinstance(res, dict):
        raise _Refused("no answer from the backend")
    if res.get("credits"):
        raise _Refused(res.get("error") or "Out of credits.", credits=True)
    if res.get("error") or not res.get("video_url"):
        raise _Refused(res.get("error") or "no video came back")
    return res["video_url"]


def _fetch(url: str, run: _Run, tag: str) -> str:
    path = os.path.join(run.work, "raw-%s.mp4" % tag)
    try:
        _download(url, path)
    except DubbingError as exc:
        raise _Refused(str(exc)) from None
    return path


def _pinned(run: _Run, t0: float, dur: int, span: float, first: str, tag: str) -> tuple:
    """The first, last and keyframe pictures for one window, pinned from the source clip inside the chunk only
    (Grok's whole-second window can run past the chunk's end, into the next shot)."""
    inside = max(0.0, min(dur, span) - 1.0 / run.fps)
    last = _frame(run.source, t0 + inside, _png(run, "last", tag))
    keys = [(t, _frame(run.source, t0 + t, _png(run, "key%d" % n, tag)))
            for n, t in enumerate(keyframe_times(dur)) if t < inside]
    return first, last, keys


def _dialogue_try(run: _Run, chunk: dict, prev: Optional[str], tag: str) -> str:
    dur = chunk["duration"]
    t0 = run.start + chunk["start"]
    first, last, keys = _pinned(run, t0, dur, chunk["end"] - chunk["start"],
                                prev or _frame(run.source, t0, _png(run, "first", tag)), tag)
    text = strip_tags(chunk["text"])
    kwargs = {"mode": "dialogue", "dialogue_text": text, "generate_audio": True,
              "voice_id": run.voice or DEFAULT_VOICE,
              "frame_images": [{"file_id": _upload(run.client, first), "frame": "first"},
                               {"file_id": _upload(run.client, last), "frame": "last"}],
              "keyframes": [{"file_id": _upload(run.client, p), "timestamp_s": t} for t, p in keys]}
    raw = _fetch(_generate(run.client, _PROMPT_DIALOGUE, dur, **kwargs), run, tag)
    try:
        said = heard(raw)
    except Exception as exc:                     # no recogniser, or no audio in the file: an attempt that failed
        raise _Refused("the words could not be checked (%s)" % exc) from None
    if word_error_rate(text, said) > WORD_ERROR_MAX:
        raise _Refused("the words did not match the script")
    return raw


def _reference_try(run: _Run, chunk: dict, prev: Optional[str], face_image: str, tag: str) -> str:
    dur = chunk["duration"]
    t0 = run.start + chunk["start"]
    first, last, keys = _pinned(run, t0, dur, chunk["end"] - chunk["start"],
                                prev or _frame(run.source, t0, _png(run, "first", tag)), tag)
    swapped = _swap_frames([first, last] + [p for _, p in keys], face_image, _picks(run),
                           os.path.join(run.work, "swap-" + tag), run.job)
    ids = [_upload(run.client, p) for p in swapped]
    kwargs = {"mode": "reference", "generate_audio": False,
              "reference_images": [{"file_id": _upload(run.client, face_image)}],
              "frame_images": [{"file_id": ids[0], "frame": "first"}, {"file_id": ids[1], "frame": "last"}],
              "keyframes": [{"file_id": fid, "timestamp_s": t} for fid, (t, _) in zip(ids[2:], keys)]}
    return _fetch(_generate(run.client, _PROMPT_SWAP, dur, **kwargs), run, tag)


def _check(job) -> None:
    if job is not None and job.should_cancel():
        raise DubbingError("cancelled")


def _cloud_loop(run: _Run, chunks: list, cloud_part, local_part) -> tuple:
    """Each chunk: up to two cloud attempts (a credits refusal stops them), then this computer for that chunk."""
    parts, prev, cloud, local, credits = [], None, 0, 0, False
    for i, chunk in enumerate(chunks, 1):
        _check(run.job)
        if run.job is not None:
            run.job.report((i - 1) / len(chunks), "chunk %d of %d on the cloud" % (i, len(chunks)))
        part, reason = None, "out of credits"
        for _attempt in (1, 2):
            if credits:
                break
            try:
                part = cloud_part(chunk, prev, str(i))
                break
            except _Refused as exc:
                reason = str(exc).rstrip(".")
                if exc.credits:
                    credits = True
                    break
        if part is not None:
            cloud += 1
        else:
            if not run.fallback:
                raise DubbingError("chunk %d of %d failed: %s. Run it on this computer instead."
                                   % (i, len(chunks), reason))
            part = local_part(chunk, str(i))
            local += 1
        parts.append(part)
        if i < len(chunks):
            prev = _last_frame(part, _png(run, "prev", str(i)))
    return parts, cloud, local


@contextlib.contextmanager
def _scratch():
    work = tempfile.mkdtemp(prefix="zenvi_dubbing_")
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _backend(client):
    if client is not None:
        return client
    from classes.api_client import get_backend_client
    return get_backend_client()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def lip_sync(*, source: str, start: float, end: float, size: tuple, fps: float, out: str, text: str = "",
             audio: str = "", cues: Optional[list] = None, voice: str = "", engine: str = "cloud",
             allow_local_fallback: bool = True, picks: Optional[dict] = None,
             resolve_picks: Optional[Callable[[], dict]] = None, client=None, job=None) -> dict:
    """Make the clip's mouth say *text* (Grok in chunks, or this computer) or *audio* (this computer), into *out*.

    *start*/*end* are the clip's source range; *cues* are the script's sentences in clip seconds. Local picks
    come from *picks*, or from *resolve_picks()* the first time this computer has to work (so a cloud job never
    installs anything it does not need). Returns {"out", "seconds", "chunks", "cloud_chunks", "local_chunks"}."""
    seconds = float(end) - float(start)
    with _JOB_LOCK, _scratch() as work:
        run = _Run(source, float(start), float(end), float(fps), None, job, work, picks=picks,
                   resolve_picks=resolve_picks, voice=voice, fallback=allow_local_fallback)
        if engine == "local" or audio:
            window = _window(source, float(start), seconds, float(fps), os.path.join(work, "window.mp4"))
            speech = audio or _speak(sanitize_tags(text), os.path.join(work, "speech.wav"), _picks(run), job)["out"]
            mouth = os.path.join(work, "mouth.mp4")
            _lipsync(window, speech, mouth, _picks(run), job)
            # ponytail: speech longer than the clip is cut at the clip's end (-shortest); a longer script needs a longer clip.
            stitch([mouth], out, fps, audio=speech, size=size, seconds=[seconds])
            return {"out": out, "seconds": seconds, "chunks": 1, "cloud_chunks": 0, "local_chunks": 1}
        run.client = _backend(client)
        chunks = plan_chunks(cues or [{"start": 0.0, "end": seconds, "text": text}],
                             cut_times=shot_cuts(source, float(start), seconds), total=seconds)

        def cloud_part(chunk, prev, tag):
            return _dialogue_try(run, chunk, prev, tag)

        def local_part(chunk, tag):
            t0 = run.start + chunk["start"]
            span = chunk["end"] - chunk["start"]
            window = _window(source, t0, span, run.fps, os.path.join(work, "win-%s.mp4" % tag))
            speech = _speak(sanitize_tags(chunk["text"]), os.path.join(work, "speech-%s.wav" % tag), _picks(run),
                            job)["out"]
            mouth = os.path.join(work, "mouth-%s.mp4" % tag)
            _lipsync(window, speech, mouth, _picks(run), job)
            return _mux(mouth, speech, os.path.join(work, "part-%s.mp4" % tag), span)

        parts, cloud, local = _cloud_loop(run, chunks, cloud_part, local_part)
        stitch(parts, out, fps, size=size, seconds=[c["end"] - c["start"] for c in chunks])
        return {"out": out, "seconds": seconds, "chunks": len(chunks), "cloud_chunks": cloud,
                "local_chunks": local}


def face_swap(*, source: str, start: float, end: float, size: tuple, fps: float, out: str, face_image: str,
              engine: str = "cloud", allow_local_fallback: bool = True, picks: Optional[dict] = None,
              client=None, job=None) -> dict:
    """Swap the clip's face for *face_image* (this computer, or Grok in chunks), keeping the clip's own audio."""
    seconds = float(end) - float(start)
    with _JOB_LOCK, _scratch() as work:
        run = _Run(source, float(start), float(end), float(fps), None, job, work, picks=picks,
                   fallback=allow_local_fallback)
        audio = _window(source, float(start), seconds, float(fps), os.path.join(work, "window.mp4"))
        if engine == "local":
            swapped = _swap_window(audio, face_image, os.path.join(work, "swapped.mp4"), _picks(run), job)
            stitch([swapped], out, fps, audio=audio, size=size, seconds=[seconds])
            return {"out": out, "seconds": seconds, "chunks": 1, "cloud_chunks": 0, "local_chunks": 1}
        run.client = _backend(client)
        chunks = plan_chunks([{"start": 0.0, "end": seconds, "text": ""}],
                             cut_times=shot_cuts(source, float(start), seconds), total=seconds)

        def cloud_part(chunk, prev, tag):
            return _reference_try(run, chunk, prev, face_image, tag)

        def local_part(chunk, tag):
            t0 = run.start + chunk["start"]
            span = chunk["end"] - chunk["start"]
            window = _window(source, t0, span, run.fps, os.path.join(work, "win-%s.mp4" % tag))
            return _swap_window(window, face_image, os.path.join(work, "part-%s.mp4" % tag), _picks(run), job)

        parts, cloud, local = _cloud_loop(run, chunks, cloud_part, local_part)
        stitch(parts, out, fps, audio=audio, size=size, seconds=[c["end"] - c["start"] for c in chunks])
        return {"out": out, "seconds": seconds, "chunks": len(chunks), "cloud_chunks": cloud,
                "local_chunks": local}
