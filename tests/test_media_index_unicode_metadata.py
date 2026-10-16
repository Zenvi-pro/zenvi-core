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
