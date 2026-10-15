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
# freeze.py copies openshot.py + _openshot.so from this dir into frozen lib/.
# Without it, import-based discovery fails and the app launches without openshot.
export ZENVI_OPENSHOT_PYROOT="${ZENVI_OPENSHOT_PYROOT:-$ZENVI_DEPS/python}"
if [ ! -f "$ZENVI_OPENSHOT_PYROOT/_openshot.so" ] && [ ! -f "$ZENVI_OPENSHOT_PYROOT/openshot.py" ]; then
  echo "ERROR: openshot bindings not found in $ZENVI_OPENSHOT_PYROOT"
  echo "       Run: bash scripts/build-mac-libopenshot.sh"
  exit 1
fi
export PYTHONPATH="$ZENVI_OPENSHOT_PYROOT${PYTHONPATH:+:$PYTHONPATH}"
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

# Same absolute-path rewrite release CI runs so _openshot.so does not keep
# host .venv / Homebrew / zenvi-deps load commands.
echo "  Rewriting Mach-O dependency paths for a portable bundle..."
python3 "$REPO_ROOT/installer/fix_macos_dylib_paths.py" "$FROZEN_DIR"
if [ ! -f "$FROZEN_DIR/lib/openshot.py" ] || [ ! -f "$FROZEN_DIR/lib/_openshot.so" ]; then
  echo "ERROR: frozen build missing lib/openshot.py or lib/_openshot.so"
  echo "       Check ZENVI_OPENSHOT_PYROOT=$ZENVI_OPENSHOT_PYROOT"
  exit 1
fi

# Match release.yml: cx_Freeze can mix Python's libssl with a different
# Homebrew libcrypto. Replace both from the same openssl@3 prefix.
OPENSSL_PREFIX="$(brew --prefix openssl@3 2>/dev/null || true)"
if [ -n "$OPENSSL_PREFIX" ] \
   && [ -f "$OPENSSL_PREFIX/lib/libssl.3.dylib" ] \
   && [ -f "$OPENSSL_PREFIX/lib/libcrypto.3.dylib" ]; then
  echo "  Syncing OpenSSL pair from $OPENSSL_PREFIX"
  for search_dir in "$FROZEN_DIR" "$FROZEN_DIR/lib"; do
    [ -d "$search_dir" ] || continue
    if [ -f "$search_dir/libssl.3.dylib" ]; then
      chmod u+w "$search_dir/libssl.3.dylib" 2>/dev/null || true
      cp -f "$OPENSSL_PREFIX/lib/libssl.3.dylib" "$search_dir/libssl.3.dylib"
      install_name_tool -id "@executable_path/lib/libssl.3.dylib" "$search_dir/libssl.3.dylib" 2>/dev/null || true
      install_name_tool -change "$OPENSSL_PREFIX/lib/libcrypto.3.dylib" \
        "@loader_path/libcrypto.3.dylib" "$search_dir/libssl.3.dylib" 2>/dev/null || true
    fi
    if [ -f "$search_dir/libcrypto.3.dylib" ]; then
      chmod u+w "$search_dir/libcrypto.3.dylib" 2>/dev/null || true
      cp -f "$OPENSSL_PREFIX/lib/libcrypto.3.dylib" "$search_dir/libcrypto.3.dylib"
      install_name_tool -id "@executable_path/lib/libcrypto.3.dylib" "$search_dir/libcrypto.3.dylib" 2>/dev/null || true
    fi
  done
fi

# ── Step 5: Construct .app bundle ────────────────────────────────────────────
echo "[4/6] Constructing .app bundle..."
APP="${APP_NAME}.app"
# Wipe any previous local .app (may contain Resources/lib symlinks from an
# earlier pack that break cp -R of directories into Contents/MacOS/).
rm -rf "$APP"
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

# Match release.yml: remove packaging junk, relocate non-code out of MacOS/,
# then ad-hoc-sign every Mach-O. Leaving text/icons under MacOS/ makes
# codesign --deep fail and dyld kill the app with "Code Signature Invalid".
find "$APP" -type d \( -name '*.egg-info' -o -name '.hash' \) -exec rm -rf {} + 2>/dev/null || true
xattr -cr "$APP" 2>/dev/null || true
mkdir -p "$APP/Contents/Resources"

MOVED=0
while IFS= read -r -d '' f; do
  if ! file "$f" 2>/dev/null | grep -q "Mach-O"; then
    base=$(basename "$f")
    case "$base" in
      launch-mac|launch|launch-zenvi|zenvi) continue ;;
    esac
    if [ -e "$APP/Contents/Resources/$base" ]; then
      rm -f "$f"
    else
      mv "$f" "$APP/Contents/Resources/"
    fi
    MOVED=$((MOVED + 1))
  fi
done < <(find "$APP/Contents/MacOS" -maxdepth 1 -type f -print0)
echo "  Moved $MOVED top-level non-Mach-O file(s) to Contents/Resources/"

while IFS= read -r -d '' d; do
  name=$(basename "$d")
  [ -L "$d" ] && continue
  [ -e "$APP/Contents/Resources/$name" ] && continue
  mv "$d" "$APP/Contents/Resources/$name"
  ln -s "../Resources/$name" "$APP/Contents/MacOS/$name"
  echo "  Relocated $name/ → Contents/Resources/$name"
done < <(find "$APP/Contents/MacOS" -mindepth 1 -maxdepth 1 -type d -print0)

chmod -R a+r "$APP/Contents/"
find "$APP" \( -name '*.dylib' -o -name '*.so' \) -exec chmod +x {} \;
for bin in zenvi launch launch-zenvi launch-mac; do
  [ -f "$APP/Contents/MacOS/$bin" ] && chmod +x "$APP/Contents/MacOS/$bin"
done

# ── Step 6: Sign ─────────────────────────────────────────────────────────────
echo "[5/6] Signing..."
if [ -n "${SIGN_IDENTITY:-}" ]; then
  echo "  Production signing with identity: $SIGN_IDENTITY"
  while IFS= read -r -d '' f; do
    file "$f" 2>/dev/null | grep -q "Mach-O" || continue
    codesign -s "$SIGN_IDENTITY" \
      --timestamp=http://timestamp.apple.com/ts01 \
      --entitlements installer/zenvi.entitlements \
      --force "$f" 2>/dev/null || true
  done < <(find "$APP/Contents" -type f -print0)
  codesign -s "$SIGN_IDENTITY" --force \
    --entitlements installer/zenvi.entitlements \
    --options runtime --timestamp=http://timestamp.apple.com/ts01 \
    "$APP"
  spctl -a -vv "$APP"
else
  echo "  Ad-hoc signing Mach-O binaries (no SIGN_IDENTITY set)"
  while IFS= read -r -d '' f; do
    file "$f" 2>/dev/null | grep -q "Mach-O" \
      && codesign -s - --force "$f" 2>/dev/null || true
  done < <(find "$APP/Contents" -type f -print0)
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
