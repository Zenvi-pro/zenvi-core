#!/usr/bin/env bash
# run-app.sh [--headless] [--gui] [--timeout SEC] [--screenshot PATH] [-- <app args>]
# Launches Zenvi from THIS worktree with the feature's libopenshot prefix and an isolated HOME
# ($PORT_ROOT/.home/<feature>), so parallel features never share settings, caches or recovery files.
# --headless: QT_QPA_PLATFORM=offscreen; exit 0 if the app is still alive after --timeout (default 45s), then stop it.
# --gui: real window; with --timeout it is stopped afterwards (and --screenshot captured just before).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[[ -f .port-env ]] || die "run from a feature worktree (no .port-env here)"
source .port-env
MODE="headless"; TIMEOUT=45; SHOT=""
while [[ $# -gt 0 ]]; do case "$1" in
  --headless) MODE=headless; shift;; --gui) MODE=gui; TIMEOUT=0; shift;;
  --timeout) TIMEOUT="$2"; shift 2;; --screenshot) SHOT="$2"; shift 2;; --) shift; break;; *) break;; esac; done
ISO_HOME="$HOME_DIR/$PORT_FEATURE"; mkdir -p "$ISO_HOME/.openshot_qt"
# carry the login session over so the auth gate does not block a test launch
[[ -f "$HOME/.openshot_qt/zenvi_auth.json" && ! -f "$ISO_HOME/.openshot_qt/zenvi_auth.json" ]] && cp "$HOME/.openshot_qt/zenvi_auth.json" "$ISO_HOME/.openshot_qt/"
[[ -f "$ZENVI_DEPS/python/_openshot.so" ]] || die "libopenshot missing at $ZENVI_DEPS (run ensure-deps.sh $ZENVI_DEPS $PORT_LIBOPENSHOT_TAG)"
APP_LOG="$LOG_DIR/$PORT_FEATURE-app.log"
: > "$APP_LOG"
log "launching ($MODE) HOME=$ISO_HOME ZENVI_DEPS=$ZENVI_DEPS log=$APP_LOG"
if [[ "$MODE" == headless ]]; then export OPENSHOT_HEADLESS=1; fi
# Keep brew/PATH from the real environment; only HOME moves.
HOME="$ISO_HOME" ZENVI_DEPS="$ZENVI_DEPS" ./run.sh "$@" >>"$APP_LOG" 2>&1 &
PID=$!
if [[ "$TIMEOUT" -le 0 ]]; then log "app pid $PID running; Ctrl-C or kill $PID to stop"; wait $PID; exit $?; fi
for ((i=0;i<TIMEOUT;i++)); do
  if ! kill -0 $PID 2>/dev/null; then wait $PID; rc=$?; echo "----- app exited early (rc=$rc); last 60 log lines -----"; tail -60 "$APP_LOG"; exit 1; fi
  sleep 1
done
if [[ -n "$SHOT" ]]; then screencapture -x "$SHOT" 2>/dev/null && log "screenshot -> $SHOT" || warn "screencapture failed"; fi
if grep -q -E 'Traceback \(most recent call last\)|Segmentation fault|Fatal Python error' "$APP_LOG"; then
  echo "----- app alive but log has tracebacks -----"; grep -n -A12 -E 'Traceback|Fatal Python error' "$APP_LOG" | head -80
  kill $PID 2>/dev/null; sleep 2; kill -9 $PID 2>/dev/null; exit 1; fi
kill $PID 2>/dev/null; sleep 3; kill -9 $PID 2>/dev/null || true
log "app stayed up ${TIMEOUT}s with no tracebacks (PASS)"; exit 0
