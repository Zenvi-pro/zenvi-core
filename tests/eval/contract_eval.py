"""Offline process eval for the edit contract: does a whole piece start with a survey and a brief, and does the edit use the
footage's best moments? Pure Python, seeded, no ffmpeg, no network, a few seconds.

What it runs: the REAL executor (``agent_tools.execute``), the REAL ``begin_edit_tool``, the REAL contract and the REAL audit
(``media_index.review``), on synthetic projects, with *scripted* agents standing in for the model:

- ``eager``      places the bin's first shots at once, never surveys; after a refusal it tries once more, then gives up.
- ``recovering`` the same, but on a refusal it does what the refusal says (begin_edit_tool) and places from the survey.
- ``careful``    calls begin_edit_tool first.

What it can show: the *mechanism* (a held-back placement + a survey digest turns an eager trajectory into a survey-first, briefed,
strong-opening one; narrow asks are never touched; what it costs in tokens). What it cannot show: that a real model follows a
refusal as the ``recovering`` script does, or that the result is a better film. That needs the live comparison (``README`` in
this folder; opt-in, uses the user's own CLI or plan). Treat these numbers as the floor of the claim, not the claim.

Run: ``python -m eval.contract_eval`` from ``tests/`` (prints a Markdown table).
"""

from __future__ import annotations

import json
import os
import random
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import edit_contract as EC  # noqa: E402
from classes.agent_tools import execute as X  # noqa: E402
from classes.editor_tools import REGISTRY, media_index_tools_edit as TE  # noqa: E402
from classes.media_index import review as R  # noqa: E402

PLACE = "add_clips_to_timeline_tool"
WHOLE_PROMPTS = [
    "make something cool with my trip", "make a vlog from my footage", "edit my footage into a reel", "make a cinematic film from these clips",
    "cut a trailer", "make me a montage of the whole holiday", "turn this into a youtube short", "make a promo video", "surprise me",
    "edit my trip into something I can share",
]
NARROW_PROMPTS = ["make it warmer", "add captions", "trim the start of this clip", "make it louder", "remove the silence"]
ROLLOUTS = ("off", "warn", "enforce")
AGENTS = ("eager", "recovering", "careful")
PLACED_CLIPS = 8


# ------------------------------------------------------------------ synthetic projects
class FakeIndex:
    """A file index: only what the survey, the audit and begin_edit_tool read."""

    def __init__(self, sha, name, shots, minutes):
        self.sha, self.file_id, self.name, self.media_type, self.shots = sha, sha, name, "video", shots
        self.look_file, self.audio, self.sentences, self.captured_at, self.gps = None, {}, [], None, None
        self.duration, self.music_desc, self.orientation, self.rows, self.vector_model, self.layers = minutes * 60.0, None, "landscape", [], "", {}

    def shot_at(self, t):
        return next((s for s in self.shots if s["start"] <= t < s["end"]), None)


def make_project(seed: int, files: int = 12):
    """Footage where the best moments are NOT first in the bin (the usual case: the good stuff is scattered)."""
    rng = random.Random(seed)
    out = []
    for i in range(files):
        shots, t = [], 0.0
        for k in range(6):
            length = rng.uniform(4.0, 9.0)
            highlight = round(rng.uniform(0.10, 0.60), 2)
            shots.append({"id": k, "start": round(t, 2), "end": round(t + length, 2), "quality": {"highlight": highlight, "flags": [], "inferred": {"usable": True}}, "watch": {}})
            t += length
        out.append(FakeIndex(f"F{i:02d}", f"clip{i:02d}.mp4", shots, t / 60.0))
    for idx in rng.sample(range(2, files), 4):                       # a few strong moments, never in the first two files
        shot = rng.choice(out[idx].shots)
        shot["quality"]["highlight"] = round(rng.uniform(0.82, 0.97), 2)
    return out


def moments_of(project):
    moms = [{"file_id": f.file_id, "name": f.name, "start": s["start"], "end": s["end"], "highlight": s["quality"]["highlight"]} for f in project for s in f.shots]
    return sorted(moms, key=lambda m: -m["highlight"])


# ------------------------------------------------------------------ the world: the real executor over a fake project
class World:
    def __init__(self, project, rollout: str):
        self.project, self.rollout = project, rollout
        self.store = {"clips": [], "layers": [{"number": 1000000, "id": "t1"}], "markers": [], TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}}
        self.log, self.placed, self.digest_chars, self.first_applied_brief = [], [], [], None
        self._stack = ExitStack()

    def __enter__(self):
        app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: self.store.get(k, d)), updates=SimpleNamespace(
            actionHistory=[], update_untracked=lambda key, values: self.store.setdefault(key[0], {}).update(json.loads(json.dumps(values)))),
            thread=lambda: None)
        handlers = {"begin_edit_tool": REGISTRY["begin_edit_tool"].func, PLACE: self._place, "trim_clips_tool": lambda **k: "Trimmed.",
                    "color_grade_clip_tool": lambda **k: "Graded.", "add_captions_tool": lambda **k: "Captioned.",
                    "review_edit_tool": self._review, "export_video_tool": lambda **k: "Exported."}
        patches = [
            mock.patch.dict(os.environ, {"ZENVI_EDIT_CONTRACT": self.rollout}),
            mock.patch.object(X, "_HANDLERS", handlers), mock.patch.object(X, "_READ_ONLY", frozenset({"review_edit_tool"})),
            mock.patch.object(X, "_BACKGROUND_SAFE", frozenset()), mock.patch.object(X, "_UNGROUPED", frozenset({"review_edit_tool"})),
            mock.patch.object(X, "_GET_APP", lambda: app), mock.patch.object(X, "_RUN_ON_MAIN", lambda fn, timeout=None: fn()),
            mock.patch.object(X, "_ATOMIC", lambda a, h: h), mock.patch.object(X, "_QTHREAD", None),
            mock.patch("classes.agent_tools.schema.validate_args", lambda tool, args: None),
            mock.patch.object(TE, "get_app", lambda: app), mock.patch.object(TE, "on_main", lambda func, *a, **k: func(*a)),
            mock.patch.object(TE, "project_indexes", lambda file_ids=None: (self.project, [])),
        ]
        for p in patches:
            self._stack.enter_context(p)
        return self

    def __exit__(self, *exc):
        self._stack.close()

    # -- tools
    def _place(self, items=None, **_kw):
        for it in items or []:
            self.placed.append(dict(it))
        self.store["clips"] = [{"id": f"c{i}"} for i in range(len(self.placed))]
        return f"Placed {len(items or [])} clip(s)."

    def timeline(self):
        clips, t = [], 0.0
        for i, it in enumerate(self.placed):
            length = it["source_end"] - it["source_start"]
            clips.append(R.TimelineClip(id=f"c{i}", name=it["file_id"], file_id=it["file_id"], sha=it["file_id"], layer=1, kind="video", start=t, end=t + length,
                                        src_in=it["source_start"], src_out=it["source_end"]))
            t += length
        return clips

    def _review(self, **_kw):
        clips = self.timeline()
        by_file = {f.file_id: f for f in self.project}
        brief_d = self.store.get(TE.BRIEF_KEY) or {}
        result = R.review(clips, R.ProjectInfo(duration=clips[-1].end if clips else 0.0), R.Brief(form=brief_d.get("form", ""), target_seconds=brief_d.get("target_seconds")),
                          index_for=lambda c: by_file.get(c.file_id), best_moments=moments_of(self.project)[:12])
        return "Review.\n" + json.dumps({"needs": result["needs"], "unknown": result["unknown"], "layers": result["layers"]})

    # -- one call through the real executor
    def call(self, tool, **args):
        receipt = X.execute_tool_rich(tool, args).receipt
        self.log.append({"tool": tool, "status": receipt.status, "warned": bool(receipt.warnings), "summary": receipt.summary})
        if tool == "begin_edit_tool" and isinstance(receipt.data, dict) and receipt.data.get("survey_digest"):
            self.digest_chars.append(len(json.dumps(receipt.data["survey_digest"], ensure_ascii=False)))
        if tool == PLACE and receipt.status != "refused" and self.first_applied_brief is None:
            self.first_applied_brief = EC.brief_ready(self.store.get(TE.BRIEF_KEY))
        return receipt


def as_items(moments):
    return [{"file_id": m["file_id"], "source_start": m["start"], "source_end": m["end"]} for m in moments]


# ------------------------------------------------------------------ scripted agents
def bin_order(project):
    return [{"file_id": f.file_id, "start": f.shots[0]["start"], "end": f.shots[0]["end"]} for f in project][:PLACED_CLIPS]


def from_digest(receipt):
    digest = (receipt.data or {}).get("survey_digest") or {}
    return [{"file_id": m["file_id"], "start": m["start"], "end": m["end"]} for m in (digest.get("top_moments") or [])][:PLACED_CLIPS]


def finish(world):
    if world.placed:
        world.call("color_grade_clip_tool")
        world.call("review_edit_tool")
    world.call("export_video_tool")


def run_whole(agent, world, prompt):
    if agent == "careful":
        picks = from_digest(world.call("begin_edit_tool", user_ask=prompt)) or bin_order(world.project)
        world.call(PLACE, items=as_items(picks))
    else:
        r = world.call(PLACE, items=as_items(bin_order(world.project)))
        if r.status == "refused" and agent == "recovering":
            picks = from_digest(world.call("begin_edit_tool", user_ask=prompt))
            world.call(PLACE, items=as_items(picks))
        elif r.status == "refused":                          # eager: one more try, then it gives up
            world.call(PLACE, items=as_items(bin_order(world.project)))
    finish(world)


def run_narrow(world, prompt):
    for tool in ("trim_clips_tool", "color_grade_clip_tool", "add_captions_tool"):
        world.call(tool)
    world.call(PLACE, items=[])                              # a narrow edit may add a clip too
    world.call("export_video_tool")


# ------------------------------------------------------------------ one run and its metrics
def run_once(agent, rollout, prompt, seed, narrow=False):
    project = make_project(seed)
    best = moments_of(project)
    with World(project, rollout) as w:
        # What the desktop does with the user's message (AIChatWindow._note_edit_intent): the classifier alone starts the contract.
        started = (EC.contract_for_ask(w.store[EC.CONTRACT_KEY], prompt, timeline_empty=True, media_minutes=sum(f.duration for f in project) / 60.0)
                   if rollout != "off" else None)
        if started is not None:
            w.store[EC.CONTRACT_KEY] = started
        if narrow:
            run_narrow(w, prompt)
        else:
            run_whole(agent, w, prompt)
        applied = [e for e in w.log if e["tool"] == PLACE and e["status"] != "refused"]
        first_applied = next((i for i, e in enumerate(w.log) if e["tool"] == PLACE and e["status"] != "refused"), None)
        surveyed_first = first_applied is not None and any(e["tool"] == "begin_edit_tool" for e in w.log[:first_applied])
        clips = w.timeline()
        opening_rank = None
        top5 = best[:5]
        used = sum(1 for m in top5 if R._moment_used(m, clips)) if clips else 0
        if clips:
            first = clips[0]
            ranks = [i + 1 for i, m in enumerate(best) if m["file_id"] == first.file_id and abs(m["start"] - first.src_in) < 1e-6]
            opening_rank = ranks[0] if ranks else None
        return {
            "placed": bool(applied), "surveyed_first": surveyed_first, "brief_ok": bool(w.first_applied_brief),
            "refused": sum(1 for e in w.log if e["status"] == "refused"), "warned": sum(1 for e in w.log if e["warned"]),
            "opening_rank": opening_rank, "top5_used": used, "digest_chars": max(w.digest_chars or [0]),
            "export_warned": any(e["tool"] == "export_video_tool" and e["warned"] for e in w.log),
        }


def evaluate(seeds=range(3)):
    """{(agent, rollout): metrics averaged over every whole-piece prompt x seed}, plus the narrow-ask controls per rollout."""
    table, narrow = {}, {}
    for rollout in ROLLOUTS:
        for agent in AGENTS:
            runs = [run_once(agent, rollout, p, 1000 * s + i) for s in seeds for i, p in enumerate(WHOLE_PROMPTS)]
            n = len(runs)
            ranks = [r["opening_rank"] for r in runs if r["opening_rank"] is not None]
            table[(agent, rollout)] = {
                "n": n, "placed_rate": sum(r["placed"] for r in runs) / n, "survey_first_rate": sum(r["surveyed_first"] for r in runs) / n,
                "brief_at_first_placement_rate": sum(r["brief_ok"] for r in runs) / n, "refused_per_run": sum(r["refused"] for r in runs) / n,
                "warned_per_run": sum(r["warned"] for r in runs) / n, "mean_opening_rank": (sum(ranks) / len(ranks)) if ranks else None,
                "top5_used_mean": sum(r["top5_used"] for r in runs) / n, "digest_chars_max": max(r["digest_chars"] for r in runs),
                "export_warned_rate": sum(r["export_warned"] for r in runs) / n,
            }
        runs = [run_once("eager", rollout, p, 7000 + i, narrow=True) for i, p in enumerate(NARROW_PROMPTS) for _ in range(len(list(seeds)))]
        narrow[rollout] = {"n": len(runs), "refused_total": sum(r["refused"] for r in runs), "warned_total": sum(r["warned"] for r in runs)}
    return table, narrow


def markdown(table, narrow):
    rows = ["| agent | flag | placed | surveyed first | brief at 1st placement | refused/run | warned/run | opening rank (1=best) | top-5 moments used | export warned |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for (agent, rollout), m in table.items():
        rank = "-" if m["mean_opening_rank"] is None else f"{m['mean_opening_rank']:.1f}"
        rows.append(f"| {agent} | {rollout} | {m['placed_rate']:.0%} | {m['survey_first_rate']:.0%} | {m['brief_at_first_placement_rate']:.0%} | "
                    f"{m['refused_per_run']:.1f} | {m['warned_per_run']:.1f} | {rank} | {m['top5_used_mean']:.1f} / 5 | {m['export_warned_rate']:.0%} |")
    rows += ["", "Narrow-ask controls (trim, grade, captions, an empty add, export; the contract is `free`):", ""]
    rows += [f"- `{r}`: {v['n']} runs, {v['refused_total']} refusals, {v['warned_total']} warnings" for r, v in narrow.items()]
    digest = max(m["digest_chars_max"] for m in table.values())
    rows += ["", f"Largest survey digest handed to a model: {digest} characters (cap {EC.DIGEST_CAP_CHARS})."]
    return "\n".join(rows)


if __name__ == "__main__":
    t, n = evaluate()
    print(markdown(t, n))
