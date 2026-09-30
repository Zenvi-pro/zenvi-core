#!/usr/bin/env bash
# status.sh [--json]  — one line per feature: state, branch, PR, tmux window, last update; then needs_human notes.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
PRS="$(gh pr list --repo "$REPO_SLUG" --author jashanpratapsingh --state all --limit 200 --json headRefName,url,state,isDraft,mergedAt 2>/dev/null || echo '[]')"
WINDOWS="$(tmux list-windows -t "$TMUX_SESSION" -F '#W' 2>/dev/null | tr '\n' ' ')"
python3 - "$PORT_SCRIPTS/manifest.json" "$STATE_DIR" "$PRS" "$WINDOWS" "${1:-}" <<'PY'
import json, os, sys, datetime
mpath, state_dir, prs_json, windows, flag = sys.argv[1:6]
m = json.load(open(mpath)); prs = {p["headRefName"]: p for p in json.loads(prs_json)}
rows = []
for fid, f in m["features"].items():
    sp = os.path.join(state_dir, fid + ".json"); st = json.load(open(sp)) if os.path.exists(sp) else {}
    pr = prs.get(m["branch_prefix"] + fid, {})
    prtxt = ""
    if pr:
        prtxt = pr["url"].split("/")[-1] + (" draft" if pr.get("isDraft") else "") + (" MERGED" if pr.get("mergedAt") else (" closed" if pr["state"] == "CLOSED" else ""))
    rows.append((fid, st.get("state", "held" if f.get("hold") else "not started"), st.get("base", ""), prtxt,
                 "win" if fid in windows.split() else "", (st.get("updated_at") or "")[:16].replace("T", " "), st.get("note", "")))
if flag == "--json":
    print(json.dumps([dict(zip(("feature","state","base","pr","tmux","updated","note"), r)) for r in rows], indent=1)); sys.exit()
print("%-25s %-13s %-22s %-14s %-4s %s" % ("feature", "state", "base", "PR", "tmux", "updated"))
for r in rows: print("%-25s %-13s %-22s %-14s %-4s %s" % r[:6])
notes = [r for r in rows if r[6]]
if notes:
    print("\nnotes:")
    for r in notes: print("  %-25s %s" % (r[0], r[6][:160]))
print("\n%s  session=%s  attach: tmux attach -t %s" % (datetime.datetime.now().strftime("%H:%M:%S"), "up" if windows.strip() else "down", os.environ.get("PORT_TMUX_SESSION", "zenvi-port")))
PY
