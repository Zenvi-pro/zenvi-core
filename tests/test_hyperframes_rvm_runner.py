"""hyperframes_rvm.cjs end to end with a stand-in ONNX runtime: frames in, a transparent WebM out.

The stand-in mimics onnxruntime-node's API and can make the GPU fail at run() time, the way
DirectML and CoreML do when a session opens but a kernel is missing.
"""

import json
import os
import shutil
import subprocess

import pytest

from classes import hyperframes_local as hf

NODE = shutil.which("node")
FFMPEG = hf.find_program("ffmpeg")
pytestmark = pytest.mark.skipif(not NODE or not FFMPEG, reason="needs node and ffmpeg")

FAKE_ORT = r"""
class Tensor {
  constructor(type, data, dims) { this.type = type; this.data = data; this.dims = dims; }
  dispose() { this.disposed = true; }
}
class Session {
  constructor(providers) { this.providers = providers; }
  async run(feeds) {
    if (this.providers[0] !== "cpu" && process.env.GPU_FAILS_AT_RUN) throw new Error("no DML kernel");
    const [, , h, w] = feeds.src.dims;
    const pha = new Float32Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) pha[y * w + x] = x < w / 2 ? 1 : 0;
    const t = (d) => new Tensor("float32", d, [1]);
    return { pha: new Tensor("float32", pha, [1, 1, h, w]), fgr: t(new Float32Array(1)),
             r1o: t(new Float32Array(1)), r2o: t(new Float32Array(1)), r3o: t(new Float32Array(1)),
             r4o: t(new Float32Array(1)) };
  }
}
module.exports = { Tensor, InferenceSession: { create: async (model, opts) => new Session(opts.executionProviders) } };
"""


def _run(tmp_path, env=None, size="33x21", rate="25"):
    ort = tmp_path / "ort"
    ort.mkdir(exist_ok=True)
    (ort / "index.js").write_text(FAKE_ORT)
    clip = tmp_path / "in.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=%s:r=%s:d=0.6" % (size, rate),
                    "-pix_fmt", "yuv444p", str(clip)], check=True)
    out = tmp_path / "out.webm"
    proc = subprocess.run([NODE, hf.RVM_RUNNER, str(ort), "model.onnx", FFMPEG, str(clip), str(out)],
                          capture_output=True, text=True, timeout=120, env=dict(os.environ, **(env or {})))
    return proc, out


def test_every_frame_is_cut_out_into_a_transparent_webm(tmp_path):
    proc, out = _run(tmp_path)                                    # odd width and height: padded to even
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["frames"] == 15
    probe = subprocess.run([FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-count_frames", "-show_entries",
                            "stream=width,height,nb_read_frames:stream_tags=alpha_mode", "-of", "json", str(out)],
                           capture_output=True, text=True).stdout
    stream = json.loads(probe)["streams"][0]
    assert (stream["width"], stream["height"], stream["nb_read_frames"]) == (34, 22, "15")
    assert stream["tags"].get("alpha_mode", stream["tags"].get("ALPHA_MODE")) == "1"


def test_a_gpu_that_fails_at_run_time_falls_back_to_the_cpu(tmp_path):
    proc, _out = _run(tmp_path, env={"GPU_FAILS_AT_RUN": "1"})
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["frames"] == 15 and report["provider"] == "cpu"


def test_an_unreadable_clip_fails_cleanly(tmp_path):
    (tmp_path / "ort").mkdir()
    (tmp_path / "ort" / "index.js").write_text(FAKE_ORT)
    (tmp_path / "in.mp4").write_bytes(b"not a video")
    proc = subprocess.run([NODE, hf.RVM_RUNNER, str(tmp_path / "ort"), "m.onnx", FFMPEG, str(tmp_path / "in.mp4"),
                           str(tmp_path / "out.webm")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and proc.stdout == ""
