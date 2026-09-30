#!/usr/bin/env bash
# shutdown.sh [--remove-merged] [--remove-all]
# Kills the tmux session (agents stop; their sessions stay resumable with launch-agent.sh). Optionally removes
# worktrees of merged features, or everything (asks first).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
MODE=""; while [[ $# -gt 0 ]]; do case "$1" in --remove-merged) MODE=merged; shift;; --remove-all) MODE=all; shift;; *) die "unknown arg $1";; esac; done
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null && log "killed tmux session $TMUX_SESSION" || log "no tmux session"
[[ -n "$MODE" ]] || exit 0
remove_feature() { local f="$1" wt; wt="$(wt_path "$f")"; [[ -d "$wt" ]] || return 0
  log "removing worktree $wt"; git -C "$PORT_MAIN_REPO" worktree remove --force "$wt"; git -C "$PORT_MAIN_REPO" branch -D "$BRANCH_PREFIX$f" 2>/dev/null || true
  rm -rf "$HOME_DIR/$f" "$STATE_DIR/$f.json" "$STATE_DIR/$f.session"; }
if [[ "$MODE" == all ]]; then read -r -p "Remove ALL feature worktrees, branches and state under $PORT_ROOT? [y/N] " a; [[ "$a" =~ ^[Yy] ]] || exit 0; fi
for f in $(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["features"]))' "$PORT_SCRIPTS/manifest.json"); do
  st="$(feature_state "$f")"; [[ "$MODE" == all || "$st" == merged ]] && remove_feature "$f"; done
git -C "$PORT_MAIN_REPO" worktree prune
