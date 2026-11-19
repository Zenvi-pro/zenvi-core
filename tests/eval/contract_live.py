"""OPT-IN live comparison: does a REAL model follow the edit contract? Spends the user's own Claude plan.

A real model (``claude -p``, run with no tools of its own, no MCP and an empty working folder, so it can touch nothing) plays the
agent. Every tool call it makes goes through the real executor, the real ``begin_edit_tool``, the real contract and the real audit
on a seeded synthetic project (``contract_eval``). Three conditions, same prompts, same footage:

- ``today``    the contract is off, the old playbook step, no begin_edit_tool, no EDIT_STATE line;
- ``warn``     the shipped default: begin_edit_tool, the new playbook step and prompt sentence, EDIT_STATE, a warning on a skipped brief;
- ``enforce``  the same, and a placement before a brief is refused;
- ``enforce_nohint`` refused too, but the prompt carries NO hint (old playbook step, no sentence): only the refusal message can teach the model.

What it can show: whether a real model calls begin_edit_tool first, recovers from a refusal, opens on a strong moment, leaves narrow
asks alone, and what it costs. What it cannot: that the film is better (no human rater), or that the app's real tools behave the same
(tool descriptions for the fake tools are abbreviated; begin_edit_tool and the audit are the real ones).

Run (from ``tests/``): ``ZENVI_EVAL_LIVE=1 python -m eval.contract_live --model sonnet --parallel 4``. Without ZENVI_EVAL_LIVE=1 it refuses to run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
try:                                                # the suite's headless Qt stub, so the editor modules import without PyQt5
    import conftest  # noqa: F401
except Exception:
    pass

from eval import contract_eval as C  # noqa: E402
from classes import edit_contract as EC  # noqa: E402
from classes import edit_playbook as P  # noqa: E402
from classes.editor_tools import REGISTRY, media_index_tools_edit as TE  # noqa: E402

CONDITIONS = ("today", "warn", "enforce", "enforce_nohint")
OLD_BRIEF_STEP = "1. Brief: form, length, vibe or reference, bans, delivery. Save it in the edit brief so a long job survives; read it back when picking one up."
WHOLE = ["make something cool with my trip", "make a vlog from my footage", "edit my footage into a reel", "cut a trailer from these clips"]
NARROW = ["make the whole thing warmer", "add captions to the timeline"]

CARDS = {
    "get_project_overview_tool": 'get_project_overview_tool({}): survey of the footage: amounts, days, best moments, music, orientation, what is not indexed.',
    "search_footage_tool": 'search_footage_tool({"query": str, "limit": int, "min_highlight": number}): the best moments matching, strongest first: [{file_id,name,start,end,highlight}].',
    "add_clips_to_timeline_tool": 'add_clips_to_timeline_tool({"items": [{"file_id": str, "source_start": number, "source_end": number}]}): place source ranges on the timeline in order.',
    "trim_clips_tool": 'trim_clips_tool({"timeline_clip_id": str, "start_seconds": number}): trim a placed clip.',
    "color_grade_clip_tool": 'color_grade_clip_tool({"scope": "all"|"clip", "look": str}): grade clips.',
    "add_captions_tool": 'add_captions_tool({"timeline_clip_id": str}): add word-timed captions over a clip.',
    "review_edit_tool": "review_edit_tool({}): measured audit of the edit: needs, unknown, layers.",
    "export_video_tool": 'export_video_tool({"preset": str}): export the edit.',
}


def begin_edit_card():
    spec = REGISTRY["begin_edit_tool"]
    props = {k: v.get("description", "") for k, v in spec.schema["properties"].items()}
    return f"begin_edit_tool({json.dumps(props)}): {' '.join((spec.func.__doc__ or '').split())}"


def set_brief_card():
    spec = REGISTRY["set_edit_brief_tool"]
    return f"set_edit_brief_tool({{\"brief\": object}}): {' '.join((spec.func.__doc__ or '').split())[:420]}"


def system_prompt(condition: str) -> str:
    tools = ["get_project_overview_tool", "search_footage_tool", "add_clips_to_timeline_tool", "trim_clips_tool", "color_grade_clip_tool",
             "add_captions_tool", "review_edit_tool", "export_video_tool"]
    cards = [CARDS[t] for t in tools]
    playbook = P.PLAYBOOK
    extra = ""
    if condition in ("today", "enforce_nohint"):
        playbook = playbook.replace(next(ln for ln in playbook.splitlines() if ln.startswith("1. Brief:")), OLD_BRIEF_STEP)
    if condition != "today":
        cards = [begin_edit_card(), set_brief_card()] + cards
    if condition in ("warn", "enforce"):
        from windows.agent_runners import _edit_contract_prompt
        extra = "\n\n" + _edit_contract_prompt()
    return ("You are an AI video-editing agent working through tools in a live editor (like Claude Code with an editor MCP server).\n"
            'Every turn reply with EXACTLY ONE JSON object and nothing else: {"tool": "<name>", "args": {...}} to call a tool, or '
            '{"final": "<short report to the user>"} when you are done. You will see each tool\'s JSON receipt next. No prose, no markdown.\n\n'
            "TOOLS:\n" + "\n".join("- " + c for c in cards) + extra + "\n\n" + playbook)


def snapshot(world, condition: str) -> str:
    files = world.project
    index_line = f"index: videos={len(files)} ready={len(files)} analyzed=0 not_indexed=0"
    contract, brief = EC.normalize(world.store.get(EC.CONTRACT_KEY)), world.store.get(TE.BRIEF_KEY) or {}
    state = EC.edit_state_line(contract, brief, index_line) if (contract["mode"] != EC.MODE_FREE or brief) else index_line
    media = "\n".join(f"  media_bin_file_id={f.file_id} name={f.name!r} duration={f.duration:.0f}s" for f in files[:8])
    more = f"\n  ... and {len(files) - 8} more" if len(files) > 8 else ""
    tl = f"{len(world.placed)} clip(s)" if world.placed else "(empty)"
    head = f"[Editor snapshot]\nProject files count: {len(files)}\n"
    if condition != "today":
        head += f"EDIT_STATE: {state}\n"
    return f"{head}MEDIA_BIN:\n{media}{more}\nTIMELINE: {tl}\n[/Editor snapshot]\n"


def receipt_text(receipt, cap=3500) -> str:
    d = json.loads(receipt.to_json())
    slim = {k: d[k] for k in ("status", "summary", "warnings", "notes", "data") if d.get(k)}
    text = json.dumps(slim, separators=(",", ":"), ensure_ascii=False)
    return text if len(text) <= cap else text[:cap] + "...(cut)"


def first_json(text: str):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            depth += (t[i] == "{") - (t[i] == "}")
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except ValueError:
                    break
        start = t.find("{", start + 1)
    return None


def ask_claude(system: str, user: str, model: str, workdir: str, timeout=240):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("the claude CLI is not on PATH")
    sysfile = os.path.join(workdir, "system.txt")
    Path(sysfile).write_text(system, encoding="utf-8")
    cmd = [claude, "-p", "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project",
           "--model", model, "--output-format", "json", "--system-prompt-file", sysfile]
    env = os.environ.copy()
    real_home = os.environ.get("ZENVI_LIVE_REAL_HOME")        # the harness keeps its own home throw-away; only `claude` needs the real login
    if real_home:
        env["USERPROFILE"] = env["HOME"] = real_home
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", cwd=workdir, timeout=timeout, env=env)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return "", {"error": (r.stderr or r.stdout)[:300]}
    usage = data.get("usage") or {}
    return str(data.get("result") or ""), {"cost": float(data.get("total_cost_usd") or 0), "in": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                                           + int(usage.get("cache_creation_input_tokens") or 0), "out": int(usage.get("output_tokens") or 0)}


def run_one(condition: str, prompt: str, seed: int, model: str, narrow: bool, max_turns: int = 10) -> dict:
    rollout = {"today": "off", "warn": "warn", "enforce": "enforce", "enforce_nohint": "enforce"}[condition]
    project = C.make_project(seed)
    best = C.moments_of(project)
    workdir = tempfile.mkdtemp(prefix="zenvi-live-")
    cost = tin = tout = bad = 0
    transcript, final = [], ""
    try:
        with C.World(project, rollout) as w:
            if narrow:
                w._place(items=C.as_items(C.bin_order(project)[:6]))          # an edit under way; the ask is a tweak
            elif rollout != "off":
                started = EC.contract_for_ask(w.store[EC.CONTRACT_KEY], prompt, timeline_empty=True, media_minutes=sum(f.duration for f in project) / 60.0)
                if started is not None:
                    w.store[EC.CONTRACT_KEY] = started                           # what the desktop does with the message
            system = system_prompt(condition)
            handlers_extra = {"set_edit_brief_tool": REGISTRY["set_edit_brief_tool"].func}
            from classes.agent_tools import execute as X
            X._HANDLERS.update(handlers_extra)
            convo = f"{snapshot(w, condition)}\nUSER: {prompt}\n"
            for turn in range(max_turns):
                reply, usage = ask_claude(system, convo + "\nYour next JSON object:", model, workdir)
                cost += usage.get("cost", 0)
                tin += usage.get("in", 0)
                tout += usage.get("out", 0)
                obj = first_json(reply)
                if not isinstance(obj, dict):
                    bad += 1
                    convo += f"\nASSISTANT: {reply[:300]}\nSYSTEM: reply with exactly one JSON object.\n"
                    transcript.append({"turn": turn, "reply": reply[:200], "bad": True})
                    continue
                if "final" in obj:
                    final = str(obj["final"])
                    break
                tool, args = str(obj.get("tool") or ""), obj.get("args") if isinstance(obj.get("args"), dict) else {}
                receipt = w.call(tool, **args)
                convo += f"\nASSISTANT: {json.dumps(obj, separators=(',', ':'))}\nRECEIPT: {receipt_text(receipt)}\n"
                transcript.append({"turn": turn, "tool": tool, "status": receipt.status, "warned": bool(receipt.warnings)})
            tools = [e["tool"] for e in w.log]
            first_applied = next((i for i, e in enumerate(w.log) if e["tool"] == C.PLACE and e["status"] != "refused"), None)
            clips = w.timeline() if not narrow else []
            used = sum(1 for m in best[:5] if C.R._moment_used(m, clips)) if clips else 0
            ranks = []
            if clips:
                first = clips[0]
                ranks = [i + 1 for i, m in enumerate(best) if m["file_id"] == first.file_id and abs(m["start"] - first.src_in) < 1e-6]
            refusals = [i for i, e in enumerate(w.log) if e["status"] == "refused"]
            recovered = bool(refusals) and any(e["tool"] in ("begin_edit_tool", "set_edit_brief_tool") and e["status"] != "refused" for e in w.log[refusals[0]:])
            return {
                "condition": condition, "prompt": prompt, "narrow": narrow, "seed": seed, "turns": len(transcript), "bad_replies": bad,
                "tools": tools, "placed": first_applied is not None if not narrow else None,
                "surveyed_first": (first_applied is not None and any(e["tool"] in ("begin_edit_tool", "get_project_overview_tool", "search_footage_tool")
                                                                       for e in w.log[:first_applied])) if not narrow else None,
                "begin_edit_first": (first_applied is not None and any(e["tool"] == "begin_edit_tool" for e in w.log[:first_applied])) if not narrow else None,
                "brief_ok": bool(w.first_applied_brief) if not narrow else None, "refused": len(refusals), "recovered_after_refusal": recovered,
                "warned": sum(1 for e in w.log if e["warned"]), "begin_edit_calls": tools.count("begin_edit_tool"),
                "opening_rank": ranks[0] if ranks else None, "top5_used": used, "reviewed": "review_edit_tool" in tools, "exported": "export_video_tool" in tools,
                "cost_usd": round(cost, 4), "input_tokens": tin, "output_tokens": tout, "final": final[:200],
            }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def aggregate(results):
    table = {}
    for cond in CONDITIONS:
        whole = [r for r in results if r["condition"] == cond and not r["narrow"]]
        narrow = [r for r in results if r["condition"] == cond and r["narrow"]]
        n = max(1, len(whole))
        ranks = [r["opening_rank"] for r in whole if r["opening_rank"]]
        table[cond] = {
            "runs": len(whole), "placed": sum(bool(r["placed"]) for r in whole) / n, "begin_edit_first": sum(bool(r["begin_edit_first"]) for r in whole) / n,
            "surveyed_first": sum(bool(r["surveyed_first"]) for r in whole) / n, "brief_at_first_placement": sum(bool(r["brief_ok"]) for r in whole) / n,
            "refused_per_run": sum(r["refused"] for r in whole) / n, "recovered_after_refusal": sum(bool(r["recovered_after_refusal"]) for r in whole if r["refused"]),
            "runs_refused": sum(1 for r in whole if r["refused"]), "mean_opening_rank": (sum(ranks) / len(ranks)) if ranks else None,
            "top5_used": sum(r["top5_used"] for r in whole) / n, "reviewed": sum(bool(r["reviewed"]) for r in whole) / n,
            "cost_usd_per_run": sum(r["cost_usd"] for r in whole) / n, "input_tokens_per_run": sum(r["input_tokens"] for r in whole) / n,
            "narrow_runs": len(narrow), "narrow_refused": sum(r["refused"] for r in narrow), "narrow_begin_edit": sum(r["begin_edit_calls"] for r in narrow),
            "bad_replies": sum(r["bad_replies"] for r in results if r["condition"] == cond),
        }
    return table


def markdown(table):
    rows = ["| condition | runs | placed | begin_edit first | surveyed first | brief at 1st placement | refused/run | recovered after refusal | opening rank (1=best) | top-5 used | reviewed | $/run | input tok/run |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cond, m in table.items():
        rank = "-" if m["mean_opening_rank"] is None else f"{m['mean_opening_rank']:.1f}"
        rec = f"{m['recovered_after_refusal']}/{m['runs_refused']}" if m["runs_refused"] else "-"
        rows.append(f"| {cond} | {m['runs']} | {m['placed']:.0%} | {m['begin_edit_first']:.0%} | {m['surveyed_first']:.0%} | {m['brief_at_first_placement']:.0%} | "
                    f"{m['refused_per_run']:.1f} | {rec} | {rank} | {m['top5_used']:.1f} / 5 | {m['reviewed']:.0%} | {m['cost_usd_per_run']:.3f} | {m['input_tokens_per_run']:.0f} |")
    rows += ["", "Narrow-ask controls: " + "; ".join(f"`{c}`: {m['narrow_runs']} runs, {m['narrow_refused']} refusals, {m['narrow_begin_edit']} begin_edit calls" for c, m in table.items())]
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--whole", type=int, default=len(WHOLE), help="how many whole-piece prompts")
    ap.add_argument("--narrow", type=int, default=len(NARROW))
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--repeat", type=int, default=1, help="runs per prompt (the model is not deterministic)")
    ap.add_argument("--out", default="")
    ap.add_argument("--one", default="", help="internal: run a single job given as JSON, print its result")
    a = ap.parse_args()
    if os.environ.get("ZENVI_EVAL_LIVE") != "1":
        sys.exit("This spends your Claude plan. Set ZENVI_EVAL_LIVE=1 to run it.")
    if a.one:
        j = json.loads(a.one)
        print(json.dumps(run_one(j["condition"], j["prompt"], j["seed"], a.model, j["narrow"], a.max_turns)))
        return
    jobs = []
    for cond in a.conditions.split(","):
        for rep in range(a.repeat):
            for i, p in enumerate(WHOLE[: a.whole]):
                jobs.append({"condition": cond, "prompt": p, "seed": 4100 + i + 100 * rep, "narrow": False})
            for i, p in enumerate(NARROW[: a.narrow]):
                jobs.append({"condition": cond, "prompt": p, "seed": 4200 + i + 100 * rep, "narrow": True})

    def go(job):                                       # one process per run: the world patches module globals, so runs must not share a process
        r = subprocess.run([sys.executable, "-m", "eval.contract_live", "--model", a.model, "--max-turns", str(a.max_turns), "--one", json.dumps(job)],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(TESTS), env=os.environ.copy(), timeout=60 * 40)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return {**job, "turns": 0, "bad_replies": 0, "tools": [], "placed": False, "surveyed_first": False, "begin_edit_first": False, "brief_ok": False,
                    "refused": 0, "recovered_after_refusal": False, "warned": 0, "begin_edit_calls": 0, "opening_rank": None, "top5_used": 0,
                    "reviewed": False, "exported": False, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0, "final": "", "error": (r.stderr or r.stdout)[-300:]}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        results = list(pool.map(go, jobs))
    table = aggregate(results)
    out = {"model": a.model, "seconds": round(time.time() - t0), "results": results, "table": table}
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(markdown(table))
    errors = [r for r in results if r.get("error")]
    if errors:
        print(f"\n{len(errors)} run(s) failed to complete, e.g.: {errors[0]['error']}")


if __name__ == "__main__":
    main()
