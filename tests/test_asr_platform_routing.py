"""Platform ASR routing: Mac→Apple when available, else Whisper; Windows→Whisper."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from classes.speech.asr import (
    APPLE_MODEL_ID,
    make_transcriber,
    resolve_engine,
    reset_transcriber_factory,
)
from classes.speech.cache import Word


def teardown_function():
    reset_transcriber_factory()


def test_resolve_engine_windows_is_whisper():
    with patch("classes.speech.asr.sys.platform", "win32"):
        assert resolve_engine("auto") == "whisper"
        assert resolve_engine("whisper") == "whisper"
        with pytest.raises(RuntimeError, match="macOS"):
            resolve_engine("apple")


def test_resolve_engine_mac_auto_uses_apple_when_helper_present():
    with patch("classes.speech.asr.sys.platform", "darwin"), \
         patch("classes.speech.apple_asr.apple_asr_available", return_value=True):
        assert resolve_engine("auto") == "apple"
        assert resolve_engine("apple") == "apple"
        assert resolve_engine("whisper") == "whisper"


def test_resolve_engine_mac_auto_falls_to_whisper_without_helper():
    with patch("classes.speech.asr.sys.platform", "darwin"), \
         patch("classes.speech.apple_asr.apple_asr_available", return_value=False):
        assert resolve_engine("auto") == "whisper"


def test_make_transcriber_apple():
    with patch("classes.speech.asr.sys.platform", "darwin"), \
         patch("classes.speech.apple_asr.apple_asr_available", return_value=True):
        from classes.speech.apple_asr import AppleSpeechTranscriber
        t = make_transcriber("apple")
        assert isinstance(t, AppleSpeechTranscriber)
        assert t.model_id == APPLE_MODEL_ID


def test_transcribe_file_apple_fallback_to_whisper(tmp_path):
    from classes.speech import asr as asr_mod
    from classes.speech.cache import TranscriptCache, reset_default_cache_for_tests

    class BoomApple:
        model_id = APPLE_MODEL_ID

        def transcribe(self, wav_path, *, language, token):
            raise RuntimeError("SpeechAnalyzer requires macOS 26+")

    class OkWhisper:
        model_id = "faster-whisper-base"

        def __init__(self, model_id=""):
            self.model_id = model_id or "faster-whisper-base"

        def transcribe(self, wav_path, *, language, token):
            return [Word("hi", 0.0, 0.2)], "en"

    def factory(engine, model_id):
        if engine == "apple":
            return BoomApple()
        return OkWhisper(model_id)

    cache = TranscriptCache(root=str(tmp_path / "c"))
    reset_default_cache_for_tests(cache)
    asr_mod.set_engine_transcriber_factory(factory)
    media = tmp_path / "a.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)

    with patch("classes.speech.asr.sys.platform", "darwin"), \
         patch("classes.speech.asr.resolve_engine", return_value="apple"), \
         patch("classes.speech.asr.extract_mono_16k_wav", return_value=(str(media), "")):
        rec = asr_mod.transcribe_file(str(media), engine="auto", force=True)

    assert rec.transcriptionSource == "local"  # whisper fallback
    assert rec.words[0].text == "hi"
    reset_default_cache_for_tests(None)
    reset_transcriber_factory()


def test_server_instructions_still_ok():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    assert "get_transcript_tool" in SERVER_INSTRUCTIONS
