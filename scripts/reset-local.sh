#!/usr/bin/env bash
#
# reset-local.sh — Reset the zenvi-core checkout to a pristine, just-cloned state.
#
# Removes build output, the Python virtualenv, bytecode/image caches, packaged
# installers and local env files, restores tracked files to HEAD, and (with a
# flag) also clears the desktop app's on-disk state in ~/.openshot_qt so the app
# behaves like a fresh install.
#
# Usage:
#   scripts/reset-local.sh [options]
#
# Options:
#   -y, --force          Do not prompt for confirmation.
#       --reinstall      Recreate the virtualenv and reinstall requirements.
#       --clear-app-state  Also reset ~/.openshot_qt (settings + saved auth).
#       --keep-env       Preserve local .env files.
#       --dry-run        Show what would be removed without removing anything.
#   -h, --help           Show this help and exit.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FORCE=0
REINSTALL=0
CLEAR_APP_STATE=0
KEEP_ENV=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--force)        FORCE=1 ;;
    --reinstall)       REINSTALL=1 ;;
    --clear-app-state) CLEAR_APP_STATE=1 ;;
    --keep-env)        KEEP_ENV=1 ;;
    --dry-run)         DRY_RUN=1 ;;
    -h|--help)         sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

run() { if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] $*"; else eval "$*"; fi; }

echo "==> zenvi-core local reset"
echo "    Repo: $REPO_ROOT"
if [[ $DRY_RUN -eq 1 ]]; then echo "    Mode: DRY RUN (nothing will be deleted)"; fi

if [[ $FORCE -ne 1 && $DRY_RUN -ne 1 ]]; then
  echo
  echo "This will DISCARD all uncommitted changes, remove the build/venv, and"
  [[ $CLEAR_APP_STATE -eq 1 ]] && echo "RESET the desktop app state in ~/.openshot_qt."
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# 1. Restore tracked files to HEAD.
echo "==> Restoring tracked files to HEAD"
run "git reset --hard HEAD"

# 2. Remove build output, venv, caches, packaged installers.
echo "==> Removing build output, virtualenv and caches"
CLEAN_EXCLUDES=()
if [[ $KEEP_ENV -eq 1 ]]; then
  CLEAN_EXCLUDES+=("-e" ".env" "-e" ".env.*")
fi
run "git clean -xfd ${CLEAN_EXCLUDES[*]:-}"

# Belt-and-suspenders removal (matches .gitignore artifacts).
run "find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true"
for path in build dist openshot_qt.egg-info openshot_qt protobuf_data doc/_build \
            .venv .pytest_cache remotion-service/node_modules remotion-service/output; do
  [[ -e "$path" ]] && run "rm -rf '$path'"
done
run "rm -f ./*.deb"
run "rm -f src/images/cache/objectdetection.png src/images/cache/objectdetection@2x.png 2>/dev/null || true"

# 3. Restore local env from example.
if [[ $KEEP_ENV -ne 1 && -f .env.example && ! -f .env ]]; then
  echo "==> Seeding .env from .env.example (fill in secrets before running)"
  run "cp .env.example .env"
fi

# 4. Reset desktop app state.
if [[ $CLEAR_APP_STATE -eq 1 ]]; then
  APP_STATE_DIR="${HOME}/.openshot_qt"
  if [[ -d "$APP_STATE_DIR" ]]; then
    echo "==> Clearing desktop app state: $APP_STATE_DIR"
    # Remove zenvi-specific local state; leave a clean directory behind.
    run "rm -f '$APP_STATE_DIR/zenvi_auth.json'"
    run "rm -f '$APP_STATE_DIR/openshot.settings'"
    run "rm -rf '$APP_STATE_DIR/recovery' '$APP_STATE_DIR/backup' '$APP_STATE_DIR/blender' '$APP_STATE_DIR/thumbnail'"
  fi
fi

# 5. Optionally re-bootstrap.
if [[ $REINSTALL -eq 1 ]]; then
  echo "==> Recreating virtualenv and installing requirements"
  run "python3 -m venv .venv"
  run "./.venv/bin/pip install --upgrade pip"
  [[ -f requirements.txt ]] && run "./.venv/bin/pip install -r requirements.txt"
fi

echo "==> Done. Checkout reset to a pristine state."
[[ $REINSTALL -ne 1 ]] && echo "    Next: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
exit 0
