#!/usr/bin/env bash
# Shared helpers for scripts/upstream-port/*. Source, do not execute.
# Every path here is derived, so the scripts work from the main checkout or any worktree.

PORT_SCRIPTS="${PORT_SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PORT_ROOT="${PORT_ROOT:-$HOME/Projects/zenvi-worktrees}"
# The main checkout owns .git; worktrees point at it through the common dir.
PORT_MAIN_REPO="${PORT_MAIN_REPO:-$(cd "$PORT_SCRIPTS/../.." && dirname "$(git rev-parse --git-common-dir)")}"
PORT_MAIN_REPO="$(cd "$PORT_MAIN_REPO" && pwd)"
PORT_GUARD="${PORT_GUARD:-$PORT_SCRIPTS/hooks/port-guard.py}"
PORT_SETTINGS="${PORT_SETTINGS:-$(cd "$PORT_SCRIPTS/../.." && pwd)/.claude/settings.json}"
STATE_DIR="$PORT_ROOT/.state"
LOG_DIR="$PORT_ROOT/.logs"
HOME_DIR="$PORT_ROOT/.home"
TMUX_SESSION="${PORT_TMUX_SESSION:-zenvi-port}"
REPO_SLUG="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["repo"])' "$PORT_SCRIPTS/manifest.json")"
BRANCH_PREFIX="jashan/"
GIT_NAME="Jashan Pratap Singh"
GIT_EMAIL="88160290+jashanpratapsingh@users.noreply.github.com"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"

mkdir -p "$STATE_DIR" "$LOG_DIR" "$HOME_DIR"

mf() { python3 "$PORT_SCRIPTS/manifest.py" "$@"; }
log() { printf '\033[36m[port]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[port] WARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[port] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
wt_path() { echo "$PORT_ROOT/$1"; }
feature_state() { python3 - "$STATE_DIR/$1.json" <<'PY'
import json,sys,os
p=sys.argv[1]
print(json.load(open(p)).get("state","") if os.path.exists(p) else "")
PY
}
require_feature() { mf get "$1" title >/dev/null 2>&1 || die "unknown feature '$1' (see manifest.py list)"; }
