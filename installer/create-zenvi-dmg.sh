#!/usr/bin/env bash
# create-zenvi-dmg.sh — Build a branded Zenvi drag-to-Applications DMG.
#
# Usage:
#   bash installer/create-zenvi-dmg.sh <path-to-Zenvi.app> <output.dmg>
#
# Requires: create-dmg (brew install create-dmg), tiffutil, ditto, codesign tools
# optional for the caller.

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
    --hide-extension "Zenvi.app" \
    --app-drop-link 450 210 \
    --no-internet-enable \
    "$DMG_PATH" \
    "$STAGE"
  status=$?
  set -e

  if [ "$status" -eq 0 ] && [ -f "$DMG_PATH" ] && [ -s "$DMG_PATH" ]; then
    echo "Created $DMG_PATH ($(du -sh "$DMG_PATH" | cut -f1))"
    # Sanity: Applications drop link must exist inside the image.
    VERIFY_MOUNT="$(mktemp -d "${TMPDIR:-/tmp}/zenvi-dmg-verify.XXXXXX")"
    if hdiutil attach "$DMG_PATH" -mountpoint "$VERIFY_MOUNT" -nobrowse -quiet; then
      if [ ! -L "$VERIFY_MOUNT/Applications" ] || [ ! -d "$VERIFY_MOUNT/Zenvi.app" ]; then
        hdiutil detach "$VERIFY_MOUNT" -quiet || true
        rm -rf "$VERIFY_MOUNT"
        echo "ERROR: DMG missing Zenvi.app or Applications drop link" >&2
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
