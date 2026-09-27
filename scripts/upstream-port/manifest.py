#!/usr/bin/env python3
"""CLI over scripts/upstream-port/manifest.json (stdlib only).

  manifest.py list                      table of features
  manifest.py get <feature> <key>       one field (JSON for lists/objects)
  manifest.py deps <feature>            space-separated dependency ids
  manifest.py commits <feature>         one JSON object per line, in pick order
  manifest.py prefix <feature>          expanded libopenshot install prefix
  manifest.py tag <feature>             libopenshot tag ('' if the default prefix is fine)
  manifest.py base <feature>            branch to cut from: origin/<base> or jashan/<last unmerged dep>
  manifest.py ready <feature>           exit 0 if every dep is pr_open/ready/merged and feature is not held
  manifest.py describe <feature>        markdown brief for the agent prompt
  manifest.py verify                    every sha resolves and is a merge commit for the stated PR
State files live in $PORT_ROOT/.state/<feature>.json (PORT_ROOT default ~/Projects/zenvi-worktrees).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "manifest.json")
PORT_ROOT = os.path.expanduser(os.environ.get("PORT_ROOT", "~/Projects/zenvi-worktrees"))
STATE_DIR = os.path.join(PORT_ROOT, ".state")
DONE_STATES = {"pr_open", "ready", "merged"}


def load():
    with open(MANIFEST) as fh:
        return json.load(fh)


def feature(m, fid):
    try:
        return m["features"][fid]
    except KeyError:
        sys.exit("unknown feature %r; known: %s" % (fid, " ".join(m["features"])))


def state(fid):
    p = os.path.join(STATE_DIR, fid + ".json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return {}


def prefix(m, f):
    return os.path.expanduser(f.get("deps_prefix") or m["default_deps_prefix"])


def base_branch(m, fid):
    f = feature(m, fid)
    pending = [d for d in f["deps"] if state(d).get("state") != "merged"]
    if pending:
        return m["branch_prefix"] + pending[-1]
    return "origin/" + m["base"]


def ready(m, fid):
    f = feature(m, fid)
    if f.get("hold"):
        return False, "held (start manually with launch-all.sh --only %s --force)" % fid
    missing = [d for d in f["deps"] if state(d).get("state") not in DONE_STATES]
    if missing:
        return False, "waiting on " + ", ".join("%s(%s)" % (d, state(d).get("state", "not started")) for d in missing)
    return True, "ready"


def describe(m, fid):
    f = feature(m, fid)
    out = ["# Feature `%s` — %s" % (fid, f["title"]), "",
           "- branch: `%s%s`  base: `%s`  effort: %s" % (m["branch_prefix"], fid, base_branch(m, fid), f.get("effort", "?")),
           "- libopenshot prefix: `%s`%s" % (prefix(m, f), (" (tag %s)" % f["libopenshot_tag"]) if f.get("libopenshot_tag") else ""),
           "- depends on: %s" % (", ".join(f["deps"]) or "nothing"),
           "- GUI feature: %s" % ("yes" if f.get("gui") else "no"), ""]
    if f["commits"]:
        out += ["## Upstream merge commits to port, in this order", ""]
        for c in f["commits"]:
            line = "- `%s` OpenShot PR #%s `%s`" % (c["sha"], c["pr"], c["title"])
            if c.get("mode") == "hunks":
                line += " — HUNKS ONLY for: " + ", ".join("`%s`" % p for p in c["paths"])
            if c.get("note"):
                line += "\n  - note: " + c["note"]
            out.append(line)
        out.append("")
    if f.get("tasks"):
        out += ["## Additional tasks (not covered by cherry-picks)", ""] + ["- " + t for t in f["tasks"]] + [""]
    if f.get("hotspots"):
        out += ["## Expected conflict hot-spots", ""] + ["- " + h for h in f["hotspots"]] + [""]
    if f.get("human_test"):
        out += ["## Human test steps to include in the PR (refine, add Pass lines)", ""] + ["%d. %s" % (i + 1, t) for i, t in enumerate(f["human_test"])] + [""]
    return "\n".join(out)


def verify(m):
    bad = 0
    for fid, f in m["features"].items():
        for c in f["commits"]:
            r = subprocess.run(["git", "log", "-1", "--format=%H %P%n%s", c["sha"]], capture_output=True, text=True)
            if r.returncode != 0:
                print("%s: %s does not resolve" % (fid, c["sha"])); bad += 1; continue
            head, subject = r.stdout.strip().split("\n", 1)
            parents = head.split()[1:]
            if len(parents) < 2:
                print("%s: %s is not a merge commit" % (fid, c["sha"])); bad += 1
            if ("#%d " % c["pr"]) not in subject + " ":
                print("%s: %s subject %r does not mention PR #%d" % (fid, c["sha"], subject, c["pr"])); bad += 1
    # dependency sanity
    for fid, f in m["features"].items():
        for d in f["deps"]:
            if d not in m["features"]:
                print("%s: unknown dep %s" % (fid, d)); bad += 1
    print("verify: %s" % ("OK" if not bad else "%d problem(s)" % bad))
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__); return 0
    m = load()
    cmd = argv[1]
    if cmd == "list":
        print("%-26s %-3s %-5s %-22s %s" % ("feature", "eff", "hold", "libopenshot prefix", "deps"))
        for fid, f in m["features"].items():
            print("%-26s %-3s %-5s %-22s %s" % (fid, f.get("effort", "?"), "yes" if f.get("hold") else "", prefix(m, f).replace(os.path.expanduser("~"), "~"), ", ".join(f["deps"]) or "-"))
        return 0
    if cmd == "verify":
        return verify(m)
    if len(argv) < 3:
        sys.exit("feature id required")
    fid = argv[2]
    f = feature(m, fid)
    if cmd == "get":
        v = f.get(argv[3]) if len(argv) > 3 else f
        print(json.dumps(v) if isinstance(v, (list, dict)) else ("" if v is None else v))
    elif cmd == "deps":
        print(" ".join(f["deps"]))
    elif cmd == "commits":
        for c in f["commits"]:
            print(json.dumps(c))
    elif cmd == "prefix":
        print(prefix(m, f))
    elif cmd == "tag":
        print(f.get("libopenshot_tag", ""))
    elif cmd == "audio_tag":
        print(f.get("libopenshot_audio_tag", ""))
    elif cmd == "base":
        print(base_branch(m, fid))
    elif cmd == "ready":
        ok, why = ready(m, fid)
        print(why)
        return 0 if ok else 1
    elif cmd == "describe":
        print(describe(m, fid))
    else:
        sys.exit("unknown command %r" % cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
