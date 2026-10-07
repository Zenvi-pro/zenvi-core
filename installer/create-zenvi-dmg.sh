#!/usr/bin/env bash
# create-zenvi-dmg.sh — Build a branded Zenvi drag-to-Applications DMG.
#
# Usage:
#   bash installer/create-zenvi-dmg.sh <path-to-Zenvi.app> <output.dmg>
#
# Requires: create-dmg (brew install create-dmg), tiffutil, ditto, codesign tools
# optional for the caller.
#
# macOS Tahoe (26.x): Finder draws bare /Applications symlinks as blank icons
# inside DMG windows (create-dmg --app-drop-link). Stage a real Mac alias and
# bake the Applications folder icon so the drop target renders correctly.

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <path-to-Zenvi.app> <output.dmg>" >&2
  exit 2
fi

APP_PATH="$1"
DMG_PATH="$2"

if [ ! -d "$APP_PATH" ]; then
  echo "ERROR: App bundle not found: $APP_PATH" >&2
  exit 1
fi

APP_BASENAME="$(basename "$APP_PATH")"
if [ "$APP_BASENAME" != "Zenvi.app" ]; then
  echo "ERROR: Expected a bundle named Zenvi.app, got: $APP_BASENAME" >&2
  exit 1
fi

if ! command -v create-dmg >/dev/null 2>&1; then
  echo "ERROR: create-dmg not found. Install with: brew install create-dmg" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BG_1X="$SCRIPT_DIR/dmg-background.png"
BG_2X="$SCRIPT_DIR/dmg-background@2x.png"
VOLICON="$SCRIPT_DIR/dmg-volume.icns"

for f in "$BG_1X" "$BG_2X" "$VOLICON"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: Required asset missing: $f" >&2
    exit 1
  fi
done

TMP="$(mktemp -d "${TMPDIR:-/tmp}/zenvi-dmg.XXXXXX")"
STAGE="$TMP/stage"
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$STAGE"
ditto "$APP_PATH" "$STAGE/Zenvi.app"

# Tahoe: prefer a real Mac alias over create-dmg's --app-drop-link symlink.
# Alias files carry icon resources; bare symlinks render blank in dark mode.
stage_applications_drop_target() {
  rm -f "$STAGE/Applications" "$STAGE/Applications alias"
  if osascript - "$STAGE" <<'APPLESCRIPT'
on run argv
  set stagingPath to item 1 of argv
  tell application "Finder"
    make new alias file to POSIX file "/Applications" at POSIX file stagingPath with properties {name:"Applications"}
  end tell
end run
APPLESCRIPT
  then
    if [ -e "$STAGE/Applications alias" ] && [ ! -e "$STAGE/Applications" ]; then
      mv "$STAGE/Applications alias" "$STAGE/Applications"
    fi
  else
    echo "WARNING: Finder alias failed; falling back to /Applications symlink" >&2
    ln -s /Applications "$STAGE/Applications"
  fi

  if [ ! -e "$STAGE/Applications" ]; then
    echo "ERROR: Failed to stage Applications drop target" >&2
    exit 1
  fi

  # Bake /Applications' icon into the alias so Finder never has to resolve it.
  if ! swift - "$STAGE/Applications" <<'SWIFT'
import AppKit
let target = CommandLine.arguments[1]
let icon = NSWorkspace.shared.icon(forFile: "/Applications")
guard NSWorkspace.shared.setIcon(icon, forFile: target, options: []) else {
  fputs("setIcon failed for \(target)\n", stderr)
  exit(1)
}
SWIFT
  then
    echo "WARNING: could not bake Applications icon (continuing)" >&2
  fi
}

echo "Staging Applications drop target (Mac alias + baked icon)..."
stage_applications_drop_target

# Tag DPI and prefer the 1x PNG for create-dmg.
# Multi-rep TIFF letterboxes (black bars) on recent macOS Finder; a plain
# PNG matching --window-size fills the content area edge-to-edge.
sips -s dpiWidth 72 -s dpiHeight 72 "$BG_1X" >/dev/null
BG_FILE="$TMP/dmg-background.png"
cp "$BG_1X" "$BG_FILE"

rm -f "$DMG_PATH"

# create-dmg may flake when hdiutil reports the device busy; retry a few times.
attempt=1
max_attempts=3
while true; do
  echo "create-dmg attempt $attempt/$max_attempts ..."
  set +e
  create-dmg \
    --volname "Zenvi" \
    --volicon "$VOLICON" \
    --background "$BG_FILE" \
    --window-pos 200 120 \
    --window-size 600 450 \
    --icon-size 110 \
    --text-size 12 \
    --icon "Zenvi.app" 150 210 \
    --icon "Applications" 450 210 \
    --hide-extension "Zenvi.app" \
    --no-internet-enable \
    "$DMG_PATH" \
    "$STAGE"
  status=$?
  set -e

  if [ "$status" -eq 0 ] && [ -f "$DMG_PATH" ] && [ -s "$DMG_PATH" ]; then
    echo "Created $DMG_PATH ($(du -sh "$DMG_PATH" | cut -f1))"
    # Sanity: Applications drop target + app must exist inside the image.
    VERIFY_MOUNT="$(mktemp -d "${TMPDIR:-/tmp}/zenvi-dmg-verify.XXXXXX")"
    if hdiutil attach "$DMG_PATH" -mountpoint "$VERIFY_MOUNT" -nobrowse -quiet; then
      if [ ! -e "$VERIFY_MOUNT/Applications" ] || [ ! -d "$VERIFY_MOUNT/Zenvi.app" ]; then
        hdiutil detach "$VERIFY_MOUNT" -quiet || true
        rm -rf "$VERIFY_MOUNT"
        echo "ERROR: DMG missing Zenvi.app or Applications drop target" >&2
        exit 1
      fi
      # Exactly one Applications entry (no duplicate symlink + alias).
      apps_count=$(find "$VERIFY_MOUNT" -maxdepth 1 \( -name 'Applications' -o -name 'Applications alias' \) | wc -l | tr -d ' ')
      if [ "$apps_count" -ne 1 ]; then
        hdiutil detach "$VERIFY_MOUNT" -quiet || true
        rm -rf "$VERIFY_MOUNT"
        echo "ERROR: Expected exactly one Applications drop target, found $apps_count" >&2
        exit 1
      fi
      hdiutil detach "$VERIFY_MOUNT" -quiet || true
      rm -rf "$VERIFY_MOUNT"
    fi
    exit 0
  fi

  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "ERROR: create-dmg failed after $max_attempts attempts (exit=$status)" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 3
done
