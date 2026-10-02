#!/usr/bin/env bash
# installer/apply-libopenshot-patches.sh SOURCE_DIR [PATCH...]
#
# Applies Zenvi's source patches (installer/mac-patches/*.patch) to an upstream
# libopenshot or libopenshot-audio checkout. Every build path uses it, so they
# all follow one rule:
#   - scripts/build-mac-libopenshot.sh
#   - installer/ci-win-msys-libopenshot.sh (Windows release job)
#   - the macOS job in .github/workflows/release.yml
#   - run-win.ps1 (Windows setup from source)
#
# For each patch:
#   - it applies cleanly                -> apply it
#   - it is already applied             -> skip it, so a re-run is safe
#   - it neither applies nor is applied -> fail the build
#
# A patch that stops applying means upstream changed the code it fixes. Someone
# has to look: drop the patch if upstream fixed the bug, otherwise rebase it.
# Skipping it quietly would ship the bug.

set -euo pipefail

fail() {
  # Surface the failure as an annotation on GitHub Actions.
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::error::$1"
  fi
  echo "ERROR: $1" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  echo "usage: $0 SOURCE_DIR [PATCH...]" >&2
  exit 2
fi
src_dir="$1"
shift
[[ -d "$src_dir" ]] || fail "source directory not found: $src_dir"

for patch in "$@"; do
  name="$(basename "$patch")"
  [[ -f "$patch" ]] || fail "patch not found: $patch"
  if git -C "$src_dir" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
    echo "  Applying $name"
    git -C "$src_dir" apply --whitespace=nowarn "$patch"
  elif git -C "$src_dir" apply --reverse --check --whitespace=nowarn "$patch" 2>/dev/null; then
    echo "  Skipping $name (already applied)"
  else
    # Show git's own reason (which hunk failed) before failing.
    git -C "$src_dir" apply --check --whitespace=nowarn "$patch" >&2 || true
    if grep -q $'\r' "$patch"; then
      fail "$name has CRLF line endings, so it cannot apply. .gitattributes keeps installer/mac-patches/*.patch LF; check the patches out again (rm installer/mac-patches/*.patch && git checkout -- installer/mac-patches)."
    fi
    fail "$name does not apply to $src_dir. Upstream changed the patched code: drop the patch if upstream fixed the bug, otherwise rebase it."
  fi
done
