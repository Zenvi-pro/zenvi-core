// EmbeddingGemma 2 for the media index, run by classes/media_index/embedder.py.
// JSON lines in and out. First line out: {"ready", "device", "dtype"}. Then one reply per request:
//   {"id", "op": "embed_texts", "texts": [...]}   texts already carry their task prefix
//   {"id", "op": "embed_images", "paths": [...]}  keyframes cut at the size the processor reads
//   {"id", "op": "embed_audio", "paths": [...]}   raw 16 kHz mono float32 files
//   {"id", "op": "shutdown"}
// Reply: {"id", "vectors": [base64 little-endian float32, ...]} or {"id", "error"}.
import fs from "node:fs";
import readline from "node:readline";
import { env, AutoModel, AutoProcessor, RawImage } from "@huggingface/transformers";

const ID = "onnx-community/embeddinggemma-2-ONNX";
const REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0";
const DTYPE = "q4";
const SOFT_TOKENS = 70;
env.cacheDir = process.env.ZENVI_MODEL_DIR;
env.allowLocalModels = false;

const say = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const b64 = (t) => {
  const [n, d] = t.dims;
  return Array.from({ length: n }, (_, i) => Buffer.from(Float32Array.from(t.data.slice(i * d, (i + 1) * d)).buffer).toString("base64"));
};
const cos = (a, b) => {
  let s = 0, na = 0, nb = 0;
  for (let k = 0; k < a.length; k++) { s += a[k] * b[k]; na += a[k] * a[k]; nb += b[k] * b[k]; }
  return s / Math.sqrt(na * nb);
};

const processor = await AutoProcessor.from_pretrained(ID, { revision: REVISION });
processor.image_processor.max_soft_tokens = SOFT_TOKENS;
const load = (device) => AutoModel.from_pretrained(ID, { device, dtype: DTYPE, revision: REVISION,
  ...(device === "cpu" ? { session_options: { intraOpNumThreads: Number(process.env.ZENVI_EMBED_THREADS || 4), interOpNumThreads: 1 } } : {}) });
const embed = async (model, ...inputs) => (await model(await processor(...inputs))).sentence_embedding;

// The same text and picture embedded on the CPU when this file was written: a GPU whose answer differs is not used.
function testPicture() {
  const w = 384, h = 384, d = new Uint8ClampedArray(w * h * 3);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 3;
    d[i] = x * 255 / w; d[i + 1] = y * 255 / h; d[i + 2] = ((x >> 5) + (y >> 5)) % 2 ? 220 : 30;
  }
  return new RawImage(d, w, h, 3);
}
async function agrees(model) {
  const ref = JSON.parse(fs.readFileSync(new URL("./embed_reference.json", import.meta.url), "utf8"));
  const text = await embed(model, ["title: none | text: a dog runs along the beach at sunset"]);
  const image = await embed(model, null, [[testPicture()]]);
  return cos(Array.from(text.data), ref.text) > 0.99 && cos(Array.from(image.data), ref.image) > 0.99;
}

let model = null, device = "cpu";
if (process.env.ZENVI_EMBED_DEVICE !== "cpu") {
  try {
    model = await load("webgpu");
    if (await agrees(model)) device = "webgpu";
    else { await model.dispose(); model = null; }
  } catch { model = null; }
}
model = model || await load("cpu");
say({ ready: true, device, dtype: DTYPE });

for await (const line of readline.createInterface({ input: process.stdin })) {
  if (!line.trim()) continue;
  const req = JSON.parse(line);
  if (req.op === "shutdown") break;
  try {
    let out;
    if (req.op === "embed_texts") out = b64(await embed(model, req.texts));
    else if (req.op === "embed_images") {
      const images = await Promise.all(req.paths.map((p) => RawImage.read(p)));
      out = b64(await embed(model, null, images.map((x) => [x])));
    } else if (req.op === "embed_audio") {
      out = [];
      for (const p of req.paths) {
        const raw = fs.readFileSync(p);
        out.push(...b64(await embed(model, null, null, new Float32Array(raw.buffer, raw.byteOffset, raw.length / 4))));
      }
    } else throw new Error(`unknown op ${req.op}`);
    say({ id: req.id, vectors: out });
  } catch (e) {
    say({ id: req.id, error: String(e && e.message || e) });
  }
}
process.exit(0);
