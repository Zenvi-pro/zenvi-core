#!/usr/bin/env bash
# scripts/build-mac-libopenshot.sh
#
# Builds libopenshot-audio + libopenshot from upstream OpenShot sources with the
# macOS / FFmpeg 8/9 / Apple Silicon patches required by zenvi-core.
#
# Why this exists:
# - Homebrew does not ship libopenshot. CI on arm64 builds it from source.
# - Older libopenshot tags predate FFmpeg 7/8/9 and macOS 26 — they won't
#   compile or run cleanly without patches. Homebrew currently ships FFmpeg 9,
#   which hides AVCodec.supported_samplerates / ch_layouts / sample_fmts /
#   pix_fmts (still used by every libopenshot tag up to and including v1.0.0).
# - When running zenvi-core from source (not the frozen .app), libopenshot's
#   absolute Qt paths collide with PyQt5's bundled Qt at runtime → segfault.
#   Post-build install_name_tool rewrites fix this.
#
# What this script does:
#   1. brew install all build deps (cmake, qt@5, swig, ffmpeg, libomp, etc.;
#      plus opencv@4 + protobuf when ZENVI_OPENCV=ON)
#   2. Clone OpenShot/libopenshot-audio $LIBOPENSHOT_AUDIO_TAG + apply the
#      installer/mac-patches/libopenshot-audio-<tag>-*.patch files that apply
#   3. Clone OpenShot/libopenshot $LIBOPENSHOT_TAG + apply the
#      installer/mac-patches/libopenshot-<tag>-*.patch files that apply, then
#      installer/patch-libopenshot-ffmpeg.py (FFmpeg 8/9 AVCodec lists)
#   4. cmake configure + build + install to $ZENVI_DEPS (default: $HOME/zenvi-deps)
#   5. install_name_tool: rewrite @rpath for Qt to point at PyQt5's bundled Qt
#
# Every patch is optional: it is applied only when `git apply --check` passes,
# so a tag that already contains a fix upstream just skips that patch.
#
# Result: $ZENVI_DEPS/lib/libopenshot.dylib + $ZENVI_DEPS/python/_openshot.so
# usable by:
#   - zenvi-core from source via PYTHONPATH=$ZENVI_DEPS/python python src/launch.py
#   - freeze.py via ZENVI_OPENSHOT_INSTALL=$ZENVI_DEPS python freeze.py build
#
# Usage:
#   bash scripts/build-mac-libopenshot.sh
#   ZENVI_DEPS=$HOME/zenvi-deps-1.0 LIBOPENSHOT_TAG=v1.0.0 LIBOPENSHOT_AUDIO_TAG=v1.0.0 \
#     bash scripts/build-mac-libopenshot.sh
#
# Environment:
#   ZENVI_DEPS             install prefix                 (default: $HOME/zenvi-deps)
#   SRC_DIR                where to clone the sources     (default: $HOME/src)
#   LIBOPENSHOT_TAG        libopenshot tag to build       (default: v0.5.0)
#   LIBOPENSHOT_AUDIO_TAG  libopenshot-audio tag to build (default: $LIBOPENSHOT_TAG)
#   ZENVI_OPENCV           ON|OFF, build the OpenCV effects (Tracker, Object
#                          Detector, Stabilizer) against Homebrew opencv@4
#                                                         (default: ON)
#   ZENVI_CXX_FLAGS        extra C++ compiler flags for both libraries, e.g.
#                          "-isystem $HOME/sdk-shim" when the local Command Line
#                          Tools SDK is missing headers (default: empty)
#   SKIP_BREW              set to 1 to skip brew installs

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_DIR="$REPO_ROOT/installer/mac-patches"
ZENVI_DEPS="${ZENVI_DEPS:-$HOME/zenvi-deps}"
SRC_DIR="${SRC_DIR:-$HOME/src}"
TAG="${LIBOPENSHOT_TAG:-v0.5.0}"
AUDIO_TAG="${LIBOPENSHOT_AUDIO_TAG:-$TAG}"
OPENCV="${ZENVI_OPENCV:-ON}"
EXTRA_CXX_FLAGS="${ZENVI_CXX_FLAGS:-}"
case "$OPENCV" in
  ON|OFF) ;;
  *) echo "ERROR: ZENVI_OPENCV must be ON or OFF (got '$OPENCV')"; exit 1 ;;
esac

echo "============================================="
echo " zenvi-core: Mac libopenshot rebuild"
echo "============================================="
echo "  Install prefix       : $ZENVI_DEPS"
echo "  Source dir           : $SRC_DIR"
echo "  libopenshot tag      : $TAG"
echo "  libopenshot-audio tag: $AUDIO_TAG"
echo "  OpenCV effects       : $OPENCV"
echo "  Extra C++ flags      : ${EXTRA_CXX_FLAGS:-<none>}"
echo "  Patch dir            : $PATCH_DIR"
echo "============================================="
echo ""

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: this script is macOS-only. On Linux use the system libopenshot package."
  exit 1
fi

# Apply every installer/mac-patches/<prefix>-*.patch that still applies to the
# checked-out sources. Patches are tag-locked by name; a patch that no longer
# applies (because upstream merged the fix) is skipped with a notice.
apply_patches() {
  local src_dir="$1" prefix="$2" patch
  shopt -s nullglob
  local patches=("$PATCH_DIR/${prefix}"-*.patch)
  shopt -u nullglob
  if [[ ${#patches[@]} -eq 0 ]]; then
    echo "  No patches named ${prefix}-*.patch; building pristine sources."
    return 0
  fi
  for patch in "${patches[@]}"; do
    if git -C "$src_dir" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
      echo "  Applying $(basename "$patch")"
      git -C "$src_dir" apply --whitespace=nowarn "$patch"
    else
      echo "  Skipping $(basename "$patch") (does not apply to this tag; likely already upstream)"
    fi
  done
}

# ── 1. Homebrew dependencies ────────────────────────────────────────────────
if [[ "${SKIP_BREW:-}" != "1" ]]; then
  echo "[1/5] Installing Homebrew dependencies..."
  brew install python@3.11 cmake swig pkg-config doxygen unittest-cpp \
    qt@5 ffmpeg libsamplerate libsndfile librsvg \
    zeromq cppzmq libomp openssl@3
  if [[ "$OPENCV" == "ON" ]]; then
    # opencv@4 is keg-only (its vtk dependency pulls in Qt 6, which conflicts
    # with a linked qt@5). We never rely on it being linked: its prefix is
    # passed to cmake explicitly below. Homebrew's plain `opencv` formula is
    # OpenCV 5, which libopenshot's find_package(OpenCV 4) rejects.
    brew install opencv@4 protobuf boost
  fi
fi

QT5_PREFIX="$(brew --prefix qt@5)"
LIBOMP_PREFIX="$(brew --prefix libomp)"
PY311_PREFIX="$(brew --prefix python@3.11)"
PY311="$PY311_PREFIX/bin/python3.11"
PY311_INCLUDE="$PY311_PREFIX/Frameworks/Python.framework/Versions/3.11/include/python3.11"
PY311_LIB="$PY311_PREFIX/Frameworks/Python.framework/Versions/3.11/lib/libpython3.11.dylib"

CMAKE_PREFIX="$ZENVI_DEPS;$QT5_PREFIX;$LIBOMP_PREFIX"
if [[ "$OPENCV" == "ON" ]]; then
  OPENCV_PREFIX="$(brew --prefix opencv@4)"
  PROTOBUF_PREFIX="$(brew --prefix protobuf)"
  if [[ ! -d "$OPENCV_PREFIX/lib/cmake" ]]; then
    echo "ERROR: opencv@4 not found at $OPENCV_PREFIX (run without SKIP_BREW=1, or set ZENVI_OPENCV=OFF)"
    exit 1
  fi
  CMAKE_PREFIX="$CMAKE_PREFIX;$OPENCV_PREFIX;$PROTOBUF_PREFIX"
fi

mkdir -p "$ZENVI_DEPS" "$SRC_DIR"

# ── 2. Build libopenshot-audio ──────────────────────────────────────────────
echo ""
echo "[2/5] Building libopenshot-audio $AUDIO_TAG..."
rm -rf "$SRC_DIR/libopenshot-audio"
git clone --depth=1 --branch "$AUDIO_TAG" \
  https://github.com/OpenShot/libopenshot-audio.git \
  "$SRC_DIR/libopenshot-audio"

echo "  Applying libopenshot-audio mac patches..."
apply_patches "$SRC_DIR/libopenshot-audio" "libopenshot-audio-${AUDIO_TAG}"

cmake -S "$SRC_DIR/libopenshot-audio" -B "$SRC_DIR/libopenshot-audio/build" \
  -DCMAKE_INSTALL_PREFIX="$ZENVI_DEPS" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_CXX_FLAGS="$EXTRA_CXX_FLAGS"
cmake --build "$SRC_DIR/libopenshot-audio/build" --parallel "$(sysctl -n hw.logicalcpu)"
cmake --install "$SRC_DIR/libopenshot-audio/build"

# ── 3. Build libopenshot ────────────────────────────────────────────────────
echo ""
echo "[3/5] Building libopenshot $TAG..."
rm -rf "$SRC_DIR/libopenshot"
git clone --depth=1 --branch "$TAG" \
  https://github.com/OpenShot/libopenshot.git \
  "$SRC_DIR/libopenshot"

echo "  Applying libopenshot mac patches..."
apply_patches "$SRC_DIR/libopenshot" "libopenshot-${TAG}"
echo "  Applying FFmpeg 8/9 AVCodec compatibility patch..."
"$PY311" "$REPO_ROOT/installer/patch-libopenshot-ffmpeg.py" "$SRC_DIR/libopenshot"

# USE_QT6=OFF: libopenshot >= 1.0 auto-detects Qt 6 when a Homebrew Qt 6 is
# present (opencv@4 drags one in via vtk). PyQt5 needs the Qt 5 build.
cmake -S "$SRC_DIR/libopenshot" -B "$SRC_DIR/libopenshot/build" \
  -DCMAKE_INSTALL_PREFIX="$ZENVI_DEPS" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DENABLE_TESTS=OFF \
  -DENABLE_RUBY=OFF \
  -DENABLE_JAVA=OFF \
  -DENABLE_PYTHON=ON \
  -DENABLE_MAGICK=OFF \
  -DENABLE_OPENCV="$OPENCV" \
  -DUSE_QT6=OFF \
  -DPYTHON_EXECUTABLE="$PY311" \
  -DPYTHON_INCLUDE_DIR="$PY311_INCLUDE" \
  -DPYTHON_LIBRARY="$PY311_LIB" \
  -DOpenMP_ROOT="$LIBOMP_PREFIX" \
  -DCMAKE_PREFIX_PATH="$CMAKE_PREFIX" \
  -DCMAKE_CXX_FLAGS="-I$LIBOMP_PREFIX/include -Wno-deprecated-declarations $EXTRA_CXX_FLAGS" \
  -DCMAKE_EXE_LINKER_FLAGS="-L$LIBOMP_PREFIX/lib -lomp" \
  -DCMAKE_SHARED_LINKER_FLAGS="-L$LIBOMP_PREFIX/lib -lomp"
cmake --build "$SRC_DIR/libopenshot/build" --parallel "$(sysctl -n hw.logicalcpu)"
cmake --install "$SRC_DIR/libopenshot/build"

# ── 4. Rewrite Qt absolute paths → @rpath, add PyQt5 wheel Qt as rpath ──────
echo ""
echo "[4/5] Rewriting dylib references for from-source runs..."

# When running zenvi-core from source (not the frozen .app), libopenshot would
# load /opt/homebrew/opt/qt@5/... (its compile-time Qt) while PyQt5 loads its
# wheel-bundled Qt → two QApplication singletons in one process → segfault.
# Solution: rewrite Qt deps to @rpath, then add the PyQt5 wheel's Qt as rpath.
# Result: both libraries share PyQt5's Qt instance.
PYQT5_QT_LIB="$REPO_ROOT/.venv/lib/python3.11/site-packages/PyQt5/Qt5/lib"
HB_QT5_LIB="$QT5_PREFIX/lib"

if [[ ! -d "$PYQT5_QT_LIB" ]]; then
  echo "  WARN: PyQt5 wheel not found at $PYQT5_QT_LIB"
  echo "        Run: python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo "        Then re-run this script (just step 4 — set SKIP_BREW=1 to skip rebuilds)."
  exit 1
fi
# Bake the *resolved* path into the rpath: .venv may be a symlink (git worktrees
# share the main checkout's venv), and a symlinked rpath dies with the worktree.
PYQT5_QT_LIB="$(cd "$PYQT5_QT_LIB" && pwd -P)"

# The versioned dylib name follows the tag (libopenshot.0.5.0.dylib,
# libopenshot.1.0.0.dylib, ...); rewrite every real libopenshot dylib plus the
# Python extension. Symlinks (libopenshot.dylib, libopenshot.<so>.dylib) point
# at the same file, so `-type f` is enough.
LIBOPENSHOT_DYLIBS=()
while IFS= read -r f; do LIBOPENSHOT_DYLIBS+=("$f"); done < <(
  find "$ZENVI_DEPS/lib" -maxdepth 1 -type f -name 'libopenshot*.dylib' | sort)
if [[ ${#LIBOPENSHOT_DYLIBS[@]} -eq 0 ]]; then
  echo "ERROR: no libopenshot*.dylib found under $ZENVI_DEPS/lib"
  exit 1
fi

for f in "${LIBOPENSHOT_DYLIBS[@]}" "$ZENVI_DEPS/python/_openshot.so"; do
  for qt_ref in $(otool -L "$f" 2>/dev/null | awk -v hb="$HB_QT5_LIB" '$1 ~ hb {print $1}'); do
    new_ref="@rpath/${qt_ref#$HB_QT5_LIB/}"
    install_name_tool -change "$qt_ref" "$new_ref" "$f"
  done
  install_name_tool -add_rpath "$PYQT5_QT_LIB" "$f" 2>/dev/null || true
  install_name_tool -add_rpath "$ZENVI_DEPS/lib"  "$f" 2>/dev/null || true
  echo "  Rewrote $(basename "$f")"
done

# ── 5. Verify ───────────────────────────────────────────────────────────────
echo ""
echo "[5/5] Verifying..."
PYTHONPATH="$ZENVI_DEPS/python" "$PY311" -c "
import json
import openshot
print('  ✓ libopenshot', openshot.OPENSHOT_VERSION_FULL)
print('  ✓ Frame.GetBytes :', hasattr(openshot.Frame, 'GetBytes'))
print('  ✓ Frame.GetImage :', hasattr(openshot.Frame, 'GetImage'))
effects = sorted(e.get('class_name', '') for e in json.loads(openshot.EffectInfo.Json()))
print('  ✓ %d effects     :' % len(effects), ', '.join(effects))
print('  ✓ OpenCV effects :', all(name in effects for name in ('Tracker', 'ObjectDetection', 'Stabilizer')))
t = openshot.Timeline(1280, 720, openshot.Fraction(30,1), 48000, 2, openshot.LAYOUT_STEREO)
print('  ✓ Timeline construct OK')
"

echo ""
echo "============================================="
echo " Done. To run zenvi-core from source:"
echo ""
echo "   export ZENVI_OPENSHOT_INSTALL=$ZENVI_DEPS"
echo "   export PYTHONPATH=$ZENVI_DEPS/python"
echo "   export QT_MAC_WANTS_LAYER=1"
echo "   export QTWEBENGINE_DISABLE_SANDBOX=1"
echo "   .venv/bin/python src/launch.py"
echo ""
echo " or simply:  ZENVI_DEPS=$ZENVI_DEPS ./run.sh"
echo ""
echo " To freeze a DMG:"
echo "   ZENVI_OPENSHOT_INSTALL=$ZENVI_DEPS bash scripts/build-local-mac.sh arm64"
echo "============================================="
