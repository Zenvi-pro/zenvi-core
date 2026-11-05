"""Speak a chat prompt instead of typing it.

The microphone is read by ffmpeg (already bundled for captions) as 16 kHz mono PCM, and the
words come from the recogniser the user picked in the Captions dock: Whistle by default,
re-heard every second so the prompt fills in while the user speaks, or Parakeet, heard once
at the end (it loads a 900 MB model per run). Users with Wispr Flow installed can dictate
with it instead: it types into whichever box has focus.
"""

import array
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import wave
from typing import Callable, List, Optional

from classes import hyperframes_local as hf
from classes.ffmpeg_cli import find_ffmpeg

SAMPLE_RATE = 16000
PARTIAL_EVERY = 1.0          # seconds between live re-hearings
MAX_SECONDS = 120            # a prompt, not a podcast: recording stops itself here
SPEECH_OVER_FLOOR = 4        # speech is this many times louder than the take's quiet 100 ms chunks
HISS_RMS = 20                # the floor never counts as lower than mic hiss (faint speech measured ~100)
SPEECH_CHUNKS = 3            # ...for at least 300 ms in all, so one knock or click is not speech
PROJECT = "dictation"        # hf.project_dir(PROJECT) holds speech.wav
WISPR_FLOW_URL = "wispr-flow://start-hands-free"
WISPR_FLOW_SITE = "https://wisprflow.ai"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
# What Whisper-family recognisers "hear" in a quiet room: a take that is only this is no words.
MADE_UP = {"", "you", "thank you", "thanks", "thank you very much", "thanks for watching",
           "thank you for watching", "bye"}
_heard = threading.Lock()    # one speech.wav, one recogniser run at a time


class DictationError(Exception):
    pass


def _dshow_mic(ffmpeg: str) -> str:
    listing = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL, timeout=15, creationflags=_NO_WINDOW).stderr or ""
    lines = listing.splitlines()
    in_audio = False                                    # ffmpeg 4 groups devices under headings
    for i, line in enumerate(lines):
        if "DirectShow audio devices" in line or "DirectShow video devices" in line:
            in_audio = "audio" in line
            continue
        device = re.search(r'"([^"]+)"(.*)', line)
        if device and "Alternative name" not in line and (in_audio or "(audio)" in device.group(2)):
            # The ASCII alternative name when listed: friendly names can lose characters like the (R) sign.
            alt = re.search(r'Alternative name "([^"]+)"', lines[i + 1]) if i + 1 < len(lines) else None
            return alt.group(1) if alt else device.group(1)
    raise DictationError("no microphone was found on this computer")


def mic_args(platform: str = sys.platform, ffmpeg: str = "ffmpeg") -> List[str]:
    """ffmpeg input arguments for the default microphone."""
    if platform == "win32":
        return ["-f", "dshow", "-i", "audio=%s" % _dshow_mic(ffmpeg)]
    if platform == "darwin":
        return ["-f", "avfoundation", "-i", ":default"]
    return ["-f", "pulse", "-i", "default"]    # PulseAudio or PipeWire's pulse server


def _parakeet() -> bool:
    return hf.recogniser() == "parakeet" and bool(hf.status().get("parakeet"))


def live_engine() -> bool:
    """Whether the recogniser is quick enough to re-hear the prompt while it is spoken."""
    return not _parakeet()


def rms(chunk: bytes) -> float:
    samples = array.array("h", chunk[:len(chunk) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def heard_speech(levels: List[float]) -> bool:
    """Whether a take's per-100 ms RMS levels hold speech: mics and rooms differ 10x in level (one
    laptop mic: faint speech ~100, "hello" ~500), so speech is measured against the take's own quiet chunks."""
    room = sorted(level for level in levels if level >= 5)   # below 5: zeros while the mic wakes up
    if not room:
        return False
    loud = SPEECH_OVER_FLOOR * max(room[len(room) // 10], HISS_RMS)
    return sum(level >= loud for level in room) >= SPEECH_CHUNKS


def transcribe(pcm: bytes, parakeet: Optional[bool] = None) -> str:
    """The words in 16 kHz mono 16-bit PCM, as one line of text."""
    with _heard:
        wav = os.path.join(hf.project_dir(PROJECT), "speech.wav")
        os.makedirs(os.path.dirname(wav), exist_ok=True)
        with wave.open(wav, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm)
        if _parakeet() if parakeet is None else parakeet:
            code, out = hf.run(PROJECT, "hyperframes", ["transcribe", "speech.wav", "--engine", "parakeet", "--json"],
                               timeout=hf.SPEECH_TIMEOUT)
            if code != 0:
                raise DictationError("Parakeet could not hear the prompt: %s" % out[-400:])
            with open(os.path.join(hf.project_dir(PROJECT), "transcript.json"), encoding="utf-8") as fh:
                words = json.load(fh)
        else:
            if not hf.speech_supported():          # Intel Macs: Parakeet is the only recogniser
                raise DictationError("no speech recogniser is installed: download Parakeet in the Captions dock")
            if not hf.speech_ready():
                hf.install_speech()                 # 18 MB, the same one-time download captions make
            words = hf.transcribe(wav)
    return " ".join(" ".join(str(w.get("text") or "").strip() for w in words).split())


def made_up(text: str) -> bool:
    """Whether *text* is only what a recogniser makes up for a quiet take (see MADE_UP)."""
    return re.sub(r"[^a-z ]", "", text.lower()).strip() in MADE_UP


def _heard_words(pcm: bytes, parakeet: bool) -> str:
    text = transcribe(pcm, parakeet)
    return "" if made_up(text) else text


class Dictation:
    """One recording. *on_text(text, final)* and *on_error(message)* are called from worker threads."""

    def __init__(self, on_text: Callable[[str, bool], None], on_error: Callable[[str], None]):
        self._on_text, self._on_error = on_text, on_error
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._levels: List[float] = []
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        ffmpeg = find_ffmpeg("ffmpeg")
        if not ffmpeg:
            raise DictationError("ffmpeg was not found, so the microphone cannot be read")
        threading.Thread(target=self._run, args=(ffmpeg,), daemon=True).start()   # listing mics can take seconds

    def stop(self) -> None:
        """Let go of the microphone now; the final words follow through *on_text*."""
        self._stopped.set()
        self._release()

    def _release(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._pcm)

    def _spoke(self) -> bool:
        with self._lock:
            return heard_speech(self._levels)

    def _read(self) -> None:
        limit = MAX_SECONDS * SAMPLE_RATE * 2
        while True:
            chunk = self._proc.stdout.read(3200)    # 100 ms
            if not chunk:
                return
            level = rms(chunk)
            with self._lock:
                self._pcm += chunk
                self._levels.append(level)
                full = len(self._pcm) >= limit
            if full:
                self.stop()
                return

    def _run(self, ffmpeg: str) -> None:
        try:
            argv = [ffmpeg, "-hide_banner", "-loglevel", "error", *mic_args(sys.platform, ffmpeg),
                    "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
            with tempfile.TemporaryFile() as errors:   # a file, not a pipe: nobody has to drain it
                with self._lock:
                    self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors,
                                                  stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                if self._stopped.is_set():           # stopped while the mic was being found
                    self._release()
                reader = threading.Thread(target=self._read, daemon=True)
                reader.start()
                live, heard = live_engine(), 0          # the engine this take started with, kept
                # ponytail: re-hears the whole prompt each time (Whistle is fast); MAX_SECONDS bounds the cost
                while not self._stopped.wait(PARTIAL_EVERY):
                    if self._proc.poll() is not None and not reader.is_alive() and not self._stopped.is_set():
                        errors.seek(0)
                        err = errors.read().decode("utf-8", "replace").strip()
                        self._on_error("the microphone could not be read: %s" % (err[-400:] or "ffmpeg stopped"))
                        return
                    pcm = self._snapshot()
                    if live and len(pcm) > heard and self._spoke():
                        heard = len(pcm)
                        self._on_text(_heard_words(pcm, not live), False)
            self._release()
            reader.join(5)
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            # Recognisers "hear" words like "Thank you." in a quiet room: no speech, no words.
            self._on_text(_heard_words(self._snapshot(), not live) if self._spoke() else "", True)
        except Exception as exc:                    # reported in the chat box, never a silent dead mic
            with self._lock:
                proc = self._proc
            if proc and proc.poll() is None:
                proc.kill()
            self._on_error(str(exc))


def wispr_flow_installed(platform: str = sys.platform, app_dirs: Optional[List[str]] = None) -> bool:
    if platform == "darwin":
        dirs = app_dirs or ["/Applications", os.path.expanduser("~/Applications")]
        return any(os.path.isdir(os.path.join(d, "Wispr Flow.app")) for d in dirs)
    if platform == "win32":
        try:
            import winreg
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "wispr-flow"))
            return True
        except OSError:
            return False
    return False


def use_wispr_flow(choice: str, installed: bool) -> bool:
    """The Dictation setting: auto (Wispr Flow when installed), wispr, or zenvi."""
    return installed and choice in ("auto", "wispr")


def engines(choice: str, wispr_installed: bool) -> List[dict]:
    """The mic menu: each engine, what using it needs, and which one the mic uses now.

    Whistle and Parakeet are the Captions dock's recogniser choice, shared with captions.
    """
    # Files, not hf.status(): that hashes them under the install lock, and this runs on the GUI thread.
    # A damaged file still shows as installed here; using it re-checks the hash.
    wispr = use_wispr_flow(choice, wispr_installed)
    current = "wispr" if wispr else hf.recogniser()
    whistle = hf.speech_supported()
    parakeet = os.path.isfile(hf._parakeet_model())
    cli = os.path.isfile(hf.cli_script())          # Parakeet is installed and run by the HyperFrames CLI
    return [
        {"id": "whistle", "label": "Whistle: fast, small, words appear as you speak", "enabled": whistle,
         "note": "installed" if os.path.isfile(hf.speech_model()) else "download 18 MB" if whistle
         else "not available here", "checked": current == "whistle"},
        {"id": "parakeet", "label": "Parakeet: more accurate, words appear when you stop",
         "enabled": parakeet or cli, "checked": current == "parakeet",
         "note": "installed" if parakeet else "download about 900 MB" if cli
         else "ask the assistant to set up HyperFrames first"},
        {"id": "wispr", "label": "Wispr Flow", "enabled": True,
         "note": "installed" if wispr_installed else "get it at wisprflow.ai", "checked": wispr},
    ]
