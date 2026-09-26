#!/usr/bin/env bash
# launch-all.sh [--only a,b] [--force] [--max N]
# Pre-flight checks, then a detached tmux session "zenvi-port": window 0 = orchestrator, window 1 = live status,
# one window per feature as they start. Attach with: tmux attach -t zenvi-port
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ARGS=("$@")
command -v tmux >/dev/null || die "tmux missing (brew install tmux)"
[[ -x "$CLAUDE_BIN" ]] || die "claude CLI missing at $CLAUDE_BIN"
gh auth status 2>&1 | grep -q 'account jashanpratapsingh' || die "gh must be logged in as jashanpratapsingh"
[[ "$(git -C "$PORT_MAIN_REPO" config user.email)" =~ jashanpratap123@gmail.com|88160290\+jashanpratapsingh ]] || die "main repo git identity is not Jashan's"
[[ -f "$PORT_SETTINGS" ]] || die "repo Claude settings missing at $PORT_SETTINGS"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("disableClaudeAiConnectors") is True' "$PORT_SETTINGS" || die "connectors are not disabled in $PORT_SETTINGS"
[[ -x "$PORT_MAIN_REPO/.venv/bin/python3" ]] || die "shared .venv missing in $PORT_MAIN_REPO (run ./run.sh once)"
(cd "$PORT_MAIN_REPO" && mf verify) || die "manifest verify failed"
git -C "$PORT_MAIN_REPO" fetch -q origin --prune
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  warn "session $TMUX_SESSION already running; restarting orchestrator window only"
  tmux kill-window -t "$TMUX_SESSION:orchestrator" 2>/dev/null || true
  tmux new-window -d -t "$TMUX_SESSION" -n orchestrator "bash '$PORT_SCRIPTS/orchestrator.sh' ${ARGS[*]:-}; echo '[orchestrator exited]'; read -r"
else
  tmux new-session -d -s "$TMUX_SESSION" -n orchestrator -c "$PORT_MAIN_REPO" "bash '$PORT_SCRIPTS/orchestrator.sh' ${ARGS[*]:-}; echo '[orchestrator exited]'; read -r"
  tmux new-window -d -t "$TMUX_SESSION" -n status "while :; do clear; bash '$PORT_SCRIPTS/status.sh'; sleep 30; done"
fi
log "session $TMUX_SESSION is up."
echo "  attach:   tmux attach -t $TMUX_SESSION      (Ctrl-b w = pick a window, Ctrl-b d = detach)"
echo "  status:   bash $PORT_SCRIPTS/status.sh"
echo "  logs:     tail -f $LOG_DIR/<feature>.tmux.log"
echo "  stop:     bash $PORT_SCRIPTS/shutdown.sh"
