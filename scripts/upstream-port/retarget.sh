#!/usr/bin/env bash
# retarget.sh <feature>
# After a dependency PR merges: rebase this stacked branch onto origin/develop (dropping the dependency's
# commits), force-with-lease push, and point the PR at develop. Human-run.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
FEATURE="${1:-}"; [[ -n "$FEATURE" ]] || die "usage: retarget.sh <feature>"
WT="$(wt_path "$FEATURE")"; [[ -f "$WT/.port-env" ]] || die "no worktree for $FEATURE"
cd "$WT"; source .port-env
OLD_BASE="$PORT_BASE_BRANCH"
[[ "$OLD_BASE" != develop ]] || die "$FEATURE already targets develop"
git fetch -q origin --prune
[[ -z "$(git status --porcelain)" ]] || die "worktree dirty; commit or stash first"
log "rebasing $PORT_BRANCH from $OLD_BASE onto origin/develop"
git rebase --onto origin/develop "origin/$OLD_BASE" "$PORT_BRANCH" || die "rebase conflicts: resolve in $WT, then: git rebase --continue && git push --force-with-lease && gh pr edit --base develop"
git push --force-with-lease origin "$PORT_BRANCH"
gh pr edit "$PORT_BRANCH" --repo "$REPO_SLUG" --base develop >/dev/null && log "PR now targets develop"
sed -i '' 's/^export PORT_BASE_BRANCH=.*/export PORT_BASE_BRANCH="develop"/' .port-env
python3 - "$STATE_DIR/$FEATURE.json" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["base"]="develop"; json.dump(d,open(p,"w"),indent=2)
PY
log "done; re-run the test gate in the agent window (scripts/upstream-port/test-all.sh)"
