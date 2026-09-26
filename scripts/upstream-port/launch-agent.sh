#!/usr/bin/env bash
# launch-agent.sh <feature> [--fresh]
# Runs an interactive Claude Code session inside the feature worktree (normally inside a tmux window created
# by orchestrator.sh). First launch sends the kickoff brief; later launches --continue the same session.
# No MCP servers, no claude.ai connectors, port guard hook active (PORT_* exported).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
FEATURE="${1:-}"; shift || true
[[ -n "$FEATURE" ]] || die "usage: launch-agent.sh <feature> [--fresh]"
FRESH=0; while [[ $# -gt 0 ]]; do case "$1" in --fresh) FRESH=1; shift;; *) die "unknown arg $1";; esac; done
WT="$(wt_path "$FEATURE")"
[[ -f "$WT/.port-env" ]] || die "no worktree for $FEATURE; run new-worktree.sh $FEATURE first"
cd "$WT"; source .port-env
[[ -x "$CLAUDE_BIN" ]] || die "claude CLI not found (CLAUDE_BIN=$CLAUDE_BIN)"
# libopenshot for this feature (the libopenshot-1.0 agent builds its own prefix as part of the feature)
if [[ "$FEATURE" != "libopenshot-1.0" ]]; then
  bash "$PORT_SCRIPTS/ensure-deps.sh" "$ZENVI_DEPS" "$PORT_LIBOPENSHOT_TAG" "$PORT_LIBOPENSHOT_AUDIO_TAG" || warn "libopenshot not available at $ZENVI_DEPS; the agent must sort this out first"
fi
cur="$(feature_state "$FEATURE")"
case "$cur" in ""|queued|bootstrapping) bash "$PORT_SCRIPTS/set-status.sh" in_progress;; esac
# Pre-trust the worktree folder so the interactive session does not stop at Claude's trust dialog.
python3 - "$WT" <<'PY'
import json, os, sys
p = os.path.expanduser("~/.claude.json"); wt = sys.argv[1]
try: d = json.load(open(p))
except Exception: d = {}
proj = d.setdefault("projects", {}).setdefault(wt, {})
if not proj.get("hasTrustDialogAccepted"):
    proj["hasTrustDialogAccepted"] = True; proj.setdefault("allowedTools", [])
    tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), indent=2); os.replace(tmp, p)
PY
EMPTY_MCP="$STATE_DIR/empty-mcp.json"; echo '{"mcpServers":{}}' > "$EMPTY_MCP"
SESSION_MARK="$STATE_DIR/$FEATURE.session"
SYS_PROMPT="$(cat "$PORT_SCRIPTS/AGENT_PROMPT.md")"
COMMON=(--dangerously-skip-permissions --strict-mcp-config --mcp-config "$EMPTY_MCP" --settings "$PORT_SETTINGS"
        --append-system-prompt "$SYS_PROMPT" --name "port-$FEATURE")
[[ -n "${PORT_MODEL:-}" ]] && COMMON+=(--model "$PORT_MODEL")
tmux pipe-pane -o -t "${TMUX_PANE:-}" "cat >> '$LOG_DIR/$FEATURE.tmux.log'" 2>/dev/null || true
if [[ -f "$SESSION_MARK" && "$FRESH" == 0 ]]; then
  log "resuming session for $FEATURE"
  exec "$CLAUDE_BIN" "${COMMON[@]}" --continue "Session resumed by launch-agent.sh. Re-read scripts/upstream-port state ($STATE_DIR/$FEATURE.json), check \`git status\` and \`git log --oneline origin/develop..HEAD\`, then continue the procedure from where you stopped."
fi
touch "$SESSION_MARK"
BRIEF="$(mf describe "$FEATURE")"
KICKOFF="You are the upstream-port agent for feature \`$FEATURE\`.
Worktree: $WT   Branch: $PORT_BRANCH   PR base: $PORT_BASE_BRANCH   libopenshot: $ZENVI_DEPS
Your procedure and hard rules are in the system prompt (upstream-port AGENT_PROMPT). Manifest brief:

$BRIEF

Begin with step 0 (orientation) now. Keep the status file current as you go."
log "starting Claude for $FEATURE in $WT"
exec "$CLAUDE_BIN" "${COMMON[@]}" "$KICKOFF"
