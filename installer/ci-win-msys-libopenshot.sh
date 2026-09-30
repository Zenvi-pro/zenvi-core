#!/usr/bin/env bash
# MSYS2 UCRT64: unittest-cpp + libopenshot-audio + libopenshot (see README.md "MSYS2 (Windows)").
#
# Environment:
#   LIBOPENSHOT_TAG        libopenshot tag        (default: v1.0.0)
#   LIBOPENSHOT_AUDIO_TAG  libopenshot-audio tag  (default: $LIBOPENSHOT_TAG)
#   ZENVI_OPENCV           ON|OFF, OpenCV effects (Tracker, Object Detector,
#                          Stabilizer) via mingw-w64-ucrt-x86_64-opencv (default: ON)
set -euo pipefail
export PATH="/ucrt64/bin:$PATH"

LIBOPENSHOT_TAG="${LIBOPENSHOT_TAG:-v1.0.0}"
LIBOPENSHOT_AUDIO_TAG="${LIBOPENSHOT_AUDIO_TAG:-$LIBOPENSHOT_TAG}"
ZENVI_OPENCV="${ZENVI_OPENCV:-ON}"
PATCH_DIR="${GITHUB_WORKSPACE}/installer/mac-patches"

# Apply every installer/mac-patches/<prefix>-*.patch that still applies (the
# same tag-locked, `git apply --check`-guarded scheme as
# scripts/build-mac-libopenshot.sh). Mac-only patches simply fail the check
# here and are skipped.
apply_patches() {
  local src_dir="$1" prefix="$2" patch
  shopt -s nullglob
  local patches=("${PATCH_DIR}/${prefix}"-*.patch)
  shopt -u nullglob
  for patch in "${patches[@]}"; do
    if git -C "${src_dir}" apply --check --whitespace=nowarn "${patch}" 2>/dev/null; then
      echo "Applying $(basename "${patch}")"
      git -C "${src_dir}" apply --whitespace=nowarn "${patch}"
    else
      echo "Skipping $(basename "${patch}") (does not apply to ${src_dir##*/} at this tag)"
    fi
  done
}

DEPS="${GITHUB_WORKSPACE}/.ci-deps"
mkdir -p "${DEPS}"
BUNDLE="${DEPS}/openshot-bundle"
rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}"

# unittest-cpp → /usr (README)
if [[ ! -f /usr/lib/libUnitTest++.a ]] && [[ ! -f /usr/lib/libUnitTest++.dll.a ]]; then
  git clone --depth 1 https://github.com/unittest-cpp/unittest-cpp.git "${DEPS}/unittest-cpp"
  cd "${DEPS}/unittest-cpp"
  cmake -B build -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make \
    -DCMAKE_INSTALL_PREFIX=/usr \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 .
  cmake --build build --parallel "$(nproc)"
  cmake --install build
fi

# libopenshot-audio → /usr (libopenshot 1.0.0 requires OpenShotAudio >= 1.0.0); disable ASIO (no Steinberg SDK on CI)
git clone --depth 1 --branch "${LIBOPENSHOT_AUDIO_TAG}" https://github.com/OpenShot/libopenshot-audio.git "${DEPS}/libopenshot-audio"
AUDIO_SRC="${DEPS}/libopenshot-audio"
apply_patches "${AUDIO_SRC}" "libopenshot-audio-${LIBOPENSHOT_AUDIO_TAG}"
APPCONFIG="${AUDIO_SRC}/JuceLibraryCode/AppConfig.h"
if [[ -f "${APPCONFIG}" ]]; then
  # Projucer emits indented/spaced "#define   JUCE_ASIO 1"; a naive sed misses it.
  sed -E -i 's/^[[:space:]]*#[[:space:]]*define[[:space:]]+JUCE_ASIO[[:space:]]+1/#define JUCE_ASIO 0/' "${APPCONFIG}" || true
fi
cmake -S "${AUDIO_SRC}" -B "${AUDIO_SRC}/build" \
  -G "MSYS Makefiles" \
  -DCMAKE_MAKE_PROGRAM=mingw32-make \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DCMAKE_CXX_FLAGS="-DJUCE_ASIO=0"
cmake --build "${AUDIO_SRC}/build" --parallel "$(nproc)"
cmake --install "${AUDIO_SRC}/build"

# libopenshot → /ucrt64 + FFmpeg 7+ compat patches (upstream may already include some)
git clone --depth 1 --branch "${LIBOPENSHOT_TAG}" https://github.com/OpenShot/libopenshot.git "${DEPS}/libopenshot"
export LOS="${DEPS}/libopenshot"
# Tag-locked source patches (e.g. the v1.0.0 non-crop location fix from libopenshot develop).
apply_patches "${LOS}" "libopenshot-${LIBOPENSHOT_TAG}"
find "${LOS}" \( -name "CMakeLists.txt" -o -name "*.cmake" \) -print0 | \
  xargs -0 -r grep -l "avresample" 2>/dev/null | while read -r f; do
    sed -i 's/ avresample//g' "$f"
  done || true

# FFmpeg 7/8: FF_PROFILE_*, side-data, and FFmpeg 8 AVCodec field removal
# (supported_samplerates / ch_layouts / sample_fmts / pix_fmts).
python3 "${GITHUB_WORKSPACE}/installer/patch-libopenshot-ffmpeg.py" "${LOS}"

cmake -S "${LOS}" -B "${LOS}/build" \
  -G "MSYS Makefiles" \
  -DCMAKE_MAKE_PROGRAM=mingw32-make \
  -DCMAKE_INSTALL_PREFIX=/ucrt64 \
  -DDISABLE_TESTS=1 \
  -DCMAKE_CXX_FLAGS="-include cstdint" \
  -DENABLE_TESTS=OFF \
  -DENABLE_RUBY=OFF \
  -DENABLE_JAVA=OFF \
  -DENABLE_PYTHON=ON \
  -DENABLE_OPENCV="${ZENVI_OPENCV}" \
  -DENABLE_MAGICK=OFF \
  -DUSE_QT6=OFF \
  -DPython3_EXECUTABLE=/ucrt64/bin/python.exe
mkdir -p "${LOS}/build/tests"
cmake --build "${LOS}/build" --parallel "$(nproc)"
cmake --install "${LOS}/build"

PYBIND="${LOS}/build/bindings/python"
[[ -f "${PYBIND}/openshot.py" ]] || { echo "::error::openshot.py not in ${PYBIND}"; exit 1; }
cp -v "${PYBIND}/openshot.py" "${BUNDLE}/"
shopt -s nullglob
for f in "${PYBIND}"/_openshot*.pyd; do cp -v "$f" "${BUNDLE}/"; done
shopt -u nullglob

# FFmpeg (MSYS2 UCRT): shared libs are avcodec-N.dll / avutil-N.dll / … — not libavcodec-*.dll.
for pat in \
    avcodec-*.dll avformat-*.dll avutil-*.dll swscale-*.dll swresample-*.dll \
    libavcodec-*.dll libavformat-*.dll libavutil-*.dll libswscale-*.dll libswresample-*.dll; do
  shopt -s nullglob
  for f in /ucrt64/bin/${pat}; do
    cp -v "$f" "${BUNDLE}/"
  done
  shopt -u nullglob
done

# libopenshot may link babl (ChromaKey); babl needs its DLL + typical lcms2 dependency.
for f in /ucrt64/bin/libbabl-0.1-0.dll; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done
shopt -s nullglob
for f in /ucrt64/bin/liblcms2-*.dll; do
  cp -v "$f" "${BUNDLE}/"
done
shopt -u nullglob

# jsoncpp (libopenshot links libjsoncpp-N.dll)
shopt -s nullglob
for f in /ucrt64/bin/libjsoncpp-*.dll; do
  cp -v "$f" "${BUNDLE}/"
done
shopt -u nullglob

for f in /ucrt64/bin/libopenshot*.dll; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done
for f in /usr/bin/openshot-audio.dll /usr/bin/libopenshot-audio.dll; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done
for f in /ucrt64/bin/libzmq*.dll; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done
for f in /ucrt64/bin/libwinpthread-1.dll /ucrt64/bin/libstdc++-6.dll \
         /ucrt64/bin/libgcc_s_seh-1.dll /ucrt64/bin/libgomp-1.dll; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done
[[ -e /ucrt64/bin/zlib1.dll ]] && cp -v /ucrt64/bin/zlib1.dll "${BUNDLE}/" || true
[[ -e /ucrt64/bin/libsamplerate-0.dll ]] && cp -v /ucrt64/bin/libsamplerate-0.dll "${BUNDLE}/" || true

# Gemini indexing / thumbnails / tool handlers spawn the FFmpeg CLI (not just libav*).
for f in /ucrt64/bin/ffmpeg.exe /ucrt64/bin/ffprobe.exe; do
  [[ -e "$f" ]] && cp -v "$f" "${BUNDLE}/"
done

# avcodec loads many codec DLLs at runtime; copy the full PE dependency closure from
# /ucrt64/bin (and JUCE audio from /usr/bin) so libopenshot.dll loads on a clean PC.
bundle_transitive_pe_deps() {
  local -i iter=0 max_iter=14 added
  while (( iter < max_iter )); do
    added=0
    shopt -s nullglob
    for f in "${BUNDLE}"/*.dll "${BUNDLE}"/*.exe; do
      [[ -f "$f" ]] || continue
      while IFS= read -r dllname; do
        [[ -z "$dllname" ]] && continue
        local base="${dllname##*[/\\]}"
        local lcb="${base,,}"
        [[ "$lcb" == *.dll ]] || continue
        if [[ "$lcb" == python*.dll ]] || [[ "$lcb" == libpython*.dll ]]; then
          continue
        fi
        if [[ -f "${BUNDLE}/${base}" ]]; then
          continue
        fi
        local src=""
        if [[ -f "/ucrt64/bin/${base}" ]]; then
          src="/ucrt64/bin/${base}"
        elif [[ -f "/usr/bin/${base}" ]]; then
          src="/usr/bin/${base}"
        fi
        [[ -n "$src" ]] || continue
        cp -v "$src" "${BUNDLE}/"
        added=1
      done < <(objdump -p "$f" 2>/dev/null | sed -n 's/.*DLL Name:[[:space:]]*//p')
    done
    shopt -u nullglob
    if (( added == 0 )); then
      break
    fi
    (( ++iter ))
  done
}
bundle_transitive_pe_deps

shopt -s nullglob
_avc=( "${BUNDLE}"/avcodec-*.dll "${BUNDLE}"/libavcodec-*.dll )
shopt -u nullglob
if [[ ${#_avc[@]} -eq 0 ]]; then
  echo "::error::OpenShot bundle has no avcodec DLL — libopenshot will not load. Expect avcodec-*.dll under /ucrt64/bin (MSYS2 FFmpeg)."
  exit 1
fi

shopt -s nullglob
_jcpp=( "${BUNDLE}"/libjsoncpp-*.dll )
shopt -u nullglob
if [[ ${#_jcpp[@]} -eq 0 ]]; then
  echo "::error::OpenShot bundle has no libjsoncpp DLL — install mingw-w64-ucrt-x86_64-jsoncpp and ensure /ucrt64/bin/libjsoncpp-*.dll exists."
  exit 1
fi

if [[ ! -f "${BUNDLE}/ffmpeg.exe" ]]; then
  echo "::error::OpenShot bundle has no ffmpeg.exe — Gemini indexing needs the FFmpeg CLI from /ucrt64/bin."
  exit 1
fi

# OpenCV runtime arrives through the PE dependency walk above (libopenshot.dll
# imports libopencv_core/video/dnn/tracking). Warn loudly if it is missing, so a
# silently OpenCV-less build (find_package(OpenCV 4) not found) is visible.
if [[ "${ZENVI_OPENCV}" == "ON" ]]; then
  shopt -s nullglob
  _ocv=( "${BUNDLE}"/libopencv_*.dll )
  shopt -u nullglob
  if [[ ${#_ocv[@]} -eq 0 ]]; then
    echo "::warning::ZENVI_OPENCV=ON but no libopencv_*.dll in the bundle — libopenshot was built without OpenCV (Tracker / Object Detector / Stabilizer unavailable). Check mingw-w64-ucrt-x86_64-opencv and -protobuf are installed."
  else
    echo "Bundled ${#_ocv[@]} OpenCV DLL(s)"
  fi
fi

ls -la "${BUNDLE}"
touch "${BUNDLE}/.built"
