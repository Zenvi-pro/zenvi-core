#!/usr/bin/env bash
# ensure-deps.sh <prefix> [libopenshot_tag] [libopenshot_audio_tag]
# Idempotent, locked libopenshot build into <prefix>. Safe to call from several worktrees at once.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
PREFIX="${1:-}"; TAG="${2:-}"; AUDIO_TAG="${3:-}"
[[ -n "$PREFIX" ]] || die "usage: ensure-deps.sh <prefix> [tag] [audio_tag]"
PREFIX="${PREFIX/#\~/$HOME}"
if [[ -f "$PREFIX/python/_openshot.so" ]]; then
  log "libopenshot present at $PREFIX ($(ls "$PREFIX"/lib/libopenshot.*.dylib 2>/dev/null | head -1 | xargs -n1 basename 2>/dev/null))"; exit 0; fi
LOCK="$PREFIX.lock"
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  [[ -f "$PREFIX/python/_openshot.so" ]] && { log "built by another process"; exit 0; }
  (( waited % 60 == 0 )) && log "waiting for lock $LOCK (another build in progress) ${waited}s"
  sleep 10; waited=$((waited+10)); (( waited > 3600 )) && die "gave up waiting for $LOCK"
done
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
# Which build script? The default (0.5.0) prefix uses the main checkout's script. A 1.0 prefix needs the
# parametrized script from the libopenshot-1.0 feature branch (worktree, or develop once merged).
SCRIPT="$PORT_MAIN_REPO/scripts/build-mac-libopenshot.sh"
if [[ -n "$TAG" && "$TAG" != "v0.5.0" ]]; then
  if [[ -f "$PORT_ROOT/libopenshot-1.0/scripts/build-mac-libopenshot.sh" ]] && grep -q 'LIBOPENSHOT_AUDIO_TAG' "$PORT_ROOT/libopenshot-1.0/scripts/build-mac-libopenshot.sh"; then
    SCRIPT="$PORT_ROOT/libopenshot-1.0/scripts/build-mac-libopenshot.sh"
  elif grep -q 'LIBOPENSHOT_AUDIO_TAG' "$SCRIPT" 2>/dev/null; then :
  else die "no build script that understands tag $TAG yet: the libopenshot-1.0 feature must land (or be in progress at $PORT_ROOT/libopenshot-1.0) before $PREFIX can be built"; fi
fi
log "building libopenshot ${TAG:-v0.5.0} into $PREFIX with $SCRIPT (10-20 min)"
ZENVI_DEPS="$PREFIX" LIBOPENSHOT_TAG="${TAG:-v0.5.0}" ${AUDIO_TAG:+LIBOPENSHOT_AUDIO_TAG="$AUDIO_TAG"} SKIP_BREW="${SKIP_BREW:-}" \
  bash "$SCRIPT" 2>&1 | tee -a "$LOG_DIR/build-$(basename "$PREFIX").log"
[[ -f "$PREFIX/python/_openshot.so" ]] || die "build finished but $PREFIX/python/_openshot.so is missing"
log "libopenshot ready at $PREFIX"
