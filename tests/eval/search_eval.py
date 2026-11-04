"""Search quality gate: index a labelled set through the real pipeline and score the product's search on it.

A set is a folder holding ``labels.json`` and ``videos/<name>.mp4``. ``hard_set_fetch.py`` and ``hard_set_build.py`` make
the hard set (confusable stock clips cut into edits, narration at known times, continuous takes, 46 labelled queries
plus 6 that match nothing). Labels: ``{"videos": {name: ...}, "queries": [{"text", "kind", "video", "spans"}], "negative": [...]}``.

    python tests/eval/search_eval.py <set_dir> [--model embeddinggemma-2|gemini-embedding-2] [--out report.json]

Reports right video first, right moment first (the top hit's peak inside a labelled span, give or take a second), right
moment in the top 5, and how many no-match queries return nothing. Exits 1 below the gate: right video first 0.95,
right moment first 0.90. The local model runs on this computer (no cost); the cloud model needs ZENVI_BACKEND_URL and a
signed-in session, and is billed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

GATE = {"video@1": 0.95, "moment@1": 0.90}
TOLERANCE = 1.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set_dir")
    ap.add_argument("--model", default="embeddinggemma-2")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    from classes.media_fingerprint import fingerprint
    from classes.media_index import embedder as E, facts, library, search, vectors
    from classes.media_index.probe import probe_media
    from classes.media_index.store import Shelf, sha_of

    root = Path(args.set_dir)
    labels = json.loads((root / "labels.json").read_text())
    shelf = Shelf(tempfile.mkdtemp(prefix="zenvi_eval_shelf_"))
    emb = E.get_embedder(args.model)
    files = []
    for name in labels["videos"]:
        path = str(root / "videos" / f"{name}.mp4")
        fp = fingerprint(path)
        sha = sha_of(fp)
        facts.compute_facts(path, fingerprint=fp, shelf=shelf, media_type="video")
        vectors.compute_vectors(path, probe_media(path), sha, shelf, emb)
        files.append(library.get_file_index(shelf, sha, file_id=name, name=name, path=path, media_type="video"))
        print("indexed", name, flush=True)

    def ask(text):
        return search.search(files, query_vector={args.model: emb.texts([text], query=True)[0]}, query_text=text, limit=5)["hits"]

    rows = []
    for q in labels["queries"]:
        hits = ask(q["text"])
        right = [h["file_id"] == q["video"] and any(a - TOLERANCE <= h["peak"] <= b + TOLERANCE for a, b in q["spans"]) for h in hits]
        rows.append({"query": q["text"], "kind": q["kind"], "video@1": bool(hits) and hits[0]["file_id"] == q["video"],
                     "moment@1": bool(right) and right[0], "moment@5": any(right),
                     "top": [(h["file_id"], h["peak"], h["matched"], h["score"]) for h in hits[:3]]})
    quiet = [not ask(t) for t in labels.get("negative") or []]
    report = {k: round(float(np.mean([r[k] for r in rows])), 3) for k in ("video@1", "moment@1", "moment@5")}
    report.update({"model": args.model, "device": getattr(emb, "device", ""), "queries": len(rows),
                   "no_match_answered_with_nothing": round(float(np.mean(quiet)), 3) if quiet else None, "per_query": rows})
    print(json.dumps({k: v for k, v in report.items() if k != "per_query"}, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    failed = [k for k, floor in GATE.items() if report[k] < floor]
    if failed:
        print("below the gate:", ", ".join(f"{k} {report[k]} < {GATE[k]}" for k in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
