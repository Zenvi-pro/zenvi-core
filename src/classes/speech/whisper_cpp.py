"""Local ASR through whisper.cpp's ``whisper-cli`` (the engine releases bundle).

faster-whisper (CTranslate2) cannot be installed in the MSYS2 Python the
Windows build uses, so every release ships whisper-cli and a quantized base
model in ``<app>/whisper/``. A source checkout finds them through
``ZENVI_WHISPER_CLI`` / ``ZENVI_WHISPER_MODEL`` or ``whisper-cli`` on PATH
(MSYS2: mingw-w64-ucrt-x86_64-whisper.cpp, macOS: brew install whisper-cpp).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

from classes.speech.cache import Word
from classes.speech.runtime import CancelToken

log = logging.getLogger("speech.whisper_cpp")

MODEL_FILE = "ggml-base-q5_1.bin"
VAD_FILE = "ggml-silero-v5.1.2.bin"
MODEL_ID = "whisper.cpp-base-q5_1"
_CLI_NAMES = ("whisper-cli.exe", "whisper-cli") if sys.platform == "win32" else ("whisper-cli",)


def _bundle_dir() -> str:
    from classes import info
    return os.path.join(info.PATH, "whisper")


def cli_path() -> str:
    env = os.environ.get("ZENVI_WHISPER_CLI", "")
    if env and os.path.isfile(env):
        return env
    for name in _CLI_NAMES:
        bundled = os.path.join(_bundle_dir(), name)
        if os.path.isfile(bundled):
            return bundled
    for name in _CLI_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _find(env_name: str, file_name: str) -> str:
    from classes import info
    for candidate in (os.environ.get(env_name, ""),
                      os.path.join(_bundle_dir(), file_name),
                      os.path.join(info.USER_PATH, "whisper", file_name)):
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def model_path() -> str:
    return _find("ZENVI_WHISPER_MODEL", MODEL_FILE)


def vad_path() -> str:
    """The Silero VAD model; optional, but without it words are timed into silence."""
    return _find("ZENVI_WHISPER_VAD", VAD_FILE)


def words_from_cli_json(data: dict, language: Optional[str]) -> tuple[list[Word], str]:
    """Words and the detected language from whisper-cli's ``-oj`` output."""
    words = []
    for seg in data.get("transcription") or []:
        text = str(seg.get("text") or "").strip()
        # Empty segments and markers such as [BLANK_AUDIO] are not words.
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        offsets = seg.get("offsets") or {}
        words.append(Word(text, float(offsets.get("from") or 0) / 1000.0,
                          float(offsets.get("to") or 0) / 1000.0))
    detected = str((data.get("result") or {}).get("language") or language or "auto")
    return words, detected


def available() -> bool:
    return bool(cli_path() and model_path())


def model_id() -> str:
    """Transcript-cache identity of the model in use.

    The bundled model (checksummed at build time) is MODEL_ID; any other file
    (ZENVI_WHISPER_MODEL, the profile folder) is identified by its name, path,
    size and modification time, so a different or replaced model never reuses
    another model's transcripts.
    """
    path = model_path()
    if not path or os.path.abspath(path) == os.path.abspath(os.path.join(_bundle_dir(), MODEL_FILE)):
        return MODEL_ID
    try:
        st = os.stat(path)
        stamp = "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = os.path.normcase(os.path.abspath(path))
    return "whisper.cpp-%s-%s" % (os.path.splitext(os.path.basename(path))[0],
                                  hashlib.sha1(stamp.encode("utf-8"), usedforsecurity=False).hexdigest()[:8])


def _time_limit(wav_path: str) -> float:
    """Seconds whisper-cli may run: 4x the audio (16 kHz mono 16-bit), at least 10 minutes."""
    try:
        audio_seconds = os.path.getsize(wav_path) / 32000.0
    except OSError:
        audio_seconds = 0.0
    return max(600.0, 4.0 * audio_seconds)


class WhisperCppTranscriber:
    @property
    def model_id(self) -> str:
        return model_id()

    def transcribe(
        self,
        wav_path: str,
        *,
        language: Optional[str],
        token: CancelToken,
    ) -> tuple[list[Word], str]:
        cli, model = cli_path(), model_path()
        if not (cli and model):
            raise RuntimeError("whisper.cpp is not installed (whisper-cli and %s)" % MODEL_FILE)
        with tempfile.TemporaryDirectory(prefix="zenvi-whisper-") as tmp:
            out_base = os.path.join(tmp, "out")
            cmd = [
                cli, "-m", model, "-f", wav_path, "-l", language or "auto",
                # One segment per word: the word timings the transcript tools edit with.
                # (No -np: it also silences whisper's own error messages.)
                "-ml", "1", "-sow", "-oj", "-of", out_base,
                "-t", str(max(1, min(8, (os.cpu_count() or 4) - 1))),
            ]
            vad = vad_path()
            if vad:
                # Speech only: otherwise words are timed into the silence around
                # them, and silence itself comes back as hallucinated words.
                cmd += ["--vad", "-vm", vad]
            else:
                log.warning("no %s found: transcribing without --vad (word timings "
                            "drift into silence)", VAD_FILE)
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace", **kwargs)
            limit = _time_limit(wav_path)
            deadline = time.monotonic() + limit
            try:
                while True:
                    try:
                        _out, err = proc.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        token.raise_if_cancelled()  # killed below
                        if time.monotonic() > deadline:
                            # It holds the one inference slot: never wait forever.
                            raise RuntimeError(
                                "whisper.cpp did not finish within %d s" % limit) from None
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()  # before the temp folder goes (Windows locks open files)
                raise
            if proc.returncode != 0:
                tail = "\n".join((err or "").strip().splitlines()[-3:])
                raise RuntimeError("whisper.cpp failed (exit %s): %s" % (proc.returncode, tail))
            with open(out_base + ".json", "r", encoding="utf-8") as fh:
                data = json.load(fh)
        return words_from_cli_json(data, language)
