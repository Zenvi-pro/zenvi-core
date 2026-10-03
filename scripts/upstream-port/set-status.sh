#!/usr/bin/env bash
# set-status.sh <state> [--pr URL] [--note TEXT] [--feature ID]
# states: queued bootstrapping in_progress testing pr_open ready needs_human failed merged
# Agents call this from inside their worktree (PORT_FEATURE is exported for them).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
STATE="${1:-}"; shift || true
[[ -n "$STATE" ]] || die "usage: set-status.sh <state> [--pr URL] [--note TEXT] [--feature ID]"
PR=""; NOTE=""; FEATURE="${PORT_FEATURE:-}"
while [[ $# -gt 0 ]]; do case "$1" in
  --pr) PR="$2"; shift 2;; --note) NOTE="$2"; shift 2;; --feature) FEATURE="$2"; shift 2;; *) die "unknown arg $1";; esac; done
[[ -n "$FEATURE" ]] || die "no feature: pass --feature or run inside an agent session"
case "$STATE" in queued|bootstrapping|in_progress|testing|pr_open|ready|needs_human|failed|merged) ;; *) die "bad state '$STATE'";; esac
WT="$(wt_path "$FEATURE")"
HEAD_SHA="$(git -C "$WT" rev-parse --short HEAD 2>/dev/null || echo "")"
BRANCH="$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
python3 - "$STATE_DIR/$FEATURE.json" "$FEATURE" "$STATE" "$PR" "$NOTE" "$HEAD_SHA" "$BRANCH" <<'PY'
import json, os, sys, datetime
p, feature, state, pr, note, sha, branch = sys.argv[1:8]
d = {}
if os.path.exists(p):
    try: d = json.load(open(p))
    except Exception: d = {}
d.update({"feature": feature, "state": state, "head_sha": sha, "branch": branch,
          "updated_at": datetime.datetime.now().isoformat(timespec="seconds")})
if pr: d["pr_url"] = pr
if note: d["note"] = note
elif state not in ("needs_human", "failed"): d.pop("note", None)
d.setdefault("history", []).append({"state": state, "at": d["updated_at"]})
d["history"] = d["history"][-30:]
tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), indent=2); os.replace(tmp, p)
print("%s -> %s%s" % (feature, state, (" (" + note + ")") if note else ""))
PY
