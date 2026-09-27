#!/usr/bin/env bash
# orchestrator.sh [--only a,b,c] [--force] [--max N] [--once]
# Wave loop (runs in tmux window 0). Every 60s: sync PR state from GitHub, start any feature whose
# dependencies are pr_open/ready/merged (or --force), up to --max concurrent active agents.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ONLY=""; FORCE=0; MAX="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("max_parallel",4))' "$PORT_SCRIPTS/manifest.json")"; ONCE=0
while [[ $# -gt 0 ]]; do case "$1" in
  --only) ONLY="$2"; shift 2;; --force) FORCE=1; shift;; --max) MAX="$2"; shift 2;; --once) ONCE=1; shift;; *) die "unknown arg $1";; esac; done
ALL_FEATURES="$(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["features"]))' "$PORT_SCRIPTS/manifest.json")"
FEATURES="${ONLY//,/ }"; [[ -n "$FEATURES" ]] || FEATURES="$ALL_FEATURES"
for f in $FEATURES; do require_feature "$f"; done

sync_github() {  # merged / pr_url from GitHub -> state files
  local prs; prs="$(gh pr list --repo "$REPO_SLUG" --author jashanpratapsingh --state all --limit 200 --json headRefName,url,state,isDraft,mergedAt 2>/dev/null || echo '[]')"
  python3 - "$STATE_DIR" "$prs" "$BRANCH_PREFIX" <<'PY'
import json, os, sys, datetime
state_dir, prs_json, prefix = sys.argv[1:4]
prs = {p["headRefName"]: p for p in json.loads(prs_json)}
for fn in os.listdir(state_dir):
    if not fn.endswith(".json") or fn == "empty-mcp.json": continue
    p = os.path.join(state_dir, fn)
    try: d = json.load(open(p))
    except Exception: continue
    pr = prs.get(prefix + d.get("feature", fn[:-5]))
    if not pr: continue
    changed = False
    if d.get("pr_url") != pr["url"]: d["pr_url"] = pr["url"]; changed = True
    new = None
    if pr.get("mergedAt"): new = "merged"
    elif pr["state"] == "CLOSED": new = None
    elif d.get("state") == "pr_open" and not pr.get("isDraft"): new = "ready"
    if new and d.get("state") != new:
        d["state"] = new; d["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        d.setdefault("history", []).append({"state": new, "at": d["updated_at"], "by": "orchestrator"}); changed = True
    if changed: json.dump(d, open(p, "w"), indent=2)
PY
}
active_count() { local n=0 f; for f in $ALL_FEATURES; do case "$(feature_state "$f")" in bootstrapping|in_progress|testing) n=$((n+1));; esac; done; echo $n; }
start_feature() {
  local f="$1"
  log "starting $f"
  bash "$PORT_SCRIPTS/new-worktree.sh" "$f" || { PORT_FEATURE="$f" bash "$PORT_SCRIPTS/set-status.sh" failed --note "new-worktree.sh failed"; return 1; }
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    tmux new-window -d -t "$TMUX_SESSION" -n "$f" "bash '$PORT_SCRIPTS/launch-agent.sh' '$f'; echo; echo '[agent for $f exited — press Enter to close this window]'; read -r"
  else
    warn "tmux session $TMUX_SESSION not running; created worktree only. Start with: launch-agent.sh $f"
  fi
}
log "orchestrator: features=[$FEATURES] max=$MAX force=$FORCE"
while :; do
  sync_github
  started_any=0
  for f in $FEATURES; do
    st="$(feature_state "$f")"
    case "$st" in in_progress|testing|pr_open|ready|needs_human|failed|merged) continue;; esac   # already running or done
    tmux list-windows -t "$TMUX_SESSION" -F '#W' 2>/dev/null | grep -qx "$f" && continue           # window already open
    if (( FORCE )); then ok=1; else mf ready "$f" >/dev/null 2>&1 && ok=1 || ok=0; fi
    (( ok )) || continue
    if (( $(active_count) >= MAX )); then log "$f is ready but $MAX agents are active; waiting"; continue; fi
    start_feature "$f" && started_any=1 && sleep 20
  done
  bash "$PORT_SCRIPTS/status.sh" 2>/dev/null | head -20
  (( ONCE )) && exit 0
  sleep 60
done
