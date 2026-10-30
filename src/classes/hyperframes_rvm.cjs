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
const LOG_LIMIT = 4000;

function gpuProvider() {
  return { win32: "dml", darwin: os.arch() === "arm64" ? "coreml" : null, linux: "cuda" }[process.platform] || null;
}

async function openSession(provider) {
  return ort.InferenceSession.create(modelPath, {
    intraOpNumThreads: Math.min(8, os.cpus().length), graphOptimizationLevel: "all",
    executionProviders: provider === "cpu" ? ["cpu"] : [provider, "cpu"],
  });
}

function probe(file) {
  const ffprobe = path.join(path.dirname(ffmpeg), path.basename(ffmpeg).replace(/ffmpeg/i, "ffprobe"));
  const [w, h, rate] = execFileSync(ffprobe, ["-v", "error", "-select_streams", "v:0", "-show_entries",
    "stream=width,height,avg_frame_rate", "-of", "csv=p=0", file]).toString().trim().split(",");
  // avg_frame_rate, not r_frame_rate: on variable-rate video r_frame_rate is a timebase-like
  // multiple (e.g. 600/1) and the cut-out would come out the wrong length.
  return { w: +w, h: +h, fps: rate && !/^0\//.test(rate) ? rate : "30" };
}

function dispose(...tensors) {
  for (const t of tensors) if (t && typeof t.dispose === "function") t.dispose();
}

(async () => {
  const { w, h, fps } = probe(input);
  const n = w * h, frameBytes = n * 3;
  let provider = gpuProvider() || "cpu", session;
  try {
    session = await openSession(provider);
  } catch (err) {
    provider = "cpu";                                       // a GPU that is not there
    session = await openSession(provider);
  }
  // RVM's guidance: work on a copy whose long side is about 512 px (0.25 for 1080p, 0.125 for 4K).
  const ratio = Math.max(0.125, Math.min(1, 512 / Math.max(w, h)));

  // -noautorotate: the pixels keep the probed (stored) size; the trim before this has already
  // turned any rotated phone video upright.
  const dec = spawn(ffmpeg, ["-v", "error", "-noautorotate", "-i", input, "-an", "-f", "rawvideo",
    "-pix_fmt", "rgb24", "-"]);
  // yuva420p needs even sides: pad an odd one by a pixel of transparency.
  const enc = spawn(ffmpeg, ["-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", `${w}x${h}`, "-r", fps,
    "-i", "-", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:color=black@0", "-c:v", "libvpx-vp9", "-b:v", "0",
    "-crf", "18", "-deadline", "good", "-row-mt", "1", "-cpu-used", "4", "-auto-alt-ref", "0",
    "-pix_fmt", "yuva420p", output]);
  let log = "";
  const keep = (d) => { log = (log + d).slice(-LOG_LIMIT); };    // read both, or a chatty ffmpeg blocks
  dec.stderr.on("data", keep);
  enc.stderr.on("data", keep);
  enc.stdout.resume();
  const encClosed = new Promise((resolve) => enc.on("close", resolve));
  enc.stdin.on("error", () => {});                         // reported through the exit code below

  // Frames are assembled straight into their own buffers; at most a few wait for the model.
  const queue = [];
  let frame = Buffer.allocUnsafe(frameBytes), filled = 0, decoded = false, wake = null;
  const poke = () => { if (wake) { const go = wake; wake = null; go(); } };
  dec.stdout.on("data", (chunk) => {
    for (let at = 0; at < chunk.length;) {
      const take = Math.min(frameBytes - filled, chunk.length - at);
      chunk.copy(frame, filled, at, at + take);
      filled += take; at += take;
      if (filled === frameBytes) { queue.push(frame); frame = Buffer.allocUnsafe(frameBytes); filled = 0; }
    }
    if (queue.length > 4) dec.stdout.pause();
    poke();
  });
  const decClosed = new Promise((resolve) => dec.on("close", (code) => { decoded = true; poke(); resolve(code); }));

  let rec = [0, 1, 2, 3].map(() => new ort.Tensor("float32", new Float32Array([0]), [1, 1, 1, 1]));
  const downsample = new ort.Tensor("float32", new Float32Array([ratio]), [1]);
  const src = new Float32Array(3 * n), rgba = Buffer.allocUnsafe(4 * n);
  const started = Date.now();
  let frames = 0, encoderGone = false;
  encClosed.then(() => { encoderGone = true; poke(); });
  while (!encoderGone) {
    if (!queue.length) {
      if (decoded) break;
      dec.stdout.resume();
      await new Promise((resolve) => { wake = resolve; });
      continue;
    }
    const rgb = queue.shift();
    if (queue.length < 2) dec.stdout.resume();
    for (let i = 0, j = 0; i < n; i++, j += 3) {           // RGB bytes -> planar floats 0..1
      src[i] = rgb[j] / 255; src[n + i] = rgb[j + 1] / 255; src[2 * n + i] = rgb[j + 2] / 255;
    }
    const feeds = () => ({ src: new ort.Tensor("float32", src, [1, 3, h, w]),
      r1i: rec[0], r2i: rec[1], r3i: rec[2], r4i: rec[3], downsample_ratio: downsample });
    let out;
    try {
      out = await session.run(feeds());
    } catch (err) {
      if (provider === "cpu" || frames > 0) throw err;      // a GPU that opened but cannot run: the CPU
      provider = "cpu";
      session = await openSession(provider);
      out = await session.run(feeds());
    }
    const previous = rec;
    rec = [out.r1o, out.r2o, out.r3o, out.r4o];
    if (frames > 0) dispose(...previous);
    const alpha = out.pha.data;
    for (let i = 0, j = 0, k = 0; i < n; i++, j += 3, k += 4) {
      rgba[k] = rgb[j]; rgba[k + 1] = rgb[j + 1]; rgba[k + 2] = rgb[j + 2];
      const a = alpha[i] * 255;
      rgba[k + 3] = a <= 0 ? 0 : a >= 255 ? 255 : a + 0.5;
    }
    dispose(out.pha, out.fgr);
    if (!enc.stdin.write(rgba)) {
      await Promise.race([new Promise((resolve) => enc.stdin.once("drain", resolve)), encClosed]);
    }
    frames++;
  }
  enc.stdin.end();
  const [decCode, encCode] = [await decClosed, await encClosed];
  if (decCode || encCode || !frames || encoderGone && queue.length) {
    throw new Error(`ffmpeg could not ${frames ? "write the cut-out" : "read the clip"}: ${log.trim().slice(-400)}`);
  }
  console.log(JSON.stringify({ frames, provider, ms_per_frame: Math.round((Date.now() - started) / frames) }));
})().catch((err) => {
  console.error(String((err && err.message) || err));
  process.exit(1);
});
