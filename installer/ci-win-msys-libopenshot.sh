#!/usr/bin/env bash
# MSYS2 UCRT64: unittest-cpp + libopenshot-audio + libopenshot (see README.md "MSYS2 (Windows)").
set -euo pipefail
export PATH="/ucrt64/bin:$PATH"

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

# libopenshot-audio v0.6.0 → /usr (pairs with libopenshot 0.7.x OpenShotAudio >= 0.6.0); disable ASIO (no Steinberg SDK on CI)
git clone --depth 1 --branch v0.6.0 https://github.com/OpenShot/libopenshot-audio.git "${DEPS}/libopenshot-audio"
AUDIO_SRC="${DEPS}/libopenshot-audio"
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

# libopenshot v0.7.0 → /ucrt64 + FFmpeg 7+ compat patches (upstream may already include some)
git clone --depth 1 --branch v0.7.0 https://github.com/OpenShot/libopenshot.git "${DEPS}/libopenshot"
export LOS="${DEPS}/libopenshot"
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
  -DENABLE_OPENCV=OFF \
  -DENABLE_MAGICK=OFF \
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

# avcodec loads many codec DLLs at runtime; copy the full PE dependency closure from
# /ucrt64/bin (and JUCE audio from /usr/bin) so libopenshot.dll loads on a clean PC.
bundle_transitive_pe_deps() {
  local -i iter=0 max_iter=14 added
  while (( iter < max_iter )); do
    added=0
    shopt -s nullglob
    for f in "${BUNDLE}"/*.dll; do
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

ls -la "${BUNDLE}"
touch "${BUNDLE}/.built"
