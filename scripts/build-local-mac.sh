#!/usr/bin/env bash
# scripts/build-local-mac.sh — Local macOS build and test script for Zenvi
#
# Usage:
#   bash scripts/build-local-mac.sh [x86_64|arm64]
#
# Requires: Python 3.11, PyQt5, cx_Freeze 7.0.0
#   pip3 install -r requirements.txt
#
# Optional env vars:
#   ZENVI_DEPS            — libopenshot prefix to bundle (default: ~/zenvi-deps)
#   OPENCV_ROOT           — OpenCV prefix libopenshot was built against
#                           (default: `brew --prefix opencv@4`); set OPENCV_ROOT=""
#                           to skip OpenCV staging. Only the OpenCV dylibs that
#                           libopenshot actually loads are bundled (Object Detector /
#                           Object Mask effects need them).
#   SIGN_IDENTITY         — codesign identity for production signing
#   MAC_NOTARIZE_PASSWORD — notarytool password (requires SIGN_IDENTITY)
#   APPLE_ID              — Apple ID for notarytool
#   TEAM_ID               — Apple Team ID for notarytool

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ARCH="${1:-$(uname -m)}"
APP_NAME="${APP_NAME:-Zenvi}"

# Parse version from source
VER=$(python3 -c "
import re, pathlib
text = pathlib.Path('src/classes/info.py').read_text()
m = re.search(r'VERSION\s*=\s*\"([^\"]+)\"', text)
print(m.group(1))
")

echo "======================================"
echo " Zenvi Local macOS Build"
echo " Version : $VER"
echo " Arch    : $ARCH"
echo " Sign    : ${SIGN_IDENTITY:-ad-hoc (no cert)}"
echo "======================================"
echo ""

# ── Step 1: Install Python dependencies ──────────────────────────────────────
echo "[1/6] Installing Python dependencies..."
pip3 install --upgrade pip
pip3 install -r requirements.txt
pip3 install pyobjc-framework-Cocoa 2>/dev/null || true

# ── Step 2: Stage OpenCV runtime for the bundle ──────────────────────────────
# libopenshot links Homebrew's OpenCV; its dylibs reference each other via
# @rpath, which cx_Freeze cannot follow. Stage the referenced closure with
# concrete paths first (installer/fix_opencv_rpath.py, from upstream OpenShot)
# and let freeze.py bundle it via OPENCV_ROOT / OPENCV_FREEZE_LIB_PATH.
# NOTE: the fixer rewrites load commands inside $ZENVI_DEPS in place (the
# staged paths remain valid for from-source runs).
ZENVI_DEPS="${ZENVI_DEPS:-$HOME/zenvi-deps}"
export ZENVI_OPENSHOT_INSTALL="${ZENVI_OPENSHOT_INSTALL:-$ZENVI_DEPS}"
echo "[2/6] Staging OpenCV runtime libraries..."
if [ -z "${OPENCV_ROOT+x}" ]; then
  OPENCV_ROOT="$(brew --prefix opencv@4 2>/dev/null || true)"
fi
OPENSHOT_BINDING="$ZENVI_DEPS/python/_openshot.so"
if [ -n "$OPENCV_ROOT" ] && [ -f "$OPENSHOT_BINDING" ] \
   && otool -L "$ZENVI_DEPS/lib/libopenshot.dylib" 2>/dev/null | grep -q opencv; then
  export OPENCV_ROOT
  export OPENCV_FREEZE_LIB_PATH="$REPO_ROOT/build/opencv-freeze-lib"
  rm -rf "$OPENCV_FREEZE_LIB_PATH"
  python3 installer/fix_opencv_rpath.py --only-referenced \
    "$OPENSHOT_BINDING" "$OPENCV_ROOT" "$OPENCV_FREEZE_LIB_PATH" \
    "$ZENVI_DEPS"/lib/libopenshot*.dylib
  echo "  OpenCV staged from $OPENCV_ROOT -> $OPENCV_FREEZE_LIB_PATH"
else
  unset OPENCV_ROOT OPENCV_FREEZE_LIB_PATH
  echo "  libopenshot at $ZENVI_DEPS has no OpenCV link (or OPENCV_ROOT unset); skipping."
fi

# ── Step 3: Freeze ────────────────────────────────────────────────────────────
echo "[3/6] Running cx_Freeze (build)..."
python3 freeze.py build --git-branch=production

# ── Step 4: Locate frozen output ─────────────────────────────────────────────
FROZEN_DIR=$(find build -maxdepth 1 -type d -name 'exe.*' | head -1)
if [ -z "$FROZEN_DIR" ]; then
  echo "ERROR: No cx_Freeze output directory found in build/. The freeze step likely failed."
  exit 1
fi
echo "Found frozen dir: $FROZEN_DIR"

# ── Step 5: Construct .app bundle ────────────────────────────────────────────
echo "[4/6] Constructing .app bundle..."
APP="${APP_NAME}.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp -R "$FROZEN_DIR"/* "$APP/Contents/MacOS/"

# Substitute VERSION in Info.plist
sed "s/VERSION/${VER}/g" installer/Info.plist > "$APP/Contents/Resources/Info.plist"
cp "$APP/Contents/Resources/Info.plist" "$APP/Contents/Info.plist"

# Icon
if [ -f "installer/zenvi.icns" ]; then
  cp installer/zenvi.icns "$APP/Contents/Resources/icon.icns"
else
  ICON=$(find xdg images -name "*.png" -path "*256*" 2>/dev/null | head -1)
  [ -n "$ICON" ] && cp "$ICON" "$APP/Contents/Resources/icon.png"
fi

# Ensure all known entry points are executable.
# Info.plist declares CFBundleExecutable=launch-mac so it must be +x.
for bin in zenvi launch launch-zenvi launch-mac; do
  [ -f "$APP/Contents/MacOS/$bin" ] && chmod +x "$APP/Contents/MacOS/$bin" || true
done

# ── Step 6: Sign ─────────────────────────────────────────────────────────────
echo "[5/6] Signing..."
if [ -n "${SIGN_IDENTITY:-}" ]; then
  echo "  Production signing with identity: $SIGN_IDENTITY"
  find build \( -name '*.dylib' -o -name '*.so' \) \
    -exec codesign -s "$SIGN_IDENTITY" --timestamp=http://timestamp.apple.com/ts01 \
      --entitlements installer/zenvi.entitlements --force "{}" \;
  codesign -s "$SIGN_IDENTITY" --force --deep \
    --entitlements installer/zenvi.entitlements \
    --options runtime --timestamp=http://timestamp.apple.com/ts01 \
    "$APP"
  spctl -a -vv "$APP"
else
  echo "  Ad-hoc signing (no SIGN_IDENTITY set)"
  find build \( -name '*.dylib' -o -name '*.so' \) \
    -exec codesign -s - --force "{}" \; 2>/dev/null || true
  codesign -s - --deep --force "$APP"
fi

# ── Step 7: Create DMG ────────────────────────────────────────────────────────
DMG_NAME="Zenvi-v${VER}-${ARCH}.dmg"
echo "[6/6] Creating branded DMG: $DMG_NAME"
bash "$REPO_ROOT/installer/create-zenvi-dmg.sh" "$APP" "$DMG_NAME"

echo ""
echo "======================================"
echo " Build complete!"
echo " Output: $(pwd)/$DMG_NAME"
echo "======================================"
echo ""
echo "Test instructions:"
echo "  1. Double-click $DMG_NAME in Finder to mount it"
echo "  2. Drag Zenvi.app to the Applications folder"
echo "  3. First launch: right-click Zenvi.app → Open → click Open in the dialog"
echo "     (This bypass is only needed once when there is no Apple Developer cert)"
echo "  4. Verify the app launches, opens a project, and plays back video"
echo ""
