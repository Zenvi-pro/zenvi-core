// Cut the subject out of a video with Robust Video Matting (MobileNetV3, ONNX).
//
// Run by classes/hyperframes_local.py with the Node.js HyperFrames uses:
//   node hyperframes_rvm.cjs <onnxruntime-node dir> <model.onnx> <ffmpeg> <input> <output.webm>
// Writes the same transparent VP9 WebM `hyperframes remove-background` does, and prints one JSON
// line ({frames, provider, ms_per_frame}). RVM keeps a memory of the previous frames, so the matte
// stays steady from frame to frame. The ONNX runtime is the one HyperFrames installs; its GPU
// back ends are used where they exist (DirectML on Windows for any GPU, CoreML on Apple Silicon,
// CUDA on Linux when its libraries are installed), else the CPU.
"use strict";
const { spawn, execFileSync } = require("child_process");
const os = require("os");
const path = require("path");

const [ortDir, modelPath, ffmpeg, input, output] = process.argv.slice(2);
const ort = require(ortDir);

function providers() {
  const gpu = { win32: "dml", darwin: os.arch() === "arm64" ? "coreml" : null, linux: "cuda" }[process.platform];
  return gpu ? [gpu, "cpu"] : ["cpu"];
}

async function openSession() {
  const options = { intraOpNumThreads: Math.min(8, os.cpus().length), graphOptimizationLevel: "all" };
  for (const list of [providers(), ["cpu"]]) {
    try {
      return { session: await ort.InferenceSession.create(modelPath, { ...options, executionProviders: list }),
               provider: list[0] };
    } catch (err) {
      if (list[0] === "cpu") throw err;                     // a GPU that is not there: try the CPU
    }
  }
}

function probe(file) {
  const ffprobe = path.join(path.dirname(ffmpeg), path.basename(ffmpeg).replace(/ffmpeg/i, "ffprobe"));
  const [w, h, fps] = execFileSync(ffprobe, ["-v", "error", "-select_streams", "v:0", "-show_entries",
    "stream=width,height,r_frame_rate", "-of", "csv=p=0", file]).toString().trim().split(",");
  return { w: +w, h: +h, fps };
}

(async () => {
  const { w, h, fps } = probe(input);
  const { session, provider } = await openSession();
  // RVM's guidance: work on a copy whose long side is about 512 px (0.25 for 1080p, 0.125 for 4K).
  const ratio = Math.max(0.125, Math.min(1, 512 / Math.max(w, h)));
  const dec = spawn(ffmpeg, ["-v", "error", "-i", input, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]);
  const enc = spawn(ffmpeg, ["-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", `${w}x${h}`, "-r", fps,
    "-i", "-", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "18", "-deadline", "good", "-row-mt", "1",
    "-cpu-used", "4", "-auto-alt-ref", "0", "-pix_fmt", "yuva420p", output]);
  let encError = "";
  enc.stderr.on("data", (d) => { encError += d; });
  const frameBytes = w * h * 3, n = w * h;
  const queue = [];
  let pending = Buffer.alloc(0), decoded = false, wake = null;
  dec.stdout.on("data", (chunk) => {
    pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
    while (pending.length >= frameBytes) {
      queue.push(pending.subarray(0, frameBytes));
      pending = pending.subarray(frameBytes);
    }
    if (queue.length > 8) dec.stdout.pause();                // do not hold the whole clip in memory
    if (wake) { wake(); wake = null; }
  });
  dec.on("close", () => { decoded = true; if (wake) { wake(); wake = null; } });

  let rec = [0, 1, 2, 3].map(() => new ort.Tensor("float32", new Float32Array([0]), [1, 1, 1, 1]));
  const downsample = new ort.Tensor("float32", new Float32Array([ratio]), [1]);
  const src = new Float32Array(3 * n);
  const started = Date.now();
  let frames = 0;
  for (;;) {
    if (!queue.length) {
      if (decoded) break;
      dec.stdout.resume();
      await new Promise((resolve) => { wake = resolve; });
      continue;
    }
    const rgb = queue.shift();
    if (queue.length < 4) dec.stdout.resume();
    for (let i = 0, j = 0; i < n; i++, j += 3) {           // RGB bytes -> planar floats 0..1
      src[i] = rgb[j] / 255; src[n + i] = rgb[j + 1] / 255; src[2 * n + i] = rgb[j + 2] / 255;
    }
    const out = await session.run({ src: new ort.Tensor("float32", src, [1, 3, h, w]),
      r1i: rec[0], r2i: rec[1], r3i: rec[2], r4i: rec[3], downsample_ratio: downsample });
    rec = [out.r1o, out.r2o, out.r3o, out.r4o];
    const alpha = out.pha.data, rgba = Buffer.allocUnsafe(4 * n);
    for (let i = 0, j = 0, k = 0; i < n; i++, j += 3, k += 4) {
      rgba[k] = rgb[j]; rgba[k + 1] = rgb[j + 1]; rgba[k + 2] = rgb[j + 2];
      const a = alpha[i] * 255;
      rgba[k + 3] = a <= 0 ? 0 : a >= 255 ? 255 : a + 0.5;
    }
    if (!enc.stdin.write(rgba)) await new Promise((resolve) => enc.stdin.once("drain", resolve));
    frames++;
  }
  enc.stdin.end();
  const code = await new Promise((resolve) => enc.on("close", resolve));
  if (dec.exitCode || code || !frames) {
    throw new Error(`ffmpeg could not ${frames ? "write the cut-out" : "read the clip"}: ${encError.trim().slice(-400)}`);
  }
  console.log(JSON.stringify({ frames, provider, ms_per_frame: Math.round((Date.now() - started) / frames) }));
})().catch((err) => {
  console.error(String((err && err.message) || err));
  process.exit(1);
});
