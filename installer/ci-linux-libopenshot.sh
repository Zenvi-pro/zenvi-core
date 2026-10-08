#!/usr/bin/env bash
# Build libopenshot-audio + libopenshot from source on Ubuntu 22.04 (Linux
# release job). Replaces the apt "libopenshot-daily" PPA, whose version we did
# not control and which could be older than MINIMUM_LIBOPENSHOT_VERSION.
#
# Installs to /usr/local (libraries are ldconfig'd; Python bindings land in
# /usr/local/python) and exports the paths freeze.py needs via GITHUB_ENV.
# Tags default to the same v1.0.0 the macOS and Windows jobs build.

set -euo pipefail

LIBOPENSHOT_TAG="${LIBOPENSHOT_TAG:-v1.0.0}"
LIBOPENSHOT_AUDIO_TAG="${LIBOPENSHOT_AUDIO_TAG:-$LIBOPENSHOT_TAG}"
ZENVI_OPENCV="${ZENVI_OPENCV:-ON}"
WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PATCH_DIR="${WORKSPACE}/installer/mac-patches"
PREFIX=/usr/local
SRC="${RUNNER_TEMP:-/tmp}/libopenshot-src"

sudo apt-get update
sudo apt-get install -y \
  build-essential cmake ninja-build git swig pkg-config \
  python3-dev libasound2-dev libfreetype6-dev libx11-dev libxext-dev \
  libxinerama-dev libxrandr-dev libxcursor-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
  libswresample-dev libavfilter-dev libavdevice-dev \
  libzmq3-dev libjsoncpp-dev libomp-dev \
  qtbase5-dev qtbase5-private-dev qtmultimedia5-dev libqt5svg5-dev \
  libopencv-dev libprotobuf-dev protobuf-compiler \
  libfuse2 desktop-file-utils

apply_patches() {
  local src_dir="$1" prefix="$2"
  shopt -s nullglob
  local patches=("${PATCH_DIR}/${prefix}"-*.patch)
  shopt -u nullglob
  if [[ ${#patches[@]} -eq 0 ]]; then
    echo "No patches named ${prefix}-*.patch"
    return 0
  fi
  bash "${WORKSPACE}/installer/apply-libopenshot-patches.sh" "${src_dir}" "${patches[@]}"
}

rm -rf "${SRC}"
mkdir -p "${SRC}"

# libopenshot-audio: no patches. The only one (libopenshot-audio-*-mac.patch) is
# macOS-only, so it is deliberately not applied here.
git clone --depth 1 --branch "${LIBOPENSHOT_AUDIO_TAG}" \
  https://github.com/OpenShot/libopenshot-audio.git "${SRC}/audio"
cmake -S "${SRC}/audio" -B "${SRC}/audio/build" \
  -DCMAKE_INSTALL_PREFIX="${PREFIX}" -DCMAKE_BUILD_TYPE=Release
cmake --build "${SRC}/audio/build" --parallel "$(nproc)"
sudo cmake --install "${SRC}/audio/build"
sudo ldconfig

# libopenshot, with the tag-locked source patches (non-crop location, stream-copy
# pre-roll); both are cross-platform source fixes.
git clone --depth 1 --branch "${LIBOPENSHOT_TAG}" \
  https://github.com/OpenShot/libopenshot.git "${SRC}/libopenshot"
apply_patches "${SRC}/libopenshot" "libopenshot-${LIBOPENSHOT_TAG}"
cmake -S "${SRC}/libopenshot" -B "${SRC}/libopenshot/build" \
  -DCMAKE_INSTALL_PREFIX="${PREFIX}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_TESTS=OFF -DDISABLE_TESTS=1 \
  -DENABLE_RUBY=OFF -DENABLE_JAVA=OFF \
  -DENABLE_PYTHON=ON \
  -DENABLE_OPENCV="${ZENVI_OPENCV}" \
  -DENABLE_MAGICK=OFF \
  -DUSE_QT6=OFF
cmake --build "${SRC}/libopenshot/build" --parallel "$(nproc)"
sudo cmake --install "${SRC}/libopenshot/build"
sudo ldconfig

# Python bindings: cmake puts them in <prefix>/python (else site-packages).
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ -f "${PREFIX}/python/openshot.py" || -f "${PREFIX}/python/_openshot.so" ]]; then
  PYROOT="${PREFIX}/python"
else
  PYROOT="${PREFIX}/lib/python${PYVER}/site-packages"
fi
echo "Python bindings: ${PYROOT}"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "PYTHONPATH=${PYROOT}:${PYTHONPATH:-}" >> "${GITHUB_ENV}"
  echo "LD_LIBRARY_PATH=${PREFIX}/lib:${LD_LIBRARY_PATH:-}" >> "${GITHUB_ENV}"
fi

# Hard checks: the bindings import, and the version meets the app minimum.
PYTHONPATH="${PYROOT}:${PYTHONPATH:-}" LD_LIBRARY_PATH="${PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
  python3 -u "${WORKSPACE}/installer/verify_openshot_bundle.py"
