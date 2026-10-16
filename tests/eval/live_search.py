"""Live search calibration: do the score cut-offs in ``classes/media_index/search.py`` separate right answers from wrong ones?

Builds a small set of clips with known content (drawn scenes with text and shapes, plus any real clips named in
``--real``), indexes them through the real editor pipeline against an IN-PROCESS copy of the backend (real Gemini, no
credits, no network service), embeds a set of queries with known answers, and reports how the raw cosine scores of right
and wrong answers are spread per channel (shot description, spoken words, keyframe picture).

Costs real Gemini calls. Every call goes through ``spend_guard`` (a hard cap, default $5 for the whole round).

    GOOGLE_API_KEY comes from the backend's own settings.
    ZENVI_BACKEND_DIR=/path/to/zenvi-backend  FFMPEG_BIN_DIR=... PYTHONPATH=... \\
        python tests/eval/live_search.py --out search_report.json [--real a.mp4 b.mp4]

Nothing here uploads anything but the proxies of the clips it names. Keep personal footage out of ``--real``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from eval import corpus, spend_guard  # noqa: E402

# (name, what is drawn, queries that should find it)
DRAWN = {
    "apple": ("circle", {"bg": (245, 245, 245), "fill": (205, 30, 30), "text": ""}, ["a red apple", "a red circle on a white background"]),
    "sun": ("sun", {"bg": (70, 140, 230), "fill": (255, 215, 40), "text": ""}, ["the sun in a blue sky"]),
    "paris": ("text", {"bg": (30, 60, 160), "fill": (255, 255, 255), "text": "WELCOME TO PARIS"}, ["a sign that says Welcome to Paris"]),
    "sale": ("text", {"bg": (190, 20, 20), "fill": (255, 255, 255), "text": "SALE 50% OFF"}, ["a sale sign, fifty percent off"]),
    "square": ("square", {"bg": (120, 120, 120), "fill": (30, 170, 60), "text": ""}, ["a green square"]),
    "triangle": ("triangle", {"bg": (0, 0, 0), "fill": (250, 220, 40), "text": ""}, ["a yellow triangle on a black background"]),
    "checker": ("checker", {"bg": (0, 0, 0), "fill": (255, 255, 255), "text": ""}, ["a black and white checkerboard"]),
    "sunset": ("gradient", {"bg": (255, 120, 40), "fill": (90, 30, 140), "text": ""}, ["an orange and purple sunset sky"]),
    "open": ("text", {"bg": (0, 0, 0), "fill": (60, 255, 80), "text": "OPEN 24 HOURS"}, ["a neon sign saying open 24 hours"]),
    "stripes": ("stripes", {"bg": (110, 40, 160), "fill": (230, 200, 250), "text": ""}, ["purple vertical stripes"]),
}
REAL = {
    "pond": ["a calm pond with trees", "still water in a park"],
    "plaza": ["an evening city plaza with lit buildings"],
    "talk": ["someone talking to the camera about the old town and its food", "welcome back to the channel"],
}
# Spoken clips: a sentence read aloud over a plain picture (macOS ``say``); the transcript comes from the local recogniser.
SPOKEN = {
    "sales": ("The quarterly sales figures exceeded our expectations by twelve percent.", ["a talk about company sales and money"]),
    "flour": ("Add two cups of flour and stir the batter slowly until it is smooth.", ["a cooking instruction about baking with flour"]),
    "directions": ("Turn left at the next intersection and continue past the gas station.", ["driving directions to a destination"]),
    "medicine": ("The patient was prescribed antibiotics and told to rest for a week.", ["a doctor talking about medical treatment"]),
    "rocket": ("Our rocket launches tomorrow at dawn from the northern coast.", ["a space launch announcement"]),
    "garden": ("Water the tomatoes every morning and pull the weeds before they spread.", ["advice about gardening and growing vegetables"]),
}
NEGATIVE_QUERIES = ["a dog running on a beach", "a surgeon in an operating room", "a race car on a track", "a snow covered mountain",
                    "a birthday cake with candles", "a person playing guitar on stage", "an airplane taking off"]


def draw(kind: str, spec: Dict[str, Any], size=(640, 360)):
    from PIL import Image, ImageDraw, ImageFont
    w, h = size
    img = Image.new("RGB", size, spec["bg"])
    d = ImageDraw.Draw(img)
    fill = spec["fill"]
    if kind == "circle":
        d.ellipse((w * 0.3, h * 0.2, w * 0.7, h * 0.85), fill=fill)
        d.rectangle((w * 0.48, h * 0.1, w * 0.52, h * 0.25), fill=(60, 120, 40))
    elif kind == "sun":
        d.ellipse((w * 0.62, h * 0.08, w * 0.88, h * 0.5), fill=fill)
    elif kind == "square":
        d.rectangle((w * 0.35, h * 0.2, w * 0.65, h * 0.8), fill=fill)
    elif kind == "triangle":
        d.polygon([(w * 0.5, h * 0.12), (w * 0.2, h * 0.88), (w * 0.8, h * 0.88)], fill=fill)
    elif kind == "checker":
        n = 8
        for i in range(n):
            for j in range(n):
                if (i + j) % 2 == 0:
                    d.rectangle((w * i / n, h * j / n, w * (i + 1) / n, h * (j + 1) / n), fill=fill)
    elif kind == "gradient":
        for y in range(h):
            t = y / h
            d.line([(0, y), (w, y)], fill=tuple(int(spec["bg"][c] * (1 - t) + fill[c] * t) for c in range(3)))
    elif kind == "stripes":
        for i in range(0, w, 64):
            d.rectangle((i, 0, i + 32, h), fill=fill)
    if spec.get("text"):
        font = ImageFont.load_default(size=56)
        box = d.textbbox((0, 0), spec["text"], font=font)
        d.text(((w - (box[2] - box[0])) / 2, (h - (box[3] - box[1])) / 2), spec["text"], fill=fill, font=font)
    return img


def make_clip(name: str, kind: str, spec: Dict[str, Any], seconds: float = 6.0) -> Path:
    def build(out):
        png = out + ".png"
        draw(kind, spec).save(png)
        corpus.run("-loop", "1", "-i", png, "-t", f"{seconds}", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", out)
        os.remove(png)
    return corpus.cached(f"live_{name}.mp4", build)


def make_spoken_clip(name: str, sentence: str, seconds_pad: float = 1.0) -> Path:
    def build(out):
        aiff = out + ".aiff"
        subprocess.run(["say", "-o", aiff, sentence], check=True)
        png = out + ".png"
        draw("square", {"bg": (60, 60, 70), "fill": (60, 60, 70), "text": ""}).save(png)
        corpus.run("-loop", "1", "-i", png, "-i", aiff, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-r", "25",
                   "-af", f"apad=pad_dur={seconds_pad}", "-t", "30", "-c:a", "aac", out)
        os.remove(png)
        os.remove(aiff)
    return corpus.cached(f"live_spoken_{name}.mp4", build)


class InProcessBackend:
    """The editor's view of the backend, answered by the backend's own modules with a spend-guarded Gemini client."""

    def __init__(self, backend_dir: str) -> None:
        sys.path.insert(0, backend_dir)
        os.chdir(backend_dir)
        from core.indexing.gemini_files import get_client
        self.client = spend_guard.GuardedClient(get_client(), "live_search")
        self.jobs: Dict[str, Dict[str, Any]] = {}
        from config import get_settings
        self.settings = get_settings()

    def _new_http_session(self):
        return None

    def v2_upload_session(self, file_id, filename, total_size, mime_type="video/mp4", session=None):
        from core.indexing.gemini_files import start_resumable_upload
        sess, err = start_resumable_upload(display_name=f"eval_{file_id}"[:100], mime_type=mime_type, num_bytes=int(total_size))
        return {"error": err} if err or not sess else {"upload_url": sess["upload_url"], "mime_type": sess["mime_type"]}

    def v2_understand(self, file_name, file_uri, shots, transcript, mime_type="video/mp4", media_type="video", session=None):
        from core.indexing import understand
        from core.indexing.gemini_files import delete_files, wait_file_active
        info, err = wait_file_active(file_name)
        job_id = str(uuid.uuid4())
        if err or not info:
            self.jobs[job_id] = {"status": "failed", "result": {"error": err}}
        else:
            model = (self.settings.gemini_analyze_model or "gemini-3.5-flash-lite").strip()
            result = understand.understand_file(self.client, model, info.get("uri") or file_uri, mime_type, shots, transcript)
            self.jobs[job_id] = {"status": "done", "result": {"video_id": job_id, **result}}
        delete_files([file_name])
        return {"job_id": job_id, "status": "started"}

    def v2_job(self, job_id, session=None):
        return self.jobs.get(job_id, {"status": "not_found", "result": None})

    def v2_embed(self, items, dims=768, task_type=None, session=None):
        from core.indexing import embed_v2
        model = (self.settings.gemini_embedding_model or "gemini-embedding-2").strip()
        return embed_v2.embed_items(self.client, model, items, dims, task_type)


def index_clip(backend: InProcessBackend, shelf, path: Path, name: str, speech: bool) -> str:
    from classes.media_fingerprint import fingerprint
    from classes.media_index import cloud, facts
    from classes.media_index.probe import probe_media
    from classes.media_index.store import sha_of
    fp = fingerprint(str(path))
    sha = sha_of(fp)
    facts.compute_facts(str(path), fingerprint=fp, shelf=shelf, media_type="video", run_speech=speech)
    out = cloud.compute_cloud(backend, str(path), probe_media(str(path)), sha, shelf, file_id=name, media_type="video")
    if out.get("error"):
        raise RuntimeError(f"{name}: {out['error']}")
    return sha


def embed_queries(backend: InProcessBackend, queries: Sequence[str]) -> Dict[str, np.ndarray]:
    reply = backend.v2_embed([{"kind": "text", "text": q} for q in queries], dims=768, task_type="RETRIEVAL_QUERY")
    import base64
    return {q: np.frombuffer(base64.b64decode(v), dtype="<f2").astype(np.float32) for q, v in zip(queries, reply["vectors"]) if v}


def channel_scores(fi, q: np.ndarray) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if fi.text_matrix is not None:
        sims = fi.text_matrix @ q
        for kind, label in (("shot", "shot"), ("speech", "speech")):
            vals = [float(sims[i]) for i, r in enumerate(fi.text_rows) if r["kind"] == kind]
            if vals:
                out[label] = max(vals)
    if fi.image_matrix is not None:
        out["image"] = float((fi.image_matrix @ q).max())
    return out


def summarise(pos: List[float], neg: List[float]) -> Dict[str, Any]:
    if not pos or not neg:
        return {"positives": len(pos), "negatives": len(neg)}
    p, n = np.array(pos), np.array(neg)
    auc = float((p[:, None] > n[None, :]).mean() + 0.5 * (p[:, None] == n[None, :]).mean())
    return {"positives": len(pos), "negatives": len(neg), "pos_min": round(float(p.min()), 3), "pos_p10": round(float(np.percentile(p, 10)), 3),
            "pos_median": round(float(np.median(p)), 3), "neg_median": round(float(np.median(n)), 3), "neg_p95": round(float(np.percentile(n, 95)), 3),
            "neg_p99": round(float(np.percentile(n, 99)), 3), "neg_max": round(float(n.max()), 3), "auc": round(auc, 4)}


def at_floor(pos: List[float], neg: List[float], floor: float) -> Dict[str, float]:
    return {"floor": floor, "positives_kept": round(float(np.mean(np.array(pos) >= floor)), 3) if pos else None,
            "negatives_passing": round(float(np.mean(np.array(neg) >= floor)), 3) if neg else None}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="")
    ap.add_argument("--real", nargs="*", default=[], help="real clips to include, as name=path (names pond, plaza, talk have built-in queries)")
    args = ap.parse_args(argv)
    args.out = str(Path(args.out).resolve()) if args.out else ""          # the backend setup changes directory
    args.real = [f"{n}={Path(p).resolve()}" for n, _, p in (x.partition("=") for x in args.real)]
    backend_dir = os.environ.get("ZENVI_BACKEND_DIR")
    if not backend_dir:
        print("set ZENVI_BACKEND_DIR to the backend checkout")
        return 2
    from classes.media_index import library, schema as S
    from classes.media_index.store import Shelf
    print(spend_guard.status())
    backend = InProcessBackend(backend_dir)
    shelf = Shelf(tempfile.mkdtemp(prefix="zenvi_live_shelf_"))
    clips: Dict[str, Tuple[Path, List[str]]] = {n: (make_clip(n, k, s), q) for n, (k, s, q) in DRAWN.items()}
    if sys.platform == "darwin":
        for name, (sentence, qs) in SPOKEN.items():
            clips["say_" + name] = (make_spoken_clip(name, sentence), qs)
    for item in args.real:
        name, _, path = item.partition("=")
        clips[name] = (Path(path), REAL.get(name, []))
    sha_of: Dict[str, str] = {}
    for name, (path, _) in clips.items():
        sha_of[name] = index_clip(backend, shelf, path, name, speech=(name == "talk" or name.startswith("say_")))
        print("indexed", name, spend_guard.status(), flush=True)
    files = {name: library.get_file_index(shelf, sha, file_id=name, name=name, path=str(clips[name][0]), media_type="video") for name, sha in sha_of.items()}
    queries = {q: n for n, (_, qs) in clips.items() for q in qs}
    vectors = embed_queries(backend, list(queries) + NEGATIVE_QUERIES)
    pos: Dict[str, List[float]] = {"shot": [], "speech": [], "image": []}
    neg: Dict[str, List[float]] = {"shot": [], "speech": [], "image": []}
    ranks: List[int] = []
    for q, qv in vectors.items():
        target = queries.get(q)
        scored = {name: channel_scores(fi, qv) for name, fi in files.items()}
        for name, ch in scored.items():
            for label, val in ch.items():
                (pos if name == target else neg)[label].append(val)
        if target:
            best = {name: max(ch.values()) for name, ch in scored.items() if ch}
            order = sorted(best, key=best.get, reverse=True)
            ranks.append(order.index(target) + 1)
    report: Dict[str, Any] = {"clips": len(clips), "queries": len(queries), "negative_queries": len(NEGATIVE_QUERIES),
                              "first_hit_rank_mean": round(float(np.mean(ranks)), 3), "top1": round(float(np.mean([r == 1 for r in ranks])), 3),
                              "top3": round(float(np.mean([r <= 3 for r in ranks])), 3), "channels": {}}
    for label in ("shot", "speech", "image"):
        report["channels"][label] = {**summarise(pos[label], neg[label]), "current": at_floor(pos[label], neg[label], S and __import__("classes.media_index.search", fromlist=["x"]).MIN_COSINE[label])}
    for label in ("shot", "speech", "image"):
        report["channels"][label]["sweep"] = [at_floor(pos[label], neg[label], f) for f in (0.30, 0.34, 0.36, 0.40, 0.44, 0.48, 0.50, 0.52, 0.55, 0.58)]
        report["channels"][label]["raw"] = {"positives": [round(v, 4) for v in pos[label]], "negatives": [round(v, 4) for v in neg[label]]}
    report["spend"] = spend_guard.status()
    text = json.dumps(report, indent=1)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
