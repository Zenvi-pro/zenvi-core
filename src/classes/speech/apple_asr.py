"""Apple SpeechAnalyzer bridge (macOS only).

Runs the ``zenvi-apple-speech-asr`` helper (Swift SpeechAnalyzer). Exit code 2
means the OS is too old / helper unavailable → caller falls back to Whisper.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

from classes.speech.cache import Word
from classes.speech.runtime import CancelToken

log = logging.getLogger("speech.apple_asr")

HELPER_NAME = "zenvi-apple-speech-asr"


def is_macos() -> bool:
    return sys.platform == "darwin"


def helper_candidates() -> list[str]:
    """Search order: env override, PATH, next to frozen app, repo native build."""
    out: list[str] = []
    env = os.environ.get("ZENVI_APPLE_SPEECH_ASR", "").strip()
    if env:
        out.append(env)
    which = shutil.which(HELPER_NAME)
    if which:
        out.append(which)
    try:
        from classes import info
        # Packaged app Resources / next to binary
        for base in (
            getattr(info, "PATH", None),
            getattr(info, "USER_PATH", None),
            os.path.join(getattr(info, "PATH", "") or "", "native"),
        ):
            if not base:
                continue
            out.append(os.path.join(str(base), HELPER_NAME))
            out.append(os.path.join(str(base), "bin", HELPER_NAME))
    except Exception:
        pass
    # Dev tree: native/apple-speech-asr/.build/.../zenvi-apple-speech-asr
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    for build in ("release", "debug"):
        out.append(
            os.path.join(
                here,
                "native",
                "apple-speech-asr",
                ".build",
                build,
                HELPER_NAME,
            )
        )
    # Dedupe preserving order
    seen = set()
    unique = []
    for p in out:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            unique.append(ap)
    return unique


def find_helper() -> Optional[str]:
    for path in helper_candidates():
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def apple_asr_available() -> bool:
    return is_macos() and find_helper() is not None


class AppleSpeechTranscriber:
    """Transcriber backed by the native SpeechAnalyzer helper."""

    model_id = "apple-speech-analyzer"

    def transcribe(
        self,
        wav_path: str,
        *,
        language: Optional[str],
        token: CancelToken,
    ) -> tuple[list[Word], str]:
        helper = find_helper()
        if not helper:
            raise RuntimeError("Apple Speech helper not found")
        token.raise_if_cancelled()
        cmd = [helper, wav_path]
        if language and language != "auto":
            cmd.extend(["--locale", language])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Apple Speech timed out") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("Apple Speech helper missing") from exc

        if proc.returncode == 2:
            raise RuntimeError(
                "Apple SpeechAnalyzer unavailable on this macOS (need 26+); use whisper"
            )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"Apple Speech failed: {err}")

        token.raise_if_cancelled()
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Apple Speech returned invalid JSON") from exc

        words: list[Word] = []
        for item in payload.get("words") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(item.get("startSec") or 0)
                end = float(item.get("endSec") or 0)
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            conf = item.get("confidence")
            words.append(
                Word(
                    text=text,
                    startSec=start,
                    endSec=end,
                    confidence=float(conf) if conf is not None else None,
                )
            )
        lang = str(payload.get("language") or language or "unknown")
        return words, lang
