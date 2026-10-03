"""The bundled whisper.cpp engine (PR #216 review, #35).

faster-whisper cannot be installed in the MSYS2 Python the Windows build uses,
so releases ship whisper.cpp's whisper-cli with a quantized base model; the
transcript tools use it whenever it is there.
"""

import json
import os
import sys

import pytest

from classes import info
from classes.speech import asr, whisper_cpp
from classes.speech.runtime import CancelToken

FAKE_CLI = r'''
import json, os, sys
args = sys.argv[1:]
out = args[args.index("-of") + 1]
with open(os.environ["FAKE_WHISPER_ARGS"], "w") as fh:
    json.dump(args, fh)
if os.environ.get("FAKE_WHISPER_SLEEP"):
    import time
    time.sleep(30)
if os.environ.get("FAKE_WHISPER_FAIL"):
    # What whisper-cli v1.9.4 really prints for a bad model.
    sys.stderr.write("whisper_model_load: invalid model data (bad magic)\n"
                     "whisper_init_with_params_no_state: failed to load model\n"
                     "error: failed to initialize whisper context\n")
    sys.exit(3)
segments = [
    {"offsets": {"from": 0, "to": 220}, "text": ""},
    {"offsets": {"from": 220, "to": 330}, "text": " And"},
    {"offsets": {"from": 330, "to": 680}, "text": " so"},
    {"offsets": {"from": 700, "to": 900}, "text": " [BLANK_AUDIO]"},
    {"offsets": {"from": 900, "to": 1170}, "text": " Americans,"},
]
with open(out + ".json", "w", encoding="utf-8") as fh:
    json.dump({"result": {"language": "fr"}, "transcription": segments}, fh)
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """A whisper-cli stand-in plus a model file, found the way a release finds them."""
    bundle = tmp_path / "app" / "whisper"
    bundle.mkdir(parents=True)
    script = bundle / "fake_whisper_cli.py"
    script.write_text(FAKE_CLI)
    if os.name == "nt":
        cli = bundle / "whisper-cli.cmd"
        cli.write_text('@"%s" "%s" %%*\n' % (sys.executable, script))
    else:
        cli = bundle / "whisper-cli"
        cli.write_text("#!%s\n" % sys.executable + FAKE_CLI)
        cli.chmod(0o755)
    (bundle / whisper_cpp.MODEL_FILE).write_bytes(b"ggml")
    (bundle / whisper_cpp.VAD_FILE).write_bytes(b"vad")
    monkeypatch.setattr(info, "PATH", str(tmp_path / "app"))
    monkeypatch.setattr(whisper_cpp, "_CLI_NAMES", (cli.name,))
    monkeypatch.delenv("ZENVI_WHISPER_CLI", raising=False)
    monkeypatch.delenv("ZENVI_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("ZENVI_WHISPER_VAD", raising=False)
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_WHISPER_ARGS", str(args_file))
    return args_file


def test_the_bundled_engine_gives_word_timestamps(fake_cli, tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    words, language = whisper_cpp.WhisperCppTranscriber().transcribe(
        str(wav), language=None, token=CancelToken())
    assert [(w.text, w.startSec, w.endSec) for w in words] == [
        ("And", 0.22, 0.33), ("so", 0.33, 0.68), ("Americans,", 0.9, 1.17)]
    assert language == "fr"
    args = json.loads(fake_cli.read_text())
    assert args[args.index("-l") + 1] == "auto"
    assert args[args.index("-m") + 1].endswith(whisper_cpp.MODEL_FILE)
    assert "-sow" in args  # one segment per word
    # Voice activity detection: without it words are timed into silence and
    # silence is "transcribed" (hallucinated words the user could then edit).
    assert "--vad" in args and args[args.index("-vm") + 1].endswith(whisper_cpp.VAD_FILE)
    assert "-np" not in args, "-np also silences whisper's own error messages"


def test_real_whisper_cli_output_parses_into_words():
    """tests/fixtures/whisper_cli_output.json is real v1.9.4 output: 6 s of
    silence, the JFK sample, 6 s of silence."""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "whisper_cli_output.json")
    with open(path, encoding="utf-8") as fh:
        words, language = whisper_cpp.words_from_cli_json(json.load(fh), None)
    assert language == "en"
    assert [w.text for w in words][:4] == ["And", "so,", "my", "fellow"] and words[-1].text == "country."
    assert 6.0 < words[0].startSec < 7.0, "speech starts after the 6 s of silence"
    assert all(w.text and w.endSec >= w.startSec for w in words)
    assert [w.startSec for w in words] == sorted(w.startSec for w in words)


def test_a_requested_language_is_passed_and_failures_are_reported(fake_cli, tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    whisper_cpp.WhisperCppTranscriber().transcribe(str(wav), language="de", token=CancelToken())
    args = json.loads(fake_cli.read_text())
    assert args[args.index("-l") + 1] == "de"
    monkeypatch.setenv("FAKE_WHISPER_FAIL", "1")
    with pytest.raises(RuntimeError, match="failed to load model"):
        whisper_cpp.WhisperCppTranscriber().transcribe(str(wav), language=None, token=CancelToken())


def test_cancelling_stops_whisper_cli(fake_cli, tmp_path, monkeypatch):
    import threading
    import time

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "1")
    token = CancelToken()
    threading.Timer(1.0, token.cancel).start()
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        whisper_cpp.WhisperCppTranscriber().transcribe(str(wav), language=None, token=token)
    assert time.monotonic() - began < 10


def test_auto_uses_the_bundled_engine_and_its_own_cache_key(fake_cli, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(asr.make_transcriber("auto"), whisper_cpp.WhisperCppTranscriber)
    assert asr._cache_model_id("whisper", asr.DEFAULT_MODEL_ID) == whisper_cpp.MODEL_ID


def test_without_the_bundle_whisper_falls_back_to_faster_whisper(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(whisper_cpp, "_CLI_NAMES", ("no-such-whisper-cli",))
    monkeypatch.delenv("ZENVI_WHISPER_CLI", raising=False)
    monkeypatch.delenv("ZENVI_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert not whisper_cpp.available()
    assert isinstance(asr.make_transcriber("auto"), asr.FasterWhisperTranscriber)
    assert asr._cache_model_id("whisper", asr.DEFAULT_MODEL_ID) == asr.DEFAULT_MODEL_ID


@pytest.mark.skipif(not (os.environ.get("ZENVI_WHISPER_CLI") and os.environ.get("ZENVI_WHISPER_MODEL")
                         and os.environ.get("ZENVI_WHISPER_SAMPLE")),
                    reason="set ZENVI_WHISPER_CLI / _MODEL / _SAMPLE to run the real binary")
def test_the_real_binary_transcribes_speech():
    words, language = whisper_cpp.WhisperCppTranscriber().transcribe(
        os.environ["ZENVI_WHISPER_SAMPLE"], language=None, token=CancelToken())
    text = " ".join(w.text for w in words).lower()
    assert language == "en" and "americans" in text and "country" in text
    assert all(w.endSec >= w.startSec for w in words)


def test_another_model_gets_its_own_cache_key(fake_cli, tmp_path, monkeypatch):
    """PR #275 review: ZENVI_WHISPER_MODEL=<other model> returned the transcript
    the bundled model had cached."""
    other = tmp_path / "ggml-small.bin"
    other.write_bytes(b"ggml")
    monkeypatch.setenv("ZENVI_WHISPER_MODEL", str(other))
    small = asr._cache_model_id("whisper", asr.DEFAULT_MODEL_ID)
    assert small.startswith("whisper.cpp-ggml-small-") and small == whisper_cpp.WhisperCppTranscriber().model_id
    # Another file under the bundled model's own name is another model too.
    same_name = tmp_path / "elsewhere" / whisper_cpp.MODEL_FILE
    same_name.parent.mkdir()
    same_name.write_bytes(b"not the bundled model")
    monkeypatch.setenv("ZENVI_WHISPER_MODEL", str(same_name))
    renamed = whisper_cpp.model_id()
    assert renamed not in (whisper_cpp.MODEL_ID, small)
    same_name.write_bytes(b"replaced with yet another model")
    assert whisper_cpp.model_id() != renamed


def test_a_stuck_whisper_cli_is_killed_at_the_time_limit(fake_cli, tmp_path, monkeypatch):
    """PR #275 review: a hung child held the one inference slot forever."""
    import time

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "1")
    monkeypatch.setattr(whisper_cpp, "_time_limit", lambda wav_path: 1.0)
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="did not finish"):
        whisper_cpp.WhisperCppTranscriber().transcribe(str(wav), language=None, token=CancelToken())
    assert time.monotonic() - began < 10


def test_the_time_limit_grows_with_the_audio(tmp_path):
    short = tmp_path / "short.wav"
    short.write_bytes(b"x" * 32000)  # 1 s of 16 kHz mono 16-bit
    hour = tmp_path / "hour.wav"
    with open(hour, "wb") as fh:
        fh.truncate(32000 * 3600)
    assert whisper_cpp._time_limit(str(short)) == 600
    assert whisper_cpp._time_limit(str(hour)) == 4 * 3600
