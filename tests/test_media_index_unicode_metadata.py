"""Files whose metadata is not ASCII (a Japanese title tag) must still be probed and measured.

ffprobe and ffmpeg print the tags as UTF-8; reading their output with the machine's default text encoding crashed on an ASCII locale
(the app launched from a bare environment), so the file never got an index and the frame tools said "could not read the picture".
"""

import os
import shutil
import subprocess
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe")

_CHILD = """
import sys
sys.path.insert(0, {src!r})
from classes.media_index.probe import probe_media
from classes.media_index.audio import measure_loudness
p = sys.argv[1]
r = probe_media(p)
print("probe_ok=%s error=%s loudness=%s" % (r.get("ok"), r.get("error"), measure_loudness(p) is not None))
"""


def test_a_japanese_title_tag_does_not_break_probe_or_loudness(tmp_path):
    clip = tmp_path / "tagged.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-f", "lavfi", "-i", "sine=d=1",
                    "-metadata", "title=推しの子 セレナーデ", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(clip)], check=True)
    # a child process with an ASCII locale is what the bare-environment app looks like
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    out = subprocess.run([sys.executable, "-c", _CHILD.format(src=os.path.abspath(SRC)), str(clip)], env=env,
                         capture_output=True, text=True, timeout=120)
    assert "probe_ok=True" in out.stdout and "loudness=True" in out.stdout, out.stdout + out.stderr


_APPLE_CHILD = """
import sys
sys.path.insert(0, {src!r})
from classes.speech.apple_asr import AppleSpeechTranscriber
from classes.speech.runtime import CancelToken
words, language = AppleSpeechTranscriber().transcribe("unused.wav", language="ja-JP", token=CancelToken())
print("words=%d first=%d language=%s" % (len(words), ord(words[0].text), language))   # the code point: this child prints in ASCII too
"""


def test_the_speech_helpers_japanese_answer_is_read_as_utf8_under_an_ascii_locale(tmp_path):
    """The lyrics of a Japanese song came back as an 'ascii' decode error inside the app, so no lyrics were ever kept."""
    helper = tmp_path / "zenvi-apple-speech-asr"
    helper.write_text('#!/bin/sh\nprintf \'{"words":[{"text":"心","startSec":0.0,"endSec":0.5},{"text":"の","startSec":0.5,"endSec":0.9}],"language":"ja-JP"}\'\n', encoding="utf-8")
    helper.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "ZENVI_APPLE_SPEECH_ASR": str(helper)}
    out = subprocess.run([sys.executable, "-c", _APPLE_CHILD.format(src=os.path.abspath(SRC))], env=env, capture_output=True, text=True, timeout=60)
    assert "words=2 first=24515 language=ja-JP" in out.stdout, out.stdout + out.stderr
