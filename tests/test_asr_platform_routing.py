"""Transcription routing: Apple SpeechAnalyzer on macOS 26+, else the recogniser captions use
(Whistle, downloaded on first use, or Parakeet when picked and installed). No Whisper anywhere."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from classes.speech import asr as asr_mod
from classes.speech.asr import (
    APPLE_MODEL_ID,
    make_transcriber,
    resolve_engine,
    reset_transcriber_factory,
)
from classes.speech.cache import Word
from classes.speech.runtime import CancelToken

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def teardown_function():
    reset_transcriber_factory()


def _recogniser(name="whistle", parakeet=False, whistle_supported=True):
    return patch.multiple(
        "classes.hyperframes_local",
        recogniser=lambda: name,
        parakeet_ready=lambda: parakeet,
        speech_supported=lambda platform=None, machine=None: whistle_supported,
    )


def test_windows_uses_the_captions_recogniser():
    with patch("classes.speech.asr.sys.platform", "win32"):
        with _recogniser("whistle"):
            assert resolve_engine("auto") == "whistle"
        with _recogniser("parakeet", parakeet=True):
            assert resolve_engine("auto") == "parakeet"
        with _recogniser("parakeet", parakeet=False):   # picked but not downloaded: never 900 MB unasked
            assert resolve_engine("auto") == "whistle"
        with pytest.raises(RuntimeError, match="macOS"):
            resolve_engine("apple")


def test_whisper_is_not_an_engine():
    with pytest.raises(RuntimeError, match="engine"):
        resolve_engine("whisper")


def test_explicit_parakeet_needs_it_installed():
    with _recogniser("whistle", parakeet=False):
        with pytest.raises(RuntimeError, match="Parakeet"):
            resolve_engine("parakeet")
    with _recogniser("whistle", parakeet=True):
        assert resolve_engine("parakeet") == "parakeet"


def test_intel_mac_without_parakeet_says_what_to_install():
    with patch("classes.speech.asr.sys.platform", "darwin"), \
         patch("classes.speech.apple_asr.apple_asr_available", return_value=False), \
         _recogniser("parakeet", parakeet=False, whistle_supported=False):
        with pytest.raises(RuntimeError, match="Parakeet"):
            resolve_engine("auto")


def test_mac_auto_prefers_apple_then_the_captions_recogniser():
    from classes.speech.apple_asr import AppleSpeechTranscriber
    with patch("classes.speech.asr.sys.platform", "darwin"), _recogniser("whistle"):
        with patch("classes.speech.apple_asr.apple_asr_available", return_value=True):
            assert resolve_engine("auto") == "apple"
            assert isinstance(make_transcriber("apple"), AppleSpeechTranscriber)
        with patch("classes.speech.apple_asr.apple_asr_available", return_value=False):
            assert resolve_engine("auto") == "whistle"


def test_whistle_downloads_on_first_use_and_passes_the_language():
    calls = []
    with patch.multiple(
        "classes.hyperframes_local",
        speech_ready=lambda fresh=False: bool(calls),
        install_speech=lambda: calls.append("install"),
        recognise=lambda wav, language=None: calls.append(("run", wav, language)) or {
            "language": "en", "words": [{"text": "hi", "start": 0.1, "end": 0.3}]},
    ):
        words, lang = make_transcriber("whistle").transcribe("a.wav", language="en", token=CancelToken())
    assert calls == ["install", ("run", "a.wav", "en")]
    assert words == [Word("hi", 0.1, 0.3)] and lang == "en"


def test_parakeet_transcriber_runs_the_cli_recogniser():
    seen = []
    with patch("classes.hyperframes_local.transcribe_parakeet",
               lambda wav, language=None: seen.append(language) or [{"text": "yo", "start": 0.0, "end": 0.2}]):
        words, lang = make_transcriber("parakeet").transcribe("a.wav", language=None, token=CancelToken())
        make_transcriber("parakeet").transcribe("a.wav", language="ja", token=CancelToken())
    assert words == [Word("yo", 0.0, 0.2)] and lang == "unknown"
    assert seen == [None, "ja"]   # the requested language reaches the CLI


def test_apple_failure_falls_back_to_the_captions_recogniser(tmp_path):
    from classes.speech.cache import TranscriptCache, reset_default_cache_for_tests

    class BoomApple:
        model_id = APPLE_MODEL_ID

        def transcribe(self, wav_path, *, language, token):
            raise RuntimeError("SpeechAnalyzer requires macOS 26+")

    class OkWhistle:
        model_id = "whistle"

        def transcribe(self, wav_path, *, language, token):
            return [Word("hi", 0.0, 0.2)], "en"

    seen = []

    def factory(engine, model_id):
        seen.append(engine)
        return BoomApple() if engine == "apple" else OkWhistle()

    cache = TranscriptCache(root=str(tmp_path / "c"))
    reset_default_cache_for_tests(cache)
    asr_mod.set_engine_transcriber_factory(factory)
    media = tmp_path / "a.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    try:
        with patch("classes.speech.asr.resolve_engine",
                   lambda engine="auto": "apple" if engine == "auto" else engine), \
             patch("classes.speech.asr._local_engine", lambda: "whistle"), \
             patch("classes.speech.asr.extract_mono_16k_wav", return_value=(str(media), "")):
            rec = asr_mod.transcribe_file(str(media), engine="auto", force=True)
    finally:
        reset_default_cache_for_tests(None)
    assert seen == ["apple", "whistle"]
    assert rec.transcriptionSource == "local" and rec.modelId == "whistle"
    assert rec.words[0].text == "hi"


def test_no_whisper_is_shipped():
    """Releases bundle no whisper.cpp / faster-whisper; the captions recogniser is downloaded on demand."""
    import importlib.util
    assert importlib.util.find_spec("classes.speech.whisper_cpp") is None
    assert not hasattr(asr_mod, "FasterWhisperTranscriber")
    assert not os.path.exists(os.path.join(REPO, "installer", "build-whisper-cli.sh"))
    for rel in (".github/workflows/release.yml", "freeze.py", "requirements.txt", "requirements-speech.txt"):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            assert "whisper" not in fh.read().lower(), rel


def test_server_instructions_still_ok():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    assert "get_transcript_tool" in SERVER_INSTRUCTIONS
    assert "whisper.cpp" not in SERVER_INSTRUCTIONS
